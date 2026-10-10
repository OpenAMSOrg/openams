"""H2: what each bay holds of filament, and where the host publishes it.

``openams_spoolman`` works out a bay's material and working temperature and
pushes them to the host (C4 §7.4); the host keeps them in memory only
(C3 §4) and publishes them per bay, per lane and per group (C2 §4.2, §4.5).
"""

import unittest

from test_ams_ht_driver import make_driver
from test_oams_manager import (FakeError, FakeGcmd, FakeGroup,
                               LINK_GROUPS_FILE, S, build_manager,
                               read_file, single_lane_objects,
                               single_lane_sections)


def driver_with_bays(count=4):
    """A bare driver with the bay links and filament store ready for use."""
    driver = make_driver()
    driver.bay_count = count
    return driver


class BayFilamentStoreTest(unittest.TestCase):
    """The unit's own store (src/oams.py): memory only, next to the links."""

    def test_a_bare_driver_has_nothing_pushed(self):
        driver = driver_with_bays()
        for bay in range(4):
            self.assertIsNone(driver.bay_filament(bay))

    def test_set_then_read_and_clear(self):
        driver = driver_with_bays()
        driver.set_bay_filament(2, "PETG", 240, "table")
        self.assertEqual(driver.bay_filament(2),
                         {"material": "PETG", "temp_c": 240,
                          "source": "table"})
        self.assertIsNone(driver.bay_filament(1))
        driver.set_bay_filament(2, None, None, None)
        self.assertIsNone(driver.bay_filament(2))

    def test_the_store_hands_out_a_copy(self):
        driver = driver_with_bays()
        driver.set_bay_filament(0, "PLA", 210, "table")
        entry = driver.bay_filament(0)
        entry["temp_c"] = 999
        self.assertEqual(driver.bay_filament(0)["temp_c"], 210)

    def test_out_of_range_bays_read_as_empty(self):
        driver = driver_with_bays()
        self.assertIsNone(driver.bay_filament(9))
        self.assertIsNone(driver.bay_filament(-1))

    def test_a_bad_source_is_refused(self):
        driver = driver_with_bays()
        with self.assertRaises(ValueError):
            driver.set_bay_filament(0, "PLA", 210, "guess")

    def test_the_bay_status_publishes_it(self):
        driver = driver_with_bays()
        bays = {entry["index"]: entry
                for entry in driver.get_webhook_status()["bays"]}
        self.assertIsNone(bays[1]["filament"])
        driver.set_bay_filament(1, "ABS", 250, "spool")
        bays = {entry["index"]: entry
                for entry in driver.get_webhook_status()["bays"]}
        self.assertEqual(bays[1]["filament"],
                         {"material": "ABS", "temp_c": 250,
                          "source": "spool"})
        self.assertIsNone(bays[0]["filament"])


class SetBayFilamentCommandTest(unittest.TestCase):
    """OAMSM_SET_BAY_FILAMENT (C3 §4): allowed at any time, never persisted."""

    def _manager(self):
        return build_manager(single_lane_objects(), single_lane_sections(),
                             groups_file=LINK_GROUPS_FILE)

    def test_it_sets_material_temperature_and_source(self):
        mgr = self._manager()
        unit = mgr.oams["oams1"]
        gcmd = FakeGcmd({"OAMS": "oams1", "BAY": "1", "MATERIAL": "PETG",
                         "TEMP": "240", "SOURCE": "table"})
        mgr.cmd_SET_BAY_FILAMENT(gcmd)
        self.assertEqual(unit.bay_filament(1),
                         {"material": "PETG", "temp_c": 240,
                          "source": "table"})
        self.assertEqual(gcmd.responses[-1],
                         "Set oams1 Spool 2 to PETG at 240 C (table).")

    def test_a_material_alone_is_kept_with_no_temperature(self):
        mgr = self._manager()
        unit = mgr.oams["oams1"]
        mgr.cmd_SET_BAY_FILAMENT(
            FakeGcmd({"OAMS": "oams1", "BAY": "0", "MATERIAL": " PETG "}))
        self.assertEqual(unit.bay_filament(0),
                         {"material": "PETG", "temp_c": None,
                          "source": "spool"})

    def test_no_material_and_no_temperature_clears_the_bay(self):
        mgr = self._manager()
        unit = mgr.oams["oams1"]
        unit.set_bay_filament(0, "PETG", 240, "table")
        gcmd = FakeGcmd({"OAMS": "oams1", "BAY": "0"})
        mgr.cmd_SET_BAY_FILAMENT(gcmd)
        self.assertIsNone(unit.bay_filament(0))
        self.assertEqual(gcmd.responses[-1],
                         "Cleared the filament of oams1 Spool 1.")
        # TEMP=0 is the same spelling as no TEMP at all.
        unit.set_bay_filament(0, "PETG", 240, "table")
        mgr.cmd_SET_BAY_FILAMENT(FakeGcmd({"OAMS": "oams1", "BAY": "0",
                                           "TEMP": "0"}))
        self.assertIsNone(unit.bay_filament(0))

    def test_a_temperature_outside_the_range_is_refused(self):
        mgr = self._manager()
        unit = mgr.oams["oams1"]
        unit.set_bay_filament(0, "PETG", 240, "table")
        for temp in ("149", "401", "-5"):
            with self.assertRaises(FakeError) as caught:
                mgr.cmd_SET_BAY_FILAMENT(
                    FakeGcmd({"OAMS": "oams1", "BAY": "0", "TEMP": temp}))
            self.assertIn("150 and 400", str(caught.exception))
        self.assertEqual(unit.bay_filament(0)["temp_c"], 240)
        mgr.cmd_SET_BAY_FILAMENT(FakeGcmd({"OAMS": "oams1", "BAY": "0",
                                           "TEMP": "400"}))
        self.assertEqual(unit.bay_filament(0)["temp_c"], 400)

    def test_a_temperature_that_is_not_a_number_is_refused(self):
        mgr = self._manager()
        with self.assertRaises(FakeError):
            mgr.cmd_SET_BAY_FILAMENT(
                FakeGcmd({"OAMS": "oams1", "BAY": "0", "TEMP": "hot"}))

    def test_an_unknown_unit_or_a_bad_source_is_refused(self):
        mgr = self._manager()
        for params in ({"OAMS": "nope", "BAY": "0", "TEMP": "240"},
                       {"OAMS": "oams1", "BAY": "0", "TEMP": "240",
                        "SOURCE": "guess"}):
            with self.assertRaises(FakeError):
                mgr.cmd_SET_BAY_FILAMENT(FakeGcmd(params))
        self.assertIsNone(mgr.oams["oams1"].bay_filament(0))

    def test_it_is_allowed_while_the_lane_is_busy(self):
        mgr = self._manager()
        mgr.cmd_SET_BAY_FILAMENT(
            FakeGcmd({"OAMS": "oams1", "BAY": "0", "TEMP": "240"}))
        self.assertEqual(mgr.oams["oams1"].bay_filament(0)["temp_c"], 240)

    def test_it_never_writes_a_config_option(self):
        mgr = self._manager()
        before = read_file(mgr.openams_config_path)
        calls = []

        def forbidden(*args, **kwargs):
            calls.append(args)
            raise AssertionError("OAMSM_SET_BAY_FILAMENT must not persist")
        mgr.persist_config_option = forbidden
        mgr.remove_config_option = forbidden
        mgr.oams["oams1"].set_bay_link(2, 9, "tag")
        for params in ({"OAMS": "oams1", "BAY": "2", "MATERIAL": "ABS",
                        "TEMP": "250", "SOURCE": "spool"},
                       {"OAMS": "oams1", "BAY": "2"},
                       {"OAMS": "oams1", "BAY": "1", "TEMP": "230"}):
            mgr.cmd_SET_BAY_FILAMENT(FakeGcmd(params))
        self.assertEqual(calls, [])
        self.assertEqual(read_file(mgr.openams_config_path), before)
        self.assertEqual(mgr.oams["oams1"].bay_link(2), (9, "tag"))


class LinkChangeClearsTheBayTest(unittest.TestCase):
    """The pushed values described the old spool (C2 §4.5, C3 §4)."""

    def _manager(self):
        mgr = build_manager(single_lane_objects(), single_lane_sections(),
                            groups_file=LINK_GROUPS_FILE)
        mgr.oams["oams1"].set_bay_filament(1, "PETG", 240, "table")
        return mgr

    def test_linking_another_spool_clears_the_bay(self):
        mgr = self._manager()
        unit = mgr.oams["oams1"]
        unit.set_bay_link(1, 42, "tag")
        mgr.cmd_SET_BAY_SPOOL(FakeGcmd({"OAMS": "oams1", "BAY": "1",
                                        "SPOOL": "9", "SOURCE": "tag"}))
        self.assertEqual(unit.bay_link(1), (9, "tag"))
        self.assertIsNone(unit.bay_filament(1))

    def test_relinking_the_same_spool_keeps_the_values(self):
        mgr = self._manager()
        unit = mgr.oams["oams1"]
        unit.set_bay_link(1, 9, "tag")
        mgr.cmd_SET_BAY_SPOOL(FakeGcmd({"OAMS": "oams1", "BAY": "1",
                                        "SPOOL": "9", "SOURCE": "tag"}))
        self.assertEqual(unit.bay_filament(1)["temp_c"], 240)

    def test_clearing_the_link_clears_the_bay(self):
        mgr = self._manager()
        unit = mgr.oams["oams1"]
        unit.set_bay_link(1, 9, "tag")
        mgr.cmd_CLEAR_BAY_SPOOL(FakeGcmd({"OAMS": "oams1", "BAY": "1"}))
        self.assertIsNone(unit.bay_filament(1))

    def test_a_spool_moved_away_clears_the_bay_it_left(self):
        mgr = self._manager()
        unit = mgr.oams["oams1"]
        unit.set_bay_link(1, 9, "tag")
        mgr.cmd_SET_BAY_SPOOL(FakeGcmd({"OAMS": "oams1", "BAY": "2",
                                        "SPOOL": "9", "SOURCE": "manual"}))
        self.assertEqual(unit.bay_link(2), (9, "manual"))
        self.assertIsNone(unit.bay_filament(1))
        self.assertIsNone(unit.bay_filament(2))

    def test_a_refused_link_change_keeps_the_values(self):
        # The write is refused before anything is applied (C3 §4), so the
        # bay still holds what it held.
        mgr = build_manager(single_lane_objects(), single_lane_sections())
        unit = mgr.oams["oams1"]
        unit.set_bay_link(1, 9, "tag")
        unit.set_bay_filament(1, "PETG", 240, "table")
        with self.assertRaises(Exception):
            mgr.cmd_SET_BAY_SPOOL(
                FakeGcmd({"OAMS": "oams1", "BAY": "1", "SPOOL": "42"}))
        self.assertEqual(unit.bay_filament(1)["temp_c"], 240)


class PublishedFilamentTest(unittest.TestCase):
    """What the v2 status adds: the lane's bay and the group's next bay."""

    def _manager(self, groups=None, ready=None):
        objects = single_lane_objects()
        if groups is not None:
            objects["filament_group"] = [
                ("filament_group %s" % name, FakeGroup(bays))
                for name, bays in groups]
        mgr = build_manager(objects, single_lane_sections(),
                            groups_file=LINK_GROUPS_FILE)
        mgr.idle_timeout = None       # _is_printing: not printing
        mgr.reload_before = 0.0       # the rest of the world build's inputs
        for fps in mgr.fpss.values():
            fps.extruder_name = "extruder"
        if ready is not None:
            mgr.oams["oams1"].f1s_hes_value = list(ready)
        return mgr

    def test_a_loaded_lane_publishes_its_bay_temperature(self):
        mgr = self._manager()
        mgr.oams["oams1"].set_bay_filament(1, "PETG", 240, "table")
        lane = S.LaneState(op=S.OP_LOADED, group="T1", unit=(1, 1))
        status = mgr._lane_status("fps1", lane)
        self.assertEqual(status["filament_temp_c"], 240)

    def test_an_unloaded_lane_publishes_nothing(self):
        mgr = self._manager()
        mgr.oams["oams1"].set_bay_filament(0, "PLA", 210, "table")
        lane = S.LaneState(op=S.OP_UNLOADED)
        self.assertIsNone(mgr._lane_status("fps1", lane)["filament_temp_c"])

    def test_a_loaded_bay_with_no_temperature_publishes_null(self):
        mgr = self._manager()
        lane = S.LaneState(op=S.OP_LOADED, group="T1", unit=(1, 1))
        self.assertIsNone(mgr._lane_status("fps1", lane)["filament_temp_c"])

    def test_a_lane_on_a_unit_without_bays_publishes_null(self):
        mgr = self._manager()
        lane = S.LaneState(op=S.OP_LOADED, group="T1", unit=(9, 0))
        self.assertIsNone(mgr._lane_status("fps1", lane)["filament_temp_c"])

    def test_a_group_publishes_its_next_bay_and_its_temperature(self):
        mgr = self._manager(groups=[("T0", [("oams1", 0)])],
                            ready=[1, 0, 0, 0])
        mgr.oams["oams1"].set_bay_filament(0, "PLA", 210, "table")
        groups = dict((g["name"], g) for g in mgr._ui_groups())
        self.assertEqual(groups["T0"]["next_bay"], [1, 0])
        self.assertEqual(groups["T0"]["next_filament_temp_c"], 210)

    def test_an_empty_group_has_no_next_bay(self):
        mgr = self._manager(groups=[("T0", [("oams1", 0)])],
                            ready=[0, 0, 0, 0])
        groups = dict((g["name"], g) for g in mgr._ui_groups())
        self.assertIsNone(groups["T0"]["next_bay"])
        self.assertIsNone(groups["T0"]["next_filament_temp_c"])

    def test_next_bay_is_the_bay_a_load_would_take(self):
        # Two bays in the group, only the second ready: the load takes the
        # second, and the published next_bay agrees.
        mgr = self._manager(groups=[("T0", [("oams1", 0), ("oams1", 1)])],
                            ready=[0, 1, 0, 0])
        mgr.oams["oams1"].set_bay_filament(1, "ABS", 250, "spool")
        groups = dict((g["name"], g) for g in mgr._ui_groups())
        self.assertEqual(groups["T0"]["next_bay"], [1, 1])
        self.assertEqual(groups["T0"]["next_filament_temp_c"], 250)
        world = mgr.build_world(0.0)
        after, _ = S.reduce(S.SystemState(lanes={"fps1": S.LaneState()}),
                            S.Load("fps1", "T0"), world, 1.0)
        self.assertEqual(list(after.lanes["fps1"].unit),
                         groups["T0"]["next_bay"])

    def test_next_bay_survives_a_status_that_cannot_build_the_world(self):
        # A manager whose topology mirrors are missing (status fixtures)
        # cannot build a world; the field is null rather than the status
        # failing.
        mgr = self._manager(groups=[("T0", [("oams1", 0)])],
                            ready=[1, 0, 0, 0])
        del mgr.reload_before
        groups = dict((g["name"], g) for g in mgr._ui_groups())
        self.assertIsNone(groups["T0"]["next_bay"])
        self.assertIsNone(groups["T0"]["next_filament_temp_c"])


if __name__ == "__main__":
    unittest.main()