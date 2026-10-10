# Reducer safety tests: H10 per-bay PTFE calibration, H14 resync follower,
# H18 refusal messages. Pure unit tests, no Klipper and no pytest needed:
#   python3 -m unittest discover -s test -p 'test_reducer_safety.py' -v

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

import oams_state as S


FPS = "fps1"
GROUP = "T0"
BAY_A = (1, 0)   # (oams_idx, bay)
BAY_B = (1, 3)
BAY_C = (1, 2)

UNCAL_A = "oams1 Spool 1 is not calibrated; run OAMS_CALIBRATE_PTFE_LENGTH SPOOL=0"
UNCAL_B = "oams1 Spool 4 is not calibrated; run OAMS_CALIBRATE_PTFE_LENGTH SPOOL=3"


def world(extruder_pos=0.0, printing=False, loaded=None, ready=None,
          calibrated=None, reserved=None, path_len=600.0,
          group_bays=None):
    lw = S.LaneWorld(
        extruder_pos=extruder_pos,
        printing=printing,
        loaded=loaded if loaded is not None else {},
        ready=ready if ready is not None else {},
        calibrated=calibrated if calibrated is not None else {},
        reserved=reserved if reserved is not None else {},
        group_bays=(group_bays if group_bays is not None
                    else {GROUP: (BAY_A, BAY_B)}),
        path_len={1: path_len},
        unit_names={1: "oams1"},
    )
    return S.World(lanes={FPS: lw})


def system(lane):
    return S.SystemState(lanes={FPS: lane})


def lane_of(sysstate):
    return sysstate.lanes[FPS]


def find(effects, cls):
    return [e for e in effects if isinstance(e, cls)]


def has(effects, cls):
    return bool(find(effects, cls))


def refusal_message(effects):
    """The message of the single Settle refusal, or None."""
    settles = find(effects, S.Settle)
    return settles[0].result.message if settles else None


class UncalibratedLoadTests(unittest.TestCase):
    """H10 layer 1: the reducer refuses a load on a bay with no PTFE length."""

    def test_group_load_skips_uncalibrated_bay_for_a_spare(self):
        sys0 = system(S.LaneState())
        w = world(ready={BAY_A: True, BAY_B: True},
                  calibrated={BAY_A: False, BAY_B: True})
        sys1, fx = S.reduce(sys0, S.Load(FPS, GROUP), w, now=1.0)
        self.assertEqual(lane_of(sys1).unit, BAY_B)
        self.assertEqual(find(fx, S.StartLoad)[0].unit, BAY_B)

    def test_group_load_names_the_uncalibrated_bay(self):
        sys0 = system(S.LaneState())
        w = world(ready={BAY_A: True}, calibrated={BAY_A: False})
        sys1, fx = S.reduce(sys0, S.Load(FPS, GROUP), w, now=1.0)
        self.assertEqual(lane_of(sys1).op, S.OP_UNLOADED)
        self.assertFalse(has(fx, S.StartLoad))
        self.assertEqual(refusal_message(fx), UNCAL_A)

    def test_missing_calibration_entries_stay_loadable(self):
        # The snapshot only carries bays a unit reports a length for. A world
        # without a `calibrated` map (e.g. a follower unit) must stay usable.
        sys0 = system(S.LaneState())
        w = world(ready={BAY_A: True})
        sys1, fx = S.reduce(sys0, S.Load(FPS, GROUP), w, now=1.0)
        self.assertEqual(lane_of(sys1).op, S.OP_LOADING)
        self.assertEqual(find(fx, S.StartLoad)[0].unit, BAY_A)

    def test_load_bay_guard_names_the_uncalibrated_bay(self):
        sys0 = system(S.LaneState())
        w = world(ready={BAY_A: True}, calibrated={BAY_A: False})
        sys1, fx = S.reduce(sys0, S.LoadBay(FPS, BAY_A), w, now=1.0)
        self.assertEqual(lane_of(sys1).op, S.OP_UNLOADED)
        self.assertFalse(has(fx, S.StartLoad))
        self.assertEqual(refusal_message(fx), UNCAL_A)

    def test_load_bay_for_a_calibrated_bay_starts(self):
        sys0 = system(S.LaneState())
        w = world(ready={BAY_B: True}, calibrated={BAY_B: True})
        sys1, fx = S.reduce(sys0, S.LoadBay(FPS, BAY_B), w, now=1.0)
        self.assertEqual(lane_of(sys1).op, S.OP_LOADING)
        self.assertEqual(find(fx, S.StartLoad)[0].unit, BAY_B)


class UncalibratedRunoutTests(unittest.TestCase):
    """H10: the runout reload picks a calibrated spare, or pauses and says so."""

    def _coasting_lane(self):
        return S.LaneState(op=S.OP_LOADED, group=GROUP, unit=BAY_A,
                           runout=S.RUNOUT_COASTING, coast_origin=0.0)

    def _past_path(self):
        return S.PAUSE_DISTANCE + 600.0 + 1.0

    def test_runout_skips_uncalibrated_spare_for_a_calibrated_one(self):
        w = world(extruder_pos=self._past_path(), printing=True,
                  loaded={BAY_A: False},
                  ready={BAY_B: True, BAY_C: True},
                  calibrated={BAY_B: False, BAY_C: True},
                  group_bays={GROUP: (BAY_A, BAY_B, BAY_C)})
        s, fx = S.reduce(system(self._coasting_lane()), S.Tick(), w, now=1.0)
        self.assertEqual(lane_of(s).runout, S.RUNOUT_LOADING)
        self.assertEqual(find(fx, S.StartLoad)[0].unit, BAY_C)

    def test_runout_pauses_when_the_only_spare_is_uncalibrated(self):
        w = world(extruder_pos=self._past_path(), printing=True,
                  loaded={BAY_A: False}, ready={BAY_B: True},
                  calibrated={BAY_B: False})
        s, fx = S.reduce(system(self._coasting_lane()), S.Tick(), w, now=1.0)
        self.assertFalse(has(fx, S.StartLoad))
        pause = find(fx, S.Pause)[0]
        self.assertIn("spare spool is not calibrated", pause.reason)
        # The spare is named the way the UI names it: its unit, and Spool <n>.
        self.assertIn("(oams1 Spool 4)", pause.reason)
        self.assertIn("SPOOL=3", pause.reason)
        self.assertEqual(lane_of(s).op, S.OP_UNLOADED)
        self.assertEqual(lane_of(s).message, "spare spool is not calibrated")

    def test_runout_with_no_ready_spare_still_pauses_generically(self):
        w = world(extruder_pos=self._past_path(), printing=True,
                  loaded={BAY_A: False}, ready={BAY_A: False, BAY_B: False})
        s, fx = S.reduce(system(self._coasting_lane()), S.Tick(), w, now=1.0)
        self.assertFalse(has(fx, S.StartLoad))
        self.assertEqual(lane_of(s).op, S.OP_UNLOADED)
        self.assertEqual(lane_of(s).message, "no spare spool")


class ResyncFollowerTests(unittest.TestCase):
    """H14: a resync trusts the hub HES only, so the follower stays unknown."""

    def test_clear_errors_does_not_claim_the_follower_is_stopped(self):
        sys0 = system(S.LaneState(op=S.OP_LOADING, unit=BAY_A))
        w = world(loaded={BAY_A: True})
        s, fx = S.reduce(sys0, S.ClearErrors(), w, now=1.0)
        lane = lane_of(s)
        self.assertEqual(lane.op, S.OP_LOADED)
        self.assertEqual(lane.unit, BAY_A)
        self.assertIsNone(lane.following)
        self.assertEqual(fx, [])   # the resync path stays effect-free

    def test_fault_clear_resync_also_leaves_the_follower_unknown(self):
        sys0 = S.SystemState(lanes={FPS: S.LaneState(
            op=S.OP_LOADED, group=GROUP, unit=BAY_A,
            following=False)},
            firmware_faults={0: (FPS, 55, ("identity",))})
        w = world(loaded={BAY_A: True})
        s, fx = S.reduce(sys0, S.FirmwareFaultCleared(0, ("identity",)),
                         w, now=1.0)
        self.assertEqual(s.firmware_faults, {})
        self.assertIsNone(lane_of(s).following)
        self.assertEqual(fx, [])

    def test_clear_errors_on_an_empty_lane_stays_unloaded(self):
        sys0 = system(S.LaneState(op=S.OP_LOADED, unit=BAY_A,
                                  following=True))
        s, fx = S.reduce(sys0, S.ClearErrors(), world(loaded={}), now=1.0)
        self.assertEqual(lane_of(s).op, S.OP_UNLOADED)
        self.assertFalse(lane_of(s).following)
        self.assertEqual(fx, [])


class RefusalMessageTests(unittest.TestCase):
    """H18: op code 1 is the unit's generic refusal and reads as such."""

    def test_describe_code_one_is_human_readable(self):
        self.assertEqual(
            S.describe_code(S.OAMS_OP_CODE_ERROR_UNSPECIFIED),
            "the unit refused the operation (not ready or not permitted)")

    def test_other_codes_keep_their_own_text(self):
        self.assertEqual(S.describe_code(S.OAMS_OP_CODE_ERROR_BUSY),
                         "OAMS is busy with another operation")
        self.assertEqual(S.describe_code(S.OAMS_OP_CODE_SUCCESS), "code 0")


if __name__ == "__main__":
    unittest.main()
