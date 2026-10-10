# Unit tests for the OAMS driver's protocol handling (oams.py) without Klipper.
#
# oams.py does `import mcu` and uses package-relative imports, so we stub the
# `mcu` module and expose src/ as a synthetic package, then drive individual
# methods on a bare instance (no real config / MCU / reactor needed).
#
#   cd openams && python3 -m unittest discover -s test

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
# Stub the Klipper `mcu` module that oams.py imports at top level.
sys.modules.setdefault("mcu", types.ModuleType("mcu"))

oams = importlib.import_module("oamspkg.oams")
OAMS = oams.OAMS
S = importlib.import_module("oamspkg.oams_state")


class FakeCmd:
    def __init__(self):
        self.sent = []

    def send(self, args=None):
        self.sent.append(args)


class FakeGcmd:
    def __init__(self, values=None):
        self.values = values or {}
        self.responses = []

    def get_float(self, name, default=None, **kwargs):
        return float(self.values.get(name, default))

    def error(self, message):
        return RuntimeError(message)

    def respond_info(self, message):
        self.responses.append(message)


def make_oams(use_gen_protocol):
    """A bare OAMS with just the fields the methods under test touch."""
    o = OAMS.__new__(OAMS)
    o.oams_idx = 1
    o._use_gen_protocol = use_gen_protocol
    o._gen_queue = deque()
    o._pending_bay = None
    o.current_spool = None
    o.action_status = None
    o.action_status_code = None
    o.action_status_value = None
    o.protocol_version = 2 if use_gen_protocol else None
    # action enum (as resolved by _resolve_protocol)
    o.status_loading = oams.OAMS_STATUS_LOADING
    o.status_unloading = oams.OAMS_STATUS_UNLOADING
    o.status_calibrating = oams.OAMS_STATUS_CALIBRATING
    o.status_error = oams.OAMS_STATUS_ERROR
    # command handles
    o.oams_load_spool_cmd = FakeCmd()
    o.oams_unload_spool_cmd = FakeCmd()
    o.oams_calibrate_ptfe_length_cmd = FakeCmd()
    o.oams_calibrate_hub_hes_cmd = FakeCmd()
    o.oams_load_spool2_cmd = FakeCmd()
    o.oams_unload_spool2_cmd = FakeCmd()
    o.oams_calibrate_ptfe_length2_cmd = FakeCmd()
    o.oams_calibrate_hub_hes2_cmd = FakeCmd()
    # completion sink
    o.completions = []
    o.on_action_complete = lambda code, value, gen: o.completions.append(
        (code, value, gen))
    return o


class WireGenTests(unittest.TestCase):
    def test_none_is_zero(self):
        self.assertEqual(OAMS._wire_gen(None), 0)

    def test_masks_to_byte(self):
        self.assertEqual(OAMS._wire_gen(0), 0)
        self.assertEqual(OAMS._wire_gen(255), 255)
        self.assertEqual(OAMS._wire_gen(300), 300 & 0xFF)


class SenderRoutingTests(unittest.TestCase):

    def test_unload_and_positioning_stage_names(self):
        o = make_oams(False)
        o.capabilities = {}
        o.reactor = types.SimpleNamespace(monotonic=lambda: 0.)
        published = []
        o._publish_telemetry = lambda name, data: published.append(data)
        for stage, name in ((5, "unload_to_hub_clear"),
                            (6, "position_feed_to_hub")):
            o._domain_status_received(dict(motion_state=3, motion_stage=stage,
                dryer_state=0, motion_fault=0, dryer_fault=0, system_fault=0))
            self.assertEqual(published[-1]["motion_stage_name"], name)

    def test_per_bay_lengths_config_fallback_and_load_order(self):
        class Config:
            def getfloat(self, name, default=None, **kwargs):
                return {"ptfe_length_0": 2382, "ptfe_length_1": 2370,
                        "ptfe_length_2": 2369, "ptfe_length_3": 2358}.get(name, default)

        for generation in (False, True):
            o = make_oams(generation)
            o.bay_count = 4
            o.filament_path_length = 2145
            o._configure_ptfe_lengths(Config())
            sent = []
            o.oams_ptfe_length_cmd = types.SimpleNamespace(
                send=lambda args: sent.append(("length", args)))
            load = types.SimpleNamespace(send=lambda args: sent.append(("load", args)))
            o.oams_load_spool_cmd = o.oams_load_spool2_cmd = load
            for bay, length in enumerate((2382, 2370, 2369, 2358)):
                sent.clear()
                o.start_load_spool(bay, gen=7)
                self.assertEqual(sent, [("length", [length]),
                    ("load", [bay, 7] if generation else [bay])])
                self.assertEqual(o._selected_ptfe_bay, bay)
                self.assertAlmostEqual(o.path_length_mm,
                                       length / oams.FILAMENT_PATH_LENGTH_FACTOR)
            o.ptfe_lengths[2] = None
            o.start_load_spool(2)
            self.assertEqual(sent[-2], ("length", [2145]))

    def test_absent_bay_lengths_keep_shared_configuration(self):
        o = make_oams(False)
        o.bay_count = 4
        o.filament_path_length = 2145
        o._configure_ptfe_lengths(types.SimpleNamespace(
            getfloat=lambda name, default=None, **kwargs: default))
        o.start_load_spool(3)
        self.assertEqual(o.ptfe_lengths, [None] * 4)
        self.assertAlmostEqual(o.path_length_mm, 2145 / oams.FILAMENT_PATH_LENGTH_FACTOR)

    def test_length_or_load_send_failure_preserves_selected_path(self):
        def fail(args):
            raise RuntimeError("send failed")
        for failing in ("length", "load"):
            o = make_oams(False)
            o.filament_path_length = 2145
            o.ptfe_lengths = [2382, 2370, 2369, 2358]
            o._selected_path_length = 2382
            o._selected_ptfe_bay = 0
            o.oams_ptfe_length_cmd = FakeCmd()
            setattr(o, "oams_ptfe_length_cmd" if failing == "length" else
                    "oams_load_spool_cmd", types.SimpleNamespace(send=fail))
            with self.assertRaisesRegex(RuntimeError, "send failed"):
                o.start_load_spool(2)
            self.assertEqual(o._selected_path_length, 2382)
            self.assertEqual(o._selected_ptfe_bay, 0)
            self.assertIsNone(o._pending_bay)
            self.assertEqual(list(o._gen_queue), [])

    def test_calibration_persists_selected_bay_and_single_bay_shared(self):
        for bays in (1, 4):
            for bay in range(bays):
                o = make_oams(False)
                o.bay_count = bays
                o.filament_path_length = 2145
                o.ptfe_lengths = [None] * bays
                o.capabilities = {"ptfe_calibration": True}
                o._calibrate = lambda kind, bay: (True, 2300 + bay)
                saved = []
                o._persist_option = lambda option, value, gcmd: saved.append((option, value))
                gcmd = types.SimpleNamespace(get_int=lambda *args, **kwargs: bay,
                    respond_info=lambda message: None)
                o.cmd_OAMS_CALIBRATE_PTFE_LENGTH(gcmd)
                self.assertEqual(saved, [("ptfe_length" if bays == 1 else
                    "ptfe_length_%d" % bay, str(2300 + bay))])
                self.assertEqual(o._ptfe_length_for_bay(bay), 2300 + bay)

    def test_disabled_motion_rejected_before_send_but_stop_remains_available(self):
        o = make_oams(True)
        o.capabilities = {"normal_motion": False}
        o.printer = types.SimpleNamespace(command_error=RuntimeError)
        o.oams_follower_cmd = FakeCmd()
        for call in (lambda: o.start_load_spool(0, gen=4),
                     lambda: o.start_unload_spool(gen=4),
                     lambda: o.start_calibrate("ptfe", 0, gen=4),
                     lambda: o.set_oams_follower(1, 1)):
            with self.assertRaisesRegex(RuntimeError, "normal filament motion"):
                call()
        self.assertEqual(o.oams_load_spool2_cmd.sent, [])
        self.assertEqual(o.oams_unload_spool2_cmd.sent, [])
        self.assertEqual(o.oams_calibrate_ptfe_length2_cmd.sent, [])
        self.assertEqual(o.oams_follower_cmd.sent, [])
        self.assertIsNone(o.action_status)
        o.set_oams_follower(0, 1)
        self.assertEqual(o.oams_follower_cmd.sent, [[0, 1]])

    def test_gen_protocol_sends_gen_on_wire_no_fifo(self):
        o = make_oams(use_gen_protocol=True)
        o.start_load_spool(2, gen=7)
        self.assertEqual(o.oams_load_spool2_cmd.sent, [[2, 7]])
        self.assertEqual(o.oams_load_spool_cmd.sent, [])      # legacy untouched
        self.assertEqual(list(o._gen_queue), [])              # no FIFO use
        self.assertEqual(o.action_status, oams.OAMS_STATUS_LOADING)

    def test_legacy_protocol_uses_fifo_no_gen_on_wire(self):
        o = make_oams(use_gen_protocol=False)
        o.start_load_spool(2, gen=7)
        self.assertEqual(o.oams_load_spool_cmd.sent, [[2]])
        self.assertEqual(o.oams_load_spool2_cmd.sent, [])
        self.assertEqual(list(o._gen_queue), [7])

    def test_unload_and_calibrate_routing(self):
        o = make_oams(use_gen_protocol=True)
        o.start_unload_spool(gen=3)
        o.start_calibrate("ptfe", 1, gen=4)
        o.start_calibrate("hub_hes", 0, gen=5)
        self.assertEqual(o.oams_unload_spool2_cmd.sent, [[3]])
        self.assertEqual(o.oams_calibrate_ptfe_length2_cmd.sent, [[1, 4]])
        self.assertEqual(o.oams_calibrate_hub_hes2_cmd.sent, [[0, 5]])

    def test_standalone_gen_none_sends_zero(self):
        o = make_oams(use_gen_protocol=True)
        o.start_load_spool(0)            # gen defaults to None (standalone path)
        self.assertEqual(o.oams_load_spool2_cmd.sent, [[0, 0]])

    def test_send_failure_leaves_no_phantom_fifo_entry(self):
        # On legacy firmware a FIFO entry for an op whose send() raised would
        # desync completion matching for every subsequent op.
        class BoomCmd:
            def send(self, args=None):
                raise RuntimeError("mcu shutdown")

        o = make_oams(use_gen_protocol=False)
        o.oams_load_spool_cmd = BoomCmd()
        with self.assertRaises(RuntimeError):
            o.start_load_spool(2, gen=7)
        self.assertEqual(list(o._gen_queue), [])
        self.assertIsNone(o.action_status)     # sentinel not left armed either


class GenSourcingTests(unittest.TestCase):
    def test_status2_uses_wire_gen(self):
        o = make_oams(use_gen_protocol=True)
        o.action_status = oams.OAMS_STATUS_LOADING
        o._apply_action_status(
            {"action": oams.OAMS_STATUS_LOADING,
             "code": S.OAMS_OP_CODE_SUCCESS, "value": 0}, wire_gen=9)
        self.assertEqual(o.completions, [(S.OAMS_OP_CODE_SUCCESS, None, 9)])
        self.assertIsNone(o.action_status)

    def test_legacy_pops_fifo(self):
        o = make_oams(use_gen_protocol=False)
        o._gen_queue.append(11)
        o.action_status = oams.OAMS_STATUS_UNLOADING
        o._apply_action_status(
            {"action": oams.OAMS_STATUS_UNLOADING,
             "code": S.OAMS_OP_CODE_SUCCESS, "value": 0})
        self.assertEqual(o.completions, [(S.OAMS_OP_CODE_SUCCESS, None, 11)])
        self.assertEqual(list(o._gen_queue), [])

    def test_unsolicited_completion_class_has_no_gen(self):
        # Empty FIFO + no wire gen -> gen=None, which the reducer rejects.
        o = make_oams(use_gen_protocol=False)
        o._apply_action_status(
            {"action": oams.OAMS_STATUS_ERROR,
             "code": S.OAMS_OP_CODE_ERROR_UNSPECIFIED, "value": 0})
        self.assertEqual(o.completions, [(S.OAMS_OP_CODE_ERROR_UNSPECIFIED,
                                          None, None)])

    def test_follower_status_is_dropped(self):
        # Non-completion action (forward following) must not notify the store.
        o = make_oams(use_gen_protocol=True)
        o._gen_queue.append(1)
        o._apply_action_status(
            {"action": oams.OAMS_STATUS_FORWARD_FOLLOWING,
             "code": S.OAMS_OP_CODE_ERROR_BUSY, "value": 0}, wire_gen=1)
        self.assertEqual(o.completions, [])

    def test_load_success_updates_spool_mirror(self):
        o = make_oams(use_gen_protocol=True)
        o._pending_bay = 3
        o._apply_action_status(
            {"action": oams.OAMS_STATUS_LOADING,
             "code": S.OAMS_OP_CODE_SUCCESS, "value": 0}, wire_gen=2)
        self.assertEqual(o.current_spool, 3)


class UnloadClearsHubTests(unittest.TestCase):
    """An unload ends when the hub HES clears; the next stats message carrying
    that reading can arrive up to 450 ms later. A load right after the unload
    must not be refused on the stale hub bit."""

    def unload_success(self, o, **kw):
        o.action_status = oams.OAMS_STATUS_UNLOADING
        o._apply_action_status(
            {"action": oams.OAMS_STATUS_UNLOADING,
             "code": S.OAMS_OP_CODE_SUCCESS, "value": 0}, **kw)

    def test_legacy_unload_clears_the_unloaded_bays_hub_bit(self):
        o = make_oams(use_gen_protocol=False)
        o.bay_count = 4
        o.hub_hes_value = [0, 0, 1, 0]
        o.current_spool = 2
        o._gen_queue.append(5)
        self.unload_success(o)
        self.assertEqual(o.hub_hes_value, [0, 0, 0, 0])
        self.assertIsNone(o.current_spool)

    def test_unknown_bay_clears_the_only_occupied_hub(self):
        # After a restart the driver mirror is empty; the firmware unloads the
        # one bay whose hub reads filament.
        o = make_oams(use_gen_protocol=True)
        o.bay_count = 4
        o.hub_hes_value = [0, 0, 0, 1]
        self.unload_success(o, wire_gen=3)
        self.assertEqual(o.hub_hes_value, [0, 0, 0, 0])

    def test_other_bays_keep_their_reading(self):
        # Filament at another bay's hub is real and must still block a load.
        o = make_oams(use_gen_protocol=False)
        o.bay_count = 4
        o.hub_hes_value = [0, 1, 1, 0]
        o.current_spool = 2
        o._gen_queue.append(5)
        self.unload_success(o)
        self.assertEqual(o.hub_hes_value, [0, 1, 0, 0])

    def test_ambiguous_unknown_bay_changes_nothing(self):
        o = make_oams(use_gen_protocol=False)
        o.bay_count = 4
        o.hub_hes_value = [1, 0, 1, 0]
        o._gen_queue.append(5)
        self.unload_success(o)
        self.assertEqual(o.hub_hes_value, [1, 0, 1, 0])

    def test_failed_unload_changes_nothing(self):
        o = make_oams(use_gen_protocol=False)
        o.bay_count = 4
        o.hub_hes_value = [0, 0, 1, 0]
        o.current_spool = 2
        o._gen_queue.append(5)
        o.action_status = oams.OAMS_STATUS_UNLOADING
        o._apply_action_status(
            {"action": oams.OAMS_STATUS_UNLOADING,
             "code": S.OAMS_OP_CODE_ERROR_UNSPECIFIED, "value": 0})
        self.assertEqual(o.hub_hes_value, [0, 0, 1, 0])
        self.assertEqual(o.current_spool, 2)


class PidCommandTests(unittest.TestCase):
    def test_rewind_current_pid_uses_dedicated_firmware_command(self):
        o = OAMS.__new__(OAMS)
        o.oams_idx = 1
        o.oams_pid_cmd = FakeCmd()
        o.oams_current_pid_cmd = FakeCmd()
        o.current_target = 64.0
        gcmd = FakeGcmd({"P": 1.25, "I": 0.5, "D": 0.125, "TARGET": 2.0})

        o.cmd_OAMS_CURRENT_PID_SET(gcmd)

        self.assertEqual(o.oams_pid_cmd.sent, [])
        self.assertEqual(o.oams_current_pid_cmd.sent, [[
            o.float_to_u32(1.25), o.float_to_u32(0.5),
            o.float_to_u32(0.125), o.float_to_u32(2.0)]])
        self.assertEqual(
            (o.current_kp, o.current_ki, o.current_kd, o.current_target),
            (1.25, 0.5, 0.125, 2.0))
        self.assertIn("Current PID values set", gcmd.responses[0])

    def test_pid_autotune_fails_closed_without_heating_or_extruding(self):
        o = OAMS.__new__(OAMS)
        gcmd = FakeGcmd({"TARGET_FLOW": 20, "TARGET_TEMP": 250})

        with self.assertRaisesRegex(RuntimeError, "autotune is unsupported"):
            o.cmd_OAMS_PID_AUTOTUNE(gcmd)


class ResolveProtocolTests(unittest.TestCase):
    def _oams_with_consts(self, consts):
        o = OAMS.__new__(OAMS)
        o.oams_idx = 1
        o.status_loading = oams.OAMS_STATUS_LOADING
        o.status_unloading = oams.OAMS_STATUS_UNLOADING
        o.status_calibrating = oams.OAMS_STATUS_CALIBRATING
        o.status_error = oams.OAMS_STATUS_ERROR
        o.protocol_version = None
        o.mcu = types.SimpleNamespace(get_constants=lambda: consts)
        return o

    def test_legacy_no_constants_keeps_defaults(self):
        o = self._oams_with_consts({})
        o._resolve_protocol()
        self.assertIsNone(o.protocol_version)
        self.assertEqual(o.status_loading, oams.OAMS_STATUS_LOADING)

    def test_published_action_enum_is_adopted(self):
        # A firmware that renumbers the action enum is followed (driver-local).
        o = self._oams_with_consts({
            "OAMS_PROTOCOL_VERSION": 2,
            "OAMS_STATUS_LOADING": 10, "OAMS_STATUS_UNLOADING": 11,
            "OAMS_STATUS_CALIBRATING": 16, "OAMS_STATUS_ERROR": 17,
        })
        o._resolve_protocol()
        self.assertEqual(o.protocol_version, 2)
        self.assertEqual(o.status_loading, 10)
        self.assertEqual(o.status_error, 17)

    def test_string_constants_are_coerced(self):
        # Dictionary values may arrive as strings; they must coerce, not
        # TypeError inside handle_connect (which would skip all command
        # lookups and kill the unit).
        o = self._oams_with_consts({
            "OAMS_PROTOCOL_VERSION": "3",
            "OAMS_STATUS_LOADING": "0",
        })
        o._resolve_protocol()                # must not raise
        self.assertEqual(o.protocol_version, 3)
        self.assertEqual(o.status_loading, 0)

    def test_garbage_constants_fall_back_to_defaults(self):
        o = self._oams_with_consts({
            "OAMS_PROTOCOL_VERSION": "not-a-number",
            "OAMS_STATUS_LOADING": "junk",
        })
        o._resolve_protocol()                # must not raise
        self.assertIsNone(o.protocol_version)          # -> legacy mode
        self.assertEqual(o.status_loading, oams.OAMS_STATUS_LOADING)

    def test_get_constants_failure_is_tolerated(self):
        o = OAMS.__new__(OAMS)
        o.oams_idx = 1
        o.status_loading = oams.OAMS_STATUS_LOADING
        o.status_unloading = oams.OAMS_STATUS_UNLOADING
        o.status_calibrating = oams.OAMS_STATUS_CALIBRATING
        o.status_error = oams.OAMS_STATUS_ERROR
        o.protocol_version = None

        def boom():
            raise RuntimeError("no constants yet")
        o.mcu = types.SimpleNamespace(get_constants=boom)
        o._resolve_protocol()                # must not raise
        self.assertIsNone(o.protocol_version)


class LivenessPropertyTests(unittest.TestCase):
    def test_v3_owns_liveness(self):
        o = make_oams(use_gen_protocol=True)
        o.protocol_version = 3
        self.assertTrue(o.firmware_owns_liveness)

    def test_v2_does_not_own_liveness(self):
        o = make_oams(use_gen_protocol=True)
        o.protocol_version = 2
        self.assertFalse(o.firmware_owns_liveness)

    def test_legacy_does_not_own_liveness(self):
        o = make_oams(use_gen_protocol=False)
        o.protocol_version = None
        self.assertFalse(o.firmware_owns_liveness)


class BayLoadableTests(unittest.TestCase):
    """A stock OAMS mainboard always has its own f1s (BAY) sensor per bay:
    bay_loadable() must answer exactly like the raw f1s_hes_value reading."""

    def test_matches_raw_f1s_reading(self):
        o = make_oams(use_gen_protocol=False)
        o.f1s_hes_value = [1, 0, 1, 0]
        self.assertTrue(o.bay_loadable(0))
        self.assertFalse(o.bay_loadable(1))
        self.assertTrue(o.bay_loadable(2))
        self.assertFalse(o.bay_loadable(3))


# ---------------------------------------------------------------- protocol v4

V2_COMMANDS = {
    "oams_cmd_load_spool2 spool=%c gen=%c",
    "oams_cmd_unload_spool2 gen=%c",
    "oams_cmd_calibrate_ptfe_length2 spool=%c gen=%c",
    "oams_cmd_calibrate_hub_hes2 spool=%c gen=%c",
}
V4_COMMANDS = {
    "oams_cmd_load_spool4 spool=%c gen=%u",
    "oams_cmd_unload_spool4 gen=%u",
    "oams_cmd_calibrate_ptfe_length4 spool=%c gen=%u",
    "oams_cmd_calibrate_hub_hes4 spool=%c gen=%u",
    "oams_cmd_cancel4 action=%c gen=%u",
}
STATUS2 = "oams_action_status2 action=%c code=%c value=%u gen=%c"
STATUS4 = "oams_action_status4 action=%c code=%c value=%u gen=%u"


class FakeMCU:
    def __init__(self, commands):
        self.commands = set(commands)
        self.responses = []

    def lookup_command(self, declaration):
        if declaration not in self.commands:
            raise KeyError(declaration)
        return FakeCmd()

    def register_serial_response(self, callback, declaration):
        self.responses.append(declaration)


def make_wide_oams():
    o = make_oams(use_gen_protocol=True)
    o.protocol_version = 4
    o._use_wide_gen_protocol = True
    o.oams_load_spool4_cmd = FakeCmd()
    o.oams_unload_spool4_cmd = FakeCmd()
    o.oams_calibrate_ptfe_length4_cmd = FakeCmd()
    o.oams_calibrate_hub_hes4_cmd = FakeCmd()
    o.oams_cancel4_cmd = FakeCmd()
    o.oams_load_spool_cancel_cmd = FakeCmd()
    return o


class WideWireGenTests(unittest.TestCase):
    def test_masks_to_uint32(self):
        self.assertEqual(OAMS._wire_gen32(None), 0)
        self.assertEqual(OAMS._wire_gen32(0x12345678), 0x12345678)
        self.assertEqual(OAMS._wire_gen32(0x100000001), 1)


class V4SenderTests(unittest.TestCase):
    def test_every_operation_is_sent_without_truncation(self):
        o = make_wide_oams()
        generation = 0x12345678
        o.start_load_spool(2, gen=generation)
        o.start_unload_spool(gen=generation + 1)
        o.start_calibrate("ptfe", 1, gen=generation + 2)
        o.start_calibrate("hub_hes", 0, gen=generation + 3)
        self.assertEqual(o.oams_load_spool4_cmd.sent, [[2, generation]])
        self.assertEqual(o.oams_unload_spool4_cmd.sent, [[generation + 1]])
        self.assertEqual(o.oams_calibrate_ptfe_length4_cmd.sent,
                         [[1, generation + 2]])
        self.assertEqual(o.oams_calibrate_hub_hes4_cmd.sent,
                         [[0, generation + 3]])
        self.assertEqual(o.oams_load_spool2_cmd.sent, [])     # never mixed
        self.assertEqual(o.oams_unload_spool_cmd.sent, [])
        self.assertEqual(list(o._gen_queue), [])

    def test_load_writes_the_length_then_load4_never_the_atomic_load(self):
        o = make_wide_oams()
        o.bay_count = 4
        o.filament_path_length = 2145
        o.ptfe_lengths = [None] * 4
        sent = []
        o.oams_ptfe_length_cmd = types.SimpleNamespace(
            send=lambda args: sent.append(("length", args)))
        o.oams_load_spool3_cmd = types.SimpleNamespace(
            send=lambda args: sent.append(("load3", args)))
        o.oams_load_spool4_cmd = types.SimpleNamespace(
            send=lambda args: sent.append(("load4", args)))
        o.start_load_spool(1, gen=0x1FFFF)
        self.assertEqual(sent, [("length", [2145]), ("load4", [1, 0x1FFFF])])

    def test_cancel_names_action_and_full_generation(self):
        o = make_wide_oams()
        o.cancel_operation(o.status_unloading, 0x89abcdef)
        self.assertEqual(o.oams_cancel4_cmd.sent,
                         [[o.status_unloading, 0x89abcdef]])
        self.assertEqual(o.oams_load_spool_cancel_cmd.sent, [])

    def test_legacy_load_cancel_entry_point_uses_cancel4(self):
        o = make_wide_oams()
        o.load_spool_cancel()
        self.assertEqual(o.oams_cancel4_cmd.sent, [[o.status_loading, 0]])
        self.assertEqual(o.oams_load_spool_cancel_cmd.sent, [])

    def test_v2_cancel_falls_back_only_for_load(self):
        o = make_oams(use_gen_protocol=True)
        o.oams_load_spool_cancel_cmd = FakeCmd()
        o.cancel_operation(o.status_loading, 7)
        self.assertEqual(o.oams_load_spool_cancel_cmd.sent, [None])
        message = o.cancel_operation(o.status_unloading, 8)
        self.assertIn("requires protocol v4", message)
        self.assertEqual(o.oams_load_spool_cancel_cmd.sent, [None])

    def test_v4_advertises_load_unload_and_calibration(self):
        o = make_wide_oams()
        o.capabilities = {"ptfe_calibration": True,
                          "hub_hes_calibration": True}
        o.device_family = "ams1"
        o.oams_follower_cmd = None
        for name in ("ams_ht_dryer_start_cmd", "ams_ht_dryer_stop_cmd",
                     "ams_ht_clear_fault_cmd"):
            setattr(o, name, None)
        o.oams_load_spool2_cmd = o.oams_unload_spool2_cmd = None
        o.oams_calibrate_ptfe_length2_cmd = None
        o.oams_calibrate_hub_hes2_cmd = None
        actions = o._supported_actions()
        for action in ("load", "unload", "calibrate_ptfe",
                       "calibrate_hub_hes"):
            self.assertIn(action, actions)

    def test_summary_names_the_wide_family(self):
        o = make_wide_oams()
        self.assertIn("32-bit", o.protocol_summary())
        self.assertNotIn("32-bit", make_oams(True).protocol_summary())


class V4StatusTests(unittest.TestCase):
    def _loading(self, generation):
        o = make_wide_oams()
        o._pending_bay = 3
        o.action_status = oams.OAMS_STATUS_LOADING
        o._active_action = o.status_loading
        o._active_gen = generation
        return o

    def test_status4_uses_the_authoritative_full_generation(self):
        generation = 0xf1020304
        o = self._loading(generation)
        o._apply_action_status(
            {"action": oams.OAMS_STATUS_LOADING,
             "code": S.OAMS_OP_CODE_SUCCESS, "value": 0},
            wire_gen=generation)
        self.assertEqual(o.completions,
                         [(S.OAMS_OP_CODE_SUCCESS, None, generation)])
        self.assertEqual(o.current_spool, 3)
        self.assertIsNone(o.action_status)
        self.assertIsNone(o._active_gen)

    def test_stale_status4_cannot_clear_the_waiter_or_update_the_spool(self):
        o = self._loading(0x200)
        o._apply_action_status(
            {"action": o.status_loading,
             "code": S.OAMS_OP_CODE_SUCCESS, "value": 0}, wire_gen=0x100)
        self.assertIsNone(o.current_spool)
        self.assertEqual(o.action_status, oams.OAMS_STATUS_LOADING)
        self.assertEqual(o._active_gen, 0x200)
        # Still delivered to the reducer, which rejects it by generation.
        self.assertEqual(o.completions,
                         [(S.OAMS_OP_CODE_SUCCESS, None, 0x100)])

    def test_low_byte_alias_of_the_active_generation_is_not_a_match(self):
        o = self._loading(0x1234)
        o._apply_action_status(
            {"action": o.status_loading,
             "code": S.OAMS_OP_CODE_SUCCESS, "value": 0}, wire_gen=0x34)
        self.assertIsNone(o.current_spool)
        self.assertEqual(o._active_gen, 0x1234)

    def test_other_action_status4_is_not_a_completion(self):
        o = self._loading(5)
        o._apply_action_status(
            {"action": oams.OAMS_STATUS_FORWARD_FOLLOWING,
             "code": S.OAMS_OP_CODE_SUCCESS, "value": 0}, wire_gen=5)
        self.assertEqual(o.completions, [])
        self.assertEqual(o._active_gen, 5)

    def test_calibration_value_is_carried_only_for_calibration(self):
        o = make_wide_oams()
        o._active_action = o.status_calibrating
        o._active_gen = 9
        o.action_status = oams.OAMS_STATUS_CALIBRATING
        o._apply_action_status(
            {"action": o.status_calibrating,
             "code": S.OAMS_OP_CODE_SUCCESS, "value": 2400}, wire_gen=9)
        self.assertEqual(o.completions,
                         [(S.OAMS_OP_CODE_SUCCESS, 2400, 9)])
        self.assertEqual(o.action_status_value, 2400)

    def test_clear_errors_drops_the_active_mirror(self):
        o = make_wide_oams()
        o._active_action = o.status_loading
        o._active_gen = 5
        o.bay_count = 0
        o.determine_current_spool = lambda: None
        o.clear_errors()
        self.assertIsNone(o._active_action)
        self.assertIsNone(o._active_gen)


class DetectProtocolTests(unittest.TestCase):
    def _detect(self, version, commands):
        o = make_oams(use_gen_protocol=False)
        o.protocol_version = version
        o.mcu = FakeMCU(commands)
        o._detect_gen_protocol()
        return o

    def test_complete_v4_dictionary_selects_the_full_width_family(self):
        o = self._detect(4, V4_COMMANDS | V2_COMMANDS)
        self.assertTrue(o._use_gen_protocol)
        self.assertTrue(o._use_wide_gen_protocol)
        self.assertTrue(o.supports_native_operations)
        self.assertEqual(o.mcu.responses, [STATUS4])

    def test_every_single_missing_v4_shape_falls_back_to_v2(self):
        for missing in sorted(V4_COMMANDS):
            o = self._detect(4, (V4_COMMANDS - {missing}) | V2_COMMANDS)
            self.assertTrue(o._use_gen_protocol, missing)
            self.assertFalse(o._use_wide_gen_protocol, missing)
            self.assertFalse(o.supports_native_operations, missing)
            # One family only: status2 is registered, status4 never is.
            self.assertEqual(o.mcu.responses, [STATUS2], missing)
            # No partial v4 handle is left behind to be used by mistake.
            self.assertIsNone(o.oams_load_spool4_cmd, missing)
            self.assertIsNone(o.oams_cancel4_cmd, missing)

    def test_partial_v4_without_v2_falls_back_to_legacy(self):
        o = self._detect(4, V4_COMMANDS - {"oams_cmd_cancel4 action=%c gen=%u"})
        self.assertFalse(o._use_gen_protocol)
        self.assertFalse(o._use_wide_gen_protocol)
        self.assertEqual(o.mcu.responses, [])

    def test_v4_shapes_are_ignored_below_protocol_four(self):
        for version in (None, 2, 3):
            o = self._detect(version, V4_COMMANDS | V2_COMMANDS)
            self.assertTrue(o._use_gen_protocol)
            self.assertFalse(o._use_wide_gen_protocol)
            self.assertEqual(o.mcu.responses, [STATUS2])

    def test_v2_only_dictionary_is_unchanged(self):
        o = self._detect(3, V2_COMMANDS)
        self.assertTrue(o._use_gen_protocol)
        self.assertFalse(o._use_wide_gen_protocol)
        self.assertEqual(o.mcu.responses, [STATUS2])

    def test_legacy_dictionary_is_unchanged(self):
        o = self._detect(None, set())
        self.assertFalse(o._use_gen_protocol)
        self.assertFalse(o._use_wide_gen_protocol)
        self.assertEqual(o.mcu.responses, [])


class V2WireBytesUnchangedTests(unittest.TestCase):
    """Widening the reducer's generation must not change one byte that the
    v2 or legacy families put on the wire, or how their echoes are matched:
    the runtime hands these drivers the low byte, exactly as the reducer's
    old one-byte counter did."""

    def test_v2_bytes_are_byte_wide_for_every_operation(self):
        for gen in (1, 7, 255, 0, 1, 0x100 & 0xFF):
            o = make_oams(use_gen_protocol=True)
            o.start_load_spool(2, gen=gen)
            o.start_unload_spool(gen=gen)
            o.start_calibrate("ptfe", 1, gen=gen)
            o.start_calibrate("hub_hes", 3, gen=gen)
            self.assertEqual(o.oams_load_spool2_cmd.sent, [[2, gen]])
            self.assertEqual(o.oams_unload_spool2_cmd.sent, [[gen]])
            self.assertEqual(o.oams_calibrate_ptfe_length2_cmd.sent,
                             [[1, gen]])
            self.assertEqual(o.oams_calibrate_hub_hes2_cmd.sent, [[3, gen]])
            self.assertEqual(list(o._gen_queue), [])     # echo is on the wire

    def test_v2_masks_a_wide_value_to_its_low_byte(self):
        o = make_oams(use_gen_protocol=True)
        o.start_load_spool(2, gen=0x12345607)
        self.assertEqual(o.oams_load_spool2_cmd.sent, [[2, 0x07]])
        self.assertIsNone(o.oams_load_spool4_cmd)

    def test_v2_atomic_load_bytes(self):
        o = make_oams(use_gen_protocol=True)
        o.bay_count = 4
        o.filament_path_length = 2145
        o.ptfe_lengths = [None] * 4
        o.oams_load_spool3_cmd = FakeCmd()
        o.start_load_spool(1, gen=0x107)
        self.assertEqual(o.oams_load_spool3_cmd.sent, [[1, 7, 2145]])
        self.assertEqual(o.oams_load_spool2_cmd.sent, [])

    def test_legacy_bytes_carry_no_generation(self):
        o = make_oams(use_gen_protocol=False)
        o.start_load_spool(2, gen=300)
        o.start_unload_spool(gen=301)
        o.start_calibrate("ptfe", 1, gen=302)
        self.assertEqual(o.oams_load_spool_cmd.sent, [[2]])
        self.assertEqual(o.oams_unload_spool_cmd.sent, [[]])
        self.assertEqual(o.oams_calibrate_ptfe_length_cmd.sent, [[1]])
        self.assertEqual(list(o._gen_queue), [300, 301, 302])

    def test_v2_echo_is_passed_through_as_received(self):
        o = make_oams(use_gen_protocol=True)
        o.action_status = oams.OAMS_STATUS_LOADING
        o._apply_action_status(
            {"action": oams.OAMS_STATUS_LOADING,
             "code": S.OAMS_OP_CODE_SUCCESS, "value": 0}, wire_gen=9)
        self.assertEqual(o.completions, [(S.OAMS_OP_CODE_SUCCESS, None, 9)])

    def test_v2_mirror_moves_without_an_active_record(self):
        # v2/legacy never had the v4 active-operation gate.
        o = make_oams(use_gen_protocol=True)
        o._pending_bay = 3
        o._apply_action_status(
            {"action": oams.OAMS_STATUS_LOADING,
             "code": S.OAMS_OP_CODE_SUCCESS, "value": 0}, wire_gen=77)
        self.assertEqual(o.current_spool, 3)

    def test_the_runtime_and_reducer_keep_a_byte_driver_in_step(self):
        # End to end across the widening: 300 operations on one lane, through
        # the real runtime and a v2-style driver that echoes the byte it was
        # given. Every operation must still complete.
        import test_oams_runtime as rt
        runtime, reactor, oam, events = rt.make_runtime()
        for _ in range(300):
            runtime.system = S.SystemState(lanes={rt.FPS: S.replace(
                runtime.system.lanes[rt.FPS], op=S.OP_UNLOADED, unit=None,
                group=None)})
            completion = runtime.request(rt.FPS, S.LoadBay(rt.FPS, rt.UNIT))
            wire = oam.calls[-1][2]
            self.assertLessEqual(wire, 0xFF)
            runtime.dispatch(S.OpCompleted(
                rt.FPS, S.OAMS_OP_CODE_SUCCESS, gen=wire))
            self.assertTrue(completion.wait().ok)
        self.assertEqual(runtime.system.lanes[rt.FPS].op_gen, 300)


if __name__ == "__main__":
    unittest.main(verbosity=2)
