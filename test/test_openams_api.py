# Manager-layer tests for the HelixScreen UI API v1 status/command contract
# (docs/UI_API.md), ported from klipper_openams master onto the pure-core
# architecture. The v1 status/slot-mapping fields are exercised directly on a
# bare OAMSManager with fakes, exactly like test_oams_manager.py; load/unload
# routing is checked by inspecting the S.Load/S.LoadBay/S.Unload action a
# fake runtime.request() receives, since the actual firmware round trip is
# already covered by test_oams_state.py and test_oams_runtime.py.
#
#   cd openams && python3 -m unittest discover -s test

import importlib
import json
import os
import pathlib
import subprocess
import sys
import types
import unittest

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if "oamspkg" not in sys.modules:
    _pkg = types.ModuleType("oamspkg")
    _pkg.__path__ = [_SRC]
    sys.modules["oamspkg"] = _pkg
sys.modules.setdefault("mcu", types.ModuleType("mcu"))

S = importlib.import_module("oamspkg.oams_state")
T = importlib.import_module("oamspkg.oams_topology")
manager_mod = importlib.import_module("oamspkg.oams_manager")
OAMSManager = manager_mod.OAMSManager


class FakeReactor:
    def __init__(self):
        self.now = 10.0

    def monotonic(self):
        return self.now

    def pause(self, wake_time):
        self.now = wake_time


class FakeUnit:
    """A minimal OAMS-shaped unit for status/slot-mapping tests."""

    def __init__(self, idx, ready, loaded=None, fps_target=None, band=None):
        self.oams_idx = idx
        self.fps_name = None
        self.f1s_hes_value = list(ready)
        self.hub_hes_value = list(loaded or [False] * len(ready))
        self.capabilities = {}
        self.connected = True
        self.fps_target = fps_target
        if band is not None:
            self.fps_lower_threshold, self.fps_upper_threshold = band

    def bay_loadable(self, bay):
        return bool(self.f1s_hes_value[bay])

    def get_webhook_status(self):
        return {"connected": self.connected}


class FakeFps:
    def __init__(self, name, value=0.5):
        self.fps_name = name
        self.extruder_name = "extruder"
        self.extruder = types.SimpleNamespace(last_position=0.0)
        self.value = value

    def get_status(self, now):
        return {"fps_value": self.value}


class FakeGcmd:
    def __init__(self, params=None, **more):
        self._p = dict(params or {})
        self._p.update(more)
        self.responses = []

    def get(self, key, default="__required__"):
        if key in self._p:
            return self._p[key]
        if default == "__required__":
            raise self.error("missing %s" % key)
        return default

    def get_int(self, key, default="__required__", minval=None, maxval=None):
        if key not in self._p:
            if default == "__required__":
                raise self.error("missing %s" % key)
            return default
        value = int(self._p[key])
        if minval is not None and value < minval:
            raise self.error("%s below minimum" % key)
        if maxval is not None and value > maxval:
            raise self.error("%s above maximum" % key)
        return value

    def error(self, message):
        return ValueError(message)

    def respond_info(self, message):
        self.responses.append(message)


class RecordingRuntime:
    """Records every runtime.request(fps, action) and answers with a canned
    OpResult, so manager-level routing (which action, which unit/group) is
    checked without re-exercising the reducer/driver plumbing tested
    elsewhere."""

    def __init__(self, lanes, result=None):
        self._sys = S.SystemState(lanes=lanes)
        self.requests = []
        self.result = result or S.OpResult(True, S.OAMS_OP_CODE_SUCCESS, "ok")

    def get_state(self):
        return self._sys

    def request(self, fps, action):
        self.requests.append((fps, action))
        result = self.result
        return types.SimpleNamespace(wait=lambda: result)


def build_manager(lane=None):
    """One lane ("fps"), two OAMS units (oams1 idx=1, oams2 idx=2, 4 bays
    each), groups T0 = {oams1-1, oams2-0} and T1 = {oams2-2, oams1-3} --
    mirroring the fixture klipper_openams master used for this contract, so
    the resulting global slot ids/status match its expectations exactly."""
    unit1 = FakeUnit(1, [True, True, False, True], [False, True, False, False],
                     fps_target=0.45, band=(0.3, 0.7))
    unit2 = FakeUnit(2, [True, False, True, True])
    mgr = OAMSManager.__new__(OAMSManager)
    mgr.oams = {"oams1": unit1, "oams2": unit2}
    mgr.fpss = {"fps": FakeFps("fps")}
    mgr.topo = T.build_topology(
        ["fps"],
        [T.OamsSpec(name="oams1", idx=1, fps="fps"),
         T.OamsSpec(name="oams2", idx=2, fps="fps")],
        [("T0", [("oams1", 1), ("oams2", 0)]),
         ("T1", [("oams2", 2), ("oams1", 3)])])
    mgr.group_lane = {"T0": "fps", "T1": "fps"}
    mgr.lane_oams = {"fps": [unit1, unit2]}
    mgr.oam_by_idx = {1: unit1, 2: unit2}
    mgr.ready = True
    mgr.reactor = FakeReactor()
    mgr._published_pressure = {}
    if lane is None:
        lane = S.LaneState(op=S.OP_LOADED, group="T0", unit=(1, 1))
    mgr.runtime = RecordingRuntime({"fps": lane})
    mgr.printer = types.SimpleNamespace(lookup_object=lambda name:
        types.SimpleNamespace(get_status=lambda now: {"commands": {
            command: {} for command in (
                "OAMSM_LOAD_TO_TOOLHEAD", "OAMSM_UNLOAD_FROM_TOOLHEAD",
                "OAMSM_LOAD_FILAMENT_CANCEL", "OAMSM_CLEAR_ERRORS")
        }}))
    return mgr, unit1, unit2


class StatusContractTests(unittest.TestCase):
    def test_status_contract_is_versioned_and_uses_stable_global_slots(self):
        mgr, unit1, unit2 = build_manager()

        status = mgr.get_status(10.0)

        self.assertEqual(status["api_version"], 1)
        self.assertEqual(status["schema"], "openams.manager")
        self.assertEqual(status["commands"], {
            "load": "OAMSM_LOAD_TO_TOOLHEAD",
            "unload": "OAMSM_UNLOAD_FROM_TOOLHEAD",
            "cancel": "OAMSM_LOAD_FILAMENT_CANCEL",
            "reset": "OAMSM_CLEAR_ERRORS"})
        self.assertEqual([u["id"] for u in status["units"]], ["1", "2"])
        self.assertEqual([u["topology"] for u in status["units"]], ["hub", "hub"])
        self.assertEqual([s["id"] for s in status["units"][0]["slots"]],
                         [0, 1, 2, 3])
        self.assertEqual([s["id"] for s in status["units"][1]["slots"]],
                         [4, 5, 6, 7])
        groups = {g["name"]: g for g in status["groups"]}
        self.assertEqual(groups["T0"]["slots"], [1, 4])
        self.assertEqual(groups["T1"]["slots"], [6, 3])
        # "lanes" is the UI API v1 array (docs/UI_API.md); released
        # HelixScreen builds read it through printer.objects.subscribe/query.
        self.assertEqual(status["lanes"], [{
            "id": "fps", "state": "loaded", "current_group": "T0",
            "current_slot": 1, "following": False, "direction": 1,
            "message": None, "pressure": 0.5, "set_point": 0.45}])
        # "lanes_by_fps" is additive, outside the v1 contract: the same
        # per-FPS dict this field has always published, enriched with the
        # v1 per-lane fields (current_slot/pressure/set_point/state).
        lane_status = status["lanes_by_fps"]["fps"]
        self.assertEqual(lane_status["state"], "loaded")
        self.assertEqual(lane_status["current_slot"], 1)
        self.assertEqual(lane_status["pressure"], 0.5)
        self.assertEqual(lane_status["set_point"], 0.45)
        self.assertEqual(lane_status["pressure_band"], [0.3, 0.7])
        json.dumps(status)

    def test_webhook_lanes_and_api_snapshot_are_unchanged(self):
        """The openams/status webhook keeps its own top-level "lanes" dict
        (its own legacy contract, independent of oams_manager.get_status())
        and its "api" key holds the complete UI API v1 payload, unaffected by
        get_status() gaining "lanes"/"lanes_by_fps" (docs/UI_API.md)."""
        mgr, _, _ = build_manager()
        replies = []
        mgr._webhook_status(types.SimpleNamespace(send=replies.append))
        status = replies[0]["status"]["openams"]
        self.assertIsInstance(status["lanes"], dict)
        api = status["api"]
        self.assertEqual(api["api_version"], 1)
        self.assertEqual(api["schema"], "openams.manager")
        self.assertEqual(api["lanes"], [{
            "id": "fps", "state": "loaded", "current_group": "T0",
            "current_slot": 1, "following": False, "direction": 1,
            "message": None, "pressure": 0.5, "set_point": 0.45}])
        json.dumps(replies)

    def test_old_macro_installation_does_not_advertise_unavailable_ui_commands(self):
        mgr, _, _ = build_manager()
        mgr.printer = types.SimpleNamespace(lookup_object=lambda name:
            types.SimpleNamespace(get_status=lambda now: {"commands": {
                "OAMSM_LOAD_FILAMENT_CANCEL": {}, "OAMSM_CLEAR_ERRORS": {}
            }}))
        status = mgr.get_status(10.)
        self.assertEqual(status["current_group"], "T0")
        self.assertEqual(status["commands"], {
            "cancel": "OAMSM_LOAD_FILAMENT_CANCEL", "reset": "OAMSM_CLEAR_ERRORS"})

    def test_status_before_ready_and_without_units_is_serializable(self):
        mgr, _, _ = build_manager(lane=S.LaneState())
        mgr.ready = False
        mgr.oams = {}
        mgr.topo = T.build_topology(["fps"], [], [])
        mgr.group_lane = {}
        mgr.lane_oams = {"fps": []}
        status = mgr.get_status(0.)
        self.assertIs(status["ready"], False)
        self.assertEqual(status["units"], [])
        self.assertEqual(status["lanes"], [{
            "id": "fps", "state": "unloaded", "current_group": None,
            "current_slot": None, "following": False, "direction": 1,
            "message": None, "pressure": 0.5, "set_point": None}])
        self.assertEqual(status["lanes_by_fps"]["fps"]["state"], "unloaded")
        json.dumps(status)

    def test_lane_pressure_follows_the_fps_past_a_deadband(self):
        mgr, unit1, _ = build_manager()
        fps = mgr.fpss["fps"]

        lane = mgr.get_status(1.)["lanes_by_fps"]["fps"]
        self.assertEqual(lane["pressure"], 0.5)
        self.assertEqual(lane["set_point"], 0.45)
        # The v1 array carries the same reading.
        self.assertEqual(mgr.get_status(1.)["lanes"][0]["pressure"], 0.5)

        # Noise inside the deadband keeps the published value still.
        fps.value = 0.515
        self.assertEqual(mgr.get_status(2.)["lanes_by_fps"]["fps"]["pressure"], 0.5)

        fps.value = 0.73
        self.assertEqual(mgr.get_status(3.)["lanes_by_fps"]["fps"]["pressure"], 0.73)

        # Readings outside the rail are clamped to 0..1.
        fps.value = 1.2
        self.assertEqual(mgr.get_status(4.)["lanes_by_fps"]["fps"]["pressure"], 1.0)


class ValidateLoadTests(unittest.TestCase):
    def test_rejects_unknown_group(self):
        mgr, _, _ = build_manager()
        with self.assertRaises(ValueError):
            mgr.cmd_VALIDATE_LOAD(FakeGcmd(GROUP="missing", SLOT=0))

    def test_rejects_slot_outside_group(self):
        mgr, _, _ = build_manager()
        with self.assertRaisesRegex(ValueError, "not assigned to group T1"):
            mgr.cmd_VALIDATE_LOAD(FakeGcmd(GROUP="T1", SLOT=0))

    def test_rejects_out_of_range_slot(self):
        mgr, _, _ = build_manager()
        with self.assertRaisesRegex(ValueError, "does not exist"):
            mgr.cmd_VALIDATE_LOAD(FakeGcmd(GROUP="T1", SLOT=99))

    def test_rejects_unready_slot(self):
        mgr, unit1, _ = build_manager()
        unit1.f1s_hes_value[3] = False   # slot 3 == oams1-3, in T1
        with self.assertRaisesRegex(ValueError, "not ready"):
            mgr.cmd_VALIDATE_LOAD(FakeGcmd(GROUP="T1", SLOT=3))

    def test_accepts_the_currently_loaded_slot_even_if_not_marked_ready(self):
        mgr, unit1, _ = build_manager()
        unit1.f1s_hes_value[1] = False   # slot 1 == oams1-1, the loaded bay
        mgr.cmd_VALIDATE_LOAD(FakeGcmd(GROUP="T0", SLOT=1))   # must not raise

    def test_rejects_when_not_ready(self):
        mgr, _, _ = build_manager()
        mgr.ready = False
        with self.assertRaisesRegex(ValueError, "not ready"):
            mgr.cmd_VALIDATE_LOAD(FakeGcmd(GROUP="T0", SLOT=1))


class TargetedLoadRoutingTests(unittest.TestCase):
    def test_untargeted_load_dispatches_group_action(self):
        mgr, _, _ = build_manager(lane=S.LaneState())
        mgr.cmd_LOAD_FILAMENT(FakeGcmd(GROUP="T0"))
        fps, action = mgr.runtime.requests[-1]
        self.assertEqual(fps, "fps")
        self.assertIsInstance(action, S.Load)
        self.assertEqual(action.group, "T0")

    def test_targeted_load_uses_requested_slot_from_group(self):
        mgr, unit1, unit2 = build_manager(lane=S.LaneState())
        mgr.cmd_LOAD_FILAMENT(FakeGcmd(GROUP="T1", SLOT=6))   # oams2-2
        fps, action = mgr.runtime.requests[-1]
        self.assertEqual(fps, "fps")
        self.assertIsInstance(action, S.LoadBay)
        self.assertEqual(action.unit, (2, 2))

    def test_targeted_load_rejects_slot_outside_group_before_dispatch(self):
        mgr, _, _ = build_manager(lane=S.LaneState())
        with self.assertRaisesRegex(ValueError, "not assigned to group T1"):
            mgr.cmd_LOAD_FILAMENT(FakeGcmd(GROUP="T1", SLOT=0))
        self.assertEqual(mgr.runtime.requests, [])

    def test_load_failure_raises_gcode_error(self):
        mgr, _, _ = build_manager(lane=S.LaneState())
        mgr.runtime.result = S.OpResult(False, S.OAMS_OP_CODE_ERROR_BUSY, "busy")
        with self.assertRaisesRegex(ValueError, "busy"):
            mgr.cmd_LOAD_FILAMENT(FakeGcmd(GROUP="T0"))

    def test_load_cancel_is_informational_not_an_error(self):
        mgr, _, _ = build_manager(lane=S.LaneState())
        mgr.runtime.result = S.OpResult(False, S.OAMS_OP_CODE_CANCEL, "cancelled")
        gcmd = FakeGcmd(GROUP="T0")
        mgr.cmd_LOAD_FILAMENT(gcmd)   # must not raise
        self.assertEqual(gcmd.responses, ["cancelled"])


class UpgradeImportTests(unittest.TestCase):
    def test_flat_extras_layout_imports_cleanly_outside_the_source_tree(self):
        """install-openams.sh symlinks every src/*.py flat into
        klippy/extras/ (both on a fresh install and when re-run to upgrade).
        oams_manager's `from . import oams_state` etc. must resolve there
        (as sibling top-level modules of the `extras` package), not only
        inside this repository's own src/ package layout."""
        import tempfile
        source = pathlib.Path(__file__).resolve().parents[1] / "src"
        with tempfile.TemporaryDirectory() as tmp:
            extras = pathlib.Path(tmp) / "extras"
            extras.mkdir()
            for path in source.glob("*.py"):
                (extras / path.name).symlink_to(path)
            result = subprocess.run([sys.executable, "-c", """
import sys, types
sys.modules['mcu'] = types.ModuleType('mcu')
from extras.oams_manager import OAMSManager
assert hasattr(OAMSManager, 'get_status')
assert hasattr(OAMSManager, 'cmd_VALIDATE_LOAD')
"""], cwd=tmp, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
