#!/usr/bin/env python3
"""Unit tests for the openams_spoolman tag -> Spoolman ops (C4 §4, §5).

Everything runs against the in-memory ``FakeSpoolman``; the real-server
behavior is covered by the S2a gate (the S2a gate).
"""

import asyncio
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "moonraker"))

from openams_spoolman import api as A  # noqa: E402
from openams_spoolman import ops  # noqa: E402


def bambu_tag(**overrides):
    bambu = {"manufacturer": "Bambu", "type": "PLA", "detailed": "PLA Basic",
             "color_argb": 0xFF0A2989, "weight_g": 1000, "diameter_mm": 1.75,
             "drying_temp_c": 55, "drying_time_h": 8, "bed_temp_c": 35,
             "hotend_max_c": 230, "hotend_min_c": 190, "nozzle_diameter": 0.4,
             "spool_width_mm": 66.25, "length_m": 330,
             "tray_uid": "00ff11ee22dd33cc44bb55aa66997788",
             "production": "240512"}
    bambu.update(overrides)
    return {"uid": "A1B2C3D4", "format": "bambu", "read_at": 1.0,
            "spoolman_id": None, "bambu": bambu}


def run(coro):
    return asyncio.run(coro)


def spool_extra(spool):
    """The decoded extra values of a spool, for readable assertions."""
    return {key: ops.decode_extra(value)
            for key, value in (spool.get("extra") or {}).items()}


class ExtraCodingTest(unittest.TestCase):
    def test_encode_then_decode_round_trips(self):
        for value in ("PLA Basic", 55, 8, 1.05, ""):
            self.assertEqual(ops.decode_extra(ops.encode_extra(value)), value)

    def test_encode_extra_is_json_text(self):
        self.assertEqual(ops.encode_extra("PLA Basic"), '"PLA Basic"')
        self.assertEqual(ops.encode_extra(55), "55")

    def test_decode_extra_tolerates_a_bare_value(self):
        self.assertEqual(ops.decode_extra("11AA22BB"), "11AA22BB")
        self.assertIsNone(ops.decode_extra(None))

    def test_normalize_lot_six_digit_bambu_date(self):
        self.assertEqual(ops.normalize_lot("240512"), "2024-05-12")

    def test_normalize_lot_eight_digits_and_iso(self):
        self.assertEqual(ops.normalize_lot("20240512"), "2024-05-12")
        self.assertEqual(ops.normalize_lot("2024-05-12"), "2024-05-12")
        self.assertEqual(ops.normalize_lot("2024-05-12T00:00:00Z"), "2024-05-12")

    def test_normalize_lot_passes_anything_else_through(self):
        self.assertEqual(ops.normalize_lot("lot 17"), "lot 17")
        self.assertEqual(ops.normalize_lot(""), "")
        self.assertIsNone(ops.normalize_lot(None))


class EnsureFieldsTest(unittest.TestCase):
    def test_creates_the_seven_fields_then_is_idempotent(self):
        fake = A.FakeSpoolman()
        created = run(ops.ensure_fields(fake))
        self.assertEqual(set(created), {"card_uids", "tray_uid", "flow_k",
                                        "drying_temp", "drying_time", "variant",
                                        "filament_code"})
        self.assertEqual(run(ops.ensure_fields(fake)), [])
        self.assertEqual({f["key"] for f in fake.fields["spool"]},
                         {"card_uids", "tray_uid", "flow_k"})
        self.assertEqual({f["key"] for f in fake.fields["filament"]},
                         {"drying_temp", "drying_time", "variant",
                          "filament_code"})
        types = {f["key"]: f["field_type"] for f in fake.fields["filament"]}
        self.assertEqual(types["filament_code"], "text")
        types = {f["key"]: f["field_type"] for f in fake.fields["spool"]}
        self.assertEqual(types["flow_k"], "float")

    def test_only_the_missing_keys_are_posted(self):
        fake = A.FakeSpoolman()
        fake.fields["spool"].append({"key": "card_uids", "name": "Card UIDs",
                                     "field_type": "text", "entity_type": "spool"})
        created = run(ops.ensure_fields(fake))
        self.assertNotIn("card_uids", created)
        self.assertIn("tray_uid", created)
        posts = [call for call in fake.calls if call[0] == "POST"]
        self.assertNotIn("/v1/field/spool/card_uids",
                         [call[1] for call in posts])


class VendorTest(unittest.TestCase):
    def test_get_or_create_is_idempotent(self):
        fake = A.FakeSpoolman()
        first = run(ops.get_or_create_vendor(fake, "Bambu Lab"))
        second = run(ops.get_or_create_vendor(fake, "Bambu Lab"))
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(fake.vendors), 1)


class FilamentTest(unittest.TestCase):
    def setUp(self):
        self.fake = A.FakeSpoolman()
        self.vendor = run(ops.get_or_create_vendor(self.fake, "Bambu Lab"))

    def test_creates_from_the_tag_then_matches_it(self):
        tag = bambu_tag()
        first = run(ops.find_or_create_filament(self.fake, self.vendor["id"],
                                                tag["bambu"]))
        self.assertEqual(first["name"], "PLA Basic")
        self.assertEqual(first["material"], "PLA")
        self.assertEqual(first["color_hex"], "0A2989")
        self.assertEqual(first["density"], 1.24)
        self.assertEqual(first["diameter"], 1.75)
        self.assertEqual(first["weight"], 1000)
        self.assertEqual(first["settings_extruder_temp"], 210)
        self.assertEqual(first["settings_bed_temp"], 35)
        self.assertEqual(spool_extra(first), {"variant": "PLA Basic",
                                              "drying_temp": 55,
                                              "drying_time": 8})
        second = run(ops.find_or_create_filament(self.fake, self.vendor["id"],
                                                 tag["bambu"]))
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(len(self.fake.filaments), 1)

    def test_a_different_color_creates_a_new_filament(self):
        first = run(ops.find_or_create_filament(self.fake, self.vendor["id"],
                                                bambu_tag()["bambu"]))
        other = bambu_tag(color_argb=0xFF112233)
        second = run(ops.find_or_create_filament(self.fake, self.vendor["id"],
                                                 other["bambu"]))
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(second["color_hex"], "112233")

    def test_unknown_material_and_missing_bed_temp_use_the_defaults(self):
        bambu = bambu_tag(type="UNOBTANIUM", bed_temp_c=0, detailed="Unob")["bambu"]
        filament = run(ops.find_or_create_filament(self.fake, self.vendor["id"],
                                                   bambu))
        self.assertEqual(filament["density"], ops.DEFAULT_DENSITY)
        self.assertEqual(filament["settings_bed_temp"], ops.DEFAULT_BED_TEMP)


class SpoolTest(unittest.TestCase):
    def setUp(self):
        self.fake = A.FakeSpoolman()
        run(ops.ensure_fields(self.fake))
        self.vendor = run(ops.get_or_create_vendor(self.fake, "Bambu Lab"))
        self.tag = bambu_tag()
        self.filament = run(ops.find_or_create_filament(
            self.fake, self.vendor["id"], self.tag["bambu"]))

    def create(self, location="oams1-2"):
        return run(ops.create_spool(self.fake, self.filament["id"],
                                    self.tag, location))

    def test_create_spool_starts_full_and_carries_its_uids(self):
        spool = self.create()
        self.assertEqual(spool["initial_weight"], 1000)
        self.assertEqual(spool["remaining_weight"], 1000)
        self.assertEqual(spool["used_weight"], 0)
        self.assertEqual(spool["lot_nr"], "2024-05-12")
        self.assertEqual(spool["location"], "oams1-2")
        self.assertEqual(spool_extra(spool),
                         {"card_uids": "A1B2C3D4",
                          "tray_uid": "00ff11ee22dd33cc44bb55aa66997788"})

    def test_create_spool_without_a_production_date_writes_no_lot_nr(self):
        tag = bambu_tag()
        tag["bambu"].pop("production")
        spool = run(ops.create_spool(self.fake, self.filament["id"],
                                     tag, "oams1-1"))
        self.assertFalse(spool.get("lot_nr"))
        self.assertEqual(spool["location"], "oams1-1")

    def test_find_spool_by_tag_by_tray_uid(self):
        spool = self.create()
        found = run(ops.find_spool_by_tag(self.fake, self.tag))
        self.assertEqual(found["id"], spool["id"])

    def test_find_spool_by_tag_by_card_uid(self):
        spool = self.create()
        tag = bambu_tag(tray_uid="f" * 32)
        found = run(ops.find_spool_by_tag(self.fake, tag))
        self.assertEqual(found["id"], spool["id"])

    def test_find_spool_by_tag_honors_a_given_list_and_returns_none(self):
        self.create()
        self.assertIsNone(run(ops.find_spool_by_tag(self.fake, self.tag, [])))
        other = bambu_tag()
        other["uid"] = "DEADBEEF"
        other["bambu"]["tray_uid"] = "0" * 32
        self.assertIsNone(run(ops.find_spool_by_tag(self.fake, other)))

    def test_add_card_uid_sorts_dedupes_and_merges(self):
        spool = self.create()
        run(ops.add_card_uid(self.fake, spool, "11aa22bb"))
        run(ops.add_card_uid(self.fake, spool, "11AA22BB"))
        got = run(self.fake.request("GET", "/v1/spool/%d" % spool["id"]))
        self.assertEqual(spool_extra(got),
                         {"card_uids": "11AA22BB,A1B2C3D4",
                          "tray_uid": "00ff11ee22dd33cc44bb55aa66997788"})
        self.assertEqual(sorted(spool_extra(got)["card_uids"].split(",")),
                         ["11AA22BB", "A1B2C3D4"])

    def test_set_location_sets_and_clears(self):
        spool = self.create()
        run(ops.set_location(self.fake, spool["id"], "oams1-3"))
        got = run(self.fake.request("GET", "/v1/spool/%d" % spool["id"]))
        self.assertEqual(got["location"], "oams1-3")
        run(ops.set_location(self.fake, spool["id"], ""))
        got = run(self.fake.request("GET", "/v1/spool/%d" % spool["id"]))
        self.assertEqual(got["location"], "")

    def test_spool_summary(self):
        spool = self.create()
        self.assertEqual(ops.spool_summary(spool),
                         {"material": "PLA", "color_hex": "0A2989",
                          "vendor": "Bambu Lab", "remaining_g": 1000,
                          "remaining_pct": 100, "initial_g": 1000,
                          "flow_k": None, "extruder_temp": 210,
                          "bed_temp": 35, "name": "PLA Basic",
                          # created from a tag: its tray UID marks it
                          "tagged": True})
        run(self.fake.request("PATCH", "/v1/spool/%d" % spool["id"],
                              body={"remaining_weight": 812}))
        got = run(self.fake.request("GET", "/v1/spool/%d" % spool["id"]))
        self.assertEqual(ops.spool_summary(got)["remaining_pct"], 81)

    def test_spool_summary_carries_the_extruder_temperature(self):
        # The temperature the linker pushes to the host (C4 §7.4): the
        # filament's own value. The key appears only when the spool carries
        # one, because the C4 §8 status schema names its keys.
        spool = self.create()
        got = run(self.fake.request("GET", "/v1/spool/%d" % spool["id"]))
        self.assertEqual(ops.spool_summary(got)["extruder_temp"], 210)
        run(self.fake.request("PATCH", "/v1/filament/%d"
                              % spool["filament"]["id"],
                              body={"settings_extruder_temp": None}))
        got = run(self.fake.request("GET", "/v1/spool/%d" % spool["id"]))
        summary = ops.spool_summary(got)
        self.assertIsNone(summary.get("extruder_temp"))
        self.assertNotIn("extruder_temp", summary)

    def test_spool_summary_percent_is_null_when_unknown(self):
        spool = self.create()
        run(self.fake.request("PATCH", "/v1/spool/%d" % spool["id"],
                              body={"remaining_weight": None,
                                    "initial_weight": None}))
        got = run(self.fake.request("GET", "/v1/spool/%d" % spool["id"]))
        summary = ops.spool_summary(got)
        self.assertIsNone(summary["remaining_g"])
        self.assertIsNone(summary["remaining_pct"])
        # The filament still knows its nominal weight, so initial_g survives.
        self.assertEqual(summary["initial_g"], 1000)

    def test_spool_summary_initial_g_falls_back_to_the_filament_weight(self):
        spool = self.create()
        run(self.fake.request("PATCH", "/v1/spool/%d" % spool["id"],
                              body={"initial_weight": None}))
        got = run(self.fake.request("GET", "/v1/spool/%d" % spool["id"]))
        self.assertIsNone(got["initial_weight"])
        self.assertEqual(got["filament"]["weight"], 1000)
        self.assertEqual(ops.spool_summary(got)["initial_g"], 1000)

    def test_spool_summary_initial_g_is_null_when_neither_knows_it(self):
        self.assertIsNone(ops.spool_summary({"filament": {}})["initial_g"])
        self.assertIsNone(ops.spool_summary({})["initial_g"])

    def test_spool_summary_flow_k_from_extra(self):
        spool = self.create()
        run(self.fake.request("PATCH", "/v1/spool/%d" % spool["id"],
                              body={"extra": {"flow_k": ops.encode_extra(0.05)}}))
        got = run(self.fake.request("GET", "/v1/spool/%d" % spool["id"]))
        self.assertEqual(ops.spool_summary(got)["flow_k"], 0.05)
        # A bare value a UI wrote without JSON quoting decodes the same way.
        spool["extra"]["flow_k"] = "1.0"
        self.assertEqual(ops.spool_summary(spool)["flow_k"], 1.0)

    def test_spool_summary_flow_k_is_null_when_unusable(self):
        spool = self.create()
        for value in (None, "", "not a number", "true",
                      ops.encode_extra("fast"), ops.encode_extra([]),
                      ops.encode_extra(None)):
            spool["extra"] = {"flow_k": value}
            self.assertIsNone(ops.spool_summary(spool)["flow_k"], value)
        # A JSON bool is not a pressure advance either (bool is an int).
        spool["extra"] = {"flow_k": ops.encode_extra(True)}
        self.assertIsNone(ops.spool_summary(spool)["flow_k"])
        spool["extra"] = {}
        self.assertIsNone(ops.spool_summary(spool)["flow_k"])

    def test_spool_summary_is_null_color_safe(self):
        self.assertEqual(ops.spool_summary({})["color_hex"], None)
        self.assertEqual(ops.spool_summary({})["vendor"], None)


class ExtruderTempTableTest(unittest.TestCase):
    """The material temperature table the host is pushed from (C4 §7.4)."""

    def test_the_table_holds_the_documented_values(self):
        self.assertEqual(ops.MATERIAL_EXTRUDER_TEMP, {
            "PLA": 210, "PLA-CF": 220, "PETG": 240, "PETG-CF": 250,
            "ABS": 250, "ASA": 255, "HIPS": 240, "PC": 270,
            "PA": 270, "PA-CF": 280, "PA6": 275, "TPU": 225,
            "TPE": 225, "PVA": 200, "PP": 235, "PPS": 320})

    def test_the_table_and_the_bed_table_cover_the_same_materials(self):
        self.assertEqual(set(ops.MATERIAL_EXTRUDER_TEMP),
                         set(ops.MATERIAL_BED_TEMP))

    def test_exact_match_is_case_and_space_insensitive(self):
        for material in ("PLA", "pla", "  Petg  "):
            self.assertIsNotNone(ops.material_extruder_temp(material))
        self.assertEqual(ops.material_extruder_temp("petg"), 240)

    def test_a_variant_falls_back_to_its_base_material(self):
        self.assertEqual(ops.material_extruder_temp("PLA+"), 210)
        self.assertEqual(ops.material_extruder_temp("ABS-GF"), 250)
        self.assertEqual(ops.material_extruder_temp("PETG CF"), 240)

    def test_an_exact_entry_wins_over_the_base_material(self):
        self.assertEqual(ops.material_extruder_temp("PLA-CF"), 220)
        self.assertEqual(ops.material_extruder_temp("PA-CF"), 280)

    def test_an_unknown_or_absent_material_has_no_temperature(self):
        for material in (None, "", "   ", "Unobtanium", "PCTG"):
            self.assertIsNone(ops.material_extruder_temp(material))

    def test_a_user_entry_replaces_or_adds_table_entries(self):
        table = ops.material_temp_table(ops.parse_material_temps(
            "PLA=215, PCTG=250"))
        self.assertEqual(ops.material_extruder_temp("PLA", table), 215)
        self.assertEqual(ops.material_extruder_temp("PCTG", table), 250)
        # Everything the option does not name keeps the built-in value.
        self.assertEqual(ops.material_extruder_temp("PETG", table), 240)

    def test_entries_may_be_written_with_new_lines(self):
        parsed = ops.parse_material_temps("PLA=215\nPETG=235, PLA-CF=225")
        self.assertEqual(parsed, {"PLA": 215, "PETG": 235, "PLA-CF": 225})

    def test_a_bad_entry_is_skipped_and_the_rest_still_parse(self):
        # A typo in one material must not cost the others, nor stop the
        # component: this never raises.
        self.assertEqual(ops.parse_material_temps(
            "PLA=hot, PETG=235, ABS=9999, PPS=1"), {"PETG": 235})
        # A good entry for the same name still applies after a bad one.
        self.assertEqual(ops.parse_material_temps("PLA=hot, PLA=320"),
                         {"PLA": 320})

    def test_an_empty_or_absent_option_parses_to_nothing(self):
        for text in (None, "", "  ,  \n "):
            self.assertEqual(ops.parse_material_temps(text), {})

    def test_a_spool_temperature_the_host_could_not_heat_to_is_rejected(self):
        self.assertEqual(ops.extruder_temp_of(240), 240)
        self.assertEqual(ops.extruder_temp_of("240"), 240)
        self.assertEqual(ops.extruder_temp_of(240.0), 240)
        for value in (None, 0, 100, 500, "hot", True, False, ""):
            self.assertIsNone(ops.extruder_temp_of(value))

    def test_a_material_name_is_trimmed_and_capped_at_16_characters(self):
        self.assertEqual(ops.material_name_of("  PETG  "), "PETG")
        self.assertEqual(ops.material_name_of("A" * 30), "A" * 16)
        self.assertIsNone(ops.material_name_of("   "))
        self.assertIsNone(ops.material_name_of(None))


class FailureTest(unittest.TestCase):
    def test_offline_spoolman_raises_unavailable(self):
        fake = A.FakeSpoolman()
        fake.online = False
        calls = [ops.ensure_fields(fake),
                 ops.get_or_create_vendor(fake, "Bambu Lab"),
                 ops.find_or_create_filament(fake, 1, bambu_tag()["bambu"]),
                 ops.create_spool(fake, 1, bambu_tag(), "oams1-0"),
                 ops.find_spool_by_tag(fake, bambu_tag()),
                 ops.add_card_uid(fake, {"id": 1, "extra": {}}, "AABB"),
                 ops.set_location(fake, 1, "")]
        for coro in calls:
            with self.assertRaises(A.SpoolmanUnavailable):
                run(coro)

    def test_http_error_carries_status_and_message(self):
        fake = A.FakeSpoolman()
        run(ops.ensure_fields(fake))
        with self.assertRaises(A.SpoolmanError) as caught:
            run(ops.set_location(fake, 4242, "oams1-0"))
        self.assertEqual(caught.exception.status, 404)
        self.assertIn("4242", caught.exception.message)

    def test_unreachable_direct_api_raises_unavailable(self):
        api = A.DirectSpoolmanAPI("http://127.0.0.1:1/api")
        with self.assertRaises(A.SpoolmanUnavailable):
            run(api.request("GET", "/v1/info"))


if __name__ == "__main__":
    unittest.main()
