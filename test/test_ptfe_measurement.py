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
        text = unit._failure_reason(S.OAMS_OP_CODE_TIMEOUT, 26,
                                    calibrating=True)
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
        self.assertEqual(unit.ptfe_lengths[3], 2573)

    def test_busy_for_too_long_fails_with_the_busy_message(self):
        unit, calls, clock = self._unit([(False, S.OAMS_OP_CODE_ERROR_BUSY)])
        with self.assertRaises(RuntimeError) as caught:
            unit.cmd_OAMS_CALIBRATE_PTFE_LENGTH(self._gcmd())
        self.assertIn("busy", str(caught.exception))
        self.assertLessEqual(clock[0], unit.CALIBRATION_BUSY_RETRY_S + 1.0)
        self.assertGreater(len(calls), 5)


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
        self.assertIn("ptfe_length_1: 2314 under [oams ams1]", message)

    def test_nothing_without_the_capability(self):
        unit = make_unit(capabilities={})
        unit._note_arrival_distance(1, 2314)
        self.assertEqual(unit.measured_path_mm, {})
        self.assertEqual(unit.gcode.messages, [])


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
