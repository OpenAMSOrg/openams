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

UNCAL_A = ("oams1 Spool 1 has no PTFE length; run OAMS_CALIBRATE_PTFE_LENGTH"
           " OAMS=1 SPOOL=0 (SPOOL counts from 0)")
UNCAL_B = ("oams1 Spool 4 has no PTFE length; run OAMS_CALIBRATE_PTFE_LENGTH"
           " OAMS=1 SPOOL=3 (SPOOL counts from 0)")


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


class UncalibratedAdviceTests(unittest.TestCase):
    """The refusal names a fix the unit accepts: a Cortex Bridge unit cannot
    calibrate itself (no ptfe_calibration), so it gets the measuring steps."""

    def _bridge_world(self, **kwargs):
        w = world(**kwargs)
        lw = w.lanes[FPS]
        from dataclasses import replace
        return S.World(lanes={FPS: replace(
            lw, unit_names={1: "ams1"}, ptfe_how={1: S.PTFE_CORTEX_BRIDGE})})

    def test_bridge_load_refusal_gives_the_measuring_steps(self):
        w = self._bridge_world(ready={BAY_A: True}, calibrated={BAY_A: False})
        _, fx = S.reduce(system(S.LaneState()), S.LoadBay(FPS, BAY_A), w,
                         now=1.0)
        message = refusal_message(fx)
        self.assertTrue(message.startswith("ams1 Spool 1 has no PTFE length;"))
        self.assertIn("cortex-bridge.py ptfe <millimetres>", message)
        self.assertIn("ptfe_length: under [oams ams1]", message)
        self.assertIn("FIRMWARE_RESTART", message)
        self.assertNotIn("OAMS_CALIBRATE_PTFE_LENGTH", message)

    def test_bridge_runout_with_no_length_gives_the_measuring_steps(self):
        lane = S.LaneState(op=S.OP_LOADED, group=GROUP, unit=BAY_A,
                           runout=S.RUNOUT_COASTING, coast_origin=0.0)
        w = self._bridge_world(extruder_pos=10.0, printing=True,
                               loaded={BAY_A: False}, path_len=0.0)
        _, fx = S.reduce(system(lane), S.Tick(), w, now=1.0)
        reason = find(fx, S.Pause)[0].reason
        self.assertIn("cortex-bridge.py ptfe", reason)
        self.assertNotIn("OAMS_CALIBRATE_PTFE_LENGTH", reason)

    def test_a_unit_measured_by_hand_is_told_to_set_ptfe_length(self):
        self.assertEqual(
            S.ptfe_length_advice(S.PTFE_MEASURE, "mmu1", 2, 1),
            "set ptfe_length: under [oams mmu1] to its filament path, then"
            " FIRMWARE_RESTART")

    def test_self_calibrating_unit_gets_the_complete_command(self):
        self.assertEqual(
            S.ptfe_length_advice(S.PTFE_CALIBRATE, "oams2", 2, 1),
            "run OAMS_CALIBRATE_PTFE_LENGTH OAMS=2 SPOOL=1 (SPOOL counts"
            " from 0)")


class HostArrivalLoadTimeoutTests(unittest.TestCase):
    """A Cortex Bridge unit stops a load that feeds past its path without the
    toolhead buffer reading arrival, and reports it as a plain timeout. The
    failure must name ptfe_length, not only a jam."""

    FAULT = ("the unit reports fault motion_timeout; clear it with"
             " OAMS_CLEAR_FAULT OAMS=1, then OAMSM_CLEAR_ERRORS")

    def _failed_load(self, host_arrival):
        from dataclasses import replace
        w = world(ready={BAY_A: True}, calibrated={BAY_A: True},
                  path_len=1300.0)
        lw = replace(w.lanes[FPS], unit_names={1: "ams1"},
                     host_arrival=frozenset({1}) if host_arrival
                     else frozenset())
        w = S.World(lanes={FPS: lw})
        s, _ = S.reduce(system(S.LaneState()), S.LoadBay(FPS, BAY_A), w,
                        now=1.0)
        gen = lane_of(s).op_gen
        s, fx = S.reduce(s, S.OpCompleted(FPS, S.OAMS_OP_CODE_TIMEOUT, 0,
                                          gen=gen, fault=self.FAULT),
                         w, now=11.0)
        return s, refusal_message(fx)

    def test_bridge_timeout_names_ptfe_length_and_keeps_the_fault(self):
        s, message = self._failed_load(host_arrival=True)
        self.assertEqual(lane_of(s).op, S.OP_UNLOADED)
        self.assertIn("fed past ptfe_length (1300 mm)", message)
        self.assertIn("a snag in the path", message)
        self.assertIn("shorter than the filament path", message)
        self.assertTrue(message.endswith(self.FAULT + ")"))
        self.assertNotIn("dead motor", message)

    def test_other_units_keep_the_firmware_watchdog_text(self):
        _, message = self._failed_load(host_arrival=False)
        self.assertIn("no filament progress", message)
        self.assertNotIn("ptfe_length", message)


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
        self.assertIn("OAMS_CALIBRATE_PTFE_LENGTH OAMS=1 SPOOL=3", pause.reason)
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
