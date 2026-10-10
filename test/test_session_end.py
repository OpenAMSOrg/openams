"""H9b: in-flight ops settle on ``klippy:shutdown`` / ``klippy:disconnect``.

Klipper stopping or disconnecting means the firmware has already stopped all
motion, but the host may still hold a lane in an in-flight op for the 300 s
backstop and then send ``CancelLoad`` to a dead MCU.  ``Runtime.end_session``
releases every waiter and every deadline at once and forbids every later
command; these tests drive that on the same fakes as ``test_oams_runtime``.

Run with either of::

    python3 -m unittest discover -s test -p 'test_session_end.py' -v
    python3 -m unittest discover -s test
"""

import importlib
import os
import sys
import unittest

_TEST = os.path.dirname(os.path.abspath(__file__))
if _TEST not in sys.path:
    sys.path.insert(0, _TEST)

from test_oams_runtime import FPS, UNIT, make_runtime  # noqa: E402

S = importlib.import_module("oamspkg.oams_state")

REASON = "Klipper shut down"


class EndSessionTests(unittest.TestCase):
    def test_pending_waiter_settles_with_the_reason_and_deadlines_are_cancelled(self):
        runtime, reactor, oam, events = make_runtime()
        completion = reactor.completion()
        runtime._pending[FPS] = completion
        runtime._arm_deadline(FPS, 300.0)
        self.assertEqual(len(reactor.timers), 1)

        runtime.end_session(REASON)

        self.assertTrue(completion.test())
        result = completion.wait()
        self.assertFalse(result.ok)
        self.assertIsNone(result.code)
        self.assertIn(REASON, result.message)
        self.assertIn("FIRMWARE_RESTART", result.message)
        self.assertEqual(runtime._pending, {})
        self.assertEqual(runtime._deadlines, {})
        self.assertEqual(reactor.timers, [])
        self.assertEqual(oam.calls, [])          # nothing was sent

    def test_end_session_is_idempotent(self):
        runtime, reactor, oam, events = make_runtime()
        runtime._pending[FPS] = reactor.completion()
        runtime.end_session(REASON)
        runtime.end_session("Klipper disconnected")   # must not raise
        self.assertEqual(oam.calls, [])

    def test_no_effect_that_sends_is_applied_after_the_session_ends(self):
        runtime, reactor, oam, events = make_runtime()
        runtime.end_session(REASON)

        runtime._apply(S.StartLoad(UNIT, FPS, gen=1))
        runtime._apply(S.StartUnload(UNIT, FPS, gen=1))
        runtime._apply(S.StartCalibrate(UNIT, "ptfe", FPS, gen=1))
        runtime._apply(S.CancelLoad(UNIT))
        runtime._apply(S.SetFollower(UNIT, 1, 0, FPS))
        runtime._apply(S.Pause(FPS, "reason"))
        runtime._apply(S.ArmDeadline(FPS, 300.0))

        self.assertEqual(oam.calls, [])
        self.assertEqual([e for e in events if e[0] in ("script", "command_script")], [])
        self.assertEqual(runtime._deadlines, {})

    def test_settle_and_cancel_deadline_still_release_state_after_end_session(self):
        runtime, reactor, oam, events = make_runtime()
        runtime.end_session(REASON)
        completion = reactor.completion()
        runtime._pending[FPS] = completion
        runtime._apply(S.Settle(FPS, S.OpResult(True, 0, "ok")))
        self.assertTrue(completion.test())
        self.assertTrue(completion.wait().ok)
        runtime._apply(S.CancelDeadline(FPS))

    def test_request_after_end_session_is_refused_without_dispatching(self):
        runtime, reactor, oam, events = make_runtime()
        before = runtime.get_state()
        runtime.end_session("Klipper disconnected")

        completion = runtime.request(FPS, S.Unload(FPS))

        self.assertTrue(completion.test())
        result = completion.wait()
        self.assertFalse(result.ok)
        self.assertIn("Klipper disconnected", result.message)
        self.assertIn("FIRMWARE_RESTART", result.message)
        self.assertIs(runtime.get_state(), before)     # reducer state untouched
        self.assertEqual(runtime._pending, {})
        self.assertEqual(oam.calls, [])


if __name__ == "__main__":
    unittest.main()
