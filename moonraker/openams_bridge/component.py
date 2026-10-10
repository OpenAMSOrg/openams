# SPDX-License-Identifier: MIT
"""The Cortex Bridge in Moonraker: status and firmware update for the UIs.

Endpoints (each is also a JSON-RPC method, e.g. ``server.openams.bridge.status``):

GET  /server/openams/bridge/status
    The relay's status file, the firmware bundled with this OpenAMS checkout,
    and whether an update is running.
POST /server/openams/bridge/update   [version=<n>]
    Install a bundled package (the newest when no version is given). Refused
    while Klipper is printing or paused. Progress is broadcast as the
    ``notify_openams_bridge_update`` notification, one message per step,
    ending with ``{"event": "result", "ok": ...}``.

The relay service does the USB work; this component only reads its status
file and talks to its control socket. It registers endpoints with plain
string request types so it imports nothing from Moonraker itself.
"""

import asyncio
import json
import logging
from pathlib import Path
import sys
import time

_LOGGER = logging.getLogger(__name__)

RUN_DIR = "/run/cortex-bridge"
EVENT_NAME = "openams_bridge:update"
NOTIFY_NAME = "openams_bridge_update"
UNITS_BACK_SECONDS = 90.0


def _openams_root() -> Path:
    # This package lives at <openams>/moonraker/openams_bridge and is linked
    # into Moonraker's components folder; resolve the link to find OpenAMS.
    return Path(__file__).resolve().parents[2]


def _firmware():
    root = str(_openams_root())
    if root not in sys.path:
        sys.path.insert(0, root)
    from cortex_bridge import firmware
    return firmware


class OpenAmsBridge:
    def __init__(self, config):
        self.server = config.get_server()
        self.run_dir = Path(config.get("run_dir", RUN_DIR))
        self.restart_klipper = config.getboolean("restart_klipper", True)
        self.update_task = None
        self.last_result = None
        self.server.register_notification(EVENT_NAME, NOTIFY_NAME)
        self.server.register_endpoint(
            "/server/openams/bridge/status", ["GET"], self._handle_status)
        self.server.register_endpoint(
            "/server/openams/bridge/update", ["POST"], self._handle_update)

    # -- status -----------------------------------------------------------

    def read_relay_status(self):
        try:
            return json.loads((self.run_dir / "status.json").read_text())
        except (OSError, ValueError):
            return None

    def bundled(self):
        try:
            packages = _firmware().bundled_packages()
        except Exception as error:  # a broken manifest must not break status
            _LOGGER.warning("openams_bridge: firmware manifest: %s", error)
            return []
        return [
            {key: package.get(key) for key in
             ("version", "file", "released", "notes", "ok", "problem")}
            for package in packages
        ]

    async def _handle_status(self, web_request):
        bundled = self.bundled()
        latest = next((p for p in bundled if p["ok"]), None)
        return {
            "relay": self.read_relay_status(),
            "bundled": bundled,
            "latest": latest,
            "update_running": self.update_task is not None and
            not self.update_task.done(),
            "last_result": self.last_result,
        }

    # -- update -----------------------------------------------------------

    async def _printer_state(self):
        klippy_apis = self.server.lookup_component("klippy_apis", None)
        if klippy_apis is None:
            return None
        try:
            result = await klippy_apis.query_objects({"print_stats": None})
        except Exception:
            return None
        return result.get("print_stats", {}).get("state")

    async def _handle_update(self, web_request):
        if self.update_task is not None and not self.update_task.done():
            raise self.server.error("A bridge update is already running", 409)
        state = await self._printer_state()
        if state in ("printing", "paused"):
            raise self.server.error(
                "The printer is %s; update the bridge when it is idle" % state,
                409)
        version = web_request.get_int("version", None)
        packages = [p for p in _firmware().bundled_packages() if p["ok"]]
        if version is not None:
            packages = [p for p in packages
                        if int(p.get("version") or 0) == version]
        if not packages:
            raise self.server.error(
                "No matching bridge firmware is bundled with this OpenAMS",
                404)
        package = packages[0]
        if not (self.run_dir / "control.sock").exists():
            raise self.server.error("The Cortex Bridge relay is not running",
                                    503)
        self.update_task = self.server.get_event_loop().create_task(
            self._run_update(package))
        return {"started": True, "version": package.get("version")}

    def _broadcast(self, message):
        self.server.send_event(EVENT_NAME, message)

    async def _run_update(self, package):
        final = {"event": "result", "ok": False, "error": "no-reply",
                 "message": "the relay closed the connection"}
        try:
            reader, writer = await asyncio.open_unix_connection(
                str(self.run_dir / "control.sock"))
            request = {"method": "update", "file": package["path"],
                       "source": "bundled"}
            writer.write((json.dumps(request) + "\n").encode("utf-8"))
            await writer.drain()
            while True:
                line = await reader.readline()
                if not line:
                    break
                message = json.loads(line.decode("utf-8"))
                self._broadcast(message)
                if message.get("event") == "result":
                    final = message
            writer.close()
            if final.get("ok"):
                await self._wait_for_units()
                if self.restart_klipper:
                    await self._firmware_restart()
        except Exception as error:
            final = {"event": "result", "ok": False,
                     "error": error.__class__.__name__,
                     "message": str(error)}
            self._broadcast(final)
        self.last_result = final

    async def _wait_for_units(self):
        deadline = time.monotonic() + UNITS_BACK_SECONDS
        while time.monotonic() < deadline:
            status = self.read_relay_status() or {}
            if any(unit.get("online") for unit in status.get("units", [])):
                return True
            await asyncio.sleep(1.0)
        return False

    async def _firmware_restart(self):
        klippy_apis = self.server.lookup_component("klippy_apis", None)
        if klippy_apis is None:
            return
        try:
            await klippy_apis.do_restart("FIRMWARE_RESTART")
        except Exception as error:
            _LOGGER.warning("openams_bridge: FIRMWARE_RESTART failed: %s",
                            error)
