"""The openams_bridge Moonraker component against a fake Moonraker and relay."""

import asyncio
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "moonraker"))

from openams_bridge import component as bridge_component  # noqa: E402


class ServerError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class FakeKlippy:
    def __init__(self, state="standby"):
        self.state = state
        self.restarts = []

    async def query_objects(self, objects):
        return {"print_stats": {"state": self.state}}

    async def do_restart(self, gcode):
        self.restarts.append(gcode)


class FakeServer:
    error = ServerError

    def __init__(self, klippy):
        self.endpoints = {}
        self.events = []
        self.klippy = klippy

    def register_notification(self, event, name):
        pass

    def register_endpoint(self, path, methods, handler):
        self.endpoints[path] = handler

    def send_event(self, event, *args):
        self.events.append((event, args))

    def lookup_component(self, name, default=None):
        return self.klippy if name == "klippy_apis" else default

    def get_event_loop(self):
        return asyncio.get_event_loop()


class FakeConfig:
    def __init__(self, server, run_dir):
        self.server = server
        self.run_dir = run_dir

    def get_server(self):
        return self.server

    def get(self, key, default=None):
        return str(self.run_dir) if key == "run_dir" else default

    def getboolean(self, key, default=None):
        return default


class Request:
    def __init__(self, **args):
        self.args = args

    def get_int(self, key, default=None):
        return self.args.get(key, default)


class ComponentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run_dir = Path(self.tmp.name)
        self.klippy = FakeKlippy()
        self.server = FakeServer(self.klippy)
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.addCleanup(self.loop.close)
        self.package = self.run_dir / "bridge-7.oac2"
        self.package.write_bytes(b"x")
        fake_firmware = types.SimpleNamespace(bundled_packages=lambda: [
            {"version": 7, "file": "bridge-7.oac2", "path": str(self.package),
             "ok": True, "problem": "", "released": "", "notes": ""}])
        patcher = mock.patch.object(bridge_component, "_firmware",
                                    lambda: fake_firmware)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.component = bridge_component.OpenAmsBridge(
            FakeConfig(self.server, self.run_dir))

    def write_status(self, online):
        (self.run_dir / "status.json").write_text(json.dumps({
            "state": "running",
            "units": [{"uid": "0a1b2c3d4e5f", "online": online,
                       "path": "/dev/oams/0a1b2c3d4e5f"}]}))

    def call(self, path, **args):
        return self.loop.run_until_complete(
            self.server.endpoints[path](Request(**args)))

    def test_status_reports_relay_and_bundled_firmware(self):
        self.write_status(True)
        result = self.call("/server/openams/bridge/status")
        self.assertEqual(result["relay"]["state"], "running")
        self.assertEqual(result["latest"]["version"], 7)
        self.assertFalse(result["update_running"])

    def test_update_is_refused_while_printing(self):
        self.klippy.state = "printing"
        with self.assertRaises(ServerError) as caught:
            self.call("/server/openams/bridge/update")
        self.assertEqual(caught.exception.status, 409)

    def test_update_needs_the_relay(self):
        with self.assertRaises(ServerError) as caught:
            self.call("/server/openams/bridge/update")
        self.assertEqual(caught.exception.status, 503)

    def test_update_streams_progress_and_restarts_klipper(self):
        requests = []

        async def relay(reader, writer):
            requests.append(json.loads(await reader.readline()))
            for line in ({"event": "accepted", "version": 7},
                         {"event": "progress", "phase": "writing",
                          "done": 1, "total": 2},
                         {"event": "result", "ok": True, "version": 7}):
                writer.write((json.dumps(line) + "\n").encode())
            await writer.drain()
            writer.close()
            self.write_status(True)

        async def scenario():
            server = await asyncio.start_unix_server(
                relay, path=str(self.run_dir / "control.sock"))
            async with server:
                result = await self.server.endpoints[
                    "/server/openams/bridge/update"](Request())
                await self.component.update_task
            return result

        self.write_status(False)
        result = self.loop.run_until_complete(scenario())
        self.assertEqual(result, {"started": True, "version": 7})
        self.assertEqual(requests[0]["method"], "update")
        self.assertEqual(requests[0]["file"], str(self.package))
        events = [args[0]["event"] for _, args in self.server.events]
        self.assertEqual(events, ["accepted", "progress", "result"])
        self.assertEqual(self.klippy.restarts, ["FIRMWARE_RESTART"])
        self.assertTrue(self.component.last_result["ok"])


if __name__ == "__main__":
    unittest.main()
