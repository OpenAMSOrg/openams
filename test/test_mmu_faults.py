"""A latched klipper-mmu fault reaches the UIs, and its Clear works.

C5 §7.6 gives every km_fault a C2 §5.7 severity and action tokens, and the
touchscreen, Mainsail and Fluidd panels render exactly those. These tests drive
the real projection table and the real manager enrichment with a klipper-mmu
unit double, and follow the `OAMS_CLEAR_FAULT OAMS=<idx>` route the UIs send.

  python3 -m unittest discover -s test -p 'test_mmu_faults*.py' -v
"""

import dataclasses
import importlib
import importlib.util
import os
import sys
import types
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _path in (os.path.join(_ROOT, "tools"), os.path.join(_ROOT, "src"),
              _HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from test_oams_manager import (FakeError, FakeFps, FakeGcmd, FakeGroup,
                               FakeOam, S, build_manager)

ui = importlib.import_module("oamspkg.openams_ui")

# The klipper-mmu host package's own __init__ needs Klipper, which this suite
# has no instance of, so the real projection and unit hooks are loaded by path
# in dependency order instead of imported.
_MMU_HOST = os.path.join(_ROOT, "klipper_mmu", "host")
_MMU_MODULES = ("wire", "protocol", "requests", "openams_sensor_projection",
                "physical_ownership", "openams_device_state",
                "openams_fault_projection", "openams_device")


def _load_mmu(name):
    full = "klipper_mmu.host.%s" % name
    spec = importlib.util.spec_from_file_location(
        full, os.path.join(_MMU_HOST, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[full] = module
    spec.loader.exec_module(module)
    return module


MMU = types.SimpleNamespace(**{name: _load_mmu(name)
                               for name in _MMU_MODULES})


class MmuUnit(FakeOam):
    """A bound klipper-mmu unit: the real C5 §7.6 projection and the real
    OAMS_CLEAR_FAULT route, with the fault acknowledgement recorded."""

    motion_available = False
    device_family = "boxturtle"
    hardware_snapshot_ready = True

    def __init__(self, idx=7, fps="fps1", bays=4):
        FakeOam.__init__(self, idx, fps)
        self.bay_count = bays
        self.capabilities = {"physical_observation": True}
        self.acknowledged = []
        self.registration_fails = False
        device = MMU.openams_device.OpenAMSDevice
        self.firmware_fault_info = types.MethodType(
            device.firmware_fault_info, self)
        self.register_fault_clear_command = types.MethodType(
            device.register_fault_clear_command, self)

    def acknowledge_fault(self):
        if self.registration_fails:
            raise ValueError("Retained fault ACK recovery is unavailable")
        self.acknowledged.append(True)
        return True

    def get_webhook_status(self):
        """The real OpenAMSDevice.get_webhook_status shape with no capture.

        A klipper-mmu unit publishes no per-bay `bays` list and no
        `supported_actions`, so a UI reads this unit's state from its lane and
        from the one fault entry the manager enriches."""
        return {"schema_version": 1, "oams_idx": self.oams_idx,
                "connected": True, "hardware_snapshot_ready": True,
                "family": self.device_family, "bay_count": self.bay_count,
                "fps_name": self.fps_name, "path_length_mm": 842.5,
                "current_spool": None, "f1s_hes_value": None,
                "load_present": None, "hub_hes_value": None,
                "protocol_version": None,
                "firmware_owns_liveness": True}


class MMUObserver:
    def __init__(self, unit):
        self.unit = unit

    def get_openams_unit(self):
        return self.unit


def mmu_manager(unit, fault=None, physical_lane=1):
    """A manager with one klipper-mmu unit and, at most, one latched fault."""
    objects = {"fps": [("fps", FakeFps("fps1"))],
               "klipper_mmu": [("klipper_mmu box", MMUObserver(unit))],
               "filament_group": [("filament_group T0",
                                   FakeGroup([("box", 0)]))]}
    manager = build_manager(objects, ["filament_group T0", "fps",
                                      "klipper_mmu box"])
    manager.ready = True
    manager._startup_resync_pending = False
    manager.fpss["fps1"].extruder_name = "extruder"
    # _ui_commands() reads the registered commands off the gcode object.
    manager._ui_commands = lambda: {}
    lanes = dict(manager.runtime.get_state().lanes)
    if fault is not None:
        manager.runtime._sys = S.SystemState(
            lanes=lanes,
            firmware_faults={physical_lane: ("fps1", fault, ("identity",))})
    return manager


# C5 §7.6, as the UIs must see it. Two corrections the klipper-mmu wire forces
# on the published tokens, both recorded in C5 §7.6: no `clear_errors` (this
# unit's clear_errors raises, so the token would clear nothing), and no
# `clear_fault` on a fault whose physical state a person must settle first.
CONTRACT = (
    ("HOST_COMM_LOST", 1, "stop", "mmu_host_comm_lost", []),
    ("DRIVER_OVERCURRENT", 2, "stop", "mmu_driver_overcurrent", []),
    ("DRIVER_FAULT", 3, "stop", "mmu_driver_fault", []),
    ("NO_PROGRESS", 4, "pause", "mmu_no_progress", ["retry_load"]),
    ("JAM", 5, "pause", "mmu_jam", ["retry_load"]),
    ("BUFFER_SIGNAL", 6, "pause", "mmu_buffer_signal", ["clear_fault"]),
    ("FILAMENT_LOST", 7, "pause", "mmu_filament_lost", ["retry_load"]),
    ("STARVED", 8, "pause", "mmu_starved", ["retry_load"]),
    ("STALLED", 9, "pause", "mmu_stalled", ["retry_load"]),
    ("EMERGENCY_STOP", 10, "stop", "mmu_emergency_stop", ["clear_fault"]),
    ("SENSOR_FAILURE", 11, "stop", "mmu_sensor_failure", ["clear_fault"]),
    ("CONTROL_TIMING", 12, "stop", "mmu_control_timing", ["clear_fault"]),
    ("SELECTOR", 13, "stop", "mmu_selector", []),
)

# The only tokens C2 §5.7's action table has; the UIs show nothing else.
UI_TOKENS = frozenset(("clear_errors", "clear_fault", "resume", "retry_load"))

# A lane latch survives every recovery except reconciliation, so these three
# are the ones an operator must settle physically first.
NEEDS_RECONCILIATION = ("DRIVER_OVERCURRENT", "DRIVER_FAULT", "SELECTOR")


def contract_of(name):
    return next(row for row in CONTRACT if row[0] == name)


class MmuFaultProjectionTests(unittest.TestCase):
    """Every C5 fault class reaches devices.<unit>.faults and the UI."""

    def published(self, fault, physical_lane=1):
        status = mmu_manager(MmuUnit(), fault, physical_lane).get_status(0.0)
        return status["devices"]["box"]["faults"]

    def test_every_c5_fault_projects_its_severity_code_and_actions(self):
        for name, value, severity, code, actions in CONTRACT:
            with self.subTest(fault=name):
                fault, = self.published(value)
                self.assertEqual(fault["severity"], severity)
                self.assertEqual(fault["code"], code)
                self.assertEqual(fault["actions"], actions)
                self.assertEqual(fault["source"], "firmware")
                self.assertEqual(fault["bay"], 1)
                self.assertNotEqual(fault["text"], str(value))
                self.assertLessEqual(
                    len(fault["text"]),
                    MMU.openams_fault_projection.MAX_TEXT)

    def test_a_lane_fault_names_its_bay_and_a_unit_wide_one_does_not(self):
        for physical_lane, bay in ((0, 0), (3, 3), (255, None)):
            with self.subTest(physical_lane=physical_lane):
                fault, = self.published(11, physical_lane)
                self.assertEqual(fault["bay"], bay)
                self.assertEqual(fault["code"], "mmu_sensor_failure")

    def test_the_offered_tokens_are_the_ones_the_uis_know(self):
        for name, value, _severity, _code, actions in CONTRACT:
            with self.subTest(fault=name):
                fault, = self.published(value)
                self.assertTrue(set(fault["actions"]) <= UI_TOKENS)
                self.assertNotIn("clear_errors", fault["actions"])

    def test_a_fault_needing_reconciliation_offers_no_clear_fault(self):
        for name in NEEDS_RECONCILIATION:
            with self.subTest(fault=name):
                _name, value, severity, _code, actions = contract_of(name)
                fault, = self.published(value)
                self.assertEqual(severity, "stop")
                self.assertEqual(fault["actions"], actions)
                self.assertNotIn("clear_fault", fault["actions"])
                # Its text says what to do instead of offering a button.
                self.assertTrue(fault["text"])

    def test_a_lost_host_link_offers_nothing(self):
        """C5 §7.6: HOST_COMM_LOST's recovery is FIRMWARE_RESTART."""
        fault, = self.published(1)
        self.assertEqual(fault["actions"], [])
        self.assertIn("firmware", fault["text"])

    def test_openams_ui_carries_the_fault_with_its_unit_and_bay(self):
        status = mmu_manager(MmuUnit(), 12, 2).get_status(0.0)
        view = ui.project(status, "box")
        self.assertEqual(view["nf"], 1)
        self.assertEqual(view["fault"]["sev"], "stop")
        self.assertEqual(view["fault"]["code"], "mmu_control_timing")
        self.assertEqual(view["fault"]["unit"], "box")
        self.assertEqual(view["fault"]["bay"], 2)
        self.assertEqual(view["fault"]["act"], ["clear_fault"])
        self.assertLessEqual(len(view["fault"]["text"]), 48)
        # The token is per fault: this unit publishes no unit-level actions.
        self.assertEqual(view["act"], [])

    def test_a_stop_fault_withholds_every_motion_action(self):
        for value in (2, 10, 12, 13):
            with self.subTest(fault=value):
                status = mmu_manager(MmuUnit(), value).get_status(0.0)
                lane = status["lanes_by_fps"]["fps1"]
                self.assertFalse(lane["motion_settled"])


class MmuClearFaultRouteTests(unittest.TestCase):
    """OAMS_CLEAR_FAULT OAMS=<idx>, the command the UIs send."""

    def routed(self, unit=None):
        unit = MmuUnit() if unit is None else unit
        manager = mmu_manager(unit)
        routes = []
        manager._register_fault_clear_routes(types.SimpleNamespace(
            register_mux_command=lambda *a, **k: routes.append((a, k))))
        return unit, {(a[0], a[1], a[2]): a[3] for a, _k in routes}

    def test_the_route_is_published_for_this_units_index(self):
        _unit, routes = self.routed()
        self.assertEqual(sorted(routes), [("OAMS_CLEAR_FAULT", "OAMS", "7")])

    def test_clear_fault_calls_the_units_fault_acknowledgement(self):
        unit, routes = self.routed()
        gcmd = FakeGcmd({})
        routes[("OAMS_CLEAR_FAULT", "OAMS", "7")](gcmd)
        self.assertEqual(unit.acknowledged, [True])
        self.assertEqual(len(gcmd.responses), 1)
        self.assertIn("reconcile", gcmd.responses[0])

    def test_a_refused_acknowledgement_is_reported_not_swallowed(self):
        unit, routes = self.routed()
        unit.registration_fails = True
        gcmd = FakeGcmd({})
        with self.assertRaises(FakeError) as caught:
            routes[("OAMS_CLEAR_FAULT", "OAMS", "7")](gcmd)
        self.assertIn("OAMS_CLEAR_FAULT", str(caught.exception))
        self.assertEqual(unit.acknowledged, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
