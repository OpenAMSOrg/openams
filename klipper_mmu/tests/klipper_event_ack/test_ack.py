# SPDX-License-Identifier: GPL-3.0-or-later
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from host.event_delivery import EventDelivery, send_pending_ack

INSTANCE = 0x123456789abcdef0


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
    def __init__(self, mcu, callback):
        self.mcu, self.callback = mcu, callback

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
        self.sent = []
        self.listeners = []

    def lookup_command(self, fmt):
        self.command_format = fmt
        return Command(self)

    def register_serial_response(self, callback, fmt):
        self.result_format = fmt
        wrapper = Wrapper(self, callback)
        self.listeners.append(wrapper)
        return wrapper

    def emit(self, params):
        for wrapper in list(self.listeners):
            wrapper.callback(params)


def event(**changes):
    params = dict(instance_hi=INSTANCE >> 32, instance_lo=INSTANCE & 0xffffffff,
                  seq=42, timestamp_ms=100, client=1, session=65537,
                  request=65538, operation=1, lane=2, event_type=1,
                  result_code=0, value=0, flags=0)
    params.update(changes)
    return params


def reply(**changes):
    params = dict(instance_hi=INSTANCE >> 32, instance_lo=INSTANCE & 0xffffffff,
                  command=6, query=42, result=0)
    params.update(changes)
    return params


class AckTest(unittest.TestCase):
    def setUp(self):
        self.delivery = EventDelivery(INSTANCE, 5)

    def send(self, respond, current=lambda: True):
        mcu = MCU(respond)
        try:
            value = send_pending_ack(mcu, Reactor(), self.delivery, 5, current)
            return value, mcu
        finally:
            self.assertEqual(mcu.listeners, [])

    def accepted(self):
        return self.delivery.observe(event(), 5, lambda _: True)

    def test_exact_ack_after_consumer_acceptance(self):
        self.accepted()
        value, mcu = self.send(lambda m: m.emit(reply()))
        self.assertTrue(value)
        self.assertEqual(mcu.sent, [[INSTANCE >> 32, INSTANCE & 0xffffffff,
                                     1, 65537, 65538, 1, 2, 42]])
        self.assertEqual(self.delivery.last_acked, 42)
        self.assertIsNone(self.delivery.pending)

    def test_refused_consumer_never_sends(self):
        self.assertIsNone(self.delivery.observe(event(), 5, lambda _: False))
        mcu = MCU(lambda _: self.fail('sent without accepted head'))
        with self.assertRaisesRegex(ValueError, 'No current accepted event'):
            send_pending_ack(mcu, Reactor(), self.delivery, 5, lambda: True)
        self.assertEqual(mcu.sent, [])

    def test_unrelated_replies_ignored(self):
        self.accepted()
        def respond(m):
            for bad in (reply(instance_lo=1), reply(command=5), reply(query=43)):
                m.emit(bad)
            m.emit(reply())
        self.send(respond)
        self.assertEqual(self.delivery.last_acked, 42)

    def test_rejected_ack_keeps_pending(self):
        self.accepted()
        with self.assertRaisesRegex(ValueError, 'result 4'):
            self.send(lambda m: m.emit(reply(result=4)))
        self.assertEqual(self.delivery.pending.seq, 42)

    def test_timeout_keeps_pending(self):
        self.accepted()
        with self.assertRaisesRegex(ValueError, 'timed out'):
            self.send(lambda m: None)
        self.assertEqual(self.delivery.pending.seq, 42)

    def test_disconnect_during_send_is_stale(self):
        self.accepted()
        def respond(m):
            self.delivery.disconnect()
            m.emit(reply())
        with self.assertRaisesRegex(ValueError, 'Stale ACK response'):
            self.send(respond)
        self.assertEqual(self.delivery.last_acked, 0)

    def test_lost_host_generation_is_stale(self):
        self.accepted()
        current = [True]
        def respond(m):
            current[0] = False
            m.emit(reply())
        with self.assertRaisesRegex(ValueError, 'Stale ACK response'):
            self.send(respond, lambda: current[0])
        self.assertEqual(self.delivery.pending.seq, 42)


if __name__ == '__main__':
    unittest.main()
