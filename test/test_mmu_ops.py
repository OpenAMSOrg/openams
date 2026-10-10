"""OAMS_PRELOAD/UNPRELOAD/EJECT/HOME reach a klipper-mmu unit, and refuse
everywhere else.

klipper-mmu drives PRELOAD, UNPRELOAD, EJECT and HOME through its own
KLIPPER_MMU_* commands, so an OpenAMS UI had no way to offer them. C3 §2
publishes the four as per-unit OAMS_* commands on the same OAMS=<idx> key
every other per-unit command uses, and C5 §7.1 maps each to the C2 action
token a UI offers it under.

  python3 -m unittest discover -s test -p 'test_mmu_ops*.py' -v
"""

import importlib
import unittest

from test_oams_manager import (FakeError, FakeFps, FakeGcmd, FakeGroup,
                               FakeOam, build_manager)
from test_oams_manager_mmu_discovery import MMUObserver

ui = importlib.import_module("oamspkg.openams_ui")

# The four commands, the unit method each one routes to, and the bay it
# carries. `home` is the only one with no bay: it moves the selector.
BAY_COMMANDS = (("OAMS_PRELOAD", "preload"),
                ("OAMS_UNPRELOAD", "unpreload"),
                ("OAMS_EJECT", "eject"))
UNIT_COMMANDS = (("OAMS_HOME", "home"),)


class MmuUnit(FakeOam):
    """A bound klipper-mmu unit: every op recorded, nothing simulated.

    The methods are the ones OpenAMSManagedUnit gains for C3 §2, so a
    recorded call is the unit's own operation with the bay the command
    named."""

    motion_available = True
    device_family = "boxturtle"
    hardware_snapshot_ready = True

    def __init__(self, idx=7, fps="fps1", family="boxturtle", bays=4,
                 refuses=()):
        FakeOam.__init__(self, idx, fps)
        self.device_family = family
        self.bay_count = bays
        self.capabilities = {"bays": bays, "normal_motion": True}
        self.ops = []
        self.refuses = refuses

    def _op(self, name, *args):
        if name in self.refuses:
            raise RuntimeError("MMU motion permission is unavailable")
        self.ops.append((name,) + args)
        return "accepted"

    def preload(self, bay):
        return self._op("preload", bay)

    def unpreload(self, bay):
        return self._op("unpreload", bay)

    def eject(self, bay):
        return self._op("eject", bay)

    def home(self):
        return self._op("home")

    def clear_errors(self):
        self.ops.append(("clear_errors",))

    def get_webhook_status(self):
        """The unit's own status, with the bays the manager enriches."""
        return {"schema_version": 1, "oams_idx": self.oams_idx,
                "connected": True, "hardware_snapshot_ready": True,
                "family": self.device_family, "bay_count": self.bay_count,
                "fps_name": self.fps_name, "path_length_mm": 600.0,
                "current_spool": None, "f1s_hes_value": [1] * self.bay_count,
                "hub_hes_value": [0] * self.bay_count,
                "protocol_version": 3, "firmware_owns_liveness": True,
                "capabilities": dict(self.capabilities),
                "identity": {"serial": "000000000000000000000005", "firmware": None,
                             "board": None},
                "calibration": {"state": "configured", "path_length_mm": 600.0},
                "environment": None, "dryer": None,
                "telemetry": {"domain": {
                    "motion_fault_name": "none", "system_fault_name": "none",
                    "dryer_fault_name": "none"}},
                "supported_actions": ["clear_errors", "load", "unload",
                                      "follower"],
                "bays": [{"index": bay, "ready": True, "loaded": False,
                          "calibrated": True, "positioning": False,
                          "calibration_state": "configured",
                          "path_length_mm": 600.0, "spool_id": None,
                          "link_source": "none", "tag": None,
                          "spool_hint": None}
                         for bay in range(self.bay_count)]}


def mmu_manager(unit, groups=("box", 0)):
    objects = {"fps": [("fps", FakeFps("fps1"))],
               "klipper_mmu": [("klipper_mmu box", MMUObserver(unit))],
               "filament_group": [("filament_group T0", FakeGroup([groups]))]}
    manager = build_manager(objects, ["fps", "klipper_mmu box",
                                      "filament_group T0"])
    manager.ready = True
    manager._startup_resync_pending = False
    manager.fpss["fps1"].extruder_name = "extruder"
    # _ui_commands() reads the registered commands off the gcode object.
    manager._ui_commands = lambda: {}
    return manager


def ams_manager():
    """One ordinary AMS unit, which has none of the four operations."""
    unit = FakeOam(0, "fps1")
    objects = {"fps": [("fps", FakeFps("fps1"))],
               "oams": [("oams oams1", unit)],
               "filament_group": [("filament_group T0",
                                   FakeGroup([("oams1", 0)]))]}
    manager = build_manager(objects, ["fps", "oams oams1",
                                      "filament_group T0"])
    manager.ready = True
    manager._startup_resync_pending = False
    manager.fpss["fps1"].extruder_name = "extruder"
    manager._ui_commands = lambda: {}
    return manager


class RecordingGcode:
    """The mux registrations the manager makes, keyed as Klipper keys them."""

    def __init__(self):
        self.mux = {}

    def register_mux_command(self, name, key, value, func, desc=None):
        self.mux[(name, key, value)] = func

    def command(self, name, idx):
        return self.mux[(name, "OAMS", str(idx))]


def routes(manager):
    gcode = RecordingGcode()
    manager._register_mmu_op_routes(gcode)
    return gcode


class BayCommandTests(unittest.TestCase):
    """OAMS_PRELOAD/UNPRELOAD/EJECT SPOOL=<bay> reach the unit's own op."""

    def sent(self, unit, command, bay):
        manager = mmu_manager(unit)
        gcmd = FakeGcmd({"OAMS": str(unit.oams_idx), "SPOOL": str(bay)})
        routes(manager).command(command, unit.oams_idx)(gcmd)
        return unit, gcmd

    def test_each_command_reaches_the_unit_method_with_that_bay(self):
        for command, op in BAY_COMMANDS:
            with self.subTest(command=command):
                unit, gcmd = self.sent(MmuUnit(), command, 2)
                self.assertEqual(unit.ops, [(op, 2)])
                self.assertEqual(len(gcmd.responses), 1)
                self.assertIn(command, gcmd.responses[0])
                self.assertIn("bay 2", gcmd.responses[0])

    def test_every_bay_of_a_wide_unit_is_reachable(self):
        for bay in range(8):
            with self.subTest(bay=bay):
                unit, _gcmd = self.sent(MmuUnit(bays=8), "OAMS_PRELOAD", bay)
                self.assertEqual(unit.ops, [("preload", bay)])

    def test_a_bay_outside_the_unit_is_refused_before_the_unit_is_called(self):
        unit = MmuUnit(bays=4)
        manager = mmu_manager(unit)
        gcmd = FakeGcmd({"OAMS": "7", "SPOOL": "4"})
        with self.assertRaises(FakeError) as caught:
            routes(manager).command("OAMS_PRELOAD", 7)(gcmd)
        self.assertIn("outside", str(caught.exception))
        self.assertEqual(unit.ops, [])

    def test_the_bay_is_required(self):
        unit = MmuUnit()
        manager = mmu_manager(unit)
        gcmd = FakeGcmd({"OAMS": "7"})
        with self.assertRaises(FakeError) as caught:
            routes(manager).command("OAMS_EJECT", 7)(gcmd)
        self.assertIn("SPOOL", str(caught.exception))
        self.assertEqual(unit.ops, [])

    def test_an_unknown_unit_index_is_refused(self):
        manager = mmu_manager(MmuUnit())
        gcmd = FakeGcmd({"OAMS": "9", "SPOOL": "0"})
        with self.assertRaises(FakeError) as caught:
            routes(manager).command("OAMS_PRELOAD", 7)(gcmd)
        self.assertIn("9", str(caught.exception))


class HomeCommandTests(unittest.TestCase):
    """OAMS_HOME is the unit's, and only a selector has one."""

    def test_home_reaches_a_selector_unit(self):
        unit = MmuUnit(family="claymore")
        manager = mmu_manager(unit)
        gcmd = FakeGcmd({"OAMS": "7"})
        routes(manager).command("OAMS_HOME", 7)(gcmd)
        self.assertEqual(unit.ops, [("home",)])

    def test_home_is_refused_on_a_feeder_per_lane_unit(self):
        unit = MmuUnit(family="boxturtle")
        manager = mmu_manager(unit)
        gcmd = FakeGcmd({"OAMS": "7"})
        with self.assertRaises(FakeError) as caught:
            routes(manager).command("OAMS_HOME", 7)(gcmd)
        self.assertIn("selector", str(caught.exception))
        self.assertEqual(unit.ops, [])


class AmsRefusalTests(unittest.TestCase):
    """An AMS unit has none of the four, and says so rather than "unknown"."""

    def command(self, manager, command, params):
        return routes(manager).command(command, 0)(FakeGcmd(params))

    def test_each_command_is_refused_on_an_ams_unit(self):
        for command, _op in BAY_COMMANDS:
            with self.subTest(command=command):
                manager = ams_manager()
                with self.assertRaises(FakeError) as caught:
                    self.command(manager, command, {"OAMS": "0", "SPOOL": "0"})
                self.assertIn(command, str(caught.exception))
                self.assertIn("not a klipper-mmu unit", str(caught.exception))

    def test_home_is_refused_on_an_ams_unit(self):
        manager = ams_manager()
        with self.assertRaises(FakeError) as caught:
            self.command(manager, "OAMS_HOME", {"OAMS": "0"})
        self.assertIn("not a klipper-mmu unit", str(caught.exception))

    def test_an_ams_unit_is_still_offered_a_route_for_each(self):
        manager = ams_manager()
        gcode = routes(manager)
        for command, _op in BAY_COMMANDS + UNIT_COMMANDS:
            with self.subTest(command=command):
                self.assertIn((command, "OAMS", "0"), gcode.mux)


class UnitRefusalTests(unittest.TestCase):
    """What the unit itself refuses is reported, not swallowed."""

    def test_a_refused_op_is_reported_under_the_openams_command_name(self):
        unit = MmuUnit(refuses=("eject",))
        manager = mmu_manager(unit)
        gcmd = FakeGcmd({"OAMS": "7", "SPOOL": "1"})
        with self.assertRaises(FakeError) as caught:
            routes(manager).command("OAMS_EJECT", 7)(gcmd)
        self.assertIn("OAMS_EJECT", str(caught.exception))
        self.assertIn("motion permission", str(caught.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
