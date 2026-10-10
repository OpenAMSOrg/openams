# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise the installed ams-family reducer/runtime against MMU host helpers."""
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.dont_write_bytecode = True
from host.event_delivery import EventDelivery
from host.protocol import Admission
from host.requests import LOAD, Requests

sys.path.insert(0, str(ROOT / "tests"))
from openams_source import load

S, runtime_module = load("oams_state", "oams_runtime")
Runtime = runtime_module.Runtime

INSTANCE = 0x123456789abcdef0
SESSION = 0x10001
FPS = "fps1"
UNIT = (1, 0)


class Completion:
    def __init__(self):
        self.result = None

    def test(self):
        return self.result is not None

    def complete(self, result):
        self.result = result

    def wait(self):
        return self.result


class Reactor:
    NEVER = 1e99

    def __init__(self):
        self.now = 0.0
        self.timers = []
        self.async_callbacks = []

    def monotonic(self):
        return self.now

    def completion(self):
        return Completion()

    def register_timer(self, callback, when):
        timer = (callback, when)
        self.timers.append(timer)
        return timer

    def unregister_timer(self, timer):
        self.timers.remove(timer)

    def register_async_callback(self, callback):
        self.async_callbacks.append(callback)


class Printer:
    def __init__(self, reactor):
        self.reactor = reactor

    def get_reactor(self):
        return self.reactor

    def lookup_object(self, name):
        raise AssertionError("Unexpected gcode effect: " + name)


class OamEffects:
    def __init__(self):
        self.calls = []
        self.encoder_clicks = 0

    def start_load_spool(self, bay, gen=None):
        self.calls.append(("load", bay, gen))

    def load_spool_cancel(self):
        self.calls.append(("cancel",))

    def set_oams_follower(self, enable, direction):
        self.calls.append(("follow", enable, direction))


def event_params(identity, seq, kind=1, code=0):
    return dict(instance_hi=INSTANCE >> 32, instance_lo=INSTANCE & 0xffffffff,
                client=identity.client, session=identity.session,
                request=identity.request, operation=identity.operation,
                lane=identity.lane, seq=seq, timestamp_ms=12,
                event_type=kind, result_code=code, value=0, flags=0)


class RuntimeContract(unittest.TestCase):
    def setUp(self):
        self.reactor = Reactor()
        self.oam = OamEffects()
        self.runtime = Runtime(
            Printer(self.reactor), [FPS],
            lambda now: S.World(lanes={FPS: S.LaneWorld()}),
            lambda idx: self.oam if idx == UNIT[0] else None)
        self.requests = Requests(INSTANCE, SESSION)
        self.delivery = EventDelivery(INSTANCE, 1)

    def begin(self):
        waiter = self.runtime.request(FPS, S.LoadBay(FPS, UNIT))
        self.waiter = waiter
        token = self.oam.calls[-1][2]
        identity = self.requests.issue(LOAD, UNIT[1], FPS, token)
        return waiter, identity, token

    def retained(self, identity, seq, kind=1, code=0):
        def settle(fps, result, value, token):
            self.runtime.dispatch(S.OpCompleted(fps, result, value, token))
            # Runtime.dispatch logs and swallows reducer/effect failures. A
            # normal return therefore does not prove the waiter was settled.
            if not self.waiter.test():
                raise RuntimeError("OpenAMS waiter did not settle")

        def consume(event):
            match = self.requests.event(event, self.requests.generation)
            return self.requests.deliver(match, settle)
        return self.delivery.observe(event_params(identity, seq, kind, code),
                                     self.delivery.generation, consume)

    def test_load_waiter_settles_only_after_retained_terminal(self):
        waiter, identity, token = self.begin()
        self.assertEqual(self.oam.calls, [("load", 0, token)])
        self.assertIsNone(self.requests.admission(
            Admission(INSTANCE, identity, 0, 0), self.requests.generation))
        self.assertFalse(waiter.test())
        ack = self.retained(identity, 42)
        self.assertEqual(ack[1][3:7], [SESSION, identity.request, LOAD, 0])
        self.assertTrue(waiter.wait().ok)
        self.assertEqual(self.runtime.get_state().lanes[FPS].op, S.OP_LOADED)
        self.assertEqual(self.delivery.observe(event_params(identity, 42), 1,
                         lambda _: self.fail("duplicate terminal")), ack)
        self.assertTrue(self.delivery.acknowledge(dict(
            instance_hi=INSTANCE >> 32, instance_lo=INSTANCE & 0xffffffff,
            command=6, query=42, result=0), 1))

    def test_failed_load_can_be_retried_with_new_identity(self):
        first, old, _ = self.begin()
        self.assertIsNotNone(self.retained(old, 42, kind=3, code=7))
        self.assertFalse(first.wait().ok)
        self.assertEqual(first.wait().code, 7)
        self.assertTrue(self.delivery.acknowledge(dict(
            instance_hi=INSTANCE >> 32, instance_lo=INSTANCE & 0xffffffff,
            command=6, query=42, result=0), 1))
        second, new, _ = self.begin()
        self.assertNotEqual(old, new)
        self.assertIsNone(self.retained(old, 43))
        self.assertFalse(second.test())
        self.assertIsNotNone(self.retained(new, 44))
        self.assertTrue(second.wait().ok)

    def test_superseded_match_cannot_settle_new_waiter(self):
        first, old, _ = self.begin()
        # The runtime settles the old waiter first; its reducer then rejects
        # a second load while the first operation is still active.
        second = self.runtime.request(FPS, S.LoadBay(FPS, UNIT))
        self.assertFalse(first.wait().ok)
        self.assertFalse(second.wait().ok)
        # A replacement identity for the same FPS invalidates a delayed match.
        new = self.requests.issue(LOAD, UNIT[1], FPS, 2)
        self.assertIsNone(self.retained(old, 42))
        self.assertIsNone(self.delivery.pending)
        self.assertNotEqual(old, new)

    def test_swallowed_runtime_failure_never_prepares_ack(self):
        waiter, identity, _ = self.begin()
        original_dispatch = self.runtime.dispatch
        self.runtime.dispatch = lambda action: self.runtime.get_state()
        with self.assertRaisesRegex(RuntimeError, "did not settle"):
            self.retained(identity, 42)
        self.assertFalse(waiter.test())
        self.assertIsNone(self.delivery.pending)
        self.assertIn(FPS, self.requests.pending)
        self.runtime.dispatch = original_dispatch
        self.assertIsNotNone(self.retained(identity, 42))
        self.assertTrue(waiter.wait().ok)


if __name__ == "__main__":
    unittest.main()
