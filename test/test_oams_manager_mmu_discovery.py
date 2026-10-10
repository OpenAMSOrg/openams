"""Optional Klipper-MMU physical view in OpenAMS manager discovery."""

import unittest
from types import SimpleNamespace

from test_oams_manager import (FakeError, FakeFps, FakeGcmd, FakeGroup,
                               FakeOam, OAMSManager, S, build_manager)


class MMUObserver:
    def __init__(self, unit=None):
        self.unit = unit

    def get_openams_unit(self):
        return self.unit


class ReadOnlyUnit(FakeOam):
    motion_available = False
    device_family = "boxturtle"
    bay_count = 4
    hardware_snapshot_ready = True

    def __init__(self, idx, fps="fps1"):
        super().__init__(idx, fps)
        self.capabilities = {"physical_observation": True}


class UnavailableUnit(FakeOam):
    """A managed unit whose motion permission is temporarily gone (a property,
    so the flag can return) rather than absent for good."""
    device_family = "boxturtle"
    bay_count = 4
    hardware_snapshot_ready = True

    def __init__(self, idx, fps="fps1"):
        super().__init__(idx, fps)
        self.motion = False

    @property
    def motion_available(self):
        return self.motion


def mmu_objects(unit=None):
    return {
        "fps": [("fps", FakeFps("fps1"))],
        "klipper_mmu": [("klipper_mmu box", MMUObserver(unit))],
        "filament_group": [("filament_group T0",
                            FakeGroup([("box", 0)]))] if unit else [],
    }


class MMUDiscoveryTests(unittest.TestCase):
    def test_unconfigured_observer_is_not_an_openams_unit(self):
        manager = build_manager(mmu_objects(), ["fps", "klipper_mmu box"])
        self.assertEqual(manager.oams, {})
        self.assertIn("klipper_mmu box", manager.printer.loaded)

    def test_configured_unit_uses_adapter_metadata_and_group(self):
        unit = ReadOnlyUnit(7)
        manager = build_manager(mmu_objects(unit),
                                ["filament_group T0", "fps",
                                 "klipper_mmu box"])
        self.assertIs(manager.oams["box"], unit)
        self.assertEqual(manager.topo.idx_of("box"), 7)
        self.assertEqual(manager.topo.family_of("box"), "boxturtle")
        self.assertEqual(manager.topo.bays_of("box"), 4)
        self.assertEqual(manager.groups_by_lane["fps1"]["T0"], ((7, 0),))
        self.assertEqual(manager._topology_status()["oams"]["box"][
            "capabilities"], unit.capabilities)

    def test_legacy_unit_remains_available(self):
        objects = mmu_objects()
        objects["oams"] = [("oams legacy", FakeOam(1))]
        manager = build_manager(objects, ["fps", "oams legacy",
                                          "klipper_mmu box"])
        self.assertEqual(tuple(manager.oams), ("legacy",))
        self.assertEqual(manager.topo.idx_of("legacy"), 1)

    def test_name_and_index_conflicts_still_fail(self):
        for legacy_name, legacy_idx, message in (
                ("box", 1, "Duplicate unit name"),
                ("legacy", 7, "unit index")):
            with self.subTest(message=message):
                objects = mmu_objects(ReadOnlyUnit(7))
                objects["oams"] = [("oams " + legacy_name,
                                    FakeOam(legacy_idx))]
                with self.assertRaisesRegex(FakeError, message):
                    build_manager(objects, ["fps", "oams " + legacy_name,
                                            "klipper_mmu box"])

    def test_read_only_unit_refuses_only_its_own_commands(self):
        manager = build_manager(mmu_objects(ReadOnlyUnit(7)),
                                ["fps", "klipper_mmu box",
                                 "filament_group T0"])
        calls = []
        manager.runtime = SimpleNamespace(
            dispatch=lambda action: calls.append(action),
            tick=lambda: calls.append("tick"),
            request=lambda *args: self.fail("motion requested"),
            get_state=lambda: S.SystemState(
                lanes={"fps1": S.LaneState(
                    op=S.OP_LOADED, unit=(7, 0), group="T0")}))
        manager._startup_resync_pending = True
        manager.ready = False
        # The unit is read-only for good, so it no longer holds the manager
        # not-ready; its snapshot is real, so the resync completes.
        self.assertTrue(manager._finish_startup_resync_if_ready())
        self.assertTrue(manager.ready)
        manager.monitor_interval = 0.5
        manager.reactor = SimpleNamespace(monotonic=lambda: 10.0)
        manager._ext_monitor = lambda now: calls.append("hook")
        self.assertEqual(manager._monitor(10.0), 10.5)
        self.assertEqual(calls, [S.ClearErrors(keep_in_flight=True), "tick",
                                 "hook"])
        # OAMSM_CLEAR_ERRORS is not in this list: it addresses every unit, so
        # a read-only one is skipped (and named) rather than obeyed -- see
        # test_oams_manager_readonly_unit.
        for command, params in (
                (manager.cmd_LOAD_FILAMENT, {"GROUP": "T0"}),
                (manager.cmd_LOAD_TO_TOOLHEAD, {"GROUP": "T0"}),
                (manager.cmd_UNLOAD_FILAMENT, {"FPS": "fps1"}),
                (manager.cmd_UNLOAD_FROM_TOOLHEAD, {"FPS": "fps1"}),
                (manager.cmd_FOLLOWER, {"ENABLE": "1", "DIRECTION": "0"})):
            with self.subTest(command=command.__name__):
                before = len(calls)
                with self.assertRaisesRegex(FakeError, "motion unavailable"):
                    command(FakeGcmd(params))
                if command.__name__ in ("cmd_UNLOAD_FILAMENT",
                                        "cmd_UNLOAD_FROM_TOOLHEAD"):
                    self.assertEqual(calls[before:],
                                     [S.Follow("fps1", 0, S.FOLLOWER_REVERSE)])
                else:
                    self.assertEqual(len(calls), before)

    def test_temporarily_unavailable_unit_still_holds_the_resync(self):
        unit = UnavailableUnit(7)
        manager = build_manager(mmu_objects(unit),
                                ["fps", "klipper_mmu box",
                                 "filament_group T0"])
        calls = []
        manager.runtime = SimpleNamespace(
            dispatch=lambda action: calls.append(action),
            tick=lambda: calls.append("tick"),
            get_state=lambda: S.SystemState())
        manager._startup_resync_pending = True
        manager.ready = False
        manager.monitor_interval = 0.5
        manager.reactor = SimpleNamespace(monotonic=lambda: 10.0)
        manager._ext_monitor = lambda now: calls.append("hook")
        self.assertFalse(manager._finish_startup_resync_if_ready())
        self.assertEqual(manager._monitor(10.0), 10.5)
        self.assertFalse(manager.ready)
        self.assertEqual(calls, [])
        # The flag can return, so the unit gates its own commands meanwhile.
        with self.assertRaisesRegex(FakeError,
                                    "motion unavailable for box"):
            manager.cmd_LOAD_FILAMENT(FakeGcmd({"GROUP": "T0"}))
        unit.motion = True
        self.assertTrue(manager._finish_startup_resync_if_ready())
        self.assertTrue(manager.ready)


if __name__ == "__main__":
    unittest.main()
