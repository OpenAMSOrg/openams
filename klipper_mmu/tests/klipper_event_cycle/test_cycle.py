# SPDX-License-Identifier: GPL-3.0-or-later
from dataclasses import replace
from contextlib import contextmanager
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests/klipper_openams_hook'))
sys.dont_write_bytecode = True

from host.event_cycle import run_event_cycle
from host.event_delivery import EventDelivery
from host.operation_stats import OperationStats
from host.protocol import decode_event
from host.requests import LOAD, UNLOAD, Requests
import test_runtime_hook as fixture

INSTANCE = 0x123456789abcdef0
SESSION = 0x10001


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
        self.now = 1.0

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


class Command:
    def __init__(self, mcu, name):
        self.mcu, self.name = mcu, name

    def send(self, args):
        self.mcu.sent.append((self.name, args))
        self.mcu.respond(self.mcu, self.name)


class MCU:
    def __init__(self, respond):
        self.respond = respond
        self.sent = []
        self.listeners = []

    def lookup_command(self, fmt):
        return Command(self, fmt.split()[0])

    def register_serial_response(self, callback, fmt):
        wrapper = Wrapper(self, callback, fmt.split()[0])
        self.listeners.append(wrapper)
        return wrapper

    def emit(self, name, params):
        for wrapper in list(self.listeners):
            if wrapper.name == name:
                wrapper.callback(params)


class EventCycleTest(unittest.TestCase):
    def setUp(self):
        self.case = fixture.RuntimeHook()
        self.case.setUp()
        self.runtime = self.case.runtime
        self.waiter = self.case.waiter
        self.requests = Requests(INSTANCE, SESSION)
        self.identity = self.requests.issue(LOAD, 0, 'fps', self.case.gen)
        self.delivery = EventDelivery(INSTANCE, 1)
        self.reactor = Reactor()

    def event(self, **changes):
        identity = self.identity
        params = dict(instance_hi=INSTANCE >> 32,
                      instance_lo=INSTANCE & 0xffffffff,
                      seq=42, timestamp_ms=12, client=identity.client,
                      session=identity.session, request=identity.request,
                      operation=identity.operation, lane=identity.lane,
                      event_type=1, result_code=0, value=0, flags=0)
        params.update(changes)
        return params

    def run_cycle(self, respond, current=lambda: True, stats=None):
        mcu = MCU(respond)
        try:
            result = run_event_cycle(mcu, self.reactor, self.delivery,
                                     self.requests, self.runtime, current,
                                     timeout=0.1, stats=stats)
            return result, mcu
        finally:
            self.assertEqual(mcu.listeners, [])

    def test_terminal_settles_runtime_then_acks_exact_head(self):
        stats = OperationStats()
        def respond(mcu, name):
            if name == 'km_event_peek':
                mcu.emit('km_event', self.event())
            else:
                self.assertEqual(name, 'km_event_ack')
                self.assertTrue(self.waiter.test())
                mcu.emit('km_command_result', dict(
                    instance_hi=INSTANCE >> 32,
                    instance_lo=INSTANCE & 0xffffffff,
                    command=6, query=42, result=0))
        result, mcu = self.run_cycle(respond, stats=stats)
        self.assertEqual(result, 'acked')
        self.assertTrue(self.waiter.wait().ok)
        self.assertEqual(self.delivery.last_acked, 42)
        self.assertEqual(stats.snapshot(), {('load', 0, 'success'): 1})
        self.assertEqual([name for name, _ in mcu.sent],
                         ['km_event_peek', 'km_event_ack'])

    def test_empty_queue_does_not_ack(self):
        result, mcu = self.run_cycle(lambda m, name: m.emit(
            'km_command_result', dict(instance_hi=INSTANCE >> 32,
                                      instance_lo=INSTANCE & 0xffffffff,
                                      command=5, query=0, result=5)))
        self.assertEqual(result, 'empty')
        self.assertEqual([name for name, _ in mcu.sent], ['km_event_peek'])

    def test_unmatched_head_blocks_without_ack(self):
        result, mcu = self.run_cycle(lambda m, name: m.emit(
            'km_event', self.event(request=self.identity.request + 1)))
        self.assertEqual(result, 'blocked')
        self.assertEqual([name for name, _ in mcu.sent], ['km_event_peek'])
        self.assertFalse(self.waiter.test())

    def test_unpreload_exact_terminal_acks_without_runtime_waiter(self):
        identity = self.requests.issue_unpreload(2)
        def respond(mcu, name):
            if name == 'km_event_peek':
                mcu.emit('km_event', self.event(
                    request=identity.request, operation=UNLOAD, lane=2,
                    event_type=4))
            else:
                self.assertEqual(name, 'km_event_ack')
                mcu.emit('km_command_result', dict(
                    instance_hi=INSTANCE >> 32,
                    instance_lo=INSTANCE & 0xffffffff,
                    command=6, query=42, result=0))
        result, mcu = self.run_cycle(respond)
        self.assertEqual(result, 'acked')
        self.assertEqual([name for name, _ in mcu.sent],
                         ['km_event_peek', 'km_event_ack'])
        self.assertIsNone(self.requests.pending_unpreload)
        self.assertEqual(self.requests.unpreload_terminal.identity, identity)
        self.assertFalse(self.waiter.test())

    def test_foreign_unload_does_not_consume_unpreload_or_ack(self):
        identity = self.requests.issue_unpreload(2)
        result, mcu = self.run_cycle(lambda m, name: m.emit(
            'km_event', self.event(request=identity.request + 1,
                                   operation=UNLOAD, lane=2, event_type=4)))
        self.assertEqual(result, 'blocked')
        self.assertEqual([name for name, _ in mcu.sent], ['km_event_peek'])
        self.assertEqual(self.requests.pending_unpreload, identity)
        self.assertIsNone(self.requests.unpreload_terminal)

    def test_ordinary_unload_routes_to_runtime_waiter(self):
        identity = self.requests.issue(UNLOAD, 2, 'fps', self.case.gen)
        accepted = []
        self.runtime.accept_terminal = lambda *args: accepted.append(args) or True
        def respond(mcu, name):
            if name == 'km_event_peek':
                mcu.emit('km_event', self.event(
                    request=identity.request, operation=UNLOAD, lane=2,
                    event_type=4))
            else:
                mcu.emit('km_command_result', dict(
                    instance_hi=INSTANCE >> 32,
                    instance_lo=INSTANCE & 0xffffffff,
                    command=6, query=42, result=0))
        result, mcu = self.run_cycle(respond)
        self.assertEqual(result, 'acked')
        self.assertEqual(accepted, [('fps', 0, 0, self.case.gen)])
        self.assertNotIn('fps', self.requests.pending)
        self.assertEqual(len(mcu.sent), 2)

    def test_unpreload_lost_ack_keeps_exact_pending_head(self):
        identity = self.requests.issue_unpreload(2)
        stats = OperationStats()
        def respond(mcu, name):
            if name == 'km_event_peek':
                mcu.emit('km_event', self.event(
                    request=identity.request, operation=UNLOAD, lane=2,
                    event_type=4))
        with self.assertRaisesRegex(ValueError, 'ACK timed out'):
            self.run_cycle(respond, stats=stats)
        self.assertEqual(stats.snapshot(), {})
        self.assertEqual(self.delivery.pending.identity, identity)
        self.assertEqual(self.delivery.last_acked, 0)
        self.assertIsNone(self.requests.pending_unpreload)
        self.assertEqual(self.requests.unpreload_terminal.identity, identity)
        def confirmed(mcu, name):
            if name == 'km_event_peek':
                mcu.emit('km_event', self.event(
                    request=identity.request, operation=UNLOAD, lane=2,
                    event_type=4))
            else:
                mcu.emit('km_command_result', dict(
                    instance_hi=INSTANCE >> 32,
                    instance_lo=INSTANCE & 0xffffffff,
                    command=6, query=42, result=0))
        self.assertEqual(self.run_cycle(confirmed, stats=stats)[0], 'acked')
        self.assertEqual(stats.snapshot(), {('unload', 2, 'success'): 1})

    def test_rejected_runtime_keeps_head_and_waiter(self):
        self.runtime.accept_terminal = lambda *args: False
        result, mcu = self.run_cycle(lambda m, name: m.emit('km_event', self.event()))
        self.assertEqual(result, 'blocked')
        self.assertEqual([name for name, _ in mcu.sent], ['km_event_peek'])
        self.assertIn('fps', self.requests.pending)

    def test_failed_synchronous_stop_withholds_ack_after_waiter_settles(self):
        mcu = MCU(lambda m, name: m.emit('km_event', self.event()))

        @contextmanager
        def boundary():
            yield
            # OpenAMS can swallow an effect exception and still settle the
            # waiter. The observer's boundary reports that failed STOP here.
            raise RuntimeError('OpenAMS follower STOP failed during terminal')

        with self.assertRaisesRegex(RuntimeError, 'STOP failed'):
            run_event_cycle(mcu, self.reactor, self.delivery, self.requests,
                            self.runtime, lambda: True, 0.1, boundary)
        self.assertTrue(self.waiter.test())
        self.assertEqual([name for name, _ in mcu.sent], ['km_event_peek'])
        self.assertEqual(self.delivery.pending.seq, 42)
        self.assertFalse(self.delivery.ack_eligible)
        self.assertEqual(self.delivery.last_acked, 0)

        # The settled waiter cannot stand in for a successful STOP. Repeated
        # peeks of the same retained head must remain blocked without ACK.
        for _ in range(2):
            result, repeated = self.run_cycle(
                lambda m, name: m.emit('km_event', self.event()))
            self.assertEqual(result, 'blocked')
            self.assertEqual([name for name, _ in repeated.sent],
                             ['km_event_peek'])
            self.assertEqual(self.delivery.pending.seq, 42)
            self.assertFalse(self.delivery.ack_eligible)

        self.assertFalse(self.delivery.acknowledge(dict(
            instance_hi=INSTANCE >> 32, instance_lo=INSTANCE & 0xffffffff,
            command=6, query=42, result=0), 1))

    def test_ack_failure_stays_pending_for_reconciliation(self):
        def respond(mcu, name):
            if name == 'km_event_peek':
                mcu.emit('km_event', self.event())
            else:
                mcu.emit('km_command_result', dict(
                    instance_hi=INSTANCE >> 32,
                    instance_lo=INSTANCE & 0xffffffff,
                    command=6, query=42, result=4))
        with self.assertRaisesRegex(ValueError, 'ACK rejected'):
            self.run_cycle(respond)
        self.assertEqual(self.delivery.pending.seq, 42)
        self.assertTrue(self.delivery.ack_eligible)
        self.assertEqual(self.delivery.last_acked, 0)

    def test_ack_timeout_retries_after_completed_boundary(self):
        def lost_reply(mcu, name):
            if name == 'km_event_peek':
                mcu.emit('km_event', self.event())

        with self.assertRaisesRegex(ValueError, 'ACK timed out'):
            self.run_cycle(lost_reply)
        self.assertTrue(self.delivery.ack_eligible)
        self.assertTrue(self.waiter.test())

        def retry_reply(mcu, name):
            if name == 'km_event_peek':
                mcu.emit('km_event', self.event())
            else:
                mcu.emit('km_command_result', dict(
                    instance_hi=INSTANCE >> 32,
                    instance_lo=INSTANCE & 0xffffffff,
                    command=6, query=42, result=0))

        result, mcu = self.run_cycle(retry_reply)
        self.assertEqual(result, 'acked')
        self.assertEqual([name for name, _ in mcu.sent],
                         ['km_event_peek', 'km_event_ack'])
        self.assertEqual(self.delivery.last_acked, 42)

    def test_empty_after_unconfirmed_ack_requires_reconciliation(self):
        def first_response(mcu, name):
            if name == 'km_event_peek':
                mcu.emit('km_event', self.event())
            # Firmware may consume the ACK even if the reply is lost.
        with self.assertRaisesRegex(ValueError, 'ACK timed out'):
            self.run_cycle(first_response)
        self.assertEqual(self.delivery.pending.seq, 42)

        def empty_response(mcu, name):
            self.assertEqual(name, 'km_event_peek')
            mcu.emit('km_command_result', dict(
                instance_hi=INSTANCE >> 32,
                instance_lo=INSTANCE & 0xffffffff,
                command=5, query=0, result=5))
        with self.assertRaisesRegex(ValueError, 'reconcile'):
            self.run_cycle(empty_response)
        self.assertEqual(self.delivery.pending.seq, 42)
        self.assertEqual(self.delivery.last_acked, 0)

    def test_older_head_after_unconfirmed_ack_requires_reconciliation(self):
        self.delivery.last_acked = 41

        def first_response(mcu, name):
            if name == 'km_event_peek':
                mcu.emit('km_event', self.event())
            # The ACK response is lost, so sequence 42 remains pending.
        with self.assertRaisesRegex(ValueError, 'ACK timed out'):
            self.run_cycle(first_response)
        self.assertEqual(self.delivery.pending.seq, 42)

        def older_response(mcu, name):
            self.assertEqual(name, 'km_event_peek')
            mcu.emit('km_event', self.event(seq=41))
        with self.assertRaisesRegex(RuntimeError, 'Queue head changed'):
            self.run_cycle(older_response)
        self.assertEqual(self.delivery.pending.seq, 42)
        self.assertEqual(self.delivery.last_acked, 41)

    def test_request_generation_change_during_peek_is_stale(self):
        def respond(mcu, name):
            self.requests.disconnect()
            mcu.emit('km_event', self.event())
        with self.assertRaisesRegex(ValueError, 'Stale event peek response'):
            self.run_cycle(respond)
        self.assertFalse(self.waiter.test())
        self.assertIsNone(self.delivery.pending)

    def test_observe_accepts_decoded_event_and_rejects_malformed_typed_event(self):
        item = decode_event(self.event())
        self.assertIsNotNone(self.delivery.observe(item, 1, lambda _: True))
        self.assertEqual(self.delivery.pending, item)
        other = EventDelivery(INSTANCE, 1)
        with self.assertRaisesRegex(ValueError, 'Invalid retained event field'):
            other.observe(replace(item, seq=True), 1, lambda _: True)


if __name__ == '__main__':
    unittest.main()
