# SPDX-License-Identifier: GPL-3.0-or-later
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from host.command_transport import send_heartbeat, send_operation, send_stop
from host.protocol import Admission, CommandResult, Identity

INSTANCE = 0x1020304050607080
ID = Identity(1, 8, 9, 1, 2)


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
    def __init__(self):
        self.now = 1.

    def completion(self):
        return Completion()

    def monotonic(self):
        return self.now

    def register_async_callback(self, callback):
        callback(self.now)


class Wrapper:
    def __init__(self, mcu, callback, name):
        self.mcu, self.callback, self.name = mcu, callback, name

    def unregister(self):
        self.mcu.listeners.remove(self)


class MCU:
    def __init__(self, respond):
        self.respond = respond
        self.listeners = []
        self.sent = []

    def lookup_command(self, fmt):
        self.command = fmt
        return self

    def send(self, args):
        self.sent.append(args)
        self.respond(self)

    def register_serial_response(self, callback, fmt):
        wrapper = Wrapper(self, callback, fmt.split()[0])
        self.listeners.append(wrapper)
        return wrapper

    def emit(self, name, params):
        for wrapper in list(self.listeners):
            if wrapper.name == name:
                wrapper.callback(params)


def admission(**changes):
    params = dict(instance_hi=INSTANCE >> 32, instance_lo=INSTANCE & 0xffffffff,
                  client=1, session=8, request=9, operation=1, lane=2,
                  admission=0, fault=0)
    params.update(changes)
    return params


def result(command=1, query=9, **changes):
    params = dict(instance_hi=INSTANCE >> 32, instance_lo=INSTANCE & 0xffffffff,
                  command=command, query=query, result=0)
    params.update(changes)
    return params


class TransportTest(unittest.TestCase):
    def run_call(self, respond, call, current=lambda: True):
        mcu = MCU(respond)
        try:
            return call(mcu, Reactor(), current), mcu
        finally:
            self.assertEqual(mcu.listeners, [])

    def test_start_exact_identity_and_admission(self):
        def respond(m):
            m.emit('km_admission', admission(request=10))
            m.emit('km_admission', admission(instance_hi=1))
            m.emit('km_admission', admission())
        reply, mcu = self.run_call(respond, lambda m, r, c:
                                   send_operation(m, r, INSTANCE, ID, c, direction=1))
        self.assertIsInstance(reply, Admission)
        self.assertEqual(reply.identity, ID)
        self.assertEqual(mcu.sent, [[INSTANCE >> 32, INSTANCE & 0xffffffff,
                                     1, 8, 9, 1, 2, 1]])

    def test_cancel_preserves_rejection(self):
        reply, mcu = self.run_call(
            lambda m: m.emit('km_admission', admission(admission=8)),
            lambda m, r, c: send_operation(m, r, INSTANCE, ID, c, cancel=True))
        self.assertEqual(reply.admission, 8)
        self.assertEqual(len(mcu.sent[0]), 7)

    def test_administrative_rejection_is_distinct(self):
        reply, _ = self.run_call(
            lambda m: m.emit('km_command_result', result(result=7)),
            lambda m, r, c: send_operation(m, r, INSTANCE, ID, c, direction=0))
        self.assertIsInstance(reply, CommandResult)
        self.assertEqual(reply.result, 7)

    def test_heartbeat_and_stop_results(self):
        beat, _ = self.run_call(
            lambda m: m.emit('km_command_result', result(3, 8, result=4)),
            lambda m, r, c: send_heartbeat(m, r, INSTANCE, 8, c))
        self.assertEqual(beat.result, 4)
        stop, mcu = self.run_call(
            lambda m: m.emit('km_command_result', result(4, 0)),
            lambda m, r, c: send_stop(m, r, INSTANCE, c))
        self.assertEqual(stop.result, 0)
        self.assertEqual(len(mcu.sent[0]), 2)

    def test_stale_malformed_and_timeout_cleanup(self):
        def call(m, r, c):
            return send_operation(m, r, INSTANCE, ID, c, direction=1)
        for response, message in (
                (lambda m: m.emit('km_admission', admission(admission=11)), 'Unsupported admission'),
                (lambda m: m.emit('km_admission', admission(fault=255)), 'Unsupported admission'),
                (lambda m: m.emit('km_command_result', result(instance_hi=1)), 'instance changed'),
                (lambda m: m.emit('km_command_result', result()), 'success requires admission'),
                (lambda m: m.emit('km_command_result', result(result=8)), 'Unsupported command result'),
                (lambda m: None, 'timed out')):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                self.run_call(response, call)

    def test_unrelated_result_ignored_and_connection_guard(self):
        def respond(m):
            m.emit('km_command_result', result(command=3))
            m.emit('km_command_result', result(result=7))
        reply, _ = self.run_call(respond, lambda m, r, c:
                                 send_operation(m, r, INSTANCE, ID, c, direction=0))
        self.assertEqual(reply.command, 1)
        with self.assertRaisesRegex(ValueError, 'connection changed'):
            self.run_call(lambda m: None, lambda m, r, c:
                          send_stop(m, r, INSTANCE, c), lambda: False)


if __name__ == '__main__':
    unittest.main()
