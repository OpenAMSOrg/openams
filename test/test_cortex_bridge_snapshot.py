"""The bridge support snapshot: wire decoding, the firmware gate, the query's
retries, the plain reading, and what a report may carry.

``encode_*`` below pack bodies straight from the wire layout with ``struct``,
independently of the decoder's own tables. Nothing here touches hardware.
"""

import base64
import json
from pathlib import Path
import struct
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cortex_bridge import bridge_snapshot as snap  # noqa: E402
from cortex_bridge.cortexd import (  # noqa: E402
    CortexCore,
    Direction,
    Message,
    MessageType,
    ProtocolError,
)

UID = bytes.fromhex("0a1b2c3d4e5f")
TOKEN = b"\x01\x02\x03\x04"
VECTORS_FILE = ROOT / "test" / "fixtures" / "bridge_snapshot" / \
    "vectors_v1.json"


def load_vectors() -> dict:
    """The firmware encoder's golden vectors, by name."""
    with open(VECTORS_FILE, encoding="utf-8") as handle:
        return {case["name"]: case for case in json.load(handle)["cases"]}


VECTORS = load_vectors()
SILENT_LINE_BODY = bytes.fromhex(VECTORS["silent_line"]["body_hex"])
SILENT_LINE = VECTORS["silent_line"]["decoded"]


def section(section_id, value):
    return bytes((section_id, len(value))) + value


def encode_system(app_version=9, uptime_ms=81234, reset_flags=24,
                  boot_status=0, genuine_status=0, image=0xDF,
                  min_evidence=0, policy=0x77, role=0x01,
                  master_acquisitions=1, master_since_ms=3120,
                  max_pass_gap_us=2210):
    return section(0x01, struct.pack(
        ">IIBBBBBBBHII", app_version, uptime_ms, reset_flags, boot_status,
        genuine_status, image, min_evidence, policy, role,
        master_acquisitions, master_since_ms, max_pass_gap_us))


def encode_line(**fields):
    names = ("bytes", "parity_errors", "framing_errors", "noise_errors",
             "overruns", "messages", "check_errors", "malformed",
             "skipped_bytes", "first_rx_ms", "last_rx_ms", "last_answer_ms")
    return section(0x02, struct.pack(">12I", *(fields.get(n, 0)
                                                for n in names)))


def encode_master(requests=1830, answers=0, unanswered=1830,
                  send_failures=0, foreign_requests=0, unmatched_answers=0,
                  sent_messages=2560):
    return section(0x03, struct.pack(
        ">7I", requests, answers, unanswered, send_failures,
        foreign_requests, unmatched_answers, sent_messages))


def encode_discovery(announces=0, first_announce_ms=0, last_announce_ms=0,
                     last_announce_marker=0, last_announce_tag_family=255,
                     offers=0, offer_giveups=0, last_offer_index=255,
                     last_offer_basis=0, last_offer_ms=0,
                     identity_conflicts=0, candidates_dropped=0,
                     index_polls=36, refused=(0, 0, 0, 0, 0), admitted=0,
                     candidates=0, omitted=0,
                     last_announce_tag_suffix=b"\x00" * 4):
    return section(0x04, struct.pack(
        ">IIIBBIHBBIHHIHHHHHBBB4s", announces, first_announce_ms,
        last_announce_ms, last_announce_marker, last_announce_tag_family,
        offers, offer_giveups, last_offer_index, last_offer_basis,
        last_offer_ms, identity_conflicts, candidates_dropped, index_polls,
        *refused, admitted, candidates, omitted, last_announce_tag_suffix))


def encode_host(usb_sessions=1, protocol_faults=0, recoveries=0,
                host_messages=0, host_dropped=0, host_stale=0,
                update_refused=0, attach_refused=0, roster_rejected=0):
    return section(0x05, struct.pack(
        ">IHHIHHHHH", usb_sessions, protocol_faults, recoveries,
        host_messages, host_dropped, host_stale, update_refused,
        attach_refused, roster_rejected))


def encode_unit(slot=0, uid=UID, index=0, link=2, family=2,
                ams_version=b"\x00\x00\x06\x31", answers=500, timeouts=1,
                last_answer_ms=80000, ams_error=0, motion_error=0,
                ams_state=1, spools_present=0x0B, host=0x01,
                configurations=0, host_quiet_ms=0xFFFFFFFF, operation=0x0F,
                stage=0, fault_severity=0, fault_code=0, fault_bay=255,
                loaded_bay=255, path_mm=0, failures=0,
                last_failure_kind=255, last_failure_outcome=255,
                last_failure_detail=0, last_failure_ms=0, tag_suffix=b"N573"):
    return section(0x10, struct.pack(
        ">B6sBBB4sHHIBBBBBHIBBBHBBHHBBHI4s", slot, uid, index, link, family,
        ams_version, answers, timeouts, last_answer_ms, ams_error,
        motion_error, ams_state, spools_present, host, configurations,
        host_quiet_ms, operation, stage, fault_severity, fault_code,
        fault_bay, loaded_bay, path_mm, failures, last_failure_kind,
        last_failure_outcome, last_failure_detail, last_failure_ms,
        tag_suffix))


def encode_candidate(index=0x80, link=4, refusal=2, waiting=0, family=2,
                     model_class=3, tag_family=0, flags=0x0D,
                     ams_version=b"\x00\x00\x06\x31", answers=12, timeouts=0,
                     first_seen_ms=5000, last_answer_ms=79000, ams_error=0,
                     tag_suffix=b"N700"):
    return section(0x11, struct.pack(
        ">BBBBBBBB4sHHIIB4s", index, link, refusal, waiting, family,
        model_class, tag_family, flags, ams_version, answers, timeouts,
        first_seen_ms, last_answer_ms, ams_error, tag_suffix))


def encode_feed_failure(slot=0, index=0, odo_start_mm=1200, odo_low_mm=600,
                        odo_at_mm=601, since_progress_ms=4000, feed_ms=6193,
                        odo_flags=0x3F, ams_state=3):
    return section(0x12, struct.pack(
        ">BBiiiIIBB", slot, index, odo_start_mm, odo_low_mm, odo_at_mm,
        since_progress_ms, feed_ms, odo_flags, ams_state))


def encode_refusal(at_ms=6000, index=0x80, reason=2, family=2,
                   model_class=3, tag_family=0, tag_suffix=b"N700"):
    return section(0x20, struct.pack(">IBBBBB4s", at_ms, index, reason,
                                     family, model_class, tag_family,
                                     tag_suffix))


def body(*sections, system=None, line=None, master=None, discovery=None,
         host=None):
    return bytes((1,)) + (system or encode_system()) + \
        (line or encode_line()) + (master or encode_master()) + \
        (discovery or encode_discovery()) + (host or encode_host()) + \
        b"".join(sections)


def report(status=0, payload_body=SILENT_LINE_BODY, token=TOKEN):
    return bytes(token) + bytes((status,)) + \
        (payload_body if status == 0 else b"")


class GoldenVectorTests(unittest.TestCase):
    """The bridge firmware's own encoder wrote these bodies; the decoder
    must give exactly the decoded JSON that came with them."""

    def test_each_body_decodes_to_its_json_exactly(self):
        for name, case in VECTORS.items():
            decoded = snap.decode_body(bytes.fromhex(case["body_hex"]))
            self.assertEqual(decoded, case["decoded"], name)
            self.assertEqual(json.dumps(decoded), json.dumps(case["decoded"]),
                             "%s: same keys in the same order" % name)

    def test_each_whole_message_frames_and_parses(self):
        for name, case in VECTORS.items():
            message = Message.decode(bytes.fromhex(case["message_hex"]),
                                     Direction.BRIDGE_TO_HOST)
            self.assertEqual(message.kind, MessageType.SUPPORT_REPORT, name)
            parsed = snap.parse_report(message.payload)
            self.assertEqual((parsed["token"], parsed["status"]),
                             (TOKEN, 0), name)
            self.assertEqual(parsed["body"].hex(), case["body_hex"], name)

    def test_each_reads_as_its_class(self):
        for name, case in VECTORS.items():
            self.assertEqual(snap.classify(case["decoded"])["class"],
                             case["reading"], name)
        refused = snap.classify(VECTORS["refused_at_ht_index"]["decoded"])
        self.assertEqual(refused["reason"], "index_mismatch")
        self.assertIn("tag suffix N700, not one this firmware recognises",
                      refused["summary"])
        unused = snap.classify(VECTORS["admitted_not_configured"]["decoded"])
        self.assertIn("/dev/oams/151a58f19349", unused["action"])

    def test_the_allowlist_keeps_every_decoded_field(self):
        for name, case in VECTORS.items():
            self.assertEqual(snap.allowlisted(case["decoded"]),
                             case["decoded"], name)


class DecodeTests(unittest.TestCase):
    def test_section_sizes_follow_the_wire_layout(self):
        sizes = {name: snap.layout_size(layout)
                 for name, layout, _ in snap.SECTIONS.values()}
        self.assertEqual(sizes, {"system": 25, "line": 48, "master": 28,
                                 "discovery": 51, "host": 22, "units": 56,
                                 "candidates": 29, "feed_failures": 24,
                                 "dryers": 77, "motor_samples": 23,
                                 "refusals": 13})
        self.assertEqual(len(body()), len(SILENT_LINE_BODY))

    def test_motor_samples_decode_and_read(self):
        # Bridge firmware 15, section 0x14: the hub servo at setpoint 0x00,
        # with the first stage idle, then a pull back with it running.
        def sample(primitive, reference, odo, buffer, motors, age=500):
            return (bytes([1]) + age.to_bytes(4, "big")
                    + bytes([primitive, reference])
                    + odo.to_bytes(2, "big", signed=True)
                    + buffer.to_bytes(2, "big", signed=True)
                    + b"".join(v.to_bytes(2, "big", signed=True)
                               for v in motors))
        first = sample(0x09, 0x00, 2251, -1200, (-7, -8, 120, 0, 0, 0))
        second = sample(0x03, 0xFF, 2230, -5400, (-7, -8, 300, 40, 12,
                                                  -16384), age=100)
        # one row per section, the id repeated
        raw = (bytes([1, 0x14, len(first)]) + first
               + bytes([0x14, len(second)]) + second)
        decoded = snap.decode_body(raw)
        self.assertEqual(len(decoded["motor_samples"]), 2)
        self.assertEqual(decoded["motor_samples"][0]["buffer"], -1200)
        self.assertEqual(decoded["motor_samples"][1]["m32"], -16384)
        self.assertEqual(snap.allowlisted(decoded)["motor_samples"],
                         decoded["motor_samples"])
        lines = snap.motor_sample_lines(decoded)
        self.assertIn("hub servo (09), setpoint 0x00", lines[0])
        self.assertNotIn("first stage running", lines[0])
        self.assertIn("first stage running", lines[1])

    def test_no_motor_samples_key_without_samples(self):
        decoded = snap.decode_body(body())
        self.assertNotIn("motor_samples", decoded)
        self.assertNotIn("motor_samples", snap.allowlisted(decoded))

    def test_rows_decode(self):
        decoded = snap.decode_body(body(
            encode_unit(), encode_unit(slot=1, uid=bytes.fromhex(
                "5f4e3d2c1b0a"), index=1, host=0x17, operation=0x81,
                host_quiet_ms=1500, last_failure_kind=0,
                last_failure_outcome=3, last_failure_detail=0x1B,
                fault_severity=2, tag_suffix=b"\x00" * 4),
            encode_candidate(tag_suffix=b"N\x01\x02\x03"), encode_refusal(),
            discovery=encode_discovery(last_announce_marker=0xFE,
                                       last_announce_tag_suffix=b"N700")))
        first, second = decoded["units"]
        self.assertEqual(first["uid"], "0a1b2c3d4e5f")
        self.assertEqual(first["ams_version"], [0, 0, 6, 49])
        self.assertEqual((first["link"], first["family"], first["ams_state"]),
                         ("online", "ams2pro", "idle"))
        self.assertEqual(first["host"], {"attached": True,
                                         "configured": False,
                                         "heard": False, "latch": "none"})
        self.assertIsNone(first["host_quiet_ms"])
        self.assertEqual(first["operation"], {"active": False, "kind": None})
        self.assertIsNone(first["fault_bay"])
        self.assertIsNone(first["last_failure_kind"])
        self.assertIsNone(first["last_failure_outcome"])
        self.assertEqual(first["tag_suffix"], "N573")
        self.assertEqual(second["host"], {"attached": True,
                                          "configured": True, "heard": True,
                                          "latch": "emergency"})
        self.assertEqual(second["operation"], {"active": True,
                                               "kind": "unload"})
        self.assertEqual((second["last_failure_kind"],
                          second["last_failure_outcome"],
                          second["last_failure_detail"],
                          second["fault_severity"]),
                         ("load", "timeout", 0x1B, "pause"))
        self.assertIsNone(second["tag_suffix"])
        candidate = decoded["candidates"][0]
        self.assertEqual((candidate["refusal"], candidate["tag_family"],
                          candidate["model_class"]),
                         ("index_mismatch", "unknown", "supported"))
        self.assertEqual(candidate["flags"]["announce"], True)
        self.assertEqual(candidate["flags"]["identity"], False)
        self.assertEqual(candidate["tag_suffix"], "0x4e010203")
        self.assertEqual(decoded["refusals"][0]["reason"], "index_mismatch")
        self.assertEqual(decoded["refusals"][0]["tag_suffix"], "N700")
        self.assertEqual(decoded["discovery"]["last_announce_marker"],
                         "ams_ht")
        self.assertEqual(decoded["discovery"]["last_announce_tag_suffix"],
                         "N700")

    def test_unknown_sections_and_appended_fields_are_skipped(self):
        extended = bytes((1,)) + section(0x01, encode_system()[2:] + b"\xEE" *
                                         3) + encode_line() + \
            encode_master() + encode_discovery() + encode_host() + \
            section(0x06, b"later") + section(0x30, b"\x00" * 7)
        self.assertEqual(snap.decode_body(extended), snap.decode_body(body()))

    def test_unknown_enum_values_are_named_not_dropped(self):
        decoded = snap.decode_body(body(encode_unit(link=9, family=7,
                                                    stage=42)))
        unit = decoded["units"][0]
        self.assertEqual((unit["link"], unit["family"], unit["stage"]),
                         ("unknown_9", "unknown_7", "unknown_42"))

    def test_malformed_bodies(self):
        cases = {
            "empty": b"",
            "format 2": b"\x02" + SILENT_LINE_BODY[1:],
            "short section": bytes((1,)) + section(0x01, b"\x00" * 24),
            "cut header": SILENT_LINE_BODY + b"\x10",
            "cut value": SILENT_LINE_BODY + b"\x10\x34\x00",
            "out of order": bytes((1,)) + encode_line() + encode_system(),
            "twice": bytes((1,)) + encode_system() + encode_system(),
        }
        for name, data in cases.items():
            with self.assertRaises(snap.SnapshotError, msg=name):
                snap.decode_body(data)

    def test_report_framing(self):
        parsed = snap.parse_report(report())
        self.assertEqual((parsed["token"], parsed["status"]), (TOKEN, 0))
        self.assertEqual(snap.parse_report(report(status=1))["body"], b"")
        with self.assertRaises(snap.SnapshotError):
            snap.parse_report(TOKEN + b"\x00")       # ok without a body


class CortexdTests(unittest.TestCase):
    def test_message_framing(self):
        encoded = snap.make_request(TOKEN).encode(Direction.HOST_TO_BRIDGE)
        self.assertEqual(encoded, b"\x04\x08\x00\x04" + TOKEN)
        with self.assertRaises(ProtocolError):
            Message(MessageType.SUPPORT_REQUEST, TOKEN + b"x").encode(
                Direction.HOST_TO_BRIDGE)
        with self.assertRaises(ProtocolError):
            Message(MessageType.SUPPORT_REPORT, report()).encode(
                Direction.HOST_TO_BRIDGE)            # bridge-only
        for payload in (TOKEN, TOKEN + b"\x00" * 509):
            with self.assertRaises(ProtocolError):
                Message(MessageType.SUPPORT_REPORT, payload).encode(
                    Direction.BRIDGE_TO_HOST)
        Message(MessageType.SUPPORT_REPORT, report()).encode(
            Direction.BRIDGE_TO_HOST)

    def test_reports_go_to_the_handler_and_never_fail_the_session(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            core = CortexCore(Path(tmp))
            try:
                core.attach(1)
                seen = []
                encoded = Message(MessageType.SUPPORT_REPORT,
                                  report()).encode(Direction.BRIDGE_TO_HOST)
                core.accept(encoded)                 # nobody asked: ignored
                core.support_handler = seen.append
                core.accept(encoded)
                self.assertEqual(seen, [report()])

                def broken(payload):
                    raise RuntimeError("handler bug")
                core.support_handler = broken
                core.accept(encoded)
                self.assertEqual(core.support_handler_errors, 1)
            finally:
                core.close()


class GateTests(unittest.TestCase):
    PRODUCTION = {"manufacturer": "SiForge"}

    @staticmethod
    def known(version):
        return {"state": "known", "version": version}

    def test_only_production_firmware_9_or_later_is_asked(self):
        self.assertTrue(snap.gate(self.PRODUCTION, self.known(9))[0])
        self.assertTrue(snap.gate(self.PRODUCTION, self.known(12))[0])
        for version in (2, 7, 8):
            allowed, error, message = snap.gate(self.PRODUCTION,
                                                self.known(version))
            self.assertFalse(allowed, version)
            self.assertEqual(error, "unsupported-firmware")
            self.assertIn("firmware %d" % version, message)

    def test_lab_unknown_or_unidentified_bridges_are_not_asked(self):
        cases = (
            ({"manufacturer": "OpenAMS"}, self.known(9)),
            ({"manufacturer": ""}, self.known(9)),
            (None, self.known(9)),
            (self.PRODUCTION, {"state": "querying", "version": None}),
            (self.PRODUCTION, {"state": "unavailable", "version": None}),
            (self.PRODUCTION, None),
        )
        for bridge, firmware in cases:
            self.assertFalse(snap.gate(bridge, firmware)[0],
                             (bridge, firmware))


class QueryTests(unittest.TestCase):
    def tokens(self):
        counter = [0]

        def source(size):
            counter[0] += 1
            return bytes((counter[0],)) * size
        return source

    def test_a_snapshot_on_the_first_try(self):
        query = snap.SnapshotQuery(10.0, token_source=self.tokens())
        request = query.due(10.0)
        self.assertEqual(request.kind, MessageType.SUPPORT_REQUEST)
        self.assertIsNone(query.due(10.5), "one request at a time")
        self.assertFalse(query.accept(report(token=b"\x09" * 4), 10.6),
                         "another token is not the answer")
        self.assertTrue(query.accept(report(token=request.payload), 10.7))
        self.assertTrue(query.reply["ok"])
        self.assertEqual(query.reply["snapshot"], SILENT_LINE)
        self.assertEqual(base64.b64decode(query.reply["raw"]),
                         SILENT_LINE_BODY)

    def test_rate_limited_and_busy_are_retried_three_times_at_most(self):
        for status, error in ((1, "rate-limited"), (4, "busy")):
            query = snap.SnapshotQuery(0.0, token_source=self.tokens())
            now = 0.0
            sent = []
            while not query.finished and now < 30:
                request = query.due(now)
                if request is not None:
                    sent.append(now)
                    query.accept(report(status, token=request.payload), now)
                now = round(now + 0.1, 1)
            self.assertEqual(len(sent), snap.MAX_ATTEMPTS)
            self.assertGreaterEqual(sent[1] - sent[0], snap.RETRY_DELAY_S)
            self.assertEqual(query.reply["error"], error)

    def test_a_retry_that_then_answers(self):
        query = snap.SnapshotQuery(0.0, token_source=self.tokens())
        first = query.due(0.0)
        self.assertFalse(query.accept(report(1, token=first.payload), 0.1))
        self.assertIsNone(query.due(1.0))
        second = query.due(1.3)
        self.assertNotEqual(second.payload, first.payload)
        self.assertTrue(query.accept(report(token=second.payload), 1.4))
        self.assertTrue(query.reply["ok"])

    def test_the_client_outwaits_the_relays_worst_case(self):
        """Finding 7: 1 s per attempt (the bridge answers in one pass); the
        worst case is the interval, three attempts and two retry waits, and
        the report waits longer than that."""
        from cortex_bridge import support_report
        self.assertEqual(snap.REPLY_TIMEOUT_S, 1.0)
        self.assertAlmostEqual(snap.WORST_CASE_S, 1.0 + 3 * 1.0 + 2 * 1.1)
        self.assertGreater(support_report.SNAPSHOT_TIMEOUT, snap.WORST_CASE_S)
        # Walk the slowest path: busy, busy, then silence.
        query = snap.SnapshotQuery(0.0, not_before=snap.MIN_INTERVAL_S,
                                   token_source=self.tokens())
        now = 0.0
        while not query.finished:
            request = query.due(now)
            if request is not None and query.attempts < snap.MAX_ATTEMPTS:
                query.accept(report(4, token=request.payload),
                             now + snap.REPLY_TIMEOUT_S - 0.01)
                now += snap.REPLY_TIMEOUT_S - 0.01
            now = round(now + 0.01, 2)
        self.assertEqual(query.reply["error"], "timeout")
        self.assertLessEqual(now, snap.WORST_CASE_S + 0.02)

    def test_silence_is_a_timeout_and_the_interval_is_kept(self):
        query = snap.SnapshotQuery(5.0, not_before=5.6,
                                   token_source=self.tokens())
        self.assertIsNone(query.due(5.0))
        self.assertIsNotNone(query.due(5.6))
        self.assertIsNone(query.due(6.5))
        self.assertIsNone(query.due(6.7))
        self.assertEqual(query.reply["error"], "timeout")

    def test_a_malformed_body_is_reported(self):
        query = snap.SnapshotQuery(0.0, token_source=self.tokens())
        request = query.due(0.0)
        query.accept(report(payload_body=b"\x02\x00", token=request.payload),
                     0.1)
        self.assertEqual(query.reply["error"], "malformed")


class ReadingTests(unittest.TestCase):
    def reading(self, *sections, **fixed):
        return snap.classify(snap.decode_body(body(*sections, **fixed)))

    def test_a_silent_line(self):
        reading = self.reading()
        self.assertEqual(reading["class"], "silent-line")
        self.assertIn("nothing comes back", reading["summary"])

    def test_bytes_that_never_decode(self):
        reading = self.reading(line=encode_line(
            bytes=4096, framing_errors=310, skipped_bytes=4000,
            first_rx_ms=3300, last_rx_ms=81000))
        self.assertEqual(reading["class"], "garbled-line")
        self.assertIn("orientation", reading["action"])

    def test_an_ams_left_without_an_index_while_another_runs(self):
        # voron350, 2026-11-08: an AMS 1 runs, an AMS HT plugged in after
        # 12.8 h announces 374 times and is never offered an index.
        stuck = dict(announces=374, first_announce_ms=45926933,
                     last_announce_ms=46153145, last_announce_marker=0xFE,
                     last_announce_tag_family=3, admitted=1, candidates=1,
                     last_announce_tag_suffix=b"N460")
        system = encode_system(uptime_ms=46153700)
        line = encode_line(bytes=22524383, messages=397270,
                           last_answer_ms=46153719)
        reading = self.reading(line=line, system=system,
                               discovery=encode_discovery(**stuck))
        self.assertEqual(reading["class"], "no-index-offered")
        self.assertIn("AMS HT, tag suffix N460", reading["summary"])
        self.assertIn("36 minutes", reading["action"])
        self.assertIn("FIRMWARE_RESTART", reading["action"])
        offered = dict(stuck, offers=12, last_offer_index=0x80,
                       last_offer_basis=1, last_offer_ms=46153000)
        reading = self.reading(line=line, system=system,
                               discovery=encode_discovery(**offered))
        self.assertEqual(reading["class"], "index-not-taken")
        self.assertIn("index 128 12 times", reading["summary"])
        # Announces that stopped long ago (the AMS was admitted) are history.
        quiet = dict(stuck, last_announce_ms=46100000)
        reading = self.reading(line=line, system=system,
                               discovery=encode_discovery(**quiet))
        self.assertNotIn(reading["class"],
                         ("no-index-offered", "index-not-taken"))
        # A few announces are a unit being admitted right now.
        brief = dict(stuck, announces=4)
        reading = self.reading(line=line, system=system,
                               discovery=encode_discovery(**brief))
        self.assertNotIn(reading["class"],
                         ("no-index-offered", "index-not-taken"))

    def test_an_ams_that_announces_and_is_refused(self):
        reading = self.reading(
            encode_candidate(), encode_refusal(),
            line=encode_line(bytes=900, messages=40, last_answer_ms=79000),
            discovery=encode_discovery(announces=6, refused=(0, 3, 0, 0, 0),
                                       candidates=1))
        self.assertEqual((reading["class"], reading["reason"]),
                         ("refused", "index_mismatch"))
        self.assertIn("index", reading["summary"])

    def test_an_ams_that_never_finishes_identification(self):
        reading = self.reading(
            encode_candidate(link=1, refusal=0, waiting=2),
            line=encode_line(bytes=900, messages=40),
            discovery=encode_discovery(announces=2, candidates=1))
        self.assertEqual((reading["class"], reading["waiting"]),
                         ("not-admitted", "identity"))

    def test_an_admitted_ams_klipper_never_uses(self):
        reading = self.reading(
            encode_unit(host=0x01),
            line=encode_line(bytes=9000, messages=700),
            discovery=encode_discovery(admitted=1, candidates=1))
        self.assertEqual(reading["class"], "unused-by-klipper")
        self.assertIn("/dev/oams/0a1b2c3d4e5f", reading["action"])

    def test_the_bridge_kept_off_the_line_comes_first(self):
        self.assertEqual(self.reading(system=encode_system(
            genuine_status=3))["class"], "bridge-off-line")
        self.assertEqual(self.reading(system=encode_system(
            role=0x03))["class"], "other-controller")

    def test_a_healthy_bridge(self):
        reading = self.reading(
            encode_unit(host=0x07),
            line=encode_line(bytes=9000, messages=700),
            discovery=encode_discovery(admitted=1, candidates=1))
        self.assertEqual(reading["class"], "none")


class FeedFailureTests(unittest.TestCase):
    """Section 0x12: the odometer around a unit's failed load or
    calibration."""

    def decode(self, *rows, unit_kind=0):
        return snap.decode_body(body(
            encode_unit(last_failure_kind=unit_kind, last_failure_outcome=3,
                        host=0x07), *rows))

    def test_rows_decode_signed_and_null(self):
        decoded = self.decode(
            encode_feed_failure(),
            encode_feed_failure(slot=1, index=1, odo_start_mm=-25,
                                odo_low_mm=-40, odo_at_mm=-31,
                                since_progress_ms=0xFFFFFFFF, feed_ms=0,
                                odo_flags=0x05, ams_state=99))
        first, second = decoded["feed_failures"]
        self.assertEqual(first, {
            "slot": 0, "index": 0, "odo_start_mm": 1200, "odo_low_mm": 600,
            "odo_at_mm": 601, "since_progress_ms": 4000, "feed_ms": 6193,
            "odo_flags": {"start": True, "base": True, "rewind_seen": True,
                          "rewinding": True, "at_valid": True, "rule": True,
                          "retract": False},
            "ams_state": "moving"})
        self.assertEqual((second["odo_start_mm"], second["odo_low_mm"],
                          second["odo_at_mm"]), (-25, -40, -31))
        self.assertIsNone(second["since_progress_ms"])
        self.assertEqual(second["feed_ms"], 0, "a duration, not an instant")
        self.assertEqual(second["odo_flags"]["rewind_seen"], True)
        self.assertEqual(second["odo_flags"]["base"], False)
        self.assertEqual(second["ams_state"], "unknown_99")
        self.assertEqual(list(decoded)[-4:],
                         ["candidates", "feed_failures", "dryers",
                          "refusals"])

    def lines(self, *rows, unit_kind=0):
        return snap.classify(self.decode(*rows, unit_kind=unit_kind))[
            "feed_failures"]

    def test_the_rewind_profile_in_one_sentence(self):
        self.assertEqual(self.lines(encode_feed_failure()), [
            "Last load of AMS 0a1b2c3d4e5f failed: the AMS rewound from "
            "1200 mm to 601 mm and then stopped moving for 4 s, 6.2 s into "
            "the feed; it was still rewinding. AMS state: moving."])

    def test_the_sentence_says_only_what_the_fields_say(self):
        cases = (
            # A lowest reading 10 mm or more from the last is named.
            (encode_feed_failure(odo_low_mm=450), 0,
             "Last load of AMS 0a1b2c3d4e5f failed: the AMS rewound from "
             "1200 mm to 601 mm (lowest 450 mm) and then stopped moving for "
             "4 s, 6.2 s into the feed; it was still rewinding. AMS state: "
             "moving."),
            # No rewind: the readings as they are.
            (encode_feed_failure(odo_start_mm=0, odo_low_mm=0,
                                 odo_at_mm=850, odo_flags=0x13,
                                 since_progress_ms=4500, feed_ms=30000,
                                 ams_state=1), 9,
             "Last calibration of AMS 0a1b2c3d4e5f failed: the odometer read "
             "0 mm at the start and 850 mm at the failure and then stopped "
             "moving for 4.5 s, 30 s into the feed. AMS state: idle."),
            # Nothing known but the feed time.
            (encode_feed_failure(slot=3, index=2, odo_flags=0x00,
                                 since_progress_ms=0xFFFFFFFF, feed_ms=1500,
                                 ams_state=0), 0,
             "Last load or calibration of the AMS at index 2 failed: the "
             "odometer gave no reading at the failure, 1.5 s into the feed. "
             "AMS state: unknown."),
        )
        for row, kind, expected in cases:
            self.assertEqual(self.lines(row, unit_kind=kind), [expected])

    def test_no_row_no_sentence(self):
        self.assertEqual(snap.classify(SILENT_LINE)["feed_failures"], [])

    def test_the_golden_vector_reads_as_its_profile(self):
        decoded = VECTORS["ams1_load_stalled_rewinding"]["decoded"]
        self.assertEqual(snap.classify(decoded)["feed_failures"], [
            "Last load of AMS df5d1d75218e failed: the AMS rewound from "
            "1200 mm to 601 mm and then stopped moving for 4 s, 6.2 s into "
            "the feed; it was still rewinding. AMS state: moving."])

    def test_a_stalled_pull_back_reads_as_an_unload(self):
        # Bridge 14: the row of an unload whose filament did not move back.
        decoded = VECTORS["ams1_retract_stalled"]["decoded"]
        uid = decoded["units"][0]["uid"]
        self.assertEqual(snap.classify(decoded)["feed_failures"], [
            "Last unload of AMS %s failed: the odometer stood at 2000 mm "
            "while the AMS pulled the filament back, and then it stopped "
            "moving for 4 s, 6.5 s into the pull back. AMS state: moving."
            % uid])

    def test_only_its_fields_pass_the_allowlist(self):
        decoded = self.decode(encode_feed_failure())
        row = dict(decoded["feed_failures"][0], raw_line="0102030405",
                   odo_flags=dict(decoded["feed_failures"][0]["odo_flags"],
                                  extra=True))
        kept = snap.allowlisted(dict(decoded, feed_failures=[row]))
        self.assertNotIn("raw_line", kept["feed_failures"][0])
        self.assertNotIn("extra", kept["feed_failures"][0]["odo_flags"])
        self.assertEqual(kept["feed_failures"][0]["odo_at_mm"], 601)


class AllowlistTests(unittest.TestCase):
    def test_only_decoded_fields_of_decoded_types_pass(self):
        decoded = snap.decode_body(body(encode_unit(), encode_candidate()))
        tampered = dict(decoded)
        tampered["serial"] = "19C06A4B2700123"
        tampered["system"] = dict(decoded["system"], certificate="AAAA",
                                  app_version="9; rm -rf")
        tampered["units"] = [dict(decoded["units"][0],
                                  tag="0102030405060708",
                                  host=dict(decoded["units"][0]["host"],
                                            extra=True))]
        kept = snap.allowlisted(tampered)
        self.assertNotIn("serial", kept)
        self.assertNotIn("certificate", kept["system"])
        self.assertNotIn("app_version", kept["system"])
        self.assertNotIn("tag", kept["units"][0])
        self.assertNotIn("extra", kept["units"][0]["host"])
        self.assertEqual(kept["units"][0]["uid"], "0a1b2c3d4e5f")
        self.assertEqual(kept["units"][0]["ams_version"], [0, 0, 6, 49])

    def test_an_untouched_snapshot_passes_whole(self):
        decoded = snap.decode_body(body(encode_unit(), encode_candidate(),
                                        encode_refusal()))
        self.assertEqual(snap.allowlisted(decoded), decoded)


if __name__ == "__main__":
    unittest.main()


class DryerRowTests(unittest.TestCase):
    """Section 0x13: the dryer row (bridge firmware 11: the stock printer's
    exhaust schedule; firmware 10: its moisture loop)."""

    CASE = VECTORS["ams2_dryer_cooldown"]

    def test_the_row_decodes_and_reads(self):
        decoded = snap.decode_body(bytes.fromhex(self.CASE["body_hex"]))
        row = decoded["dryers"][0]
        self.assertEqual((row["vent_state"], row["pulses"],
                          row["reply_length"]), ("cooldown", 1, 55))
        self.assertIs(row["vent_flags"]["open_at_pulse"], True)
        self.assertEqual(len(row["reply"]), 110)
        self.assertEqual(snap.classify(decoded)["dryers"], [
            "Dryer of the AMS at index 0: in the sealed cooldown, 1 exhaust "
            "request this dry, vents closed (the AMS opened them on a "
            "request); the last start was confirmed; vents last closed at "
            "the end of the dry; the last dry ended with the cool-down "
            "request."])

    def test_a_refused_start_says_why(self):
        decoded = snap.decode_body(bytes.fromhex(
            VECTORS["ams2_dryer_refused_hub"]["body_hex"]))
        row = decoded["dryers"][0]
        self.assertEqual((row["start"], row["start_lanes"], row["start_bay"]),
                         ("refused_hub", 0x75, 2))
        self.assertIn("refused: filament is past the feeder, in the hub "
                      "(bay 2)", snap.classify(decoded)["dryers"][0])

    def test_bridge_20_says_whether_the_start_turned_the_spools(self):
        # Bridge firmware 20: vent_flags bit 7 is "the last start asked for
        # rotation" (its cooldown vector's flag byte 0x21 became 0xa1), and
        # start 7 is a rotating start refused with filament out.
        flags = snap._bitfield(snap.VENT_FLAG_BITS)(0xa1)
        self.assertIs(flags["rotate"], True)
        self.assertIs(flags["enabled"], True)
        self.assertIs(snap._bitfield(snap.VENT_FLAG_BITS)(0x21)["rotate"],
                      False)
        self.assertEqual(snap.START[7], "refused_rotate")
        row = {"index": 0, "vent_state": "idle", "vent_flags": flags,
               "start": "confirmed"}
        self.assertIn("the last start was confirmed, turning the spools",
                      snap.dryer_lines({"dryers": [row]})[0])
        row = dict(row, start="refused_rotate")
        line = snap.dryer_lines({"dryers": [row]})[0]
        self.assertIn("the last start was refused: the spools cannot turn "
                      "while filament is out", line)
        self.assertNotIn("turning the spools", line)

    def test_a_firmware_11_row_of_73_bytes_still_decodes(self):
        body = bytearray(bytes.fromhex(self.CASE["body_hex"]))
        at = bytes(body).index(bytes((0x13, 77)))
        body[at + 1] = 73
        del body[at + 2 + 73:at + 2 + 77]
        row = snap.decode_body(bytes(body))["dryers"][0]
        self.assertEqual(row["vent_state"], "cooldown")
        self.assertNotIn("start", row)

    def test_a_firmware_10_row_keeps_its_own_names(self):
        body = bytearray(bytes.fromhex(self.CASE["body_hex"]))
        system = snap.decode_body(bytes(body))["system"]
        # app_version is the system section's first field (u32, big-endian).
        at = bytes(body).index(bytes((0x01, 25))) + 2
        body[at:at + 4] = (10).to_bytes(4, "big")
        decoded = snap.decode_body(bytes(body))
        self.assertNotEqual(system["app_version"], 10)
        row = decoded["dryers"][0]
        self.assertIn("openings", row)
        self.assertIn("ah_mgm3", row)
        self.assertNotIn("pulses", row)
        kept = snap.allowlisted(decoded)["dryers"][0]
        self.assertEqual(kept["openings"], row["openings"])
        self.assertIn("opening", snap.classify(decoded)["dryers"][0])

    def test_a_report_keeps_the_reply_and_nothing_else_raw(self):
        decoded = snap.decode_body(bytes.fromhex(self.CASE["body_hex"]))
        row = dict(decoded["dryers"][0])
        kept = snap.allowlisted(dict(decoded, dryers=[row]))["dryers"][0]
        self.assertEqual(kept["reply"], row["reply"])
        self.assertEqual(kept["vent_flags"], row["vent_flags"])
        # Anything but 110 lowercase hex digits is dropped.
        for bad in ("zz" * 55, row["reply"][:108], row["reply"].upper(),
                    b"\x00" * 55):
            row["reply"] = bad
            kept = snap.allowlisted(dict(decoded, dryers=[row]))
            self.assertNotIn("reply", kept["dryers"][0], bad)

    def test_a_bridge_9_snapshot_has_no_dryers(self):
        decoded = snap.decode_body(SILENT_LINE_BODY)
        self.assertEqual(decoded["dryers"], [])
        self.assertEqual(snap.classify(decoded)["dryers"], [])
