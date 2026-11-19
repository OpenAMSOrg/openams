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
        # printer.info after each FIRMWARE_RESTART (the last one repeats);
        # "ready" when not scripted.
        self.after_restart = []
        self.on_restart = None

    async def query_objects(self, objects):
        return {"print_stats": {"state": self.state}}

    async def do_restart(self, gcode):
        self.restarts.append(gcode)
        if self.on_restart is not None:
            self.on_restart()

    async def get_klippy_info(self):
        index = len(self.restarts) - 1
        if index < 0 and self.before_restart is not None:
            return self.before_restart
        if 0 <= index < len(self.after_restart):
            return self.after_restart[index]
        return {"state": "ready", "state_message": "Printer is ready"}

    before_restart = None


class FakeMachine:
    def __init__(self, on_restart=None):
        self.actions = []
        self.on_restart = on_restart

    async def do_service_action(self, action, service):
        self.actions.append((action, service))
        if self.on_restart is not None:
            self.on_restart()


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

    machine = None
    app_args = {}

    def lookup_component(self, name, default=None):
        if name == "klippy_apis":
            return self.klippy
        if name == "machine" and self.machine is not None:
            return self.machine
        return default

    def get_app_args(self):
        return self.app_args

    def get_event_loop(self):
        return asyncio.get_event_loop()


class FakeConfig:
    def __init__(self, server, run_dir, options=None):
        self.server = server
        self.run_dir = run_dir
        self.options = dict(options or {})
        self.read = set()

    def get_server(self):
        return self.server

    def get(self, key, default=None):
        self.read.add(key)
        if key == "run_dir":
            return str(self.run_dir)
        return self.options.get(key, default)

    def getboolean(self, key, default=None):
        return default


class Request:
    def __init__(self, **args):
        self.args = args

    def get_int(self, key, default=None):
        return self.args.get(key, default)


class ComponentCase(unittest.TestCase):
    """The component against the fakes; no tests of its own."""

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
        for name, value in (("POLL_SECONDS", 0.01),
                            ("KLIPPER_SETTLE_SECONDS", 0.0)):
            patcher = mock.patch.object(bridge_component, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)


class ComponentTests(ComponentCase):
    def write_status(self, online, version=None, state="known",
                     reason="", second=None):
        """One unit, or two with ``second`` (the other unit's online)."""
        firmware = None
        if version is not None or state != "known":
            firmware = {"state": state, "reason": reason, "version": version}
        units = [("0a1b2c3d4e5f", online)]
        if second is not None:
            units.append(("5f4e3d2c1b0a", second))
        (self.run_dir / "status.json").write_text(json.dumps({
            "state": "running",
            "firmware": firmware,
            "units": [{"uid": uid, "online": up, "path": "/dev/oams/" + uid}
                      for uid, up in units]}))

    def call(self, path, **args):
        return self.loop.run_until_complete(
            self.server.endpoints[path](Request(**args)))

    def test_support_url_is_accepted_and_unset_by_default(self):
        """Moonraker warns about options a component does not read; the
        support report's destination lives in this section."""
        config = FakeConfig(self.server, self.run_dir)
        component = bridge_component.OpenAmsBridge(config)
        self.assertIn("support_url", config.read)
        self.assertIsNone(component.support_url)
        config = FakeConfig(self.server, self.run_dir,
                            {"support_url": "https://support.example.org/r"})
        self.assertEqual(bridge_component.OpenAmsBridge(config).support_url,
                         "https://support.example.org/r")

    def test_status_reports_relay_and_bundled_firmware(self):
        self.write_status(True)
        result = self.call("/server/openams/bridge/status")
        self.assertEqual(result["relay"]["state"], "running")
        self.assertEqual(result["latest"]["version"], 7)
        self.assertFalse(result["update_running"])

    def test_status_says_whether_the_bridge_is_up_to_date(self):
        self.write_status(True, version=7)
        result = self.call("/server/openams/bridge/status")
        self.assertEqual(result["installed"], 7)
        self.assertEqual(result["verdict"], "up-to-date")
        self.assertFalse(result["update_available"])
        self.write_status(True, version=4)
        result = self.call("/server/openams/bridge/status")
        self.assertEqual(result["verdict"], "update-available")
        self.assertTrue(result["update_available"])
        self.write_status(True, state="unavailable", reason="lab-firmware")
        result = self.call("/server/openams/bridge/status")
        self.assertIsNone(result["installed"])
        self.assertIsNone(result["update_available"])
        self.assertIn("lab", result["installed_unknown_reason"])

    def test_update_is_refused_when_the_bridge_already_runs_it(self):
        self.write_status(True, version=7)
        with self.assertRaises(ServerError) as caught:
            self.call("/server/openams/bridge/update")
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("already runs version 7", str(caught.exception))

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
            self.write_status(True, version=7)

        async def scenario():
            server = await asyncio.start_unix_server(
                relay, path=str(self.run_dir / "control.sock"))
            async with server:
                result = await self.server.endpoints[
                    "/server/openams/bridge/update"](Request())
                await self.component.update_task
            return result

        self.write_status(True, version=4)
        result = self.loop.run_until_complete(scenario())
        self.assertEqual(result, {"started": True, "version": 7})
        self.assertEqual(requests[0]["method"], "update")
        self.assertEqual(requests[0]["file"], str(self.package))
        events = [args[0]["event"] for _, args in self.server.events]
        self.assertEqual(events, ["accepted", "progress", "result"])
        self.assertEqual(self.klippy.restarts, ["FIRMWARE_RESTART"])
        self.assertTrue(self.component.last_result["ok"])
        self.assertEqual(self.component.last_result["installed_version"], 7)
        self.assertEqual(self.component.last_result["units_text"],
                         "1 of 1 AMS back online.")
        self.assertEqual(self.component.last_result["klipper_state"], "ready")

    def run_update(self, lines=None):
        """An update whose relay leaves both units offline at its result;
        the test brings them back. ``lines``: what the relay sends."""
        lines = lines or [{"event": "result", "ok": True, "version": 7}]

        async def relay(reader, writer):
            await reader.readline()
            for line in lines:
                writer.write((json.dumps(line) + "\n").encode())
            await writer.drain()
            writer.close()
            self.write_status(False, version=7, second=False)

        async def scenario():
            server = await asyncio.start_unix_server(
                relay, path=str(self.run_dir / "control.sock"))
            async with server:
                await self.server.endpoints[
                    "/server/openams/bridge/update"](Request())
                await self.component.update_task

        self.loop.run_until_complete(scenario())
        return self.component.last_result

    def test_update_waits_for_every_unit_and_restarts_klipper_first(self):
        # Two units online before the update; after it the first is back
        # at once, the second a little later. Klipper is restarted before
        # either is back, and its first start finds a stopped AMS port.
        self.write_status(True, version=4, second=True)
        seen_at_restart = []

        def restarted():
            seen_at_restart.append(json.loads(
                (self.run_dir / "status.json").read_text())["units"])
            if len(self.klippy.restarts) == 1:
                loop = asyncio.get_event_loop()
                loop.call_later(0.02, self.write_status, True, 7, "known",
                                "", False)
                loop.call_later(0.15, self.write_status, True, 7, "known",
                                "", True)
        self.klippy.on_restart = restarted
        self.klippy.after_restart = [
            {"state": "error", "state_message":
             "Can not update MCU 'ams2' config as it is shutdown"}]
        result = self.run_update()
        self.assertFalse(any(unit["online"] for unit in seen_at_restart[0]),
                         "the first FIRMWARE_RESTART came before the units")
        self.assertEqual(result["units_expected"],
                         ["0a1b2c3d4e5f", "5f4e3d2c1b0a"])
        self.assertEqual(result["units_missing"], [])
        self.assertEqual(result["units_text"], "2 of 2 AMS back online.")
        self.assertEqual(self.klippy.restarts, ["FIRMWARE_RESTART"] * 2)
        self.assertEqual(result["klipper_restarts"], 2)
        self.assertEqual(result["klipper_state"], "ready")

    def test_a_failure_after_the_detach_brings_klipper_back(self):
        # The bridge refused update mode after the relay left the session:
        # nothing installed, but Klipper lost the AMS.
        self.write_status(True, version=4, second=True)
        self.klippy.on_restart = lambda: asyncio.get_event_loop().call_later(
            0.02, self.write_status, True, 4, "known", "", True)
        result = self.run_update([
            {"event": "accepted", "version": 7},
            {"event": "result", "ok": False, "error": "UpdateModeRefused",
             "post_state": ""}])
        self.assertFalse(result["ok"])
        self.assertEqual(self.klippy.restarts, ["FIRMWARE_RESTART"])
        self.assertEqual(result["units_text"], "2 of 2 AMS back online.")
        self.assertEqual(result["klipper_state"], "ready")

    def test_a_refusal_before_the_detach_leaves_klipper_alone(self):
        self.write_status(True, version=4, second=True)
        result = self.run_update([
            {"event": "result", "ok": False, "error": "BridgeBusy"}])
        self.assertFalse(result["ok"])
        self.assertEqual(self.klippy.restarts, [])

    def test_a_bridge_left_waiting_for_firmware_is_not_restarted(self):
        self.write_status(True, version=4, second=True)
        result = self.run_update([
            {"event": "accepted", "version": 7},
            {"event": "result", "ok": False, "error": "TransferInterrupted",
             "post_state": "receiver"}])
        self.assertFalse(result["ok"])
        self.assertEqual(self.klippy.restarts, [])

    def test_a_missing_unit_is_reported_and_klipper_not_retried(self):
        reconnect = bridge_component._reconnect()
        patcher = mock.patch.object(reconnect, "UNITS_BACK_WAIT", 0.2)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.write_status(True, version=4, second=True)
        self.klippy.on_restart = lambda: asyncio.get_event_loop().call_later(
            0.02, self.write_status, True, 7, "known", "", False)
        self.klippy.after_restart = [
            {"state": "error",
             "state_message": "mcu 'ams1': Unable to connect"}]
        result = self.run_update()
        self.assertEqual(result["units_back"], ["0a1b2c3d4e5f"])
        self.assertEqual(result["units_missing"], ["5f4e3d2c1b0a"])
        self.assertIn("1 of 2 AMS back after 0 s: 5f4e3d2c1b0a not yet.",
                      result["units_text"])
        self.assertEqual(self.klippy.restarts, ["FIRMWARE_RESTART"])
        self.assertEqual(result["klipper_state"], "error")



class RelayAfterUpdateTests(ComponentCase):
    """An OpenAMS update changed the relay's code: the component restarts it
    when nothing would be interrupted, then FIRMWARE_RESTARTs Klipper."""

    def setUp(self):
        super().setUp()
        self.code = bridge_component._code_version()
        self.machine = FakeMachine(on_restart=self.relay_restarted)
        self.server.machine = self.machine
        self.write_relay(outdated=True)

    def write_relay(self, outdated, state="running", **extra):
        status = {"state": state, "firmware": None,
                  "units": [{"uid": "0a1b2c3d4e5f", "online": True,
                             "path": "/dev/oams/0a1b2c3d4e5f"}]}
        if not outdated:
            status["code"] = self.code.snapshot()
        status.update(extra)
        (self.run_dir / "status.json").write_text(json.dumps(status))

    def relay_restarted(self):
        self.write_relay(outdated=False)

    def check(self):
        return self.loop.run_until_complete(self.component.check_relay())

    def test_an_idle_printer_gets_the_relay_restarted_and_klipper_back(self):
        self.assertEqual(self.check(), "restarted")
        self.assertEqual(self.machine.actions, [("restart", "cortex-bridge")])
        self.assertEqual(self.klippy.restarts, ["FIRMWARE_RESTART"])
        self.assertEqual(self.component.relay_code["state"], "restarted")
        self.assertEqual(self.component.relay_code["klipper_state"], "ready")
        self.assertEqual(self.check(), "current", "only once")
        self.assertEqual(len(self.machine.actions), 1)

    def test_never_while_printing_or_paused(self):
        for state in ("printing", "paused"):
            self.klippy.state = state
            self.assertEqual(self.check(), "waiting")
            self.assertEqual(self.component.relay_code["detail"],
                             "a print is running")
        self.assertEqual(self.machine.actions, [])
        self.assertEqual(self.klippy.restarts, [])

    def test_never_during_a_firmware_update(self):
        self.write_relay(outdated=True, ams_update=True)
        self.assertEqual(self.check(), "waiting")
        self.write_relay(outdated=True, state="updating")
        self.assertEqual(self.check(), "waiting")
        self.assertEqual(self.machine.actions, [])

    def test_not_while_klipper_is_starting(self):
        self.klippy.before_restart = {"state": "startup"}
        self.assertEqual(self.check(), "waiting")
        self.assertEqual(self.machine.actions, [])

    def test_a_current_relay_is_left_alone(self):
        self.write_relay(outdated=False)
        self.assertEqual(self.check(), "current")
        self.assertEqual(self.machine.actions, [])

    def test_a_relay_with_no_unit_in_use_needs_no_klipper_restart(self):
        self.write_relay(outdated=True, units=[])
        self.assertEqual(self.check(), "restarted")
        self.assertEqual(self.klippy.restarts, [])


class StoppedAtStartTests(ComponentCase):
    """Klipper restarted (an OpenAMS update) while an AMS kept the bridge's
    stop: one FIRMWARE_RESTART, only for an MCU on a bridge port."""

    def setUp(self):
        super().setUp()
        self.config_dir = self.run_dir / "config"
        self.config_dir.mkdir()
        (self.config_dir / "oams.cfg").write_text(
            "[mcu ams2]\nserial: /dev/oams/0a1b2c3d4e5f\n\n[mcu]\n"
            "serial: /dev/serial/by-id/usb-Klipper\n")

    def stopped(self, mcu):
        self.klippy.before_restart = {
            "state": "error", "config_file": str(self.config_dir /
                                                 "printer.cfg"),
            "state_message": "Can not update MCU '%s' config as it is "
                             "shutdown\nOnce the underlying issue" % mcu}
        return self.loop.run_until_complete(
            self.component.check_stopped_at_start())

    def test_a_bridge_unit_gets_one_restart(self):
        self.assertEqual(self.stopped("ams2"), "ready")
        self.assertEqual(self.klippy.restarts, ["FIRMWARE_RESTART"])
        self.klippy.restarts.clear()
        self.assertIsNone(self.stopped("ams2"), "not again within 10 min")
        self.assertEqual(self.klippy.restarts, [])

    def test_another_mcu_is_left_to_the_user(self):
        self.assertIsNone(self.stopped("mcu"))
        self.assertEqual(self.klippy.restarts, [])

    def test_other_errors_are_left_alone(self):
        self.klippy.before_restart = {"state": "error",
                                      "state_message": "Option 'x' missing"}
        self.assertIsNone(self.loop.run_until_complete(
            self.component.check_stopped_at_start()))
        self.assertEqual(self.klippy.restarts, [])


class UpdateEntryFixTests(ComponentCase):
    def test_the_entry_is_corrected_at_start(self):
        conf = self.run_dir / "moonraker.conf"
        conf.write_text("[update_manager openams]\ntype: git_repo\n"
                        "managed_services: cortex-bridge klipper\n")
        self.server.app_args = {"config_file": str(conf)}
        result = self.component.fix_update_entry()
        self.assertTrue(result["changed"])
        self.assertIn("managed_services: klipper moonraker", conf.read_text())
        self.assertFalse(self.component.fix_update_entry()["changed"])

    def test_the_allowed_services_are_repaired_at_start(self):
        asvc = self.run_dir / "moonraker.asvc"
        asvc.write_text("klipper_mcu\ncrowsnestcortex-bridge\ncortex-bridge\n")
        self.server.app_args = {"data_path": str(self.run_dir)}
        self.assertTrue(self.component.fix_allowed_services()["changed"])
        self.assertEqual(asvc.read_text(),
                         "klipper_mcu\ncrowsnest\ncortex-bridge\n")
        self.assertFalse(self.component.fix_allowed_services()["changed"])
        asvc.unlink()
        self.assertFalse(self.component.fix_allowed_services()["changed"])


if __name__ == "__main__":
    unittest.main()
