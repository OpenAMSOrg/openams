"""The LCD "AMS status" screen's data (C2 §6a, C3 §4).

``openams_spoolman`` pushes each bay's material, working temperature and,
now, the spool's remaining percent (``OAMSM_SET_BAY_FILAMENT ... REMAINING=``);
the host keeps them in memory and publishes material and remaining only in
the ``openams_screen`` object, with the units the screen pages through and
the unit it shows (the owner's rules, 2026-11-11). ``OAMSM_SCREEN`` pins a
unit.
"""

import importlib
import unittest

from test_ams_ht_driver import make_driver
from test_oams_manager import (FakeError, FakeFollower, FakeFps, FakeGcmd,
                               FakeGroup, FakeOam, FakePrinter, S,
                               build_manager)

screen_mod = importlib.import_module("oamspkg.openams_screen")
pick_active = screen_mod.pick_active


def two_unit_manager():
    """voron350's layout: an AMS 1 (ams1, T0-T3) and an AMS 2 Pro (ams2,
    T4-T7) on one lane, plus an inline follower (no bays to show)."""
    objects = {
        "fps": [("fps", FakeFps("fps1"))],
        "oams": [("oams ams1", FakeOam(0)), ("oams ams2", FakeOam(1))],
        "follower": [("follower belay", FakeFollower(9))],
        "filament_group": [
            ("filament_group T%d" % (u * 4 + b),
             FakeGroup([("ams%d" % (u + 1), b)]))
            for u in range(2) for b in range(4)],
    }
    sections = (["fps", "oams ams1", "oams ams2", "follower belay"]
                + ["filament_group T%d" % i for i in range(8)]
                + ["oams_manager"])
    groups = "".join("[filament_group T%d]\ngroup: ams%d-%d\n\n"
                     % (u * 4 + b, u + 1, b)
                     for u in range(2) for b in range(4))
    return build_manager(objects, sections, groups_file=groups)


class PrintStats:
    def __init__(self, state="standby"):
        self.state = state

    def get_status(self, eventtime):
        return {"state": self.state}


class PrinterWithStats(FakePrinter):
    """The fake printer, plus print_stats the way Klipper looks it up."""

    def __init__(self, printer, stats):
        self.__dict__.update(printer.__dict__)
        self.stats = stats

    def lookup_object(self, name, *default):
        if name == "print_stats":
            return self.stats
        return FakePrinter.lookup_object(self, name)


def with_print(mgr, state):
    stats = PrintStats(state)
    mgr.printer = PrinterWithStats(mgr.printer, stats)
    return stats


def set_lane(mgr, **values):
    mgr.runtime._sys.lanes["fps1"] = S.LaneState(**values)


class PickActiveTest(unittest.TestCase):
    """The rules alone (src/openams_screen.py pick_active)."""

    UNITS = ["ams1", "ams2", "ams3"]

    def pick(self, pin=None, printing=False, loaded=(), faulted=(), busy=(),
             last=None, page=0):
        return pick_active(self.UNITS, pin, printing, set(loaded),
                           set(faulted), set(busy), last, page)

    def test_idle_pages_through_the_units(self):
        self.assertEqual([self.pick(page=p) for p in range(4)],
                         ["ams1", "ams2", "ams3", "ams1"])

    def test_outside_a_print_a_fault_then_a_load_holds_it(self):
        self.assertEqual(self.pick(busy=["ams3"], page=1), "ams3")
        self.assertEqual(self.pick(faulted=["ams2"], busy=["ams3"]), "ams2")
        # A loaded bay alone does not stop the paging outside a print.
        self.assertEqual(self.pick(loaded=["ams3"], page=0), "ams1")

    def test_a_print_stays_on_the_loaded_unit(self):
        self.assertEqual(self.pick(printing=True, loaded=["ams2"],
                                   faulted=["ams3"], busy=["ams1"], page=2),
                         "ams2")

    def test_mid_toolchange_a_print_follows_the_fault_or_the_load(self):
        self.assertEqual(self.pick(printing=True, faulted=["ams3"],
                                   busy=["ams1"]), "ams3")
        self.assertEqual(self.pick(printing=True, busy=["ams1"]), "ams1")

    def test_a_print_never_pages(self):
        for page in range(5):
            self.assertEqual(self.pick(printing=True, last="ams3",
                                       page=page), "ams3")
        self.assertEqual(self.pick(printing=True, page=4), "ams1")

    def test_a_pin_overrides_everything(self):
        self.assertEqual(self.pick(pin="ams3", printing=True,
                                   loaded=["ams1"]), "ams3")
        # A pin naming no current unit is ignored.
        self.assertEqual(self.pick(pin="gone", page=1), "ams2")

    def test_no_units_no_active(self):
        self.assertIsNone(pick_active([], None, False, set(), set(), set(),
                                      None, 0))


class UnitStoreTest(unittest.TestCase):
    """The unit keeps the remaining percent beside its filament (src/oams.py)
    and publishes it only through openams_screen."""

    def driver(self):
        driver = make_driver()
        driver.bay_count = 4
        return driver

    def test_set_read_and_clear(self):
        driver = self.driver()
        self.assertIsNone(driver.bay_remaining_pct(1))
        driver.set_bay_filament(1, "PETG", 240, "table", remaining_pct=62)
        self.assertEqual(driver.bay_remaining_pct(1), 62)
        self.assertEqual(driver.bay_filament(1),
                         {"material": "PETG", "temp_c": 240,
                          "source": "table"})
        driver.set_bay_filament(1, None, None, None)
        self.assertIsNone(driver.bay_remaining_pct(1))
        self.assertIsNone(driver.bay_filament(1))

    def test_a_remaining_percent_alone_is_kept(self):
        driver = self.driver()
        driver.set_bay_filament(2, None, None, None, remaining_pct=30)
        self.assertEqual(driver.bay_remaining_pct(2), 30)
        self.assertIsNone(driver.bay_filament(2))

    def test_out_of_range_values_are_refused_and_change_nothing(self):
        driver = self.driver()
        driver.set_bay_filament(0, "PLA", 210, "table", remaining_pct=40)
        for bad in (-1, 101):
            with self.assertRaises(ValueError):
                driver.set_bay_filament(0, "PLA", 210, "table",
                                        remaining_pct=bad)
        with self.assertRaises(ValueError):
            driver.set_bay_filament(0, "PLA", 210, "guess", remaining_pct=9)
        self.assertEqual(driver.bay_remaining_pct(0), 40)

    def test_the_unit_status_does_not_carry_it(self):
        driver = self.driver()
        driver.set_bay_filament(1, "ABS", 250, "spool", remaining_pct=55)
        bays = {entry["index"]: entry
                for entry in driver.get_webhook_status()["bays"]}
        self.assertEqual(bays[1]["filament"],
                         {"material": "ABS", "temp_c": 250,
                          "source": "spool"})
        self.assertNotIn(55, [v for e in bays.values() for v in e.values()
                              if isinstance(v, int)
                              and not isinstance(v, bool)])


class RemainingArgumentTest(unittest.TestCase):
    """OAMSM_SET_BAY_FILAMENT REMAINING= (C3 §4)."""

    def setUp(self):
        self.mgr = two_unit_manager()
        self.unit = self.mgr.oams["ams2"]

    def run_cmd(self, **params):
        gcmd = FakeGcmd(dict({"OAMS": "ams2", "BAY": "1"}, **params))
        self.mgr.cmd_SET_BAY_FILAMENT(gcmd)
        return gcmd.responses[-1]

    def test_it_is_kept_with_the_material_and_temperature(self):
        said = self.run_cmd(MATERIAL="PETG", TEMP="240", SOURCE="table",
                            REMAINING="62")
        self.assertEqual(self.unit.bay_remaining_pct(1), 62)
        self.assertEqual(self.unit.bay_filament(1)["temp_c"], 240)
        self.assertEqual(said, "Set ams2 Spool 2 to PETG at 240 C (table), "
                               "62% left.")

    def test_an_older_component_sends_none_and_it_is_unknown(self):
        self.unit.set_bay_filament(1, "PETG", 240, "table", remaining_pct=62)
        said = self.run_cmd(MATERIAL="PETG", TEMP="240", SOURCE="table")
        self.assertIsNone(self.unit.bay_remaining_pct(1))
        self.assertEqual(said, "Set ams2 Spool 2 to PETG at 240 C (table).")

    def test_a_remaining_percent_alone_is_not_a_clear(self):
        said = self.run_cmd(REMAINING="30")
        self.assertEqual(self.unit.bay_remaining_pct(1), 30)
        self.assertEqual(said, "Set ams2 Spool 2 to 30% left.")

    def test_an_empty_value_is_unknown(self):
        self.run_cmd(MATERIAL="ASA", REMAINING="")
        self.assertIsNone(self.unit.bay_remaining_pct(1))
        self.assertEqual(self.unit.bay_filament(1)["material"], "ASA")

    def test_bad_values_are_refused(self):
        self.unit.set_bay_filament(1, "PETG", 240, "table", remaining_pct=62)
        for raw, words in (("101", "between 0 and 100"),
                           ("-1", "between 0 and 100"),
                           ("lots", "integer")):
            with self.assertRaises(FakeError) as caught:
                self.run_cmd(MATERIAL="PETG", REMAINING=raw)
            self.assertIn(words, str(caught.exception))
        self.assertEqual(self.unit.bay_remaining_pct(1), 62)

    def test_the_bare_command_clears_it(self):
        self.unit.set_bay_filament(1, "PETG", 240, "table", remaining_pct=62)
        said = self.run_cmd()
        self.assertIsNone(self.unit.bay_remaining_pct(1))
        self.assertEqual(said, "Cleared the filament of ams2 Spool 2.")

    def test_a_link_change_clears_it(self):
        # The fake follower keeps no spool links; the real one has none.
        self.mgr.oams["belay"].bay_link = lambda bay: (None, "none")
        self.unit.set_bay_link(1, 42, "tag")
        self.unit.set_bay_filament(1, "PETG", 240, "table", remaining_pct=62)
        self.mgr.persist_config_option = lambda *a, **k: None
        self.mgr.remove_config_option = lambda *a, **k: None
        self.mgr.cmd_SET_BAY_SPOOL(FakeGcmd({"OAMS": "ams2", "BAY": "1",
                                             "SPOOL": "9", "SOURCE": "tag"}))
        self.assertIsNone(self.unit.bay_remaining_pct(1))

    def test_it_is_never_persisted(self):
        def forbidden(*args, **kwargs):
            raise AssertionError("REMAINING must not persist")
        self.mgr.persist_config_option = forbidden
        self.mgr.remove_config_option = forbidden
        self.run_cmd(MATERIAL="PLA", TEMP="210", REMAINING="8")
        self.assertEqual(self.unit.bay_remaining_pct(1), 8)


class ScreenObjectTest(unittest.TestCase):
    """The openams_screen object (C2 §6a)."""

    def setUp(self):
        self.mgr = two_unit_manager()
        self.screen = self.mgr._screen_object()
        self.now = 100.0

    def status(self, step=1.0):
        self.now += step
        return self.screen.get_status(self.now)

    def test_units_leave_the_follower_out(self):
        status = self.status()
        self.assertEqual(status["v"], 1)
        self.assertEqual(status["units"], ["ams1", "ams2"])
        self.assertEqual(sorted(status["bays"]), ["ams1", "ams2"])
        self.assertEqual(len(status["bays"]["ams2"]), 4)

    def test_bays_carry_the_pushed_material_and_remaining(self):
        self.mgr.oams["ams2"].set_bay_filament(1, "PETG", 240, "table",
                                               remaining_pct=62)
        self.mgr.oams["ams2"].set_bay_filament(3, None, None, None,
                                               remaining_pct=8)
        bays = self.status()["bays"]["ams2"]
        self.assertEqual(bays[1], {"m": "PETG", "r": 62})
        self.assertEqual(bays[3], {"m": None, "r": 8})
        self.assertEqual(bays[0], {"m": None, "r": None})

    def test_an_unlinked_bay_falls_back_to_its_tag_material(self):
        unit = self.mgr.oams["ams1"]
        unit.tags = {
            0: {"format": "unit_summary", "summary": {"material": "ABS"}},
            1: {"format": "bambu", "bambu": {"type": "PLA"}},
            2: {"format": "openspool", "openspool": {"type": "PETG"}},
            3: {"format": "unknown", "bambu": None}}
        unit.set_bay_filament(1, "PLA-CF", 220, "table")
        bays = self.status()["bays"]["ams1"]
        self.assertEqual([b["m"] for b in bays], ["ABS", "PLA-CF", "PETG",
                                                  None])

    def test_it_hands_back_the_same_object_until_something_changes(self):
        first = self.status()
        self.assertIs(self.status(), first)
        self.mgr.oams["ams1"].set_bay_filament(0, "PLA", 210, "table",
                                               remaining_pct=78)
        changed = self.status()
        self.assertIsNot(changed, first)
        self.assertEqual(changed["bays"]["ams1"][0], {"m": "PLA", "r": 78})
        self.assertIs(self.status(), changed)

    def test_reads_inside_the_interval_do_not_rebuild(self):
        first = self.status()
        self.mgr.oams["ams1"].set_bay_filament(0, "PLA", 210, "table")
        self.assertIs(self.status(step=0.1), first)
        self.assertIsNot(self.status(step=0.2), first)

    def test_idle_it_pages_every_five_seconds(self):
        shown = []
        for step in range(4):
            self.now = 10.0 + 5.0 * step
            shown.append(self.screen.get_status(self.now)["active"])
        self.assertEqual(shown, ["ams1", "ams2", "ams1", "ams2"])

    def test_printing_it_stays_on_the_unit_holding_the_loaded_bay(self):
        with_print(self.mgr, "printing")
        set_lane(self.mgr, op=S.OP_LOADED, unit=(1, 1), group="T5")
        for step in range(4):
            self.now = 10.0 + 5.0 * step
            self.assertEqual(self.screen.get_status(self.now)["active"],
                             "ams2")

    def test_a_paused_print_counts_as_printing(self):
        with_print(self.mgr, "paused")
        set_lane(self.mgr, op=S.OP_LOADED, unit=(1, 2), group="T6")
        self.now = 10.0
        self.assertEqual(self.screen.get_status(self.now)["active"], "ams2")

    def test_outside_a_print_a_loaded_bay_does_not_stop_the_paging(self):
        with_print(self.mgr, "standby")
        set_lane(self.mgr, op=S.OP_LOADED, unit=(1, 1), group="T5")
        self.now = 10.0
        self.assertEqual(self.screen.get_status(self.now)["active"], "ams1")

    def test_a_load_holds_the_screen_on_its_unit(self):
        set_lane(self.mgr, op=S.OP_LOADING, unit=(1, 1), group="T5")
        for step in range(3):
            self.now = 10.0 + 5.0 * step
            self.assertEqual(self.screen.get_status(self.now)["active"],
                             "ams2")

    def test_an_unload_holds_its_unit_from_its_first_step(self):
        # Home, heat and the cut come before the AMS moves (op still
        # LOADED): outside a print the screen must not page away meanwhile.
        with_print(self.mgr, "standby")
        set_lane(self.mgr, op=S.OP_LOADED, unit=(1, 1), group="T5",
                 stage_plan=("home", "heat", "cut"), stage_index=1)
        for step in range(3):
            self.now = 10.0 + 5.0 * step
            self.assertEqual(self.screen.get_status(self.now)["active"],
                             "ams2")

    def test_a_load_into_an_empty_lane_holds_the_unit_of_its_group(self):
        # No bay is picked while it homes and heats: the plan's GROUP=
        # names the tool, and the unit holding it.
        with_print(self.mgr, "standby")
        set_lane(self.mgr, op=S.OP_UNLOADED, stage_group="T5",
                 stage_plan=("home", "heat", "feed"), stage_index=1)
        for step in range(3):
            self.now = 10.0 + 5.0 * step
            status = self.screen.get_status(self.now)
            self.assertEqual((status["active"], status["tool"]),
                             ("ams2", "T5"))

    def test_the_tool_is_the_loaded_group_during_an_unload(self):
        set_lane(self.mgr, op=S.OP_LOADED, unit=(1, 1), group="T5",
                 stage_plan=("heat", "cut"), stage_index=0)
        self.now = 10.0
        self.assertEqual(self.screen.get_status(self.now)["tool"], "T5")

    def test_no_tool_without_a_running_plan(self):
        set_lane(self.mgr, op=S.OP_LOADED, unit=(1, 1), group="T5")
        self.now = 10.0
        self.assertIsNone(self.screen.get_status(self.now)["tool"])

    def test_a_failed_load_holds_the_unit_whose_bay_failed(self):
        # Both units of the lane carry the failure; only ams2 names the bay.
        set_lane(self.mgr, op=S.OP_UNLOADED, message="load failed",
                 fault_unit=(1, 1), fault_text="feed failed")
        self.now = 10.0
        self.assertEqual(self.screen.get_status(self.now)["active"], "ams2")

    def test_the_pin_command(self):
        gcmd = FakeGcmd({"UNIT": "ams2"})
        self.mgr.cmd_SCREEN(gcmd)
        self.assertEqual(gcmd.responses[-1],
                         "AMS status screen: shows ams2 (pinned).")
        status = self.status()
        self.assertEqual((status["active"], status["pin"]), ("ams2", "ams2"))
        self.now = 15.0
        self.assertEqual(self.screen.get_status(self.now)["active"], "ams2")
        gcmd = FakeGcmd({"UNIT": "auto"})
        self.mgr.cmd_SCREEN(gcmd)
        self.assertIn("follows the active unit", gcmd.responses[-1])
        self.assertIsNone(self.status()["pin"])

    def test_the_pin_command_refuses_a_unit_it_cannot_show(self):
        for name in ("nope", "belay"):
            with self.assertRaises(FakeError) as caught:
                self.mgr.cmd_SCREEN(FakeGcmd({"UNIT": name}))
            self.assertIn("units: ams1, ams2", str(caught.exception))
        self.assertIsNone(self.screen.pin)

    def test_the_pin_command_without_a_unit_reports(self):
        gcmd = FakeGcmd({})
        self.mgr.cmd_SCREEN(gcmd)
        self.assertTrue(gcmd.responses[-1].startswith(
            "AMS status screen: follows the active unit (now "))

    def test_it_matches_the_contract_schema(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema not installed")
        import json
        import os
        schema = os.path.join(os.path.dirname(__file__), "..", "docs",
                              "contracts", "schemas",
                              "openams_screen_v1.schema.json")
        with open(schema) as f:
            validator = jsonschema.Draft202012Validator(json.load(f))
        self.mgr.oams["ams2"].set_bay_filament(1, "PETG", 240, "table",
                                               remaining_pct=62)
        self.mgr.cmd_SCREEN(FakeGcmd({"UNIT": "ams2"}))
        errors = [e.message for e in validator.iter_errors(self.status())]
        self.assertEqual(errors, [])

    def test_spool_data_stays_off_openams_ui(self):
        # The owner's decision (C2 4/7 stands): material and remaining are
        # published here, never in the openams_ui projection's bays.
        self.mgr.oams["ams2"].set_bay_filament(1, "PETG", 240, "table",
                                               remaining_pct=62)
        self.assertEqual(self.status()["bays"]["ams2"][1]["r"], 62)
        ui = importlib.import_module("oamspkg.openams_ui")
        self.assertNotIn("r", ui._BAY_ACTION_CODES)


if __name__ == "__main__":
    unittest.main()
