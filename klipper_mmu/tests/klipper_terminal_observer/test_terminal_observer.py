# SPDX-License-Identifier: GPL-3.0-or-later
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from host.protocol import Identity
from host.terminal_observer import query_terminal

INSTANCE = 0x1020304050607080
IDENTITY = Identity(1, 100, 200, 1, 2)


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
        self.mcu.respond()


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
    record = dict(instance_hi=INSTANCE >> 32, instance_lo=INSTANCE & 0xffffffff,
                  seq=3, timestamp_ms=400, client=1, session=100, request=200,
                  operation=1, lane=2, event_type=1, result_code=0,
                  value=5, flags=0)
    record.update(changes)
    return record


def result(**changes):
    record = dict(instance_hi=INSTANCE >> 32, instance_lo=INSTANCE & 0xffffffff,
                  command=7, query=200, result=4)
    record.update(changes)
    return record


class TerminalQueryTest(unittest.TestCase):
    def run_query(self, respond, current=lambda: True):
        reactor = Reactor()
        mcu = MCU(lambda: respond(mcu))
        try:
            value = query_terminal(mcu, reactor, INSTANCE, IDENTITY, current)
            return value, mcu
        finally:
            self.assertEqual(mcu.listeners, [])

    def test_matching_terminal(self):
        value, mcu = self.run_query(lambda m: m.emit('km_event', event()))
        self.assertEqual(value.identity, IDENTITY)
        self.assertEqual(value.instance, INSTANCE)
        self.assertEqual(value.event_type, 1)
        self.assertEqual(mcu.sent, [[INSTANCE >> 32, INSTANCE & 0xffffffff,
                                     1, 100, 200, 1, 2]])

    def test_history_miss_is_unknown(self):
        value, _ = self.run_query(lambda m: m.emit('km_command_result', result()))
        self.assertIsNone(value)

    def test_old_instance_and_unrelated_request_are_ignored(self):
        def respond(m):
            m.emit('km_event', event(instance_lo=1))
            m.emit('km_event', event(request=201))
            m.emit('km_command_result', result(instance_lo=1))
            m.emit('km_event', event())
        value, _ = self.run_query(respond)
        self.assertEqual(value.seq, 3)

    def test_malformed_matching_event_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Invalid seq'):
            self.run_query(lambda m: m.emit('km_event', event(seq='bad')))

    def test_wrong_terminal_type_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Unexpected terminal event type'):
            self.run_query(lambda m: m.emit('km_event', event(event_type=4)))

    def test_follow_fault_is_observable(self):
        reactor = Reactor()
        mcu = MCU(lambda: mcu.emit('km_event', event(operation=3, event_type=7,
                                                    result_code=1)))
        found = query_terminal(mcu, reactor, INSTANCE,
                               Identity(1, 100, 200, 3, 2), lambda: True)
        self.assertEqual(found.event_type, 7)
        self.assertEqual(mcu.listeners, [])

    def test_rejection_not_success(self):
        with self.assertRaisesRegex(ValueError, 'result 2'):
            self.run_query(lambda m: m.emit('km_command_result', result(result=2)))

    def test_disconnect_during_send(self):
        live = [True]
        def respond(m):
            live[0] = False
            m.emit('km_event', event())
        with self.assertRaisesRegex(ValueError, 'Stale terminal response'):
            self.run_query(respond, lambda: live[0])

    def test_timeout(self):
        with self.assertRaisesRegex(ValueError, 'timed out'):
            self.run_query(lambda m: None)


if __name__ == '__main__':
    unittest.main()
