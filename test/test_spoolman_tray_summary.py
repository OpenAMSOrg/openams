#!/usr/bin/env python3
"""Linking a unit-decoded tag summary (C4 §4.1).

A unit that decodes its own tags (C1 §4.7.1) reports a ``unit_summary`` tag:
filament code, material, colour, nozzle range, and no UID. With no tray UID a
spool cannot be identified uniquely, so the linker keeps a link that still
matches, links only when exactly one Spoolman spool can be it, offers the
candidates when several can, and creates a spool only when none can. Against
the in-memory ``FakeSpoolman``, like ``test_spoolman_linker.py``. Run with::

    python3 -m unittest discover -s test -p 'test_spoolman_tray_summary.py' -v
"""

import json
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "moonraker"))
sys.path.insert(0, _HERE)

from openams_spoolman import linker as L           # noqa: E402
from openams_spoolman import ops                   # noqa: E402
from test_spoolman_linker import Harness, bay, run, status  # noqa: E402

try:
    import jsonschema
except ImportError:
    jsonschema = None


def summary_tag(material="ABS", color_argb=0xFF112233, code="GFB60",
                manufacturer="Bambu", read_at=1.0, nozzle=(240, 270)):
    """C2 §4.6's ``unit_summary`` tag, as the host publishes it."""
    return {"uid": None, "format": "unit_summary", "read_at": read_at,
            "spoolman_id": None, "bambu": None,
            "summary": {"code": code, "material": material,
                        "color_argb": color_argb, "nozzle_min_c": nozzle[0],
                        "nozzle_max_c": nozzle[1],
                        "manufacturer": manufacturer}}


def add_spool(harness, material="ABS", color_hex="112233",
              vendor="Bambu Lab", location="", archived=False,
              filament_weight=None):
    """A spool already in Spoolman, with the filament it names."""
    async def build():
        api = harness.api
        was_online, api.online = api.online, True    # a spool made by others
        try:
            return await make(api)
        finally:
            api.online = was_online

    async def make(api):
        vendors = await api.request("GET", "/v1/vendor",
                                    query={"name": vendor})
        made = vendors[0] if vendors else await api.request(
            "POST", "/v1/vendor", body={"name": vendor})
        filament = await api.request("POST", "/v1/filament", body={
            "name": material, "vendor_id": made["id"], "material": material,
            "color_hex": color_hex, "weight": filament_weight})
        spool = await api.request("POST", "/v1/spool", body={
            "filament_id": filament["id"], "initial_weight": 1000,
            "remaining_weight": 800, "location": location})
        if archived:
            api.spools[-1]["archived"] = True
        return spool["id"]
    return run(build())


def summary_status(harness, bays, **kwargs):
    harness.send(bays, **kwargs)


class Base(unittest.TestCase):
    def setUp(self):
        self.h = Harness().start()

    def bay0(self):
        return self.h.bay("oams1-0")

    def spool_count(self):
        return len(self.h.api.spools)


class KeepTests(Base):
    def test_a_matching_link_of_any_source_is_kept(self):
        spool = add_spool(self.h)
        for source in ("manual", "tag"):
            self.setUp()
            spool = add_spool(self.h)
            self.h.send([bay(spool_id=spool, link_source=source)])
            self.assertEqual(self.bay0()["spool_id"], spool)
            before = list(self.h.sent)
            self.h.send([bay(tag=summary_tag(), spool_id=spool,
                             link_source=source)])
            self.assertEqual(self.h.sent, before, source)    # nothing sent
            self.assertEqual(self.bay0()["state"], "linked")
            self.assertEqual(self.bay0()["spool_id"], spool)
            self.assertEqual(self.spool_count(), 1)

    def test_keep_survives_a_new_read_of_the_same_spool(self):
        spool = add_spool(self.h)
        self.h.send([bay(tag=summary_tag(read_at=1.0), spool_id=spool,
                         link_source="manual")])
        self.h.send([bay(tag=summary_tag(read_at=9.0), spool_id=spool,
                         link_source="manual")])
        self.assertEqual(self.h.sets(), [])
        self.assertEqual(self.bay0()["source"], "manual")

    def test_keep_leaves_pending_confirmation_alone(self):
        self.h.send([bay(tag=summary_tag())])
        first = self.bay0()
        self.assertEqual(first["state"], "pending_confirmation")
        self.h.send([bay(tag=summary_tag(read_at=5.0), spool_id=first["spool_id"],
                         link_source="tag")])
        self.assertEqual(self.bay0()["state"], "pending_confirmation")
        self.assertEqual(self.spool_count(), 1)

    def test_vendor_is_compared_only_when_the_manufacturer_is_known(self):
        spool = add_spool(self.h, vendor="Acme")
        self.h.send([bay(spool_id=spool, link_source="manual")])
        # A Bambu filament code names Bambu: an Acme spool does not match.
        self.h.send([bay(tag=summary_tag(), spool_id=spool,
                         link_source="manual")])
        self.assertNotEqual(self.bay0()["spool_id"], spool)
        # An unknown manufacturer matches any vendor.
        self.setUp()
        spool = add_spool(self.h, vendor="Acme")
        self.h.send([bay(spool_id=spool, link_source="manual")])
        self.h.send([bay(tag=summary_tag(manufacturer=None, code="X1"),
                         spool_id=spool, link_source="manual")])
        self.assertEqual(self.bay0()["spool_id"], spool)
        self.assertEqual(self.h.sets(), [])

    def test_a_link_that_no_longer_fits_is_re_matched(self):
        old = add_spool(self.h, material="PLA", color_hex="FF0000")
        new = add_spool(self.h)
        self.h.send([bay(spool_id=old, link_source="manual")])
        self.h.send([bay(tag=summary_tag(), spool_id=old,
                         link_source="manual")])
        self.assertEqual(self.bay0()["spool_id"], new)
        self.assertEqual(self.bay0()["source"], "tag")
        self.assertEqual(self.h.get_spool(old)["location"], "")
        self.assertEqual(self.h.get_spool(new)["location"], "oams1-0")

    def test_a_link_that_does_not_fit_and_has_no_match_is_cleared(self):
        old = add_spool(self.h, material="PLA", color_hex="FF0000")
        self.h.send([bay(spool_id=old, link_source="manual")])
        self.h.send([bay(tag=summary_tag(), spool_id=old,
                         link_source="manual")], auto_create=False)
        self.assertEqual(self.bay0()["state"], "unlinked")
        self.assertEqual(len(self.h.clears()), 1)
        self.assertEqual(self.h.get_spool(old)["location"], "")


class MatchTests(Base):
    def test_exactly_one_matching_spool_is_linked_as_a_tag_link(self):
        add_spool(self.h, material="PLA", color_hex="FF0000")   # not a match
        wanted = add_spool(self.h)
        self.h.send([bay(tag=summary_tag())])
        b = self.bay0()
        self.assertEqual((b["state"], b["source"], b["spool_id"]),
                         ("linked", "tag", wanted))
        self.assertFalse(b["pending_confirmation"])
        self.assertIsNone(b["error"])
        self.assertNotIn("suggestions", b)
        self.assertEqual(self.h.sets(), [
            "OAMSM_SET_BAY_SPOOL OAMS=oams1 BAY=0 SPOOL=%d SOURCE=tag"
            % wanted])
        self.assertEqual(self.h.get_spool(wanted)["location"], "oams1-0")
        self.assertEqual(self.spool_count(), 2)          # nothing created
        self.assertEqual(b["spool"]["remaining_g"], 800)

    def test_the_spool_located_at_this_bay_is_preferred(self):
        add_spool(self.h, location="Shelf")
        here = add_spool(self.h, location="oams1-0")
        add_spool(self.h)
        self.h.send([bay(tag=summary_tag())])
        self.assertEqual(self.bay0()["spool_id"], here)
        self.assertEqual(self.bay0()["state"], "linked")

    def test_a_spool_linked_to_another_bay_is_not_a_candidate(self):
        taken = add_spool(self.h, location="oams1-1")
        free = add_spool(self.h)
        self.h.send([bay(tag=summary_tag()),
                     bay(spool_id=taken, link_source="manual")])
        self.assertEqual(self.bay0()["spool_id"], free)

    def test_a_spool_another_bay_machine_holds_is_not_a_candidate(self):
        add_spool(self.h)
        self.h.send([bay(tag=summary_tag()),
                     bay(tag=summary_tag(read_at=2.0))])
        first, second = self.h.bay("oams1-0"), self.h.bay("oams1-1")
        self.assertEqual(first["state"], "linked")
        # The one matching spool is bay 0's; bay 1 must not take it too.
        self.assertNotEqual(second["spool_id"], first["spool_id"])

    def test_an_archived_spool_is_not_a_candidate(self):
        add_spool(self.h, archived=True)
        self.h.send([bay(tag=summary_tag())], auto_create=False)
        self.assertEqual(self.bay0()["state"], "unlinked")
        self.assertEqual(self.h.sets(), [])

    def test_a_different_colour_or_material_is_not_a_candidate(self):
        add_spool(self.h, color_hex="445566")
        add_spool(self.h, material="ASA")
        self.h.send([bay(tag=summary_tag())], auto_create=False)
        self.assertEqual(self.bay0()["state"], "unlinked")

    def test_the_material_and_colour_compare_loosely(self):
        wanted = add_spool(self.h, material="abs", color_hex="112233")
        self.h.send([bay(tag=summary_tag(material=" ABS "))])
        self.assertEqual(self.bay0()["spool_id"], wanted)

    def test_a_summary_without_a_colour_matches_any_colour(self):
        wanted = add_spool(self.h, color_hex="ABCDEF")
        self.h.send([bay(tag=summary_tag(color_argb=None))])
        self.assertEqual(self.bay0()["spool_id"], wanted)


class AmbiguousTests(Base):
    def test_several_matches_never_guess_and_offer_suggestions(self):
        first = add_spool(self.h, location="Dry box")
        second = add_spool(self.h)
        self.h.send([bay(tag=summary_tag())])
        b = self.bay0()
        self.assertEqual(b["state"], "unlinked")
        self.assertEqual(b["source"], "none")
        self.assertIsNone(b["spool_id"])
        self.assertIn("2 Spoolman spools match", b["error"])
        self.assertEqual([s["spool_id"] for s in b["suggestions"]],
                         [first, second])
        self.assertEqual(b["suggestions"][0], {
            "spool_id": first, "material": "ABS", "color_hex": "112233",
            "vendor": "Bambu Lab", "remaining_g": 800, "location": "Dry box"})
        self.assertEqual(self.h.sets(), [])
        self.assertEqual(self.spool_count(), 2)              # none created
        self.assertEqual(self.h.get_spool(first)["location"], "Dry box")

    def test_several_spools_at_this_bay_are_still_ambiguous(self):
        add_spool(self.h, location="oams1-0")
        add_spool(self.h, location="oams1-0")
        self.h.send([bay(tag=summary_tag())])
        self.assertEqual(self.bay0()["state"], "unlinked")
        self.assertEqual(len(self.bay0()["suggestions"]), 2)

    def test_ambiguity_never_creates_even_with_creation_on(self):
        add_spool(self.h)
        add_spool(self.h)
        self.h.send([bay(tag=summary_tag())], auto_create=True)
        self.assertEqual(self.spool_count(), 2)

    def test_a_manual_assignment_ends_the_suggestions(self):
        first = add_spool(self.h)
        add_spool(self.h)
        tag = summary_tag()
        self.h.send([bay(tag=tag)])
        self.assertIn("suggestions", self.bay0())
        self.h.send([bay(tag=tag, spool_id=first, link_source="manual")])
        b = self.bay0()
        self.assertEqual((b["state"], b["source"], b["spool_id"]),
                         ("linked", "manual", first))
        self.assertNotIn("suggestions", b)

    def test_the_suggestions_are_capped(self):
        for _ in range(L.MAX_SUGGESTIONS + 3):
            add_spool(self.h)
        self.h.send([bay(tag=summary_tag())])
        self.assertEqual(len(self.bay0()["suggestions"]), L.MAX_SUGGESTIONS)

    def test_a_link_that_does_not_fit_and_is_ambiguous_is_cleared(self):
        old = add_spool(self.h, material="PLA", color_hex="FF0000")
        add_spool(self.h)
        add_spool(self.h)
        self.h.send([bay(spool_id=old, link_source="manual")])
        self.h.send([bay(tag=summary_tag(), spool_id=old,
                         link_source="manual")])
        b = self.bay0()
        self.assertEqual(b["state"], "unlinked")
        self.assertEqual(len(b["suggestions"]), 2)
        self.assertEqual(len(self.h.clears()), 1)

    def test_the_status_validates_against_the_schema(self):
        if jsonschema is None:
            self.skipTest("jsonschema is not installed")
        add_spool(self.h)
        add_spool(self.h)
        self.h.send([bay(tag=summary_tag())])
        path = os.path.join(_ROOT, "docs", "contracts", "schemas",
                            "openams_spoolman_status_v1.schema.json")
        with open(path, encoding="utf-8") as handle:
            schema = json.load(handle)
        jsonschema.validate(self.h.linker.status(), schema)


class CreateTests(Base):
    def test_no_match_creates_vendor_filament_and_spool_pending(self):
        self.h.send([bay(tag=summary_tag())])
        b = self.bay0()
        self.assertEqual(b["state"], "pending_confirmation")
        self.assertTrue(b["pending_confirmation"])
        self.assertEqual(b["source"], "tag")
        self.assertEqual(b["spool"]["material"], "ABS")
        self.assertEqual(b["spool"]["color_hex"], "112233")
        self.assertEqual(b["spool"]["vendor"], "Bambu Lab")
        self.assertEqual(b["spool"]["remaining_g"], 1000)
        spool = self.h.get_spool(b["spool_id"])
        self.assertEqual(spool["location"], "oams1-0")
        # There is no tray UID or tag UID to record.
        self.assertEqual(spool["extra"], {})
        self.assertEqual(spool["filament"]["settings_extruder_temp"], 255)
        self.assertEqual(self.h.sets(), [
            "OAMSM_SET_BAY_SPOOL OAMS=oams1 BAY=0 SPOOL=%d SOURCE=tag"
            % b["spool_id"]])

    def test_an_unknown_manufacturer_uses_the_generic_vendor(self):
        self.h.send([bay(tag=summary_tag(manufacturer=None, code="X1"))])
        self.assertEqual(self.bay0()["spool"]["vendor"], "Generic")

    def test_confirmation_ends_pending_as_for_a_bambu_tag(self):
        self.h.send([bay(tag=summary_tag())])
        result = run(self.h.linker.confirm("oams1-0", 600))
        self.assertEqual(result["state"], "linked")
        self.assertEqual(result["spool"]["remaining_g"], 600)

    def test_the_default_weight_is_the_filaments_own(self):
        # A filament that already exists, with no spool, supplies its weight.
        async def filament():
            vendor = await self.h.api.request(
                "POST", "/v1/vendor", body={"name": "Bambu Lab"})
            await self.h.api.request("POST", "/v1/filament", body={
                "name": "ABS", "vendor_id": vendor["id"], "material": "ABS",
                "color_hex": "112233", "weight": 750})
        run(filament())
        self.h.send([bay(tag=summary_tag())])
        b = self.bay0()
        self.assertEqual(b["spool"]["remaining_g"], 750)
        self.assertEqual(b["spool"]["initial_g"], 750)
        self.assertEqual(len(self.h.api.filaments), 1)    # reused

    def test_creation_off_ends_unlinked_with_an_error(self):
        self.h.send([bay(tag=summary_tag())], auto_create=False)
        b = self.bay0()
        self.assertEqual(b["state"], "unlinked")
        self.assertEqual(b["error"], L.NO_SPOOL_ERROR)
        self.assertEqual(self.spool_count(), 0)
        self.assertEqual(self.h.api.vendors, [])
        self.assertEqual(self.h.sets(), [])
        self.assertNotIn("suggestions", b)

    def test_a_summary_without_a_material_is_not_acted_on(self):
        self.h.send([bay(tag=summary_tag(material=None))])
        self.assertEqual(self.bay0()["state"], "unlinked")
        self.assertEqual(self.spool_count(), 0)
        self.assertEqual(self.h.sent, [])

    def test_a_removed_spool_unlinks_a_tag_link(self):
        self.h.send([bay(tag=summary_tag())])
        spool_id = self.bay0()["spool_id"]
        self.h.send([bay(tag=summary_tag(), spool_id=spool_id,
                         link_source="tag")])
        self.h.send([bay(ready=False, tag=None, spool_id=spool_id,
                         link_source="tag")])
        self.assertEqual(self.bay0()["state"], "unlinked")
        self.assertEqual(len(self.h.clears()), 1)
        self.assertEqual(self.h.get_spool(spool_id)["location"], "")

    def test_a_read_that_finds_no_tag_unlinks_a_tag_link(self):
        self.h.send([bay(tag=summary_tag())])
        spool_id = self.bay0()["spool_id"]
        self.h.send([bay(tag=summary_tag(), spool_id=spool_id,
                         link_source="tag")])
        self.h.send([bay(tag=None, spool_id=spool_id, link_source="tag")])
        self.assertEqual(self.bay0()["state"], "unlinked")


class OfflineTests(Base):
    def test_a_known_bay_and_summary_links_from_the_cache(self):
        spool = add_spool(self.h)
        self.h.send([bay(tag=summary_tag())])
        self.assertEqual(self.bay0()["spool_id"], spool)
        # The spool leaves, Spoolman goes away, the same spool is back.
        self.h.send([bay(ready=False, tag=None, spool_id=spool,
                         link_source="tag")])
        self.assertEqual(self.bay0()["state"], "unlinked")
        self.h.api.online = False
        self.h.send([bay(tag=summary_tag(read_at=7.0))])
        b = self.bay0()
        self.assertEqual((b["state"], b["spool_id"], b["source"]),
                         ("linked", spool, "tag"))
        self.assertFalse(self.h.linker.status()["spoolman_online"])
        self.assertEqual(self.h.sets()[-1],
                         "OAMSM_SET_BAY_SPOOL OAMS=oams1 BAY=0 SPOOL=%d"
                         " SOURCE=tag" % spool)
        # The location mirror waits in the queue and replays later.
        self.assertTrue(self.h.linker.status()["queued_writes"])
        self.h.api.online = True
        self.h.clock.advance(L.REPLAY_SECONDS + 1)
        self.h.send([bay(tag=summary_tag(read_at=7.0), spool_id=spool,
                         link_source="tag")])
        self.assertEqual(self.h.get_spool(spool)["location"], "oams1-0")

    def test_the_cache_is_keyed_by_bay_and_summary(self):
        spool = add_spool(self.h)
        self.h.send([bay(tag=summary_tag())])
        self.assertEqual(self.bay0()["spool_id"], spool)
        self.h.send([bay(ready=False, tag=None, spool_id=spool,
                         link_source="tag")])
        self.h.api.online = False
        links = len(self.h.sets())
        # Another summary on the same bay is a miss: with creation off it
        # waits, and nothing is linked on a guess.
        self.h.send([bay(tag=summary_tag(color_argb=0xFFFFFFFF, read_at=3.0))],
                    auto_create=False)
        self.assertEqual(self.bay0()["state"], "resolving")
        self.assertEqual(self.bay0()["error"], L.OFFLINE_ERROR)
        self.assertEqual(len(self.h.sets()), links)

    def test_a_miss_queues_the_creation_and_replays_it_when_online(self):
        self.h.api.online = False
        self.h.send([bay(tag=summary_tag())])
        b = self.bay0()
        self.assertEqual(b["state"], "resolving")
        self.assertEqual(b["error"], L.OFFLINE_QUEUED_ERROR)
        self.assertEqual(self.h.sets(), [])
        self.assertEqual(self.h.linker.status()["queued_writes"], 1)
        # One creation per bay, however often the read is retried.
        self.h.send([bay(tag=summary_tag())])
        self.assertEqual(self.h.linker.status()["queued_writes"], 1)
        self.h.api.online = True
        self.h.clock.advance(L.REPLAY_SECONDS + 1)
        self.h.send([bay(tag=summary_tag())])
        b = self.bay0()
        self.assertEqual(b["state"], "pending_confirmation")
        self.assertEqual(self.spool_count(), 1)
        self.assertEqual(self.h.linker.status()["queued_writes"], 0)

    def test_a_queued_creation_matches_a_spool_made_meanwhile(self):
        self.h.api.online = False
        self.h.send([bay(tag=summary_tag())])
        spool = add_spool(self.h)          # appeared while Spoolman was away
        self.h.api.online = True
        self.h.clock.advance(L.REPLAY_SECONDS + 1)
        self.h.send([bay(tag=summary_tag())])
        self.assertEqual(self.bay0()["spool_id"], spool)
        self.assertEqual(self.bay0()["state"], "linked")
        self.assertEqual(self.spool_count(), 1)       # no duplicate

    def test_a_queued_creation_for_a_read_that_moved_on_is_dropped(self):
        self.h.api.online = False
        self.h.send([bay(tag=summary_tag())])
        self.h.send([bay(ready=False, tag=None)])
        self.h.api.online = True
        self.h.clock.advance(L.REPLAY_SECONDS + 1)
        self.h.send([bay(ready=False, tag=None)])
        self.assertEqual(self.spool_count(), 0)

    def test_offline_without_creation_waits_and_retries_when_online(self):
        spool = add_spool(self.h)
        self.h.api.online = False
        self.h.send([bay(tag=summary_tag())], auto_create=False)
        self.assertEqual(self.bay0()["state"], "resolving")
        self.h.api.online = True
        self.h.send([bay(tag=summary_tag())], auto_create=False)
        self.assertEqual(self.bay0()["spool_id"], spool)

    def test_offline_a_matching_link_is_kept(self):
        spool = add_spool(self.h)
        self.h.send([bay(spool_id=spool, link_source="manual")])
        self.h.api.online = False
        self.h.send([bay(tag=summary_tag(read_at=4.0), spool_id=spool,
                         link_source="manual")])
        self.assertEqual(self.bay0()["state"], "linked")
        self.assertEqual(self.h.sets(), [])


class HelperTests(unittest.TestCase):
    def test_summary_of_normalizes_the_colour(self):
        summary = ops.summary_of(summary_tag(color_argb=0x80ABCDEF))
        self.assertEqual(summary["color_hex"], "ABCDEF")
        self.assertIsNone(ops.summary_of(
            summary_tag(color_argb=None))["color_hex"])

    def test_the_fingerprint_ignores_case_and_spacing_of_the_material(self):
        a = ops.summary_of(summary_tag(material="ABS"))
        b = ops.summary_of(summary_tag(material=" abs "))
        self.assertEqual(ops.summary_fingerprint(a),
                         ops.summary_fingerprint(b))

    def test_a_uid_less_tag_does_not_disturb_the_bambu_lookups(self):
        from openams_spoolman.cache import SpoolCache
        cache = SpoolCache()
        self.assertIsNone(cache.lookup(summary_tag()))
        run(self._find(summary_tag()))

    @staticmethod
    async def _find(tag):
        from openams_spoolman import api as A
        assert await ops.find_spool_by_tag(A.FakeSpoolman(), tag) is None


if __name__ == "__main__":
    unittest.main()
