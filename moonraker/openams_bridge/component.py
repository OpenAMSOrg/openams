# SPDX-License-Identifier: MIT
"""The Cortex Bridge in Moonraker: status and firmware update for the UIs.

Endpoints (each is also a JSON-RPC method, e.g. ``server.openams.bridge.status``):

GET  /server/openams/bridge/status
    The relay's status file, the firmware bundled with this OpenAMS checkout,
    whether an update is running, and the firmware version the bridge runs:
    ``installed`` (a number, or null when unknown), ``verdict`` ("up-to-date",
    "update-available", "newer-than-bundled", "nothing-bundled" or
    "unknown"), ``update_available`` (true, false or null) and
    ``installed_unknown_reason`` (plain words when ``installed`` is null).
POST /server/openams/bridge/update   [version=<n>]
    Install a bundled package (the newest when no version is given). Refused
    while Klipper is printing or paused, and when the bridge already runs that
    version or a newer one. Progress is broadcast as the
    ``notify_openams_bridge_update`` notification, one message per step,
    ending with ``{"event": "result", "ok": ...}``; a failed result carries
    ``summary``, ``outcome`` and ``action`` sentences for the UI to show.
    After an install Klipper is restarted at once, and the component waits
    for every AMS that was online before the update; ``last_result`` in the
    status then adds ``units_expected``, ``units_back``, ``units_missing``,
    ``units_text`` (e.g. "2 of 2 AMS back online."), ``klipper_state`` and
    ``klipper_restarts``.

After an OpenAMS update the component keeps the bridge relay on the new
code: a relay whose code no longer matches the checkout is restarted once
nothing would be interrupted (no print printing or paused, no firmware
update, Klipper not starting), then Klipper gets FIRMWARE_RESTART if it used
the AMS. ``restart_relay: False`` turns that off; the status reports it as
``relay_code``. At start it also corrects the ``[update_manager openams]``
entry's managed_services once (cortex_bridge.moonraker_conf), and splits the
relay's name off a service name an earlier installer glued it to in
moonraker.asvc (cortex_bridge.moonraker_asvc).

The bridge stops an AMS whose Klipper went quiet for 4 s, as across a
Klipper restart (an OpenAMS update restarts Klipper), and the next Klipper
start then stops with "Can not update MCU '<ams>' config as it is shutdown".
For an MCU on a bridge port the component sends the FIRMWARE_RESTART that
clears it, at most once in 10 minutes.

The relay service does the USB work; this component only reads its status
file and talks to its control socket. It registers endpoints with plain
string request types so it imports nothing from Moonraker itself.
"""

import asyncio
import json
import logging
from pathlib import Path
import re
import sys
import time

_LOGGER = logging.getLogger(__name__)

RUN_DIR = "/run/cortex-bridge"
EVENT_NAME = "openams_bridge:update"
NOTIFY_NAME = "openams_bridge_update"
VERSION_BACK_SECONDS = 30.0
RELAY_SERVICE = "cortex-bridge"
RELAY_FIRST_CHECK_SECONDS = 30.0
RELAY_CHECK_SECONDS = 60.0
RELAY_BACK_SECONDS = 60.0
# At most one FIRMWARE_RESTART this often for a Klipper that started while an
# AMS still held the bridge's stop.
STOPPED_AT_START_PAUSE = 600.0
KLIPPER_SETTLE_SECONDS = 5.0    # the old Klipper exits after FIRMWARE_RESTART
KLIPPER_READY_SECONDS = 60.0
POLL_SECONDS = 1.0


def _openams_root() -> Path:
    # This package lives at <openams>/moonraker/openams_bridge and is linked
    # into Moonraker's components folder; resolve the link to find OpenAMS.
    return Path(__file__).resolve().parents[2]


def _on_path():
    root = str(_openams_root())
    if root not in sys.path:
        sys.path.insert(0, root)


def _firmware():
    _on_path()
    from cortex_bridge import firmware
    return firmware


def _bridge_info():
    _on_path()
    from cortex_bridge import bridge_info
    return bridge_info


def _reconnect():
    _on_path()
    from cortex_bridge import reconnect
    return reconnect


def _code_version():
    _on_path()
    from cortex_bridge import code_version
    return code_version


def _moonraker_conf():
    _on_path()
    from cortex_bridge import moonraker_conf
    return moonraker_conf


def _moonraker_asvc():
    _on_path()
    from cortex_bridge import moonraker_asvc
    return moonraker_asvc


def _running_firmware(relay):
    """The relay's firmware object while a bridge session is up."""
    relay = relay or {}
    if relay.get("state") not in ("running", "connecting"):
        return None
    return relay.get("firmware")


class OpenAmsBridge:
    def __init__(self, config):
        self.server = config.get_server()
        self.run_dir = Path(config.get("run_dir", RUN_DIR))
        self.restart_klipper = config.getboolean("restart_klipper", True)
        self.restart_relay = config.getboolean("restart_relay", True)
        self.relay_task = None
        self.relay_code = {"state": "unchecked", "detail": "",
                           "restarted": None}
        self.stopped_at_start_at = None
        # Where `cortex-bridge.py support --submit` may send a support report
        # (cortex_bridge/support_submit.py), after asking. Unset by default;
        # read here so Moonraker accepts the option. This component sends
        # nothing.
        self.support_url = config.get("support_url", None)
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
        relay = self.read_relay_status()
        firmware = _running_firmware(relay)
        info = _bridge_info()
        comparison = info.compare(firmware, bundled)
        return {
            "relay": relay,
            "bundled": bundled,
            "latest": latest,
            "installed": comparison["installed"],
            "verdict": comparison["verdict"],
            "update_available": None if comparison["installed"] is None or
            latest is None else comparison["verdict"] == "update-available",
            "installed_unknown_reason": "" if comparison["installed"]
            is not None else info.unknown_reason(firmware),
            "update_running": self.update_task is not None and
            not self.update_task.done(),
            "last_result": self.last_result,
            "relay_code": self.relay_code,
        }

    # -- the relay after an OpenAMS update ----------------------------------

    async def component_init(self):
        self.fix_update_entry()
        self.fix_allowed_services()
        if self.restart_relay:
            self.relay_task = self.server.get_event_loop().create_task(
                self._watch_relay())

    def fix_update_entry(self):
        """Correct the OpenAMS update entry's managed_services once; Moonraker
        reads it at its next start."""
        try:
            path = self.server.get_app_args().get("config_file")
        except Exception:
            return None
        if not path:
            return None
        try:
            result = _moonraker_conf().fix_file(path)
        except Exception as error:
            _LOGGER.warning("openams_bridge: could not check the OpenAMS "
                            "update entry: %s", error)
            return None
        if result["changed"]:
            _LOGGER.info("openams_bridge: the OpenAMS update entry now has "
                         "'%s' (the old file is kept as %s); it applies "
                         "from Moonraker's next start", result["line"],
                         result["backup"])
        return result

    def fix_allowed_services(self):
        """Repair moonraker.asvc once (an earlier installer glued the relay's
        name to the last service); Moonraker reads it at its next start."""
        try:
            data_path = self.server.get_app_args().get("data_path")
        except Exception:
            return None
        if not data_path:
            return None
        path = Path(data_path) / "moonraker.asvc"
        try:
            result = _moonraker_asvc().repair(path)
        except Exception as error:
            _LOGGER.warning("openams_bridge: could not check %s: %s", path,
                            error)
            return None
        if result["changed"]:
            _LOGGER.info("openams_bridge: %s has one service name per line "
                         "again (the old file is kept as %s); it applies "
                         "from Moonraker's next start", path,
                         result["backup"])
        return result

    async def _watch_relay(self):
        await asyncio.sleep(RELAY_FIRST_CHECK_SECONDS)
        while True:
            try:
                if await self.check_relay() != "restarted":
                    await self.check_stopped_at_start()
            except Exception as error:
                _LOGGER.warning("openams_bridge: relay check failed: %s",
                                error)
            await asyncio.sleep(RELAY_CHECK_SECONDS)

    async def check_stopped_at_start(self):
        """FIRMWARE_RESTART for a Klipper that stopped at start because an AMS
        behind the bridge still held a stop; None when there is none. Only
        for an MCU on a bridge port, at most once in 10 minutes."""
        klippy_apis = self.server.lookup_component("klippy_apis", None)
        try:
            info = await klippy_apis.get_klippy_info()
        except Exception:
            return None
        message = info.get("state_message", "") or ""
        reconnect = _reconnect()
        if info.get("state") != "error" or \
                reconnect.SHUTDOWN_AT_START not in message:
            return None
        match = re.search(r"MCU '([^']+)'", message)
        if match is None or match.group(1) not in \
                self._bridge_mcus(info.get("config_file")):
            return None
        now = time.monotonic()
        if self.stopped_at_start_at is not None and \
                now - self.stopped_at_start_at < STOPPED_AT_START_PAUSE:
            return None
        self.stopped_at_start_at = now
        _LOGGER.info("openams_bridge: Klipper started with %s still stopped "
                     "by the bridge (%s); FIRMWARE_RESTART", match.group(1),
                     reconnect.first_line(message))
        if not await self._firmware_restart():
            return None
        state, _ = await self._klipper_after_restart(())
        return state

    @staticmethod
    def _bridge_mcus(printer_cfg):
        """The [mcu] names on a bridge port in Klipper's config folder."""
        if not printer_cfg:
            return set()
        _on_path()
        from cortex_bridge import config as bridge_config
        names = set()
        for path in Path(printer_cfg).expanduser().parent.glob("*.cfg"):
            try:
                names |= set(bridge_config.bridge_ports(
                    path.read_text(encoding="utf-8", errors="replace")))
            except OSError:
                continue
        return names

    def _relay_state(self, state, detail="", **extra):
        self.relay_code = dict(self.relay_code, state=state, detail=detail,
                               **extra)
        return state

    async def check_relay(self):
        """One look. A relay running older code than the checkout (an
        OpenAMS update changed it) is restarted, but only when nothing would
        be interrupted; then Klipper gets FIRMWARE_RESTART if it used the AMS
        ports (it lost them). Returns what was found or done."""
        status = self.read_relay_status() or {}
        outdated = _code_version().outdated(status)
        if not outdated:
            return self._relay_state(
                "current" if outdated is False else "not-running")
        if (self.update_task is not None and not self.update_task.done()) \
                or status.get("state") == "updating" \
                or status.get("ams_update"):
            return self._relay_state("waiting", "a firmware update is running")
        state, _ = await self._klippy_state()
        if state not in ("ready", "error", "shutdown"):
            return self._relay_state("waiting", "Klipper is not ready")
        if state == "ready" and \
                await self._printer_state() not in ("standby", "complete",
                                                    "cancelled", "error"):
            return self._relay_state("waiting", "a print is running")
        machine = self.server.lookup_component("machine", None)
        if machine is None:
            return self._relay_state("waiting", "no machine component")
        reconnect = _reconnect()
        in_use = reconnect.online_uids(status)
        _LOGGER.info("openams_bridge: the bridge relay runs OpenAMS code from "
                     "before an update; restarting it")
        try:
            await machine.do_service_action("restart", RELAY_SERVICE)
        except Exception as error:
            return self._relay_state("failed", str(error))
        if not await self._wait_relay_current():
            return self._relay_state(
                "failed", "the relay did not come back on the new code")
        restarted = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if not in_use or not self.restart_klipper:
            return self._relay_state("restarted", restarted=restarted)
        # Klipper lost every AMS port with the old relay.
        seen = await self._wait_for_units(in_use)
        klipper = None
        if await self._firmware_restart():
            klipper, _ = await self._klipper_after_restart(seen["missing"])
        _LOGGER.info("openams_bridge: relay restarted; %s Klipper is %s",
                     " ".join(reconnect.summary(in_use, seen, seen["waited"])),
                     klipper)
        return self._relay_state("restarted", restarted=restarted,
                                 klipper_state=klipper)

    async def _wait_relay_current(self):
        deadline = time.monotonic() + RELAY_BACK_SECONDS
        while time.monotonic() < deadline:
            if _code_version().outdated(self.read_relay_status()) is False:
                return True
            await asyncio.sleep(POLL_SECONDS)
        return False

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
        firmware = _running_firmware(self.read_relay_status()) or {}
        if firmware.get("state") == "known" and \
                int(package.get("version") or 0) <= int(firmware["version"]):
            raise self.server.error(
                "The bridge already runs version %s; there is nothing to "
                "install" % firmware["version"]
                if int(package.get("version") or 0) == firmware["version"]
                else "The bridge runs version %s, newer than version %s; "
                "there is nothing to install"
                % (firmware["version"], package.get("version")), 409)
        if not (self.run_dir / "control.sock").exists():
            raise self.server.error("The Cortex Bridge relay is not running",
                                    503)
        # The units to wait for once the bridge is back: all that run now.
        expected = _reconnect().expected_uids(self.read_relay_status())
        self.update_task = self.server.get_event_loop().create_task(
            self._run_update(package, expected))
        return {"started": True, "version": package.get("version")}

    def _broadcast(self, message):
        self.server.send_event(EVENT_NAME, message)

    async def _run_update(self, package, expected=()):
        final = {"event": "result", "ok": False, "error": "no-reply",
                 "message": "the relay closed the connection"}
        try:
            reader, writer = await asyncio.open_unix_connection(
                str(self.run_dir / "control.sock"))
            request = {"method": "update", "file": package["path"],
                       "source": "bundled"}
            writer.write((json.dumps(request) + "\n").encode("utf-8"))
            await writer.drain()
            accepted = False
            while True:
                line = await reader.readline()
                if not line:
                    break
                message = json.loads(line.decode("utf-8"))
                self._broadcast(message)
                if message.get("event") == "accepted":
                    # The relay left the bridge session: Klipper loses the
                    # AMS from here on, whatever the result.
                    accepted = True
                if message.get("event") == "result":
                    final = message
            writer.close()
            if not final.get("ok") and accepted and \
                    final.get("post_state") not in ("receiver", "fault"):
                # Nothing installed, but Klipper lost the AMS: bring it back
                # as after an install. A bridge left waiting for firmware
                # keeps its AMS offline until an update succeeds.
                restarted = self.restart_klipper and \
                    await self._firmware_restart()
                seen = await self._wait_for_units(list(expected))
                final = dict(final, units_expected=list(expected),
                             units_back=seen["back"],
                             units_missing=seen["missing"],
                             units_text=" ".join(_reconnect().summary(
                                 list(expected), seen, seen["waited"])))
                if restarted:
                    state, restarts = await self._klipper_after_restart(
                        seen["missing"])
                    final = dict(final, klipper_state=state,
                                 klipper_restarts=restarts)
            if final.get("ok") and final.get("changed", True):
                # Klipper is restarted now, before the AMS are back: the
                # Klipper that ran through the install keeps sending to their
                # ports, and once they were back it left one stopped ("Can not
                # update MCU '...' config as it is shutdown"). Started now,
                # Klipper waits for each AMS port to answer.
                restarted = self.restart_klipper and \
                    await self._firmware_restart()
                seen = await self._wait_for_units(list(expected))
                reconnect = _reconnect()
                final = dict(
                    final, units_expected=list(expected),
                    units_back=seen["back"], units_missing=seen["missing"],
                    units_text=" ".join(reconnect.summary(
                        list(expected), seen, seen["waited"])))
                firmware = await self._wait_for_version()
                if firmware and firmware.get("state") == "known":
                    final = dict(final, installed_version=firmware["version"])
                if restarted:
                    state, restarts = await self._klipper_after_restart(
                        seen["missing"])
                    final = dict(final, klipper_state=state,
                                 klipper_restarts=restarts)
        except Exception as error:
            final = {"event": "result", "ok": False,
                     "error": error.__class__.__name__,
                     "message": str(error)}
            self._broadcast(final)
        self.last_result = final

    async def _wait_for_units(self, expected):
        """Every unit in ``expected`` online at once, or the wait's end (with
        none expected, a while for any): ``reconnect.progress()`` plus
        ``waited``. The units come back one by one; the first is not all."""
        reconnect = _reconnect()
        seconds = reconnect.UNITS_BACK_WAIT if expected \
            else reconnect.ANY_UNIT_WAIT
        start = time.monotonic()
        while True:
            seen = reconnect.progress(self.read_relay_status(), expected)
            waited = time.monotonic() - start
            if (expected and not seen["missing"]) or waited >= seconds:
                seen["waited"] = waited
                return seen
            await asyncio.sleep(POLL_SECONDS)

    async def _wait_for_version(self):
        """The version the restarted bridge reports, as the relay's
        firmware object; None if it did not answer in time."""
        deadline = time.monotonic() + VERSION_BACK_SECONDS
        while True:
            firmware = _running_firmware(self.read_relay_status())
            if firmware and firmware.get("state") != "querying":
                return firmware
            if time.monotonic() >= deadline:
                return firmware
            await asyncio.sleep(1.0)

    async def _firmware_restart(self):
        """True when Klipper took the FIRMWARE_RESTART."""
        klippy_apis = self.server.lookup_component("klippy_apis", None)
        if klippy_apis is None:
            return False
        try:
            await klippy_apis.do_restart("FIRMWARE_RESTART")
        except Exception as error:
            _LOGGER.warning("openams_bridge: FIRMWARE_RESTART failed: %s",
                            error)
            return False
        return True

    async def _klippy_state(self):
        """(state, state_message), or (None, "") while Klipper is away."""
        klippy_apis = self.server.lookup_component("klippy_apis", None)
        try:
            info = await klippy_apis.get_klippy_info()
        except Exception:
            return None, ""
        return info.get("state"), info.get("state_message", "")

    async def _klipper_after_restart(self, missing):
        """Wait for Klipper after the FIRMWARE_RESTART; one more when it
        stopped in a way that one fixes (an AMS port still reporting the
        earlier Klipper's stop) and no AMS is missing. Returns its last state
        and how many restarts were sent."""
        reconnect = _reconnect()
        restarts = 1
        while True:
            await asyncio.sleep(KLIPPER_SETTLE_SECONDS)
            deadline = time.monotonic() + KLIPPER_READY_SECONDS
            while True:
                state, message = await self._klippy_state()
                if state in ("ready", "error", "shutdown") or \
                        time.monotonic() >= deadline:
                    break
                await asyncio.sleep(POLL_SECONDS)
            if state == "ready" or restarts > 1 or missing or \
                    not reconnect.restart_clears(state, message):
                return state, restarts
            _LOGGER.info("openams_bridge: Klipper stopped with %r after the "
                         "update; FIRMWARE_RESTART once more",
                         reconnect.first_line(message))
            if not await self._firmware_restart():
                return state, restarts
            restarts += 1
