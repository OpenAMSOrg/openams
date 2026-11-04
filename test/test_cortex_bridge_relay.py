"""The Cortex Bridge relay: roster wait, bridge discovery, and a whole session.

The session tests stand a socket pair in for the bridge's USB serial port, so
they exercise the real cortexd routing, the PTY links, the status file and
the control socket without hardware.
"""

import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cortex_bridge  # noqa: E402
from cortex_bridge import relay  # noqa: E402
from cortex_bridge.cortexd import (  # noqa: E402
    Direction,
    Message,
    MessageType,
    NodeStatus,
    ROSTER_PAYLOAD_SIZE,
    VERSION,
    make_node_data,
    make_roster,
)

UID = bytes.fromhex("0a1b2c3d4e5f")


def klipper_frame(sequence=0x10, payload=b"\x01\x00\x28"):
    """A well-formed Klipper message block (CRC is not checked by cortexd)."""
    length = 2 + len(payload) + 3
    return bytes((length, sequence)) + payload + b"\x00\x00\x7e"


def identity_report(token, version=7, status=0, boot_abi=6, genuine=0,
                    reset_flags=0x08, uptime_ms=1234):
    """An identity report payload as production bridge firmware sends it."""
    if status:
        return bytes(token) + bytes((status,))
    body = bytes((1, 1)) + version.to_bytes(4, "big") + \
        boot_abi.to_bytes(2, "big") + bytes((genuine, reset_flags)) + \
        uptime_ms.to_bytes(4, "big") + bytes(9 + 12 + 156)
    return bytes(token) + b"\x00" + body


class BytesPort:
    """Reads a fixed byte string, then behaves like an idle serial port."""

    timeout = 0.25

    def __init__(self, data):
        self.incoming = bytearray(data)
        self.output = bytearray()

    def read(self, length):
        chunk = bytes(self.incoming[:length])
        del self.incoming[:length]
        return chunk

    def write(self, data):
        self.output.extend(data)
        return len(data)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        self.now += 1.0
        return self.now


def comport(device, pid, serial, manufacturer="SiForge",
            product="Cortex Bridge", vid=0x1A86):
    return types.SimpleNamespace(device=device, vid=vid, pid=pid,
                                 serial_number=serial,
                                 manufacturer=manufacturer, product=product)


def list_ports_of(*ports):
    return types.SimpleNamespace(comports=lambda: list(ports))


class RosterWaitTests(unittest.TestCase):
    def test_roster_header_is_returned_and_nothing_is_written(self):
        prefix = bytes((VERSION, MessageType.ROSTER, 0, 5))
        port = BytesPort(prefix + b"rest")
        self.assertEqual(relay.wait_for_roster(port, clock=Clock()), prefix)
        self.assertEqual(port.output, b"")

    def test_header_may_arrive_in_pieces(self):
        prefix = bytes((VERSION, MessageType.ROSTER, 0, 5))

        class Trickle(BytesPort):
            def read(self, length):
                return super().read(1)

        self.assertEqual(relay.wait_for_roster(Trickle(prefix),
                                               clock=Clock()), prefix)

    def test_anything_else_is_rejected_without_a_reply(self):
        for data in (b"IPGW", bytes((3, 1, 0, 5)),
                     bytes((VERSION, MessageType.NODE_DATA, 0, 9)),
                     bytes((VERSION, MessageType.ROSTER, 0,
                            ROSTER_PAYLOAD_SIZE + 1))):
            port = BytesPort(data)
            with self.assertRaises(relay.RelayError) as caught:
                relay.wait_for_roster(port, clock=Clock())
            self.assertEqual(caught.exception.reason, "unsupported-firmware")
            self.assertEqual(port.output, b"")

    def test_silent_bridge_times_out(self):
        with self.assertRaises(relay.RelayError) as caught:
            relay.wait_for_roster(BytesPort(b""), clock=Clock(), timeout=5.0)
        self.assertEqual(caught.exception.reason, "no-roster")


class FindBridgeTests(unittest.TestCase):
    def test_the_one_running_bridge_is_found(self):
        ports = list_ports_of(
            comport("/dev/ttyACM0", 0x55D3, "x"),
            comport("/dev/ttyACM1", 0xFE11, "CB0123456789AB"))
        bridge = relay.find_bridge(ports)
        self.assertEqual(bridge["device"], "/dev/ttyACM1")
        self.assertEqual(bridge["serial"], "CB0123456789AB")

    def test_serial_pattern_selects_one_of_several(self):
        ports = list_ports_of(
            comport("/dev/ttyACM1", 0xFE11, "CB0000000000AA"),
            comport("/dev/ttyACM2", 0xFE11, "CB0000000000BB"))
        with self.assertRaises(relay.RelayError) as caught:
            relay.find_bridge(ports)
        self.assertEqual(caught.exception.reason, "several-bridges")
        bridge = relay.find_bridge(ports, serial_pattern="CB*BB")
        self.assertEqual(bridge["device"], "/dev/ttyACM2")

    def test_the_reason_names_what_is_wrong(self):
        cases = (
            ((), "no-bridge"),
            ((comport("/dev/ttyACM0", 0xFE10, "", product="Cortex Updater"),),
             "bridge-in-update-mode"),
            ((comport("/dev/ttyACM0", 0xFE10, "F-BOARD-01",
                      product="Cortex Fault"),), "bridge-fault"),
            ((comport("/dev/ttyACM0", 0xFE11, "XX0000"),), "no-bridge"),
        )
        for ports, reason in cases:
            with self.assertRaises(relay.RelayError) as caught:
                relay.find_bridge(list_ports_of(*ports))
            self.assertEqual(caught.exception.reason, reason, ports)


class SocketPort:
    """The relay's end of a socket pair, with the pyserial surface it uses."""

    def __init__(self, sock):
        self.sock = sock
        self.timeout = 0.25
        self.write_timeout = 5

    def fileno(self):
        return self.sock.fileno()

    def read(self, length):
        if self.timeout == 0:
            self.sock.setblocking(False)
        else:
            self.sock.settimeout(self.timeout)
        try:
            data = self.sock.recv(length)
        except (BlockingIOError, socket.timeout):
            return b""
        if not data:
            raise OSError("the bridge disconnected")
        return data

    def write(self, data):
        self.sock.setblocking(False)
        try:
            return self.sock.send(data)
        except BlockingIOError:
            return 0

    def flush(self):
        pass

    def close(self):
        self.sock.close()


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.pty_dir = base / "oams"
        self.run_dir = base / "run"
        self.log_file = base / "logs" / "cortex-bridge.jsonl"
        self.bridge_side, relay_side = socket.socketpair()
        self.relay_port = SocketPort(relay_side)
        serial_module = types.SimpleNamespace(
            Serial=lambda *args, **kwargs: self.relay_port)
        ports = list_ports_of(comport("/dev/ttyACM9", 0xFE11,
                                      "CB0123456789AB"))
        args = relay.parser().parse_args([
            "--pty-dir", str(self.pty_dir), "--run-dir", str(self.run_dir),
            "--log-file", str(self.log_file)])
        self.relay = relay.Relay(args, serial_module=serial_module,
                                 list_ports=ports)
        self.stopped = threading.Event()

        def idle(_seconds):
            # The test ends the relay once its session is over.
            raise relay.RelayStop()

        self.relay.idle = idle
        self.thread = threading.Thread(target=self.run_relay, daemon=True)

    def run_relay(self):
        try:
            self.relay.run()
        except relay.RelayStop:
            pass
        finally:
            self.stopped.set()

    def tearDown(self):
        try:
            self.bridge_side.close()
        except OSError:
            pass
        self.stopped.wait(5)
        self.tmp.cleanup()

    def send_from_bridge(self, message):
        self.bridge_side.sendall(message.encode(Direction.BRIDGE_TO_HOST))

    def wait_for(self, condition, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = condition()
            if value:
                return value
            time.sleep(0.02)
        self.fail("condition not met in %.1f s" % timeout)

    def status(self):
        try:
            return json.loads((self.run_dir / "status.json").read_text())
        except (OSError, ValueError):
            return {}

    def read_one_message(self, timeout=5.0):
        self.bridge_side.settimeout(timeout)
        header = b""
        while len(header) < 4:
            header += self.bridge_side.recv(4 - len(header))
        length = int.from_bytes(header[2:4], "big")
        body = b""
        while len(body) < length:
            body += self.bridge_side.recv(length - len(body))
        return Message.decode(header + body, Direction.HOST_TO_BRIDGE)

    def read_bridge_message(self, timeout=5.0,
                            kind=MessageType.HOST_DATA):
        """The next message of ``kind``; identity requests from the relay
        are skipped unless they are what the test waits for."""
        deadline = time.monotonic() + timeout
        while True:
            message = self.read_one_message(
                max(0.1, deadline - time.monotonic()))
            if message.kind == kind:
                return message
            if message.kind != MessageType.IDENTITY_REQUEST:
                self.fail("unexpected %s message" % message.kind.name)

    def answer_identity(self, version=7, status=0):
        request = self.read_bridge_message(
            kind=MessageType.IDENTITY_REQUEST)
        self.send_from_bridge(Message(MessageType.IDENTITY_REPORT,
                                      identity_report(request.payload,
                                                      version, status)))
        return request

    def update_request(self, path):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(10)
            client.connect(str(self.run_dir / "control.sock"))
            client.sendall(json.dumps(
                {"method": "update", "file": str(path)}).encode() + b"\n")
            return [json.loads(line) for line in client.makefile()]

    def start_session(self):
        self.thread.start()
        self.send_from_bridge(make_roster(1, [NodeStatus(UID, True)]))
        link = self.pty_dir / UID.hex()
        self.wait_for(link.is_symlink)
        self.wait_for(lambda: self.status().get("state") == "running" and
                      self.status().get("units", [{}])[0].get("online"))
        return link

    def test_klipper_traffic_crosses_the_relay_both_ways(self):
        link = self.start_session()
        pty = os.open(str(link), os.O_RDWR | os.O_NOCTTY)
        try:
            frame = klipper_frame()
            os.write(pty, frame)
            message = self.read_bridge_message()
            self.assertEqual(message.kind, MessageType.HOST_DATA)
            self.assertEqual(message.payload, UID + frame)

            reply = klipper_frame(0x11, b"\x00\x00\x00")
            self.send_from_bridge(make_node_data(UID, 1, reply))
            received = b""
            deadline = time.monotonic() + 5
            while len(received) < len(reply) and time.monotonic() < deadline:
                received += os.read(pty, 64)
            self.assertEqual(received, reply)
        finally:
            os.close(pty)
        status = self.status()
        self.assertEqual(status["bridge"]["serial"], "CB0123456789AB")
        self.assertEqual(status["bridge"]["image"], "production")
        self.bridge_side.close()
        self.assertTrue(self.stopped.wait(5))
        self.assertFalse(link.exists(), "PTY links are removed on stop")
        records = [json.loads(line) for line in
                   self.log_file.read_text().splitlines()]
        names = [record["record"] for record in records]
        for expected in ("service_start", "runtime_started", "node_created",
                         "runtime_roster", "service_stop"):
            self.assertIn(expected, names)
        self.assertNotIn(frame.hex(), self.log_file.read_text())

    def test_control_socket_reports_status(self):
        self.start_session()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(5)
            client.connect(str(self.run_dir / "control.sock"))
            client.sendall(b'{"method": "status"}\n')
            reply = json.loads(client.makefile().readline())
        self.assertEqual(reply["event"], "status")
        self.assertEqual(reply["units"][0]["uid"], UID.hex())
        self.assertTrue(reply["units"][0]["online"])

    def test_update_request_releases_the_port_and_runs_the_updater(self):
        calls = []

        class PackageRefused(Exception):
            pass

        def update_bridge(data, **kwargs):
            # The session must be gone: the relay no longer holds the port.
            calls.append((data, kwargs, self.relay_port.sock.fileno()))
            kwargs["progress"]("writing", 10, 20)
            return {"result": "installed"}

        fake = types.SimpleNamespace(
            PackageRefused=PackageRefused,
            inspect_package=lambda data: {"version": 7},
            update_bridge=update_bridge)
        package = Path(self.tmp.name) / "bridge-7.oac2"
        package.write_bytes(b"OAC2-test")
        self.start_session()
        with mock.patch.dict(sys.modules, {"cortex_bridge.updater": fake}), \
                mock.patch.object(cortex_bridge, "updater", fake,
                                  create=True):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(10)
                client.connect(str(self.run_dir / "control.sock"))
                client.sendall(json.dumps(
                    {"method": "update", "file": str(package)}).encode() +
                    b"\n")
                lines = [json.loads(line) for line in client.makefile()]
        events = [line["event"] for line in lines]
        self.assertEqual(events, ["accepted", "progress", "result"])
        self.assertTrue(lines[-1]["ok"])
        self.assertEqual(len(calls), 1)
        data, kwargs, fileno = calls[0]
        self.assertEqual(data, b"OAC2-test")
        self.assertEqual(kwargs["serial_number"], "CB0123456789AB")
        self.assertEqual(fileno, -1, "the bridge port was closed first")
        self.assertEqual(self.status()["update"]["result"], "installed")

    # -- the firmware version ----------------------------------------------

    def use_manufacturer(self, manufacturer):
        """Rebuild the relay (before it starts) for another USB string."""
        self.relay.log.close()
        self.relay.control.close()
        serial_module = types.SimpleNamespace(
            Serial=lambda *args, **kwargs: self.relay_port)
        args = relay.parser().parse_args([
            "--pty-dir", str(self.pty_dir), "--run-dir", str(self.run_dir),
            "--log-file", str(self.log_file)])
        self.relay = relay.Relay(args, serial_module=serial_module,
                                 list_ports=list_ports_of(comport(
                                     "/dev/ttyACM9", 0xFE11, "CB0123456789AB",
                                     manufacturer=manufacturer)))
        self.relay.idle = lambda _seconds: (_ for _ in ()).throw(
            relay.RelayStop())

    def firmware(self):
        return self.status().get("firmware") or {}

    def fake_updater(self, version, calls, error=None):
        class PackageRefused(Exception):
            pass

        def update_bridge(data, **kwargs):
            calls.append(data)
            if error is not None:
                raise error
            return {"result": "installed"}

        return types.SimpleNamespace(
            PackageRefused=PackageRefused,
            inspect_package=lambda data: {"version": version},
            update_bridge=update_bridge)

    def run_update(self, fake, path):
        with mock.patch.dict(sys.modules, {"cortex_bridge.updater": fake}), \
                mock.patch.object(cortex_bridge, "updater", fake,
                                  create=True):
            return self.update_request(path)

    def test_a_production_bridge_reports_its_version(self):
        self.start_session()
        request = self.answer_identity(version=7)
        self.assertEqual(len(request.payload), 4)
        self.wait_for(lambda: self.firmware().get("state") == "known")
        firmware = self.firmware()
        self.assertEqual(firmware["version"], 7)
        self.assertEqual(firmware["boot_abi"], 6)
        self.assertEqual(firmware["genuine"], "passed")
        self.assertEqual(firmware["reset_cause"], "power-on")
        self.wait_for(lambda: "bridge_identity" in self.log_file.read_text())
        records = [json.loads(line) for line in
                   self.log_file.read_text().splitlines()]
        identity = [r for r in records if r["record"] == "bridge_identity"]
        self.assertEqual(identity[0]["firmware_version"], 7)
        self.assertEqual(identity[0]["result"], "known")

    def test_a_busy_bridge_is_asked_again(self):
        self.start_session()
        self.answer_identity(status=4)
        self.wait_for(lambda: self.firmware().get("reason") == "busy")
        self.assertEqual(self.firmware()["state"], "querying")
        self.answer_identity(version=7)
        self.wait_for(lambda: self.firmware().get("version") == 7)

    def test_a_lab_bridge_is_never_asked(self):
        self.use_manufacturer("OpenAMS")
        self.start_session()
        self.assertEqual(self.firmware()["state"], "unavailable")
        self.assertEqual(self.firmware()["reason"], "lab-firmware")
        with self.assertRaises(socket.timeout):
            self.read_one_message(timeout=1.5)

    def session_still_up(self):
        self.assertNotEqual(self.relay_port.sock.fileno(), -1,
                            "the session kept the bridge's port")
        self.assertEqual(self.status()["state"], "running")

    def test_updating_to_the_running_version_keeps_the_session(self):
        self.start_session()
        self.answer_identity(version=7)
        self.wait_for(lambda: self.firmware().get("version") == 7)
        calls = []
        package = Path(self.tmp.name) / "bridge-7.oac2"
        package.write_bytes(b"OAC2-test")
        lines = self.run_update(self.fake_updater(7, calls), package)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0]["ok"])
        self.assertFalse(lines[0]["changed"])
        self.assertIn("already runs version 7", lines[0]["summary"])
        self.assertEqual(calls, [], "the bridge was not asked to update")
        self.session_still_up()

    def test_an_older_version_is_refused_without_touching_the_bridge(self):
        self.start_session()
        self.answer_identity(version=7)
        self.wait_for(lambda: self.firmware().get("version") == 7)
        calls = []
        package = Path(self.tmp.name) / "bridge-4.oac2"
        package.write_bytes(b"OAC2-test")
        lines = self.run_update(self.fake_updater(4, calls), package)
        self.assertFalse(lines[-1]["ok"])
        self.assertEqual(lines[-1]["error"], "OlderThanInstalled")
        self.assertIn("runs version 7", lines[-1]["summary"])
        self.assertEqual(calls, [])
        self.session_still_up()

    def test_a_file_that_is_not_firmware_is_refused_during_the_session(self):
        self.start_session()
        package = Path(self.tmp.name) / "notes.txt"
        package.write_bytes(b"not firmware at all")
        lines = self.update_request(package)
        self.assertFalse(lines[-1]["ok"])
        self.assertEqual(lines[-1]["error"], "PackageRefused")
        self.assertIn("This file cannot be installed", lines[-1]["summary"])
        self.assertEqual(lines[-1]["outcome"], "Nothing was sent to the "
                                               "bridge.")
        self.session_still_up()

    def test_a_refusal_names_the_running_version(self):
        self.start_session()
        self.answer_identity(version=4)
        self.wait_for(lambda: self.firmware().get("version") == 4)

        class ReceiverRejected(Exception):
            reason_code = "refused"
            post_state = "application"

        calls = []
        package = Path(self.tmp.name) / "bridge-7.oac2"
        package.write_bytes(b"OAC2-test")
        lines = self.run_update(self.fake_updater(
            7, calls, ReceiverRejected("the boot refused it")), package)
        result = lines[-1]
        self.assertFalse(result["ok"])
        self.assertEqual(result["running_version"], 4)
        self.assertIn("checked version 7 and refused it. It runs version 4",
                      result["summary"])
        self.assertIn("still runs version 4", result["outcome"])
        self.assertTrue(result["action"])
        self.assertEqual(self.status()["update"]["summary"], result["summary"])

    def test_waiting_is_logged_once_per_reason(self):
        args = relay.parser().parse_args([
            "--pty-dir", str(self.pty_dir), "--run-dir", "",
            "--log-file", str(self.log_file)])
        quiet = relay.Relay(args, serial_module=None,
                            list_ports=list_ports_of())
        error = relay.RelayError("no-bridge", "no bridge")
        for _ in range(5):
            quiet.waiting(error)
        quiet.log.close()
        records = [json.loads(line)["record"] for line in
                   self.log_file.read_text().splitlines()]
        self.assertEqual(records.count("bridge_waiting"), 1)


class ControlServerTests(unittest.TestCase):
    def test_an_unusable_socket_path_does_not_stop_the_relay(self):
        # A Unix socket path is limited to about 108 bytes; the relay must
        # keep serving the AMS ports without its control socket.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ("x" * 120) / "control.sock"
            server = relay.ControlServer(path)
            self.assertIsNone(server.sock)
            self.assertIsNone(server.poll())
            server.close()


class CommandLineTests(unittest.TestCase):
    def test_help_runs_without_pyserial_installed(self):
        import subprocess
        result = subprocess.run(
            [sys.executable, "-m", "cortex_bridge.relay", "--help"],
            cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, check=True)
        self.assertIn("--pty-dir", result.stdout)


if __name__ == "__main__":
    unittest.main()
