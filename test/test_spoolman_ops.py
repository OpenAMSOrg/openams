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


class TagVendorOptionTest(unittest.TestCase):
    """The ``tag_vendor`` option (C4 §5.3)."""

    def test_unset_or_empty_is_bambu_lab(self):
        for text in (None, "", "   "):
            self.assertEqual(ops.parse_tag_vendor(text), "Bambu Lab")

    def test_a_name_is_trimmed_and_kept(self):
        self.assertEqual(ops.parse_tag_vendor("  My Filaments "), "My Filaments")

    def test_a_name_that_is_too_long_is_ignored_not_fatal(self):
        with self.assertLogs(level="WARNING"):
            self.assertEqual(ops.parse_tag_vendor("x" * 65), "Bambu Lab")
        self.assertEqual(ops.parse_tag_vendor("x" * 64), "x" * 64)

    def test_the_option_vendor_matches_a_bambu_code(self):
        summary = {"material": "PLA", "color_hex": None, "manufacturer": "Bambu"}
        spool = {"material": "PLA", "color_hex": None, "vendor": "My Filaments"}
        self.assertFalse(ops.summary_matches(spool, summary))
        self.assertTrue(ops.summary_matches(spool, summary, "My Filaments"))
        spool["vendor"] = "Bambu Lab"
        self.assertTrue(ops.summary_matches(spool, summary, "My Filaments"))


class FindSpoolByTagOrderTest(unittest.TestCase):
    """A UID a user listed on a spool wins over the shared tray UID (C4 §4)."""

    def test_the_card_uid_wins_whatever_the_order(self):
        tag = dict(bambu_tag(), uid="04aabbcc")
        tray = {"id": 1, "extra": {"tray_uid": ops.encode_extra(
            tag["bambu"]["tray_uid"])}}
        card = {"id": 2, "extra": {"card_uids": ops.encode_extra("11,04AABBCC")}}
        for spools in ([tray, card], [card, tray]):
            self.assertEqual(
                run(ops.find_spool_by_tag(None, tag, spools))["id"], 2)

    def test_the_tray_uid_finds_the_second_tag_of_a_spool(self):
        tag = dict(bambu_tag(), uid="04aabbcc")
        tray = {"id": 1, "extra": {"tray_uid": ops.encode_extra(
            tag["bambu"]["tray_uid"])}}
        self.assertEqual(run(ops.find_spool_by_tag(None, tag, [tray]))["id"], 1)


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


class SpoolKeyTest(unittest.TestCase):
    """``spool_key``/``valid_tray`` against the vectors of the two-tag
    interfaces (§1), identical in the host's ``src/tag_codec.py``."""

    TRAY = "0123456789abcdef0123456789abcdef"
    VECTORS = (
        ({"format": "bambu", "uid": "04a1b2c3",
          "bambu": {"tray_uid": "0123456789ABCDEF0123456789abcdef"}},
         "bambu:0123456789abcdef0123456789abcdef"),
        ({"format": "bambu", "uid": "04a1b2c3",
          "bambu": {"tray_uid": " 0123456789abcdef0123456789abcdef "}},
         "bambu:0123456789abcdef0123456789abcdef"),
        ({"format": "bambu", "uid": "04a1b2c3",
          "bambu": {"tray_uid": "00000000000000000000000000000000"}},
         "uid:04A1B2C3"),
        ({"format": "bambu", "uid": "04a1b2c3",
          "bambu": {"tray_uid": "ffffffffffffffffffffffffffffffff"}},
         "uid:04A1B2C3"),
        ({"format": "bambu", "uid": "04a1b2c3", "bambu": {"tray_uid": "0123"}},
         "uid:04A1B2C3"),
        ({"format": "bambu", "uid": "04a1b2c3", "bambu": None},
         "uid:04A1B2C3"),
        ({"format": "spoolman_link", "uid": "99887766", "spoolman_id": 12},
         "uid:99887766"),
        ({"format": "unit_summary", "uid": None,
          "summary": {"material": "ABS"}}, None),
        (None, None),
    )

    def test_the_interface_vectors(self):
        for tag, key in self.VECTORS:
            with self.subTest(tag=tag):
                self.assertEqual(ops.spool_key(tag), key)

    def test_valid_tray(self):
        self.assertTrue(ops.valid_tray(self.TRAY))
        self.assertTrue(ops.valid_tray(" " + self.TRAY.upper() + "\n"))
        for bad in ("0" * 32, "f" * 32, "F" * 32, "fF" * 16, "0123",
                    self.TRAY + "0", "g" * 32, "", None, 12345):
            with self.subTest(bad=bad):
                self.assertFalse(ops.valid_tray(bad))

    def test_the_hosts_key_and_uids_are_used_when_published(self):
        tag = {"format": "bambu", "uid": "04a1b2c3", "bambu": None,
               "spool_key": "bambu:" + self.TRAY,
               "uids": ["11aa22bb", "04A1B2C3"]}
        self.assertEqual(ops.tag_key(tag), "bambu:" + self.TRAY)
        self.assertEqual(ops.tag_uids(tag), ["11AA22BB", "04A1B2C3"])
        # Without them, both are computed from the tag.
        bare = {"format": "bambu", "uid": "04a1b2c3", "bambu": None}
        self.assertEqual(ops.tag_key(bare), "uid:04A1B2C3")
        self.assertEqual(ops.tag_uids(bare), ["04A1B2C3"])
        self.assertEqual(ops.tag_key({"format": "unit_summary", "uid": None,
                                      "spool_key": None}), None)

    def test_a_tag_is_decoded_only_with_bambu_content(self):
        self.assertTrue(ops.tag_decoded(bambu_tag()))
        for tag in ({"format": "bambu", "uid": "AA", "bambu": None},
                    {"format": "bambu", "uid": "AA", "bambu": {}},
                    {"format": "unknown", "uid": "AA", "bambu": None}):
            self.assertFalse(ops.tag_decoded(tag))


def tagged(spool_id, uids=None, tray=None, **fields):
    extra = {}
    if uids is not None:
        extra["card_uids"] = ops.encode_extra(uids)
    if tray is not None:
        extra["tray_uid"] = ops.encode_extra(tray)
    return dict({"id": spool_id, "extra": extra, "location": ""}, **fields)


class MatchTagTest(unittest.TestCase):
    """The resolution order of C4 §4.2 (``ops.match_tag``)."""

    TRAY = "00ff11ee22dd33cc44bb55aa66997788"

    def test_the_read_uid_then_the_tray_then_a_sibling(self):
        by_uid = tagged(1, uids="A1B2C3D4")
        by_tray = tagged(2, tray=self.TRAY)
        sibling = tagged(3, uids="11AA22BB")
        tag = dict(bambu_tag(), uids=["A1B2C3D4", "11AA22BB"])
        self.assertEqual(ops.match_tag(tag, [sibling, by_tray, by_uid]),
                         (by_uid, "uid"))
        self.assertEqual(ops.match_tag(tag, [sibling, by_tray]),
                         (by_tray, "tray"))
        self.assertEqual(ops.match_tag(tag, [sibling]), (sibling, "sibling"))
        self.assertEqual(ops.match_tag(tag, []), (None, None))

    def test_a_tray_uid_matches_in_any_case_and_trimmed(self):
        stored = tagged(5, tray="  " + self.TRAY.upper() + " ")
        self.assertEqual(ops.match_tag(bambu_tag(), [stored])[0], stored)
        tag = bambu_tag(tray_uid=" " + self.TRAY.upper())
        self.assertEqual(ops.match_tag(tag, [tagged(6, tray=self.TRAY)])[0]
                         ["id"], 6)

    def test_a_blank_tray_never_matches(self):
        for blank in ("0" * 32, "f" * 32, "F" * 32):
            spools = [tagged(1, tray=blank), tagged(2, tray=self.TRAY)]
            tag = bambu_tag(tray_uid=blank)
            self.assertEqual(ops.match_tag(tag, spools), (None, None))

    def test_a_sibling_on_a_spool_of_another_tray_or_a_shared_key_is_not(self):
        other = tagged(3, uids="11AA22BB", tray="1" * 32)
        tag = dict(bambu_tag(), uids=["A1B2C3D4", "11AA22BB"])
        self.assertEqual(ops.match_tag(tag, [other]), (None, None))
        mine = tagged(4, uids="11AA22BB")
        self.assertEqual(
            ops.match_tag(dict(tag, shared_identity=True), [mine]),
            (None, None))

    def test_tag_extra_adds_the_uid_and_backfills_normalised(self):
        spool = tagged(1, uids="a1b2c3d4", tray=None)
        extra = ops.tag_extra(spool, "11aa22bb", self.TRAY.upper())
        self.assertEqual(ops.decode_extra(extra["card_uids"]),
                         "11AA22BB,A1B2C3D4")
        self.assertEqual(ops.decode_extra(extra["tray_uid"]), self.TRAY)
        # Nothing new: no write.
        done = tagged(1, uids="A1B2C3D4,11AA22BB", tray=self.TRAY)
        self.assertIsNone(ops.tag_extra(done, "11AA22BB", self.TRAY))
        # A spool that has a tray UUID keeps it.
        own = tagged(1, uids="A1B2C3D4", tray="1" * 32)
        extra = ops.tag_extra(own, "11AA22BB", self.TRAY)
        self.assertEqual(ops.decode_extra(extra["tray_uid"]), "1" * 32)

    def test_create_spool_writes_only_a_valid_tray(self):
        api = A.FakeSpoolman()
        vendor = run(ops.get_or_create_vendor(api, "Bambu Lab"))
        filament = run(ops.find_or_create_filament(api, vendor["id"],
                                                   bambu_tag()["bambu"]))
        blank = run(ops.create_spool(api, filament["id"],
                                     bambu_tag(tray_uid="0" * 32), "oams1-0"))
        self.assertNotIn("tray_uid", blank["extra"])
        upper = run(ops.create_spool(api, filament["id"],
                                     bambu_tag(tray_uid=self.TRAY.upper()),
                                     "oams1-1"))
        self.assertEqual(spool_extra(upper)["tray_uid"], self.TRAY)


class LegacyAndDuplicatesTest(unittest.TestCase):
    """The legacy check (C4 §4.2 step 6), the duplicates report and the merge
    plan (C4 §9.2)."""

    TRAY = "00ff11ee22dd33cc44bb55aa66997788"

    def filament(self, vendor="Bambu Lab", material="PLA", name="PLA Basic",
                 color_hex="0a2989"):
        return {"name": name, "material": material, "color_hex": color_hex,
                "vendor": {"name": vendor}}

    def test_a_legacy_spool_is_found_by_filament(self):
        legacy = tagged(1, uids="11AA22BB", filament=self.filament())
        found = ops.legacy_spools([legacy], bambu_tag(), "oams1-0")
        self.assertEqual(found, [legacy])

    def test_what_is_not_a_legacy_spool(self):
        cases = {
            "has a tray": tagged(1, uids="11AA22BB", tray="1" * 32,
                                 filament=self.filament()),
            "no uid": tagged(2, filament=self.filament()),
            "another bay": tagged(3, uids="11AA22BB", location="oams1-3",
                                  filament=self.filament()),
            "archived": tagged(4, uids="11AA22BB", archived=True,
                               filament=self.filament()),
            "colour": tagged(5, uids="11AA22BB",
                             filament=self.filament(color_hex="000000")),
            "vendor": tagged(6, uids="11AA22BB",
                             filament=self.filament(vendor="Polymaker")),
            "name": tagged(7, uids="11AA22BB",
                           filament=self.filament(name="PLA Matte")),
        }
        for reason, spool in cases.items():
            with self.subTest(reason=reason):
                self.assertEqual(
                    ops.legacy_spools([spool], bambu_tag(), "oams1-0"), [])
        here = tagged(8, uids="11AA22BB", location="oams1-0",
                      filament=self.filament())
        self.assertEqual(ops.legacy_spools([here], bambu_tag(), "oams1-0"),
                         [here])
        self.assertEqual(ops.legacy_spools([here], bambu_tag(), "oams1-0",
                                           excluded={8}), [])

    def test_duplicate_pairs(self):
        spools = [tagged(1, uids="AA", tray=self.TRAY),
                  tagged(2, uids="BB", tray=self.TRAY.upper()),
                  tagged(3, uids="AA,CC"),
                  tagged(4, uids="CC", archived=True),
                  tagged(5, tray="0" * 32), tagged(6, tray="0" * 32)]
        self.assertEqual(ops.duplicate_pairs(spools), [
            {"spools": [1, 2], "reason": "shared_tray",
             "tray_uid": self.TRAY, "uid": None},
            {"spools": [1, 3], "reason": "shared_uid", "tray_uid": None,
             "uid": "AA"}])

    def test_tag_gaps(self):
        enc = ops.encode_extra
        spools = [
            {"id": 1, "extra": {"card_uids": enc("AA")}},
            {"id": 2, "extra": {"card_uids": enc("BB"),
                                "tray_uid": enc("ab" * 16)}},
            {"id": 3, "extra": {"card_uids": enc("CC"),
                                "tray_uid": enc("0" * 32)}},
            {"id": 4, "extra": {"tray_uid": enc("nonsense")}},
            {"id": 5, "extra": {}},
            {"id": 6, "archived": True, "extra": {"card_uids": enc("DD")}},
        ]
        self.assertEqual(ops.tag_gaps(spools),
                         {"without_tray": [1], "invalid_tray": [3, 4]})

    def test_merge_plan(self):
        keep = tagged(1, uids="AA")
        other = tagged(2, uids="BB", tray=self.TRAY)
        self.assertEqual(ops.merge_plan(keep, other),
                         {"card_uids": ["AA", "BB"], "tray_uid": self.TRAY})
        with self.assertRaises(ValueError):
            ops.merge_plan(tagged(1, tray="1" * 32), other)

    def test_find_archived(self):
        api = A.FakeSpoolman()
        vendor = run(ops.get_or_create_vendor(api, "Bambu Lab"))
        filament = run(ops.find_or_create_filament(api, vendor["id"],
                                                   bambu_tag()["bambu"]))
        spool = run(ops.create_spool(api, filament["id"], bambu_tag(), ""))
        self.assertIsNone(run(ops.find_archived(api, bambu_tag())))
        run(api.request("PATCH", "/v1/spool/%d" % spool["id"],
                        body={"archived": True}))
        self.assertEqual(run(api.request("GET", "/v1/spool")), [])
        self.assertEqual(run(ops.find_archived(api, bambu_tag()))["id"],
                         spool["id"])


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
