"""The CyberBrick Time-lapse Kit through a Cortex Bridge (firmware 40 and
later): the kit requests and reports, their framing in the relay core, the
relay's "kit" control method and the CLI's kit lines."""

import contextlib
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import time
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cortex_bridge import cli, kit, relay  # noqa: E402
from cortex_bridge.cortexd import (  # noqa: E402
    KIT_REQUEST_PAYLOAD_SIZE, VERSION, CortexCore, Direction, Message,
    MessageType, ProtocolError,
)

# The only keys a report, the relay's status entry or the CLI may carry.
ALLOWED = {"event", "service", "present", "shots", "shot", "last_shot", "ok",
           "error", "message"}


def report(event=0, status=0, present=True, service=True, shots=0,
           extra_flags=0):
    flags = (1 if service else 0) | (2 if present else 0) | extra_flags
    return bytes((2, event, status, flags)) + struct.pack("<H", shots)


def wire(kind, payload):
    return bytes((VERSION, kind, 0, len(payload))) + payload


class Codec(unittest.TestCase):
    def test_requests(self):
        message = kit.shot(1)
        self.assertEqual(message.kind, MessageType.KIT_REQUEST)
        self.assertEqual(message.encode(Direction.HOST_TO_BRIDGE),
                         wire(12, b"\x02\x01"))
        self.assertEqual(kit.service().payload, b"\x01\x01")
        self.assertEqual(kit.service(False).payload, b"\x01\x00")
        self.assertEqual(len(kit.service().payload), KIT_REQUEST_PAYLOAD_SIZE)
        for bad in (-1, 256):
            with self.assertRaises(ValueError):
                kit.shot(bad)

    def test_service_only_ever_sends_0_or_1(self):
        """Any other argument would fault the bridge's USB session."""
        for value in (True, False, 1, 0, 2, 7, 255, -1, "on", "", None,
                      [], [0], 0.5):
            payload = kit.service(value).payload
            self.assertEqual(payload[0], 1)
            self.assertIn(payload[1], (0, 1), value)
            self.assertEqual(payload[1], 1 if value else 0)

    def test_the_bridge_40_report_layout(self):
        # format 2 | event | status | flags (bit0 service, bit1 present) |
        # shots u16 little endian; 6 bytes
        self.assertEqual(kit.REPORT_SIZE, 6)
        self.assertEqual(kit.decode(bytes((2, 2, 3, 0x02, 0x34, 0x12))),
                         {"event": "shot", "service": False, "present": True,
                          "shots": 0x1234, "shot": "refused"})
        self.assertEqual(kit.decode(bytes((2, 2, 5, 0x01, 0, 0)))["shot"],
                         "not-permitted")
        self.assertEqual(kit.decode(bytes((2, 1, 0, 0x03, 1, 0))),
                         {"event": "service", "service": True,
                          "present": True, "shots": 1})

    def test_service_presence_and_shots_decoded(self):
        decoded = kit.decode(report(event=2, status=1, shots=513))
        self.assertEqual(decoded, {"event": "shot", "service": True,
                                   "present": True, "shots": 513,
                                   "shot": "queued"})
        self.assertEqual(kit.decode(report(present=False, service=False)),
                         {"event": "change", "service": False,
                          "present": False, "shots": 0})
        self.assertEqual(kit.decode(report(event=1))["event"], "service")

    def test_unknown_flags_are_ignored(self):
        plain = kit.decode(report(shots=7))
        self.assertEqual(kit.decode(report(shots=7, extra_flags=0xFC)), plain)
        self.assertLessEqual(set(plain), ALLOWED)

    def test_bad_reports(self):
        with self.assertRaises(ValueError):
            kit.decode(report()[:-1])
        with self.assertRaises(ValueError):
            kit.decode(b"\x01" + report()[1:])        # another format
        with self.assertRaises(ValueError):
            kit.decode(report() + b"\0")


class Framing(unittest.TestCase):
    def test_sizes_and_directions(self):
        Message.decode(wire(12, kit.service().payload),
                       Direction.HOST_TO_BRIDGE)
        Message.decode(wire(13, report()), Direction.BRIDGE_TO_HOST)
        for raw, direction in (
                (wire(12, kit.service().payload + b"\0"),
                 Direction.HOST_TO_BRIDGE),
                (wire(13, report()[:-1]), Direction.BRIDGE_TO_HOST),
                (wire(13, report()), Direction.HOST_TO_BRIDGE),
                (wire(12, kit.service().payload), Direction.BRIDGE_TO_HOST)):
            with self.assertRaises(ProtocolError):
                Message.decode(raw, direction)

    def test_the_core_hands_reports_over_and_never_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            core = CortexCore(Path(directory) / "dev")
            core.attach(1)
            seen = []
            core.accept(wire(13, report()))            # nobody asked
            core.kit_handler = seen.append
            core.accept(wire(13, report(shots=2)))
            self.assertEqual(len(seen), 1)
            core.kit_handler = lambda payload: 1 / 0
            core.accept(wire(13, report()))
            self.assertEqual(core.kit_handler_errors, 1)


class Connection:
    def __init__(self):
        self.lines = []
        self.closed = False

    def sendall(self, data):
        self.lines += [json.loads(x) for x in data.decode().splitlines() if x]

    def close(self):
        self.closed = True


class RelayKit(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        base = Path(self.directory.name)
        self.args = relay.parser().parse_args([
            "--pty-dir", str(base / "pty"), "--run-dir", str(base / "run"),
            "--log-file", str(base / "log.jsonl")])
        self.relay = relay.Relay(self.args, serial_module=None,
                                 list_ports=lambda: [])
        self.relay.core = object()      # a running session
        self.sent = []
        self.daemon = types.SimpleNamespace(send_request=self.sent.append)

    def tearDown(self):
        self.relay.log.close()
        self.relay.control.close()
        self.directory.cleanup()

    def on_firmware(self, version, image="production"):
        self.relay.bridge = {"image": image}
        self.relay.status.set(firmware={"state": "known", "version": version})

    def ask(self, request):
        connection = Connection()
        self.relay.request_kit(connection, dict(request, method="kit"),
                               running=True)
        return connection

    def test_an_older_or_lab_bridge_is_never_sent_a_kit_request(self):
        self.on_firmware(39)
        connection = self.ask({"shot": 1})
        self.assertEqual(connection.lines[0]["error"], "unsupported-firmware")
        self.relay.kit_service = True
        self.relay.drive_kit(self.daemon, 0.0)
        self.assertEqual(self.sent, [])
        self.on_firmware(40, image="lab")
        self.relay.drive_kit(self.daemon, 0.0)
        self.assertEqual(self.sent, [])

    def test_no_service_unless_asked(self):
        self.on_firmware(40)
        self.relay.drive_kit(self.daemon, 0.0)
        self.assertEqual(self.sent, [])
        connection = self.ask({})
        self.assertEqual(connection.lines[0]["error"], "no-report")

    def test_the_service_answers_and_comes_back_each_session(self):
        self.on_firmware(40)
        connection = self.ask({"service": True})
        self.relay.drive_kit(self.daemon, 0.0)
        self.assertEqual(self.sent[-1].payload, kit.service().payload)
        self.relay.accept_kit(report(event=0))           # a change first
        self.assertEqual(connection.lines, [])
        self.relay.accept_kit(report(event=1))
        self.assertTrue(connection.lines[0]["ok"])
        self.relay.end_kit()                             # the session ends
        self.relay.drive_kit(self.daemon, 0.1)
        self.assertEqual([m.payload for m in self.sent],
                         [kit.service().payload] * 2)
        self.relay.drive_kit(self.daemon, 0.2)
        self.assertEqual(len(self.sent), 2)

    def test_the_relay_sends_the_service_only_as_0_or_1(self):
        self.on_firmware(40)
        for value in (True, 7, "yes", 1, False, 0, None, ""):
            self.ask({"service": value})
        self.relay.drive_kit(self.daemon, 0.0)
        self.assertEqual([m.payload[1] for m in self.sent],
                         [1, 1, 1, 1, 0, 0, 0, 0])
        self.assertTrue(all(m.payload[0] == 1 for m in self.sent))

    def test_service_off_is_sent_once_and_not_given_again(self):
        self.on_firmware(40)
        self.relay.kit_service = True
        off = self.ask({"service": False})
        self.relay.drive_kit(self.daemon, 0.0)
        self.assertEqual(self.sent[-1].payload, kit.service(False).payload)
        self.relay.accept_kit(report(event=1, service=False))
        self.assertTrue(off.lines[0]["ok"])
        self.assertFalse(self.relay.status.state["kit"]["service"])
        self.relay.end_kit()
        self.relay.drive_kit(self.daemon, 0.1)
        self.assertEqual(len(self.sent), 1)

    def test_an_unanswered_request_times_out(self):
        self.on_firmware(40)
        late = self.ask({"service": True})
        self.relay.drive_kit(self.daemon, 0.0)
        self.relay.drive_kit(self.daemon,
                             time.monotonic() + relay.KIT_ANSWER_WAIT + 1.0)
        self.assertEqual(late.lines[0]["error"], "no-answer")
        bad = self.ask({"shot": 300})
        self.assertEqual(bad.lines[0]["error"], "bad-request")

    def test_a_shot_is_answered_with_its_status(self):
        self.on_firmware(40)
        connection = self.ask({"shot": 1})
        self.relay.drive_kit(self.daemon, 0.0)
        self.assertEqual(self.sent[-1].payload, kit.shot(1).payload)
        self.relay.accept_kit(report(event=2, status=1, shots=1))
        reply = connection.lines[0]
        self.assertTrue(reply["ok"] and connection.closed)
        self.assertEqual((reply["shot"], reply["shots"]), ("queued", 1))
        self.assertIn("last_shot", reply)
        refused = self.ask({"shot": 1})
        self.relay.drive_kit(self.daemon, 0.1)
        self.relay.accept_kit(report(event=2, status=3, shots=1))
        self.assertFalse(refused.lines[0]["ok"])
        self.assertEqual(refused.lines[0]["shot"], "refused")
        self.assertEqual(self.relay.status.state["kit"]["shots"], 1)

    def test_the_status_entry_and_replies_carry_only_what_openams_shows(self):
        self.on_firmware(40)
        connection = self.ask({"service": True})
        self.relay.drive_kit(self.daemon, 0.0)
        self.relay.accept_kit(report(event=1, shots=4))
        entry = self.relay.status.state["kit"]
        self.assertLessEqual(set(entry), ALLOWED)
        self.assertLessEqual(set(connection.lines[0]), ALLOWED | {"event"})
        for word in ("version", "serial", '"id"', "raw"):
            self.assertNotIn(word, json.dumps(entry))

    def test_presence_is_logged_once_per_change(self):
        self.on_firmware(40)
        records = []
        self.relay.log = types.SimpleNamespace(
            record=lambda event, **fields: records.append((event, fields)),
            close=lambda: None)
        for present in (False, True, True, True, False):
            self.relay.accept_kit(report(present=present))
        self.assertEqual(records, [("kit_presence", {"present": True}),
                                   ("kit_presence", {"present": False})])


class Cli(unittest.TestCase):
    def test_the_kit_lines_show_presence_service_and_shots_only(self):
        lines = cli.kit_lines({"ok": True, "present": True, "service": True,
                               "shots": 3,
                               "last_shot": "2026-11-20T18:00:00Z"})
        self.assertEqual(lines, ["CyberBrick kit: present",
                                 "  service: on",
                                 "  shots: 3, last 2026-11-20T18:00:00Z"])

    def test_the_kit_command_offers_status_shot_on_and_off(self):
        parser = cli.parser() if hasattr(cli, "parser") else None
        if parser is None:
            self.skipTest("no CLI parser builder")
        args = parser.parse_args(["kit", "shot", "--p", "1"])
        self.assertEqual((args.action, args.p), ("shot", 1))
        for action in ("status", "on", "off"):
            self.assertEqual(parser.parse_args(["kit", action]).action, action)
        with self.assertRaises(SystemExit), \
                contextlib.redirect_stderr(io.StringIO()):
            parser.parse_args(["kit", "dump"])      # no raw report path


if __name__ == "__main__":
    unittest.main()
