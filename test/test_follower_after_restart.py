"""A loaded lane is usable again after a Klipper restart without manual
commands: the manager re-establishes the forward follower once, only when the
lane is provably idle and healthy."""

import logging
import types
import unittest

from test_oams_manager import (FakeFps, manager_mod, FakeRuntime, S, build_manager,
                               single_lane_objects, single_lane_sections)

FOLLOWER_RETRY_BACKOFF = manager_mod.FOLLOWER_RETRY_BACKOFF


class Runtime(FakeRuntime):
    """The real reducer; SetFollower effects reach the unit like the real one."""

    def __init__(self, lanes, manager):
        super().__init__(lanes)
        self.manager = manager
        self.actions = []

    def dispatch(self, action):
        self.actions.append(action)
        self._sys, effects = S.reduce(self._sys, action, S.World(), 0.0)
        for e in effects:
            if isinstance(e, S.SetFollower):
                oam = self.manager.oam_by_idx[e.unit[0]]
                response = oam.set_oams_follower(e.enable, e.direction)
                if getattr(response, "follower_refused", False):
                    self.dispatch(S.FollowerApplied(e.fps, e.unit, False))
                elif getattr(response, "follower_confirmed", True) is False:
                    self.dispatch(S.FollowerFailed(e.fps, e.unit))
                else:
                    self.dispatch(S.FollowerApplied(e.fps, e.unit,
                                                    bool(e.enable)))


def restarted_manager(following=None, **lane_kw):
    lane = S.LaneState(op=S.OP_LOADED, unit=(1, 0), group="T0",
                       following=following, **lane_kw)
    mgr = build_manager(single_lane_objects(), single_lane_sections(),
                        lanes={"fps1": lane})
    mgr.runtime = Runtime({"fps1": lane}, mgr)
    unit = mgr.oams["oams1"]
    unit.oams_load_spool_cmd = object()       # connected
    unit.hardware_snapshot_ready = True
    unit.sent = []
    unit.response = None

    def set_follower(enable, direction):
        unit.sent.append((enable, direction))
        return unit.response
    unit.set_oams_follower = set_follower
    mgr.fpss["fps1"].extruder_name = "extruder"
    mgr._slot_id_by_unit = lambda: {}
    mgr.ready = True
    mgr._startup_resync_pending = False
    mgr._follower_rearm = {}
    mgr._follower_gave_up = set()
    mgr._heat_waiting = set()
    mgr.idle_timeout = types.SimpleNamespace(
        get_status=lambda now: {"state": "Idle"})
    return mgr, unit


def mgr_backoff(n):
    return FOLLOWER_RETRY_BACKOFF[n]


def lane_of(mgr):
    return mgr.runtime.get_state().lanes["fps1"]


class RestartFollowerTests(unittest.TestCase):
    def test_loaded_lane_gets_forward_follower_once(self):
        mgr, unit = restarted_manager()
        lane = lane_of(mgr)
        self.assertFalse(mgr._motion_settled("fps1", lane))
        self.assertIsNotNone(mgr._lane_status("fps1", lane)
                             ["motion_blocked_reason"])
        with self.assertLogs(level=logging.INFO) as logs:
            mgr._reestablish_followers_after_restart()
        self.assertEqual(unit.sent, [(1, S.FOLLOWER_FORWARD)])
        lane = lane_of(mgr)
        self.assertIs(lane.following, True)
        self.assertEqual(lane.direction, S.FOLLOWER_FORWARD)
        self.assertTrue(mgr._motion_settled("fps1", lane))
        self.assertNotIn("motion_blocked_reason",
                         mgr._lane_status("fps1", lane))
        self.assertTrue(any("fps1" in m and "re-established the forward"
                            in m for m in logs.output))
        mgr._reestablish_followers_after_restart()
        self.assertEqual(len(unit.sent), 1)

    def _assert_untouched(self, mgr, unit):
        mgr._reestablish_followers_after_restart()
        self.assertEqual(unit.sent, [])
        self.assertIsNone(lane_of(mgr).following)

    def test_unit_disconnected(self):
        mgr, unit = restarted_manager()
        unit.oams_load_spool_cmd = None
        self._assert_untouched(mgr, unit)

    def test_unit_faulted(self):
        mgr, unit = restarted_manager()
        mgr.runtime._sys = S.SystemState(
            lanes=dict(mgr.runtime.get_state().lanes),
            firmware_faults={1: ("fps1", "JAM", ("x",))})
        self._assert_untouched(mgr, unit)

    def test_uncertain_unit(self):
        mgr, unit = restarted_manager(uncertain_follower_units=((1, 0),))
        self._assert_untouched(mgr, unit)

    def test_printing(self):
        mgr, unit = restarted_manager()
        mgr.idle_timeout = types.SimpleNamespace(
            get_status=lambda now: {"state": "Printing"})
        self._assert_untouched(mgr, unit)

    def test_not_ready_or_resync_pending(self):
        mgr, unit = restarted_manager()
        mgr.ready = False
        self._assert_untouched(mgr, unit)
        mgr.ready = True
        mgr._startup_resync_pending = True
        self._assert_untouched(mgr, unit)

    def test_runout_in_flight(self):
        mgr, unit = restarted_manager(runout=S.RUNOUT_LOADING)
        self._assert_untouched(mgr, unit)

    def test_snapshot_missing(self):
        mgr, unit = restarted_manager()
        unit.hardware_snapshot_ready = False
        self._assert_untouched(mgr, unit)

    def test_unit_connects_later(self):
        mgr, unit = restarted_manager()
        unit.oams_load_spool_cmd = None
        self._assert_untouched(mgr, unit)
        self._assert_untouched(mgr, unit)       # still waiting, no attempt used
        unit.oams_load_spool_cmd = object()
        mgr._reestablish_followers_after_restart()
        self.assertEqual(unit.sent, [(1, S.FOLLOWER_FORWARD)])
        self.assertIs(lane_of(mgr).following, True)

    def _clock(self, mgr, start=100.0):
        now = [start]
        mgr.reactor = types.SimpleNamespace(monotonic=lambda: now[0])
        return now

    def test_refusal_stops_then_retries_with_backoff_and_recovers(self):
        mgr, unit = restarted_manager()
        now = self._clock(mgr)
        unit.response = types.SimpleNamespace(follower_refused=True)
        with self.assertLogs(level=logging.WARNING):
            mgr._reestablish_followers_after_restart()
        self.assertEqual(unit.sent[0], (1, S.FOLLOWER_FORWARD))
        # The refusal leaves a known stopped state: the UI can act at once.
        lane = lane_of(mgr)
        self.assertIs(lane.following, False)
        self.assertTrue(mgr._motion_settled("fps1", lane))
        # Inside the backoff nothing more is sent.
        sent = len(unit.sent)
        now[0] += 1.0
        mgr._reestablish_followers_after_restart()
        self.assertEqual(len(unit.sent), sent)
        # The unit becomes ready: the retry succeeds and ends the loop.
        unit.response = None
        now[0] += mgr_backoff(0)
        mgr._reestablish_followers_after_restart()
        self.assertIs(lane_of(mgr).following, True)
        count = len(unit.sent)
        now[0] += 1000.0
        mgr._reestablish_followers_after_restart()
        self.assertEqual(len(unit.sent), count)

    def test_retries_are_bounded_and_leave_the_lane_usable(self):
        mgr, unit = restarted_manager()
        now = self._clock(mgr)
        unit.response = types.SimpleNamespace(follower_refused=True)
        with self.assertLogs(level=logging.WARNING):
            for _ in range(20):
                mgr._reestablish_followers_after_restart()
                now[0] += 100.0
        forward = [c for c in unit.sent if c == (1, S.FOLLOWER_FORWARD)]
        self.assertEqual(len(forward), len(FOLLOWER_RETRY_BACKOFF) + 1)
        lane = lane_of(mgr)
        self.assertIs(lane.following, False)
        self.assertTrue(mgr._motion_settled("fps1", lane))

    def test_unconfirmed_command_is_followed_by_a_stop(self):
        mgr, unit = restarted_manager()
        self._clock(mgr)
        unit.response = types.SimpleNamespace(follower_confirmed=False)
        mgr._reestablish_followers_after_restart()
        # The forward command, then a stop to reach a known state.
        self.assertEqual(unit.sent[0], (1, S.FOLLOWER_FORWARD))
        self.assertIn((0, S.FOLLOWER_REVERSE), unit.sent)
        unit.response = None
        mgr._reestablish_followers_after_restart()    # inside the backoff

    def test_a_resync_after_clear_errors_is_resolved_again(self):
        mgr, unit = restarted_manager()
        self._clock(mgr)
        mgr._reestablish_followers_after_restart()
        self.assertIs(lane_of(mgr).following, True)
        # ClearErrors/resync publishes an unknown follower again.
        mgr.runtime._sys = S.SystemState(lanes={
            "fps1": S.replace(lane_of(mgr), following=None)})
        mgr._reestablish_followers_after_restart()
        self.assertIs(lane_of(mgr).following, True)

    def test_print_start_arms_a_stopped_lane(self):
        mgr, unit = restarted_manager(following=False)
        self._clock(mgr)
        stats = {"state": "standby"}
        mgr.printer = types.SimpleNamespace(lookup_object=lambda n, d=None:
            types.SimpleNamespace(get_status=lambda now: stats)
            if n == "print_stats" else d)
        mgr._heat_waiting = set()
        mgr._arm_followers_for_print(100.0)
        mgr._reestablish_followers_after_restart()
        self.assertEqual(unit.sent, [])               # idle: left stopped
        stats["state"] = "printing"
        mgr._arm_followers_for_print(101.0)
        mgr.idle_timeout = types.SimpleNamespace(
            get_status=lambda now: {"state": "Printing"})
        mgr._reestablish_followers_after_restart()
        self.assertEqual(unit.sent, [(1, S.FOLLOWER_FORWARD)])
        self.assertIs(lane_of(mgr).following, True)

    def test_known_follower_is_left_alone(self):
        mgr, unit = restarted_manager(following=False)
        mgr._reestablish_followers_after_restart()
        self.assertEqual(unit.sent, [])

    def test_unloaded_lane_is_left_alone(self):
        mgr, unit = restarted_manager()
        mgr.runtime._sys = S.SystemState(lanes={"fps1": S.LaneState()})
        mgr._reestablish_followers_after_restart()
        self.assertEqual(unit.sent, [])


if __name__ == "__main__":
    unittest.main()
