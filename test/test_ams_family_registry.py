"""One family registry: klipper-mmu's devices and the C2 schemas must agree.

`src/ams_family.py` is the single registry. This test holds it to that:

* every name in klipper-mmu's `_OPENAMS_DEVICE_IDS` normalizes to a family
  with a profile, with the display name, lane count, selector kind and
  capability defaults its own README and Kconfig give it;
* the AMS profiles are unchanged by any of that;
* the C2 schemas' `family` enums are exactly the registry plus
  `inline_follower` where that is a topology kind.
"""
import json
import os
import re
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))

import ams_family as F  # noqa: E402

SCHEMAS = os.path.join(REPO, "docs", "contracts", "schemas")
UI_SCHEMA = os.path.join(SCHEMAS, "openams_ui_v1.schema.json")
MANAGER_SCHEMA = os.path.join(SCHEMAS, "oams_manager_status_v2.schema.json")
MMU_HOST = os.path.join(REPO, "klipper_mmu", "host", "__init__.py")

# The three AMS families, unchanged by this registry.
AMS_FAMILIES = ("ams1", "ams_ht", "ams2")


def openams_device_ids():
    with open(MMU_HOST, encoding="utf-8") as handle:
        match = re.search(r"_OPENAMS_DEVICE_IDS = (\{.*?\})", handle.read())
    return eval(match.group(1))          # noqa: S307 - a literal in our tree


def load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


class KlipperMmuDeviceTests(unittest.TestCase):
    """Every _OPENAMS_DEVICE_IDS name is a family with a profile."""

    def test_every_device_id_normalizes_to_a_profile(self):
        missing = []
        for name in sorted(openams_device_ids()):
            family = F.normalize_family(name)
            if not family or not F.profile(family):
                missing.append(name)
        self.assertEqual(missing, [])

    def test_the_names_are_accepted_exactly_as_klipper_mmu_spells_them(self):
        for name in openams_device_ids():
            self.assertEqual(F.normalize_family(name), name)
            self.assertEqual(F.normalize_family(name.upper()), name)
            self.assertEqual(F.normalize_family(name.replace("_", " ")), name)

    def test_registry_covers_the_device_set_and_nothing_else(self):
        self.assertEqual(set(F.KLIPPER_MMU_FAMILIES),
                         set(openams_device_ids()))
        self.assertEqual(set(F.FAMILIES),
                         set(AMS_FAMILIES) | set(F.KLIPPER_MMU_FAMILIES))

    def test_every_device_has_a_display_name_and_a_selector_kind(self):
        for family in F.KLIPPER_MMU_FAMILIES:
            profile = F.profile(family)
            self.assertTrue(profile["display_name"].strip(), family)
            self.assertIn(profile["selector_kind"], F.SELECTOR_KINDS, family)
            self.assertTrue(
                profile["selector_kind_firmware"].startswith(
                    "KLIPPER_MMU_SELECTOR_KIND_"), family)

    def test_lane_counts_are_a_range_containing_the_default(self):
        for family in F.KLIPPER_MMU_FAMILIES:
            profile = F.profile(family)
            self.assertGreaterEqual(profile["bays_min"], 1, family)
            self.assertLessEqual(profile["bays_max"], 12, family)
            self.assertLessEqual(profile["bays_min"], profile["bays"], family)
            self.assertLessEqual(profile["bays"], profile["bays_max"], family)
            self.assertEqual(profile["spool_drive_count"], profile["bays"],
                             family)

    def test_known_device_shapes(self):
        # klipper_mmu/README.md "What it drives", the same table as
        # site/devices.html, and mcu/Kconfig KLIPPER_MMU_LANE_COUNT.
        expected = {
            # family: (display name, default lanes, low, high, selector kind)
            "boxturtle": ("BoxTurtle", 4, 4, 8, "feeder_per_lane"),
            "nightowl": ("NightOwl", 2, 2, 4, "feeder_per_lane"),
            "emu": ("EMU", 5, 5, 5, "feeder_per_lane"),
            "kms": ("KMS", 4, 4, 4, "feeder_per_lane"),
            "qidi_box": ("QIDI Box", 4, 4, 4, "feeder_per_lane"),
            "angry_beaver": ("Angry Beaver", 4, 4, 4, "feeder_per_lane"),
            "3ms": ("3MS", 4, 4, 4, "feeder_per_lane"),
            "quattrobox_1_0": ("QuattroBox 1.0", 4, 4, 4, "feeder_per_lane"),
            "quattrobox_1_1": ("QuattroBox 1.1", 4, 4, 4, "feeder_per_lane"),
            "quattrobox_2": ("QuattroBox 2", 4, 4, 4, "feeder_per_lane"),
            "custom": ("Custom", 4, 1, 12, "feeder_per_lane"),
            "ercf_1_1": ("ERCF 1.1", 9, 9, 9, "linear"),
            "ercf_2_0": ("ERCF 2.0", 8, 8, 8, "linear"),
            "ercf_3_0": ("ERCF 3.0", 8, 8, 8, "linear"),
            "tradrack": ("Tradrack", 10, 10, 10, "linear"),
            "3d_chameleon": ("3D Chameleon", 4, 4, 4, "rotary"),
            "htlf": ("HTLF", 4, 4, 4, "rotary"),
            "mmx6": ("MMX6", 6, 6, 6, "rotary"),
            "mmx": ("MMX", 4, 4, 4, "servo"),
            "low_rider": ("Low Rider", 6, 6, 6, "servo"),
            "pico_mmu": ("Pico MMU", 4, 4, 4, "servo"),
            "btt_vivid": ("BTT ViViD", 4, 4, 4, "indexed"),
            "claymore": ("Claymore", 4, 4, 4, "indexed"),
        }
        self.assertEqual(set(expected), set(F.KLIPPER_MMU_FAMILIES))
        for family, shape in expected.items():
            profile = F.profile(family)
            self.assertEqual(
                (profile["display_name"], profile["bays"],
                 profile["bays_min"], profile["bays_max"],
                 profile["selector_kind"]), shape, family)

    def test_capability_defaults_are_no_dryer_no_rfid_and_owned_motion(self):
        for family in F.KLIPPER_MMU_FAMILIES:
            profile = F.profile(family)
            for key in ("dryer", "spool_rotation_during_drying",
                        "rfid", "rfid_uid_inventory",
                        "ptfe_calibration", "hub_hes_calibration",
                        "host_sensor_configuration", "runtime_pid_tuning",
                        "logger_control", "error_led_control"):
                self.assertFalse(profile[key], "%s: %s" % (family, key))
            for key in ("heater_count", "fan_count", "vent_count",
                        "dryer_zone_count", "dryer_module_count",
                        "dryer_temperature_sensor_count"):
                self.assertEqual(profile[key], 0, "%s: %s" % (family, key))
            self.assertIsNone(profile["dryer_target_min_c"], family)
            self.assertIsNone(profile["dryer_target_max_c"], family)
            self.assertIsNone(profile["rfid_reader_scope"], family)
            self.assertIsNone(profile["rfid_material_metadata"], family)
            # Motion as klipper-mmu reports it: the MCU owns it.
            self.assertTrue(profile["device_owned_motion_control"], family)
            self.assertTrue(profile["autonomous_follower"], family)
            self.assertTrue(profile["normal_motion"], family)
            self.assertIsNone(F.motion_block_reason(profile), family)

    def test_only_the_linear_machines_wire_an_encoder(self):
        for family in F.KLIPPER_MMU_FAMILIES:
            self.assertEqual(
                F.profile(family)["encoder"],
                F.profile(family)["selector_kind"] == "linear", family)

    def test_resolve_capabilities_keeps_the_explicit_family(self):
        caps, warnings = F.resolve_capabilities(
            "boxturtle", {"BOARD_NAME": "klipper_mmu_boxturtle"})
        self.assertEqual(caps["family"], "boxturtle")
        self.assertEqual(caps["display_name"], "BoxTurtle")
        self.assertEqual(caps["bays"], 4)
        self.assertEqual(warnings, [])

    def test_an_unknown_name_is_still_rejected(self):
        with self.assertRaises(ValueError):
            F.normalize_family("not_a_device")


class AmsProfileTests(unittest.TestCase):
    """The registry must not have changed the three AMS profiles."""

    EXPECTED = {
        "ams1": ("AMS 1", 4, 0, False),
        "ams_ht": ("AMS HT", 1, 1, True),
        "ams2": ("AMS 2 Pro", 4, 2, True),
    }

    def test_shapes_are_unchanged(self):
        for family, shape in self.EXPECTED.items():
            profile = F.profile(family)
            self.assertEqual(
                (profile["family"], profile["display_name"], profile["bays"],
                 profile["heater_count"], profile["dryer"]),
                (family,) + shape, family)
            self.assertIsInstance(profile["rfid"], bool, family)
            self.assertEqual(profile["schema_version"], F.SCHEMA_VERSION)

    def test_ams_profiles_carry_no_klipper_mmu_keys(self):
        for family in AMS_FAMILIES:
            profile = F.profile(family)
            for key in ("selector_kind", "bays_min", "bays_max", "encoder"):
                self.assertNotIn(key, profile, "%s: %s" % (family, key))

    def test_ams_aliases_still_resolve(self):
        for value, expected in (("AMS", "ams1"), ("ams_1", "ams1"),
                                ("ams2_pro", "ams2"), ("AMS 2 Pro", "ams2"),
                                ("ht", "ams_ht"), ("auto", "auto")):
            self.assertEqual(F.normalize_family(value), expected)

    def test_auto_still_resolves_to_the_ams1_shape(self):
        self.assertEqual(F.profile("auto")["family"], "ams1")


class SchemaEnumTests(unittest.TestCase):
    """The C2 schemas' family enums are CHECKED against the registry.

    They are not generated from it: a schema is a published artifact, and
    generating it would make a registry edit a silent contract change. The
    check lives here instead, so a new family without a schema edit fails.
    """

    def setUp(self):
        self.ui = load(UI_SCHEMA)
        self.manager = load(MANAGER_SCHEMA)

    def test_openams_ui_family_enum_is_the_registry(self):
        enum = self.ui["properties"]["family"]["enum"]
        self.assertEqual(set(enum), set(F.FAMILIES) | {"inline_follower"})
        self.assertEqual(len(enum), len(set(enum)))

    def test_topology_family_enum_is_the_registry_plus_the_follower(self):
        unit = self.manager["$defs"]["topology"]["properties"]["oams"][
            "additionalProperties"]["properties"]
        enum = unit["family"]["enum"]
        self.assertEqual(set(enum), set(F.FAMILIES) | {"inline_follower"})

    def test_device_family_enum_is_the_registry_without_the_follower(self):
        enum = self.manager["$defs"]["device"]["properties"]["family"]["enum"]
        self.assertEqual(set(enum), set(F.FAMILIES))


class BayCountTests(unittest.TestCase):
    """Bay counts up to 12 validate; every bay container allows 16."""

    def test_openams_ui_allows_sixteen_bays(self):
        bays = load(UI_SCHEMA)["properties"]["bays"]
        self.assertEqual(bays["maxItems"], 16)

    def test_topology_bay_count_allows_sixteen(self):
        unit = load(MANAGER_SCHEMA)["$defs"]["topology"]["properties"]["oams"][
            "additionalProperties"]["properties"]
        self.assertEqual(unit["bays"]["minimum"], 1)
        self.assertEqual(unit["bays"]["maximum"], 16)

    def test_a_twelve_lane_unit_validates_against_both_schemas(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest('jsonschema is not installed (the tool-suites job runs this)')
        manager = load(MANAGER_SCHEMA)
        ui = load(UI_SCHEMA)
        widest = max(F.profile(f)["bays_max"] for f in F.KLIPPER_MMU_FAMILIES)
        self.assertGreaterEqual(widest, 12)
        for bays in (1, 4, 8, 12, 16):
            jsonschema.Draft202012Validator(ui).validate(_minimal_ui(bays))
        unit_schema = {
            "type": "object",
            "properties": manager["$defs"]["topology"]["properties"]["oams"][
                "additionalProperties"]["properties"],
        }
        for bays in (1, 4, 8, 12, 16):
            jsonschema.Draft202012Validator(unit_schema).validate(
                {"bays": bays})
        for bays in (0, 17):
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.Draft202012Validator(unit_schema).validate(
                    {"bays": bays})

    def test_seventeen_bays_do_not_fit_the_ui_projection(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest('jsonschema is not installed (the tool-suites job runs this)')
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(load(UI_SCHEMA)).validate(
                _minimal_ui(17))


def _minimal_ui(bay_count):
    """The smallest openams_ui document that exercises the bays array."""
    return {
        "v": 1, "unit": "box", "idx": 0, "family": "boxturtle",
        "serial": None, "online": True, "ready": True, "cal": "c",
        "lane": {"id": "fps", "ext": "extruder", "op": "UNLOADED",
                 "group": None, "bay": None, "stage": None, "runout": "idle",
                 "from": None, "to": None, "ms": True},
        "bays": [{"id": b, "s": "e", "cal": "c", "a": "", "sp": None}
                 for b in range(bay_count)],
        "groups": [], "env": None, "dryer": None, "fault": None, "nf": 0,
        "act": [],
    }


if __name__ == "__main__":
    unittest.main(verbosity=2)
