#!/usr/bin/env python3
"""Unit tests for the openams_spoolman per-bay linker (C4 §3-§5, §8).

Everything runs against the in-memory ``FakeSpoolman``, a recording
``run_gcode``, and a settable clock; the end-to-end gate is
the S2b gate.
"""

import asyncio
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "moonraker"))

from openams_spoolman import api as A  # noqa: E402
from openams_spoolman import linker as L  # noqa: E402
from openams_spoolman import ops  # noqa: E402

try:
    import jsonschema
except ImportError:
    jsonschema = None

# A Bambu decode as the host publishes it in C2 (all the fields ops.py reads).
BAMBU = {"manufacturer": "Bambu", "type": "PETG", "detailed": "PETG HF",
         "color_argb": 0xFF000000, "weight_g": 1000, "diameter_mm": 1.75,
         "drying_temp_c": 65, "drying_time_h": 8, "bed_temp_c": 70,
         "hotend_max_c": 260, "hotend_min_c": 230, "nozzle_diameter": 0.4,
         "spool_width_mm": 66, "length_m": 330,
         "tray_uid": "0123456789abcdef0123456789abcdef", "production": "240101"}


def bambu_tag(uid="04a1b2c3", read_at=1.0, bambu=None):
    values = dict(BAMBU)
    values.update(bambu or {})
    return {"uid": uid, "format": "bambu", "read_at": read_at,
            "spoolman_id": None, "bambu": values}


def link_tag(spoolman_id, uid="99887766", read_at=1.0):
    return {"uid": uid, "format": "spoolman_link", "read_at": read_at,
            "spoolman_id": spoolman_id, "bambu": None}


def bay(index=0, ready=True, tag=None, spool_id=None, link_source="none"):
    return {"index": index, "ready": ready, "loaded": False, "tag": tag,
            "spool_id": spool_id, "link_source": link_source}


def status(bays, runout="idle", auto_create=True, ready=True):
    return {"ready": ready,
            "settings": {"auto_create_spoolman_spools": auto_create,
                         "apply_pa_on_load": False},
            "topology": {"oams": {"oams1": {"idx": 1, "lane": "fps1"}}},
            "lanes_by_fps": {"fps1": {"op": "UNLOADED", "unit": None,
                                      "runout": runout, "extruder": "extruder"}},
            "devices": {"oams1": {"bays":
                                  [dict(b, index=i) for i, b in enumerate(bays)]}}}


def run(coro):
    return asyncio.run(coro)


class Clock(object):
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Harness(object):
    """A fake Spoolman, a recording gcode channel and a controllable clock."""

    def __init__(self, auto_create=True, material_temps=None, tag_vendor=None):
        self.api = A.FakeSpoolman()
        self.sent = []
        self.clock = Clock()
        self.auto_create = auto_create
        kwargs = {} if tag_vendor is None else {"tag_vendor": tag_vendor}
        self.linker = L.Linker(self.api, self._gcode, clock=self.clock,
                               material_temps=material_temps, **kwargs)

    async def _gcode(self, script):
        self.sent.append(script)

    def start(self):
        run(self.linker.start())
        return self

    def send(self, bays, runout="idle", auto_create=None, ready=True):
        auto = self.auto_create if auto_create is None else auto_create
        run(self.linker.on_status(status(bays, runout=runout, auto_create=auto,
                                         ready=ready)))

    def bay(self, key="oams1-0"):
        return self.linker.status()["bays"][key]

    def spools(self):
        return run(self.api.request("GET", "/v1/spool"))

    def get_spool(self, spool_id):
        return run(self.api.request("GET", "/v1/spool/%d" % spool_id))

    def sets(self):
        return [s for s in self.sent if s.startswith("OAMSM_SET_BAY_SPOOL")]

    def clears(self):
        return [s for s in self.sent if s.startswith("OAMSM_CLEAR_BAY_SPOOL")]

    def filaments(self):
        return [s for s in self.sent
                if s.startswith("OAMSM_SET_BAY_FILAMENT")]

    def make_spool(self, extra=None, material="PETG", extruder_temp=None):
        """A spool already in Spoolman, with optional decoded-extra values."""
        async def build():
            vendor = await self.api.request("POST", "/v1/vendor",
                                            body={"name": "Test"})
            filament = await self.api.request(
                "POST", "/v1/filament",
                body={"name": "Test", "vendor_id": vendor["id"],
                      "material": material,
                      "settings_extruder_temp": extruder_temp})
            spool = await self.api.request(
                "POST", "/v1/spool", body={"filament_id": filament["id"],
                                           "initial_weight": 1000,
                                           "remaining_weight": 1000})
            if not extra:
                return spool
            return await self.api.request(
                "PATCH", "/v1/spool/%d" % spool["id"], body={"extra": extra})
        return run(build())


class ReadyGateTest(unittest.TestCase):
    def test_a_manager_that_is_not_ready_is_ignored(self):
        h = Harness().start()
        h.send([bay(tag=bambu_tag())], ready=False)
        self.assertEqual(h.sent, [])
        self.assertEqual(h.linker.status()["bays"], {})


class CreateAndConfirmTest(unittest.TestCase):
    def test_a_bambu_tag_is_created_linked_and_pending(self):
        h = Harness().start()
        h.send([bay(tag=bambu_tag())])
        b = h.bay()
        self.assertEqual(b["state"], "pending_confirmation")
        self.assertTrue(b["pending_confirmation"])
        self.assertEqual(b["source"], "tag")
        self.assertIsNone(b["error"])
        self.assertEqual(b["spool"]["material"], "PETG")
        self.assertEqual(b["spool"]["vendor"], "Bambu Lab")
        self.assertEqual(b["spool"]["remaining_g"], 1000)
        self.assertEqual(b["spool"]["remaining_pct"], 100)
        self.assertEqual(h.sets(), ["OAMSM_SET_BAY_SPOOL OAMS=oams1 BAY=0 "
                                    "SPOOL=%d SOURCE=tag" % b["spool_id"]])
        self.assertEqual(h.get_spool(b["spool_id"])["location"], "oams1-0")

    def test_confirm_writes_the_weight_and_clears_pending(self):
        h = Harness().start()
        h.send([bay(tag=bambu_tag())])
        spool_id = h.bay()["spool_id"]
        result = run(h.linker.confirm("oams1-0", 750))
        self.assertIsNotNone(result)
        self.assertEqual(result["state"], "linked")
        self.assertFalse(result["pending_confirmation"])
        self.assertEqual(result["source"], "tag")
        self.assertEqual(result["spool"]["remaining_g"], 750)
        self.assertEqual(result["spool"]["remaining_pct"], 75)
        self.assertEqual(h.get_spool(spool_id)["remaining_weight"], 750)

    def test_pending_survives_the_host_echoing_the_link(self):
        h = Harness().start()
        t = bambu_tag()
        h.send([bay(tag=t)])
        spool_id = h.bay()["spool_id"]
        h.send([bay(tag=t, spool_id=spool_id, link_source="tag")])
        self.assertEqual(h.bay()["state"], "pending_confirmation")
        self.assertTrue(h.bay()["pending_confirmation"])
        self.assertIsNotNone(run(h.linker.confirm("oams1-0", 500)))
        h.send([bay(tag=t, spool_id=spool_id, link_source="tag")])
        self.assertEqual(h.bay()["state"], "linked")

    def test_confirm_is_none_for_an_unknown_bay_or_a_wrong_state(self):
        h = Harness().start()
        self.assertIsNone(run(h.linker.confirm("oams1-0", 500)))
        h.send([bay(tag=bambu_tag())])
        run(h.linker.confirm("oams1-0", 800))
        self.assertIsNone(run(h.linker.confirm("oams1-0", 500)))
        self.assertIsNone(run(h.linker.confirm("oams1-7", 500)))


class KnownSpoolTest(unittest.TestCase):
    def test_a_known_tray_links_and_adds_the_new_uid(self):
        h = Harness().start()
        h.send([bay(tag=bambu_tag())])
        spool_id = h.bay()["spool_id"]
        run(h.linker.confirm("oams1-0", 1000))
        h.clock.advance(6)                      # the host link never came back
        h.send([bay()])
        self.assertEqual(h.bay()["state"], "unlinked")
        h.sent.clear()
        before = len(h.spools())
        h.send([bay(tag=bambu_tag(uid="11223344", read_at=5.0))])
        self.assertEqual(len(h.spools()), before)
        self.assertEqual(h.bay()["state"], "linked")
        self.assertEqual(h.bay()["source"], "tag")
        self.assertEqual(h.sets(), ["OAMSM_SET_BAY_SPOOL OAMS=oams1 BAY=0 "
                                    "SPOOL=%d SOURCE=tag" % spool_id])
        self.assertEqual(ops.decode_extra(
            h.get_spool(spool_id)["extra"]["card_uids"]), "04A1B2C3,11223344")

    def test_a_known_card_uid_links(self):
        h = Harness().start()
        spool = h.make_spool({"card_uids": ops.encode_extra("04A1B2C3")})
        h.send([bay(tag=bambu_tag(bambu={"tray_uid": "f" * 32}))])
        self.assertEqual(h.bay()["state"], "linked")
        self.assertEqual(h.bay()["spool_id"], spool["id"])
        self.assertIn("SPOOL=%d " % spool["id"], h.sets()[0] + " ")

    def test_a_different_uid_is_a_new_physical_spool(self):
        h = Harness().start()
        first = h.make_spool({"tray_uid": ops.encode_extra("0" * 32)})
        h.send([bay(tag=bambu_tag(bambu={"tray_uid": "0" * 32}))])
        self.assertEqual(h.bay()["spool_id"], first["id"])
        h.send([bay(tag=bambu_tag(uid="cafebabe", read_at=2.0,
                                  bambu={"tray_uid": "1" * 32}))])
        self.assertEqual(h.bay()["state"], "pending_confirmation")
        self.assertEqual(len(h.spools()), 2)

    def test_relinking_by_tag_clears_the_old_spools_location(self):
        h = Harness().start()
        first = h.make_spool({"tray_uid": ops.encode_extra("0" * 32)})
        h.send([bay(tag=bambu_tag(bambu={"tray_uid": "0" * 32}))])
        self.assertEqual(h.bay()["spool_id"], first["id"])
        self.assertEqual(h.get_spool(first["id"])["location"], "oams1-0")

        # A different tag re-reads on the same bay and relinks it to Y: X
        # (still linked to no bay) must have its location mirror cleared,
        # exactly as a manual relink already clears it via _adopt (C4 §5.6).
        h.send([bay(tag=bambu_tag(uid="cafebabe", read_at=2.0,
                                  bambu={"tray_uid": "1" * 32}))])
        second_id = h.bay()["spool_id"]
        self.assertNotEqual(second_id, first["id"])
        self.assertEqual(h.get_spool(first["id"])["location"], "")
        self.assertEqual(h.get_spool(second_id)["location"], "oams1-0")


class SpoolmanLinkTagTest(unittest.TestCase):
    def test_a_spoolman_link_tag_links_that_id(self):
        h = Harness().start()
        spool = h.make_spool()
        h.send([bay(tag=link_tag(spool["id"]))])
        self.assertEqual(h.bay()["state"], "linked")
        self.assertEqual(h.bay()["spool_id"], spool["id"])
        self.assertIn("SPOOL=%d " % spool["id"], h.sets()[0] + " ")
        self.assertEqual(h.get_spool(spool["id"])["location"], "oams1-0")

    def test_a_link_tag_to_a_missing_spool_is_unlinked_with_an_error(self):
        h = Harness().start()
        h.send([bay(tag=link_tag(4242))])
        self.assertEqual(h.bay()["state"], "unlinked")
        self.assertIn("4242", h.bay()["error"])
        self.assertEqual(h.sets(), [])

    def test_an_unknown_format_or_a_missing_id_does_nothing(self):
        h = Harness().start()
        for fmt in ("unknown", "blank"):
            h.send([bay(tag={"uid": "AA", "format": fmt, "read_at": 1.0,
                             "spoolman_id": None, "bambu": None})])
        h.send([bay(tag=link_tag(None))])       # OpenSpool without an id
        self.assertEqual(h.sent, [])
        self.assertEqual(h.bay()["state"], "unlinked")


class CreationOffTest(unittest.TestCase):
    def test_an_unknown_bambu_tag_stays_unlinked_with_the_error(self):
        h = Harness(auto_create=False).start()
        h.send([bay(tag=bambu_tag())])
        b = h.bay()
        self.assertEqual(b["state"], "unlinked")
        self.assertIn("no Spoolman spool", b["error"])
        self.assertIsNone(b["spool_id"])
        self.assertEqual(b["source"], "none")
        self.assertIsNone(b["spool"])
        self.assertEqual(h.sent, [])
        self.assertEqual(h.spools(), [])


class RemovalTest(unittest.TestCase):
    def setUp(self):
        self.h = Harness().start()
        self.h.send([bay(tag=bambu_tag())])
        self.spool_id = self.h.bay()["spool_id"]
        run(self.h.linker.confirm("oams1-0", 1000))
        # The host now holds what the component asked for.
        self.h.send([bay(tag=bambu_tag(), spool_id=self.spool_id,
                         link_source="tag")])
        self.h.sent.clear()

    def test_a_runout_is_not_an_observed_removal(self):
        self.h.send([bay(ready=False, tag=bambu_tag(), spool_id=self.spool_id,
                         link_source="tag")], runout="pausing")
        self.assertEqual(self.h.clears(), [])
        self.assertEqual(self.h.bay()["state"], "linked")
        # The tag is already gone, but the runout still means "not removed".
        self.h.send([bay(ready=False, tag=None, spool_id=self.spool_id,
                         link_source="tag")], runout="pausing")
        self.assertEqual(self.h.clears(), [])
        self.assertEqual(self.h.bay()["state"], "linked")

    def test_removal_clears_the_link_and_the_location(self):
        self.h.send([bay(ready=False, tag=None, spool_id=self.spool_id,
                         link_source="tag")])
        self.assertEqual(self.h.clears(),
                         ["OAMSM_CLEAR_BAY_SPOOL OAMS=oams1 BAY=0"])
        self.assertEqual(self.h.bay()["state"], "unlinked")
        self.assertIsNone(self.h.bay()["spool_id"])
        self.assertEqual(self.h.bay()["source"], "none")
        self.assertEqual(self.h.get_spool(self.spool_id)["location"], "")

    def test_post_runout_swap_unlinks_when_tag_disappears(self):
        # C4 §3: tagged spool links, runout (ready falls with tag kept), then
        # ready rises with no tag (untagged spool inserted) — must unlink and
        # clear the location mirror.  Finding F-4 #1.
        t = bambu_tag()
        self.h.send([bay(ready=False, tag=t, spool_id=self.spool_id,
                         link_source="tag")], runout="pausing")
        self.assertEqual(self.h.bay()["state"], "linked")
        self.assertEqual(self.h.clears(), [])
        # User removes the empty spool and inserts an untagged replacement.
        self.h.send([bay(ready=True, tag=None, spool_id=self.spool_id,
                         link_source="tag")])
        self.assertEqual(self.h.bay()["state"], "unlinked")
        self.assertIsNone(self.h.bay()["spool_id"])
        self.assertEqual(self.h.bay()["source"], "none")
        self.assertEqual(self.h.clears(),
                         ["OAMSM_CLEAR_BAY_SPOOL OAMS=oams1 BAY=0"])
        self.assertEqual(self.h.get_spool(self.spool_id)["location"], "")


class ManualLinkTest(unittest.TestCase):
    def test_a_manual_link_is_observed_and_mirrors_the_location(self):
        h = Harness().start()
        first = h.make_spool()
        second = h.make_spool()
        h.send([bay(spool_id=first["id"], link_source="manual")])
        self.assertEqual(h.bay()["state"], "linked")
        self.assertEqual(h.bay()["source"], "manual")
        self.assertEqual(h.get_spool(first["id"])["location"], "oams1-0")
        h.send([bay(spool_id=second["id"], link_source="manual")])
        self.assertEqual(h.bay()["spool_id"], second["id"])
        self.assertEqual(h.get_spool(second["id"])["location"], "oams1-0")
        self.assertEqual(h.get_spool(first["id"])["location"], "")
        # The host did the linking (the filament push is not a link command).
        self.assertEqual(h.sets(), [])

    def test_a_manual_link_on_the_loaded_bay_survives_a_runout_edge(self):
        # A status sampled after the F1S fall but before the host recorded
        # the runout: the bay is still the lane's loaded bay.
        h = Harness().start()
        spool = h.make_spool()
        h.send([bay(tag=None, spool_id=spool["id"], link_source="manual")])
        edge = status([bay(ready=False, tag=None, spool_id=spool["id"],
                           link_source="manual")])
        edge["lanes_by_fps"]["fps1"].update({"op": "LOADED", "unit": [1, 0]})
        run(h.linker.on_status(edge))
        self.assertEqual(h.clears(), [])
        self.assertEqual(h.bay()["state"], "linked")

    def test_an_untagged_manual_link_survives_a_runout_only(self):
        h = Harness().start()
        spool = h.make_spool()
        h.send([bay(spool_id=spool["id"], link_source="manual")])
        h.send([bay(ready=False, spool_id=spool["id"], link_source="manual")],
               runout="loading")
        self.assertEqual(h.clears(), [])
        self.assertEqual(h.bay()["state"], "linked")
        h.send([bay(ready=False)])
        self.assertEqual(h.clears(),
                         ["OAMSM_CLEAR_BAY_SPOOL OAMS=oams1 BAY=0"])
        self.assertEqual(h.bay()["state"], "unlinked")
        self.assertEqual(h.get_spool(spool["id"])["location"], "")

    def test_a_re_read_over_a_manual_link_reapplies_the_tag(self):
        h = Harness().start()
        manual = h.make_spool()
        tagged = h.make_spool(
            {"tray_uid": ops.encode_extra(BAMBU["tray_uid"])})
        h.send([bay(tag=bambu_tag(read_at=1.0), spool_id=manual["id"],
                    link_source="manual")])
        # Manual wins: the tag already in the bay does not override the UI.
        self.assertEqual(h.bay()["spool_id"], manual["id"])
        self.assertEqual(h.bay()["source"], "manual")
        self.assertEqual(h.sets(), [])
        h.send([bay(tag=bambu_tag(read_at=2.0), spool_id=manual["id"],
                    link_source="manual")])
        self.assertEqual(h.bay()["spool_id"], tagged["id"])
        self.assertEqual(h.bay()["source"], "tag")
        self.assertEqual(h.sets(), ["OAMSM_SET_BAY_SPOOL OAMS=oams1 BAY=0 "
                                    "SPOOL=%d SOURCE=tag" % tagged["id"]])


class OfflineTest(unittest.TestCase):
    def test_an_offline_bambu_tag_stays_resolving(self):
        # auto_create is on (Harness's default): the miss queues a creation
        # (C4 §6, S2c2), so the error says so and one write is queued.
        h = Harness().start()
        h.api.online = False
        h.send([bay(tag=bambu_tag())])
        b = h.bay()
        self.assertEqual(b["state"], "resolving")
        self.assertEqual(b["error"], "Spoolman offline; creation queued")
        self.assertIsNone(b["spool_id"])
        self.assertEqual(h.sent, [])
        self.assertFalse(h.linker.status()["spoolman_online"])
        self.assertEqual(h.linker.status()["queued_writes"], 1)

    def test_an_offline_bambu_tag_without_auto_create_just_waits(self):
        # With creation off there is nothing to queue; the bay stays
        # resolving with the plain offline error and retries once Spoolman
        # answers again (the retry branch in ``_bay_status``).
        h = Harness(auto_create=False).start()
        h.api.online = False
        h.send([bay(tag=bambu_tag())])
        b = h.bay()
        self.assertEqual(b["state"], "resolving")
        self.assertEqual(b["error"], "Spoolman offline")
        self.assertEqual(h.linker.status()["queued_writes"], 0)
        h.api.online = True
        h.send([bay(tag=bambu_tag())])
        b = h.bay()
        self.assertEqual(b["state"], "unlinked")
        self.assertIn("no Spoolman spool", b["error"])

    def test_a_spoolman_link_tag_links_anyway_when_offline(self):
        h = Harness().start()
        h.api.online = False
        h.send([bay(tag=link_tag(21))])
        self.assertEqual(h.bay()["state"], "linked")
        self.assertEqual(h.bay()["spool_id"], 21)
        self.assertEqual(h.sets(), ["OAMSM_SET_BAY_SPOOL OAMS=oams1 BAY=0 "
                                    "SPOOL=21 SOURCE=tag"])
        self.assertFalse(h.linker.status()["spoolman_online"])


class BayFilamentPushTest(unittest.TestCase):
    """What the component tells the host a bay holds (C4 §7.4): the material
    and working temperature, pushed on change and after a Klipper restart."""

    def test_a_linked_bay_pushes_the_table_temperature_once(self):
        h = Harness().start()
        spool = h.make_spool()
        h.send([bay(spool_id=spool["id"], link_source="manual")])
        self.assertEqual(h.filaments(), [
            'OAMSM_SET_BAY_FILAMENT OAMS=oams1 BAY=0 MATERIAL="PETG" '
            'TEMP=240 SOURCE=table'])
        # Unchanged passes cost nothing.
        for _ in range(3):
            h.send([bay(spool_id=spool["id"], link_source="manual")])
        self.assertEqual(len(h.filaments()), 1)

    def test_the_spools_own_temperature_wins_over_the_table(self):
        h = Harness().start()
        spool = h.make_spool(material="PETG", extruder_temp=232)
        h.send([bay(spool_id=spool["id"], link_source="manual")])
        self.assertEqual(h.filaments(), [
            'OAMSM_SET_BAY_FILAMENT OAMS=oams1 BAY=0 MATERIAL="PETG" '
            'TEMP=232 SOURCE=spool'])

    def test_a_spool_temperature_the_host_could_not_use_falls_back(self):
        h = Harness().start()
        spool = h.make_spool(material="PLA", extruder_temp=1000)
        h.send([bay(spool_id=spool["id"], link_source="manual")])
        self.assertEqual(h.filaments(), [
            'OAMSM_SET_BAY_FILAMENT OAMS=oams1 BAY=0 MATERIAL="PLA" '
            'TEMP=210 SOURCE=table'])

    def test_a_material_variant_falls_back_to_its_base(self):
        h = Harness().start()
        spool = h.make_spool(material="PLA+")
        h.send([bay(spool_id=spool["id"], link_source="manual")])
        self.assertIn('MATERIAL="PLA+" TEMP=210 SOURCE=table',
                      h.filaments()[0])

    def test_an_unknown_material_pushes_nothing(self):
        h = Harness().start()
        spool = h.make_spool(material="Unobtanium")
        h.send([bay(spool_id=spool["id"], link_source="manual")])
        self.assertEqual(h.filaments(), [])

    def test_material_temps_from_the_option_win(self):
        h = Harness(material_temps={"PETG": 235, "PCTG": 250}).start()
        petg = h.make_spool()
        pctg = h.make_spool(material="PCTG")
        h.send([bay(spool_id=petg["id"], link_source="manual")])
        self.assertIn('MATERIAL="PETG" TEMP=235 SOURCE=table',
                      h.filaments()[-1])
        h.send([bay(spool_id=pctg["id"], link_source="manual")])
        self.assertIn('MATERIAL="PCTG" TEMP=250 SOURCE=table',
                      h.filaments()[-1])

    def test_an_unlinked_bay_clears_once(self):
        h = Harness().start()
        spool = h.make_spool()
        h.send([bay(spool_id=spool["id"], link_source="manual")])
        self.assertEqual(len(h.filaments()), 1)
        h.send([bay(spool_id=None)])
        self.assertEqual(h.filaments()[-1],
                         "OAMSM_SET_BAY_FILAMENT OAMS=oams1 BAY=0")
        for _ in range(3):
            h.send([bay(spool_id=None)])
        self.assertEqual(len(h.filaments()), 2)   # the clear is sent once

    def test_another_spool_on_the_same_bay_is_pushed(self):
        h = Harness().start()
        first = h.make_spool(material="PLA", extruder_temp=200)
        second = h.make_spool(material="ABS", extruder_temp=250)
        h.send([bay(spool_id=first["id"], link_source="manual")])
        h.send([bay(spool_id=second["id"], link_source="manual")])
        self.assertEqual(h.filaments()[-1],
                         'OAMSM_SET_BAY_FILAMENT OAMS=oams1 BAY=0 '
                         'MATERIAL="ABS" TEMP=250 SOURCE=spool')

    def test_a_klipper_restart_pushes_every_bay_again(self):
        h = Harness().start()
        first = h.make_spool(material="PLA")
        second = h.make_spool(material="ABS")
        h.send([bay(spool_id=first["id"], link_source="manual"),
               bay(spool_id=second["id"], link_source="manual")])
        pushed = len(h.filaments())
        self.assertEqual(pushed, 2)
        h.linker.forget_filament()
        h.send([bay(spool_id=first["id"], link_source="manual"),
               bay(spool_id=second["id"], link_source="manual")])
        self.assertEqual(len(h.filaments()), 2 * pushed)

    def test_a_refused_command_does_not_break_the_pass(self):
        h = Harness().start()
        first = h.make_spool(material="PLA")
        second = h.make_spool(material="ABS")
        attempted = []

        async def refuse(script):
            attempted.append(script)
            if "SET_BAY_FILAMENT" in script:
                raise RuntimeError("Unknown command")
            h.sent.append(script)
        h.linker._run_gcode = refuse
        # Bay 0's push is refused; bay 1's must still be attempted, and the
        # next pass must try bay 0 again (nothing was recorded).
        h.send([bay(spool_id=first["id"], link_source="manual"),
                bay(spool_id=second["id"], link_source="manual")])
        pushes = [s for s in attempted if "SET_BAY_FILAMENT" in s]
        self.assertEqual(len(pushes), 2)     # one per bay, neither swallowed
        self.assertIn("BAY=1", pushes[-1])
        h.send([bay(spool_id=first["id"], link_source="manual"),
                bay(spool_id=second["id"], link_source="manual")])
        pushes = [s for s in attempted if "SET_BAY_FILAMENT" in s]
        self.assertEqual(len(pushes), 4)

    def test_offline_a_cached_summary_still_pushes(self):
        h = Harness().start()
        spool = h.make_spool()
        h.send([bay(spool_id=spool["id"], link_source="manual")])
        self.assertEqual(len(h.filaments()), 1)
        h.api.online = False
        h.send([bay(spool_id=spool["id"], link_source="manual")])
        self.assertEqual(len(h.filaments()), 1)   # the cache still answers

    def test_offline_without_a_summary_leaves_the_bay_unknown(self):
        h = Harness().start()
        # A link the host already holds, that this component never read.
        h.send([bay(spool_id=7, link_source="manual")])
        self.assertEqual(h.filaments(), [])
        h.api.online = False
        h.send([bay(spool_id=7, link_source="manual")])
        self.assertEqual(h.filaments(), [])

    def test_the_published_spool_object_carries_only_c4_8_fields(self):
        h = Harness().start()
        spool = h.make_spool(material="PLA", extruder_temp=200)
        h.send([bay(spool_id=spool["id"], link_source="manual")])
        published = h.bay()["spool"]
        self.assertEqual(published["extruder_temp"] if "extruder_temp"
                         in published else None, None)
        self.assertEqual(published["material"], "PLA")
        self.assertIn("remaining_g", published)
        if jsonschema is not None:
            path = os.path.join(ROOT, "docs", "contracts", "schemas",
                                "openams_spoolman_status_v1.schema.json")
            with open(path) as handle:
                jsonschema.validate(h.linker.status(), json.load(handle))


class StatusSchemaTest(unittest.TestCase):
    def setUp(self):
        if jsonschema is None:
            self.skipTest("jsonschema is not installed")
        path = os.path.join(ROOT, "docs", "contracts", "schemas",
                            "openams_spoolman_status_v1.schema.json")
        with open(path) as handle:
            self.schema = json.load(handle)

    def validate(self, linker):
        jsonschema.validate(linker.status(), self.schema)

    def test_every_state_validates(self):
        h = Harness().start()
        self.validate(h.linker)
        h.send([bay(tag=bambu_tag())])
        self.validate(h.linker)                 # pending_confirmation
        run(h.linker.confirm("oams1-0", 250))
        self.validate(h.linker)                 # linked
        h.api.online = False
        h.send([bay(tag=bambu_tag(uid="aabbccdd", read_at=9.0))])
        self.validate(h.linker)                 # resolving
        h.api.online = True
        h.send([bay()])
        self.validate(h.linker)

    def test_two_bays_use_one_key_each(self):
        h = Harness().start()
        h.send([bay(tag=bambu_tag()), bay(tag=bambu_tag(uid="aabbccdd",
                                                        read_at=2.0))])
        self.assertEqual(set(h.linker.status()["bays"]),
                         {"oams1-0", "oams1-1"})
        self.validate(h.linker)


class TagVendorTest(unittest.TestCase):
    """The vendor a Bambu tag's new spools are filed under (C4 §5.3)."""

    def vendor_names(self, h):
        return sorted(v["name"] for v in h.api.vendors)

    def test_the_default_is_bambu_lab(self):
        h = Harness().start()
        h.send([bay(tag=bambu_tag())])
        self.assertEqual(self.vendor_names(h), ["Bambu Lab"])
        self.assertEqual(h.bay()["spool"]["vendor"], "Bambu Lab")

    def test_a_new_spool_is_filed_under_the_option(self):
        h = Harness(tag_vendor="My Filaments").start()
        h.send([bay(tag=bambu_tag())])
        self.assertEqual(self.vendor_names(h), ["My Filaments"])
        self.assertEqual(h.bay()["spool"]["vendor"], "My Filaments")
        self.assertEqual(h.api.spools[0]["filament"]["vendor"]["name"],
                         "My Filaments")

    def test_the_filament_lookup_uses_the_same_vendor(self):
        h = Harness(tag_vendor="My Filaments").start()
        h.send([bay(tag=bambu_tag())])
        h.send([bay(tag=bambu_tag()),
                bay(tag=bambu_tag(uid="cafebabe", bambu={"tray_uid": "1" * 32}))])
        # The second spool shares the first one's filament, and no filament
        # was filed under Bambu Lab.
        self.assertEqual(len(h.spools()), 2)
        self.assertEqual(len(h.api.filaments), 1)
        queries = [call[2] for call in h.api.calls
                   if call[0] == "GET" and call[1] == "/v1/filament"]
        self.assertTrue(queries)
        for query in queries:
            self.assertEqual(query.get("vendor.name"), "My Filaments")

    def test_a_filament_of_the_default_vendor_is_not_reused(self):
        h = Harness(tag_vendor="My Filaments").start()
        run(h.api.request("POST", "/v1/vendor", body={"name": "Bambu Lab"}))
        run(h.api.request("POST", "/v1/filament", body={
            "name": "PETG HF", "vendor_id": 1, "material": "PETG",
            "color_hex": "000000"}))
        h.send([bay(tag=bambu_tag())])
        self.assertEqual(h.bay()["spool"]["vendor"], "My Filaments")
        self.assertEqual(len(h.api.filaments), 2)

    def test_a_queued_creation_replays_under_the_option(self):
        h = Harness(tag_vendor="My Filaments").start()
        h.api.online = False
        h.send([bay(tag=bambu_tag())])
        h.api.online = True
        h.clock.advance(L.REPLAY_SECONDS + 1)
        h.send([bay(tag=bambu_tag())])
        self.assertEqual(self.vendor_names(h), ["My Filaments"])
        self.assertEqual(h.bay()["spool"]["vendor"], "My Filaments")


class CardUidWinsTest(unittest.TestCase):
    """A UID a user put on a spool beats the tag's shared tray UID (C4 §4)."""

    def test_a_card_uid_beats_the_tray_uid_of_an_earlier_spool(self):
        h = Harness().start()
        h.send([bay(tag=bambu_tag())])           # auto-created, tray UID T
        auto = h.bay()["spool_id"]
        h.clock.advance(6)
        h.send([bay()])                          # removed
        mine = h.make_spool({"card_uids": ops.encode_extra("DEADBEEF")})
        h.send([bay(tag=bambu_tag(uid="deadbeef", read_at=3.0))])
        self.assertEqual(h.bay()["spool_id"], mine["id"])
        self.assertNotEqual(mine["id"], auto)
        self.assertEqual(h.bay()["spool"]["vendor"], "Test")
        self.assertEqual(len(h.spools()), 2)

    def test_a_card_uid_beats_a_tray_uid_spool_listed_first(self):
        tag = bambu_tag(uid="deadbeef")
        first = {"id": 1, "extra": {"tray_uid": ops.encode_extra(
            tag["bambu"]["tray_uid"])}}
        second = {"id": 2, "extra": {"card_uids": ops.encode_extra("DEADBEEF")}}
        found = run(ops.find_spool_by_tag(None, tag, [first, second]))
        self.assertEqual(found["id"], 2)
        # Without the UID on any spool the tray UID still finds the spool.
        other = bambu_tag(uid="0badf00d")
        self.assertEqual(
            run(ops.find_spool_by_tag(None, other, [first, second]))["id"], 1)

    def test_both_tags_of_one_genuine_spool_find_the_same_spool(self):
        h = Harness().start()
        h.send([bay(tag=bambu_tag())])
        spool_id = h.bay()["spool_id"]
        h.send([bay(), bay(tag=bambu_tag(uid="11223344", read_at=2.0))])
        self.assertEqual(h.bay("oams1-1")["spool_id"], spool_id)
        self.assertEqual(ops.decode_extra(
            h.get_spool(spool_id)["extra"]["card_uids"]), "04A1B2C3,11223344")


def change_vendor(h, spool_id, name="Polymaker"):
    """What Spoolman's web UI does: the spool's filament gets another vendor."""
    api = h.api
    vendor = {"id": api._next_id("vendor"), "name": name, "extra": {}}
    api.vendors.append(vendor)
    spool = [s for s in api.spools if s["id"] == spool_id][0]
    spool["filament"]["vendor"] = vendor


class SpoolmanWinsTest(unittest.TestCase):
    """A vendor changed in Spoolman survives every way a tag is read again
    (C4 §4, §6), and a web edit shows without one."""

    def setUp(self):
        self.h = Harness().start()
        self.h.send([bay(tag=bambu_tag())])
        self.spool_id = self.h.bay()["spool_id"]
        self.linked = dict(spool_id=self.spool_id, link_source="tag")
        change_vendor(self.h, self.spool_id)

    def vendor(self, key="oams1-0"):
        return (self.h.bay(key)["spool"] or {}).get("vendor")

    def test_a_web_edit_shows_once_the_linked_spools_are_re_read(self):
        self.assertEqual(self.vendor(), "Bambu Lab")      # stale until then
        self.assertTrue(run(self.h.linker.refresh_linked()))
        self.assertEqual(self.vendor(), "Polymaker")
        self.assertFalse(run(self.h.linker.refresh_linked()))   # unchanged

    def test_a_web_edit_of_the_filament_shows_too(self):
        spool = [s for s in self.h.api.spools if s["id"] == self.spool_id][0]
        spool["filament"]["material"] = "ASA"
        spool["filament"]["color_hex"] = "00ff00"
        run(self.h.linker.refresh_linked())
        shown = self.h.bay()["spool"]
        self.assertEqual((shown["material"], shown["color_hex"]),
                         ("ASA", "00FF00"))

    def test_a_re_read_of_the_same_tag_keeps_it(self):
        self.h.send([bay(tag=bambu_tag(read_at=2.0), **self.linked)])
        run(self.h.linker.refresh_linked())
        self.assertEqual(self.vendor(), "Polymaker")
        self.assertEqual(len(self.h.spools()), 1)

    def test_removal_and_insertion_keep_it(self):
        self.h.send([bay(ready=False, **self.linked)])
        self.h.send([bay(tag=bambu_tag(read_at=3.0))])
        self.assertEqual(self.h.bay()["spool_id"], self.spool_id)
        self.assertEqual(self.vendor(), "Polymaker")
        self.assertEqual(len(self.h.spools()), 1)

    def test_the_second_tag_of_the_spool_keeps_it(self):
        self.h.send([bay(tag=bambu_tag(uid="11223344", read_at=2.0),
                         **self.linked)])
        self.h.send([bay(ready=False, **self.linked)])
        self.h.send([bay(tag=bambu_tag(uid="11223344", read_at=3.0))])
        self.assertEqual(self.h.bay()["spool_id"], self.spool_id)
        self.assertEqual(self.vendor(), "Polymaker")
        self.assertEqual(len(self.h.spools()), 1)

    def test_refresh_pushes_what_depends_on_the_summary(self):
        self.h.send([bay(tag=bambu_tag(), **self.linked)])
        spool = [s for s in self.h.api.spools if s["id"] == self.spool_id][0]
        spool["filament"]["material"] = "ASA"
        self.h.sent.clear()
        self.assertTrue(run(self.h.linker.refresh_linked()))
        self.h.send([bay(tag=bambu_tag(), **self.linked)])
        self.assertEqual(len(self.h.filaments()), 1)
        self.assertIn('MATERIAL="ASA"', self.h.filaments()[0])

    def test_refresh_reports_spoolman_going_away_and_coming_back(self):
        self.h.api.online = False
        self.assertTrue(run(self.h.linker.refresh_linked()))
        self.assertFalse(self.h.linker.status()["spoolman_online"])
        self.assertFalse(run(self.h.linker.refresh_linked()))   # still away
        self.h.api.online = True
        self.assertTrue(run(self.h.linker.refresh_linked()))
        self.assertTrue(self.h.linker.status()["spoolman_online"])

    def test_refresh_costs_nothing_while_nothing_is_linked(self):
        h = Harness().start()
        h.send([bay()])
        h.api.calls.clear()
        self.assertFalse(run(h.linker.refresh_linked()))
        self.assertEqual(h.api.calls, [])

    def test_refresh_is_one_request_for_every_bay(self):
        self.h.send([bay(tag=bambu_tag(), **self.linked),
                     bay(tag=bambu_tag(uid="cafebabe",
                                       bambu={"tray_uid": "1" * 32}))])
        self.h.api.calls.clear()
        run(self.h.linker.refresh_linked())
        self.assertEqual([c[:2] for c in self.h.api.calls],
                         [("GET", "/v1/spool")])


if __name__ == "__main__":
    unittest.main()
