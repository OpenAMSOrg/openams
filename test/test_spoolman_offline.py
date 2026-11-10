#!/usr/bin/env python3
"""Unit tests for the openams_spoolman offline cache, write queue and replay
(C4 §6). Everything runs against the in-memory ``FakeSpoolman`` wrapped to go
offline on demand, a recording ``run_gcode``, and a settable clock; the
end-to-end gate is the S2c2 gate.
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
from openams_spoolman.cache import SpoolCache  # noqa: E402

try:
    import jsonschema
except ImportError:
    jsonschema = None

BAMBU = {"manufacturer": "Bambu", "type": "PETG", "detailed": "PETG HF",
         "color_argb": 0xFF000000, "weight_g": 1000, "diameter_mm": 1.75,
         "drying_temp_c": 65, "drying_time_h": 8, "bed_temp_c": 70,
         "hotend_max_c": 260, "hotend_min_c": 230, "nozzle_diameter": 0.4,
         "spool_width_mm": 66, "length_m": 330,
         "tray_uid": "0123456789abcdef0123456789abcdef", "production": "240101"}


def bambu_tag(uid="04a1b2c3", read_at=1.0, tray=None):
    values = dict(BAMBU)
    if tray is not None:
        values["tray_uid"] = tray
    return {"uid": uid, "format": "bambu", "read_at": read_at,
            "spoolman_id": None, "bambu": values}


def bay(index=0, ready=True, tag=None, spool_id=None, link_source="none"):
    return {"index": index, "ready": ready, "loaded": False, "tag": tag,
            "spool_id": spool_id, "link_source": link_source}


def status(bays, auto_create=True, ready=True):
    return {"ready": ready,
            "settings": {"auto_create_spoolman_spools": auto_create,
                         "apply_pa_on_load": False},
            "topology": {"oams": {"oams1": {"idx": 1, "lane": "fps1"}}},
            "lanes_by_fps": {"fps1": {"op": "UNLOADED", "unit": None,
                                      "runout": "idle", "extruder": "extruder"}},
            "devices": {"oams1": {"bays":
                                  [dict(b, index=i) for i, b in enumerate(bays)]}}}


def run(coro):
    return asyncio.run(coro)


class Flaky(A.FakeSpoolman):
    """The in-memory Spoolman, made unavailable on demand (a flag on top)."""

    def __init__(self):
        A.FakeSpoolman.__init__(self)
        self.down = False

    async def request(self, method, path, query=None, body=None):
        if self.down:
            raise A.SpoolmanUnavailable("down for maintenance")
        return await A.FakeSpoolman.request(self, method, path, query, body)


class Clock(object):
    def __init__(self, now=100.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Harness(object):
    def __init__(self, auto_create=True, store=None):
        self.api = Flaky()
        self.sent = []
        self.clock = Clock()
        self.auto_create = auto_create
        self.store = {} if store is None else store
        cache = SpoolCache(load=lambda: self.store.get("state"),
                           save=self._save)
        self.linker = L.Linker(self.api, self._gcode, clock=self.clock,
                               cache=cache)

    def _save(self, state):
        self.store["state"] = json.loads(json.dumps(state))   # JSON-safe

    async def _gcode(self, script):
        self.sent.append(script)

    def start(self):
        run(self.linker.start())
        return self

    def send(self, bays, auto_create=None, ready=True):
        auto = self.auto_create if auto_create is None else auto_create
        run(self.linker.on_status(status(bays, auto_create=auto, ready=ready)))

    def bay(self, key="oams1-0"):
        return self.linker.status()["bays"][key]

    def spools(self):
        return run(self.api.request("GET", "/v1/spool"))

    def get_spool(self, spool_id):
        return run(self.api.request("GET", "/v1/spool/%d" % spool_id))

    def sets(self):
        return [s for s in self.sent if s.startswith("OAMSM_SET_BAY_SPOOL")]


class OfflineCacheHitTest(unittest.TestCase):
    def test_cache_hit_links_at_once_and_queues_the_location(self):
        h = Harness().start()
        t1 = bambu_tag(uid="0A0B0C0D", tray="a" * 32)
        h.send([bay(tag=t1)])                       # online: create + cache it
        a_id = h.bay()["spool_id"]
        self.assertTrue(a_id)

        h.api.down = True
        h.sent.clear()
        t2 = bambu_tag(uid="11111111", read_at=2.0, tray="a" * 32)
        h.send([bay(tag=t1), bay(tag=t2)])
        b = h.bay("oams1-1")
        self.assertEqual(b["state"], "linked")
        self.assertEqual(b["spool_id"], a_id)
        self.assertTrue(any("BAY=1 SPOOL=%d" % a_id in s for s in h.sets()))
        self.assertGreaterEqual(h.linker.status()["queued_writes"], 1)
        self.assertFalse(h.linker.status()["spoolman_online"])


class OfflineMissTest(unittest.TestCase):
    def test_miss_stays_resolving_with_one_queued_create(self):
        h = Harness().start()
        h.api.down = True
        t = bambu_tag(uid="22222222", tray="b" * 32)
        h.send([bay(tag=t)])
        b = h.bay()
        self.assertEqual(b["state"], "resolving")
        self.assertIn("offline", b["error"])
        self.assertIsNone(b["spool_id"])
        self.assertEqual(h.linker.status()["queued_writes"], 1)

        # A second read of the same tag while still offline does not queue
        # a second creation (at most one queued create per bay key).
        h.clock.advance(1)
        h.send([bay(tag=t)])
        self.assertEqual(h.linker.status()["queued_writes"], 1)


class ReplayTest(unittest.TestCase):
    def test_back_online_the_create_replays_links_and_drains_the_queue(self):
        h = Harness().start()
        h.api.down = True
        t = bambu_tag(uid="22222222", tray="b" * 32)
        h.send([bay(tag=t)])
        self.assertEqual(h.linker.status()["queued_writes"], 1)

        h.api.down = False
        h.clock.advance(6)                          # past REPLAY_SECONDS
        h.sent.clear()
        h.send([bay(tag=t)])

        b = h.bay()
        self.assertEqual(b["state"], "pending_confirmation")
        self.assertTrue(b["spool_id"])
        self.assertEqual(h.linker.status()["queued_writes"], 0)
        self.assertTrue(any("SET_BAY_SPOOL" in s for s in h.sent))
        self.assertEqual(h.get_spool(b["spool_id"])["location"], "oams1-0")

    def test_a_replayed_create_finds_a_spool_made_meanwhile_by_card_uids(self):
        h = Harness().start()
        h.api.down = True
        t = bambu_tag(uid="33333333", tray="c" * 32)
        h.send([bay(tag=t)])

        # Someone (another tool, the web UI) creates the physical spool's
        # Spoolman record directly, with the same card UID, while we wait.
        h.api.down = False
        made = run(_make_matching_spool(h.api, "33333333", "c" * 32))
        before = len(h.spools())

        h.clock.advance(6)
        h.send([bay(tag=t)])

        self.assertEqual(len(h.spools()), before, "replay created a duplicate")
        b = h.bay()
        self.assertEqual(b["spool_id"], made["id"])
        self.assertIn(b["state"], ("linked", "pending_confirmation"))
        self.assertEqual(h.linker.status()["queued_writes"], 0)

    def test_the_queue_and_cache_survive_a_new_linker(self):
        store = {}
        h = Harness(store=store).start()
        h.api.down = True
        t = bambu_tag(uid="44444444", tray="d" * 32)
        h.send([bay(tag=t)])
        queued = h.linker.status()["queued_writes"]
        self.assertEqual(queued, 1)

        again = Harness(store=store)
        run(again.linker.start())
        self.assertEqual(again.linker.status()["queued_writes"], queued)

    def test_a_bay_removed_while_its_create_was_queued_is_not_linked(self):
        h = Harness().start()
        h.api.down = True
        t = bambu_tag(uid="55555555", tray="e" * 32)
        h.send([bay(tag=t)])
        self.assertEqual(h.linker.status()["queued_writes"], 1)

        # The spool is pulled out of the bay before Spoolman comes back.
        h.send([bay(ready=False, tag=None)])

        h.api.down = False
        h.clock.advance(6)
        h.sent.clear()
        h.send([bay(ready=False, tag=None)])

        b = h.bay()
        self.assertNotEqual(b["state"], "linked")
        self.assertIsNone(b["spool_id"])
        self.assertFalse([s for s in h.sent if "SET_BAY_SPOOL" in s])
        self.assertEqual(h.linker.status()["queued_writes"], 0)
        # ... and no spool is made for it: its location would name a bay it
        # is not in (stack review 2026-11-10, 25)
        self.assertEqual(h.spools(), [])

    def test_a_spool_made_meanwhile_for_a_bay_that_moved_on_is_remembered_not_linked(self):
        h = Harness().start()
        h.api.down = True
        t = bambu_tag(uid="58585858", tray="3" * 32)
        h.send([bay(tag=t)])
        h.send([bay(ready=False, tag=None)])

        h.api.down = False
        made = run(_make_matching_spool(h.api, "58585858", "3" * 32))
        h.clock.advance(6)
        h.send([bay(ready=False, tag=None)])

        self.assertIsNone(h.bay()["spool_id"])
        self.assertEqual(len(h.spools()), 1)
        self.assertEqual(h.linker._cache.lookup(t), made["id"])

    def test_a_bay_that_moves_on_while_its_spool_is_created_leaves_no_location(self):
        h = Harness().start()
        h.api.down = True
        t = bambu_tag(uid="59595959", tray="4" * 32)
        h.send([bay(tag=t)])

        # The spool leaves the bay while the replay is creating its record.
        machine = h.linker._bays["oams1-0"]
        request = h.api.request

        async def leaves_during_the_create(method, path, query=None, body=None):
            result = await request(method, path, query, body)
            if method == "POST" and path == "/v1/spool":
                machine.state = "unlinked"
                machine.last_tag = None
            return result

        h.api.down = False
        h.api.request = leaves_during_the_create
        h.clock.advance(6)
        h.send([bay(ready=False, tag=None)])        # the replay runs first
        h.api.request = request

        spools = h.spools()
        self.assertEqual(len(spools), 1)
        self.assertFalse(spools[0].get("location"),
                         "the new spool names a bay it is not in")
        self.assertNotEqual(h.bay()["spool_id"], spools[0]["id"])


class StatusSchemaOfflineTest(unittest.TestCase):
    def test_status_validates_while_offline_and_after_replay(self):
        if jsonschema is None:
            self.skipTest("jsonschema is not installed")
        path = os.path.join(ROOT, "docs", "contracts", "schemas",
                            "openams_spoolman_status_v1.schema.json")
        with open(path) as handle:
            schema = json.load(handle)

        h = Harness().start()
        h.api.down = True
        t = bambu_tag(uid="66666666", tray="f" * 32)
        h.send([bay(tag=t)])
        jsonschema.validate(h.linker.status(), schema)

        h.api.down = False
        h.clock.advance(6)
        h.send([bay(tag=t)])
        jsonschema.validate(h.linker.status(), schema)


async def _make_spool(api, vendor_name, extra):
    """A spool of its own vendor and filament, carrying ``extra`` as given."""
    vendor = await api.request("POST", "/v1/vendor", body={"name": vendor_name})
    filament = await api.request(
        "POST", "/v1/filament",
        body={"name": "PETG HF", "vendor_id": vendor["id"], "material": "PETG"})
    spool = await api.request(
        "POST", "/v1/spool", body={"filament_id": filament["id"],
                                   "initial_weight": 1000, "remaining_weight": 1000})
    return await api.request("PATCH", "/v1/spool/%d" % spool["id"],
                             body={"extra": extra})


class CardUidOrderTest(unittest.TestCase):
    """Offline, as online, a UID in card_uids beats a shared tray UID (C4 §4,
    §6), and what Spoolman holds now beats what the cache learned once."""

    TRAY = "a" * 32

    def two_spools(self, h):
        shared = run(_make_spool(h.api, "Bambu Lab", {
            "tray_uid": ops.encode_extra(self.TRAY)}))
        mine = run(_make_spool(h.api, "Mine", {
            "card_uids": ops.encode_extra("DEADBEEF")}))
        return shared, mine

    def test_the_prewarmed_cache_prefers_the_card_uid(self):
        h = Harness()
        shared, mine = self.two_spools(h)
        h.start()                        # the pre-warm scan sees both
        h.api.down = True
        h.send([bay(tag=bambu_tag(uid="deadbeef", tray=self.TRAY))])
        self.assertEqual(h.bay()["spool_id"], mine["id"])

    def test_a_tray_key_is_only_cached_for_a_spool_that_has_it(self):
        h = Harness()
        shared, mine = self.two_spools(h)
        h.start()
        h.send([bay(tag=bambu_tag(uid="deadbeef", tray=self.TRAY))])
        self.assertEqual(h.bay()["spool_id"], mine["id"])
        h.api.down = True
        # The spool's other tag: online it finds the tray spool.
        h.send([bay(), bay(tag=bambu_tag(uid="0badf00d", tray=self.TRAY))])
        self.assertEqual(h.bay("oams1-1")["spool_id"], shared["id"])

    def test_a_uid_moved_in_spoolman_moves_in_the_cache(self):
        h = Harness().start()
        shared, mine = self.two_spools(h)
        h.send([bay(tag=bambu_tag(uid="deadbeef", tray=self.TRAY))])
        run(ops.add_card_uid(h.api, shared, "FEEDFACE"))
        # In Spoolman's web UI the user moves FEEDFACE to their own spool.
        h.api.spools[0]["extra"].pop("card_uids")
        h.api.spools[1]["extra"]["card_uids"] = ops.encode_extra(
            "DEADBEEF,FEEDFACE")
        run(h.linker.refresh_linked())
        h.api.down = True
        h.send([bay(), bay(tag=bambu_tag(uid="feedface", tray="f" * 32))])
        self.assertEqual(h.bay("oams1-1")["spool_id"], mine["id"])

    def test_a_queued_uid_write_keeps_its_cache_key(self):
        h = Harness().start()
        shared, mine = self.two_spools(h)
        h.api.down = True
        h.linker._cache.enqueue({"op": "card_uid", "spool_id": shared["id"],
                                 "uid": "FEEDFACE"})
        h.linker._cache.remember(bambu_tag(uid="feedface", tray=self.TRAY),
                                 h.api.spools[0])
        h.api.down = False
        h.linker._cache.scan(h.spools())
        self.assertEqual(h.linker._cache.lookup(
            bambu_tag(uid="feedface", tray="f" * 32)), shared["id"])

    def test_a_replayed_create_finds_the_card_uid_spool_first(self):
        h = Harness().start()
        h.api.down = True
        t = bambu_tag(uid="deadbeef", tray=self.TRAY)
        h.send([bay(tag=t)])
        self.assertEqual(h.linker.status()["queued_writes"], 1)
        h.api.down = False
        shared, mine = self.two_spools(h)
        before = len(h.spools())
        h.clock.advance(6)
        h.send([bay(tag=t)])
        self.assertEqual(len(h.spools()), before)
        self.assertEqual(h.bay()["spool_id"], mine["id"])


class SpoolmanWinsOfflineTest(unittest.TestCase):
    """The cache never puts back a vendor the user changed (C4 §6)."""

    def setUp(self):
        self.store = {}
        self.h = Harness(store=self.store).start()
        self.h.send([bay(tag=bambu_tag())])
        self.spool_id = self.h.bay()["spool_id"]
        # The user changes the spool's vendor in Spoolman.
        self.h.api.spools[0]["filament"]["vendor"] = {
            "id": 9, "name": "Polymaker", "extra": {}}
        self.linked = dict(spool_id=self.spool_id, link_source="tag")

    def test_the_same_tag_read_offline_links_the_same_spool(self):
        run(self.h.linker.refresh_linked())
        self.h.send([bay(ready=False, **self.linked)])
        self.h.api.down = True
        self.h.send([bay(tag=bambu_tag(read_at=3.0))])
        self.assertEqual(self.h.bay()["spool_id"], self.spool_id)
        self.assertEqual(self.h.bay()["spool"]["vendor"], "Polymaker")

    def test_a_restart_restores_the_refreshed_summary(self):
        run(self.h.linker.refresh_linked())
        again = Harness(store=self.store)
        again.api.down = True
        again.send([bay(tag=bambu_tag(read_at=9.0), **self.linked)])
        self.assertEqual(again.bay()["spool"]["vendor"], "Polymaker")
        # ... and the online restart reads the vendor from Spoolman itself.
        online = Harness(store=self.store)
        online.api = self.h.api
        online.linker._api = self.h.api
        online.start()
        online.send([bay(tag=bambu_tag(read_at=9.0), **self.linked)])
        self.assertEqual(online.bay()["spool"]["vendor"], "Polymaker")


class TwoTagsOfflineTest(unittest.TestCase):
    """Offline, the cache and the queued-create replay resolve in the order
    of C4 §4, so the two tags of one spool end as one spool (C4 §6)."""

    TRAY = "a" * 32

    def test_two_queued_creations_for_one_spools_two_tags_end_as_one(self):
        h = Harness().start()
        h.api.down = True
        first = bambu_tag(uid="0A0B0C0D", tray=self.TRAY)
        second = bambu_tag(uid="11111111", read_at=2.0, tray=self.TRAY)
        h.send([bay(tag=first), bay(tag=second)])
        self.assertEqual(h.linker.status()["queued_writes"], 2)
        h.api.down = False
        h.clock.advance(6)
        h.send([bay(tag=first), bay(tag=second)])
        spools = h.spools()
        self.assertEqual(len(spools), 1)
        spool_id = spools[0]["id"]
        self.assertEqual(ops.decode_extra(spools[0]["extra"]["card_uids"]),
                         "0A0B0C0D,11111111")
        self.assertEqual(h.bay("oams1-1")["spool_id"], spool_id)
        self.assertEqual(h.bay("oams1-1")["state"], "linked")
        self.assertEqual(h.linker.status()["queued_writes"], 0)

    def test_the_cache_finds_the_other_tag_by_tray_in_any_case(self):
        h = Harness().start()
        h.send([bay(tag=bambu_tag(uid="0A0B0C0D", tray=self.TRAY.upper()))])
        spool_id = h.bay()["spool_id"]
        h.api.down = True
        h.send([bay(tag=bambu_tag(uid="0A0B0C0D", tray=self.TRAY.upper())),
                bay(tag=bambu_tag(uid="22222222", read_at=2.0,
                                  tray=" " + self.TRAY))])
        self.assertEqual(h.bay("oams1-1")["spool_id"], spool_id)
        # The new UID and the location mirror wait; back online, both land.
        self.assertEqual(h.linker.status()["queued_writes"], 2)
        h.api.down = False
        h.clock.advance(6)
        h.send([bay(tag=bambu_tag(uid="0A0B0C0D", tray=self.TRAY.upper())),
                bay(tag=bambu_tag(uid="22222222", read_at=2.0,
                                  tray=" " + self.TRAY),
                    spool_id=spool_id, link_source="tag")])
        self.assertEqual(ops.decode_extra(
            h.get_spool(spool_id)["extra"]["card_uids"]), "0A0B0C0D,22222222")
        self.assertEqual(len(h.spools()), 1)

    def test_a_blank_tray_is_never_a_cache_key(self):
        h = Harness().start()
        h.send([bay(tag=bambu_tag(uid="0A0B0C0D", tray="0" * 32))])
        h.api.down = True
        h.send([bay(tag=bambu_tag(uid="0A0B0C0D", tray="0" * 32)),
                bay(tag=bambu_tag(uid="33333333", read_at=2.0,
                                  tray="0" * 32))])
        self.assertEqual(h.bay("oams1-1")["state"], "resolving")
        self.assertNotIn("tray:" + "0" * 32,
                         self.h_keys(h))

    @staticmethod
    def h_keys(h):
        return h.store["state"]["keys"]

    def test_a_bare_uid_offline_queues_nothing_and_retries(self):
        h = Harness().start()
        h.api.down = True
        bare = {"uid": "44444444", "format": "bambu", "read_at": 1.0,
                "spoolman_id": None, "bambu": None}
        h.send([bay(tag=bare)])
        self.assertEqual(h.bay()["state"], "resolving")
        self.assertEqual(h.bay()["error"], "Spoolman offline")
        self.assertEqual(h.linker.status()["queued_writes"], 0)
        # Back online: the UID is looked up once more, then the bay waits
        # for the decode without asking again.
        h.api.down = False
        h.send([bay(tag=bare)])
        self.assertEqual(h.bay()["state"], "resolving")
        self.assertIsNone(h.bay()["error"])
        self.assertEqual(h.spools(), [])
        h.api.calls.clear()
        h.send([bay(tag=bare)])
        self.assertEqual(h.api.calls, [])

    def test_a_replay_suggests_a_legacy_spool_instead_of_creating(self):
        h = Harness().start()
        h.api.down = True
        t = bambu_tag(uid="55555555", tray=self.TRAY)
        h.send([bay(tag=t)])
        h.api.down = False
        legacy = run(_make_spool(h.api, "Bambu Lab", {
            "card_uids": ops.encode_extra("66666666")}))
        run(h.api.request("PATCH", "/v1/filament/%d"
                          % legacy["filament"]["id"],
                          body={"color_hex": "000000"}))
        h.clock.advance(6)
        h.send([bay(tag=t)])
        self.assertEqual(len(h.spools()), 1)
        self.assertEqual(h.bay()["state"], "unlinked")
        self.assertEqual([s["spool_id"] for s in h.bay()["suggestions"]],
                         [legacy["id"]])
        self.assertEqual(h.linker.status()["queued_writes"], 0)


async def _make_matching_spool(api, uid, tray_uid):
    vendor = await api.request("POST", "/v1/vendor", body={"name": "Bambu Lab"})
    filament = await api.request(
        "POST", "/v1/filament",
        body={"name": "PETG HF", "vendor_id": vendor["id"], "material": "PETG"})
    spool = await api.request(
        "POST", "/v1/spool", body={"filament_id": filament["id"],
                                   "initial_weight": 1000, "remaining_weight": 1000})
    return await api.request(
        "PATCH", "/v1/spool/%d" % spool["id"],
        body={"extra": {"card_uids": ops.encode_extra(uid),
                        "tray_uid": ops.encode_extra(tray_uid)}})


if __name__ == "__main__":
    unittest.main()
