#!/usr/bin/env python3
"""Unit tests for the spool edit endpoint (C4 §9), on the component harness and
the in-memory Spoolman."""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_spoolman_component as T  # noqa: E402
from openams_spoolman import api as A  # noqa: E402
from openams_spoolman import ops  # noqa: E402


class EditRequest(T.StubWebRequest):
    def get_args(self):
        return self.args


def seed(api, material="PLA", color="ff0000", vendor="Acme", initial=1000,
         remaining=600, diameter=1.75, density=1.24, extruder=210, bed=60,
         name="Basic"):
    """One vendor, filament and spool in the fake Spoolman; returns the spool."""
    vendors = [v for v in api.vendors if v["name"] == vendor]
    if vendors:
        vendor_row = vendors[0]
    else:
        vendor_row = {"id": api._next_id("vendor"), "name": vendor, "extra": {}}
        api.vendors.append(vendor_row)
    filament = {"id": api._next_id("filament"), "name": name,
                "vendor": vendor_row, "material": material, "density": density,
                "diameter": diameter, "weight": initial, "color_hex": color,
                "settings_extruder_temp": extruder, "settings_bed_temp": bed,
                "extra": {}}
    api.filaments.append(filament)
    spool = {"id": api._next_id("spool"), "filament": filament,
             "initial_weight": initial, "remaining_weight": remaining,
             "used_weight": initial - remaining, "location": "", "lot_nr": "",
             "archived": False, "extra": {}}
    api.spools.append(spool)
    return spool


def linked_bay(spool, index=0):
    return T.bay(index=index, spool_id=spool["id"], link_source="manual")


class Base(unittest.IsolatedAsyncioTestCase):
    async def setUp_bays(self, bays, spools=()):
        self.api = A.FakeSpoolman()
        self.spools = [seed(self.api, **kw) for kw in spools]
        bays = bays(self.spools) if callable(bays) else bays
        self.h = await T.Harness(api=self.api).init()
        await self.h.ready(bays)
        self.h.server.sent_events.clear()
        self.h.klippy_apis.gcode.clear()
        self.api.calls.clear()

    async def edit(self, **args):
        args.setdefault("unit", "oams1")
        args.setdefault("bay", 0)
        return await self.h.component._handle_edit(EditRequest(args))

    async def refused(self, message, status=None, **args):
        with self.assertRaises(T.ServerErrorStub) as ctx:
            await self.edit(**args)
        self.assertIn(message, str(ctx.exception))
        if status is not None:
            self.assertEqual(ctx.exception.status_code, status)
        return ctx.exception

    def spool(self, index=0):
        return self.api.spools[index]

    def writes(self):
        return [c for c in self.api.calls if c[0] != "GET"]


class LinkedWeightsTest(Base):
    async def asyncSetUp(self):
        await self.setUp_bays(lambda s: [linked_bay(s[0])], [{}])

    async def test_weights_and_flow_k_patch_the_spool(self):
        result = await self.edit(remaining_weight=450, initial_weight=900,
                                 flow_k=0.05)
        spool = self.spool()
        self.assertEqual(spool["initial_weight"], 900)
        self.assertEqual(spool["remaining_weight"], 450)
        self.assertEqual(ops.decode_extra(spool["extra"]["flow_k"]), 0.05)
        self.assertEqual(spool["extra"]["flow_k"], "0.05")
        self.assertEqual(result["spool_id"], spool["id"])
        self.assertFalse(result["created"])
        self.assertEqual(result["bay"]["spool"]["remaining_g"], 450)
        self.assertEqual(result["bay"]["spool"]["remaining_pct"], 50)
        self.assertEqual(result["bay"]["spool"]["initial_g"], 900)
        self.assertEqual(result["bay"]["spool"]["flow_k"], 0.05)
        # One spool PATCH, no filament or vendor traffic, no G-code.
        self.assertEqual([(m, p) for m, p, _, _ in self.writes()],
                         [("PATCH", "/v1/spool/%d" % spool["id"])])
        self.assertEqual(self.h.klippy_apis.gcode, [])

    async def test_the_bay_entry_is_what_the_status_endpoint_publishes(self):
        result = await self.edit(remaining_weight=100)
        published = await self.h.component._handle_status(None)
        self.assertEqual(result["bay"], published["bays"]["oams1-0"])

    async def test_flow_k_keeps_the_other_extras(self):
        self.spool()["extra"]["card_uids"] = json.dumps("AABB")
        await self.edit(flow_k=1)
        self.assertEqual(self.spool()["extra"]["card_uids"], '"AABB"')
        self.assertEqual(self.spool()["extra"]["flow_k"], "1.0")

    async def test_initial_weight_alone_keeps_the_remaining_weight(self):
        await self.edit(initial_weight=2000)
        self.assertEqual(self.spool()["remaining_weight"], 600)
        self.assertEqual(self.spool()["initial_weight"], 2000)

    async def test_remaining_above_initial_names_the_field_and_writes_nothing(self):
        await self.refused("remaining_weight", 400, remaining_weight=1500)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.spool()["remaining_weight"], 600)

    async def test_the_notification_follows_a_success(self):
        await self.edit(remaining_weight=300)
        self.assertEqual(len(self.h.server.sent_events), 1)
        event, args = self.h.server.sent_events[0]
        self.assertEqual(event, "openams_spoolman:status_changed")
        self.assertEqual(args[0]["bays"]["oams1-0"]["spool"]["remaining_g"], 300)
        self.assertEqual(self.h.server.notifications[-1],
                         ("openams_spoolman:status_changed",
                          "openams_spoolman_status"))

    async def test_a_failure_sends_no_notification(self):
        await self.refused("flow_k", flow_k=3)
        self.assertEqual(self.h.server.sent_events, [])


class LinkedFilamentTest(Base):
    async def asyncSetUp(self):
        # Two spools share one filament; bay 0 holds the first.
        self.api = A.FakeSpoolman()
        first = seed(self.api)
        second = {"id": self.api._next_id("spool"), "filament": first["filament"],
                  "initial_weight": 1000, "remaining_weight": 1000,
                  "used_weight": 0, "location": "", "lot_nr": "",
                  "archived": False, "extra": {}}
        self.api.spools.append(second)
        self.h = await T.Harness(api=self.api).init()
        await self.h.ready([linked_bay(first)])
        self.h.server.sent_events.clear()
        self.api.calls.clear()
        self.old = first["filament"]
        self.snapshot = dict(self.old)

    async def test_a_new_color_repoints_to_a_new_filament(self):
        result = await self.edit(color_hex="00FF7F")
        spool = self.spool(0)
        self.assertNotEqual(spool["filament"]["id"], self.old["id"])
        self.assertEqual(spool["filament"]["color_hex"], "00ff7f")
        self.assertEqual(spool["filament"]["material"], "PLA")
        self.assertEqual(spool["filament"]["vendor"]["name"], "Acme")
        # Carried over from the current filament.
        self.assertEqual(spool["filament"]["diameter"], 1.75)
        self.assertEqual(spool["filament"]["density"], 1.24)
        self.assertEqual(spool["filament"]["settings_extruder_temp"], 210)
        self.assertEqual(spool["filament"]["settings_bed_temp"], 60)
        self.assertEqual(spool["filament"]["name"], "Basic")
        # The old filament is unchanged and the other spool still uses it.
        self.assertEqual(self.old, self.snapshot)
        self.assertEqual(self.spool(1)["filament"]["id"], self.old["id"])
        self.assertEqual(result["bay"]["spool"]["color_hex"], "00FF7F")
        self.assertFalse(any(p.startswith("/v1/filament/") and m == "PATCH"
                             for m, p, _, _ in self.api.calls))

    async def test_a_matching_filament_is_reused(self):
        other = seed(self.api, color="0000ff", name="Other")["filament"]
        count = len(self.api.filaments)
        await self.edit(color_hex="0000FF")
        self.assertEqual(self.spool(0)["filament"]["id"], other["id"])
        self.assertEqual(len(self.api.filaments), count)
        self.assertEqual(self.old, self.snapshot)

    async def test_a_new_material_takes_the_material_defaults(self):
        await self.edit(material="PETG")
        filament = self.spool(0)["filament"]
        self.assertEqual(filament["material"], "PETG")
        self.assertEqual(filament["color_hex"], "ff0000")
        self.assertEqual(filament["density"], ops.MATERIAL_DENSITY["PETG"])
        self.assertEqual(filament["settings_bed_temp"],
                         ops.MATERIAL_BED_TEMP["PETG"])
        self.assertEqual(filament["diameter"], 1.75)
        self.assertEqual(filament["name"], "PETG")
        self.assertEqual(self.old, self.snapshot)

    async def test_a_new_vendor_creates_it_and_leaves_the_old_filament(self):
        await self.edit(vendor="Polymaker")
        filament = self.spool(0)["filament"]
        self.assertEqual(filament["vendor"]["name"], "Polymaker")
        self.assertIn("Polymaker", [v["name"] for v in self.api.vendors])
        self.assertEqual(self.old["vendor"]["name"], "Acme")
        self.assertEqual(self.old, self.snapshot)

    async def test_an_unchanged_color_writes_no_filament(self):
        await self.edit(color_hex="FF0000", material="pla", vendor="Acme")
        self.assertEqual(self.spool(0)["filament"]["id"], self.old["id"])
        self.assertEqual(self.writes(), [])

    async def test_the_spool_patch_is_one_request_with_the_weights(self):
        await self.edit(color_hex="123456", remaining_weight=10)
        patches = [(m, p) for m, p, _, _ in self.writes()
                   if p.startswith("/v1/spool/")]
        self.assertEqual(patches, [("PATCH", "/v1/spool/1")])
        self.assertEqual(self.spool(0)["remaining_weight"], 10)


class UnlinkedCreateTest(Base):
    async def asyncSetUp(self):
        await self.setUp_bays([T.bay(index=0, ready=True),
                               T.bay(index=1, ready=False)])

    async def test_create_and_link(self):
        result = await self.edit(material="PETG", color_hex="0A2989",
                                 vendor="Polymaker", flow_k=0.04,
                                 initial_weight=750, remaining_weight=500)
        self.assertTrue(result["created"])
        spool = self.spool()
        self.assertEqual(result["spool_id"], spool["id"])
        self.assertEqual(spool["location"], "oams1-0")
        self.assertEqual(spool["initial_weight"], 750)
        self.assertEqual(spool["remaining_weight"], 500)
        self.assertEqual(spool["filament"]["material"], "PETG")
        self.assertEqual(spool["filament"]["color_hex"], "0a2989")
        self.assertEqual(spool["filament"]["vendor"]["name"], "Polymaker")
        self.assertEqual(spool["filament"]["density"],
                         ops.MATERIAL_DENSITY["PETG"])
        self.assertEqual(ops.decode_extra(spool["extra"]["flow_k"]), 0.04)
        # The link, then the bay's working temperature, both before the edit
        # is answered: a display's "Save and load" sends the load right after.
        self.assertEqual(self.h.klippy_apis.gcode, [
            "OAMSM_SET_BAY_SPOOL OAMS=oams1 BAY=0 SPOOL=%d SOURCE=manual"
            % spool["id"],
            'OAMSM_SET_BAY_FILAMENT OAMS=oams1 BAY=0 MATERIAL="PETG" TEMP=240'
            ' SOURCE=table'])
        self.assertEqual(result["bay"]["state"], "linked")
        self.assertEqual(result["bay"]["source"], "manual")
        self.assertEqual(result["bay"]["spool_id"], spool["id"])
        self.assertEqual(result["bay"]["spool"]["vendor"], "Polymaker")
        self.assertEqual(result["bay"]["spool"]["initial_g"], 750)
        self.assertEqual(result["bay"]["spool"]["flow_k"], 0.04)
        self.assertEqual(self.h.bay()["spool_id"], spool["id"])

    async def test_defaults(self):
        result = await self.edit(material="PLA")
        spool = self.spool()
        self.assertEqual(spool["initial_weight"], 1000)
        self.assertEqual(spool["remaining_weight"], 1000)
        self.assertEqual(spool["filament"]["vendor"]["name"], "Generic")
        self.assertIsNone(spool["filament"]["color_hex"])
        self.assertEqual(result["bay"]["spool"]["remaining_pct"], 100)
        # Nothing was written for the pressure advance, so there is none.
        self.assertEqual(result["bay"]["spool"]["initial_g"], 1000)
        self.assertIsNone(result["bay"]["spool"]["flow_k"])

    async def test_the_host_echo_leaves_the_linked_bay_alone(self):
        result = await self.edit(material="PLA", bay=0)
        self.h.server.sent_events.clear()
        self.h.push({"oams_manager": {"devices": {"oams1": {"bays": [
            T.bay(index=0, spool_id=result["spool_id"], link_source="manual"),
            T.bay(index=1, ready=False)]}}}})
        await self.h.flush()
        self.assertEqual(self.h.bay()["state"], "linked")
        self.assertEqual(self.h.bay()["source"], "manual")
        self.assertEqual(self.spool()["location"], "oams1-0")

    async def test_the_notification_follows_a_create(self):
        result = await self.edit(material="PLA")
        self.assertEqual(len(self.h.server.sent_events), 1)
        payload = self.h.server.sent_events[0][1][0]
        self.assertEqual(payload["bays"]["oams1-0"], result["bay"])

    async def test_a_missing_material_names_it_and_writes_nothing(self):
        await self.refused("material is required", 400, color_hex="00ff00")
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.h.klippy_apis.gcode, [])

    async def test_a_refused_link_removes_the_new_spool(self):
        async def refuse(script):
            raise RuntimeError("Unknown command")
        self.h.component._run_gcode = refuse
        await self.refused("did not accept the spool link", 502, material="PLA")
        self.assertEqual(self.api.spools, [])
        self.assertEqual(self.h.server.sent_events, [])

    async def test_an_empty_bay(self):
        await self.refused("no spool in oams1 Spool 2", 409, bay=1,
                        material="PLA")
        self.assertEqual(self.api.calls, [])

    async def test_create_weights_cannot_exceed_each_other(self):
        await self.refused("remaining_weight", 400, material="PLA",
                        remaining_weight=1200)
        await self.refused("remaining_weight", 400, material="PLA",
                        initial_weight=300, remaining_weight=301)
        self.assertEqual(self.writes(), [])


class ValidationTest(Base):
    async def asyncSetUp(self):
        await self.setUp_bays(lambda s: [linked_bay(s[0])], [{}])

    async def test_unknown_unit_and_bay(self):
        await self.refused("unknown unit 'oams9'", 404, unit="oams9",
                        remaining_weight=1)
        await self.refused("oams1 has no Spool 4", 404, bay=3, remaining_weight=1)

    async def test_bad_params(self):
        cases = [
            ({"unit": 1, "remaining_weight": 1}, "unit"),
            ({"bay": "0", "remaining_weight": 1}, "bay"),
            ({"bay": -1, "remaining_weight": 1}, "bay"),
            ({"bay": True, "remaining_weight": 1}, "bay"),
            ({}, "nothing to change"),
            ({"material": ""}, "material"),
            ({"material": "x" * 33}, "material"),
            ({"material": 5}, "material"),
            ({"vendor": "x" * 65}, "vendor"),
            ({"vendor": ""}, "vendor"),
            ({"color_hex": "#ff0000"}, "color_hex"),
            ({"color_hex": "ff00"}, "color_hex"),
            ({"color_hex": "gg0000"}, "color_hex"),
            ({"remaining_weight": -1}, "remaining_weight"),
            ({"remaining_weight": "5"}, "remaining_weight"),
            ({"remaining_weight": True}, "remaining_weight"),
            ({"initial_weight": 0}, "initial_weight"),
            ({"flow_k": -0.1}, "flow_k"),
            ({"flow_k": 2.01}, "flow_k"),
            ({"flow_k": float("nan")}, "flow_k"),
        ]
        for args, word in cases:
            with self.subTest(args=args):
                await self.refused(word, 400, **args)
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.h.server.sent_events, [])

    async def test_the_limits_are_accepted(self):
        await self.edit(material="x" * 32, vendor="y" * 64, flow_k=2,
                        remaining_weight=0, color_hex="AbCdEf")
        self.assertEqual(self.spool()["remaining_weight"], 0)
        self.assertEqual(self.spool()["filament"]["color_hex"], "abcdef")
        await self.edit(flow_k=0)
        self.assertEqual(self.spool()["extra"]["flow_k"], "0.0")


class OfflineTest(Base):
    async def test_offline_on_a_linked_bay_changes_nothing(self):
        await self.setUp_bays(lambda s: [linked_bay(s[0])], [{}])
        before = json.dumps(self.api.spools, sort_keys=True)
        self.api.online = False
        await self.refused("Spoolman is offline", 503, color_hex="00ff00",
                        remaining_weight=5)
        self.api.online = True
        self.assertEqual(json.dumps(self.api.spools, sort_keys=True), before)
        self.assertEqual(self.h.server.sent_events, [])
        self.assertEqual(self.h.component.linker.status()["queued_writes"], 0)

    async def test_offline_on_an_unlinked_bay_creates_nothing(self):
        await self.setUp_bays([T.bay(index=0, ready=True)])
        self.api.online = False
        await self.refused("Spoolman is offline", 503, material="PLA")
        self.assertEqual(self.h.klippy_apis.gcode, [])
        self.assertEqual(self.api.spools, [])
        self.assertEqual(self.h.component.linker.status()["queued_writes"], 0)


class CapabilityTest(Base):
    async def test_the_status_says_edit(self):
        await self.setUp_bays([T.bay(index=0, ready=False)])
        status = await self.h.component._handle_status(None)
        self.assertIs(status["edit"], True)

    async def test_the_endpoint_is_registered(self):
        h = T.Harness()
        entries = [e for e in h.server.endpoints
                   if e[0] == "/server/openams_spoolman/edit"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0][1], ["POST"])


class PendingConfirmationTest(Base):
    async def link_by_tag(self):
        await self.setUp_bays([T.bay(tag=T.bambu_tag())])
        spool_id = self.h.bay()["spool_id"]
        self.h.push({"oams_manager": {"devices": {"oams1": {"bays": [T.bay(
            tag=T.bambu_tag(), spool_id=spool_id, link_source="tag")]}}}})
        await self.h.flush()

    async def test_a_weight_edit_ends_pending_confirmation(self):
        await self.link_by_tag()
        self.assertEqual(self.h.bay()["state"], "pending_confirmation")
        result = await self.edit(remaining_weight=800)
        self.assertEqual(result["bay"]["state"], "linked")
        self.assertFalse(result["bay"]["pending_confirmation"])

    async def test_a_color_edit_keeps_it(self):
        await self.link_by_tag()
        result = await self.edit(color_hex="00ff00")
        self.assertEqual(result["bay"]["state"], "pending_confirmation")


if __name__ == "__main__":
    unittest.main()
