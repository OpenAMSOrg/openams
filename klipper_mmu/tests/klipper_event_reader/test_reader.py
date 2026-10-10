# SPDX-License-Identifier: GPL-3.0-or-later
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from host.event_reader import peek_event

INSTANCE = 0x1020304050607080


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
        self.time = 1.0

    def completion(self):
        return Completion()

    def monotonic(self):
        return self.time

    def register_async_callback(self, callback):
        callback(self.time)


class Wrapper:
    def __init__(self, mcu, callback, name):
        self.mcu, self.callback, self.name = mcu, callback, name

    def unregister(self):
        self.mcu.listeners.remove(self)


class Command:
    def __init__(self, mcu):
        self.mcu = mcu

    def send(self, args):
        self.mcu.sent.append(args)
        self.mcu.respond(self.mcu)


class MCU:
    def __init__(self, respond):
        self.respond = respond
        self.listeners = []
        self.sent = []

    def lookup_command(self, message):
        self.command_format = message
        return Command(self)

    def register_serial_response(self, callback, message):
        wrapper = Wrapper(self, callback, message.split()[0])
        self.listeners.append(wrapper)
        return wrapper

    def emit(self, message, params):
        for wrapper in list(self.listeners):
            if wrapper.name == message:
                wrapper.callback(params)


def event(**changes):
    params = dict(instance_hi=INSTANCE >> 32, instance_lo=INSTANCE & 0xffffffff,
                  seq=3, timestamp_ms=400, client=1, session=100, request=200,
                  operation=1, lane=2, event_type=1, result_code=0,
                  value=5, flags=0)
    params.update(changes)
    return params


def result(**changes):
    params = dict(instance_hi=INSTANCE >> 32, instance_lo=INSTANCE & 0xffffffff,
                  command=5, query=0, result=5)
    params.update(changes)
    return params


class EventReaderTest(unittest.TestCase):
    def read(self, respond, current=lambda: True):
        mcu = MCU(respond)
        try:
            value = peek_event(mcu, Reactor(), INSTANCE, current)
            return value, mcu
        finally:
            self.assertEqual(mcu.listeners, [])

    def test_retained_event_and_exact_command(self):
        value, mcu = self.read(lambda m: m.emit('km_event', event()))
        self.assertEqual(value.seq, 3)
        self.assertEqual(value.instance, INSTANCE)
        self.assertEqual(mcu.sent, [[INSTANCE >> 32, INSTANCE & 0xffffffff]])
        self.assertEqual(mcu.command_format,
                         'km_event_peek instance_hi=%u instance_lo=%u')

    def test_preload_terminals_reach_event_delivery(self):
        for event_type in (13, 14, 15):
            with self.subTest(event_type=event_type):
                value, _ = self.read(lambda m: m.emit('km_event',
                                     event(operation=6, event_type=event_type)))
                self.assertEqual(value.event_type, event_type)
                self.assertEqual(value.identity.operation, 6)

    def test_empty_queue(self):
        value, _ = self.read(lambda m: m.emit('km_command_result', result()))
        self.assertIsNone(value)

    def test_unrelated_replies_are_ignored(self):
        def respond(m):
            m.emit('km_event', event(instance_lo=1))
            m.emit('km_command_result', result(command=7))
            m.emit('km_command_result', result(query=8))
            m.emit('km_command_result', result(instance_lo=1))
            m.emit('km_event', event())
        value, _ = self.read(respond)
        self.assertEqual(value.seq, 3)

    def test_stale_instance_reported_with_new_controller_identity(self):
        with self.assertRaisesRegex(ValueError, 'stale controller'):
            self.read(lambda m: m.emit('km_command_result',
                                       result(instance_lo=1, result=2)))

    def test_malformed_or_unsupported_head_is_error(self):
        for changes in ({'seq': 'bad'}, {'event_type': 21}, {'flags': 2},
                        {'operation': 4, 'event_type': 10, 'flags': 2},
                        {'operation': 4, 'event_type': 8, 'flags': 4}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.read(lambda m: m.emit('km_event', event(**changes)))

    def test_step_estimate_only_on_calibration_success(self):
        value, _ = self.read(lambda m: m.emit('km_event', event(
            operation=4, event_type=8, value=12345, flags=3)))
        self.assertEqual((value.event_type, value.value, value.flags), (8, 12345, 3))

    def test_rejected_command_is_error(self):
        with self.assertRaisesRegex(ValueError, 'result 6'):
            self.read(lambda m: m.emit('km_command_result', result(result=6)))

    def test_disconnect_during_send_is_error(self):
        current = [True]
        def respond(m):
            current[0] = False
            m.emit('km_event', event())
        with self.assertRaisesRegex(ValueError, 'Stale event peek response'):
            self.read(respond, lambda: current[0])

    def test_timeout_cleans_up(self):
        with self.assertRaisesRegex(ValueError, 'timed out'):
            self.read(lambda m: None)


if __name__ == '__main__':
    unittest.main()
