# SPDX-License-Identifier: GPL-3.0-or-later
from pathlib import Path
import threading
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from host.protocol import Event, Identity
from host.requests import Requests, EJECT, UNLOAD
from host.terminal_observer import _TERMINAL_TYPES
from host.command_transport import send_operation
from host.event_delivery import EventDelivery

INSTANCE = 0x123456789abcdef0


def event(identity, kind, code=0, instance=INSTANCE):
    return Event(instance, identity, 1, 100, kind, code, 0, 0)


class EjectIdentityTest(unittest.TestCase):
    def setUp(self):
        self.requests = Requests(INSTANCE, 101)
        self.identity = self.requests.issue_eject(2)
        self.generation = self.requests.generation

    def test_exact_terminal_and_distinct_unload(self):
        self.assertEqual(self.identity.operation, EJECT)
        self.assertEqual(_TERMINAL_TYPES[EJECT], (16, 17, 18))
        wrong = Identity(1, 101, self.identity.request, UNLOAD, 2)
        for candidate in (event(wrong, 4), event(self.identity, 16, instance=1),
                          event(Identity(1, 101, self.identity.request, EJECT, 3), 16),
                          event(self.identity, 15)):
            self.assertFalse(self.requests.accept_eject(candidate, self.generation))
        self.assertIsNone(self.requests.eject_terminal)
        terminal = event(self.identity, 16)
        self.assertTrue(self.requests.accept_eject(terminal, self.generation))
        self.assertEqual(self.requests.eject_terminal, terminal)
        self.assertFalse(self.requests.accept_eject(terminal, self.generation))

    def test_cancel_failure_and_stale_generation(self):
        self.assertFalse(self.requests.accept_eject(event(self.identity, 17),
                                                    self.generation + 1))
        with self.assertRaisesRegex(ValueError, 'Contradictory'):
            self.requests.accept_eject(event(self.identity, 16, 2), self.generation)
        self.assertTrue(self.requests.accept_eject(event(self.identity, 18, 5),
                                                  self.generation))
        self.assertEqual(self.requests.eject_terminal.result_code, 5)

    def test_retired_exact_terminal(self):
        self.requests.disconnect()
        with self.assertRaisesRegex(ValueError, 'Unsupported retired'):
            self.requests.accept_retired_local(event(self.identity, 4))
        terminal = event(self.identity, 17, 6)
        self.assertTrue(self.requests.accept_retired_local(terminal))
        self.assertEqual(self.requests.eject_terminal, terminal)
        self.assertIsNone(self.requests.pending_eject)

    def test_local_request_is_serialized(self):
        with self.assertRaisesRegex(ValueError, 'Existing local request'):
            self.requests.issue_unpreload(2)
        with self.assertRaisesRegex(ValueError, 'Existing local request'):
            self.requests.issue_preload(2)


class Completion:
    def __init__(self):
        self.value = None
    def test(self):
        return self.value is not None
    def complete(self, value):
        self.value = value
    def wait(self, deadline):
        return self.value


class Reactor:
    def completion(self):
        return Completion()
    def monotonic(self):
        return 1.0
    def register_async_callback(self, callback):
        callback(1.0)


class FakeMCU:
    def __init__(self, responses):
        self.responses = responses
        self.handlers = []
        self.sent = []
    def lookup_command(self, fmt):
        mcu = self
        class Command:
            def send(self, args):
                mcu.sent.append((fmt.split()[0], args))
                for kind, params in mcu.responses:
                    for registered_kind, callback in list(mcu.handlers):
                        if registered_kind == kind:
                            callback(params)
        return Command()
    def register_serial_response(self, callback, fmt):
        item = (fmt.split()[0], callback)
        self.handlers.append(item)
        mcu = self
        class Wrapper:
            def unregister(self):
                mcu.handlers.remove(item)
        return Wrapper()


class EjectTransportTest(unittest.TestCase):
    def test_wrong_identity_ignored_then_exact_admission_and_cancel(self):
        identity = Identity(1, 101, 5, EJECT, 2)
        base = dict(instance_hi=INSTANCE >> 32, instance_lo=INSTANCE & 0xffffffff,
                    client=1, session=101, request=5, operation=EJECT, lane=2,
                    admission=0, fault=0)
        mcu = FakeMCU([('km_admission', dict(base, lane=3)),
                       ('km_admission', base)])
        reply = send_operation(mcu, Reactor(), INSTANCE, identity, lambda: True,
                               direction=0)
        self.assertEqual(reply.identity, identity)
        self.assertEqual(mcu.sent, [('km_start', [INSTANCE >> 32,
            INSTANCE & 0xffffffff, 1, 101, 5, 7, 2, 0])])
        mcu.responses = [('km_admission', base)]
        reply = send_operation(mcu, Reactor(), INSTANCE, identity, lambda: True,
                               cancel=True)
        self.assertEqual(reply.identity, identity)
        self.assertEqual(mcu.sent[-1][0], 'km_cancel')
        self.assertEqual(mcu.sent[-1][1][-3:], [5, 7, 2])
        self.assertEqual(mcu.handlers, [])


class EjectRegistrationTest(unittest.TestCase):
    def test_default_off_registration(self):
        import types
        from unittest.mock import patch
        from host import KlipperMMU

        class Config:
            def __init__(self, printer, motion):
                self.printer, self.motion = printer, motion
            def get_printer(self):
                return self.printer
            def get_name(self):
                return 'klipper_mmu box'
            def get(self, key, default=None):
                return dict(openams_idx='1', openams_fps='fps',
                            openams_family='boxturtle', openams_bays='4',
                            openams_path_length_mm='800',
                            openams_host_watchdog_ms='1000',
                            openams_lease_max_age_ms='400').get(key, default)
            def getboolean(self, key, default=False):
                return self.motion if key in ('openams_motion',
                                              'diagnostic_startup') else default

        class Printer:
            config_error = ValueError
            def __init__(self):
                self.mcu = types.SimpleNamespace(register_config_callback=lambda cb: None)
                self.commands = []
            def lookup_objects(self, name):
                return []
            def lookup_object(self, name):
                return types.SimpleNamespace(register_mux_command=lambda *a, **k:
                                             self.commands.append(a[0]))
            get_reactor = lambda self: types.SimpleNamespace(mutex=threading.Lock)
            def register_event_handler(self, *args):
                pass

        with patch.dict(sys.modules, {'mcu': types.SimpleNamespace(
                get_printer_mcu=lambda p, n: p.mcu)}):
            for motion in (False, True):
                printer = Printer()
                KlipperMMU(Config(printer, motion))
                self.assertEqual('KLIPPER_MMU_EJECT' in printer.commands, motion)
