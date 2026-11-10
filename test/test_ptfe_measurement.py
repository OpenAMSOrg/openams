# Units that measure their own PTFE path by odometer (the Cortex Bridge,
# C1 sections 4.1 and 4.4.1): the capability constants, the reason a unit
# gives for a failed operation, the path a load measured, and the calibration
# command with its limit. No Klipper, no hardware.
#
#   python3 -m unittest discover -s test -p 'test_ptfe_measurement.py' -v

import importlib
import os
import sys
import types
import unittest
from collections import deque

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if "oamspkg" not in sys.modules:
    _pkg = types.ModuleType("oamspkg")
    _pkg.__path__ = [_SRC]
    sys.modules["oamspkg"] = _pkg
sys.modules.setdefault("mcu", types.ModuleType("mcu"))

oams = importlib.import_module("oamspkg.oams")
ams_family = importlib.import_module("oamspkg.ams_family")
S = importlib.import_module("oamspkg.oams_state")

# What bridge firmware 8 publishes for a stock AMS 1 (src/vmcu_dict.cpp).
BRIDGE_CONSTANTS = {
    "AMS_FAMILY": "ams1", "AMS_BAYS": 4, "BUILD_VERSION": "cortex-bridge",
    "AMS_CAP_F1S_NORMAL_MOTION": 1, "AMS_CAP_HOST_ARRIVAL_CONFIRM": 1,
    "AMS_HT_CAP_PTFE_CALIBRATION": 0, "AMS_CAP_PTFE_CALIBRATION": 1,
    "AMS_CAP_ARRIVAL_DISTANCE": 1, "AMS_CAP_OP_REASON": 1,
    "AMS_REASON_NO_SPOOL": 1, "AMS_REASON_MOVING_NOT_OBSERVED": 16,
    "AMS_REASON_ARRIVAL_ACK_TIMEOUT": 17, "AMS_REASON_RETRACT_TIMEOUT": 19,
    "AMS_REASON_FEED_OVERRUN": 26, "AMS_REASON_FEED_STALLED": 27,
    "AMS_REASON_ODOMETER_LOST": 28, "AMS_REASON_ERROR_BASE": 512,
    "AMS_PTFE_CALIBRATION_DEFAULT_MAX_CLICKS": 4560,
    "AMS_PTFE_CALIBRATION_MAX_CLICKS": 11400,
}


def bridge_capabilities(**changes):
    constants = dict(BRIDGE_CONSTANTS, **changes)
    caps, _ = ams_family.resolve_capabilities("auto", constants)
    return caps


class FakeCmd:
    def __init__(self):
        self.sent = []

    def send(self, args=None):
        self.sent.append(args)


class FakeGcode:
    def __init__(self):
        self.messages = []

    def respond_info(self, message):
        self.messages.append(message)


def make_unit(capabilities=None, ptfe_length=2850):
    o = oams.OAMS.__new__(oams.OAMS)
    o.name = "oams ams1"
    o.oams_idx = 1
    o.bay_count = 4
    o.capabilities = capabilities if capabilities is not None \
        else bridge_capabilities()
    o.filament_path_length = ptfe_length
    o.ptfe_lengths = [None] * 4
    o.measured_path_mm = {}
    o.action_status = None
    o.status_loading = oams.OAMS_STATUS_LOADING
    o.status_calibrating = oams.OAMS_STATUS_CALIBRATING
    o.ptfe_calibrating = False
    o._ptfe_calibration_max_clicks = None
    gcode = FakeGcode()
    o.printer = types.SimpleNamespace(
        lookup_object=lambda name, default=None: gcode
        if name == "gcode" else default)
    o.gcode = gcode
    return o


class CapabilityTests(unittest.TestCase):
    def test_the_bridge_constants_turn_the_features_on(self):
        caps = bridge_capabilities()
        self.assertTrue(caps["ptfe_calibration"])
        self.assertTrue(caps["arrival_distance"])
        self.assertTrue(caps["op_reason"])
        self.assertEqual(caps["op_reasons"][26], "FEED_OVERRUN")
        self.assertEqual(caps["op_reasons"][512], "ERROR_BASE")
        self.assertEqual(caps["ptfe_calibration_default_max_clicks"], 4560)

    def test_older_bridge_firmware_keeps_calibration_off(self):
        constants = {name: value for name, value in BRIDGE_CONSTANTS.items()
                     if not name.startswith(("AMS_CAP_PTFE", "AMS_CAP_ARRIVAL",
                                             "AMS_CAP_OP", "AMS_REASON",
                                             "AMS_PTFE"))}
        caps, _ = ams_family.resolve_capabilities("auto", constants)
        self.assertFalse(caps["ptfe_calibration"])
        self.assertFalse(caps.get("arrival_distance", False))
        self.assertNotIn("op_reasons", caps)

    def test_a_bridge_unit_with_calibration_is_told_to_calibrate(self):
        unit = make_unit()
        self.assertEqual(unit.ptfe_how, S.PTFE_CALIBRATE)
        unit.capabilities = bridge_capabilities(AMS_CAP_PTFE_CALIBRATION=0)
        self.assertEqual(unit.ptfe_how, S.PTFE_CORTEX_BRIDGE)


class ReasonTests(unittest.TestCase):
    def test_a_load_that_ran_past_its_length_names_the_length(self):
        unit = make_unit()
        text = unit._failure_reason(S.OAMS_OP_CODE_TIMEOUT, 26)
        self.assertIn("fed 2700 mm (ptfe_length 2500 mm + 200 mm)", text)
        self.assertIn("ptfe_length is shorter than the filament path", text)
        self.assertIn("OAMS_CALIBRATE_PTFE_LENGTH OAMS=1", text)

    def test_a_calibration_that_ran_past_its_limit_names_max_length(self):
        unit = make_unit()
        unit._ptfe_calibration_max_clicks = 3420  # 3000 mm
        unit._completed_action = unit.status_calibrating  # as at completion
        text = unit._failure_reason(S.OAMS_OP_CODE_TIMEOUT, 26)
        self.assertIn("fed 3200 mm (MAX_LENGTH 3000 mm + 200 mm)", text)
        self.assertIn("larger MAX_LENGTH", text)

    def test_a_stall_is_a_snag_not_a_length(self):
        text = make_unit()._failure_reason(S.OAMS_OP_CODE_TIMEOUT, 27)
        self.assertIn("stopped moving", text)
        self.assertNotIn("ptfe_length", text)

    def test_an_ams_error_is_named_by_its_code(self):
        text = make_unit()._failure_reason(S.OAMS_OP_CODE_ERROR_UNSPECIFIED,
                                           512 + 0x25)
        self.assertEqual(text, "the AMS reported error 0x25")

    def test_an_ams_jam_is_named_as_a_jam(self):
        # 0x17 is the AMS's own feeder stall (its jam dialog on a Bambu
        # printer); a cardboard spool's friction raised it on 2026-11-10.
        text = make_unit()._failure_reason(S.OAMS_OP_CODE_ERROR_UNSPECIFIED,
                                           512 + 0x17)
        self.assertIn("jam", text)
        self.assertIn("0x17", text)

    def test_a_retract_stall_names_the_cut(self):
        # Bridge 14: the pull back stopped because the odometer stood still.
        unit = make_unit(bridge_capabilities(AMS_REASON_RETRACT_STALLED=34))
        text = unit._failure_reason(S.OAMS_OP_CODE_TIMEOUT, 34)
        self.assertIn("did not move back", text)
        self.assertIn("cut", text)

    def test_older_bridge_firmware_gets_both_causes_of_a_load_timeout(self):
        unit = make_unit(capabilities={"host_arrival_confirm": True},
                         ptfe_length=1482)  # 1300 mm
        unit._completed_action = unit.status_loading
        text = unit._failure_reason(S.OAMS_OP_CODE_TIMEOUT, 0)
        self.assertIn("fed past ptfe_length (1300 mm)", text)
        self.assertIn("a snag in the path", text)
        unit._completed_action = unit.status_calibrating
        self.assertIsNone(unit._failure_reason(S.OAMS_OP_CODE_TIMEOUT, 0))

    def test_no_reason_without_the_capability_or_on_success(self):
        old = make_unit(capabilities={"op_reason": False})
        self.assertIsNone(old._failure_reason(S.OAMS_OP_CODE_TIMEOUT, 26))
        self.assertIsNone(make_unit()._failure_reason(S.OAMS_OP_CODE_SUCCESS, 26))
        self.assertIsNone(make_unit()._failure_reason(S.OAMS_OP_CODE_TIMEOUT, 0))

    def test_the_reducer_shows_the_reason_in_place_of_the_code(self):
        lane = S.LaneState(op=S.OP_LOADING, unit=(1, 2), op_gen=7, group="T2")
        lw = S.LaneWorld(unit_names={1: "ams1"}, host_arrival=frozenset({1}),
                         path_len={1: 2500.0})
        world = S.World(lanes={"fps1": lw})
        state = S.SystemState(lanes={"fps1": lane})
        _, effects = S.reduce(state, S.OpCompleted(
            "fps1", S.OAMS_OP_CODE_TIMEOUT, 26, gen=7,
            reason="the filament fed 2700 mm"), world, now=1.0)
        settle = [e for e in effects if isinstance(e, S.Settle)][0]
        self.assertEqual(settle.result.message,
                         "Spool loading failed (the filament fed 2700 mm)")


    def test_the_reducer_shows_the_reason_of_a_failed_unload(self):
        # 2026-11-10: an unload's reason (bridge 12: RETRACT_TIMEOUT) was
        # dropped for the op code's generic text.
        lane = S.LaneState(op=S.OP_UNLOADING, unit=(1, 2), op_gen=7, group="T2")
        lw = S.LaneWorld(unit_names={1: "ams1"})
        world = S.World(lanes={"fps1": lw})
        state = S.SystemState(lanes={"fps1": lane})
        _, effects = S.reduce(state, S.OpCompleted(
            "fps1", S.OAMS_OP_CODE_TIMEOUT, 34, gen=7,
            reason="the filament did not move back"), world, now=1.0)
        settle = [e for e in effects if isinstance(e, S.Settle)][0]
        self.assertEqual(settle.result.message,
                         "Spool unloading failed (the filament did not move back)")


class CalibrationGuardTests(unittest.TestCase):
    """A PTFE calibration feeds the bay to the toolhead: never with the lane
    loaded or another bay's filament still in a hub."""

    def _try(self, lane, loaded=None):
        lw = S.LaneWorld(unit_names={1: "ams1"}, loaded=loaded or {})
        world = S.World(lanes={"fps1": lw})
        state = S.SystemState(lanes={"fps1": lane})
        return S.reduce(state, S.Calibrate("fps1", 1, 0, "ptfe"), world, now=1.0)

    def test_a_hub_with_filament_refuses_it(self):
        _, effects = self._try(S.LaneState(), loaded={(1, 1): True})
        settle = [e for e in effects if isinstance(e, S.Settle)][0]
        self.assertFalse(settle.result.ok)
        self.assertIn("rewind it first", settle.result.message)

    def test_a_loaded_lane_refuses_it(self):
        lane = S.LaneState(op=S.OP_LOADED, unit=(1, 1), group="T1")
        _, effects = self._try(lane)
        self.assertFalse([e for e in effects if isinstance(e, S.Settle)][0]
                         .result.ok)

    def test_the_bay_itself_partway_out_is_rewound_first(self):
        # A stopped calibration leaves the bay's own filament past its hub.
        # An AMS 1 rewinds such a bay before it feeds, which bridge firmware
        # 8 takes for a stall (hardware, 2026-11-07): rewind it first.
        lw = S.LaneWorld(unit_names={1: "ams1"}, loaded={(1, 0): True})
        state, effects = S.reduce(
            S.SystemState(lanes={"fps1": S.LaneState()}),
            S.Calibrate("fps1", 1, 0, "ptfe"), S.World(lanes={"fps1": lw}),
            now=1.0)
        self.assertEqual(state.lanes["fps1"].op, S.OP_UNLOADED)
        message = [e for e in effects if isinstance(e, S.Settle)][0].result.message
        self.assertIn("rewind it first with OAMSM_UNLOAD_FILAMENT", message)

    def test_a_clear_lane_starts_it(self):
        state, effects = self._try(S.LaneState())
        self.assertEqual(state.lanes["fps1"].op, S.OP_CALIBRATING)
        self.assertTrue([e for e in effects if isinstance(e, S.StartCalibrate)])


class FedBayUnloadTests(unittest.TestCase):
    """A load or calibration that stopped short leaves the bay's filament out
    past its hub with the lane unloaded. On a unit that rewinds such a bay
    itself (a host-arrival unit), OAMSM_UNLOAD_FILAMENT brings it back."""

    def _unload(self, loaded, host_arrival=frozenset({1})):
        lw = S.LaneWorld(unit_names={1: "ams1"}, loaded=loaded,
                         host_arrival=host_arrival)
        return S.reduce(S.SystemState(lanes={"fps1": S.LaneState()}),
                        S.Unload("fps1"), S.World(lanes={"fps1": lw}),
                        now=1.0)

    def test_the_one_bay_out_is_rewound(self):
        state, effects = self._unload({(1, 2): True, (1, 0): False})
        lane = state.lanes["fps1"]
        self.assertEqual((lane.op, lane.unit), (S.OP_UNLOADING, (1, 2)))
        start = [e for e in effects if isinstance(e, S.StartUnload)][0]
        self.assertEqual(start.unit, (1, 2))

    def test_other_units_and_ambiguity_keep_nothing_loaded(self):
        for loaded, arrival in (({(1, 2): True}, frozenset()),
                                ({(1, 2): True, (1, 3): True}, frozenset({1})),
                                ({}, frozenset({1}))):
            state, effects = self._unload(loaded, arrival)
            settle = [e for e in effects if isinstance(e, S.Settle)][0]
            self.assertEqual(settle.result.message, "nothing loaded")
            self.assertEqual(state.lanes["fps1"].op, S.OP_UNLOADED)


class CalibrationRefusalTests(unittest.TestCase):
    def test_a_refusal_before_the_unit_is_shown_not_the_old_status(self):
        unit = make_unit()
        unit.runtime = types.SimpleNamespace(request=lambda fps, action:
            types.SimpleNamespace(wait=lambda: S.OpResult(
                False, None, "busy, cannot calibrate now")))
        unit.fps_name = "fps1"
        # The previous calibration's failure is still in the driver.
        unit.action_status_code = S.OAMS_OP_CODE_TIMEOUT
        unit.action_status_value = 26
        ok, _ = unit._calibrate("ptfe", 1)
        self.assertFalse(ok)
        self.assertEqual(unit._ptfe_calibration_error_message(1),
                         "Cannot calibrate ams1 Spool 2: busy, cannot"
                         " calibrate now.")


class CalibrationBusyRetryTests(unittest.TestCase):
    """A unit that has just rewound answers Busy for a moment: the command
    tries again for a few seconds instead of failing at once."""

    def _unit(self, answers):
        unit = make_unit(capabilities=dict(bridge_capabilities(),
                                           host_arrival_confirm=False))
        unit.bay_count = 4
        unit.ptfe_lengths = [None] * 4
        clock = [0.0]
        unit.reactor = types.SimpleNamespace(
            monotonic=lambda: clock[0],
            pause=lambda until: clock.__setitem__(0, until))
        calls = []

        def calibrate(kind, bay):
            ok, code = answers[min(len(calls), len(answers) - 1)]
            calls.append(clock[0])
            unit._calibration_refusal = None
            unit.action_status_code = code
            unit.action_status_value = 2573 if ok else 0
            return ok, 2573 if ok else None
        unit._calibrate = calibrate
        unit._persist_option = lambda option, value, gcmd: "saved"
        unit.capabilities["ptfe_calibration"] = True
        return unit, calls, clock

    def _gcmd(self):
        class G:
            def __init__(self):
                self.out = []

            def get_int(self, name, default=None, minval=None, maxval=None):
                return 3

            def error(self, message):
                return RuntimeError(message)

            def respond_info(self, message):
                self.out.append(message)
        return G()

    def test_busy_then_accepted(self):
        unit, calls, _ = self._unit([(False, S.OAMS_OP_CODE_ERROR_BUSY),
                                     (False, S.OAMS_OP_CODE_ERROR_BUSY),
                                     (True, S.OAMS_OP_CODE_SUCCESS)])
        gcmd = self._gcmd()
        unit.cmd_OAMS_CALIBRATE_PTFE_LENGTH(gcmd)
        self.assertEqual(len(calls), 3)
        # an AMS 1: one path for the unit
        self.assertEqual(unit.filament_path_length, 2573)
        self.assertEqual(unit.ptfe_lengths, [None] * 4)

    def test_busy_for_too_long_fails_with_the_busy_message(self):
        unit, calls, clock = self._unit([(False, S.OAMS_OP_CODE_ERROR_BUSY)])
        with self.assertRaises(RuntimeError) as caught:
            unit.cmd_OAMS_CALIBRATE_PTFE_LENGTH(self._gcmd())
        self.assertIn("busy", str(caught.exception))
        self.assertLessEqual(clock[0], unit.CALIBRATION_BUSY_RETRY_S + 1.0)
        self.assertGreater(len(calls), 5)


class SharedPathTests(unittest.TestCase):
    """Every bay of an AMS 1 or AMS 2 Pro leaves through one outlet, so one
    calibration is the unit's path: it saves ptfe_length and drops the bays'
    own lengths. A unit of no known family keeps ptfe_length_<bay>."""

    def _unit(self, family, stale):
        caps = dict(bridge_capabilities(), host_arrival_confirm=False)
        if family is None:
            caps.pop("family", None)
        else:
            caps["family"] = family
        unit = make_unit(capabilities=caps)
        unit.ptfe_lengths = list(stale)
        unit.reactor = None
        unit._calibrate = lambda kind, bay: (True, 2600)
        unit.capabilities["ptfe_calibration"] = True
        saved, removed = [], []
        unit._persist_option = lambda option, value, gcmd: saved.append((option, value)) or "saved"
        manager = types.SimpleNamespace(remove_config_option=lambda section, option: removed.append((section, option)))
        gcode = unit.gcode
        unit.printer = types.SimpleNamespace(
            lookup_object=lambda name, default=None: {"gcode": gcode, "oams_manager": manager}.get(name, default))
        return unit, saved, removed

    def _calibrate(self, unit, bay):
        out = []
        gcmd = types.SimpleNamespace(get_int=lambda *args, **kwargs: bay,
                                     respond_info=out.append, error=RuntimeError)
        unit.cmd_OAMS_CALIBRATE_PTFE_LENGTH(gcmd)
        return out

    def test_one_calibration_sets_the_path_of_every_bay_of_an_ams(self):
        for family in ("ams1", "ams2"):
            unit, saved, removed = self._unit(family, [2775, None, 2666, None])
            self._calibrate(unit, 3)
            self.assertEqual(saved, [("ptfe_length", "2600")])
            self.assertEqual(removed, [("oams ams1", "ptfe_length_0"), ("oams ams1", "ptfe_length_2")])
            self.assertEqual([unit._ptfe_length_for_bay(b) for b in range(4)], [2600] * 4)

    def test_the_result_says_every_bay_uses_it(self):
        unit, _, _ = self._unit("ams2", [None] * 4)
        unit.capabilities["host_arrival_confirm"] = True   # measures to the buffer
        unit.fps_name = "fps1"
        manager = unit.printer.lookup_object("oams_manager")
        manager._arrival_block_reason = lambda oam, fps: None
        out = []
        gcmd = types.SimpleNamespace(get_int=lambda *args, **kwargs: 0,
                                     get_float=lambda name, default, **kwargs: default,
                                     respond_info=out.append, error=RuntimeError)
        unit.cmd_OAMS_CALIBRATE_PTFE_LENGTH(gcmd)
        self.assertIn("ptfe_length: 2600 (saved). Every bay of ams1 uses it:"
                      " they share the path.", out[-1])

    def test_a_unit_of_no_known_family_keeps_each_bays_own_length(self):
        unit, saved, removed = self._unit(None, [2775, None, None, None])
        self._calibrate(unit, 2)
        self.assertEqual(saved, [("ptfe_length_2", "2600")])
        self.assertEqual(removed, [])
        self.assertEqual(unit.ptfe_lengths, [2775, None, 2600, None])


class SnapshotTests(unittest.TestCase):
    def test_unload_and_calibrate_get_the_hardware_snapshot(self):
        # The runtime builds the world only for these actions; without it the
        # fed-bay rewind and the hub guard of a calibration see no filament
        # (found on the printer, 2026-11-07).
        self.assertIn(S.Unload, S.WORLD_ACTIONS)
        self.assertIn(S.Calibrate, S.WORLD_ACTIONS)


class MeasuredPathTests(unittest.TestCase):
    def test_the_first_measurement_of_a_bay_is_reported(self):
        unit = make_unit(ptfe_length=2337)  # 2050 mm
        unit._note_arrival_distance(1, 2314)  # 2030 mm
        self.assertAlmostEqual(unit.measured_path_mm[1], 2029.8, places=1)
        self.assertEqual(len(unit.gcode.messages), 1)
        self.assertIn("ams1 Spool 2 reached the toolhead after 2030 mm",
                      unit.gcode.messages[0])
        self.assertNotIn("To use the measured length", unit.gcode.messages[0])
        unit._note_arrival_distance(1, 2320)
        self.assertEqual(len(unit.gcode.messages), 1)  # close: quiet after

    def test_a_length_far_from_the_measurement_gets_the_command(self):
        unit = make_unit(ptfe_length=2850)  # 2500 mm
        unit._note_arrival_distance(1, 2314)
        message = unit.gcode.messages[-1]
        self.assertIn("(ptfe_length: 2500 mm)", message)
        self.assertIn("OAMS_CALIBRATE_PTFE_LENGTH OAMS=1 SPOOL=1", message)
        # every bay of an AMS 1 shares the path: the unit's length
        self.assertIn("set ptfe_length: 2314 under [oams ams1]", message)

    def test_nothing_without_the_capability(self):
        unit = make_unit(capabilities={})
        unit._note_arrival_distance(1, 2314)
        self.assertEqual(unit.measured_path_mm, {})
        self.assertEqual(unit.gcode.messages, [])

    def test_a_bays_own_length_left_from_an_older_calibration_is_named(self):
        # An AMS 1 config upgraded from per-bay calibration: ptfe_length_1
        # overrides ptfe_length for bay 1, so setting ptfe_length alone would
        # change nothing there; the hint names the options to remove.
        unit = make_unit(ptfe_length=2850)
        unit.ptfe_lengths = [None, 2850, None, 2700]
        unit._note_arrival_distance(1, 2314)
        message = unit.gcode.messages[-1]
        self.assertIn("(ptfe_length_1: 2500 mm)", message)
        self.assertIn("OAMS_CALIBRATE_PTFE_LENGTH OAMS=1 SPOOL=1", message)
        self.assertIn("set ptfe_length: 2314 under [oams ams1] and remove"
                      " ptfe_length_1 and ptfe_length_3", message)

    def test_a_unit_of_no_known_family_is_told_to_set_the_bays_own_length(self):
        caps = bridge_capabilities()
        caps.pop("family", None)
        unit = make_unit(capabilities=caps, ptfe_length=2850)
        unit.ptfe_lengths = [None, 2850, None, None]
        unit._note_arrival_distance(1, 2314)
        message = unit.gcode.messages[-1]
        self.assertIn("set ptfe_length_1: 2314 under [oams ams1].", message)
        self.assertNotIn("remove", message)


class UnitCalibrationStateTests(unittest.TestCase):
    """The unit's calibration.state (C2 5.4): configured with a shared
    ptfe_length, or when every bay has its own length (a config left from
    per-bay calibration, which is never migrated silently)."""

    def test_a_unit_whose_bays_all_have_their_own_length_is_calibrated(self):
        unit = make_unit(ptfe_length=0)
        unit.ptfe_lengths = [2382, 2370, 2369, 2358]
        status = unit._calibration_status()
        self.assertEqual(status["state"], "configured")
        self.assertAlmostEqual(status["path_length_mm"],
                               2382 / oams.FILAMENT_PATH_LENGTH_FACTOR)

    def test_one_bay_without_a_length_leaves_the_unit_uncalibrated(self):
        unit = make_unit(ptfe_length=0)
        unit.ptfe_lengths = [2382, 2370, None, 2358]
        self.assertEqual(unit._calibration_status(),
                         {"state": "uncalibrated", "path_length_mm": 0.0})

    def test_the_shared_length_still_decides_when_it_is_set(self):
        unit = make_unit(ptfe_length=2850)
        unit.ptfe_lengths = [2382, None, None, None]
        status = unit._calibration_status()
        self.assertEqual(status["state"], "configured")
        self.assertAlmostEqual(status["path_length_mm"],
                               2850 / oams.FILAMENT_PATH_LENGTH_FACTOR)


class CalibrationSenderTests(unittest.TestCase):
    def _unit(self):
        o = make_unit()
        o._use_gen_protocol = True
        o._use_wide_gen_protocol = False
        o._gen_queue = deque()
        o._require_motion = lambda: None
        o._note_active = lambda action, gen: None
        o.oams_calibrate_ptfe_length2_cmd = FakeCmd()
        o.oams_calibrate_ptfe_length3_cmd = FakeCmd()
        o.oams_calibrate_hub_hes2_cmd = FakeCmd()
        return o

    def test_a_limited_calibration_uses_the_command_with_the_limit(self):
        o = self._unit()
        o._ptfe_calibration_max_clicks = 4560
        o.start_calibrate("ptfe", 1, gen=12)
        self.assertEqual(o.oams_calibrate_ptfe_length3_cmd.sent, [[1, 12, 4560]])
        self.assertEqual(o.oams_calibrate_ptfe_length2_cmd.sent, [])
        self.assertTrue(o.ptfe_calibrating)

    def test_without_a_limit_or_for_the_hub_the_plain_command(self):
        o = self._unit()
        o.start_calibrate("ptfe", 1, gen=13)
        o.start_calibrate("hub_hes", 2, gen=14)
        self.assertEqual(o.oams_calibrate_ptfe_length2_cmd.sent, [[1, 13]])
        self.assertEqual(o.oams_calibrate_hub_hes2_cmd.sent, [[2, 14]])
        self.assertFalse(o.ptfe_calibrating)

    def test_arrival_is_confirmed_during_a_ptfe_calibration(self):
        o = self._unit()
        o.oams_load_arrived_cmd = FakeCmd()
        o._arrival_sent_gen = None
        o._ptfe_calibration_max_clicks = 4560
        o.start_calibrate("ptfe", 1, gen=15)
        o.action_status = o.status_calibrating
        self.assertTrue(o.confirm_load_arrived(15))
        self.assertEqual(o.oams_load_arrived_cmd.sent, [[15]])
        o.ptfe_calibrating = False  # a hub sensor calibration: never
        o._arrival_sent_gen = None
        self.assertFalse(o.confirm_load_arrived(16))


if __name__ == "__main__":
    unittest.main()
