"""The bridge's firmware version: framing, the report, the query and the
words a customer reads about an update."""

from pathlib import Path
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cortex_bridge import bridge_info, update_messages  # noqa: E402
from cortex_bridge.cortexd import (  # noqa: E402
    CortexCore,
    Direction,
    Message,
    MessageType,
    NodeStatus,
    ProtocolError,
    make_roster,
)

UID = bytes.fromhex("0a1b2c3d4e5f")
TOKEN = b"\x01\x02\x03\x04"


def report(token=TOKEN, version=7, status=0, boot_abi=6, genuine=0,
           reset_flags=0x08, uptime_ms=1234, extra=b""):
    if status:
        return bytes(token) + bytes((status,))
    body = bytes((1, 1)) + version.to_bytes(4, "big") + \
        boot_abi.to_bytes(2, "big") + bytes((genuine, reset_flags)) + \
        uptime_ms.to_bytes(4, "big") + bytes(9 + 12 + 156) + extra
    return bytes(token) + b"\x00" + body


class FramingTests(unittest.TestCase):
    def test_the_host_may_send_a_four_byte_identity_request(self):
        encoded = Message(MessageType.IDENTITY_REQUEST, TOKEN).encode(
            Direction.HOST_TO_BRIDGE)
        self.assertEqual(encoded, bytes((4, 4, 0, 4)) + TOKEN)
        for payload in (b"", TOKEN + b"\x00"):
            with self.assertRaises(ProtocolError):
                Message(MessageType.IDENTITY_REQUEST, payload).encode(
                    Direction.HOST_TO_BRIDGE)

    def test_requests_and_reports_go_one_way_only(self):
        with self.assertRaises(ProtocolError):
            Message(MessageType.IDENTITY_REQUEST, TOKEN).encode(
                Direction.BRIDGE_TO_HOST)
        with self.assertRaises(ProtocolError):
            Message(MessageType.IDENTITY_REPORT, report()).encode(
                Direction.HOST_TO_BRIDGE)

    def test_reports_of_any_reasonable_length_are_framed(self):
        for payload in (report(status=4), report(), report(extra=bytes(100))):
            encoded = Message(MessageType.IDENTITY_REPORT, payload).encode(
                Direction.BRIDGE_TO_HOST)
            self.assertEqual(Message.decode(encoded, Direction.BRIDGE_TO_HOST)
                             .payload, payload)
        with self.assertRaises(ProtocolError):
            Message(MessageType.IDENTITY_REPORT, TOKEN).encode(
                Direction.BRIDGE_TO_HOST)

    def core(self, tmp):
        core = CortexCore(Path(tmp))
        self.addCleanup(core.close)
        core.attach(1)
        return core

    def encoded_report(self, payload):
        return Message(MessageType.IDENTITY_REPORT, payload).encode(
            Direction.BRIDGE_TO_HOST)

    def test_a_report_reaches_the_handler_even_before_the_roster(self):
        with tempfile.TemporaryDirectory() as tmp:
            core = self.core(tmp)
            seen = []
            core.identity_handler = seen.append
            core.accept(self.encoded_report(report()))
            self.assertEqual(seen, [report()])

    def test_a_report_nobody_asked_for_or_a_failing_handler_is_harmless(self):
        with tempfile.TemporaryDirectory() as tmp:
            core = self.core(tmp)
            core.accept(make_roster(1, [NodeStatus(UID, True)]).encode(
                Direction.BRIDGE_TO_HOST))
            core.accept(self.encoded_report(report()))

            def broken(_payload):
                raise RuntimeError("bug")

            core.identity_handler = broken
            core.accept(self.encoded_report(report()))
            self.assertEqual(core.identity_handler_errors, 1)
            self.assertTrue(core.nodes[UID].online, "the units stay online")


class ReportTests(unittest.TestCase):
    def test_the_fields_are_read(self):
        parsed = bridge_info.parse_report(report(version=9, boot_abi=6,
                                                 reset_flags=0x08 | 0x04,
                                                 uptime_ms=5000))
        self.assertEqual(parsed["status"], "ok")
        self.assertEqual(parsed["version"], 9)
        self.assertEqual(parsed["boot_abi"], 6)
        self.assertEqual(parsed["app_kind"], "bridge")
        self.assertEqual(parsed["genuine"], "passed")
        self.assertEqual(parsed["reset_cause"], "power-on")
        self.assertEqual(parsed["uptime_ms"], 5000)

    def test_genuine_and_reset_cause_names(self):
        self.assertEqual(bridge_info.parse_report(report(genuine=0xFF))
                         ["genuine"], "pending")
        self.assertEqual(bridge_info.parse_report(report(genuine=3))
                         ["genuine"], "failed")
        self.assertEqual(bridge_info.reset_cause(0x20 | 0x08), "watchdog")
        self.assertEqual(bridge_info.reset_cause(0x10), "software")
        self.assertEqual(bridge_info.reset_cause(0), "unknown")

    def test_a_failure_carries_only_its_status(self):
        parsed = bridge_info.parse_report(report(status=4))
        self.assertEqual(parsed["status"], "busy")
        self.assertNotIn("version", parsed)
        self.assertEqual(bridge_info.parse_report(report(status=9))["status"],
                         "status-9")

    def test_an_unknown_format_is_an_error(self):
        payload = bytearray(report())
        payload[5] = 2
        with self.assertRaises(bridge_info.ReportError):
            bridge_info.parse_report(bytes(payload))
        with self.assertRaises(bridge_info.ReportError):
            bridge_info.parse_report(TOKEN + b"\x00\x01")


class Tokens:
    def __init__(self):
        self.count = 0

    def __call__(self, size):
        self.count += 1
        return bytes((self.count,)) * size


class QueryTests(unittest.TestCase):
    def query(self):
        return bridge_info.IdentityQuery(0.0, token_source=Tokens())

    def test_it_asks_once_after_the_roster_settles(self):
        query = self.query()
        self.assertIsNone(query.due(0.1))
        request = query.due(1.0)
        self.assertEqual(request.kind, MessageType.IDENTITY_REQUEST)
        self.assertIsNone(query.due(1.5), "one request at a time")
        self.assertTrue(query.accept(report(token=request.payload), 2.0))
        self.assertEqual(query.firmware["state"], "known")
        self.assertEqual(query.firmware["version"], 7)
        self.assertIsNone(query.due(100.0), "never asked again")

    def test_an_answer_to_another_request_is_ignored(self):
        query = self.query()
        query.due(1.0)
        self.assertFalse(query.accept(report(token=b"zzzz"), 1.1))
        self.assertEqual(query.firmware["state"], "querying")

    def test_busy_answers_are_retried_with_a_growing_delay(self):
        query = self.query()
        first = query.due(1.0)
        query.accept(report(token=first.payload, status=4), 1.1)
        self.assertEqual(query.firmware["reason"], "busy")
        self.assertIsNone(query.due(2.0))
        second = query.due(3.2)
        self.assertIsNotNone(second)
        self.assertNotEqual(second.payload, first.payload)
        query.accept(report(token=second.payload, status=4), 3.3)
        self.assertIsNone(query.due(6.0))
        self.assertIsNotNone(query.due(7.4))

    def test_silence_gives_up_after_three_requests(self):
        query = self.query()
        now = 1.0
        sent = 0
        while not query.finished and now < 100:
            if query.due(now) is not None:
                sent += 1
            now += 0.5
        self.assertEqual(sent, bridge_info.MAX_SILENT)
        self.assertEqual(query.firmware["state"], "unavailable")
        self.assertEqual(query.firmware["reason"], "no-answer")

    def test_a_boot_failure_or_unreadable_answer_ends_the_query(self):
        query = self.query()
        request = query.due(1.0)
        query.accept(report(token=request.payload, status=5), 1.1)
        self.assertEqual(query.firmware["state"], "unavailable")
        self.assertEqual(query.firmware["reason"], "boot-failed")
        query = self.query()
        request = query.due(1.0)
        query.accept(request.payload + b"\x00\x02", 1.1)
        self.assertEqual(query.firmware["reason"], "unreadable-report")


class FakeBridgePort:
    """A bridge port that announces a roster and answers identity requests."""

    timeout = 0.25

    def __init__(self, answer):
        self.answer = answer
        self.incoming = bytearray(make_roster(1, [NodeStatus(UID, True)])
                                  .encode(Direction.BRIDGE_TO_HOST))
        self.requests = []
        self.closed = False

    def read(self, length):
        chunk = bytes(self.incoming[:length])
        del self.incoming[:length]
        return chunk

    def write(self, data):
        message = Message.decode(bytes(data), Direction.HOST_TO_BRIDGE)
        self.requests.append(message)
        reply = self.answer(message.payload)
        if reply is not None:
            self.incoming.extend(Message(MessageType.IDENTITY_REPORT, reply)
                                 .encode(Direction.BRIDGE_TO_HOST))
        return len(data)

    def close(self):
        self.closed = True


class DirectQueryTests(unittest.TestCase):
    def run_query(self, answer):
        port = FakeBridgePort(answer)
        module = types.SimpleNamespace(Serial=lambda *a, **k: port)
        clock = [0.0]

        def tick():
            clock[0] += 0.05
            return clock[0]

        firmware = bridge_info.query_direct(
            "/dev/ttyACM9", serial_module=module, clock=tick,
            sleep=lambda _s: None)
        self.assertTrue(port.closed)
        return firmware, port

    def test_the_version_is_read_and_the_port_closed(self):
        firmware, port = self.run_query(lambda token: report(token=token))
        self.assertEqual(firmware["version"], 7)
        self.assertEqual(len(port.requests), 1)

    def test_a_silent_bridge_is_unavailable(self):
        firmware, port = self.run_query(lambda token: None)
        self.assertEqual(firmware["state"], "unavailable")
        self.assertEqual(firmware["reason"], "no-answer")


class CompareTests(unittest.TestCase):
    BUNDLED = [{"version": 7, "ok": True}, {"version": 4, "ok": True}]

    def verdict(self, version, bundled=None):
        firmware = bridge_info.firmware_status(
            "known", "", dict(bridge_info.parse_report(report(version=version))))
        return bridge_info.compare(firmware, self.BUNDLED if bundled is None
                                   else bundled)["verdict"]

    def test_verdicts(self):
        self.assertEqual(self.verdict(7), "up-to-date")
        self.assertEqual(self.verdict(4), "update-available")
        self.assertEqual(self.verdict(9), "newer-than-bundled")
        self.assertEqual(self.verdict(7, []), "nothing-bundled")
        unknown = bridge_info.firmware_status("unavailable", "lab-firmware")
        self.assertEqual(bridge_info.compare(unknown, self.BUNDLED)["verdict"],
                         "unknown")
        self.assertIn("lab", bridge_info.unknown_reason(unknown))
        self.assertIn("not connected", bridge_info.unknown_reason(None))

    def test_an_unusable_package_is_never_the_newest(self):
        bundled = [{"version": 9, "ok": False}, {"version": 7, "ok": True}]
        self.assertEqual(self.verdict(7, bundled), "up-to-date")


class MessageTests(unittest.TestCase):
    ERRORS = ("AlreadyInstalled", "OlderThanInstalled", "PrinterBusy",
              "UpdateModeRefused", "PackageRefused", "ReceiverRejected",
              "TransferInterrupted", "BridgeFault", "BridgeNotFound",
              "BridgeAmbiguous", "UpdateError", "SomethingNew")

    def test_every_outcome_says_what_happened_changed_and_to_do(self):
        for error in self.ERRORS:
            for code in ("", "busy", "no-receiver", "boot-abi", "lab-boot",
                         "did-not-return", "not-finished", "port-in-use",
                         "unverified"):
                text = update_messages.explain(error, reason_code=code,
                                               running=4, package=7,
                                               post_state="application")
                for key in ("summary", "outcome", "action"):
                    self.assertTrue(text[key].strip(), (error, code, key))
                    self.assertTrue(text[key].rstrip().endswith((".", ")")),
                                    (error, code, key, text[key]))

    def test_a_refusal_uses_the_running_version_when_known(self):
        known = update_messages.explain("ReceiverRejected", running=4,
                                        package=7, post_state="application")
        self.assertIn("It runs version 4, so the version is not the reason",
                      known["summary"])
        self.assertIn("still runs version 4", known["outcome"])
        unknown = update_messages.explain("ReceiverRejected", package=7,
                                          post_state="receiver")
        self.assertIn("not newer than the version it runs", unknown["summary"])
        self.assertIn("cortex-bridge.py version", unknown["action"])
        self.assertIn("waiting for firmware", unknown["outcome"])

    def test_an_exception_is_explained_from_its_attributes(self):
        from cortex_bridge import updater
        error = updater.UpdateModeRefused("x", reason_code="busy")
        text = update_messages.explain_exception(error, running=7, package=9)
        self.assertIn("did not switch to update mode", text["summary"])
        self.assertIn("still runs version 7", text["outcome"])
        fault = updater.BridgeFault("F-BOARD-01")
        text = update_messages.explain_exception(fault)
        self.assertIn("F-BOARD-01", text["summary"])
        self.assertIn("startup check", text["summary"])
        self.assertIn("F-BOARD-01", text["action"])
        with self.assertRaises(updater.PackageRefused) as caught:
            updater.inspect_package(b"not firmware")
        text = update_messages.explain_exception(caught.exception)
        self.assertEqual(text["summary"], "This file cannot be installed: "
                         "this is not an OAC2 firmware package.")

    def test_lines_put_the_technical_detail_last(self):
        text = update_messages.explain("BridgeAmbiguous")
        lines = update_messages.lines(text, "2 bridges: CB1, CB2")
        self.assertEqual(lines[-1], "Details: 2 bridges: CB1, CB2")
        self.assertEqual(len(update_messages.lines(text)), 3)


if __name__ == "__main__":
    unittest.main()
