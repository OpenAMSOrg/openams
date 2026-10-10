"""Tests for tools/agent/bringup_check.py, driven by the recorded idle fixture."""
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "tools", "agent", "bringup_check.py")
IDLE = os.path.join(ROOT, "test", "fixtures", "agent", "idle")

# Written out here rather than imported, so a wrong mapping in the tool cannot
# hide behind a server that makes the same mistake.
ENDPOINTS = {
    "/server/info": "server_info.json",
    "/printer/info": "printer_info.json",
    "/printer/objects/list": "objects_list.json",
    "/printer/objects/query": "objects_query.json",
    "/printer/gcode/help": "gcode_help.json",
    "/server/gcode_store": "gcode_store.json",
    "/server/openams_spoolman/status": "openams_spoolman_status.json",
    "/machine/update/status": "update_status.json",
    "/printer/gcode/script": "selftest_reply.json",
}


def load_tool():
    spec = importlib.util.spec_from_file_location("bringup_check_under_test", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bringup = load_tool()


class FixtureCase(unittest.TestCase):
    """A private copy of the recorded fixture, edited one JSON value at a time."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bringup-check-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.fixture = os.path.join(self.dir, "idle")
        shutil.copytree(IDLE, self.fixture)

    def edit(self, filename, change):
        path = os.path.join(self.fixture, filename)
        with open(path, "r") as handle:
            data = json.load(handle)
        change(data)
        with open(path, "w") as handle:
            json.dump(data, handle)

    def status(self, obj):
        def change(data):
            data["result"]["status"][obj]  # must exist
        return change

    def run_tool(self, *extra):
        out = io.StringIO()
        with redirect_stdout(out):
            code = bringup.main(["--fixture-dir", self.fixture] + list(extra))
        return code, out.getvalue()

    def results(self, *extra):
        """Run the tool and return (exit code, {level: result}, full text)."""
        code, text = self.run_tool(*extra)
        rows = {}
        for line in text.splitlines():
            parts = line.split(" ")
            if len(parts) >= 4 and parts[0][:1] == "L" and parts[0][1:].isdigit():
                rows[int(parts[0][1:])] = parts[2]
        return code, rows, text


class IdleFixtureTests(FixtureCase):
    """The recorded idle printer is a working one: nothing may be reported broken."""

    def test_every_level_passes_or_warns(self):
        code, rows, text = self.results("--selftest")
        for level in range(10):
            self.assertIn(rows.get(level), ("PASS", "WARN"), text)
        self.assertEqual(code, 0, text)

    def test_exact_result_per_level(self):
        _, rows, _ = self.results("--selftest")
        self.assertEqual(
            rows,
            {
                0: "PASS",
                1: "PASS",
                2: "PASS",
                3: "PASS",   # every MCU the printer lists is reporting
                4: "PASS",
                5: "WARN",   # the recording's reader 1 does not answer
                6: "PASS",
                7: "PASS",
                8: "PASS",
                9: "WARN",   # the recorded checkout has no remote: is_valid false
            },
        )

    def test_bringup_tally_is_the_last_line(self):
        _, _, text = self.results("--selftest")
        lines = text.splitlines()
        self.assertEqual(lines[-1], "BRINGUP pass=8 fail=0 warn=2 skip=0")

    def test_detail_lines_are_indented(self):
        _, _, text = self.results("--selftest")
        levels = [line for line in text.splitlines() if line[:1] == "L"]
        self.assertEqual(len(levels), 10, text)
        for line in text.splitlines():
            if line[:1] not in ("L", "B", "") and not line.startswith("  "):
                self.fail("detail line is not indented by two spaces: %r" % line)

    def test_selftest_is_skipped_without_the_flag(self):
        code, rows, text = self.results()
        self.assertEqual(rows.get(7), "SKIP", text)
        self.assertIn("run with --selftest", text)
        self.assertEqual(code, 0, text)
        self.assertEqual(text.splitlines()[-1], "BRINGUP pass=7 fail=0 warn=2 skip=1")

    def test_the_fixtures_are_never_written_to(self):
        before = sorted(os.listdir(IDLE))
        self.run_tool("--selftest")
        self.assertEqual(sorted(os.listdir(IDLE)), before)


def set_status(obj, field, value):
    def change(data):
        data["result"]["status"][obj][field] = value
    return change


def set_result(field, value):
    def change(data):
        data["result"][field] = value
    return change


def set_top(field, value):
    """For the recorded gcode/script reply, which is not wrapped in "result"."""
    def change(data):
        data[field] = value
    return change


class FailureCaseTests(FixtureCase):
    """One broken value per test, each made by editing a copy of the recording."""

    def test_klipper_in_error_blocks_the_levels_that_need_it(self):
        self.edit("printer_info.json", set_result("state", "error"))
        self.edit(
            "printer_info.json",
            set_result("state_message", "mcu 'stm32f072cb': Unable to open port\nconfig error\nline 3"),
        )
        code, rows, text = self.results("--selftest")
        self.assertEqual(rows.get(1), "FAIL", text)
        self.assertIn("state=error", text)
        self.assertIn("mcu 'stm32f072cb': Unable to open port", text)
        self.assertIn("config error", text)
        for level in (3, 4, 5, 6, 7):
            self.assertEqual(rows.get(level), "SKIP", text)
        self.assertIn("L3 MCUS SKIP blocked by L1", text)
        self.assertEqual(rows.get(0), "PASS", text)
        self.assertEqual(code, 1, text)

    def test_moonraker_not_answering_skips_everything_after_it(self):
        os.remove(os.path.join(self.fixture, "server_info.json"))
        code, rows, text = self.results("--selftest")
        self.assertEqual(rows.get(0), "FAIL", text)
        for level in range(1, 10):
            self.assertEqual(rows.get(level), "SKIP", text)
        self.assertIn("L9 UPDATES SKIP blocked by L0", text)
        self.assertEqual(text.splitlines()[-1], "BRINGUP pass=0 fail=1 warn=0 skip=9")
        self.assertEqual(code, 1, text)

    def test_a_printing_printer_is_a_failure_to_stop_and_ask(self):
        self.edit("objects_query.json", set_status("print_stats", "state", "printing"))
        code, rows, text = self.results("--selftest")
        self.assertEqual(rows.get(2), "FAIL", text)
        self.assertIn("printer busy: stop and ask the human", text)
        self.assertEqual(code, 1, text)

    def test_an_mcu_without_a_version_fails(self):
        self.edit("objects_query.json", set_status("mcu fps", "mcu_version", None))
        code, rows, text = self.results("--selftest")
        self.assertEqual(rows.get(3), "FAIL", text)
        self.assertIn("fps", text)
        self.assertEqual(code, 1, text)

    def test_a_disconnected_unit_fails_openams(self):
        self.edit("objects_query.json", set_status("oams oams1", "connected", False))
        code, rows, text = self.results("--selftest")
        self.assertEqual(rows.get(4), "FAIL", text)
        self.assertIn("oams1 is not connected", text)
        self.assertEqual(code, 1, text)

    def test_a_manager_that_is_not_ready_blocks_the_levels_below_it(self):
        self.edit("objects_query.json", set_status("oams_manager", "ready", False))
        code, rows, text = self.results("--selftest")
        self.assertEqual(rows.get(4), "FAIL", text)
        for level in (5, 6, 7):
            self.assertEqual(rows.get(level), "SKIP", text)
        self.assertIn("blocked by L4", text)
        self.assertEqual(code, 1, text)

    def test_a_loaded_fps_on_an_unloaded_lane_is_a_possible_reversed_fps(self):
        self.edit("objects_query.json", set_status("fps", "fps_value", 0.95))
        code, rows, text = self.results("--selftest")
        self.assertEqual(rows.get(5), "WARN", text)
        self.assertIn("fps_is_reversed", text)
        self.assertEqual(code, 0, text)

    def test_an_fps_value_outside_zero_to_one_fails(self):
        self.edit("objects_query.json", set_status("fps", "fps_value", 4.2))
        code, rows, text = self.results("--selftest")
        self.assertEqual(rows.get(5), "FAIL", text)
        self.assertEqual(code, 1, text)

    def test_a_group_without_a_macro_fails(self):
        def rename(data):
            for group in data["result"]["status"]["oams_manager"]["groups"]:
                if group["name"] == "T2":
                    group["name"] = "T7"
        self.edit("objects_query.json", rename)
        code, rows, text = self.results("--selftest")
        self.assertEqual(rows.get(6), "FAIL", text)
        self.assertIn("T7", text)
        self.assertEqual(code, 1, text)

    def test_a_missing_clean_nozzle_only_warns(self):
        def drop(data):
            data["result"].pop("CLEAN_NOZZLE")
        self.edit("gcode_help.json", drop)
        code, rows, text = self.results("--selftest")
        self.assertEqual(rows.get(6), "WARN", text)
        self.assertIn("CLEAN_NOZZLE", text)
        self.assertEqual(code, 0, text)

    def test_a_missing_required_macro_fails(self):
        def drop(data):
            data["result"].pop("SAFE_UNLOAD_FILAMENT")
        self.edit("gcode_help.json", drop)
        code, rows, text = self.results("--selftest")
        self.assertEqual(rows.get(6), "FAIL", text)
        self.assertIn("SAFE_UNLOAD_FILAMENT", text)
        self.assertEqual(code, 1, text)

    def test_a_selftest_without_result_pass_fails(self):
        def rewrite(data):
            entry = data["result"]["gcode_store"][-1]
            entry["message"] = entry["message"].replace("RESULT: PASS", "RESULT: FAIL")
        self.edit("gcode_store.json", rewrite)
        code, rows, text = self.results("--selftest")
        self.assertEqual(rows.get(7), "FAIL", text)
        self.assertIn("RESULT: FAIL", text)
        self.assertEqual(code, 1, text)

    def test_a_selftest_command_moonraker_rejects_fails(self):
        self.edit("selftest_reply.json", set_top("error", "MCU shutdown"))
        code, rows, text = self.results("--selftest")
        self.assertEqual(rows.get(7), "FAIL", text)
        self.assertIn("MCU shutdown", text)
        self.assertEqual(code, 1, text)

    def test_a_missing_selftest_reply_fails(self):
        os.remove(os.path.join(self.fixture, "selftest_reply.json"))
        code, rows, text = self.results("--selftest")
        self.assertEqual(rows.get(7), "FAIL", text)
        self.assertEqual(code, 1, text)

    def test_spoolman_offline_only_warns(self):
        self.edit("openams_spoolman_status.json", set_result("spoolman_online", False))
        code, rows, text = self.results("--selftest")
        self.assertEqual(rows.get(8), "WARN", text)
        self.assertEqual(code, 0, text)

    def test_a_missing_spoolman_endpoint_only_warns(self):
        os.remove(os.path.join(self.fixture, "openams_spoolman_status.json"))
        code, rows, text = self.results("--selftest")
        self.assertEqual(rows.get(8), "WARN", text)
        self.assertEqual(code, 0, text)

    def test_openams_missing_from_the_update_status_only_warns(self):
        def drop(data):
            data["result"]["version_info"].pop("openams")
        self.edit("update_status.json", drop)
        code, rows, text = self.results("--selftest")
        self.assertEqual(rows.get(9), "WARN", text)
        self.assertIn("not in version_info", text)
        self.assertEqual(code, 0, text)

    def test_a_dirty_openams_checkout_warns_with_the_version(self):
        def dirty(data):
            data["result"]["version_info"]["openams"]["is_dirty"] = True
        self.edit("update_status.json", dirty)
        code, rows, text = self.results("--selftest")
        self.assertEqual(rows.get(9), "WARN", text)
        self.assertIn("is_dirty", text)
        self.assertEqual(code, 0, text)

    def test_a_can_bus_that_is_not_active_warns(self):
        def down(data):
            data["result"]["status"]["canbus_stats oams_mcu1"]["bus_state"] = 2
        self.edit("objects_query.json", down)
        code, rows, text = self.results("--selftest")
        self.assertEqual(rows.get(3), "WARN", text)
        self.assertIn("bus_state=2 is not active", text)
        self.assertEqual(code, 0, text)

    def test_a_can_bus_with_burned_error_counters_warns(self):
        def noisy(data):
            data["result"]["status"]["canbus_stats oams_mcu1"]["rx_error"] = 17
        self.edit("objects_query.json", noisy)
        code, rows, text = self.results("--selftest")
        self.assertEqual(rows.get(3), "WARN", text)
        self.assertIn("rx_error=17", text)
        self.assertEqual(code, 0, text)

    def test_a_temperature_outside_minus_twenty_to_eighty_warns(self):
        self.edit("objects_query.json", set_status("aht3x oams", "temperature", 95.0))
        code, rows, text = self.results("--selftest")
        self.assertEqual(rows.get(5), "WARN", text)
        self.assertIn("outside -20..80", text)
        self.assertEqual(code, 0, text)


class FixtureHandler(BaseHTTPRequestHandler):
    """Serves the recorded answers by path, the way a real Moonraker would."""

    def log_message(self, *args):
        pass

    def _answer(self, path, params):
        name = ENDPOINTS.get(path)
        if name is None:
            return 404, {"error": "unknown endpoint"}
        target = os.path.join(self.server.fixture, name)
        if not os.path.isfile(target):
            return 404, {"error": "no such file"}
        with open(target, "r") as handle:
            data = json.load(handle)
        if path == "/printer/objects/query":
            data = {"result": {"status": self._status(data, params)}}
        return 200, data

    @staticmethod
    def _status(data, params):
        """Answer like Klipper: only the objects and the fields that were asked for."""
        recorded = data["result"]["status"]
        # Moonraker's form: each object is a key, its value the fields wanted
        # (none: every field).
        out = {}
        for name, values in params.items():
            if name not in recorded:
                continue
            fields = [f for f in values[0].split(",") if f]
            if not fields:
                out[name] = dict(recorded[name])
                continue
            out[name] = dict((f, recorded[name][f]) for f in fields if f in recorded[name])
        return out

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query, keep_blank_values=True)
        code, payload = self._answer(parsed.path, params)
        self._send(code, payload)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        code, payload = self._answer(urlparse(self.path).path, {})
        self._send(code, payload)

    def _send(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class HttpModeTests(FixtureCase):
    """The live mode must say exactly what the recorded mode says."""

    def setUp(self):
        FixtureCase.setUp(self)
        self.server = HTTPServer(("127.0.0.1", 0), FixtureHandler)
        self.server.fixture = self.fixture
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]

    def run_live(self, *extra):
        out = io.StringIO()
        with redirect_stdout(out):
            code = bringup.main(["--url", self.url] + list(extra))
        return code, out.getvalue()

    def test_live_output_matches_the_fixture_output(self):
        live_code, live_text = self.run_live("--selftest")
        fixture_code, fixture_text = self.run_tool("--selftest")
        self.assertEqual(live_text, fixture_text)
        self.assertEqual(live_code, fixture_code)
        self.assertEqual(live_code, 0, live_text)

    def test_the_api_key_is_sent_as_x_api_key(self):
        seen = []
        original = FixtureHandler.do_GET

        def spy(handler):
            seen.append(handler.headers.get("X-Api-Key"))
            return original(handler)

        FixtureHandler.do_GET = spy
        self.addCleanup(setattr, FixtureHandler, "do_GET", original)
        self.run_live("--api-key", "s3cret")
        self.assertTrue(seen)
        self.assertEqual(set(seen), {"s3cret"})

    def test_a_closed_port_is_l0_fail_and_exit_1_without_a_traceback(self):
        closed = self.url
        self.server.shutdown()
        self.server.server_close()
        done = subprocess.run(
            [sys.executable, TOOL, "--url", closed, "--selftest"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=60,
        )
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("L0 MOONRAKER FAIL", done.stdout)
        self.assertIn("L1 KLIPPY SKIP blocked by L0", done.stdout)
        self.assertNotIn("Traceback", done.stderr)
        self.assertEqual(done.stderr, "")

    def test_a_bad_fixture_dir_is_a_usage_error(self):
        done = subprocess.run(
            [sys.executable, TOOL, "--fixture-dir", os.path.join(self.dir, "nope")],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=60,
        )
        self.assertEqual(done.returncode, 2, done.stderr)


class JsonOutputTests(FixtureCase):
    def test_json_shape(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = bringup.main(["--fixture-dir", self.fixture, "--selftest", "--json"])
        report = json.loads(out.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(sorted(report), ["levels", "summary"])
        self.assertEqual(len(report["levels"]), 10)
        for index, level in enumerate(report["levels"]):
            self.assertEqual(sorted(level), ["detail", "level", "lines", "name", "result"])
            self.assertEqual(level["level"], index)
            self.assertIn(level["result"], ("PASS", "FAIL", "WARN", "SKIP"))
            self.assertIsInstance(level["detail"], str)
            self.assertIsInstance(level["lines"], list)
        self.assertEqual(report["summary"], {"PASS": 8, "FAIL": 0, "WARN": 2, "SKIP": 0})
        self.assertEqual(report["levels"][0]["name"], "MOONRAKER")
        self.assertEqual(report["levels"][9]["name"], "UPDATES")
