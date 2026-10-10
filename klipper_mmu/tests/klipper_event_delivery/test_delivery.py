# SPDX-License-Identifier: GPL-3.0-or-later
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from host.event_delivery import EventDelivery
from host.requests import LOAD, Requests


INSTANCE = 0x123456789abcdef0


def event(**changes):
    params = dict(instance_hi=INSTANCE >> 32, instance_lo=INSTANCE & 0xffffffff,
                  seq=42, timestamp_ms=100, client=1, session=0x10001,
                  request=0x10002, operation=1, lane=2, event_type=1,
                  result_code=0, value=0, flags=0)
    params.update(changes)
    return params


def result(**changes):
    params = dict(instance_hi=INSTANCE >> 32, instance_lo=INSTANCE & 0xffffffff,
                  command=6, query=42, result=0)
    params.update(changes)
    return params


class EventDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.delivery = EventDelivery(INSTANCE, 5)
        self.seen = []

    def accept(self, item):
        self.seen.append(item)
        return True

    def test_full_identity_and_ack_after_acceptance(self):
        self.assertEqual(self.delivery.peek_command(),
                         ("km_event_peek instance_hi=%u instance_lo=%u",
                          [305419896, 2596069104]))
        command = self.delivery.observe(event(), 5, self.accept)
        self.assertEqual(command,
                         ("km_event_ack instance_hi=%u instance_lo=%u client=%c "
                          "session=%u request=%u operation=%c lane=%c seq=%u",
                          [305419896, 2596069104, 1, 65537, 65538, 1, 2, 42]))
        self.assertEqual(len(self.seen), 1)
        self.assertTrue(self.delivery.acknowledge(result(), 5))
        self.assertIsNone(self.delivery.observe(event(), 5, self.accept))
        self.assertEqual(len(self.seen), 1)

    def test_rejected_or_throwing_consumer_is_not_acked(self):
        self.assertIsNone(self.delivery.observe(event(), 5, lambda _: False))
        self.assertIsNone(self.delivery.pending)
        def fail(_):
            raise RuntimeError("consumer failed")
        with self.assertRaisesRegex(RuntimeError, "consumer failed"):
            self.delivery.observe(event(), 5, fail)
        self.assertIsNone(self.delivery.pending)
        self.assertIsNotNone(self.delivery.observe(event(), 5, self.accept))

    def test_duplicate_head_retries_ack_without_reprocessing(self):
        command = self.delivery.observe(event(), 5, self.accept)
        self.assertEqual(self.delivery.observe(event(), 5, self.accept), command)
        self.assertEqual(len(self.seen), 1)
        for bad in (result(query=41), result(command=5), result(result=4),
                    result(instance_lo=0)):
            self.assertFalse(self.delivery.acknowledge(bad, 5))
        self.assertEqual(self.delivery.observe(event(), 5, self.accept), command)
        with self.assertRaisesRegex(RuntimeError, "head changed"):
            self.delivery.observe(event(request=999), 5, self.accept)
        self.assertEqual(len(self.seen), 1)

    def test_boundary_pending_requires_explicit_completion(self):
        self.assertIsNone(self.delivery.observe(event(), 5, self.accept,
                                                ack_eligible=False))
        self.assertEqual(self.delivery.pending.seq, 42)
        self.assertFalse(self.delivery.ack_eligible)
        self.assertIsNone(self.delivery.observe(event(), 5, self.accept))
        self.assertFalse(self.delivery.acknowledge(result(), 5))
        self.assertEqual(len(self.seen), 1)
        self.delivery.complete_consumer_boundary(self.delivery.pending, 5)
        self.assertIsNotNone(self.delivery.observe(event(), 5, self.accept))
        self.assertTrue(self.delivery.acknowledge(result(), 5))

    def test_stale_generation_or_instance_cannot_deliver_or_ack(self):
        self.assertIsNone(self.delivery.observe(event(), 4, self.accept))
        self.assertIsNone(self.delivery.observe(event(instance_lo=1), 5, self.accept))
        self.assertEqual(self.seen, [])
        self.delivery.observe(event(), 5, self.accept)
        self.assertFalse(self.delivery.acknowledge(result(), 4))
        self.delivery.disconnect()
        self.assertFalse(self.delivery.acknowledge(result(), 5))
        self.assertIsNone(self.delivery.observe(event(), 5, self.accept))
        with self.assertRaises(ConnectionError):
            self.delivery.peek_command()

    def test_sequence_wrap_keeps_new_head_and_ignores_old(self):
        self.assertIsNotNone(self.delivery.observe(event(seq=0xffffffff), 5, self.accept))
        self.assertTrue(self.delivery.acknowledge(result(query=0xffffffff), 5))
        self.assertIsNotNone(self.delivery.observe(event(seq=1), 5, self.accept))
        self.assertTrue(self.delivery.acknowledge(result(query=1), 5))
        self.assertIsNone(self.delivery.observe(event(seq=0xffffffff), 5, self.accept))
        self.assertEqual(len(self.seen), 2)

    def test_stale_malformed_reply_does_not_affect_new_generation(self):
        self.assertIsNone(self.delivery.observe({"seq": "bad"}, 4, self.accept))
        self.assertFalse(self.delivery.acknowledge({"result": "bad"}, 4))

    def test_malformed_event_never_reaches_consumer(self):
        for bad in (event(seq=0), event(event_type=0), event(event_type=21),
                    event(result_code=8), event(flags=2), event(lane=-1),
                    event(operation=4, event_type=9, flags=2),
                    event(operation=4, event_type=8, flags=4),
                    event(session=True), event(request=1 << 32)):
            with self.assertRaises(ValueError):
                self.delivery.observe(bad, 5, self.accept)
        self.assertEqual(self.seen, [])

    def test_step_estimated_calibration_reaches_consumer(self):
        self.assertIsNotNone(self.delivery.observe(
            event(operation=4, event_type=8, value=12345, flags=2), 5, self.accept))
        self.assertEqual((self.seen[0].value, self.seen[0].flags), (12345, 2))

    def test_requests_terminal_delivery_ack_and_stale_identity(self):
        requests = Requests(INSTANCE, 0x10001, last_request=0x10001)
        identity = requests.issue(LOAD, 2, waiter="tool0", token=17)
        callbacks = []
        should_fail = [True]

        def settle(waiter, code, value, token):
            if should_fail[0]:
                raise RuntimeError("OpenAMS callback failed")
            callbacks.append((waiter, code, value, token))

        def consume(retained):
            completion = requests.event(retained, requests.generation)
            return requests.deliver(completion, settle)

        terminal = event(session=identity.session, request=identity.request,
                         operation=identity.operation, lane=identity.lane)
        stale = event(request=identity.request + 1)
        self.assertIsNone(self.delivery.observe(stale, 5, consume))
        self.assertIsNone(self.delivery.pending)
        with self.assertRaisesRegex(RuntimeError, "callback failed"):
            self.delivery.observe(terminal, 5, consume)
        self.assertIsNone(self.delivery.pending)
        self.assertIn("tool0", requests.pending)

        should_fail[0] = False
        command = self.delivery.observe(terminal, 5, consume)
        self.assertEqual(command[1][3:7], [identity.session, identity.request,
                                          identity.operation, identity.lane])
        self.assertEqual(callbacks, [("tool0", 0, 0, 17)])
        self.assertEqual(self.delivery.observe(terminal, 5, consume), command)
        self.assertEqual(len(callbacks), 1)
        self.assertTrue(self.delivery.acknowledge(result(), 5))
        self.assertIsNone(self.delivery.observe(terminal, 5, consume))
        self.assertEqual(len(callbacks), 1)


if __name__ == "__main__":
    unittest.main()
