#!/usr/bin/env python3
"""Unit tests for the openams_spoolman active spool and PA (C4 §7.2).

Everything runs against the in-memory ``FakeSpoolman`` and recording
``run_gcode`` / ``set_active_spool`` callables; the end-to-end gate is
the S2c1 gate.
"""

import asyncio
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "moonraker"))

from openams_spoolman import api as A  # noqa: E402
from openams_spoolman import ops  # noqa: E402
from openams_spoolman.active import ActiveSpool  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def lane(op="UNLOADED", unit=None, extruder="extruder"):
    return {"op": op, "unit": unit, "runout": "idle", "extruder": extruder}


def bay(index, spool_id=None):
    return {"index": index, "ready": True, "spool_id": spool_id,
            "link_source": "tag" if spool_id is not None else "none"}


def status(lanes, bays=None, pa=True, ready=True):
    return {"ready": ready,
            "settings": {"apply_pa_on_load": pa, "auto_create_spoolman_spools": True},
            "topology": {"oams": {"oams1": {"idx": 3, "lane": "fps1"},
                                  "oams2": {"idx": 4, "lane": "fps2"}}},
            "lanes_by_fps": lanes,
            "devices": bays or {"oams1": {"bays": [bay(i) for i in range(4)]}}}


class Recorder(object):
    def __init__(self):
        self.active = []
        self.gcode = []

    async def set_active(self, spool_id):
        self.active.append(spool_id)

    async def run_gcode(self, gcode):
        self.gcode.append(gcode)


class TestActiveSpool(unittest.TestCase):
    def setUp(self):
        self.api = A.FakeSpoolman()

    def spools(self, *flows):
        """Create one spool per flow value (``None`` leaves ``flow_k`` unset)."""
        async def make():
            created = []
            for flow in flows:
                vendor = await self.api.request("POST", "/v1/vendor",
                                                body={"name": "V"})
                filament = await self.api.request(
                    "POST", "/v1/filament",
                    body={"name": "F", "vendor_id": vendor["id"],
                          "material": "PLA"})
                spool = await self.api.request(
                    "POST", "/v1/spool", body={"filament_id": filament["id"]})
                if flow is not None:
                    await self.api.request(
                        "PATCH", "/v1/spool/%d" % spool["id"],
                        body={"extra": {"flow_k": ops.encode_extra(float(flow))}})
                created.append(spool["id"])
            return created
        return run(make())

    def test_load_sets_active_and_applies_pa(self):
        ids = self.spools(0.021, None)
        bays = {"oams1": {"bays": [bay(0, ids[0]), bay(1, ids[1]), bay(2)]}}
        rec = Recorder()
        active = ActiveSpool(self.api, rec.run_gcode, rec.set_active)
        run(active.on_status(status({"fps1": lane()}, bays=bays)))
        run(active.on_status(status({"fps1": lane("LOADED", [3, 0])}, bays=bays)))
        self.assertEqual(rec.active, [None, ids[0]])
        self.assertEqual(active.active, ids[0])
        self.assertEqual(
            rec.gcode,
            ["SET_PRESSURE_ADVANCE EXTRUDER=extruder ADVANCE=0.0210"])

    def test_a_failed_set_is_retried_on_the_next_status(self):
        ids = self.spools(None)
        bays = {"oams1": {"bays": [bay(0, ids[0])]}}
        calls = []

        async def flaky(spool_id):
            calls.append(spool_id)
            if len(calls) == 1:
                raise RuntimeError("moonraker busy")

        rec = Recorder()
        active = ActiveSpool(self.api, rec.run_gcode, flaky)
        loaded = status({"fps1": lane("LOADED", [3, 0])}, bays=bays)
        run(active.on_status(loaded))
        self.assertIsNone(active.active)
        run(active.on_status(loaded))
        self.assertEqual(calls, [ids[0], ids[0]])
        self.assertEqual(active.active, ids[0])

    def test_seeded_then_an_unlinked_loaded_bay_clears_it(self):
        bays = {"oams1": {"bays": [bay(0), bay(1)]}}
        rec = Recorder()
        active = ActiveSpool(self.api, rec.run_gcode, rec.set_active, seed=42)
        run(active.on_status(status({"fps1": lane("LOADED", [3, 0])}, bays=bays)))
        self.assertEqual(rec.active, [None])

    def test_unload_clears_active(self):
        ids = self.spools(0.021, None)
        bays = {"oams1": {"bays": [bay(0, ids[0]), bay(1, ids[1]), bay(2)]}}
        rec = Recorder()
        active = ActiveSpool(self.api, rec.run_gcode, rec.set_active)
        run(active.on_status(status({"fps1": lane("LOADED", [3, 0])}, bays=bays)))
        run(active.on_status(status({"fps1": lane("UNLOADED", None)}, bays=bays)))
        self.assertIsNone(active.active)
        self.assertEqual(rec.active, [ids[0], None])

    def test_runout_reload_applies_pa_from_new_bay(self):
        ids = self.spools(0.021, 0.030)
        bays = {"oams1": {"bays": [bay(0, ids[0]), bay(1, ids[1])]}}
        rec = Recorder()
        active = ActiveSpool(self.api, rec.run_gcode, rec.set_active)
        run(active.on_status(status({"fps1": lane("LOADED", [3, 0])}, bays=bays)))
        # The same lane reloads onto bay 1 while still LOADED: a runout reload.
        run(active.on_status(status({"fps1": lane("LOADED", [3, 1])}, bays=bays)))
        self.assertEqual(active.active, ids[1])
        self.assertEqual(rec.active, [ids[0], ids[1]])
        self.assertEqual(rec.gcode, [
            "SET_PRESSURE_ADVANCE EXTRUDER=extruder ADVANCE=0.0210",
            "SET_PRESSURE_ADVANCE EXTRUDER=extruder ADVANCE=0.0300"])

    def test_unlinked_loaded_bay_clears_active(self):
        ids = self.spools(0.021)
        bays = {"oams1": {"bays": [bay(0, ids[0]), bay(2, None)]}}
        rec = Recorder()
        active = ActiveSpool(self.api, rec.run_gcode, rec.set_active)
        run(active.on_status(status({"fps1": lane("LOADED", [3, 0])}, bays=bays)))
        run(active.on_status(status({"fps1": lane("LOADED", [3, 2])}, bays=bays)))
        self.assertIsNone(active.active)
        self.assertEqual(rec.active, [ids[0], None])

    def test_two_loaded_lanes_follow_extruder(self):
        first, second = self.spools(None, None)
        bays = {"oams1": {"bays": [bay(0, first)]},
                "oams2": {"bays": [bay(0, second)]}}
        lanes = {"fps1": lane("LOADED", [3, 0], extruder="extruder"),
                 "fps2": lane("LOADED", [4, 0], extruder="extruder1")}
        rec = Recorder()
        active = ActiveSpool(self.api, rec.run_gcode, rec.set_active)
        run(active.on_status(status(lanes, bays=bays), extruder="extruder1"))
        self.assertEqual(active.active, second)
        run(active.on_status(status(lanes, bays=bays), extruder="extruder"))
        self.assertEqual(active.active, first)
        # No PA: neither spool carries flow_k.
        self.assertEqual(rec.gcode, [])

    def test_pa_off_applies_nothing(self):
        ids = self.spools(0.021)
        bays = {"oams1": {"bays": [bay(0, ids[0])]}}
        rec = Recorder()
        active = ActiveSpool(self.api, rec.run_gcode, rec.set_active)
        run(active.on_status(status({"fps1": lane()}, bays=bays, pa=False)))
        run(active.on_status(
            status({"fps1": lane("LOADED", [3, 0])}, bays=bays, pa=False)))
        self.assertEqual(active.active, ids[0])
        self.assertEqual(rec.gcode, [])

    def test_no_flow_k_applies_no_pa(self):
        ids = self.spools(None)
        bays = {"oams1": {"bays": [bay(0, ids[0])]}}
        rec = Recorder()
        active = ActiveSpool(self.api, rec.run_gcode, rec.set_active)
        run(active.on_status(status({"fps1": lane("LOADED", [3, 0])}, bays=bays)))
        self.assertEqual(active.active, ids[0])
        self.assertEqual(rec.gcode, [])

    def test_spoolman_down_still_sets_active(self):
        ids = self.spools(0.021)
        self.api.online = False
        bays = {"oams1": {"bays": [bay(0, ids[0])]}}
        rec = Recorder()
        active = ActiveSpool(self.api, rec.run_gcode, rec.set_active)
        run(active.on_status(status({"fps1": lane()}, bays=bays)))
        run(active.on_status(status({"fps1": lane("LOADED", [3, 0])}, bays=bays)))
        self.assertEqual(active.active, ids[0])
        self.assertEqual(rec.active, [None, ids[0]])
        self.assertEqual(rec.gcode, [])

    def test_seeded_first_status_sends_nothing(self):
        # Finding F-4 #6: seeded with 42 (Moonraker already has spool 42 active)
        # and no OAMS lane loaded, the first status must not clear it.
        # A load then switches to the bay's spool.
        ids = self.spools(None)
        bays = {"oams1": {"bays": [bay(0, ids[0])]}}
        rec = Recorder()
        active = ActiveSpool(self.api, rec.run_gcode, rec.set_active, seed=42)
        # No lane loaded: must not call set_active_spool (would clear spool 42).
        run(active.on_status(status({"fps1": lane()}, bays=bays)))
        self.assertEqual(rec.active, [])
        # A lane loads: switch to the bay's spool.
        run(active.on_status(status({"fps1": lane("LOADED", [3, 0])}, bays=bays)))
        self.assertEqual(rec.active, [ids[0]])
        self.assertEqual(active.active, ids[0])

    def test_repeated_status_sets_active_once(self):
        ids = self.spools(0.021)
        bays = {"oams1": {"bays": [bay(0, ids[0])]}}
        rec = Recorder()
        active = ActiveSpool(self.api, rec.run_gcode, rec.set_active)
        loaded = status({"fps1": lane("LOADED", [3, 0])}, bays=bays)
        run(active.on_status(loaded))
        run(active.on_status(loaded))
        run(active.on_status(loaded))
        self.assertEqual(rec.active, [ids[0]])
        self.assertEqual(len(rec.gcode), 1)


if __name__ == "__main__":
    unittest.main()
