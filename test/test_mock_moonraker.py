# Tests for the mock Moonraker (tools/mock_moonraker): a fixture replayed over
# Moonraker's WebSocket JSON-RPC API. The server runs in-process on an
# ephemeral port and time is driven explicitly (speed=None + apply_due()), so
# nothing here waits on a wall clock.
#
#   cd openams && python3 -m unittest discover -s test

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest

try:
    import aiohttp
except ImportError:  # pragma: no cover - environment dependent
    raise unittest.SkipTest("aiohttp is not installed; skipping the mock Moonraker tests")

_TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

from mock_moonraker import FixtureError, load, loads  # noqa: E402
from mock_moonraker.server import REFUSAL_MESSAGE, MockMoonraker  # noqa: E402

_BUILD = os.path.join(_TOOLS, "mock_moonraker", "build_fixtures.py")
_FIXTURES = os.path.join(_TOOLS, "mock_moonraker", "fixtures")

UI_V1 = {"v": 1, "unit": "oams1", "family": "ams1", "serial": "AA01",
         "online": True, "ready": True, "cal": "c", "bays": [], "fault": None}
UI_V1_CHANGED = dict(UI_V1, online=False)

# A unit with a linked bay (0), an unlinked bay that holds a spool (1) and an
# empty one (2), plus the C4 §8 component status that links bay 0 to spool 12.
SPOOL_UI = {
    "v": 1, "unit": "oams1", "family": "ams1", "serial": "AA01",
    "online": True, "ready": True, "cal": "c",
    "lane": {"id": "fps", "ext": "extruder", "op": "UNLOADED", "group": None,
             "bay": None, "stage": None, "runout": "idle", "from": None,
             "to": None, "ms": True},
    "bays": [
        {"id": 0, "s": "r", "cal": "c", "a": "lcad", "sp": {"id": 12, "src": "t"}},
        {"id": 1, "s": "r", "cal": "c", "a": "lca", "sp": None},
        {"id": 2, "s": "e", "cal": "c", "a": "", "sp": None},
    ],
    "groups": [{"n": "T0", "b": ["oams1-0", "oams1-1"], "s": "rr"}],
}
SPOOL_COMPONENT = {
    "schema_version": 1, "spoolman_online": True, "edit": True, "stop": True,
    "queued_writes": 0,
    "bays": {
        "oams1-0": {"state": "linked", "spool_id": 12, "source": "tag",
                    "pending_confirmation": False, "error": None,
                    "spool": {"material": "PLA", "color_hex": "0A2989",
                              "vendor": "Bambu Lab", "remaining_g": 812,
                              "remaining_pct": 81, "initial_g": 1000,
                              "flow_k": 0.02}},
        "oams1-1": {"state": "unlinked", "spool_id": None, "source": "none",
                    "pending_confirmation": False, "error": None, "spool": None},
        "oams1-2": {"state": "unlinked", "spool_id": None, "source": "none",
                    "pending_confirmation": False, "error": None, "spool": None},
    },
}


def spool_document(scenario="spool_edit", lane=None, responses=None):
    """The one unit the spool edit and the G-code modes are driven against."""
    ui = json.loads(json.dumps(SPOOL_UI))
    if lane:
        ui["lane"].update(lane)
    return {
        "scenario": scenario,
        "seed": 11,
        "family_map": {"oams1": "ams1"},
        "spoolman_responses": responses or {},
        "events": [
            {"t": 0.0, "kind": "openams_ui", "unit": "oams1", "payload": ui},
            {"t": 0.0, "kind": "component_status", "unit": None,
             "payload": json.loads(json.dumps(SPOOL_COMPONENT))},
        ],
    }


def fixture_document():
    """A small hand-made fixture: initial state at t=0, two changes later."""
    component_v1 = {"schema_version": 1, "spoolman_online": True, "bays": {}}
    return {
        "scenario": "test_unit",
        "seed": 7,
        "family_map": {"oams1": "ams1"},
        "spoolman_responses": {"GET /v1/spool/1": {"id": 1, "filament": {}}},
        "file_metadata": {"cube.gcode": {"filament_weights": [12.5, 3.0]}},
        "events": [
            {"t": 0.0, "kind": "c2_status", "unit": None,
             "payload": {"schema_version": 2, "ready": True, "lanes": []}},
            {"t": 0.0, "kind": "openams_ui", "unit": "oams1", "payload": UI_V1},
            {"t": 0.0, "kind": "component_status", "unit": None,
             "payload": component_v1},
            {"t": 5.0, "kind": "openams_ui", "unit": "oams1", "payload": UI_V1_CHANGED},
            {"t": 6.0, "kind": "component_status", "unit": None,
             "payload": dict(component_v1, spoolman_online=False)},
        ],
    }


class FixtureFormatTest(unittest.TestCase):
    def test_loads_accepts_the_documented_format(self):
        fixture = loads(json.dumps(fixture_document()))
        self.assertEqual(fixture.scenario, "test_unit")
        self.assertEqual(fixture.units, ["oams1"])
        self.assertEqual(len(fixture.events), 5)
        self.assertEqual(fixture.events[3].t, 5.0)

    def test_rejects_bad_input(self):
        cases = [
            ("not json", "not valid JSON"),
            ('{"scenario": "x"}', "missing key 'seed'"),
            (json.dumps(dict(fixture_document(), events=[
                {"t": 0, "kind": "nope", "unit": None, "payload": {}}])), "unknown kind"),
            (json.dumps(dict(fixture_document(), events=[
                {"t": 0, "kind": "gcode_script", "unit": None, "payload": {}}])),
             "takes a str"),
            (json.dumps(dict(fixture_document(), events=[
                {"t": 0, "kind": "openams_ui", "unit": None, "payload": {}}])),
             "must name its unit"),
            (json.dumps(dict(fixture_document(), events=[
                {"t": 9, "kind": "c2_status", "unit": None, "payload": {}},
                {"t": 1, "kind": "c2_status", "unit": None, "payload": {}}])),
             "time-ordered"),
            (json.dumps(dict(fixture_document(), extra=1)), "unknown top-level key"),
            (json.dumps(dict(fixture_document(), seed=True)), "expected an integer"),
        ]
        for text, expected in cases:
            with self.assertRaises(FixtureError) as caught:
                loads(text)
            self.assertIn(expected, str(caught.exception), text)

    def test_load_reports_the_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "broken.json")
            with open(path, "w") as handle:
                handle.write("[]")
            with self.assertRaises(FixtureError) as caught:
                load(path)
            self.assertIn("broken.json", str(caught.exception))


class BuildFixturesTest(unittest.TestCase):
    def test_output_validates_and_is_deterministic(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            for out in (first, second):
                subprocess.check_output([sys.executable, _BUILD, "--out", out])
            names = sorted(os.listdir(first))
            self.assertTrue(names, "build_fixtures.py wrote nothing")
            for name in names:
                with open(os.path.join(first, name), "rb") as handle:
                    one = handle.read()
                with open(os.path.join(second, name), "rb") as handle:
                    two = handle.read()
                self.assertEqual(one, two, "%s is not deterministic" % name)
                fixture = loads(one.decode(), source=name)
                self.assertEqual("%s.json" % fixture.scenario, name)
                self.assertEqual(fixture.events[0].kind, "c2_status")
                self.assertEqual([e.kind for e in fixture.events[1:]],
                                 ["openams_ui", "component_status"])
                for event in fixture.events:
                    self.assertEqual(event.t, 0.0)

    def test_checked_in_fixtures_match_a_fresh_build(self):
        with tempfile.TemporaryDirectory() as out:
            subprocess.check_output([sys.executable, _BUILD, "--out", out])
            for name in sorted(os.listdir(out)):
                with open(os.path.join(out, name), "rb") as handle:
                    fresh = handle.read()
                with open(os.path.join(_FIXTURES, name), "rb") as handle:
                    checked_in = handle.read()
                self.assertEqual(fresh, checked_in, "%s is stale" % name)


class ClientTestCase(unittest.IsolatedAsyncioTestCase):
    """A server on an ephemeral port, driven over a real WebSocket."""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.fixture_path = os.path.join(self.tmp.name, "fixture.json")
        with open(self.fixture_path, "w") as handle:
            json.dump(fixture_document(), handle)
        self.fixture = load(self.fixture_path)
        self.record_path = os.path.join(self.tmp.name, "calls.jsonl")
        # speed=None: the timeline is off, apply_due() drives it.
        self.server = MockMoonraker(self.fixture, host="127.0.0.1", port=0,
                                    speed=None, record_path=self.record_path)
        await self.server.start()
        self.session = aiohttp.ClientSession()
        self.ws = await self.session.ws_connect(self.server.url)

    async def asyncTearDown(self):
        await self.ws.close()
        await self.session.close()
        await self.server.stop()
        self.tmp.cleanup()

    async def rpc(self, method, params=None, ident=1, ws=None):
        ws = ws if ws is not None else self.ws
        await ws.send_json({"jsonrpc": "2.0", "id": ident,
                            "method": method, "params": params or {}})
        while True:
            message = await asyncio.wait_for(ws.receive_json(), 5.0)
            if message.get("id") == ident and ("result" in message or "error" in message):
                return message

    async def notification(self, method, ws=None):
        ws = ws if ws is not None else self.ws
        while True:
            message = await asyncio.wait_for(ws.receive_json(), 5.0)
            if message.get("method") == method:
                return message

    async def start_fixture(self, document, **kwargs):
        """Write, load and start one more server sharing the test's temp dir."""
        path = os.path.join(self.tmp.name, "extra_%s.json" % document["scenario"])
        with open(path, "w") as handle:
            json.dump(document, handle)
        server = MockMoonraker(load(path), host="127.0.0.1", port=0, **kwargs)
        await server.start()
        self.addAsyncCleanup(server.stop)
        return server

    async def connect(self, server):
        """A second client socket, on the server under test."""
        ws = await self.session.ws_connect(server.url)
        self.addAsyncCleanup(ws.close)
        return ws

    async def lane_frames(self, ws, until):
        """The `openams_ui oams1` lane pushed up to the `until` op."""
        frames = []
        while True:
            message = await asyncio.wait_for(ws.receive_json(), 6.0)
            if message.get("method") != "notify_status_update":
                continue
            lane = message["params"][0].get("openams_ui oams1", {}).get("lane")
            if lane is None:
                continue
            frames.append(lane)
            if lane.get("op") == until:
                return frames


class MockMoonrakerTest(ClientTestCase):
    async def test_objects_and_subscribe_return_the_fixture_state(self):
        listing = await self.rpc("printer.objects.list")
        self.assertEqual(listing["result"]["objects"],
                         ["oams_manager", "openams_ui oams1", "print_stats"])

        reply = await self.rpc("printer.objects.subscribe",
                               {"objects": {"openams_ui oams1": None,
                                            "print_stats": None}})
        status = reply["result"]["status"]
        self.assertEqual(status["openams_ui oams1"], UI_V1)
        self.assertIn("state", status["print_stats"])

        query = await self.rpc("printer.objects.query",
                               {"objects": {"openams_ui oams1": ["online", "v"]}}, ident=2)
        self.assertEqual(query["result"]["status"]["openams_ui oams1"],
                         {"v": 1, "online": True})

    async def test_server_info_and_printer_info(self):
        info = await self.rpc("server.info")
        self.assertEqual(info["result"]["klippy_state"], "ready")
        self.assertIn("openams_spoolman", info["result"]["components"])
        printer = await self.rpc("printer.info", ident=2)
        self.assertEqual(printer["result"]["state"], "ready")

    async def test_later_event_pushes_only_changed_fields(self):
        await self.rpc("printer.objects.subscribe",
                       {"objects": {"openams_ui oams1": None, "oams_manager": None}})
        self.assertEqual(self.server.apply_due(5.0), 1)
        message = await self.notification("notify_status_update")
        self.assertEqual(message["params"][0], {"openams_ui oams1": {"online": False}})
        self.assertEqual(message["params"][1], 5.0)

    async def test_component_status_is_served_and_pushed(self):
        status = await self.rpc("server.openams_spoolman.status")
        self.assertEqual(status["result"]["spoolman_online"], True)

        self.assertEqual(self.server.apply_due(6.0), 2)
        message = await self.notification("notify_openams_spoolman_status")
        self.assertEqual(message["params"][0]["spoolman_online"], False)

        served = await self.rpc("server.openams_spoolman.status", ident=2)
        self.assertEqual(served["result"]["spoolman_online"], False)

        confirm = await self.rpc("server.openams_spoolman.confirm", ident=3)
        self.assertEqual(confirm["result"], "ok")
        self.assertTrue(any(call["method"] == "server.openams_spoolman.confirm"
                            for call in self.server.calls))

    async def test_gcode_script_is_recorded_and_ok(self):
        reply = await self.rpc("printer.gcode.script", {"script": "OAMSM_LOAD A1 B0"})
        self.assertEqual(reply["result"], "ok")
        scripts = [call for call in self.server.calls
                   if call["method"] == "printer.gcode.script"]
        self.assertEqual(scripts[0]["params"], {"script": "OAMSM_LOAD A1 B0"})
        with open(self.record_path) as handle:
            lines = [json.loads(line) for line in handle if line.strip()]
        self.assertTrue(any(line["method"] == "printer.gcode.script" for line in lines))

    async def test_spoolman_proxy_uses_the_fixture_map(self):
        reply = await self.rpc("server.spoolman.proxy",
                               {"request_method": "GET", "path": "/v1/spool/1"})
        self.assertEqual(reply["result"], {"id": 1, "filament": {}})
        missing = await self.rpc("server.spoolman.proxy",
                                 {"request_method": "GET", "path": "/v1/spool/2"}, ident=2)
        self.assertEqual(missing["error"]["code"], -32000)

    async def test_file_metadata_uses_the_fixture_map(self):
        reply = await self.rpc("server.files.metadata", {"filename": "cube.gcode"})
        self.assertEqual(reply["result"]["filament_weights"], [12.5, 3.0])
        missing = await self.rpc("server.files.metadata", {"filename": "no.gcode"},
                                 ident=2)
        self.assertEqual(missing["error"]["code"], -32000)

    async def test_unknown_method_and_object_error(self):
        reply = await self.rpc("printer.no_such_method")
        self.assertEqual(reply["error"]["code"], -32601)
        unknown = await self.rpc("printer.objects.query",
                                 {"objects": {"not_an_object": None}}, ident=2)
        self.assertIn("error", unknown)

    async def test_c2_status_change_pushes_under_the_object_name(self):
        document = {
            "scenario": "c2_change",
            "seed": 2,
            "family_map": {},
            "events": [
                {"t": 0.0, "kind": "c2_status", "unit": None, "payload": {"a": 1}},
                {"t": 2.0, "kind": "c2_status", "unit": None, "payload": {"a": 2}},
            ],
        }
        server = await self.start_fixture(document, speed=None)
        async with self.session.ws_connect(server.url) as ws:
            await self.rpc("printer.objects.subscribe",
                           {"objects": {"oams_manager": None}}, ws=ws)
            self.assertEqual(server.apply_due(2.0), 1)
            message = await self.notification("notify_status_update", ws=ws)
            self.assertEqual(message["params"][0], {"oams_manager": {"a": 2}})
            self.assertEqual(message["params"][1], 2.0)

    async def test_automatic_timeline_pushes_and_loops(self):
        document = {
            "scenario": "timeline",
            "seed": 1,
            "family_map": {},
            "events": [
                {"t": 0.0, "kind": "c2_status", "unit": None, "payload": {"a": 1}},
                {"t": 0.5, "kind": "c2_status", "unit": None, "payload": {"a": 2}},
            ],
        }
        server = await self.start_fixture(document, speed=1.0, loop=True)
        async with self.session.ws_connect(server.url) as ws:
            reply = await self.rpc("printer.objects.subscribe",
                                   {"objects": {"oams_manager": None}}, ws=ws)
            self.assertEqual(reply["result"]["status"]["oams_manager"], {"a": 1})

            first = await self.notification("notify_status_update", ws=ws)
            self.assertEqual(first["params"][0], {"oams_manager": {"a": 2}})
            self.assertEqual(first["params"][1], 0.5)

            # The loop restarts from an empty state, so the t=0 event pushes
            # the full status again rather than an empty diff.
            second = await self.notification("notify_status_update", ws=ws)
            self.assertEqual(second["params"][0], {"oams_manager": {"a": 1}})

    async def test_speed_zero_applies_everything_on_connect(self):
        server = MockMoonraker(self.fixture, host="127.0.0.1", port=0, speed=0)
        await server.start()
        try:
            async with self.session.ws_connect(server.url) as ws:
                self.assertEqual(server.apply_due(), 0, "all events must be applied already")
                self.assertEqual(server.units["oams1"], UI_V1_CHANGED)
        finally:
            await server.stop()


class SpoolEditTest(ClientTestCase):
    """`server.openams_spoolman.edit` (C4 §9) against the in-memory Spoolman."""

    async def spool_client(self, document=None, **kwargs):
        """A second server on the one unit, connected over a second socket."""
        server = await self.start_fixture(document or spool_document(), **kwargs)
        return server, await self.connect(server)

    async def test_edit_of_a_linked_bay_writes_the_spool(self):
        server, ws = await self.spool_client()
        reply = await self.rpc("server.openams_spoolman.edit",
                               {"unit": "oams1", "bay": 0, "remaining_weight": 700,
                                "color_hex": "FF0000"}, ws=ws)
        result = reply["result"]
        self.assertEqual(result["spool_id"], 12)
        self.assertIs(result["created"], False)
        self.assertEqual(result["bay"], {
            "state": "linked", "spool_id": 12, "source": "tag",
            "pending_confirmation": False, "error": None,
            "spool": {"material": "PLA", "color_hex": "FF0000",
                      "vendor": "Bambu Lab", "remaining_g": 700,
                      "remaining_pct": 70, "initial_g": 1000, "flow_k": 0.02}})

        served = await self.rpc("server.openams_spoolman.status", ident=2, ws=ws)
        self.assertEqual(served["result"]["bays"]["oams1-0"], result["bay"])

        spool = await self.rpc("server.spoolman.proxy",
                               {"request_method": "GET", "path": "/v1/spool/12"},
                               ident=3, ws=ws)
        self.assertEqual(spool["result"]["remaining_weight"], 700)
        self.assertEqual(spool["result"]["filament"]["color_hex"], "ff0000")
        # The shared filament is never edited: a matching one is created.
        self.assertEqual([f["color_hex"] for f in server.spoolman.filaments],
                         ["0A2989", "ff0000"])

    async def test_edit_of_an_unlinked_bay_creates_and_links_a_spool(self):
        server, ws = await self.spool_client()
        reply = await self.rpc("server.openams_spoolman.edit",
                               {"unit": "oams1", "bay": 1, "material": "PETG",
                                "vendor": "Acme", "color_hex": "00ff00",
                                "initial_weight": 750, "remaining_weight": 500},
                               ws=ws)
        result = reply["result"]
        self.assertIs(result["created"], True)
        self.assertEqual(result["spool_id"], 13)
        self.assertEqual(result["bay"], {
            "state": "linked", "spool_id": 13, "source": "manual",
            "pending_confirmation": False, "error": None,
            "spool": {"material": "PETG", "color_hex": "00FF00",
                      "vendor": "Acme", "remaining_g": 500, "remaining_pct": 67,
                      "initial_g": 750, "flow_k": None}})
        # The host link reaches `openams_ui` and Spoolman's location mirror.
        self.assertEqual(server.units["oams1"]["bays"][1]["sp"],
                         {"id": 13, "src": "m"})
        spool = await self.rpc("server.spoolman.proxy",
                               {"request_method": "GET", "path": "/v1/spool/13"},
                               ident=2, ws=ws)
        self.assertEqual(spool["result"]["location"], "oams1-1")

    async def test_empty_bay_and_bad_values_are_refused(self):
        server, ws = await self.spool_client()
        empty = await self.rpc("server.openams_spoolman.edit",
                               {"unit": "oams1", "bay": 2, "material": "PLA"}, ws=ws)
        self.assertEqual(empty["error"]["code"], 409)
        self.assertEqual(empty["error"]["message"], "no spool in oams1 Spool 3")

        bad = await self.rpc("server.openams_spoolman.edit",
                             {"unit": "oams1", "bay": 0, "flow_k": 5}, ident=2, ws=ws)
        self.assertEqual(bad["error"]["code"], 400)
        self.assertEqual(bad["error"]["message"], "flow_k must be between 0 and 2")

        nothing = await self.rpc("server.openams_spoolman.edit",
                                 {"unit": "oams1", "bay": 0}, ident=3, ws=ws)
        self.assertEqual(nothing["error"]["code"], 400)
        self.assertIn("nothing to change", nothing["error"]["message"])

        unknown_unit = await self.rpc("server.openams_spoolman.edit",
                                      {"unit": "oams9", "bay": 0, "material": "PLA"},
                                      ident=4, ws=ws)
        self.assertEqual(unknown_unit["error"]["code"], 404)
        self.assertEqual(unknown_unit["error"]["message"], "unknown unit 'oams9'")

        unknown_bay = await self.rpc("server.openams_spoolman.edit",
                                     {"unit": "oams1", "bay": 9, "material": "PLA"},
                                     ident=5, ws=ws)
        self.assertEqual(unknown_bay["error"]["code"], 404)
        self.assertEqual(unknown_bay["error"]["message"], "oams1 has no Spool 10")
        # A refused edit writes nothing.
        self.assertEqual(server.spoolman.spools_view()[0]["remaining_weight"], 812)

    async def test_proxy_serves_the_spoolman_the_edit_wrote(self):
        document = spool_document(responses={"GET /v1/spool/77": {"id": 77}})
        server, ws = await self.spool_client(document)
        spools = await self.rpc("server.spoolman.proxy",
                                {"request_method": "GET", "path": "/v1/spool"}, ws=ws)
        self.assertEqual([s["id"] for s in spools["result"]], [12])

        vendors = await self.rpc("server.spoolman.proxy",
                                 {"request_method": "GET", "path": "/v1/vendor"},
                                 ident=2, ws=ws)
        self.assertEqual([v["name"] for v in vendors["result"]], ["Bambu Lab"])

        await self.rpc("server.openams_spoolman.edit",
                       {"unit": "oams1", "bay": 1, "material": "PETG",
                        "vendor": "Acme", "color_hex": "00ff00",
                        "initial_weight": 750}, ident=3, ws=ws)

        listed = await self.rpc("server.spoolman.proxy",
                                {"request_method": "GET", "path": "/v1/spool"},
                                ident=4, ws=ws)
        new = [s for s in listed["result"] if s["id"] == 13]
        self.assertEqual(len(new), 1)
        self.assertEqual(new[0]["filament"]["vendor"]["name"], "Acme")
        self.assertEqual(new[0]["remaining_weight"], 750)
        self.assertEqual(new[0]["filament"]["material"], "PETG")

        named = await self.rpc("server.spoolman.proxy",
                               {"request_method": "GET", "path": "/v1/vendor",
                                "query": {"name": "Acme"}}, ident=5, ws=ws)
        self.assertEqual([v["name"] for v in named["result"]], ["Acme"])

        # What the in-memory Spoolman does not hold still falls back to the
        # fixture's own responses, and a miss is still the proxy's error.
        fallback = await self.rpc("server.spoolman.proxy",
                                  {"request_method": "GET", "path": "/v1/spool/77"},
                                  ident=6, ws=ws)
        self.assertEqual(fallback["result"], {"id": 77})
        missing = await self.rpc("server.spoolman.proxy",
                                 {"request_method": "GET", "path": "/v1/spool/99"},
                                 ident=7, ws=ws)
        self.assertEqual(missing["error"]["code"], -32000)

    async def test_an_edit_pushes_the_component_and_the_unit_status(self):
        server, ws = await self.spool_client()
        await self.rpc("printer.objects.subscribe",
                       {"objects": {"openams_ui oams1": None}}, ws=ws)
        await self.rpc("server.openams_spoolman.edit",
                       {"unit": "oams1", "bay": 1, "material": "PETG",
                        "initial_weight": 750}, ident=2, ws=ws)
        messages = {}
        for _ in range(2):
            message = await ws.receive_json()
            messages[message["method"]] = message["params"]
        self.assertEqual(messages["notify_openams_spoolman_status"][0]
                         ["bays"]["oams1-1"]["spool_id"], 13)
        bays = messages["notify_status_update"][0]["openams_ui oams1"]["bays"]
        self.assertEqual(bays[1]["sp"], {"id": 13, "src": "m"})

    async def test_refuse_mode_fails_the_toolchange_scripts(self):
        server, ws = await self.spool_client(gcode_mode="refuse")
        for index, script in enumerate(["OPENAMS_LOAD GROUP=T0 SLOT=0",
                                        "OPENAMS_UNLOAD", "T200"], start=1):
            reply = await self.rpc("printer.gcode.script", {"script": script},
                                   ident=index, ws=ws)
            self.assertEqual(reply["error"]["code"], 400, script)
            self.assertEqual(reply["error"]["message"], REFUSAL_MESSAGE)
        other = await self.rpc("printer.gcode.script", {"script": "G28"},
                               ident=9, ws=ws)
        self.assertEqual(other["result"], "ok")
        self.assertEqual([call["params"]["script"] for call in server.calls
                          if call["method"] == "printer.gcode.script"],
                         ["OPENAMS_LOAD GROUP=T0 SLOT=0", "OPENAMS_UNLOAD",
                          "T200", "G28"])

    async def test_simulate_mode_plays_a_load_then_an_unload(self):
        lane = {"op": "LOADED", "group": "T0", "bay": "oams1-0"}
        document = spool_document("simulate", lane=lane)
        server, ws = await self.spool_client(document, gcode_mode="simulate")
        await self.rpc("printer.objects.subscribe",
                       {"objects": {"openams_ui oams1": None}}, ws=ws)
        reply = await self.rpc("printer.gcode.script",
                               {"script": "OPENAMS_LOAD GROUP=T1 SLOT=1"}, ws=ws)
        self.assertEqual(reply["result"], "ok")

        frames = await self.lane_frames(ws, "LOADED")
        self.assertEqual([frame["op"] for frame in frames],
                         ["LOADING", "LOADING", "LOADING", "LOADED"])
        self.assertEqual([frame["stage"] for frame in frames[:3]],
                         [[["cut", "retract", "feed"], index, None, None, {}]
                          for index in (0, 1, 2)])
        self.assertEqual(frames[-1]["group"], "T1")
        self.assertEqual(frames[-1]["bay"], "oams1-1")
        self.assertIsNone(frames[-1]["stage"])

        await self.rpc("printer.gcode.script", {"script": "OPENAMS_UNLOAD"},
                       ident=2, ws=ws)
        unloading = await self.lane_frames(ws, "UNLOADED")
        self.assertEqual([frame["op"] for frame in unloading],
                         ["UNLOADING", "UNLOADING", "UNLOADED"])
        self.assertEqual(unloading[0]["stage"],
                         [["cut", "retract"], 0, None, None, {}])
        self.assertIsNone(server.units["oams1"]["lane"]["bay"])


if __name__ == "__main__":
    unittest.main()
