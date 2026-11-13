"""The host's side of an AMS firmware update through the bridge (bridge 22):
the request and report layout, and the driver against a simulated bridge.

Every image here is made up. Nothing here touches hardware.
"""

import os
from pathlib import Path
import struct
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cortex_bridge import ams_update as au  # noqa: E402
from cortex_bridge.cortexd import (  # noqa: E402
    Direction, Message, MessageType, ProtocolError)

PHASE = {name: i for i, name in enumerate(au.PHASES)}
CODE = {name: i for i, name in enumerate(au.CODES)}


def image(body=5000):
    head = bytearray(416)
    head[0:4] = b"BIMH"
    struct.pack_into("<II", head, 4, 1, 416 + body)
    struct.pack_into("<I", head, 0x20, 416)
    struct.pack_into("<I", head, 0x28, body)
    return bytes(head) + os.urandom(body)


def status_body(phase, code=0, *, nxt=0, window=0, size=0, want=0xFFFF,
                served=0, retries=0, blocker=0xFF, before=b"\0" * 4,
                after=b"\0" * 4, banner=b"", model=b"", units=()):
    body = bytearray(au.STATUS_SIZE)
    struct.pack_into("<BBBBIIIIHHHHHB", body, 0, phase, code, 1, 2, nxt,
                     window, size, 0x2D000, 1024, want, served, retries, 0,
                     blocker)
    body[32:36] = before
    body[36:40] = after
    body[40:40 + len(banner)] = banner
    body[56:56 + len(model)] = model
    for i, (index, state) in enumerate(units):
        body[64 + 2 * i] = index
        body[65 + 2 * i] = state
    return bytes(body)


def report(token, status, body, kind=au.KIND_STATUS):
    return token + bytes((status, kind)) + body


class FakeBridge:
    """Enough of the bridge's session for the driver: a 4 KiB window from the
    block the loader asked for last, data taken only in order, one STATUS per
    change."""

    def __init__(self, *, refuse=None, isolation=False, after=b"\x16\x16\x00\x05"):
        self.refuse = refuse
        self.isolation = isolation
        self.after = after
        self.token = None
        self.data = bytearray()
        self.size = 0
        self.phase = "idle"
        self.want = None
        self.served = 0
        self.dropped_data = 0
        self.out = []

    def window(self):
        if self.want is None:
            return min(4096, self.size)
        return min(self.size, 416 + self.want * 1024 + 4096)

    def status(self, status=0, code=0, blocker=0xFF, units=()):
        self.out.append(report(self.token, status, status_body(
            PHASE[self.phase], code, nxt=len(self.data),
            window=self.window(), size=self.size,
            want=0xFFFF if self.want is None else self.want,
            served=self.served, blocker=blocker,
            before=b"\x1d\x15\x00\x03", after=self.after
            if self.phase == "done" else b"\0" * 4,
            banner=b"N3F05", units=units)))

    def receive(self, payload):
        token, op = payload[:4], payload[4]
        if op == au.OP_BEGIN:
            self.token = token
            if self.refuse:
                self.out.append(report(token, CODE[self.refuse],
                                       status_body(0)))
                return
            self.size = int.from_bytes(payload[7:11], "big")
            assert payload[11:23] == au.CONFIRM
            self.phase = "settle"
            self.status()
        elif op == au.OP_DATA:
            offset = int.from_bytes(payload[5:9], "big")
            chunk = payload[9:]
            if offset != len(self.data) or offset + len(chunk) > self.window():
                self.dropped_data += 1
                self.status()
                return
            self.data += chunk
        elif op == au.OP_ABORT:
            self.phase = "failed"
            self.status(status=0, code=CODE["aborted"])

    def run_loader(self):
        """One loader step: advance as far as the data allows."""
        if self.phase == "settle" and len(self.data) >= 416:
            self.phase = "isolate"
            self.status()
            if self.isolation:
                self.phase = "failed"
                self.status(code=CODE["isolation"], blocker=0)
                return
            self.phase = "transfer"
            self.want = 0
            self.status()
        if self.phase == "transfer":
            blocks = -(-(self.size - 416) // 1024)
            while self.want < blocks and \
                    len(self.data) >= min(self.size,
                                          416 + (self.want + 1) * 1024):
                self.served += 1
                if self.want + 1 == blocks:
                    self.phase = "verify"
                    self.status()
                    self.phase = "done"
                    self.status()
                    self.status(units=((0, 1), (1, 1)))  # after-session units
                    return
                self.want += 1
                self.status()


def run(driver, bridge, steps=400):
    now = 0.0
    for _ in range(steps):
        for payload in driver.due(now):
            Message(MessageType.AMS_UPDATE_REQUEST, payload).encode(
                Direction.HOST_TO_BRIDGE)       # the framing accepts it
            bridge.receive(payload)
        bridge.run_loader()
        for payload in bridge.out:
            Message.decode(Message(MessageType.AMS_UPDATE_REPORT, payload)
                           .encode(Direction.BRIDGE_TO_HOST),
                           Direction.BRIDGE_TO_HOST)
            driver.accept(payload, now)
        bridge.out = []
        if driver.finished and driver.final_units is not None:
            break
        now += 0.05
    return driver


class LayoutTests(unittest.TestCase):
    def test_begin_bytes(self):
        payload = au.make_begin(b"TOKN", 1, "ams2", 172132, "N3F05")
        self.assertEqual(payload[:4], b"TOKN")
        self.assertEqual(payload[4:7], bytes((1, 1, 2)))
        self.assertEqual(int.from_bytes(payload[7:11], "big"), 172132)
        self.assertEqual(payload[11:23], b"ERASE-AMS-FW")
        self.assertEqual(payload[23], 5)
        self.assertEqual(payload[24:], b"N3F05")

    def test_data_and_abort_bytes(self):
        payload = au.make_data(b"TOKN", 0x01020304, b"\xaa" * 256)
        self.assertEqual(payload[4], au.OP_DATA)
        self.assertEqual(payload[5:9], bytes((1, 2, 3, 4)))
        self.assertEqual(len(payload), 4 + 1 + 4 + 256)
        self.assertLessEqual(len(payload), au.REQUEST_MAX_PAYLOAD)
        self.assertEqual(au.make_abort(b"TOKN"), b"TOKN\x03")
        with self.assertRaises(au.AmsUpdateError):
            au.make_data(b"TOKN", 0, b"\0" * 257)

    def test_status_fields(self):
        body = status_body(PHASE["transfer"], 0, nxt=5000, window=8192,
                           size=160552, want=3, served=3, retries=2,
                           before=b"\x1d\x15\x00\x03",
                           banner=b"N3S05", model=b"N3S05",
                           units=((0, 1), (1, 3)))
        parsed = au.parse_status(body)
        self.assertEqual(parsed["phase"], "transfer")
        self.assertEqual((parsed["next"], parsed["window"], parsed["want"]),
                         (5000, 8192, 3))
        self.assertEqual(parsed["before"], "03.00.21.29")
        self.assertEqual(parsed["after"], "")
        self.assertEqual(parsed["banner"], "N3S05")
        self.assertEqual(parsed["units"], [{"index": 0, "state": "application"},
                                           {"index": 1, "state": "loader"}])
        self.assertIsNone(parsed["blocker"])

    def test_text_report(self):
        payload = b"TOKN" + bytes((0, au.KIND_TEXT)) + \
            (0x0700).to_bytes(2, "little") + b"[MCU_UP] N3F05"
        parsed = au.parse_report(payload)
        self.assertEqual((parsed["kind"], parsed["source"], parsed["line"]),
                         ("text", 0x0700, "[MCU_UP] N3F05"))

    def test_mux_framing_limits(self):
        Message(MessageType.AMS_UPDATE_REQUEST, b"TOKN\x03").encode(
            Direction.HOST_TO_BRIDGE)
        with self.assertRaises(ProtocolError):
            Message(MessageType.AMS_UPDATE_REQUEST, b"\0" * 266).encode(
                Direction.HOST_TO_BRIDGE)
        with self.assertRaises(ProtocolError):
            Message(MessageType.AMS_UPDATE_REPORT, b"TOKN\x00\x01").encode(
                Direction.HOST_TO_BRIDGE)   # bridge-only type from the host
        with self.assertRaises(ProtocolError):
            Message(MessageType.AMS_UPDATE_REPORT, b"\0" * 79).encode(
                Direction.BRIDGE_TO_HOST)

    def test_model_names(self):
        self.assertEqual(au.model_for("ams2", "rev5"), "N3F05")
        self.assertEqual(au.model_for("ams_ht", "rev5"), "N3S05")
        self.assertEqual(au.model_for("ams1", "rev8"), "AMS08")
        with self.assertRaises(au.AmsUpdateError):
            au.model_for("ams1", "x")


class DriverTests(unittest.TestCase):
    def driver(self, img):
        return au.AmsUpdateDriver(img, index=1, family="ams2", model="N3F05",
                                  now=0.0, token=b"TOKN")

    def test_a_whole_update(self):
        img = image(172132 - 416)
        bridge = FakeBridge()
        driver = run(self.driver(img), bridge, steps=2000)
        self.assertTrue(driver.outcome.ok, driver.outcome)
        self.assertEqual(bytes(bridge.data), img)
        self.assertEqual(driver.outcome.before, "03.00.21.29")
        self.assertEqual(driver.outcome.after, "05.00.22.22")
        self.assertEqual(driver.final_units, [
            {"index": 0, "state": "application"},
            {"index": 1, "state": "application"}])
        kinds = [e["event"] for e in driver.events]
        self.assertIn("progress", kinds)
        self.assertIn("result", kinds)
        self.assertIn("units", kinds)
        phases = [e["phase"] for e in driver.events if e["event"] == "phase"]
        self.assertEqual(phases[0], "settle")
        self.assertIn("transfer", phases)

    def test_a_refused_begin_ends_with_its_reason(self):
        driver = run(self.driver(image()), FakeBridge(refuse="busy"))
        self.assertFalse(driver.outcome.ok)
        self.assertEqual(driver.outcome.code, "busy")
        self.assertIn("busy", driver.outcome.message)

    def test_isolation_names_the_unit(self):
        driver = run(self.driver(image()), FakeBridge(isolation=True))
        self.assertFalse(driver.outcome.ok)
        self.assertEqual(driver.outcome.code, "isolation")
        self.assertIn("index 0", driver.outcome.message)
        self.assertIn("before erasing", driver.outcome.message)

    def test_a_silent_bridge_ends_the_session(self):
        driver = self.driver(image())
        self.assertEqual(len(driver.due(0.0)), 1)        # BEGIN
        driver.accept(report(b"TOKN", 0, status_body(
            PHASE["settle"], window=4096, size=len(driver.image))), 0.1)
        self.assertTrue(driver.due(0.2))                 # data
        out = driver.due(0.2 + au.REPORT_SILENCE_S + 1)
        self.assertTrue(driver.finished)
        self.assertEqual(driver.outcome.code, "bridge-silent")
        self.assertEqual(out, [au.make_abort(b"TOKN")])

    def test_data_not_taken_is_sent_again(self):
        img = image()
        driver = self.driver(img)
        driver.due(0.0)
        driver.accept(report(b"TOKN", 0, status_body(
            PHASE["settle"], window=4096, size=len(img))), 0.0)
        first = driver.due(0.1)
        self.assertEqual(sum(len(p) - 9 for p in first), 4096)
        self.assertEqual(driver.due(0.2), [])            # nothing new yet
        # The bridge took only the first 1000 bytes and says so.
        driver.accept(report(b"TOKN", 0, status_body(
            PHASE["settle"], nxt=1000, window=4096, size=len(img))), 0.3)
        again = driver.due(0.1 + au.RESEND_AFTER_S + 0.1)
        self.assertEqual(int.from_bytes(again[0][5:9], "big"), 1000)

    def test_abort(self):
        img = image()
        bridge = FakeBridge()
        driver = self.driver(img)
        for payload in driver.due(0.0):
            bridge.receive(payload)
        for payload in bridge.out:
            driver.accept(payload, 0.0)
        bridge.out = []
        driver.abort()
        sent = driver.due(0.1)
        self.assertIn(au.make_abort(b"TOKN"), sent)
        for payload in sent:
            bridge.receive(payload)
        for payload in bridge.out:
            driver.accept(payload, 0.2)
        self.assertEqual(driver.outcome.code, "aborted")

    def test_a_probe_reads_the_model_code_and_aborts_before_any_data(self):
        img = image()
        driver = au.AmsUpdateDriver(img, index=0, family="ams1",
                                    model="AMS08", now=0.0, token=b"TOKN",
                                    probe=True)
        self.assertEqual(driver.due(0.0)[0][4], au.OP_BEGIN)
        driver.accept(report(b"TOKN", 0, status_body(
            PHASE["settle"], window=4096, size=len(img),
            model=b"AMS07")), 0.1)
        sent = driver.due(0.2)
        self.assertEqual(sent, [au.make_abort(b"TOKN")])     # no DATA
        self.assertEqual(driver.probed["model_code"], "AMS07")
        driver.accept(report(b"TOKN", 0, status_body(
            PHASE["failed"], CODE["aborted"], model=b"AMS07")), 0.3)
        self.assertTrue(driver.outcome.ok)
        self.assertEqual(driver.outcome.code, "probe")
        self.assertEqual(driver.due(0.4), [])

    def test_resume_opens_with_the_resume_op(self):
        driver = au.AmsUpdateDriver(image(), index=1, family="ams2",
                                    model="N3F05", now=0.0, token=b"TOKN",
                                    resume=True)
        first = driver.due(0.0)[0]
        self.assertEqual(first[4], au.OP_RESUME)
        self.assertEqual(first[5:], au.make_begin(
            b"TOKN", 1, "ams2", len(driver.image), "N3F05")[5:])

    def test_no_rejoin_is_done_but_says_the_unit_has_not_answered(self):
        driver = self.driver(image())
        driver.due(0.0)
        driver.accept(report(b"TOKN", 0, status_body(
            PHASE["settle"], window=4096, size=len(driver.image))), 0.1)
        driver.accept(report(b"TOKN", 0, status_body(
            PHASE["done"], CODE["no-rejoin"])), 0.2)
        self.assertTrue(driver.outcome.ok)
        self.assertEqual(driver.outcome.code, "no-rejoin")
        self.assertIn("has not answered", driver.outcome.message)

    def test_the_version_read_after_the_session_is_the_result(self):
        img = image(172132 - 416)
        bridge = FakeBridge(after=b"\0\0\0\0")       # rejoin read nothing
        driver = self.driver(img)
        run(driver, bridge, steps=2000)
        self.assertTrue(driver.outcome.ok)
        self.assertEqual(driver.outcome.after, "")
        # The bridge's later units report carries the version it read.
        driver.accept(report(b"TOKN", 0, status_body(
            PHASE["done"], after=b"\x16\x16\x00\x05",
            units=((0, 1), (1, 1)))), 99.0)
        self.assertEqual(driver.outcome.after, "05.00.22.22")
        self.assertEqual(driver.events[-1]["after"], "05.00.22.22")

    def test_reports_for_another_token_are_ignored(self):
        driver = self.driver(image())
        driver.due(0.0)
        driver.accept(report(b"XXXX", CODE["busy"], status_body(0)), 0.1)
        self.assertFalse(driver.finished)


class RelayTests(unittest.TestCase):
    """The relay's ams_update method end to end, with a real support log, a
    fake bridge behind a fake daemon and a socket for the command's side."""

    def setUp(self):
        import json
        import socket
        import tempfile
        import types
        from cortex_bridge import relay
        self.json = json
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.log_file = base / "cortex-bridge.jsonl"
        args = relay.parser().parse_args([
            "--pty-dir", str(base / "oams"), "--run-dir", "",
            "--log-file", str(self.log_file)])
        self.relay = relay.Relay(args, serial_module=None, list_ports=None)
        self.relay.core = types.SimpleNamespace(ams_update_handler=None)
        self.relay.bridge = {"manufacturer": "SiForge"}
        self.relay.status.state["firmware"] = {"state": "known",
                                               "version": 23}
        self.image = image(20000)
        self.image_path = base / "image.bin"
        self.image_path.write_bytes(self.image)
        self.client, self.server = socket.socketpair()
        self.bridge = FakeBridge()
        relay_self = self.relay
        bridge = self.bridge

        class Daemon:
            def send_request(self, message):
                bridge.receive(message.payload)
        self.daemon = Daemon()

    def tearDown(self):
        self.client.close()
        self.relay.log.close()
        self.tmp.cleanup()

    def request(self, **over):
        request = {"method": "ams_update", "image": str(self.image_path),
                   "index": 1, "family": "ams2", "model": "N3F05"}
        request.update(over)
        return request

    def lines(self):
        self.client.settimeout(1.0)
        data = b""
        while True:
            try:
                chunk = self.client.recv(65536)
            except OSError:
                break
            if not chunk:
                break
            data += chunk
        return [self.json.loads(line) for line in data.splitlines() if line]

    def run_relay(self):
        now = 0.0
        while self.relay.ams is not None and now < 60:
            self.bridge.run_loader()
            for payload in self.bridge.out:
                self.relay.core.ams_update_handler(payload)
            self.bridge.out = []
            self.relay.drive_ams_update(self.daemon, now)
            now += 0.05

    def test_an_update_runs_and_is_logged(self):
        self.relay.start_ams_update(self.server, self.request(),
                                    running=True)
        self.assertIsNotNone(self.relay.ams)
        self.run_relay()
        self.assertIsNone(self.relay.ams)
        self.assertIsNone(self.relay.core.ams_update_handler)
        self.assertEqual(bytes(self.bridge.data), self.image)
        events = self.lines()
        result = [e for e in events if e["event"] == "result"][-1]
        self.assertTrue(result["ok"], result)
        self.assertIn("units", [e["event"] for e in events])
        records = [self.json.loads(line) for line in
                   self.log_file.read_text().splitlines()]
        kinds = [r["record"] for r in records]
        self.assertIn("ams_update_start", kinds)
        end = [r for r in records if r["record"] == "ams_update_end"][0]
        self.assertEqual((end["result"], end["before"], end["after"]),
                         ("ok", "03.00.21.29", "05.00.22.22"))

    def test_refusals_answer_and_close(self):
        cases = [
            ({"running": False}, "not-connected"),
            ({"firmware": 22}, "unsupported-firmware"),
            ({"image": "/nonexistent"}, "bad-image"),
            ({"family": "ams9"}, "bad-request"),
        ]
        import socket
        for change, code in cases:
            client, server = socket.socketpair()
            self.relay.status.state["firmware"] = {
                "state": "known", "version": change.get("firmware", 23)}
            request = self.request()
            if "image" in change:
                request["image"] = change["image"]
            if "family" in change:
                request["family"] = change["family"]
            self.relay.start_ams_update(server, request,
                                        running=change.get("running", True))
            client.settimeout(1.0)
            reply = self.json.loads(client.recv(65536).decode())
            self.assertEqual(reply["code"], code, change)
            self.assertIsNone(self.relay.ams)
            client.close()

    def test_a_waiting_client_is_not_taken_for_gone(self):
        from cortex_bridge import relay
        self.server.settimeout(2.0)          # as ControlServer leaves it
        import time
        started = time.monotonic()
        self.assertFalse(relay._peer_closed(self.server))
        self.assertLess(time.monotonic() - started, 0.5)
        self.client.sendall(b"x")
        self.assertFalse(relay._peer_closed(self.server))
        self.client.close()
        self.server.recv(1)
        self.assertTrue(relay._peer_closed(self.server))

    def test_the_update_survives_a_client_that_only_listens(self):
        self.server.settimeout(2.0)
        self.relay.start_ams_update(self.server, self.request(),
                                    running=True)
        self.run_relay()
        result = [e for e in self.lines() if e["event"] == "result"][-1]
        self.assertTrue(result["ok"], result)

    def test_listen_streams_loader_lines_without_a_session(self):
        sent = []

        class Daemon:
            def send_request(self, message):
                sent.append(message.payload)
        self.relay.start_ams_listen(self.server, {"seconds": 1},
                                    running=True)
        self.relay.drive_ams_update(Daemon(), 0.0)
        # One ABORT with a fresh token, nothing else.
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][4], au.OP_ABORT)
        token = sent[0][:4]
        handler = self.relay.core.ams_update_handler
        handler(report(token, CODE["no-session"], status_body(0)))
        handler(token + bytes((0, au.KIND_TEXT)) +
                (0x0700).to_bytes(2, "little") + b"[MCU_UP] ams 1 wait cmd1!")
        import time
        deadline = time.monotonic() + 3
        while self.relay.ams is not None and time.monotonic() < deadline:
            self.relay.drive_ams_update(Daemon(), time.monotonic() + 10)
        events = self.lines()
        loader = [e for e in events if e["event"] == "loader"]
        self.assertEqual(loader[0]["line"], "[MCU_UP] ams 1 wait cmd1!")
        result = [e for e in events if e["event"] == "result"][-1]
        self.assertTrue(result["ok"])
        self.assertEqual(result["lines"], 1)
        self.assertEqual(len(sent), 1)

    def test_resume_needs_bridge_24(self):
        import socket
        client, server = socket.socketpair()
        self.relay.status.state["firmware"] = {"state": "known",
                                               "version": 23}
        self.relay.start_ams_update(server, self.request(resume=True),
                                    running=True)
        client.settimeout(1.0)
        reply = self.json.loads(client.recv(65536).decode())
        self.assertEqual(reply["code"], "unsupported-firmware")
        self.assertIn("24", reply["message"])
        client.close()

    def test_a_session_end_reports_host_lost(self):
        self.relay.start_ams_update(self.server, self.request(),
                                    running=True)
        self.relay.drive_ams_update(self.daemon, 0.0)
        self.relay.end_ams_update(reason="the bridge session ended")
        result = [e for e in self.lines() if e["event"] == "result"][-1]
        self.assertEqual(result["code"], "host-lost")



class StaleRelayTests(unittest.TestCase):
    """The tools run from the checkout each time; the relay keeps the code it
    started with until its service restarts. A customer's relay that was not
    restarted after an update answered `unknown method 'ams_update'`."""

    def setUp(self):
        import json
        import tempfile
        import types
        from unittest import mock
        from cortex_bridge import cli, relay
        self.cli, self.relay, self.mock, self.json = cli, relay, mock, json
        self.tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.tmp.name)
        self.args = types.SimpleNamespace(run_dir=str(self.run_dir),
                                          moonraker="http://moonraker",
                                          yes=False)
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def write_status(self, **fields):
        status = {"state": "running", "started": "2026-11-01T09:30:12Z"}
        status.update(fields)
        (self.run_dir / "status.json").write_text(self.json.dumps(status))

    def old_relay(self):
        self.write_status()      # a relay from before the methods list

    def new_relay(self):
        self.write_status(methods=list(self.relay.CONTROL_METHODS))

    def test_the_relay_lists_what_it_answers(self):
        status = self.relay.StatusFile(None).state
        for method in ("status", "update", "support_snapshot", "ams_update",
                       "ams_listen"):
            self.assertIn(method, status["methods"])

    def test_old_and_new_relays_are_told_apart(self):
        self.assertTrue(self.cli.relay_lacks({"state": "running"}, "ams_update"))
        self.assertFalse(self.cli.relay_lacks(
            {"state": "running", "methods": ["ams_update"]}, "ams_update"))
        # A relay that is not running is a different problem.
        self.assertFalse(self.cli.relay_lacks(None, "ams_update"))
        self.assertFalse(self.cli.relay_lacks({"state": "stopped"}, "ams_update"))

    def ensure(self, *, answer="y", state="standby", restart_ok=True,
               comes_back=True):
        def fake_moonraker(url, path, post=False, timeout=5.0):
            self.calls.append(path)
            if not restart_ok:
                raise OSError("400 Bad Request")
            if comes_back:
                self.new_relay()
            return {"result": "ok"}
        clock = [0.0]

        def sleep(seconds):
            clock[0] += seconds
        mock = self.mock
        with mock.patch.object(self.cli, "moonraker", fake_moonraker), \
                mock.patch.object(self.cli, "printer_state", lambda url: state), \
                mock.patch.object(self.cli, "restart_klipper",
                                  lambda url, **kw: self.calls.append("FIRMWARE_RESTART")), \
                mock.patch("builtins.input", lambda prompt: answer), \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            return self.cli.ensure_relay_current(
                self.args, "ams_update", sleep=sleep, clock=lambda: clock[0])

    def test_a_current_relay_is_left_alone(self):
        self.new_relay()
        self.assertTrue(self.ensure())
        self.assertEqual(self.calls, [])

    def test_an_old_relay_is_restarted_then_klipper(self):
        self.old_relay()
        self.assertTrue(self.ensure())
        self.assertEqual(self.calls, [
            "/machine/services/restart?service=cortex-bridge",
            "FIRMWARE_RESTART"])

    def test_no_restart_without_a_yes(self):
        self.old_relay()
        self.assertFalse(self.ensure(answer="n"))
        self.assertEqual(self.calls, [])

    def test_no_restart_during_a_print(self):
        self.old_relay()
        for state in ("printing", "paused"):
            self.assertFalse(self.ensure(state=state))
        self.assertEqual(self.calls, [])

    def test_moonraker_refusing_the_restart_says_what_to_do(self):
        self.old_relay()
        self.assertFalse(self.ensure(restart_ok=False))
        self.assertNotIn("FIRMWARE_RESTART", self.calls)

    def test_a_relay_that_comes_back_old_is_not_trusted(self):
        self.old_relay()
        self.assertFalse(self.ensure(comes_back=False))
        self.assertNotIn("FIRMWARE_RESTART", self.calls)


if __name__ == "__main__":
    unittest.main()
