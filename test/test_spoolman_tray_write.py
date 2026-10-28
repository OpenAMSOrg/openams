#!/usr/bin/env python3
"""A unit's stored per-bay filament setting, kept in step with Spoolman (C4 §7.5).

The mapping from a Spoolman spool to the setting (``tray.setting_for``), the
sync policy (``tray.TraySync`` through the real ``Linker``), the Keep rule for
the unit's own report of a written setting (C4 §4.1), and the component's
remote method and timed passes. Against the in-memory ``FakeSpoolman``, like
``test_spoolman_linker.py``. Run with::

    python3 -m unittest discover -s test -p 'test_spoolman_tray_write.py' -v
"""

import json
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "moonraker"))
sys.path.insert(0, _HERE)

from openams_spoolman import api as A               # noqa: E402
from openams_spoolman import cache as C             # noqa: E402
from openams_spoolman import linker as L            # noqa: E402
from openams_spoolman import ops                    # noqa: E402
from openams_spoolman import tray                   # noqa: E402
from test_spoolman_linker import Clock, run         # noqa: E402
import test_spoolman_component as T                 # noqa: E402

try:
    import jsonschema
except ImportError:
    jsonschema = None

CAPS = {"tray_info": True, "tray_write": True}


def summary_tag(code="GFB60", material="ABS", color_argb=0xFF898989,
                nozzle=(240, 280), manufacturer="Bambu", read_at=1.0):
    return {"uid": None, "format": "unit_summary", "read_at": read_at,
            "spoolman_id": None, "bambu": None,
            "summary": {"code": code, "material": material,
                        "color_argb": color_argb, "nozzle_min_c": nozzle[0],
                        "nozzle_max_c": nozzle[1],
                        "manufacturer": manufacturer}}


def echo_of(setting, read_at):
    """The unit's report of a setting it stored (C2 §4.6, as the host
    publishes it): the code names Bambu when it starts with GF."""
    rgba = setting["color_rgba"]
    return summary_tag(
        code=setting["code"] or None, material=setting["material"],
        color_argb=((rgba & 0xFF) << 24) | (rgba >> 8) if rgba else None,
        nozzle=(setting["nozzle_min_c"], setting["nozzle_max_c"]),
        manufacturer="Bambu" if setting["code"].startswith("GF") else None,
        read_at=read_at)


def bay(index=2, ready=True, tag=None, spool_id=None, link_source="none",
        positioning=False):
    return {"index": index, "ready": ready, "loaded": False, "tag": tag,
            "spool_id": spool_id, "link_source": link_source,
            "positioning": positioning}


def status(bays, capabilities=CAPS, sync=True, auto_create=False):
    settings = {"auto_create_spoolman_spools": auto_create,
                "apply_pa_on_load": False}
    if sync is not None:
        settings["sync_unit_tray_info"] = sync
    return {"ready": True, "settings": settings,
            "topology": {"oams": {"oams1": {"idx": 1, "lane": "fps1"}}},
            "lanes_by_fps": {"fps1": {"op": "UNLOADED", "unit": None,
                                      "runout": "idle",
                                      "extruder": "extruder"}},
            "devices": {"oams1": {"capabilities": dict(capabilities or {}),
                                  "bays": list(bays)}}}


class Harness(object):
    def __init__(self, fail=None):
        self.api = A.FakeSpoolman()
        self.sent = []
        self.wakes = []
        self.fail = fail            # an exception every write raises
        self.clock = Clock()
        self.cache = C.SpoolCache()
        self.linker = L.Linker(self.api, self._gcode, clock=self.clock,
                               cache=self.cache, wake=self.wakes.append)
        run(self.linker.start())

    async def _gcode(self, script):
        self.sent.append(script)
        if self.fail is not None and script.startswith("OAMSM_SET_TRAY_INFO"):
            raise self.fail

    def send(self, bays, **kwargs):
        run(self.linker.on_status(status(bays, **kwargs)))

    def writes(self):
        return [s for s in self.sent if s.startswith("OAMSM_SET_TRAY_INFO")]

    def links(self):
        return [s for s in self.sent
                if s.startswith(("OAMSM_SET_BAY_SPOOL",
                                 "OAMSM_CLEAR_BAY_SPOOL"))]

    def entry(self, key="oams1-2"):
        return self.linker.status()["bays"][key]

    def spool(self, material="PLA", color_hex="FF0000", vendor="Acme",
              temp=None, code=None, extra=None):
        async def make():
            made = await self.api.request("POST", "/v1/vendor",
                                          body={"name": vendor})
            filament = await self.api.request("POST", "/v1/filament", body={
                "name": material, "vendor_id": made["id"],
                "material": material, "color_hex": color_hex,
                "settings_extruder_temp": temp})
            if code is not None:
                await self.api.request(
                    "PATCH", "/v1/filament/%d" % filament["id"],
                    body={"extra": {"filament_code": json.dumps(code)}})
            spool = await self.api.request("POST", "/v1/spool", body={
                "filament_id": filament["id"], "initial_weight": 1000,
                "remaining_weight": 1000, "extra": dict(extra or {})})
            return spool["id"]
        return run(make())

    def patch_filament(self, spool_id, **body):
        async def change():
            spool = await self.api.request("GET", "/v1/spool/%d" % spool_id)
            await self.api.request(
                "PATCH", "/v1/filament/%d" % spool["filament"]["id"],
                body=body)
        run(change())

    def linked(self, spool_id, tag=None, source="manual"):
        """Link a bay and let the debounce pass."""
        self.send([bay(tag=tag, spool_id=spool_id, link_source=source)])
        self.clock.advance(tray.DEBOUNCE_SECONDS + 0.1)
        self.send([bay(tag=tag, spool_id=spool_id, link_source=source)])


# ------------------------------------------------------------------ mapping


class MappingTests(unittest.TestCase):
    def setting(self, current=None, temps=None, **summary):
        return tray.setting_for(summary, current, temps)

    def test_the_default_table(self):
        expected = {"PLA": ("GFL99", 190, 230), "PETG": ("GFG99", 220, 260),
                    "ABS": ("GFB99", 240, 280), "ASA": ("GFB98", 240, 280),
                    "TPU": ("GFU99", 200, 240), "PA": ("GFN99", 260, 300),
                    "PC": ("GFC99", 260, 300), "PVA": ("GFS99", 190, 230),
                    "PLA-CF": ("GFL98", 190, 230)}
        for material, (code, low, high) in expected.items():
            got = self.setting(material=material, color_hex="000000")
            self.assertEqual((got["code"], got["nozzle_min_c"],
                              got["nozzle_max_c"]), (code, low, high),
                             material)

    def test_a_variant_takes_its_base_material(self):
        got = self.setting(material="pla+")
        self.assertEqual((got["code"], got["material"]), ("GFL99", "PLA+"))
        got = self.setting(material="PETG-CF")
        self.assertEqual(got["code"], "GFG99")
        self.assertEqual((got["nozzle_min_c"], got["nozzle_max_c"]),
                         (220, 260))

    def test_an_unknown_material(self):
        # Generic PLA's code; a range from the extruder table, else PLA's.
        got = self.setting(material="PA6")
        self.assertEqual(got["code"], "GFL99")
        self.assertEqual((got["nozzle_min_c"], got["nozzle_max_c"]),
                         (265, 285))
        got = self.setting(material="PEEK")
        self.assertEqual((got["code"], got["nozzle_min_c"],
                          got["nozzle_max_c"]), ("GFL99", 190, 230))
        got = self.setting(material="PEEK", temps={"PEEK": 390})
        self.assertEqual((got["nozzle_min_c"], got["nozzle_max_c"]),
                         (380, 400))

    def test_the_filaments_own_code_wins(self):
        self.assertEqual(self.setting(material="PLA",
                                      filament_code=" gfa00 ")["code"],
                         "GFA00")
        for bad in ("GFA00XY", "GF A0", "", None, 7):
            self.assertEqual(self.setting(material="PLA",
                                          filament_code=bad)["code"],
                             "GFL99", bad)

    def test_material_is_upper_cased_ascii_and_at_most_eight(self):
        self.assertEqual(self.setting(material=" pla ")["material"], "PLA")
        self.assertEqual(self.setting(material="PLA Matte Pro")["material"],
                         "PLA MATT")
        self.assertEqual(self.setting(material="PLA–CF")["material"],
                         "PLACF")
        self.assertIsNone(self.setting(material=""))
        self.assertIsNone(self.setting(material=None))

    def test_colour(self):
        self.assertEqual(tray.color_rgba_of("ff0000"), 0xFF0000FF)
        self.assertEqual(tray.color_rgba_of("#11223380"), 0x11223380)
        for bad in ("red", "12345", "", None):
            self.assertIsNone(tray.color_rgba_of(bad))
        self.assertEqual(self.setting(material="PLA",
                                      color_hex="0A2989")["color_rgba"],
                         0x0A2989FF)
        self.assertEqual(self.setting(material="PLA")["color_rgba"], 0)

    def test_temperature_is_the_spools_own_plus_minus_ten(self):
        got = self.setting(material="PETG", extruder_temp=245)
        self.assertEqual((got["nozzle_min_c"], got["nozzle_max_c"]),
                         (235, 255))
        got = self.setting(material="PLA", extruder_temp=155)
        self.assertEqual((got["nozzle_min_c"], got["nozzle_max_c"]),
                         (150, 165))
        got = self.setting(material="PC", extruder_temp=395)
        self.assertEqual((got["nozzle_min_c"], got["nozzle_max_c"]),
                         (385, 400))
        # Not a temperature the host could hold: the table instead.
        got = self.setting(material="PLA", extruder_temp=1000)
        self.assertEqual((got["nozzle_min_c"], got["nozzle_max_c"]),
                         (190, 230))

    def test_what_the_spool_does_not_name_keeps_the_units_own(self):
        current = {"code": "GFA00", "material": "PLA",
                   "color_rgba": 0x112233FF, "nozzle_min_c": 190,
                   "nozzle_max_c": 240}
        self.assertEqual(self.setting(current, material="pla"), current)
        got = self.setting(current, material="PLA", color_hex="FF0000",
                           extruder_temp=220, filament_code="GFL03")
        self.assertEqual(got, {"code": "GFL03", "material": "PLA",
                               "color_rgba": 0xFF0000FF, "nozzle_min_c": 210,
                               "nozzle_max_c": 230})
        # Another material: the defaults, and no colour.
        got = self.setting(current, material="PETG")
        self.assertEqual(got, {"code": "GFG99", "material": "PETG",
                               "color_rgba": 0, "nozzle_min_c": 220,
                               "nozzle_max_c": 260})
        # A code the unit could not take back is not kept.
        got = self.setting(dict(current, code="GFSL99_01"), material="PLA")
        self.assertEqual(got["code"], "GFL99")

    def test_the_units_report_and_the_fingerprint(self):
        tag = summary_tag(code="GFB60", material="ABS",
                          color_argb=0xFF112233, nozzle=(240, 270))
        current = tray.setting_of_tag(tag)
        self.assertEqual(current, {"code": "GFB60", "material": "ABS",
                                   "color_rgba": 0x112233FF,
                                   "nozzle_min_c": 240, "nozzle_max_c": 270})
        self.assertIsNone(tray.setting_of_tag(None))
        self.assertIsNone(tray.setting_of_tag({"format": "bambu"}))
        # The written setting and the unit's report of it fingerprint alike.
        for setting in (current, dict(current, color_rgba=0, code=""),
                        dict(current, color_rgba=0x000000FF)):
            self.assertEqual(
                ops.summary_fingerprint(tray.fingerprint_summary(setting)),
                ops.summary_fingerprint(ops.summary_of(echo_of(setting, 2.0))))

    def test_the_command(self):
        setting = {"code": "GFL99", "material": "PLA",
                   "color_rgba": 0xFF0000FF, "nozzle_min_c": 190,
                   "nozzle_max_c": 230}
        self.assertEqual(tray.gcode("oams1", 2, setting),
                         "OAMSM_SET_TRAY_INFO OAMS=oams1 BAY=2 CODE=GFL99"
                         " MATERIAL=PLA COLOR=FF0000FF MIN=190 MAX=230")
        self.assertEqual(
            tray.gcode("oams1", 0, dict(setting, code="", material="PLA CF",
                                        color_rgba=0)),
            'OAMSM_SET_TRAY_INFO OAMS=oams1 BAY=0 MATERIAL="PLA CF"'
            " COLOR=00000000 MIN=190 MAX=230")

    def test_the_spool_summary_carries_the_code_and_the_tag_mark(self):
        h = Harness()
        plain = h.spool(code="GFA00")
        tagged = h.spool(extra={"tray_uid": json.dumps("ab" * 16)})
        carded = h.spool(extra={"card_uids": json.dumps("04AABB")})
        get = lambda sid: ops.spool_summary(
            run(h.api.request("GET", "/v1/spool/%d" % sid)))
        self.assertEqual(get(plain)["filament_code"], "GFA00")
        self.assertNotIn("tagged", get(plain))
        self.assertTrue(get(tagged)["tagged"])
        self.assertTrue(get(carded)["tagged"])
        self.assertNotIn("filament_code", get(tagged))


# ------------------------------------------------------------------- policy


class SyncTests(unittest.TestCase):
    def test_a_manual_link_writes_once_after_the_debounce(self):
        h = Harness()
        spool = h.spool(material="PLA", color_hex="FF0000")
        h.send([bay(tag=summary_tag(), spool_id=spool)])
        self.assertEqual(h.writes(), [])
        self.assertIn(tray.DEBOUNCE_SECONDS, h.wakes)
        self.assertEqual(h.entry()["tray_sync"],
                         {"state": "pending", "error": None})
        h.clock.advance(tray.DEBOUNCE_SECONDS + 0.1)
        h.send([bay(tag=summary_tag(), spool_id=spool)])
        self.assertEqual(h.writes(), [
            "OAMSM_SET_TRAY_INFO OAMS=oams1 BAY=2 CODE=GFL99 MATERIAL=PLA"
            " COLOR=FF0000FF MIN=190 MAX=230"])
        self.assertIn(tray.RETRY_SECONDS, h.wakes)

    def test_nothing_is_written_when_the_unit_already_has_it(self):
        h = Harness()
        spool = h.spool(material="ABS", color_hex="898989", temp=260,
                        code="GFB60")
        h.linked(spool, tag=summary_tag(nozzle=(250, 270)))
        self.assertEqual(h.writes(), [])
        self.assertEqual(h.entry()["tray_sync"]["state"], "in_sync")

    def test_an_untagged_bay_report_is_written(self):
        # No report at all (read, no tag): the unit holds nothing.
        h = Harness()
        spool = h.spool(material="PETG", color_hex="00FF00", temp=245)
        h.linked(spool)
        self.assertEqual(h.writes(), [
            "OAMSM_SET_TRAY_INFO OAMS=oams1 BAY=2 CODE=GFG99 MATERIAL=PETG"
            " COLOR=00FF00FF MIN=235 MAX=255"])

    def test_a_link_the_units_report_made_is_kept_until_the_spool_changes(self):
        h = Harness()
        spool = h.spool(material="ABS", color_hex="898989", temp=260)
        h.linked(spool, tag=summary_tag(), source="tag")
        self.assertEqual(h.writes(), [])
        self.assertEqual(h.entry()["tray_sync"]["state"], "kept")
        # An edit of the filament's colour is a Spoolman-side change.
        h.patch_filament(spool, color_hex="0000FF")
        h.linker.apply_edit("oams1-2", spool, run(
            h.api.request("GET", "/v1/spool/%d" % spool)))
        h.linked(spool, tag=summary_tag(), source="tag")
        self.assertEqual(len(h.writes()), 1)
        self.assertIn("COLOR=0000FFFF", h.writes()[0])

    def test_an_edit_made_in_spoolman_arrives_by_the_refresh(self):
        h = Harness()
        spool = h.spool(material="PLA", color_hex="FF0000", temp=210)
        h.linked(spool)
        tag = echo_of({"code": "GFL99", "material": "PLA",
                       "color_rgba": 0xFF0000FF, "nozzle_min_c": 200,
                       "nozzle_max_c": 220}, 5.0)          # the unit's echo
        h.send([bay(tag=tag, spool_id=spool)])
        self.assertEqual(len(h.writes()), 1)
        self.assertEqual(h.entry()["tray_sync"]["state"], "in_sync")
        h.patch_filament(spool, settings_extruder_temp=230)
        h.send([bay(tag=tag, spool_id=spool)])
        self.assertEqual(len(h.writes()), 1)      # not looked at yet
        h.clock.advance(tray.REFRESH_SECONDS)
        h.send([bay(tag=tag, spool_id=spool)])
        h.clock.advance(tray.DEBOUNCE_SECONDS + 0.1)
        h.send([bay(tag=tag, spool_id=spool)])
        self.assertEqual(len(h.writes()), 2)
        self.assertIn("MIN=220 MAX=240", h.writes()[1])

    def test_a_web_edit_reaches_the_unit_through_the_linked_refresh(self):
        h = Harness()
        spool = h.spool(material="PLA", color_hex="FF0000", temp=210)
        h.linked(spool)
        tag = echo_of({"code": "GFL99", "material": "PLA",
                       "color_rgba": 0xFF0000FF, "nozzle_min_c": 200,
                       "nozzle_max_c": 220}, 5.0)
        h.send([bay(tag=tag, spool_id=spool)])
        self.assertEqual(len(h.writes()), 1)
        h.patch_filament(spool, color_hex="00FF00")
        # The periodic re-read of the linked spools (C4 §6) comes well before
        # the sync's own 60 s one.
        self.assertTrue(run(h.linker.refresh_linked()))
        h.send([bay(tag=tag, spool_id=spool)])
        h.clock.advance(tray.DEBOUNCE_SECONDS + 0.1)
        h.send([bay(tag=tag, spool_id=spool)])
        self.assertEqual(len(h.writes()), 2)
        self.assertIn("COLOR=00FF00FF", h.writes()[1])

    def test_the_units_echo_of_a_write_keeps_a_spool_of_another_vendor(self):
        h = Harness()
        spool = h.spool(material="PLA", color_hex="FF0000", vendor="Polymaker")
        h.linked(spool, tag=summary_tag(manufacturer=None, code=None,
                                        material="PLA", color_argb=None))
        written = h.writes()
        self.assertEqual(len(written), 1)
        # The unit reports what it was given; GFL99 names Bambu.
        echo = echo_of({"code": "GFL99", "material": "PLA",
                        "color_rgba": 0xFF0000FF, "nozzle_min_c": 190,
                        "nozzle_max_c": 230}, 9.0)
        before = len(h.api.spools)
        h.send([bay(tag=echo, spool_id=spool, link_source="manual")])
        self.assertEqual(h.entry()["spool_id"], spool)
        self.assertEqual(h.entry()["spool"]["vendor"], "Polymaker")
        self.assertEqual(len(h.api.spools), before)
        # Nor does the unit's own read of a Bambu code, later and unwritten.
        other = summary_tag(code="GFA00", material="PLA",
                            color_argb=0xFFFF0000, read_at=11.0)
        h.send([bay(tag=other, spool_id=spool, link_source="manual")])
        self.assertEqual(h.entry()["spool_id"], spool)
        self.assertEqual(h.entry()["spool"]["vendor"], "Polymaker")
        self.assertEqual(len(h.api.spools), before)

    def test_a_re_link_writes_the_new_spool(self):
        h = Harness()
        first = h.spool(material="PLA", color_hex="FF0000")
        second = h.spool(material="PETG", color_hex="000000")
        h.linked(first)
        h.linked(second)
        self.assertEqual(len(h.writes()), 2)
        self.assertIn("MATERIAL=PETG", h.writes()[1])

    def test_no_write_where_the_tag_is_authoritative(self):
        h = Harness()
        tagged = h.spool(extra={"tray_uid": json.dumps("ab" * 16)})
        h.linked(tagged)
        plain = h.spool()
        bambu = {"uid": "04aabbcc", "format": "bambu", "read_at": 1.0,
                 "spoolman_id": None, "bambu": {}}
        h.send([bay(tag=bambu, spool_id=plain)])
        h.clock.advance(5.0)
        h.send([bay(tag=bambu, spool_id=plain)])
        self.assertEqual(h.writes(), [])
        self.assertNotIn("tray_sync", h.entry())

    def test_no_write_without_a_spool_or_a_link(self):
        h = Harness()
        spool = h.spool()
        for entry in (bay(ready=False, spool_id=spool), bay()):
            h.send([entry])
            h.clock.advance(5.0)
            h.send([entry])
        self.assertEqual(h.writes(), [])

    def test_no_write_with_the_setting_off_or_without_the_capability(self):
        for kwargs in ({"sync": False}, {"capabilities": {"tray_info": True}},
                       {"capabilities": {"tray_write": True}},
                       {"capabilities": None}):
            h = Harness()
            spool = h.spool()
            for _ in range(2):
                h.send([bay(spool_id=spool)], **kwargs)
                h.clock.advance(5.0)
            self.assertEqual(h.writes(), [], kwargs)

    def test_an_absent_setting_means_on(self):
        h = Harness()
        spool = h.spool()
        for _ in range(2):
            h.send([bay(spool_id=spool)], sync=None)
            h.clock.advance(5.0)
        self.assertEqual(len(h.writes()), 1)

    def test_a_positioning_bay_waits(self):
        h = Harness()
        spool = h.spool()
        h.send([bay(spool_id=spool, positioning=True)])
        h.clock.advance(5.0)
        h.send([bay(spool_id=spool, positioning=True)])
        self.assertEqual(h.writes(), [])
        h.send([bay(spool_id=spool)])
        self.assertEqual(len(h.writes()), 1)


class LoopTests(unittest.TestCase):
    def written(self, h, spool):
        h.linked(spool, tag=summary_tag())
        self.assertEqual(len(h.writes()), 1)
        return tray.setting_for(
            ops.spool_summary(run(h.api.request("GET", "/v1/spool/%d"
                                                % spool))),
            tray.setting_of_tag(summary_tag()))

    def test_the_units_echo_is_no_new_spool(self):
        h = Harness()
        # An Acme spool written with a GF code: the echo names Bambu, which
        # the Keep rule would otherwise read as another vendor's spool.
        spool = h.spool(material="PLA", color_hex="FF0000", vendor="Acme")
        setting = self.written(h, spool)
        echo = echo_of(setting, read_at=7.0)
        self.assertEqual(echo["summary"]["manufacturer"], "Bambu")
        before = list(h.sent)
        for read_at in (7.0, 9.0, 30.0):
            h.send([bay(tag=echo_of(setting, read_at), spool_id=spool)])
            h.clock.advance(tray.RETRY_SECONDS)
        self.assertEqual(h.sent[len(before):], [])        # nothing at all
        self.assertEqual(h.entry()["spool_id"], spool)
        self.assertEqual(h.entry()["tray_sync"]["state"], "in_sync")
        self.assertEqual(h.cache.state()["written"]["oams1-2"]["spool_id"],
                         spool)

    def test_a_re_inserted_spool_matches_without_the_manufacturer(self):
        h = Harness()
        spool = h.spool(material="PLA", color_hex="FF0000", vendor="Acme")
        setting = self.written(h, spool)
        h.send([bay(ready=False)])           # removed: unlinked
        self.assertEqual(h.links()[-1], "OAMSM_CLEAR_BAY_SPOOL OAMS=oams1"
                         " BAY=2")
        h.send([bay(tag=echo_of(setting, 50.0))])   # back, read again
        self.assertEqual(h.links()[-1], "OAMSM_SET_BAY_SPOOL OAMS=oams1 BAY=2"
                         " SPOOL=%d SOURCE=tag" % spool)
        self.assertEqual(len(h.writes()), 1)

    def test_the_written_record_survives_a_restart(self):
        h = Harness()
        spool = h.spool(material="PLA", color_hex="FF0000")
        setting = self.written(h, spool)
        state = h.cache.state()
        again = C.SpoolCache(load=lambda: json.loads(json.dumps(state)))
        self.assertEqual(again.written_spool(
            "oams1-2", ops.summary_of(echo_of(setting, 3.0))), spool)
        self.assertIsNone(again.written_spool(
            "oams1-2", ops.summary_of(summary_tag())))

    def test_a_unit_that_keeps_another_setting_gets_at_most_three(self):
        h = Harness()
        spool = h.spool()
        for _ in range(tray.MAX_ATTEMPTS + 3):
            h.send([bay(tag=summary_tag(), spool_id=spool)])
            h.clock.advance(tray.RETRY_SECONDS)
        self.assertEqual(len(h.writes()), tray.MAX_ATTEMPTS)
        entry = h.entry()["tray_sync"]
        self.assertEqual(entry["state"], "failed")
        self.assertIn("after 3 writes", entry["error"])
        # The spool's next change is tried afresh.
        h.patch_filament(spool, color_hex="00FF00")
        run(h.linker._reload_summary(spool))
        for _ in range(2):
            h.send([bay(tag=summary_tag(), spool_id=spool)])
            h.clock.advance(tray.DEBOUNCE_SECONDS + 0.1)
        self.assertEqual(len(h.writes()), tray.MAX_ATTEMPTS + 1)

    def test_a_refused_write_is_retried_later(self):
        h = Harness(fail=RuntimeError("oams1 Spool 3 refused the filament"
                                      " setting: busy."))
        spool = h.spool()
        h.linked(spool)
        self.assertEqual(len(h.writes()), 1)
        self.assertIn("busy", h.entry()["tray_sync"]["error"])
        self.assertEqual(h.entry()["tray_sync"]["state"], "pending")
        h.send([bay(spool_id=spool)])
        self.assertEqual(len(h.writes()), 1)      # not before the retry
        for _ in range(tray.MAX_ATTEMPTS + 2):
            h.clock.advance(tray.RETRY_SECONDS)
            h.send([bay(spool_id=spool)])
        self.assertEqual(len(h.writes()), tray.MAX_ATTEMPTS)
        self.assertEqual(h.entry()["tray_sync"]["state"], "failed")

    def test_invalid_field_is_never_retried(self):
        h = Harness(fail=RuntimeError(
            "oams1 Spool 3 refused the filament setting: invalid_field (it"
            " cannot store this code or material; nothing was written)."))
        spool = h.spool()
        h.linked(spool)
        for _ in range(3):
            h.clock.advance(tray.RETRY_SECONDS)
            h.send([bay(spool_id=spool)])
        self.assertEqual(len(h.writes()), 1)
        entry = h.entry()["tray_sync"]
        self.assertEqual(entry["state"], "failed")
        self.assertIn("invalid_field", entry["error"])


class StatusTests(unittest.TestCase):
    def test_the_status_validates(self):
        if jsonschema is None:
            self.skipTest("jsonschema is not installed")
        path = os.path.join(_ROOT, "docs", "contracts", "schemas",
                            "openams_spoolman_status_v1.schema.json")
        with open(path, encoding="utf-8") as handle:
            validator = jsonschema.Draft202012Validator(json.load(handle))
        h = Harness(fail=RuntimeError("busy"))
        spool = h.spool()
        h.linked(spool)
        validator.validate(h.linker.status())
        self.assertEqual(set(h.entry()["tray_sync"]), {"state", "error"})
        bad = h.linker.status()
        bad["bays"]["oams1-2"]["tray_sync"]["state"] = "maybe"
        with self.assertRaises(jsonschema.ValidationError):
            validator.validate(bad)


# ---------------------------------------------------------------- component


class ComponentTests(unittest.IsolatedAsyncioTestCase):
    async def ready(self, bays):
        h = T.Harness()
        await h.init()
        await h.ready(bays)
        return h

    async def test_the_remote_method_writes_the_spool(self):
        h = T.Harness()
        self.assertIn("openams_spoolman_tray_write", h.server.remote_methods)
        await h.init()
        spool = await make_spool(h.api)
        await h.ready([T.bay(index=0, tag=None)])
        method = h.server.remote_methods["openams_spoolman_tray_write"]
        await method(unit="oams1", bay=0, spool_id=spool)
        self.assertEqual(h.klippy_apis.gcode[-1],
                         "OAMSM_SET_TRAY_INFO OAMS=oams1 BAY=0 CODE=GFG99"
                         " MATERIAL=PETG COLOR=000000FF MIN=230 MAX=250")
        self.assertEqual(
            h.component.linker._cache.state()["written"]["oams1-0"][
                "spool_id"], spool)

    async def test_a_failure_is_reported_to_the_console(self):
        h = await self.ready([T.bay(index=0)])
        method = h.server.remote_methods["openams_spoolman_tray_write"]
        await method(unit="oams1", bay=0, spool_id=999)
        console = [args[0] for event, args in h.server.sent_events
                   if event == "server:gcode_response"]
        self.assertEqual(console, [
            "!! The filament of spool 999 was not written to oams1 Spool 1:"
            " Spoolman has no spool 999"])
        self.assertFalse([g for g in h.klippy_apis.gcode
                          if g.startswith("OAMSM_SET_TRAY_INFO")])

    async def test_timed_passes_are_one_timer(self):
        h = T.Harness(options={"refresh_interval": "0"})   # no refresh timer
        await h.init()
        loop = h.server.get_event_loop()
        h.component._wake(30.0)
        h.component._wake(60.0)            # covered by the earlier one
        self.assertEqual([t.delay for t in loop.timers], [30.0])
        h.component._wake(2.0)             # sooner: replaces it
        self.assertEqual([t.delay for t in loop.timers], [30.0, 2.0])
        self.assertTrue(loop.timers[0].cancelled)
        loop.timers[1].callback()
        self.assertIsNone(h.component._wake_handle)
        await h.flush()


async def make_spool(api):
    """A PETG spool at 240 C."""
    vendor = await api.request("POST", "/v1/vendor", body={"name": "Acme"})
    filament = await api.request("POST", "/v1/filament", body={
        "name": "PETG", "vendor_id": vendor["id"], "material": "PETG",
        "color_hex": "000000", "settings_extruder_temp": 240})
    spool = await api.request("POST", "/v1/spool", body={
        "filament_id": filament["id"], "initial_weight": 1000,
        "remaining_weight": 1000})
    return spool["id"]


if __name__ == "__main__":
    unittest.main()
