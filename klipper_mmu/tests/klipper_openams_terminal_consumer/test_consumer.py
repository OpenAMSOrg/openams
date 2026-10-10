# SPDX-License-Identifier: GPL-3.0-or-later
"""Compose the real OpenAMS runtime with retained MMU delivery."""
from dataclasses import replace
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "klipper_openams_hook"))
sys.dont_write_bytecode = True

from host.event_delivery import EventDelivery
from host.openams_terminal_consumer import consume_terminal
from host.requests import LOAD, Requests
import test_runtime_hook as fixture

INSTANCE = 0x123456789abcdef0
SESSION = 0x10001


class TerminalConsumer(unittest.TestCase):
    S = fixture.S

    def setUp(self):
        self.case = fixture.RuntimeHook()
        self.case.setUp()
        self.runtime = self.case.runtime
        self.waiter = self.case.waiter
        self.requests = Requests(INSTANCE, SESSION)
        self.identity = self.requests.issue(LOAD, 0, "fps", self.case.gen)
        self.delivery = EventDelivery(INSTANCE, 1)

    def params(self, identity, seq=42, kind=1, code=0):
        return dict(instance_hi=INSTANCE >> 32,
                    instance_lo=INSTANCE & 0xffffffff,
                    client=identity.client, session=identity.session,
                    request=identity.request, operation=identity.operation,
                    lane=identity.lane, seq=seq, timestamp_ms=12,
                    event_type=kind, result_code=code, value=0, flags=0)

    def observe(self, identity=None, seq=42, kind=1, code=0):
        return self.delivery.observe(
            self.params(identity or self.identity, seq, kind, code),
            self.delivery.generation,
            lambda event: consume_terminal(event, self.requests,
                                           self.requests.generation,
                                           self.runtime))

    def test_success_ack_follows_exact_waiter(self):
        self.assertIsNotNone(self.observe())
        self.assertTrue(self.waiter.wait().ok)
        self.assertNotIn("fps", self.requests.pending)

    def test_failure_result_is_delivered_before_ack(self):
        self.assertIsNotNone(self.observe(kind=3, code=7))
        self.assertEqual(self.waiter.wait().code, 7)
        self.assertFalse(self.waiter.wait().ok)

    def test_stale_identity_leaves_event_and_waiter_pending(self):
        stale = replace(self.identity, request=self.identity.request + 1)
        self.assertIsNone(self.observe(identity=stale))
        self.assertFalse(self.waiter.test())
        self.assertIsNone(self.delivery.pending)
        self.assertIn("fps", self.requests.pending)

    def test_rejected_terminal_can_be_retried(self):
        # Model a physical STOP whose completion has not yet been confirmed.
        # The runtime must decline the terminal until that local gate clears.
        original = self.runtime.accept_terminal
        stop_confirmed = [False]
        calls = []

        def gated_accept(*args):
            calls.append(args)
            return original(*args) if stop_confirmed[0] else False

        self.runtime.accept_terminal = gated_accept
        head = self.params(self.identity)
        self.assertIsNone(self.observe())
        self.assertEqual(calls, [("fps", 0, 0, self.case.gen)])
        self.assertFalse(self.waiter.test())
        self.assertEqual(self.requests.pending["fps"][0], self.identity)
        self.assertIsNone(self.delivery.pending)
        self.assertEqual(self.delivery.last_acked, 0)

        # A different request, operation, or local generation cannot consume
        # the retained queue head or make an ACK eligible.
        for wrong in (replace(self.identity, request=self.identity.request + 1),
                      replace(self.identity, operation=self.identity.operation + 1)):
            self.assertIsNone(self.observe(identity=wrong))
            self.assertEqual(self.requests.pending["fps"][0], self.identity)
            self.assertIsNone(self.delivery.pending)
        wrong_gen = self.requests.generation + 1
        self.assertIsNone(self.delivery.observe(
            head, self.delivery.generation,
            lambda event: consume_terminal(event, self.requests,
                                           wrong_gen, self.runtime)))
        self.assertEqual(self.requests.pending["fps"][0], self.identity)
        self.assertIsNone(self.delivery.pending)
        self.assertFalse(self.delivery.acknowledge(dict(
            instance_hi=INSTANCE >> 32, instance_lo=INSTANCE & 0xffffffff,
            command=6, query=42, result=0), self.delivery.generation))

        stop_confirmed[0] = True
        ack = self.observe()
        self.assertIsNotNone(ack)
        self.assertEqual(ack[1][-1], head["seq"])
        self.assertTrue(self.waiter.wait().ok)
        self.assertNotIn("fps", self.requests.pending)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.delivery.observe(
            head, self.delivery.generation, lambda event: self.fail(
                "accepted head was delivered twice")), ack)
        reply = dict(instance_hi=INSTANCE >> 32,
                     instance_lo=INSTANCE & 0xffffffff,
                     command=6, query=42, result=0)
        self.assertTrue(self.delivery.acknowledge(reply, self.delivery.generation))
        self.assertFalse(self.delivery.acknowledge(reply, self.delivery.generation))
        self.assertEqual(self.delivery.last_acked, head["seq"])
        self.assertIsNone(self.delivery.pending)

    def test_auto_reload_accepts_only_exact_active_request_once(self):
        self.assertIsNotNone(self.observe())
        self.assertTrue(self.delivery.acknowledge(dict(
            instance_hi=INSTANCE >> 32, instance_lo=INSTANCE & 0xffffffff,
            command=6, query=42, result=0), 1))
        lane = self.runtime.system.lanes["fps"]
        self.runtime.system = replace(self.runtime.system, lanes={
            "fps": replace(lane, runout=self.S.RUNOUT_LOADING,
                           reload_target=(1, 1))})
        identity = self.requests.issue(LOAD, 1, "fps", lane.op_gen)
        stale = replace(identity, request=identity.request + 1)
        self.assertIsNone(self.observe(identity=stale, seq=43))
        self.assertEqual(self.runtime.system.lanes["fps"].runout,
                         self.S.RUNOUT_LOADING)
        self.assertIsNotNone(self.observe(identity=identity, seq=43))
        self.assertEqual(self.runtime.system.lanes["fps"].unit, (1, 1))
        self.assertEqual(self.runtime.system.lanes["fps"].runout,
                         self.S.RUNOUT_IDLE)
        self.assertNotIn("fps", self.requests.pending)
        applied = self.runtime.system.lanes["fps"]
        self.assertIsNotNone(self.observe(identity=identity, seq=43))
        self.assertEqual(self.runtime.system.lanes["fps"], applied)

    def test_reload_dispatch_failure_retains_head_and_request(self):
        self.assertIsNotNone(self.observe())
        self.assertTrue(self.delivery.acknowledge(dict(
            instance_hi=INSTANCE >> 32, instance_lo=INSTANCE & 0xffffffff,
            command=6, query=42, result=0), 1))
        lane = self.runtime.system.lanes["fps"]
        self.runtime.system = replace(self.runtime.system, lanes={
            "fps": replace(lane, runout=self.S.RUNOUT_LOADING,
                           reload_target=(1, 1))})
        identity = self.requests.issue(LOAD, 1, "fps", lane.op_gen)
        original = self.runtime.dispatch
        self.runtime.dispatch = lambda action: self.runtime.system
        self.assertIsNone(self.observe(identity=identity, seq=43))
        self.assertIsNone(self.delivery.pending)
        self.assertIn("fps", self.requests.pending)
        self.runtime.dispatch = original
        self.assertIsNotNone(self.observe(identity=identity, seq=43))
        self.assertNotIn("fps", self.requests.pending)

    def test_reload_terminal_for_other_bay_stays_pending(self):
        self.assertIsNotNone(self.observe())
        self.assertTrue(self.delivery.acknowledge(dict(
            instance_hi=INSTANCE >> 32, instance_lo=INSTANCE & 0xffffffff,
            command=6, query=42, result=0), 1))
        lane = self.runtime.system.lanes["fps"]
        reloading = replace(lane, runout=self.S.RUNOUT_LOADING,
                            reload_target=(1, 1))
        self.runtime.system = replace(self.runtime.system,
                                      lanes={"fps": reloading})
        wrong_bay = self.requests.issue(LOAD, 0, "fps", lane.op_gen)
        self.assertIsNone(self.observe(identity=wrong_bay, seq=43))
        self.assertEqual(self.runtime.system.lanes["fps"], reloading)
        self.assertIsNone(self.delivery.pending)
        self.assertEqual(self.requests.pending["fps"][0], wrong_bay)


if __name__ == "__main__":
    unittest.main()
