# Pure unit tests for the OpenAMS reducer (oams_state.py).
#
# No Klipper, no hardware, no pytest required:
#   cd openams && python3 -m unittest discover -s test -v
#   (or: python3 test/test_oams_state.py)

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import oams_state as S


FPS = "fps1"
GROUP = "T0"
BAY_A = (1, 0)   # (oams_idx, bay)
BAY_B = (1, 3)


def world(extruder_pos=0.0, printing=False, loaded=None, ready=None,
          path_len=600.0, reload_before=0.0, reserved=None,
          motion_blocked=None):
    lw = S.LaneWorld(
        extruder_pos=extruder_pos,
        printing=printing,
        loaded=loaded if loaded is not None else {},
        ready=ready if ready is not None else {},
        reserved=reserved if reserved is not None else {},
        motion_blocked=motion_blocked if motion_blocked is not None else {},
        group_bays={GROUP: (BAY_A, BAY_B)},
        path_len={1: path_len},
        reload_before=reload_before,
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


class LoadTests(unittest.TestCase):
    def test_group_load_prefers_local_reservation(self):
        sys0 = system(S.LaneState())
        w = world(ready={BAY_A: True, BAY_B: True}, reserved={1: 3})
        sys1, fx = S.reduce(sys0, S.Load(FPS, GROUP), w, now=1.0)
        self.assertEqual(lane_of(sys1).unit, BAY_B)
        self.assertEqual(find(fx, S.StartLoad)[0].unit, BAY_B)
        self.assertFalse(any(w.lanes[FPS].loaded.values()))

    def test_group_load_rejects_other_bay_on_reserved_unit(self):
        sys0 = system(S.LaneState())
        w = world(ready={BAY_A: True}, reserved={1: 3})
        sys1, fx = S.reduce(sys0, S.Load(FPS, GROUP), w, now=1.0)
        self.assertEqual(lane_of(sys1).op, S.OP_UNLOADED)
        self.assertFalse(has(fx, S.StartLoad))

    def test_load_ready_bay_starts_loading(self):
        sys0 = system(S.LaneState(op=S.OP_UNLOADED))
        w = world(ready={BAY_A: True})
        sys1, fx = S.reduce(sys0, S.Load(FPS, GROUP), w, now=10.0)
        lane = lane_of(sys1)
        self.assertEqual(lane.op, S.OP_LOADING)
        self.assertEqual(lane.unit, BAY_A)
        self.assertEqual(find(fx, S.StartLoad)[0].unit, BAY_A)
        self.assertTrue(has(fx, S.ArmDeadline))

    def test_load_none_ready_stays_unloaded(self):
        sys0 = system(S.LaneState(op=S.OP_UNLOADED))
        w = world(ready={BAY_A: False, BAY_B: False})
        sys1, fx = S.reduce(sys0, S.Load(FPS, GROUP), w, now=0.0)
        self.assertEqual(lane_of(sys1).op, S.OP_UNLOADED)
        self.assertFalse(has(fx, S.StartLoad))
        self.assertFalse(find(fx, S.Settle)[0].result.ok)

    def test_load_completion_success(self):
        sys0 = system(S.LaneState(op=S.OP_LOADING, unit=BAY_A, group=GROUP))
        sys1, fx = S.reduce(sys0, S.OpCompleted(FPS, S.OAMS_OP_CODE_SUCCESS, gen=0),
                            world(), now=1.0)
        self.assertEqual(lane_of(sys1).op, S.OP_LOADED)
        self.assertEqual(lane_of(sys1).unit, BAY_A)
        self.assertTrue(lane_of(sys1).following)
        self.assertEqual(lane_of(sys1).direction, S.FOLLOWER_FORWARD)
        self.assertTrue(find(fx, S.Settle)[0].result.ok)
        self.assertTrue(has(fx, S.CancelDeadline))

    def test_load_completion_error(self):
        sys0 = system(S.LaneState(op=S.OP_LOADING, unit=BAY_A, group=GROUP))
        sys1, fx = S.reduce(sys0, S.OpCompleted(FPS, S.OAMS_OP_CODE_ERROR_BUSY, gen=0),
                            world(), now=1.0)
        self.assertEqual(lane_of(sys1).op, S.OP_UNLOADED)
        self.assertIsNone(lane_of(sys1).unit)
        self.assertFalse(find(fx, S.Settle)[0].result.ok)

    def test_cancel_then_completed_is_not_loaded(self):
        sys0 = system(S.LaneState(op=S.OP_LOADING, unit=BAY_A, group=GROUP))
        sys1, fx = S.reduce(sys0, S.Cancel(FPS), world(), now=1.0)
        self.assertEqual(find(fx, S.CancelLoad)[0].unit, BAY_A)
        self.assertEqual(lane_of(sys1).op, S.OP_LOADING)  # still loading until ack
        sys2, fx2 = S.reduce(sys1, S.OpCompleted(FPS, S.OAMS_OP_CODE_CANCEL, gen=0),
                             world(), now=2.0)
        self.assertEqual(lane_of(sys2).op, S.OP_UNLOADED)
        self.assertIsNone(lane_of(sys2).unit)          # NOT recorded as loaded
        self.assertFalse(find(fx2, S.Settle)[0].result.ok)

    def test_load_timeout(self):
        sys0 = system(S.LaneState(op=S.OP_LOADING, unit=BAY_A, group=GROUP))
        sys1, fx = S.reduce(sys0, S.Timeout(FPS), world(), now=999.0)
        self.assertEqual(lane_of(sys1).op, S.OP_UNLOADED)
        self.assertFalse(find(fx, S.Settle)[0].result.ok)
        self.assertFalse(has(fx, S.SetFollower))  # nothing to rewind on a load


class LoadBayTests(unittest.TestCase):
    def test_direct_load_rejects_other_bay_on_reserved_unit(self):
        sys0 = system(S.LaneState())
        sys1, fx = S.reduce(sys0, S.LoadBay(FPS, BAY_A),
                            world(reserved={1: 3}), now=1.0)
        self.assertEqual(lane_of(sys1).op, S.OP_UNLOADED)
        self.assertFalse(has(fx, S.StartLoad))

    def test_load_bay_starts_and_resolves_group(self):
        sys0 = system(S.LaneState(op=S.OP_UNLOADED))
        sys1, fx = S.reduce(sys0, S.LoadBay(FPS, BAY_B), world(), now=0.0)
        lane = lane_of(sys1)
        self.assertEqual(lane.op, S.OP_LOADING)
        self.assertEqual(lane.unit, BAY_B)
        self.assertEqual(lane.group, GROUP)              # resolved from group_bays
        self.assertEqual(find(fx, S.StartLoad)[0].unit, BAY_B)

    def test_load_bay_completion(self):
        sys0 = system(S.LaneState(op=S.OP_LOADING, unit=BAY_B, group=GROUP))
        sys1, fx = S.reduce(sys0, S.OpCompleted(FPS, S.OAMS_OP_CODE_SUCCESS, gen=0),
                            world(), now=1.0)
        self.assertEqual(lane_of(sys1).op, S.OP_LOADED)
        self.assertEqual(lane_of(sys1).unit, BAY_B)
        self.assertTrue(lane_of(sys1).following)
        self.assertEqual(lane_of(sys1).direction, S.FOLLOWER_FORWARD)

    def test_load_bay_rejected_when_busy(self):
        sys0 = system(S.LaneState(op=S.OP_LOADED, unit=BAY_A, group=GROUP))
        sys1, fx = S.reduce(sys0, S.LoadBay(FPS, BAY_B), world(), now=0.0)
        self.assertEqual(lane_of(sys1).op, S.OP_LOADED)   # unchanged
        self.assertEqual(lane_of(sys1).unit, BAY_A)
        self.assertFalse(has(fx, S.StartLoad))
        self.assertFalse(find(fx, S.Settle)[0].result.ok)


class UnloadTests(unittest.TestCase):
    def test_unload_starts(self):
        sys0 = system(S.LaneState(op=S.OP_LOADED, unit=BAY_A, group=GROUP))
        sys1, fx = S.reduce(sys0, S.Unload(FPS), world(), now=0.0)
        self.assertEqual(lane_of(sys1).op, S.OP_UNLOADING)
        self.assertEqual(find(fx, S.StartUnload)[0].unit, BAY_A)

    def test_unload_success(self):
        sys0 = system(S.LaneState(op=S.OP_UNLOADING, unit=BAY_A, group=GROUP))
        sys1, fx = S.reduce(sys0, S.OpCompleted(FPS, S.OAMS_OP_CODE_SUCCESS, gen=0),
                            world(), now=0.0)
        self.assertEqual(lane_of(sys1).op, S.OP_UNLOADED)
        self.assertIsNone(lane_of(sys1).unit)
        self.assertTrue(find(fx, S.Settle)[0].result.ok)

    def test_unload_failure_stops_follower_and_stays_loaded(self):
        sys0 = system(S.LaneState(op=S.OP_UNLOADING, unit=BAY_A, group=GROUP))
        sys1, fx = S.reduce(sys0, S.OpCompleted(FPS, S.OAMS_OP_CODE_ERROR_BUSY, gen=0),
                            world(), now=0.0)
        self.assertEqual(lane_of(sys1).op, S.OP_LOADED)
        sf = find(fx, S.SetFollower)
        self.assertTrue(sf and sf[0].enable == 0)


class CalibrateTests(unittest.TestCase):
    def test_calibrate_async_and_return_to_prior(self):
        sys0 = system(S.LaneState(op=S.OP_UNLOADED))
        sys1, fx = S.reduce(sys0, S.Calibrate(FPS, oams_idx=1, bay=0, kind="ptfe"),
                            world(), now=0.0)
        self.assertEqual(lane_of(sys1).op, S.OP_CALIBRATING)
        self.assertEqual(find(fx, S.StartCalibrate)[0].kind, "ptfe")
        sys2, fx2 = S.reduce(sys1, S.OpCompleted(
            FPS, S.OAMS_OP_CODE_SUCCESS, value=1234, gen=lane_of(sys1).op_gen),
                             world(), now=1.0)
        self.assertEqual(lane_of(sys2).op, S.OP_UNLOADED)
        res = find(fx2, S.Settle)[0].result
        self.assertTrue(res.ok)
        self.assertEqual(res.value, 1234)


class RunoutTests(unittest.TestCase):
    def _loaded_lane(self):
        return S.LaneState(op=S.OP_LOADED, group=GROUP, unit=BAY_A)

    def test_happy_path(self):
        lane = self._loaded_lane()
        # 1) runout detected -> PAUSING
        w = world(extruder_pos=1000.0, printing=True,
                  loaded={BAY_A: False, BAY_B: True},
                  ready={BAY_A: False, BAY_B: True})
        s, fx = S.reduce(system(lane), S.Tick(), w, now=1.0)
        self.assertEqual(lane_of(s).runout, S.RUNOUT_PAUSING)

        # 2) advance >= PAUSE_DISTANCE -> COASTING + coast follower off
        w = world(extruder_pos=1000.0 + S.PAUSE_DISTANCE, printing=True,
                  loaded={BAY_A: False, BAY_B: True},
                  ready={BAY_A: False, BAY_B: True})
        s, fx = S.reduce(s, S.Tick(), w, now=2.0)
        self.assertEqual(lane_of(s).runout, S.RUNOUT_COASTING)
        self.assertTrue(has(fx, S.SetFollower))

        # 3) consume beyond path_len (mm) -> LOADING the next group bay
        far = 1000.0 + S.PAUSE_DISTANCE + 600.0
        w = world(extruder_pos=far + 1.0, printing=True,
                  loaded={BAY_A: False, BAY_B: True},
                  ready={BAY_A: False, BAY_B: True})
        s, fx = S.reduce(s, S.Tick(), w, now=3.0)
        self.assertEqual(lane_of(s).runout, S.RUNOUT_LOADING)
        self.assertEqual(find(fx, S.StartLoad)[0].unit, BAY_B)

        # 4) reload completes -> unit switched, back to idle (the reload bumped
        # the lane's op generation, so the completion must carry it)
        s, fx = S.reduce(s, S.OpCompleted(
            FPS, S.OAMS_OP_CODE_SUCCESS, gen=lane_of(s).op_gen), w, now=4.0)
        self.assertEqual(lane_of(s).op, S.OP_LOADED)
        self.assertEqual(lane_of(s).unit, BAY_B)
        self.assertEqual(lane_of(s).runout, S.RUNOUT_IDLE)
        self.assertTrue(lane_of(s).following)
        self.assertEqual(lane_of(s).direction, S.FOLLOWER_FORWARD)

    def test_uncalibrated_pauses(self):
        lane = S.LaneState(op=S.OP_LOADED, group=GROUP, unit=BAY_A,
                           runout=S.RUNOUT_COASTING, coast_origin=0.0)
        w = world(extruder_pos=10.0, printing=True,
                  loaded={BAY_A: False}, ready={BAY_B: True}, path_len=0.0)
        s, fx = S.reduce(system(lane), S.Tick(), w, now=1.0)
        self.assertTrue(has(fx, S.Pause))
        self.assertEqual(lane_of(s).op, S.OP_UNLOADED)

    def test_no_spare_pauses(self):
        lane = S.LaneState(op=S.OP_LOADED, group=GROUP, unit=BAY_A,
                           runout=S.RUNOUT_COASTING, coast_origin=0.0)
        far = S.PAUSE_DISTANCE + 600.0 + 1.0
        # only the ran-out bay looks "ready" -> must NOT be chosen
        w = world(extruder_pos=far, printing=True,
                  loaded={BAY_A: False}, ready={BAY_A: True, BAY_B: False})
        s, fx = S.reduce(system(lane), S.Tick(), w, now=1.0)
        self.assertFalse(has(fx, S.StartLoad))
        self.assertTrue(has(fx, S.Pause))
        self.assertEqual(lane_of(s).op, S.OP_UNLOADED)

    def test_reload_failure_pauses(self):
        lane = S.LaneState(op=S.OP_LOADED, group=GROUP, unit=BAY_A,
                           runout=S.RUNOUT_LOADING, reload_target=BAY_B)
        s, fx = S.reduce(system(lane),
                         S.OpCompleted(FPS, S.OAMS_OP_CODE_ERROR_BUSY, gen=0),
                         world(), now=1.0)
        self.assertTrue(has(fx, S.Pause))
        self.assertEqual(lane_of(s).op, S.OP_UNLOADED)

    def test_reload_into_single_bay_follower_unit(self):
        # The reducer is unit-kind agnostic: a follower is just another
        # (idx, bay) with its own path_len entry. Runout on the OAMS bay must
        # auto-reload from the follower's bay 0 spare.
        FOLLOWER_BAY = (9, 0)
        lane = S.LaneState(op=S.OP_LOADED, group="MIX", unit=BAY_A,
                           runout=S.RUNOUT_COASTING, coast_origin=0.0)
        lw = S.LaneWorld(
            extruder_pos=S.PAUSE_DISTANCE + 600.0 + 1.0, printing=True,
            loaded={BAY_A: False, FOLLOWER_BAY: False},
            ready={FOLLOWER_BAY: True},
            group_bays={"MIX": (BAY_A, FOLLOWER_BAY)},
            path_len={1: 600.0, 9: 500.0})
        s, fx = S.reduce(system(lane), S.Tick(),
                         S.World(lanes={FPS: lw}), now=1.0)
        self.assertEqual(lane_of(s).runout, S.RUNOUT_LOADING)
        self.assertEqual(find(fx, S.StartLoad)[0].unit, FOLLOWER_BAY)
        # completion switches the lane onto the follower unit
        s2, _ = S.reduce(s, S.OpCompleted(FPS, S.OAMS_OP_CODE_SUCCESS,
                                          gen=lane_of(s).op_gen),
                         S.World(lanes={FPS: lw}), now=2.0)
        self.assertEqual(lane_of(s2).unit, FOLLOWER_BAY)

    def test_idle_tick_is_noop(self):
        lane = self._loaded_lane()
        w = world(extruder_pos=5.0, printing=True, loaded={BAY_A: True})
        s, fx = S.reduce(system(lane), S.Tick(), w, now=1.0)
        self.assertEqual(fx, [])
        self.assertEqual(lane_of(s).runout, S.RUNOUT_IDLE)


class ClearErrorsTests(unittest.TestCase):
    def test_resync_loaded(self):
        # Start from a stale in-flight state; resync trusts the hub HES only.
        sys0 = system(S.LaneState(op=S.OP_LOADING, unit=BAY_A))
        w = world(loaded={BAY_B: True})
        s, fx = S.reduce(sys0, S.ClearErrors(), w, now=1.0)
        self.assertEqual(lane_of(s).op, S.OP_LOADED)
        self.assertEqual(lane_of(s).unit, BAY_B)
        self.assertEqual(lane_of(s).group, GROUP)

    def test_resync_unloaded(self):
        sys0 = system(S.LaneState(op=S.OP_LOADED, unit=BAY_A))
        w = world(loaded={BAY_A: False, BAY_B: False})
        s, fx = S.reduce(sys0, S.ClearErrors(), w, now=1.0)
        self.assertEqual(lane_of(s).op, S.OP_UNLOADED)
        self.assertIsNone(lane_of(s).unit)


    def test_monitor_resync_keeps_an_in_flight_op_for_its_completion(self):
        # The monitor's automatic resync must not strand a running UNLOAD:
        # its completion (matching op_gen) still settles it as a success.
        unloading = S.LaneState(op=S.OP_UNLOADING, unit=BAY_A, group=GROUP,
                                op_gen=5, op_deadline=10.0)
        w = world(loaded={BAY_A: False, BAY_B: False})
        s, fx = S.reduce(system(unloading), S.ClearErrors(keep_in_flight=True),
                         w, now=1.0)
        self.assertEqual(lane_of(s), unloading)
        self.assertEqual(fx, [])
        s, fx = S.reduce(s, S.OpCompleted(FPS, S.OAMS_OP_CODE_SUCCESS, gen=5),
                         w, now=2.0)
        self.assertEqual(lane_of(s).op, S.OP_UNLOADED)
        self.assertTrue(find(fx, S.Settle)[0].result.ok)
        # An explicit clear still resyncs it, and the late completion then
        # belongs to no op in flight.
        s, _ = S.reduce(system(unloading), S.ClearErrors(), w, now=1.0)
        s, fx = S.reduce(s, S.OpCompleted(FPS, S.OAMS_OP_CODE_SUCCESS, gen=5),
                         w, now=2.0)
        self.assertFalse(has(fx, S.Settle))

    def test_monitor_resync_still_resyncs_an_idle_lane(self):
        sys0 = system(S.LaneState(op=S.OP_LOADED, unit=BAY_A))
        w = world(loaded={BAY_A: False, BAY_B: False})
        s, fx = S.reduce(sys0, S.ClearErrors(keep_in_flight=True), w, now=1.0)
        self.assertEqual(lane_of(s).op, S.OP_UNLOADED)
        self.assertIsNone(lane_of(s).unit)


class FirmwareFaultClearedTests(unittest.TestCase):
    IDENTITY = (1, 2, 3, 0, 4, 5, 6, 0, 7, 0, 8, 0)

    def test_exact_identity_clears_and_resyncs_loaded(self):
        sys0 = S.SystemState(lanes={FPS: S.LaneState(op=S.OP_UNLOADED)},
                             firmware_faults={0: (FPS, 5, self.IDENTITY)})
        w = world(loaded={BAY_A: True})
        s, fx = S.reduce(sys0, S.FirmwareFaultCleared(0, self.IDENTITY), w, now=2.0)
        self.assertEqual(s.firmware_faults, {})
        self.assertEqual(fx, [])
        self.assertEqual(lane_of(s).op, S.OP_LOADED)
        self.assertEqual(lane_of(s).unit, BAY_A)
        self.assertEqual(lane_of(s).group, GROUP)

    def test_exact_identity_clears_and_resyncs_empty(self):
        sys0 = S.SystemState(lanes={FPS: S.LaneState(op=S.OP_LOADED, unit=BAY_A)},
                             firmware_faults={0: (FPS, 5, self.IDENTITY)})
        w = world(loaded={BAY_A: False})
        s, fx = S.reduce(sys0, S.FirmwareFaultCleared(0, self.IDENTITY), w, now=2.0)
        self.assertEqual(s.firmware_faults, {})
        self.assertEqual(lane_of(s).op, S.OP_UNLOADED)
        self.assertIsNone(lane_of(s).unit)

    def test_wrong_identity_ignored(self):
        sys0 = S.SystemState(lanes={FPS: S.LaneState()},
                             firmware_faults={0: (FPS, 5, self.IDENTITY)})
        w = world(loaded={BAY_A: True})
        s, fx = S.reduce(sys0, S.FirmwareFaultCleared(0, ("different",)), w, now=2.0)
        self.assertEqual(s, sys0)
        self.assertEqual(fx, [])

    def test_no_latch_ignored(self):
        sys0 = S.SystemState(lanes={FPS: S.LaneState()})
        s, fx = S.reduce(sys0, S.FirmwareFaultCleared(0, self.IDENTITY), world(), now=2.0)
        self.assertEqual(s, sys0)
        self.assertEqual(fx, [])

    def test_global_255_resyncs_all_lanes(self):
        l1 = S.LaneState(op=S.OP_UNLOADED)
        l2 = S.LaneState(op=S.OP_LOADED, group="T1", unit=(2, 0))
        sys0 = S.SystemState(lanes={"fps1": l1, "fps2": l2},
                             firmware_faults={255: (None, 9, self.IDENTITY)})
        w = S.World(lanes={
            "fps1": S.LaneWorld(group_bays={"T0": ((1, 0), (1, 3))},
                                path_len={1: 600.0}, loaded={(1, 0): True}),
            "fps2": S.LaneWorld(group_bays={"T1": ((2, 0),)},
                                path_len={2: 600.0}, loaded={(2, 0): False}),
        })
        s, fx = S.reduce(sys0, S.FirmwareFaultCleared(255, self.IDENTITY), w, now=3.0)
        self.assertEqual(s.firmware_faults, {})
        self.assertEqual(fx, [])
        self.assertEqual(s.lanes["fps1"].op, S.OP_LOADED)
        self.assertEqual(s.lanes["fps1"].unit, (1, 0))
        self.assertEqual(s.lanes["fps2"].op, S.OP_UNLOADED)
        self.assertIsNone(s.lanes["fps2"].unit)


class MultiLaneTests(unittest.TestCase):
    # fps1 owns group T0 (bays on oams 1); fps2 owns group T1 (bays on oams 2).
    def _world(self, fps1_kwargs=None, fps2_kwargs=None):
        lw1 = S.LaneWorld(group_bays={"T0": ((1, 0), (1, 3))}, path_len={1: 600.0},
                          **(fps1_kwargs or {}))
        lw2 = S.LaneWorld(group_bays={"T1": ((2, 0),)}, path_len={2: 600.0},
                          **(fps2_kwargs or {}))
        return S.World(lanes={"fps1": lw1, "fps2": lw2})

    def _system(self, l1, l2):
        return S.SystemState(lanes={"fps1": l1, "fps2": l2})

    def test_load_routes_to_correct_lane(self):
        sys0 = self._system(S.LaneState(), S.LaneState())
        w = self._world(fps2_kwargs={"ready": {(2, 0): True}})
        sys1, fx = S.reduce(sys0, S.Load("fps2", "T1"), w, now=0.0)
        self.assertEqual(sys1.lanes["fps2"].op, S.OP_LOADING)
        self.assertEqual(sys1.lanes["fps2"].unit, (2, 0))
        self.assertEqual(sys1.lanes["fps1"].op, S.OP_UNLOADED)   # untouched
        self.assertEqual(find(fx, S.StartLoad)[0].unit, (2, 0))

    def test_tick_isolation(self):
        # fps1 is mid-runout (PAUSING); fps2 is idle-LOADED and must be untouched.
        l1 = S.LaneState(op=S.OP_LOADED, group="T0", unit=(1, 0),
                         runout=S.RUNOUT_PAUSING, pause_origin=0.0)
        l2 = S.LaneState(op=S.OP_LOADED, group="T1", unit=(2, 0))
        w = self._world(
            fps1_kwargs={"extruder_pos": S.PAUSE_DISTANCE + 1, "printing": True,
                         "loaded": {(1, 0): False}, "ready": {(1, 3): True}},
            fps2_kwargs={"extruder_pos": 0.0, "printing": True,
                         "loaded": {(2, 0): True}})
        s, fx = S.reduce(self._system(l1, l2), S.Tick(), w, now=1.0)
        self.assertEqual(s.lanes["fps1"].runout, S.RUNOUT_COASTING)  # advanced
        self.assertEqual(s.lanes["fps2"], l2)                        # identical


class OpGenerationTests(unittest.TestCase):
    def test_start_load_stamps_generation(self):
        sys0 = system(S.LaneState(op=S.OP_UNLOADED, op_gen=4))
        sys1, fx = S.reduce(sys0, S.Load(FPS, GROUP), world(ready={BAY_A: True}),
                            now=0.0)
        self.assertEqual(lane_of(sys1).op_gen, 5)
        self.assertEqual(find(fx, S.StartLoad)[0].gen, 5)

    def test_generation_is_not_truncated_to_one_byte(self):
        # The reducer keeps the complete uint32 logical generation; a one-byte
        # wire is the runtime's concern, so op 256 is 256, not 0.
        sys0 = system(S.LaneState(op=S.OP_UNLOADED, op_gen=255))
        sys1, fx = S.reduce(sys0, S.Load(FPS, GROUP), world(ready={BAY_A: True}),
                            now=0.0)
        self.assertEqual(lane_of(sys1).op_gen, 256)
        self.assertEqual(find(fx, S.StartLoad)[0].gen, 256)

    def test_generation_wraps_at_uint32_and_reserves_zero(self):
        # Zero is the firmware sentinel for "no active operation", so the
        # wrap from UINT32_MAX lands on one, and a completion echoing one
        # must match it.
        sys0 = system(S.LaneState(
            op=S.OP_UNLOADED, op_gen=0xFFFFFFFF))
        sys1, fx = S.reduce(sys0, S.Load(FPS, GROUP), world(ready={BAY_A: True}),
                            now=0.0)
        self.assertEqual(lane_of(sys1).op_gen, 1)
        self.assertEqual(find(fx, S.StartLoad)[0].gen, 1)
        sys2, fx2 = S.reduce(sys1, S.OpCompleted(FPS, S.OAMS_OP_CODE_SUCCESS,
                                                 gen=1), world(), now=1.0)
        self.assertEqual(lane_of(sys2).op, S.OP_LOADED)

    def test_zero_generation_never_issued(self):
        for start in (0, 0xFFFFFFFE, 0xFFFFFFFF):
            sys0 = system(S.LaneState(op=S.OP_UNLOADED, op_gen=start))
            sys1, fx = S.reduce(sys0, S.Load(FPS, GROUP),
                                world(ready={BAY_A: True}), now=0.0)
            self.assertNotEqual(find(fx, S.StartLoad)[0].gen, 0)

    def test_stale_generation_completion_is_ignored(self):
        # A reply from a previous (timed-out) op, or from another OAMS unit on
        # the lane, carries an old gen and must not complete the current op.
        sys0 = system(S.LaneState(op=S.OP_LOADING, unit=BAY_A, group=GROUP,
                                  op_gen=7))
        sys1, fx = S.reduce(sys0, S.OpCompleted(FPS, S.OAMS_OP_CODE_SUCCESS,
                                                gen=6), world(), now=1.0)
        self.assertEqual(lane_of(sys1).op, S.OP_LOADING)   # still in flight
        self.assertEqual(fx, [])

    def test_matching_generation_completes(self):
        sys0 = system(S.LaneState(op=S.OP_LOADING, unit=BAY_A, group=GROUP,
                                  op_gen=7))
        sys1, fx = S.reduce(sys0, S.OpCompleted(FPS, S.OAMS_OP_CODE_SUCCESS,
                                                gen=7), world(), now=1.0)
        self.assertEqual(lane_of(sys1).op, S.OP_LOADED)

    def test_unknown_generation_is_rejected(self):
        # gen=None marks an unsolicited firmware status (e.g. from an idle
        # unit on the lane that never started an op); it must not complete
        # the op in flight.
        sys0 = system(S.LaneState(op=S.OP_LOADING, unit=BAY_A, group=GROUP,
                                  op_gen=7))
        sys1, fx = S.reduce(sys0, S.OpCompleted(FPS, S.OAMS_OP_CODE_SUCCESS),
                            world(), now=1.0)
        self.assertEqual(lane_of(sys1).op, S.OP_LOADING)
        self.assertEqual(fx, [])


class TimeoutHardeningTests(unittest.TestCase):
    def test_load_timeout_cancels_firmware_op(self):
        sys0 = system(S.LaneState(op=S.OP_LOADING, unit=BAY_A, group=GROUP))
        sys1, fx = S.reduce(sys0, S.Timeout(FPS), world(), now=999.0)
        self.assertEqual(lane_of(sys1).op, S.OP_UNLOADED)
        self.assertEqual(find(fx, S.CancelLoad)[0].unit, BAY_A)

    def test_load_error_does_not_cancel(self):
        # A real firmware error reply means the op already ended on the MCU.
        sys0 = system(S.LaneState(op=S.OP_LOADING, unit=BAY_A, group=GROUP))
        sys1, fx = S.reduce(sys0, S.OpCompleted(FPS, S.OAMS_OP_CODE_ERROR_BUSY, gen=0),
                            world(), now=1.0)
        self.assertFalse(has(fx, S.CancelLoad))

    def test_unload_timeout_stops_follower(self):
        sys0 = system(S.LaneState(op=S.OP_UNLOADING, unit=BAY_A, group=GROUP))
        sys1, fx = S.reduce(sys0, S.Timeout(FPS), world(), now=999.0)
        self.assertEqual(lane_of(sys1).op, S.OP_LOADED)
        sf = find(fx, S.SetFollower)
        self.assertTrue(sf and sf[0].enable == 0)

    def test_reload_timeout_cancels_firmware_op(self):
        lane = S.LaneState(op=S.OP_LOADED, group=GROUP, unit=BAY_A,
                           runout=S.RUNOUT_LOADING, reload_target=BAY_B)
        s, fx = S.reduce(system(lane), S.Timeout(FPS), world(), now=999.0)
        self.assertEqual(find(fx, S.CancelLoad)[0].unit, BAY_B)
        self.assertTrue(has(fx, S.Pause))

    def test_tick_enforces_missed_deadline(self):
        # Belt-and-braces: even if the runtime's deadline timer was lost, a
        # Tick past op_deadline must fail the op instead of wedging the lane.
        lane = S.LaneState(op=S.OP_LOADING, unit=BAY_A, group=GROUP,
                           op_deadline=100.0)
        s, fx = S.reduce(system(lane), S.Tick(), world(), now=101.0)
        self.assertEqual(lane_of(s).op, S.OP_UNLOADED)
        self.assertFalse(find(fx, S.Settle)[0].result.ok)

    def test_tick_before_deadline_does_not_fail(self):
        lane = S.LaneState(op=S.OP_LOADING, unit=BAY_A, group=GROUP,
                           op_deadline=100.0)
        s, fx = S.reduce(system(lane), S.Tick(), world(), now=99.0)
        self.assertEqual(lane_of(s).op, S.OP_LOADING)
        self.assertEqual(fx, [])


class HubOccupiedTests(unittest.TestCase):
    def test_load_rejected_when_hub_occupied(self):
        # Store says UNLOADED (e.g. after a timeout) but a hub HES still sees
        # filament: starting another load would collide with it.
        sys0 = system(S.LaneState(op=S.OP_UNLOADED))
        w = world(ready={BAY_B: True}, loaded={BAY_A: True})
        sys1, fx = S.reduce(sys0, S.Load(FPS, GROUP), w, now=0.0)
        self.assertEqual(lane_of(sys1).op, S.OP_UNLOADED)
        self.assertFalse(has(fx, S.StartLoad))
        self.assertFalse(find(fx, S.Settle)[0].result.ok)

    def test_load_bay_rejected_when_hub_occupied(self):
        sys0 = system(S.LaneState(op=S.OP_UNLOADED))
        w = world(loaded={BAY_A: True})
        sys1, fx = S.reduce(sys0, S.LoadBay(FPS, BAY_B), w, now=0.0)
        self.assertFalse(has(fx, S.StartLoad))
        self.assertFalse(find(fx, S.Settle)[0].result.ok)


class FollowTests(unittest.TestCase):
    def test_follow_updates_state_and_drives_hardware(self):
        sys0 = system(S.LaneState(op=S.OP_LOADED, unit=BAY_A, group=GROUP))
        sys1, fx = S.reduce(sys0, S.Follow(FPS, 1, S.FOLLOWER_REVERSE),
                            world(), now=0.0)
        self.assertTrue(lane_of(sys1).following)
        self.assertEqual(lane_of(sys1).direction, S.FOLLOWER_REVERSE)
        sf = find(fx, S.SetFollower)[0]
        self.assertEqual((sf.unit, sf.enable, sf.direction),
                         (BAY_A, 1, S.FOLLOWER_REVERSE))

    def test_follow_without_unit_is_noop(self):
        sys0 = system(S.LaneState(op=S.OP_UNLOADED))
        sys1, fx = S.reduce(sys0, S.Follow(FPS, 1, S.FOLLOWER_FORWARD),
                            world(), now=0.0)
        self.assertFalse(lane_of(sys1).following)
        self.assertEqual(fx, [])

    def test_load_failure_stops_follower_enabled_mid_load(self):
        # A follower enabled while a load is in flight must not outlive the
        # lane's knowledge of its unit when the load fails.
        sys0 = system(S.LaneState(op=S.OP_LOADING, unit=BAY_A, group=GROUP,
                                  following=True, direction=S.FOLLOWER_FORWARD))
        sys1, fx = S.reduce(sys0, S.OpCompleted(FPS, S.OAMS_OP_CODE_ERROR_BUSY,
                                                gen=0), world(), now=1.0)
        self.assertEqual(lane_of(sys1).op, S.OP_UNLOADED)
        self.assertFalse(lane_of(sys1).following)
        sf = find(fx, S.SetFollower)
        self.assertTrue(sf and sf[0].enable == 0 and sf[0].unit == BAY_A)

    def test_load_cancel_stops_follower_enabled_mid_load(self):
        sys0 = system(S.LaneState(op=S.OP_LOADING, unit=BAY_A, group=GROUP,
                                  following=True, direction=S.FOLLOWER_FORWARD))
        sys1, fx = S.reduce(sys0, S.OpCompleted(FPS, S.OAMS_OP_CODE_CANCEL,
                                                gen=0), world(), now=1.0)
        self.assertEqual(lane_of(sys1).op, S.OP_UNLOADED)
        self.assertFalse(lane_of(sys1).following)
        sf = find(fx, S.SetFollower)
        self.assertTrue(sf and sf[0].enable == 0)

    def test_coasting_clears_following(self):
        lane = S.LaneState(op=S.OP_LOADED, group=GROUP, unit=BAY_A,
                           following=True, runout=S.RUNOUT_PAUSING,
                           pause_origin=0.0)
        w = world(extruder_pos=S.PAUSE_DISTANCE + 1, printing=True,
                  loaded={BAY_A: False}, ready={BAY_B: True})
        s, fx = S.reduce(system(lane), S.Tick(), w, now=1.0)
        self.assertEqual(lane_of(s).runout, S.RUNOUT_COASTING)
        self.assertFalse(lane_of(s).following)


class StrayCompletionTests(unittest.TestCase):
    def test_completion_with_nothing_in_flight_is_ignored(self):
        sys0 = system(S.LaneState(op=S.OP_LOADED, unit=BAY_A, group=GROUP))
        sys1, fx = S.reduce(sys0, S.OpCompleted(FPS, S.OAMS_OP_CODE_SUCCESS, gen=0),
                            world(), now=1.0)
        self.assertEqual(lane_of(sys1), lane_of(sys0))
        self.assertEqual(fx, [])

    def test_cancel_when_not_loading_is_noop(self):
        sys0 = system(S.LaneState(op=S.OP_LOADED, unit=BAY_A, group=GROUP))
        sys1, fx = S.reduce(sys0, S.Cancel(FPS), world(), now=1.0)
        self.assertEqual(lane_of(sys1), lane_of(sys0))
        self.assertEqual(fx, [])


class FirmwareTimeoutTests(unittest.TestCase):
    # Protocol v3: the firmware's no-progress watchdog stops the motors and
    # completes the op with code TIMEOUT(7). The host treats it as an ordinary
    # failure and must NOT send a cancel (the hardware is already stopped).
    def test_load_timeout_is_failure_without_cancel(self):
        sys0 = system(S.LaneState(op=S.OP_LOADING, unit=BAY_A, group=GROUP,
                                  op_gen=3))
        sys1, fx = S.reduce(sys0, S.OpCompleted(FPS, S.OAMS_OP_CODE_TIMEOUT,
                                                gen=3), world(), now=1.0)
        self.assertEqual(lane_of(sys1).op, S.OP_UNLOADED)
        self.assertFalse(find(fx, S.Settle)[0].result.ok)
        self.assertFalse(has(fx, S.CancelLoad))
        self.assertIn("progress", find(fx, S.Settle)[0].result.message)

    def test_unload_timeout_stops_follower_without_cancel(self):
        sys0 = system(S.LaneState(op=S.OP_UNLOADING, unit=BAY_A, group=GROUP,
                                  op_gen=2))
        sys1, fx = S.reduce(sys0, S.OpCompleted(FPS, S.OAMS_OP_CODE_TIMEOUT,
                                                gen=2), world(), now=1.0)
        self.assertEqual(lane_of(sys1).op, S.OP_LOADED)
        sf = find(fx, S.SetFollower)
        self.assertTrue(sf and sf[0].enable == 0)
        self.assertFalse(has(fx, S.CancelLoad))

    def test_describe_timeout_is_human_readable(self):
        self.assertIn("progress", S.describe_code(S.OAMS_OP_CODE_TIMEOUT))

    def test_reload_timeout_code_pauses_without_cancel(self):
        # Firmware TIMEOUT(7) during a runout auto-reload: the firmware
        # already stopped, so pause but do NOT send a cancel.
        lane = S.LaneState(op=S.OP_LOADED, group=GROUP, unit=BAY_A,
                           runout=S.RUNOUT_LOADING, reload_target=BAY_B,
                           op_gen=4)
        s, fx = S.reduce(system(lane),
                         S.OpCompleted(FPS, S.OAMS_OP_CODE_TIMEOUT, gen=4),
                         world(), now=1.0)
        self.assertTrue(has(fx, S.Pause))
        self.assertFalse(has(fx, S.CancelLoad))
        self.assertEqual(lane_of(s).op, S.OP_UNLOADED)


class RunoutReloadGuardTests(unittest.TestCase):
    # While a runout auto-reload firmware op is in flight (op stays LOADED,
    # runout == RUNOUT_LOADING), starting another op would bump op_gen and
    # orphan the reload's completion — Unload/Calibrate must be rejected.
    def _reloading_lane(self):
        return S.LaneState(op=S.OP_LOADED, group=GROUP, unit=BAY_A,
                           runout=S.RUNOUT_LOADING, reload_target=BAY_B,
                           op_gen=4)

    def test_unload_rejected_during_reload(self):
        s, fx = S.reduce(system(self._reloading_lane()), S.Unload(FPS),
                         world(), now=1.0)
        self.assertEqual(lane_of(s).op, S.OP_LOADED)
        self.assertEqual(lane_of(s).runout, S.RUNOUT_LOADING)  # untouched
        self.assertEqual(lane_of(s).op_gen, 4)                 # not bumped
        self.assertFalse(has(fx, S.StartUnload))
        self.assertFalse(find(fx, S.Settle)[0].result.ok)

    def test_calibrate_rejected_during_reload(self):
        s, fx = S.reduce(system(self._reloading_lane()),
                         S.Calibrate(FPS, oams_idx=1, bay=2, kind="ptfe"),
                         world(), now=1.0)
        self.assertEqual(lane_of(s).op, S.OP_LOADED)           # not CALIBRATING
        self.assertEqual(lane_of(s).op_gen, 4)
        self.assertFalse(has(fx, S.StartCalibrate))
        # ...and the reload can still complete normally afterwards
        s2, fx2 = S.reduce(s, S.OpCompleted(FPS, S.OAMS_OP_CODE_SUCCESS, gen=4),
                           world(), now=2.0)
        self.assertEqual(lane_of(s2).unit, BAY_B)
        self.assertEqual(lane_of(s2).runout, S.RUNOUT_IDLE)


class LivenessTests(unittest.TestCase):
    def test_coarse_deadline_when_firmware_owns_liveness(self):
        sys0 = S.set_liveness(system(S.LaneState(op=S.OP_UNLOADED)), True)
        sys1, fx = S.reduce(sys0, S.Load(FPS, GROUP), world(ready={BAY_A: True}),
                            now=100.0)
        self.assertEqual(lane_of(sys1).op_deadline,
                         100.0 + S.OAMS_DISCONNECT_BACKSTOP)
        self.assertEqual(find(fx, S.ArmDeadline)[0].seconds,
                         S.OAMS_DISCONNECT_BACKSTOP)

    def test_authoritative_deadline_for_legacy_firmware(self):
        sys0 = system(S.LaneState(op=S.OP_UNLOADED))   # fw_owns_liveness=False
        sys1, fx = S.reduce(sys0, S.Load(FPS, GROUP), world(ready={BAY_A: True}),
                            now=100.0)
        self.assertEqual(find(fx, S.ArmDeadline)[0].seconds,
                         S.OAMS_ACTION_TIMEOUT)

    def test_liveness_flag_survives_every_reduce_path(self):
        sys0 = S.set_liveness(
            system(S.LaneState(op=S.OP_LOADED, unit=BAY_A, group=GROUP)), True)
        sys1, _ = S.reduce(sys0, S.Tick(), world(loaded={BAY_A: True}), now=1.0)
        self.assertTrue(sys1.fw_owns_liveness)
        sys2, _ = S.reduce(sys1, S.Follow(FPS, 1, S.FOLLOWER_FORWARD),
                           world(), now=2.0)
        self.assertTrue(sys2.fw_owns_liveness)
        sys3, _ = S.reduce(sys2, S.ClearErrors(), world(loaded={BAY_A: True}),
                           now=3.0)
        self.assertTrue(sys3.fw_owns_liveness)


class FirmwareFaultLatchTests(unittest.TestCase):
    """Reducer must settle request-type actions with a refusal when a firmware
    fault is latched, instead of silently dropping them and stranding waiters."""

    IDENTITY = (9, 8, 7)
    LANE_PHYSICAL = 0   # physical_lane that matches BAY_A's bay index

    def _system_with_lane_fault(self, lane_state=None):
        """System with a fault latched on the lane's physical bay."""
        if lane_state is None:
            lane_state = S.LaneState(op=S.OP_UNLOADED)
        return S.SystemState(
            lanes={FPS: lane_state},
            firmware_faults={self.LANE_PHYSICAL: (FPS, 5, self.IDENTITY)})

    def _system_with_global_fault(self, lane_state=None):
        """System with a global (physical_lane 255) fault latched."""
        if lane_state is None:
            lane_state = S.LaneState(op=S.OP_UNLOADED)
        return S.SystemState(
            lanes={FPS: lane_state},
            firmware_faults={255: (None, 12, self.IDENTITY)})

    def _assert_fault_settle(self, sys0, action):
        """Reduce action and assert exactly one Settle with ok=False and the
        firmware fault message; the system state must be unchanged."""
        s, fx = S.reduce(sys0, action, world(), now=1.0)
        self.assertEqual(s, sys0)
        settles = find(fx, S.Settle)
        self.assertEqual(len(settles), 1, "expected exactly one Settle")
        result = settles[0].result
        self.assertFalse(result.ok)
        self.assertIn("firmware fault", result.message)
        self.assertIn("OAMS_CLEAR_FAULT", result.message)
        # No op should have started
        self.assertFalse(has(fx, S.StartLoad))
        self.assertFalse(has(fx, S.StartUnload))
        self.assertFalse(has(fx, S.StartCalibrate))

    # --- lane-specific fault ---

    def test_load_refused_with_lane_fault(self):
        self._assert_fault_settle(
            self._system_with_lane_fault(),
            S.Load(FPS, GROUP))

    def test_load_bay_refused_with_lane_fault(self):
        self._assert_fault_settle(
            self._system_with_lane_fault(),
            S.LoadBay(FPS, BAY_A))

    def test_unload_refused_with_lane_fault(self):
        # Put the lane in LOADED so Unload would normally start.
        self._assert_fault_settle(
            self._system_with_lane_fault(S.LaneState(op=S.OP_LOADED, unit=BAY_A,
                                                     group=GROUP)),
            S.Unload(FPS))

    def test_calibrate_refused_with_lane_fault(self):
        self._assert_fault_settle(
            self._system_with_lane_fault(),
            S.Calibrate(FPS, oams_idx=BAY_A[0], bay=BAY_A[1], kind="ptfe"))

    # --- global (physical_lane 255) fault ---

    def test_load_refused_with_global_fault(self):
        self._assert_fault_settle(
            self._system_with_global_fault(),
            S.Load(FPS, GROUP))

    def test_load_bay_refused_with_global_fault(self):
        self._assert_fault_settle(
            self._system_with_global_fault(),
            S.LoadBay(FPS, BAY_A))

    def test_unload_refused_with_global_fault(self):
        self._assert_fault_settle(
            self._system_with_global_fault(
                S.LaneState(op=S.OP_LOADED, unit=BAY_A, group=GROUP)),
            S.Unload(FPS))

    def test_calibrate_refused_with_global_fault(self):
        self._assert_fault_settle(
            self._system_with_global_fault(),
            S.Calibrate(FPS, oams_idx=BAY_A[0], bay=BAY_A[1], kind="ptfe"))

    # --- message content ---

    def test_fault_message_contains_code(self):
        sys0 = self._system_with_lane_fault()
        _, fx = S.reduce(sys0, S.Load(FPS, GROUP), world(), now=1.0)
        msg = find(fx, S.Settle)[0].result.message
        self.assertIn("5", msg)   # the fault code stored in the record

    def test_global_fault_message_contains_code(self):
        sys0 = self._system_with_global_fault()
        _, fx = S.reduce(sys0, S.Load(FPS, GROUP), world(), now=1.0)
        msg = find(fx, S.Settle)[0].result.message
        self.assertIn("12", msg)   # the fault code stored in the global record

    # --- pass-through actions (must NOT produce a Settle) ---

    def test_cancel_passes_through_with_lane_fault(self):
        sys0 = self._system_with_lane_fault(
            S.LaneState(op=S.OP_LOADING, unit=BAY_A, group=GROUP))
        s, fx = S.reduce(sys0, S.Cancel(FPS), world(), now=1.0)
        self.assertEqual(s, sys0)
        self.assertEqual(fx, [])

    def test_follow_passes_through_with_lane_fault(self):
        sys0 = self._system_with_lane_fault(
            S.LaneState(op=S.OP_LOADED, unit=BAY_A, group=GROUP))
        s, fx = S.reduce(sys0, S.Follow(FPS, 0, S.FOLLOWER_FORWARD), world(), now=1.0)
        self.assertEqual(s, sys0)
        self.assertEqual(fx, [])

    def test_op_completed_dropped_with_lane_fault(self):
        # OpCompleted for an in-flight op is also blocked by the fault gate:
        # by the time a fault is latched the runtime has already settled the
        # waiter (via accept_firmware_fault), so no second settle is needed.
        # The existing behavior is to drop it silently.
        sys0 = self._system_with_lane_fault(
            S.LaneState(op=S.OP_LOADING, unit=BAY_A, group=GROUP, op_gen=3))
        s, fx = S.reduce(sys0, S.OpCompleted(FPS, S.OAMS_OP_CODE_SUCCESS, gen=3),
                         world(), now=1.0)
        self.assertEqual(s, sys0)
        self.assertEqual(fx, [])


class UnknownLaneTests(unittest.TestCase):
    """An fps that is not None but not registered must produce a refusal Settle
    for request-type actions, not silently drop the action."""

    def _system(self):
        return S.SystemState(lanes={FPS: S.LaneState()})

    def test_load_unknown_fps_refused(self):
        s, fx = S.reduce(self._system(), S.Load("no_such_fps", GROUP),
                         world(), now=1.0)
        settles = find(fx, S.Settle)
        self.assertEqual(len(settles), 1)
        self.assertFalse(settles[0].result.ok)
        self.assertIn("unknown lane", settles[0].result.message)
        self.assertEqual(s, self._system())

    def test_load_bay_unknown_fps_refused(self):
        s, fx = S.reduce(self._system(), S.LoadBay("no_such_fps", BAY_A),
                         world(), now=1.0)
        settles = find(fx, S.Settle)
        self.assertEqual(len(settles), 1)
        self.assertFalse(settles[0].result.ok)
        self.assertIn("unknown lane", settles[0].result.message)

    def test_unload_unknown_fps_refused(self):
        s, fx = S.reduce(self._system(), S.Unload("no_such_fps"),
                         world(), now=1.0)
        settles = find(fx, S.Settle)
        self.assertEqual(len(settles), 1)
        self.assertFalse(settles[0].result.ok)
        self.assertIn("unknown lane", settles[0].result.message)

    def test_calibrate_unknown_fps_refused(self):
        s, fx = S.reduce(self._system(), S.Calibrate("no_such_fps", 1, 0, "ptfe"),
                         world(), now=1.0)
        settles = find(fx, S.Settle)
        self.assertEqual(len(settles), 1)
        self.assertFalse(settles[0].result.ok)
        self.assertIn("unknown lane", settles[0].result.message)

    def test_fps_none_still_ignored(self):
        # fps=None must remain silent (it marks non-lane-scoped actions that
        # somehow reach the lane section; the existing behavior must not change).
        s, fx = S.reduce(self._system(), S.Tick(), world(), now=1.0)
        self.assertFalse(has(fx, S.Settle))


class LaneFaultFieldsTests(unittest.TestCase):
    """C2 §4.8: a lane failure records the reducer's reason and the failed bay
    on the lane, so the manager can publish them as a lane fault entry."""

    def _loading_lane(self):
        sys1, _ = S.reduce(system(S.LaneState()), S.Load(FPS, GROUP),
                           world(ready={BAY_A: True}), now=1.0)
        self.assertEqual(lane_of(sys1).op, S.OP_LOADING)
        self.assertEqual(lane_of(sys1).unit, BAY_A)
        return sys1

    def _fail_load(self, sys1):
        return S.reduce(
            sys1, S.OpCompleted(FPS, S.OAMS_OP_CODE_TIMEOUT,
                                gen=lane_of(sys1).op_gen), world(), now=2.0)

    def test_failed_load_sets_fault_text_and_unit(self):
        sys2, fx = self._fail_load(self._loading_lane())
        lane = lane_of(sys2)
        self.assertEqual(lane.message, "load failed")
        self.assertEqual(lane.fault_unit, BAY_A)
        self.assertTrue(lane.fault_text.startswith("Spool loading failed ("))
        self.assertEqual(find(fx, S.Settle)[0].result.message, lane.fault_text)

    def test_clear_errors_resyncs_lane_message(self):
        sys2, _ = self._fail_load(self._loading_lane())
        self.assertEqual(lane_of(sys2).message, "load failed")
        sys3, _ = S.reduce(sys2, S.ClearErrors(), world(), now=3.0)
        self.assertIsNone(lane_of(sys3).message)


class MotionBlockedTests(unittest.TestCase):
    """H15: a load on a unit that may not move filament is refused with the
    unit's reason before any command, and the lane is unchanged."""

    REASON = "This firmware does not support normal filament motion"

    def test_group_load_on_blocked_unit_refused(self):
        sys0 = system(S.LaneState())
        w = world(ready={BAY_A: True, BAY_B: True},
                  motion_blocked={1: self.REASON})
        sys1, fx = S.reduce(sys0, S.Load(FPS, GROUP), w, now=1.0)
        self.assertEqual(lane_of(sys1), lane_of(sys0))
        self.assertFalse(has(fx, S.StartLoad))
        settles = find(fx, S.Settle)
        self.assertEqual(len(settles), 1)
        self.assertFalse(settles[0].result.ok)
        self.assertEqual(settles[0].result.message, self.REASON)

    def test_load_bay_on_blocked_unit_refused(self):
        sys0 = system(S.LaneState())
        w = world(ready={BAY_A: True}, motion_blocked={1: self.REASON})
        sys1, fx = S.reduce(sys0, S.LoadBay(FPS, BAY_A), w, now=1.0)
        self.assertEqual(lane_of(sys1), lane_of(sys0))
        self.assertFalse(has(fx, S.StartLoad))
        settles = find(fx, S.Settle)
        self.assertEqual(len(settles), 1)
        self.assertFalse(settles[0].result.ok)
        self.assertEqual(settles[0].result.message, self.REASON)


class PositioningTests(unittest.TestCase):
    """H16: while a unit positions a newly inserted spool it answers BUSY to
    every motion command, so the reducer refuses loads on it and a runout
    reload waits instead of pausing the print."""

    POS_MSG = ("OAMS 1 is positioning a newly inserted spool; retry when it"
               " finishes")
    BUSY_MSG = "OAMS is busy (positioning a newly inserted spool?); retry"

    def _world(self, **kw):
        return S.World(lanes={FPS: S.LaneWorld(
            loaded=kw.get("loaded", {}), ready=kw.get("ready", {}),
            positioning=kw.get("positioning", {}),
            extruder_pos=kw.get("extruder_pos", 0.0),
            group_bays=kw.get("group_bays", {GROUP: (BAY_A, BAY_B)}),
            path_len=kw.get("path_len", {1: 600.0}))})

    def test_group_load_on_a_positioning_unit_is_refused(self):
        sys0 = system(S.LaneState())
        w = self._world(ready={BAY_A: True}, positioning={1: frozenset({2})})
        sys1, fx = S.reduce(sys0, S.Load(FPS, GROUP), w, now=1.0)
        self.assertEqual(lane_of(sys1), lane_of(sys0))
        self.assertFalse(has(fx, S.StartLoad))
        settles = find(fx, S.Settle)
        self.assertEqual(len(settles), 1)
        self.assertFalse(settles[0].result.ok)
        self.assertEqual(settles[0].result.message, self.POS_MSG)

    def test_load_bay_on_a_positioning_unit_is_refused(self):
        sys0 = system(S.LaneState())
        w = self._world(ready={BAY_A: True}, positioning={1: frozenset({2})})
        sys1, fx = S.reduce(sys0, S.LoadBay(FPS, BAY_A), w, now=1.0)
        self.assertEqual(lane_of(sys1), lane_of(sys0))
        self.assertFalse(has(fx, S.StartLoad))
        settles = find(fx, S.Settle)
        self.assertEqual(len(settles), 1)
        self.assertFalse(settles[0].result.ok)
        self.assertEqual(settles[0].result.message, self.POS_MSG)

    def test_coasting_runout_waits_for_a_positioning_spare(self):
        lane = S.LaneState(op=S.OP_LOADED, group=GROUP, unit=(1, 0),
                           runout=S.RUNOUT_COASTING, coast_origin=0.0,
                           pause_origin=0.0, following=False)
        w = self._world(extruder_pos=1000.0, loaded={(1, 0): True},
                        ready={(2, 0): True},
                        positioning={2: frozenset({0})},
                        group_bays={GROUP: ((1, 0), (2, 0))},
                        path_len={1: 600.0, 2: 600.0})
        sys1, fx = S.reduce(system(lane), S.Tick(), w, now=5.0)
        self.assertEqual(lane_of(sys1), lane)   # still coasting
        self.assertEqual(fx, [])

    def test_busy_load_reply_is_a_refusal_not_a_failure(self):
        lane = S.LaneState(op=S.OP_LOADING, group=GROUP, unit=BAY_A,
                           op_gen=1, following=False)
        sys1, fx = S.reduce(
            system(lane),
            S.OpCompleted(FPS, S.OAMS_OP_CODE_ERROR_BUSY, gen=1),
            S.World(), now=2.0)
        nl = lane_of(sys1)
        self.assertEqual(nl.op, S.OP_UNLOADED)
        self.assertIsNone(nl.unit)
        self.assertIsNone(nl.group)
        self.assertFalse(nl.following)
        self.assertIsNone(nl.op_deadline)
        self.assertIsNone(nl.message)          # a refusal is not a failure
        settles = find(fx, S.Settle)
        self.assertEqual(len(settles), 1)
        self.assertFalse(settles[0].result.ok)
        self.assertEqual(settles[0].result.message, self.BUSY_MSG)
        self.assertFalse(has(fx, S.Pause))
        self.assertFalse(has(fx, S.CancelLoad))  # no op ever started


class UnitSessionLostTests(unittest.TestCase):
    """H9c-2: a unit whose host-communication watchdog fires ends its lane's
    op at once, with the reason, and sends nothing (the unit no longer
    listens)."""

    REASON = "unit 1 lost its session (host watchdog)"

    def _lost(self, lane, oams_idx=1, printing=False):
        return S.reduce(system(lane),
                        S.UnitSessionLost(FPS, oams_idx, self.REASON, printing),
                        world(), now=9.0)

    def assert_no_command(self, fx):
        for cls in (S.StartLoad, S.StartUnload, S.CancelLoad, S.SetFollower):
            self.assertFalse(has(fx, cls), "unexpected %s" % cls.__name__)

    def test_not_a_world_action(self):
        self.assertNotIn(S.UnitSessionLost, S.WORLD_ACTIONS)

    def test_loading_lane_settles_unloaded_now(self):
        lane = S.LaneState(op=S.OP_LOADING, group=GROUP, unit=BAY_A,
                           op_deadline=100.0, following=False)
        sys1, fx = self._lost(lane)
        nl = lane_of(sys1)
        self.assertEqual(nl.op, S.OP_UNLOADED)
        self.assertIsNone(nl.group)
        self.assertIsNone(nl.unit)
        self.assertFalse(nl.following)
        self.assertIsNone(nl.op_deadline)
        self.assertEqual(nl.message, "load failed")
        self.assertEqual(nl.fault_unit, BAY_A)
        self.assertEqual(nl.fault_text, self.REASON)
        self.assertTrue(has(fx, S.CancelDeadline))
        settles = find(fx, S.Settle)
        self.assertEqual(len(settles), 1)
        self.assertFalse(settles[0].result.ok)
        self.assertIsNone(settles[0].result.code)
        self.assertEqual(settles[0].result.message, self.REASON)
        self.assert_no_command(fx)

    def test_unloading_lane_returns_to_loaded(self):
        lane = S.LaneState(op=S.OP_UNLOADING, group=GROUP, unit=BAY_A,
                           op_deadline=100.0, following=False)
        sys1, fx = self._lost(lane)
        nl = lane_of(sys1)
        self.assertEqual(nl.op, S.OP_LOADED)
        self.assertEqual(nl.unit, BAY_A)
        self.assertFalse(nl.following)
        self.assertIsNone(nl.op_deadline)
        self.assertEqual(nl.message, "unload failed")
        self.assertEqual(nl.fault_unit, BAY_A)
        self.assertEqual(nl.fault_text, self.REASON)
        self.assertTrue(has(fx, S.CancelDeadline))
        settles = find(fx, S.Settle)
        self.assertEqual(len(settles), 1)
        self.assertFalse(settles[0].result.ok)
        self.assertEqual(settles[0].result.message, self.REASON)
        self.assert_no_command(fx)

    def test_calibrating_lane_returns_to_prior_op(self):
        lane = S.LaneState(op=S.OP_CALIBRATING, unit=BAY_A,
                           prior_op=S.OP_LOADED, op_deadline=100.0)
        sys1, fx = self._lost(lane)
        nl = lane_of(sys1)
        self.assertEqual(nl.op, S.OP_LOADED)
        self.assertIsNone(nl.prior_op)
        self.assertIsNone(nl.op_deadline)
        self.assertEqual(nl.message, "calibration failed")
        self.assertEqual(nl.fault_unit, BAY_A)
        self.assertEqual(nl.fault_text, self.REASON)
        self.assertTrue(has(fx, S.CancelDeadline))
        settles = find(fx, S.Settle)
        self.assertEqual(len(settles), 1)
        self.assertFalse(settles[0].result.ok)
        self.assertEqual(settles[0].result.message, self.REASON)
        self.assert_no_command(fx)

    def test_calibrating_lane_without_prior_op_falls_back_to_unloaded(self):
        lane = S.LaneState(op=S.OP_CALIBRATING, unit=BAY_A,
                           prior_op=None, op_deadline=100.0)
        sys1, fx = self._lost(lane)
        self.assertEqual(lane_of(sys1).op, S.OP_UNLOADED)
        self.assert_no_command(fx)

    def test_reload_target_loss_pauses_without_a_command(self):
        lane = S.LaneState(op=S.OP_LOADED, group=GROUP, unit=BAY_A,
                           runout=S.RUNOUT_LOADING, reload_target=BAY_B,
                           pause_origin=1.0, coast_origin=2.0,
                           op_deadline=100.0, following=True)
        sys1, fx = self._lost(lane, oams_idx=BAY_B[0])
        nl = lane_of(sys1)
        self.assertEqual(nl.op, S.OP_UNLOADED)
        self.assertIsNone(nl.group)
        self.assertIsNone(nl.unit)
        self.assertEqual(nl.runout, S.RUNOUT_IDLE)
        # C2 §5.3 rule 5: the failed reload keeps its target (and the runout's
        # origin) so the UI can still show which spool ran out.
        self.assertEqual(nl.reload_target, BAY_B)
        self.assertIsNone(nl.runout_from)
        self.assertIsNone(nl.pause_origin)
        self.assertIsNone(nl.coast_origin)
        self.assertIsNone(nl.op_deadline)
        self.assertEqual(nl.message, "reload failed")
        self.assertEqual(nl.fault_unit, BAY_B)
        self.assertEqual(nl.fault_text, self.REASON)
        self.assertTrue(has(fx, S.CancelDeadline))
        self.assertFalse(has(fx, S.Settle))
        pauses = find(fx, S.Pause)
        self.assertEqual(len(pauses), 1)
        self.assertEqual(pauses[0].reason, self.REASON)
        self.assert_no_command(fx)

    def test_loaded_lane_drops_following_and_pauses_when_printing(self):
        lane = S.LaneState(op=S.OP_LOADED, group=GROUP, unit=BAY_A,
                           following=True, direction=S.FOLLOWER_FORWARD)
        sys1, fx = self._lost(lane, printing=True)
        nl = lane_of(sys1)
        self.assertEqual(nl.op, S.OP_LOADED)
        self.assertEqual(nl.unit, BAY_A)
        self.assertFalse(nl.following)
        pauses = find(fx, S.Pause)
        self.assertEqual(len(pauses), 1)
        self.assertEqual(pauses[0].reason, self.REASON)
        self.assert_no_command(fx)

    def test_loaded_lane_drops_following_silently_when_not_printing(self):
        lane = S.LaneState(op=S.OP_LOADED, group=GROUP, unit=BAY_A,
                           following=True)
        sys1, fx = self._lost(lane, printing=False)
        self.assertFalse(lane_of(sys1).following)
        self.assertEqual(fx, [])

    def test_loaded_lane_coasting_is_left_alone(self):
        lane = S.LaneState(op=S.OP_LOADED, group=GROUP, unit=BAY_A,
                           runout=S.RUNOUT_COASTING, coast_origin=0.0,
                           pause_origin=0.0, following=False)
        sys1, fx = self._lost(lane, printing=True)
        self.assertEqual(lane_of(sys1), lane)
        self.assertEqual(fx, [])

    def test_another_units_loss_changes_nothing(self):
        lane = S.LaneState(op=S.OP_LOADING, group=GROUP, unit=BAY_A,
                           op_deadline=100.0, following=False)
        sys1, fx = self._lost(lane, oams_idx=2)
        self.assertEqual(lane_of(sys1), lane)
        self.assertEqual(fx, [])


class StageActionTests(unittest.TestCase):
    """C2 §5.3 rule 2 / C3 §4: OAMSM_STAGE is pure and never errors. An
    invalid request leaves the lane exactly as it was."""

    def test_plan_announces_the_steps(self):
        s, fx = S.reduce(system(S.LaneState()),
                         S.StagePlan(FPS, ("cut", "feed")), world(), now=7.0)
        lane = lane_of(s)
        self.assertEqual(lane.stage_plan, ("cut", "feed"))
        self.assertIsNone(lane.stage_index)       # announced, not started
        self.assertIsNone(lane.stage_failed)
        self.assertFalse(lane.stage_auto)
        self.assertEqual(lane.stage_since, 7.0)
        self.assertEqual(fx, [])

    def test_plan_replaces_an_earlier_plan_and_its_failure(self):
        lane = S.LaneState(stage_plan=("feed",), stage_index=0,
                           stage_failed="feed", stage_auto=True, stage_since=1.0)
        s, fx = S.reduce(system(lane), S.StagePlan(FPS, ("cut", "retract")),
                         world(), now=9.0)
        nl = lane_of(s)
        self.assertEqual(nl.stage_plan, ("cut", "retract"))
        self.assertIsNone(nl.stage_index)
        self.assertIsNone(nl.stage_failed)
        self.assertFalse(nl.stage_auto)
        self.assertEqual(fx, [])

    def test_plan_rejects_an_empty_oversized_or_malformed_step(self):
        lane = S.LaneState(stage_plan=("feed",))
        # A custom name is legal, so what is refused is a name that breaks the
        # name rule, an empty plan, a duplicate and an over-long one.
        plans = ((), ("Feed",), ("2cut",), ("cut", "cut"),
                 ("a" * 17,), tuple("abcdefghi"))
        for plan in plans:
            s, fx = S.reduce(system(lane), S.StagePlan(FPS, plan), world(),
                             now=2.0)
            self.assertEqual(lane_of(s), lane, plan)
            self.assertEqual(fx, [], plan)

    def test_enter_starts_an_announced_step(self):
        lane = S.LaneState(stage_plan=("cut", "retract", "feed"))
        s, fx = S.reduce(system(lane), S.StageEnter(FPS, "retract"), world(),
                         now=3.0)
        nl = lane_of(s)
        self.assertEqual(nl.stage_index, 1)
        self.assertEqual(nl.stage_since, 3.0)
        self.assertEqual(fx, [])

    def test_enter_rejects_a_step_outside_the_plan(self):
        lane = S.LaneState(stage_plan=("feed",))
        s, _ = S.reduce(system(lane), S.StageEnter(FPS, "purge"), world(),
                        now=1.0)
        self.assertEqual(lane_of(s), lane)
        # a vocabulary step the plan does not contain is refused too
        s, _ = S.reduce(system(S.LaneState()), S.StageEnter(FPS, "feed"),
                        world(), now=1.0)
        self.assertEqual(lane_of(s), S.LaneState())

    def test_enter_never_moves_the_index_backwards(self):
        lane = S.LaneState(stage_plan=("cut", "retract", "feed"), stage_index=2)
        s, _ = S.reduce(system(lane), S.StageEnter(FPS, "cut"), world(), now=4.0)
        self.assertEqual(lane_of(s).stage_index, 2)
        s, _ = S.reduce(system(lane), S.StageEnter(FPS, "feed"), world(), now=4.0)
        self.assertEqual(lane_of(s).stage_index, 2)      # same step: no-op

    def test_fail_names_the_current_step_and_the_first_failure_wins(self):
        lane = S.LaneState(stage_plan=("cut", "retract", "feed"), stage_index=1)
        s, fx = S.reduce(system(lane), S.StageFail(FPS), world(), now=5.0)
        self.assertEqual(lane_of(s).stage_failed, "retract")
        self.assertEqual(lane_of(s).stage_since, 5.0)
        self.assertEqual(fx, [])
        # moving on to the next step does not rewrite the failure
        s, _ = S.reduce(s, S.StageEnter(FPS, "feed"), world(), now=6.0)
        self.assertEqual(lane_of(s).stage_index, 2)
        self.assertEqual(lane_of(s).stage_failed, "retract")
        s, _ = S.reduce(s, S.StageFail(FPS), world(), now=7.0)
        self.assertEqual(lane_of(s).stage_failed, "retract")

    def test_fail_without_a_started_step_does_nothing(self):
        # no plan at all
        s, _ = S.reduce(system(S.LaneState()), S.StageFail(FPS), world(),
                        now=1.0)
        self.assertIsNone(lane_of(s).stage_failed)
        # announced but never started: there is no step to blame
        lane = S.LaneState(stage_plan=("feed",))
        s, _ = S.reduce(system(lane), S.StageFail(FPS), world(), now=1.0)
        self.assertEqual(lane_of(s), lane)

    def test_done_clears_a_plan_and_is_harmless_without_one(self):
        lane = S.LaneState(stage_plan=("cut", "feed"), stage_index=1,
                           stage_failed="feed", stage_since=4.0,
                           stage_labels=(("cut", "Cut"),), stage_detail="50 mm")
        s, fx = S.reduce(system(lane), S.StageDone(FPS), world(), now=8.0)
        nl = lane_of(s)
        self.assertEqual(nl.stage_plan, ())
        self.assertIsNone(nl.stage_index)
        self.assertIsNone(nl.stage_failed)
        self.assertFalse(nl.stage_auto)
        self.assertEqual(nl.stage_labels, ())
        self.assertIsNone(nl.stage_detail)
        self.assertEqual(fx, [])
        s, fx = S.reduce(system(S.LaneState()), S.StageDone(FPS), world(),
                         now=8.0)
        self.assertEqual(fx, [])

    def test_stage_actions_ignore_an_unregistered_lane(self):
        s, fx = S.reduce(system(S.LaneState()), S.StagePlan("fps9", ("feed",)),
                         world(), now=1.0)
        self.assertEqual(lane_of(s), S.LaneState())
        self.assertEqual(fx, [])


class StageLabelAndDetailTests(unittest.TestCase):
    """C2 §5.3: the vocabulary is standard plus custom, a plan carries the
    display labels of its steps, and a detail belongs to its own step."""

    def test_the_standard_names(self):
        self.assertEqual(S.STAGE_NAMES,
                         ("cut", "retract", "feed", "purge", "calibrate",
                          "heat", "home", "clean"))
        for name in S.STAGE_NAMES:
            self.assertTrue(S.stage_name_ok(name), name)

    def test_a_custom_name_is_anything_the_rule_accepts(self):
        for name in ("brush", "park_x2", "a", "s0", "a" * 16):
            self.assertTrue(S.stage_name_ok(name), name)
        for name in ("Brush", "2cut", "_cut", "cut filament", "", "a" * 17,
                     "cut-retract"):
            self.assertFalse(S.stage_name_ok(name), name)

    def test_a_custom_step_with_and_without_a_label(self):
        s, fx = S.reduce(system(S.LaneState()),
                         S.StagePlan(FPS, ("cut", "brush"),
                                     (("brush", "Brush the nozzle"),)),
                         world(), now=3.0)
        lane = lane_of(s)
        self.assertEqual(lane.stage_plan, ("cut", "brush"))
        self.assertEqual(lane.stage_labels, (("brush", "Brush the nozzle"),))
        self.assertIsNone(lane.stage_detail)
        self.assertEqual(fx, [])
        # A custom step needs no label: the interface shows its name.
        s, _ = S.reduce(system(S.LaneState()),
                        S.StagePlan(FPS, ("cut", "park_x2")), world(), now=3.0)
        self.assertEqual(lane_of(s).stage_labels, ())

    def test_a_label_for_a_step_the_plan_does_not_carry_is_dropped(self):
        s, _ = S.reduce(system(S.LaneState()),
                        S.StagePlan(FPS, ("cut",), (("feed", "Load"),)),
                        world(), now=3.0)
        self.assertEqual(lane_of(s).stage_labels, ())

    def test_a_new_plan_replaces_the_earlier_labels(self):
        lane = S.LaneState(stage_plan=("cut",), stage_index=0,
                           stage_labels=(("cut", "Cut"),),
                           stage_detail="50 mm")
        s, _ = S.reduce(system(lane), S.StagePlan(FPS, ("feed",)), world(),
                        now=9.0)
        nl = lane_of(s)
        self.assertEqual(nl.stage_labels, ())
        self.assertIsNone(nl.stage_detail)

    def test_a_detail_is_cut_on_a_character_boundary(self):
        lane = S.LaneState(stage_plan=("feed",))
        # 20 two-byte characters is 40 bytes: the 24-byte cut lands mid
        # character and must back off to the last whole one.
        s, _ = S.reduce(system(lane), S.StageEnter(FPS, "feed", "é" * 20),
                        world(), now=4.0)
        detail = lane_of(s).stage_detail
        self.assertEqual(detail, "é" * 12)
        self.assertEqual(detail.encode("utf-8").decode("utf-8"), detail)
        # Entering the same step without a detail clears it.
        s, _ = S.reduce(s, S.StageEnter(FPS, "feed", "  "), world(), now=5.0)
        self.assertIsNone(lane_of(s).stage_detail)

    def test_entering_a_later_step_clears_the_previous_detail(self):
        lane = S.LaneState(stage_plan=("heat", "cut", "feed"), stage_index=0,
                           stage_detail="250 C")
        s, _ = S.reduce(system(lane), S.StageEnter(FPS, "heat"), world(),
                        now=6.0)
        self.assertIsNone(lane_of(s).stage_detail)

    def test_the_automatic_plan_carries_neither_labels_nor_detail(self):
        # An op entering a step of a live macro plan clears that plan's
        # detail; an op that creates the automatic plan brings nothing.
        lane = S.LaneState(stage_plan=("cut", "feed"), stage_index=0,
                           stage_labels=(("cut", "Cut"),),
                           stage_detail="50 mm")
        s, _ = S.reduce(system(lane), S.StageEnter(FPS, "feed"), world(),
                        now=7.0)
        self.assertEqual(lane_of(s).stage_index, 1)
        self.assertIsNone(lane_of(s).stage_detail)
        self.assertEqual(lane_of(s).stage_labels, (("cut", "Cut"),))
        # No macro plan (or a stale announced one): the automatic one-step
        # plan the manager creates is bare.
        s, _ = S.reduce(system(S.LaneState()),
                        S.StagePlan(FPS, ("cut", "feed"),
                                    (("feed", "Load"),)), world(), now=7.0)
        # Announced but never entered, so it is stale and the op takes the
        # lane over with its own bare one-step plan.
        lane = S._stage_step(lane_of(s), "feed", 8.0)
        self.assertTrue(lane.stage_auto)
        self.assertEqual(lane.stage_plan, ("feed",))
        self.assertEqual(lane.stage_labels, ())
        self.assertIsNone(lane.stage_detail)


class StageAutomaticPlanTests(unittest.TestCase):
    """C2 §5.3 rule 2: the manager's own ops enter their step in an active
    macro plan, or create the automatic one-step plan."""

    def test_load_creates_the_automatic_feed_plan(self):
        s, _ = S.reduce(system(S.LaneState(op=S.OP_UNLOADED)),
                        S.Load(FPS, GROUP), world(ready={BAY_A: True}), now=1.0)
        nl = lane_of(s)
        self.assertEqual(nl.op, S.OP_LOADING)
        self.assertEqual(nl.stage_plan, ("feed",))
        self.assertEqual(nl.stage_index, 0)
        self.assertTrue(nl.stage_auto)

    def test_load_bay_creates_the_automatic_feed_plan(self):
        s, _ = S.reduce(system(S.LaneState(op=S.OP_UNLOADED)),
                        S.LoadBay(FPS, BAY_B), world(), now=1.0)
        self.assertEqual(lane_of(s).stage_plan, ("feed",))
        self.assertTrue(lane_of(s).stage_auto)

    def test_unload_creates_the_automatic_retract_plan(self):
        lane = S.LaneState(op=S.OP_LOADED, group=GROUP, unit=BAY_A)
        s, _ = S.reduce(system(lane), S.Unload(FPS), world(), now=1.0)
        self.assertEqual(lane_of(s).op, S.OP_UNLOADING)
        self.assertEqual(lane_of(s).stage_plan, ("retract",))
        self.assertTrue(lane_of(s).stage_auto)

    def test_calibrate_creates_the_automatic_calibrate_plan(self):
        s, _ = S.reduce(system(S.LaneState()), S.Calibrate(FPS, 1, 0), world(),
                        now=1.0)
        self.assertEqual(lane_of(s).op, S.OP_CALIBRATING)
        self.assertEqual(lane_of(s).stage_plan, ("calibrate",))
        self.assertTrue(lane_of(s).stage_auto)

    def test_load_enters_feed_in_an_active_macro_plan(self):
        # As the real macros do: NAME=feed already entered the step before
        # the manager's own load starts (oams_macros.cfg's
        # _OAMS_CONTINUE_AFTER_UNLOAD sends OAMSM_STAGE NAME=feed immediately
        # before OAMSM_LOAD_FILAMENT).
        lane = S.LaneState(stage_plan=("cut", "retract", "feed"),
                           stage_index=2)
        s, _ = S.reduce(system(lane), S.Load(FPS, GROUP),
                        world(ready={BAY_A: True}), now=2.0)
        nl = lane_of(s)
        self.assertEqual(nl.stage_plan, ("cut", "retract", "feed"))
        self.assertEqual(nl.stage_index, 2)
        self.assertFalse(nl.stage_auto)

    def test_load_leaves_a_macro_plan_without_feed_alone(self):
        lane = S.LaneState(stage_plan=("cut", "retract"), stage_index=0)
        s, _ = S.reduce(system(lane), S.Load(FPS, GROUP),
                        world(ready={BAY_A: True}), now=2.0)
        nl = lane_of(s)
        self.assertEqual(nl.stage_plan, ("cut", "retract"))
        self.assertEqual(nl.stage_index, 0)
        self.assertFalse(nl.stage_auto)

    def test_load_with_a_never_entered_plan_falls_through_to_automatic(self):
        """a plan that was announced (PLAN=)
        but never entered (``index`` still ``None``) is stale -- it never
        got the STAGE FAILED=1 a strict macro raise should have sent, so a
        later manager op must not silently resume it."""
        lane = S.LaneState(stage_plan=("cut", "retract", "feed"))
        self.assertIsNone(lane.stage_index)
        s, _ = S.reduce(system(lane), S.Load(FPS, GROUP),
                        world(ready={BAY_A: True}), now=2.0)
        nl = lane_of(s)
        self.assertEqual(nl.stage_plan, ("feed",))
        self.assertEqual(nl.stage_index, 0)
        self.assertTrue(nl.stage_auto)

    def test_load_with_a_backward_step_falls_through_to_automatic(self):
        """a macro plan already past the step
        this op is entering is stale (the step would move it backwards);
        never resumed, and never attributed a backward index."""
        lane = S.LaneState(op=S.OP_LOADED, unit=BAY_A,
                           stage_plan=("cut", "retract", "feed"),
                           stage_index=2)
        s, _ = S.reduce(system(lane), S.Unload(FPS), world(), now=2.0)
        nl = lane_of(s)
        self.assertNotEqual(nl.stage_plan, ("cut", "retract", "feed"))
        self.assertEqual(nl.stage_plan, ("retract",))
        self.assertEqual(nl.stage_index, 0)
        self.assertTrue(nl.stage_auto)


class StageCompletionTests(unittest.TestCase):
    """C2 §5.3 rule 3: a completion, timeout or session loss never ends a
    macro plan; it ends an automatic one on success and blames the current
    step on any failure."""

    def test_success_ends_an_automatic_plan(self):
        lane = S.LaneState(op=S.OP_LOADING, group=GROUP, unit=BAY_A,
                           stage_plan=("feed",), stage_index=0, stage_auto=True,
                           stage_since=1.0)
        s, _ = S.reduce(system(lane),
                        S.OpCompleted(FPS, S.OAMS_OP_CODE_SUCCESS, gen=0),
                        world(), now=2.0)
        nl = lane_of(s)
        self.assertEqual(nl.op, S.OP_LOADED)
        self.assertEqual(nl.stage_plan, ())
        self.assertIsNone(nl.stage_index)
        self.assertIsNone(nl.stage_failed)
        self.assertFalse(nl.stage_auto)

    def test_success_leaves_a_macro_plan_for_the_macro(self):
        lane = S.LaneState(op=S.OP_LOADING, group=GROUP, unit=BAY_A,
                           stage_plan=("cut", "feed"), stage_index=1,
                           stage_since=1.0)
        s, _ = S.reduce(system(lane),
                        S.OpCompleted(FPS, S.OAMS_OP_CODE_SUCCESS, gen=0),
                        world(), now=2.0)
        nl = lane_of(s)
        self.assertEqual(nl.op, S.OP_LOADED)
        self.assertEqual(nl.stage_plan, ("cut", "feed"))
        self.assertEqual(nl.stage_index, 1)
        self.assertIsNone(nl.stage_failed)

    def test_a_failure_keeps_an_automatic_plan_and_names_the_step(self):
        lane = S.LaneState(op=S.OP_LOADING, group=GROUP, unit=BAY_A,
                           stage_plan=("feed",), stage_index=0, stage_auto=True)
        s, _ = S.reduce(
            system(lane),
            S.OpCompleted(FPS, S.OAMS_OP_CODE_ERROR_UNSPECIFIED, gen=0),
            world(), now=2.0)
        nl = lane_of(s)
        self.assertEqual(nl.op, S.OP_UNLOADED)
        self.assertEqual(nl.stage_plan, ("feed",))
        self.assertEqual(nl.stage_failed, "feed")
        self.assertTrue(nl.stage_auto)

    def test_a_failure_names_the_current_macro_step(self):
        lane = S.LaneState(op=S.OP_LOADING, group=GROUP, unit=BAY_A,
                           stage_plan=("cut", "feed"), stage_index=1)
        s, _ = S.reduce(
            system(lane),
            S.OpCompleted(FPS, S.OAMS_OP_CODE_ERROR_UNSPECIFIED, gen=0),
            world(), now=2.0)
        nl = lane_of(s)
        self.assertEqual(nl.stage_plan, ("cut", "feed"))
        self.assertEqual(nl.stage_failed, "feed")

    def test_timeout_names_the_current_step(self):
        lane = S.LaneState(op=S.OP_LOADING, group=GROUP, unit=BAY_A,
                           stage_plan=("feed",), stage_index=0, stage_auto=True,
                           op_deadline=100.0)
        s, _ = S.reduce(system(lane), S.Timeout(FPS), world(), now=101.0)
        self.assertEqual(lane_of(s).stage_failed, "feed")
        self.assertTrue(lane_of(s).stage_auto)

    def test_session_loss_names_the_current_step(self):
        lane = S.LaneState(op=S.OP_LOADING, group=GROUP, unit=BAY_A,
                           stage_plan=("feed",), stage_index=0, stage_auto=True,
                           op_deadline=100.0)
        s, _ = S.reduce(system(lane),
                        S.UnitSessionLost(FPS, oams_idx=1, reason="silent"),
                        world(), now=2.0)
        self.assertEqual(lane_of(s).stage_failed, "feed")
        self.assertTrue(lane_of(s).stage_auto)


class StageRefusalTests(unittest.TestCase):
    """C2 §5.3 rule 2: a synchronous refusal starts no operation, so it never
    creates a plan; with an active plan it blames the current step."""

    def _refused_load(self, lane):
        return S.reduce(system(lane), S.Load(FPS, GROUP),
                        world(ready={BAY_A: False, BAY_B: False}), now=1.0)

    def test_a_refusal_does_not_create_a_plan(self):
        s, fx = self._refused_load(S.LaneState(op=S.OP_UNLOADED))
        self.assertFalse(find(fx, S.Settle)[0].result.ok)
        self.assertEqual(lane_of(s).stage_plan, ())

    def test_a_refusal_names_the_current_macro_step(self):
        lane = S.LaneState(stage_plan=("cut", "feed"), stage_index=1)
        s, fx = self._refused_load(lane)
        self.assertFalse(find(fx, S.Settle)[0].result.ok)
        nl = lane_of(s)
        self.assertEqual(nl.stage_plan, ("cut", "feed"))
        self.assertEqual(nl.stage_failed, "feed")
        self.assertFalse(nl.stage_auto)


class StageClearingTests(unittest.TestCase):
    """C2 §5.3 rule 4: DONE, a new PLAN, CLEAR_ERRORS and the 10-minute
    staleness drop."""

    def test_clear_errors_clears_the_stage(self):
        lane = S.LaneState(stage_plan=("feed",), stage_index=0,
                           stage_failed="feed", stage_auto=True)
        s, fx = S.reduce(system(lane), S.ClearErrors(), world(), now=2.0)
        nl = lane_of(s)
        self.assertEqual(nl.stage_plan, ())
        self.assertIsNone(nl.stage_index)
        self.assertIsNone(nl.stage_failed)
        self.assertFalse(nl.stage_auto)
        self.assertEqual(fx, [])

    def test_tick_drops_a_stale_plan(self):
        lane = S.LaneState(op=S.OP_UNLOADED, stage_plan=("cut", "feed"),
                           stage_since=0.0)
        s, fx = S.reduce(system(lane), S.Tick(), world(),
                         now=S.STAGE_STALE_S + 1.0)
        self.assertEqual(lane_of(s).stage_plan, ())
        self.assertEqual(fx, [])

    def test_tick_keeps_a_plan_inside_the_stale_window(self):
        lane = S.LaneState(op=S.OP_LOADED, group=GROUP, unit=BAY_A,
                           stage_plan=("cut", "feed"), stage_since=0.0)
        s, _ = S.reduce(system(lane), S.Tick(), world(), now=S.STAGE_STALE_S)
        self.assertEqual(lane_of(s).stage_plan, ("cut", "feed"))

    def test_tick_never_drops_a_failed_plan(self):
        lane = S.LaneState(op=S.OP_UNLOADED, stage_plan=("feed",),
                           stage_index=0, stage_failed="feed", stage_auto=True)
        s, _ = S.reduce(system(lane), S.Tick(), world(),
                        now=S.STAGE_STALE_S + 1.0)
        nl = lane_of(s)
        self.assertEqual(nl.stage_plan, ("feed",))
        self.assertEqual(nl.stage_failed, "feed")

    def test_tick_keeps_a_plan_while_an_op_is_in_flight(self):
        lane = S.LaneState(op=S.OP_LOADING, group=GROUP, unit=BAY_A,
                           stage_plan=("feed",), stage_index=0, stage_auto=True,
                           stage_since=0.0, op_deadline=1e9)
        s, _ = S.reduce(system(lane), S.Tick(), world(),
                        now=S.STAGE_STALE_S + 1.0)
        self.assertEqual(lane_of(s).stage_plan, ("feed",))


class StageRunoutLifetimeTests(unittest.TestCase):
    """C2 §5.3 rule 5: runout_from / reload_target survive the reload and are
    cleared only by a user-initiated op or CLEAR_ERRORS, never by time."""

    def _pausing(self, runout_from=None):
        lane = S.LaneState(op=S.OP_LOADED, group=GROUP, unit=BAY_A,
                           runout_from=runout_from)
        w = world(extruder_pos=10.0, printing=True, loaded={BAY_A: False})
        s, _ = S.reduce(system(lane), S.Tick(), w, now=1.0)
        return lane_of(s)

    def test_pausing_sets_runout_from(self):
        nl = self._pausing()
        self.assertEqual(nl.runout, S.RUNOUT_PAUSING)
        self.assertEqual(nl.runout_from, BAY_A)

    def test_pausing_overwrites_an_earlier_runout_from(self):
        self.assertEqual(self._pausing(runout_from=BAY_B).runout_from, BAY_A)

    def test_a_user_load_clears_both(self):
        lane = S.LaneState(op=S.OP_UNLOADED, runout_from=BAY_A,
                           reload_target=BAY_B)
        s, _ = S.reduce(system(lane), S.Load(FPS, GROUP),
                        world(ready={BAY_A: True}), now=2.0)
        self.assertIsNone(lane_of(s).runout_from)
        self.assertIsNone(lane_of(s).reload_target)

    def test_a_user_unload_clears_both(self):
        lane = S.LaneState(op=S.OP_LOADED, group=GROUP, unit=BAY_A,
                           runout_from=BAY_A, reload_target=BAY_B)
        s, _ = S.reduce(system(lane), S.Unload(FPS), world(), now=2.0)
        self.assertIsNone(lane_of(s).runout_from)
        self.assertIsNone(lane_of(s).reload_target)

    def test_clear_errors_clears_both(self):
        lane = S.LaneState(op=S.OP_LOADED, group=GROUP, unit=BAY_A,
                           runout_from=BAY_A, reload_target=BAY_B)
        s, _ = S.reduce(system(lane), S.ClearErrors(), world(), now=2.0)
        self.assertIsNone(lane_of(s).runout_from)
        self.assertIsNone(lane_of(s).reload_target)

    def test_a_successful_reload_keeps_both(self):
        lane = S.LaneState(op=S.OP_LOADED, group=GROUP, unit=BAY_A,
                           runout=S.RUNOUT_LOADING, reload_target=BAY_B,
                           runout_from=BAY_A)
        s, _ = S.reduce(system(lane),
                        S.OpCompleted(FPS, S.OAMS_OP_CODE_SUCCESS, gen=0),
                        world(), now=3.0)
        nl = lane_of(s)
        self.assertEqual(nl.unit, BAY_B)
        self.assertEqual(nl.runout, S.RUNOUT_IDLE)
        self.assertEqual(nl.runout_from, BAY_A)
        self.assertEqual(nl.reload_target, BAY_B)

    def test_a_failed_reload_keeps_both_into_the_unloaded_lane(self):
        lane = S.LaneState(op=S.OP_LOADED, group=GROUP, unit=BAY_A,
                           runout=S.RUNOUT_LOADING, reload_target=BAY_B,
                           runout_from=BAY_A)
        s, _ = S.reduce(
            system(lane),
            S.OpCompleted(FPS, S.OAMS_OP_CODE_ERROR_UNSPECIFIED, gen=0),
            world(), now=3.0)
        nl = lane_of(s)
        self.assertEqual(nl.op, S.OP_UNLOADED)
        self.assertEqual(nl.runout_from, BAY_A)
        self.assertEqual(nl.reload_target, BAY_B)


class CancelGenerationTests(unittest.TestCase):
    """Every cancel names the action and the generation it cancels, so a
    delayed cancel cannot touch a newer physical operation (protocol v4)."""

    def test_load_cancel_carries_generation(self):
        lane = S.LaneState(op=S.OP_LOADING, unit=BAY_A, group=GROUP,
                           op_gen=0x123456)
        state, fx = S.reduce(system(lane), S.Cancel(FPS), world(), now=1.0)
        self.assertEqual(lane_of(state), lane)
        cancel = find(fx, S.CancelLoad)[0]
        self.assertEqual((cancel.unit, cancel.gen), (BAY_A, 0x123456))

    def test_unload_cancel_is_generation_scoped(self):
        lane = S.LaneState(op=S.OP_UNLOADING, unit=BAY_A, group=GROUP,
                           op_gen=19)
        state, fx = S.reduce(system(lane), S.Cancel(FPS), world(), now=1.0)
        self.assertEqual(lane_of(state), lane)
        cancel = find(fx, S.CancelUnload)[0]
        self.assertEqual((cancel.unit, cancel.gen), (BAY_A, 19))

    def test_load_timeout_cancels_with_generation(self):
        lane = S.LaneState(op=S.OP_LOADING, unit=BAY_A, group=GROUP,
                           op_gen=17)
        _, fx = S.reduce(system(lane), S.Timeout(FPS), world(), now=999.0)
        self.assertEqual(find(fx, S.CancelLoad)[0].gen, 17)

    def test_unload_timeout_cancels_with_generation(self):
        lane = S.LaneState(op=S.OP_UNLOADING, unit=BAY_A, group=GROUP,
                           op_gen=18)
        _, fx = S.reduce(system(lane), S.Timeout(FPS), world(), now=999.0)
        self.assertEqual(find(fx, S.CancelUnload)[0].gen, 18)
        self.assertTrue(has(fx, S.SetFollower))

    def test_unload_error_reply_does_not_cancel(self):
        # An error reply means the firmware op already ended.
        lane = S.LaneState(op=S.OP_UNLOADING, unit=BAY_A, group=GROUP,
                           op_gen=18)
        _, fx = S.reduce(system(lane), S.OpCompleted(
            FPS, S.OAMS_OP_CODE_ERROR_BUSY, gen=18), world(), now=1.0)
        self.assertFalse(has(fx, S.CancelUnload))

    def test_reload_timeout_cancels_with_generation(self):
        lane = S.LaneState(op=S.OP_LOADED, group=GROUP, unit=BAY_A,
                           runout=S.RUNOUT_LOADING, reload_target=BAY_B,
                           op_gen=4)
        _, fx = S.reduce(system(lane), S.Timeout(FPS), world(), now=999.0)
        cancel = find(fx, S.CancelLoad)[0]
        self.assertEqual((cancel.unit, cancel.gen), (BAY_B, 4))


class StoppedByTheUserTests(unittest.TestCase):
    """openams/stop: a cancelled unload is not a fault, and Stopped fails the
    current step and words the lane's message."""

    def test_a_cancelled_unload_leaves_the_spool_loaded_without_a_fault(self):
        lane = S.LaneState(op=S.OP_UNLOADING, unit=BAY_A, group=GROUP,
                           op_gen=18, following=True)
        state, fx = S.reduce(system(lane), S.OpCompleted(
            FPS, S.OAMS_OP_CODE_CANCEL, gen=18), world(), now=1.0)
        nl = lane_of(state)
        self.assertEqual((nl.op, nl.message, nl.fault_text, nl.fault_unit),
                         (S.OP_LOADED, "cancelled", None, None))
        self.assertTrue(has(fx, S.SetFollower))

    def test_stopped_fails_the_current_step_and_sets_the_message(self):
        lane = S.LaneState(op=S.OP_LOADED, group=GROUP, unit=BAY_A,
                           stage_plan=("cut", "retract"), stage_index=0)
        state, _ = S.reduce(system(lane), S.Stopped(FPS), world(), now=1.0)
        nl = lane_of(state)
        self.assertEqual((nl.stage_failed, nl.message),
                         ("cut", "Stopped by the user"))

    def test_stopped_without_a_plan_only_sets_the_message(self):
        lane = S.LaneState(op=S.OP_LOADED, group=GROUP, unit=BAY_A)
        state, _ = S.reduce(system(lane), S.Stopped(FPS), world(), now=1.0)
        nl = lane_of(state)
        self.assertEqual((nl.stage_failed, nl.message),
                         (None, "Stopped by the user"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
