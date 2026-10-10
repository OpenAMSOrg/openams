# SPDX-License-Identifier: GPL-3.0-or-later
"""Pinned Klipper wire integration for the read-only terminal observer.

Run with an explicit timeout; this never opens a printer serial device.
"""
import threading
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[2]
WORKSPACE = ROOT.parent
UPSTREAM = Path(os.environ.get('KM_WIRE_KLIPPER_SOURCE',
                               WORKSPACE / 'build/klipper-menuconfig'))
DICTIONARY = Path(os.environ.get('KM_WIRE_DICTIONARY',
                                 WORKSPACE / 'build/calibration-build-matrix/diagnostic_diff/out/klipper.dict'))
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
from host.protocol import Identity
from host.terminal_observer import query_terminal

PINNED = {
    'klippy/mcu.py': '36c47e1f910abfdd1670b323af752fa4a44afdf8ea21a936664f067b1e35c23b',
    'klippy/msgproto.py': 'c205bb7d70d968b5fbe8bbb86247a125994eb5d7acde16660afb2471672d6278',
}
for relative, expected in PINNED.items():
    path = UPSTREAM / relative
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise RuntimeError('Pinned Klipper source changed: ' + relative)
spec = importlib.util.spec_from_file_location('pinned_msgproto', UPSTREAM / 'klippy/msgproto.py')
msgproto = importlib.util.module_from_spec(spec)
spec.loader.exec_module(msgproto)


def selected(source, names):
    tree = ast.parse(source.read_text())
    nodes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name in names]
    if {node.name for node in nodes} != set(names):
        raise RuntimeError('Klipper wrapper selection changed')
    namespace = {}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), namespace)
    return namespace


klipper = selected(UPSTREAM / 'klippy/mcu.py', {'CommandWrapper', 'AsyncResponseWrapper'})
INSTANCE = 0x87654321fedcba98
IDENTITY = Identity(1, 0xabcdef01, 0x87654321, 1, 7)
dictionary = json.loads(DICTIONARY.read_text())


class Completion:
    def __init__(self, reactor):
        self.reactor = reactor
        self.value = None

    def test(self):
        return self.value is not None

    def complete(self, value):
        self.value = value

    def wait(self, deadline):
        while self.reactor.pending:
            callback = self.reactor.pending.pop(0)
            callback(self.reactor.now)
        if self.value is None:
            self.reactor.now = deadline
        return self.value


class Reactor:
    def __init__(self):
        self.now = 1.0
        self.pending = []

    def monotonic(self):
        return self.now

    def completion(self):
        return Completion(self)

    def register_async_callback(self, callback):
        self.pending.append(callback)

    def mutex(self):
        return threading.Lock()


class Serial:
    def __init__(self, reactor):
        self.reactor = reactor
        self.parser = msgproto.MessageParser()
        self.parser.process_identify(json.dumps(dictionary).encode(), decompress=False)
        self.callbacks = {}
        self.sent = []
        self.replies = []
        self.after_send = None

    def get_msgparser(self):
        return self.parser

    def get_default_command_queue(self):
        return 'default'

    def register_response(self, callback, name, oid=None):
        if callback is None:
            self.callbacks.pop((name, oid), None)
        else:
            self.callbacks[name, oid] = callback

    def raw_send(self, wire, *args):
        self.raw_send_wait_ack(wire, *args)

    def raw_send_wait_ack(self, wire, *args):
        tag, _ = self.parser.msgid_parser.parse(wire, 0)
        fmt = self.parser.messages_by_id[tag]
        params, end = fmt.parse(wire, 0)
        assert end == len(wire) and fmt.name == 'km_terminal_query'
        self.sent.append(params)
        for name, fields in self.replies:
            response = self.parser.messages_by_name[name]
            frame = response.encode_by_name(**fields)
            decoded, end = response.parse(frame, 0)
            assert end == len(frame)
            callback = self.callbacks.get((name, None))
            if callback:
                callback(decoded)
        if self.after_send:
            self.after_send()


class MCU:
    def __init__(self, reactor):
        self.serial = Serial(reactor)
        self.conn = types.SimpleNamespace(get_serial=lambda: self.serial,
                                          get_mcu=lambda: self)
        self.cfg = types.SimpleNamespace(is_config_finalized=lambda: True)

    def is_fileoutput(self):
        return False

    def lookup_command(self, fmt):
        return klipper['CommandWrapper'](self.conn, fmt)

    def try_lookup_command(self, fmt):  # Same as klippy/mcu.py's MCU.
        try:
            return self.lookup_command(fmt)
        except self.serial.get_msgparser().error:
            return None

    def register_serial_response(self, callback, fmt):
        return klipper['AsyncResponseWrapper'](self.conn, self.cfg, callback, fmt)


def event(identity=IDENTITY, instance=INSTANCE, **changes):
    fields = dict(instance_hi=instance >> 32, instance_lo=instance & 0xffffffff,
                  seq=0x76543210, timestamp_ms=123, client=identity.client,
                  session=identity.session, request=identity.request,
                  operation=identity.operation, lane=identity.lane,
                  event_type=1, result_code=0, value=42, flags=0)
    fields.update(changes)
    return ('km_event', fields)


def miss(identity=IDENTITY):
    return ('km_command_result', dict(instance_hi=INSTANCE >> 32,
                                      instance_lo=INSTANCE & 0xffffffff,
                                      command=7, query=identity.request, result=4))


class TerminalPinnedTest(unittest.TestCase):
    def setUp(self):
        self.reactor = Reactor()
        self.mcu = MCU(self.reactor)
        self.current = True

    def query(self):
        try:
            return query_terminal(self.mcu, self.reactor, INSTANCE, IDENTITY,
                                  lambda: self.current, timeout=0.25)
        finally:
            self.assertEqual(self.mcu.serial.callbacks, {})

    def test_real_command_encode_response_parse_and_full_identity(self):
        wrong = Identity(IDENTITY.client, IDENTITY.session, IDENTITY.request + 1,
                         IDENTITY.operation, IDENTITY.lane)
        self.mcu.serial.replies = [event(wrong), event(instance=INSTANCE + 1), event()]
        result = self.query()
        self.assertEqual((result.instance, result.identity, result.value),
                         (INSTANCE, IDENTITY, 42))
        sent = self.mcu.serial.sent
        self.assertEqual(len(sent), 1)
        self.assertEqual((sent[0]['instance_hi'], sent[0]['instance_lo'],
                          sent[0]['session'], sent[0]['request'], sent[0]['lane']),
                         (INSTANCE >> 32, INSTANCE & 0xffffffff,
                          IDENTITY.session, IDENTITY.request, IDENTITY.lane))

    def test_history_miss_and_malformed_matched_event(self):
        self.mcu.serial.replies = [miss()]
        self.assertIsNone(self.query())
        self.mcu.serial.replies = [event(seq=0)]
        with self.assertRaisesRegex(ValueError, 'Event sequence'):
            self.query()

    def test_timeout_and_connection_change_cleanup(self):
        with self.assertRaisesRegex(ValueError, 'timed out'):
            self.query()
        self.assertEqual(self.reactor.now, 1.25)
        self.mcu.serial.replies = [event()]
        self.current = False
        with self.assertRaisesRegex(ValueError, 'connection changed'):
            self.query()
        self.assertEqual(len(self.mcu.serial.sent), 1)

    def test_queued_reply_dropped_after_disconnect_during_send(self):
        self.mcu.serial.replies = [event()]
        self.mcu.serial.after_send = lambda: setattr(self, 'current', False)
        with self.assertRaisesRegex(ValueError, 'Stale terminal response'):
            self.query()
        self.assertEqual(len(self.mcu.serial.sent), 1)


if __name__ == '__main__':
    unittest.main()
