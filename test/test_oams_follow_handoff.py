"""Focused tests for the reverse FOLLOW/toolhead unload handoff."""

import types
import unittest

from test_oams_manager import FakeError, FakeGcmd, OAMSManager, S


class FollowHandoffTests(unittest.TestCase):
    def _manager(self, lane, on_follow=None, on_pause=None):
        mgr = OAMSManager.__new__(OAMSManager)
        mgr.toolhead_load_macro = "LOAD_FILAMENT"
        mgr.toolhead_unload_macro = "UNLOAD_FILAMENT"
        state = {"lane": lane, "now": 0.0}
        calls = []

        def dispatch(action):
            calls.append(action)
            if on_follow:
                on_follow(state, action)

        def pause(until):
            state["now"] = until
            if on_pause:
                on_pause(state, until)

        mgr.reactor = types.SimpleNamespace(
            monotonic=lambda: state["now"], pause=pause)
        mgr.runtime = types.SimpleNamespace(
            get_state=lambda: S.SystemState(lanes={"fps1": state["lane"]}),
            dispatch=dispatch)
        mgr._resolve_fps = lambda gcmd, prefer_op, required: "fps1"
        mgr._run_toolhead_macro = lambda name: calls.append(name)
        mgr.cmd_UNLOAD_FILAMENT = lambda gcmd: calls.append("ams_unload")
        mgr._stop_followers = lambda lanes: calls.append(("stop", lanes))
        return mgr, state, calls

    @staticmethod
    def _lane(following=True, direction=S.FOLLOWER_FORWARD,
              uncertain=()):
        return S.LaneState(op=S.OP_LOADED, unit=(1, 0), group="T0",
                           following=following, direction=direction,
                           uncertain_follower_units=uncertain)

    def _run(self, mgr):
        mgr.cmd_UNLOAD_FROM_TOOLHEAD(FakeGcmd({"FPS": "fps1"}))

    def test_admission_waits_for_delayed_verified_confirmation(self):
        def admitted(state, action):
            state["lane"] = self._lane(
                following=None, direction=S.FOLLOWER_REVERSE,
                uncertain=((1, 0),))

        def confirm(state, now):
            if now >= 0.1:
                state["lane"] = self._lane(
                    following=True, direction=S.FOLLOWER_REVERSE)

        mgr, state, calls = self._manager(
            self._lane(), on_follow=admitted, on_pause=confirm)
        self._run(mgr)
        self.assertEqual(calls[1:], ["UNLOAD_FILAMENT", "ams_unload"])

    def test_rejection_times_out_and_stops_without_running_macro(self):
        def reject(state, action):
            state["lane"] = self._lane(
                following=None, direction=S.FOLLOWER_REVERSE,
                uncertain=((1, 0),))

        mgr, state, calls = self._manager(self._lane(), on_follow=reject)
        with self.assertRaisesRegex(FakeError, "was not confirmed"):
            self._run(mgr)
        self.assertEqual(calls[1:], [("stop", ["fps1"])])
        self.assertNotIn("UNLOAD_FILAMENT", calls)
        self.assertNotIn("ams_unload", calls)

    def test_failed_dispatch_cannot_reuse_stale_reverse_confirmation(self):
        old_lane = self._lane(following=True, direction=S.FOLLOWER_REVERSE)
        mgr, state, calls = self._manager(old_lane)
        with self.assertRaisesRegex(FakeError, "was not confirmed"):
            self._run(mgr)
        self.assertIs(state["lane"], old_lane)
        self.assertEqual(calls[1:], [("stop", ["fps1"])])
        self.assertNotIn("UNLOAD_FILAMENT", calls)


if __name__ == "__main__":
    unittest.main()
