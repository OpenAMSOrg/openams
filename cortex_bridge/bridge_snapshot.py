# SPDX-License-Identifier: MIT
"""The bridge's support snapshot: why no AMS comes online, in counters.

When no AMS is online, Klipper has nothing to log. Production bridge firmware
9 and later can say what it saw instead: whether the AMS data line is silent,
whether bytes arrive but never decode, whether an AMS announced itself and
was refused (and why), and whether an admitted AMS was ever used by Klipper.

On the runtime mux stream the relay sends a SUPPORT_REQUEST (type 8) with a
4-byte token; the bridge answers a SUPPORT_REPORT (type 9): the token, a
status byte (0 ok, 1 rate-limited, 4 busy) and, when the status is 0, a body:
``format(1) = 1`` then sections ``id(1) length(1) value(length)`` in
ascending id order. Integers are big-endian. Unknown sections are skipped and
bytes after the known fields of a section are ignored; a section shorter
than its known fields makes the snapshot malformed.

Firmware 7 and 8 do not know type 8: they treat it as a stream fault and drop
every AMS for a moment, which Klipper sees as units going offline. So
``gate`` allows a request only to a bridge whose USB manufacturer is
``SiForge`` and whose identity report gave version 9 or later.

The snapshot holds counts, enums, reason codes, boot-relative instants,
versions and, for an admitted AMS, its roster UID: nothing that identifies a
unit beyond what ``status`` shows. ``allowlisted`` keeps only the fields
decoded here, so a field a later firmware adds is never passed on unread.
"""

from __future__ import annotations

import base64
import os
import re

from cortex_bridge.cortexd import (
    Message,
    MessageType,
    SUPPORT_TOKEN_SIZE,
)

SNAPSHOT_METHOD = "support_snapshot"     # the relay's control-socket method
MINIMUM_FIRMWARE = 9
FORMAT_1 = 1
STATUS_OK = 0
STATUS_RATE_LIMITED = 1
STATUS_BUSY = 4
RETRY_STATUSES = {STATUS_RATE_LIMITED: "rate-limited", STATUS_BUSY: "busy"}

# The bridge builds its answer in one pass of its main loop and sends it
# before its next roster or unit message, so 1 s per attempt is ample.
REPLY_TIMEOUT_S = 1.0
RETRY_DELAY_S = 1.1         # after rate-limited or busy
MAX_ATTEMPTS = 3
MIN_INTERVAL_S = 1.0        # never ask more often than this
# The longest the relay takes to answer the control socket: waiting out the
# interval, then every attempt timing out but the retries in between.
WORST_CASE_S = MIN_INTERVAL_S + MAX_ATTEMPTS * REPLY_TIMEOUT_S + \
    (MAX_ATTEMPTS - 1) * RETRY_DELAY_S

NEVER = 0
AGE_NEVER = 0xFFFFFFFF
NO_INDEX = 0xFF


class SnapshotError(ValueError):
    """The bytes are not a snapshot this module can read."""


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

FAMILY = ("unknown", "ams1", "ams2pro", "ams_ht")
LINK = ("absent", "discovering", "online", "silent", "refused")
REFUSAL = ("none", "unsupported_family", "index_mismatch", "over_capacity",
           "duplicate_identity", "tag_model_mismatch")
WAITING = ("none", "model", "identity", "status", "evidence", "uid")
MODEL_CLASS = ("none", "unrecognized", "unsupported", "supported")
OFFER_BASIS = ("none", "known_unit", "tag_suffix", "learned_model",
               "marker_ams", "marker_ht", "no_free_index", "verbatim")
AMS_STATE = ("unknown", "idle", "waiting", "moving", "tag_read", "in_use",
             "drying", "initializing")
OP_KIND = ("load", "unload", "cancel", "follower_on", "follower_off",
           "dryer_start", "dryer_stop", "clear_fault", "tag_read",
           "calibrate")
STAGE = ("none", "feed", "retract", "purge", "cut", "calibrate")
OUTCOME = ("success", "failed", "cancelled", "timeout", "aborted")
EVIDENCE = ("hypothesis", "observed", "qualified")
SEVERITY = ("none", "info", "pause", "stop")
LATCH = ("none", "emergency", "watchdog")
MARKERS = {0xFF: "ams", 0xFE: "ams_ht"}
# Family ids and announce markers in a sentence.
FAMILY_TEXT = {"ams1": "AMS 1", "ams2pro": "AMS 2 Pro", "ams_ht": "AMS HT",
               "ams": "AMS 1 or AMS 2 Pro"}

IMAGE_BITS = ("tx", "motion", "dryer", "tray_info", "tag_read",
              "diagnostics", "production", "application")
ROLE_BITS = ("master", "foreign_master", "tx_fault", "awaiting")
POLICY_BITS = ("master", "motion", "dryer")
HOST_BITS = ("attached", "configured", "heard")
FLAG_BITS = ("model", "identity", "status", "announce", "conflict", "fresh",
             "synthetic")
# retract (bridge 14): the row is a pull back's (an unload, or a calibration's
# return), not a feed's.
ODO_BITS = ("start", "base", "rewind_seen", "rewinding", "at_valid", "rule",
            "retract")
# The dryer vent loop (bridge firmware 10, section 0x13).
# Bridge firmware 11 follows the stock printer's schedule (an exhaust request
# held about 9.5 s every 327 s); firmware 10 ran a moisture loop, and its rows
# keep their own names (DRYER_V10, chosen by system.app_version).
VENT_STATE = ("idle", "heating", "drying", "forcing", "cooldown")
VENT_STATE_V10 = ("idle", "heating", "closed", "open", "cooldown")
OPENED_BY = ("none", "plateau", "forced", "unit")
CLOSED_BY = ("none", "settled", "max_open", "cold", "end", "stopped",
             "disabled")
# Bit 7 (bridge firmware 20): the last start asked the AMS to turn its spools.
VENT_FLAG_BITS = ("enabled", "timed", "forcing", "cooling", "fast",
                  "open_at_pulse", "vents_open", "rotate")
ENDED_BITS = ("cool_down", "stop", "unit_off", "vents_opened", "capped")
DRYER_REPLY_SIZE = 55


def enum_name(table, value: int) -> str:
    return table[value] if 0 <= value < len(table) else "unknown_%d" % value


def _bits(names, value: int) -> dict:
    return {name: bool(value >> bit & 1) for bit, name in enumerate(names)}


# ---------------------------------------------------------------------------
# Field rules: each turns the field's integer (or bytes) into its JSON value
# ---------------------------------------------------------------------------

def _uint(value):
    return value


def _instant(value):            # boot-clock milliseconds; 0 is never
    return None if value == NEVER else value


def _age(value):                # an age; all ones is never
    return None if value == AGE_NEVER else value


def _index(value):              # 255 is none
    return None if value == NO_INDEX else value


def _enum(table, none_value=None):
    def rule(value):
        if none_value is not None and value == none_value:
            return None
        return enum_name(table, value)
    return rule


def _tag_family(value: int) -> str:
    # 0: a suffix no rule matches; 255: no tag seen; else the family.
    return "none" if value == 0xFF else enum_name(FAMILY, value)


def _marker(value: int):
    if value == 0:
        return None
    return MARKERS.get(value, "unknown_%d" % value)


def _policy(value: int) -> dict:
    return {"requested": _bits(POLICY_BITS, value & 0x07),
            "effective": _bits(POLICY_BITS, value >> 4 & 0x07)}


def _host_flags(value: int) -> dict:
    flags = _bits(HOST_BITS, value & 0x07)
    flags["latch"] = enum_name(LATCH, value >> 4 & 0x03)
    return flags


def _operation(value: int) -> dict:
    kind = value & 0x0F
    return {"active": bool(value & 0x80),
            "kind": None if kind == 0x0F else enum_name(OP_KIND, kind)}


def _bitfield(names):
    return lambda value: _bits(names, value)


def _signed(raw: bytes) -> int:
    """A signed (two's complement) reading: below park is negative."""
    return int.from_bytes(raw, "big", signed=True)


def _uid(raw: bytes) -> str:
    return raw.hex()


def _reply(raw: bytes) -> str:
    """The unit's latest dryer status answer, 110 hex digits (zero past its
    length). The one block of received bytes a report carries: status bytes
    with no identifiers, allowed by the owner's decision of 2026-11-08."""
    return raw.hex()


def _version(raw: bytes) -> list:
    return list(raw)


def _suffix(raw: bytes):
    """The AMS tag's 4-character model-class suffix (N573), allowed in a
    report by the owner's decision of 2026-11-08."""
    if raw == bytes(len(raw)):
        return None
    if all(0x20 <= byte <= 0x7E for byte in raw):
        return raw.decode("ascii")
    return "0x" + raw.hex()


# A field: (name, width, rule, kind). ``kind`` is what ``allowlisted``
# accepts for it: int, int? (or None), enum, enum? (or None), bits, uid,
# version, suffix. A rule marked BYTES gets the raw bytes.
BYTES = "bytes"


def _f(name, width, rule=_uint, kind="int", raw=False):
    return (name, width, (BYTES, rule) if raw else rule, kind)


SYSTEM = (
    _f("app_version", 4), _f("uptime_ms", 4), _f("reset_flags", 1),
    _f("boot_status", 1), _f("genuine_status", 1),
    _f("image", 1, _bitfield(IMAGE_BITS), "bits"),
    _f("min_evidence", 1, _enum(EVIDENCE), "enum"),
    _f("policy", 1, _policy, "bits"),
    _f("role", 1, _bitfield(ROLE_BITS), "bits"),
    _f("master_acquisitions", 2), _f("master_since_ms", 4, _instant, "int?"),
    _f("max_pass_gap_us", 4),
)
LINE = tuple(_f(name, 4) for name in (
    "bytes", "parity_errors", "framing_errors", "noise_errors", "overruns",
    "messages", "check_errors", "malformed", "skipped_bytes")) + tuple(
    _f(name, 4, _instant, "int?") for name in (
        "first_rx_ms", "last_rx_ms", "last_answer_ms"))
MASTER = tuple(_f(name, 4) for name in (
    "requests", "answers", "unanswered", "send_failures", "foreign_requests",
    "unmatched_answers", "sent_messages"))
REFUSED_KEYS = ("unsupported_family", "index_mismatch", "over_capacity",
                "duplicate_identity", "tag_model_mismatch")
DISCOVERY = (
    _f("announces", 4), _f("first_announce_ms", 4, _instant, "int?"),
    _f("last_announce_ms", 4, _instant, "int?"),
    _f("last_announce_marker", 1, _marker, "enum?"),
    _f("last_announce_tag_family", 1, _tag_family, "enum"),
    _f("offers", 4), _f("offer_giveups", 2),
    _f("last_offer_index", 1, _index, "int?"),
    _f("last_offer_basis", 1, _enum(OFFER_BASIS), "enum"),
    _f("last_offer_ms", 4, _instant, "int?"), _f("identity_conflicts", 2),
    _f("candidates_dropped", 2), _f("index_polls", 4),
) + tuple(_f("refused_" + key, 2) for key in REFUSED_KEYS) + (
    _f("admitted", 1), _f("candidates", 1), _f("omitted", 1),
    _f("last_announce_tag_suffix", 4, _suffix, "suffix", raw=True),
)
HOST = (
    _f("usb_sessions", 4), _f("protocol_faults", 2), _f("recoveries", 2),
    _f("host_messages", 4), _f("host_dropped", 2), _f("host_stale", 2),
    _f("update_refused", 2), _f("attach_refused", 2),
    _f("roster_rejected", 2),
)
UNIT = (
    _f("slot", 1), _f("uid", 6, _uid, "uid", raw=True), _f("index", 1),
    _f("link", 1, _enum(LINK), "enum"), _f("family", 1, _enum(FAMILY), "enum"),
    _f("ams_version", 4, _version, "version", raw=True),
    _f("answers", 2), _f("timeouts", 2),
    _f("last_answer_ms", 4, _instant, "int?"), _f("ams_error", 1),
    _f("motion_error", 1), _f("ams_state", 1, _enum(AMS_STATE), "enum"),
    _f("spools_present", 1), _f("host", 1, _host_flags, "bits"),
    _f("configurations", 2), _f("host_quiet_ms", 4, _age, "int?"),
    _f("operation", 1, _operation, "bits"),
    _f("stage", 1, _enum(STAGE), "enum"),
    _f("fault_severity", 1, _enum(SEVERITY), "enum"),
    _f("fault_code", 2), _f("fault_bay", 1, _index, "int?"),
    _f("loaded_bay", 1, _index, "int?"), _f("path_mm", 2),
    _f("failures", 2),
    _f("last_failure_kind", 1, _enum(OP_KIND, NO_INDEX), "enum?"),
    _f("last_failure_outcome", 1, _enum(OUTCOME, NO_INDEX), "enum?"),
    _f("last_failure_detail", 2), _f("last_failure_ms", 4, _instant, "int?"),
    _f("tag_suffix", 4, _suffix, "suffix", raw=True),
)
CANDIDATE = (
    _f("index", 1), _f("link", 1, _enum(LINK), "enum"),
    _f("refusal", 1, _enum(REFUSAL), "enum"),
    _f("waiting", 1, _enum(WAITING), "enum"),
    _f("family", 1, _enum(FAMILY), "enum"),
    _f("model_class", 1, _enum(MODEL_CLASS), "enum"),
    _f("tag_family", 1, _tag_family, "enum"),
    _f("flags", 1, _bitfield(FLAG_BITS), "bits"),
    _f("ams_version", 4, _version, "version", raw=True),
    _f("answers", 2), _f("timeouts", 2),
    _f("first_seen_ms", 4, _instant, "int?"),
    _f("last_answer_ms", 4, _instant, "int?"), _f("ams_error", 1),
    _f("tag_suffix", 4, _suffix, "suffix", raw=True),
)
# One row per admitted unit whose latest failure was a load or calibration:
# the odometer around it, in millimetres from park.
FEED_FAILURE = (
    _f("slot", 1), _f("index", 1),
    _f("odo_start_mm", 4, _signed, raw=True),
    _f("odo_low_mm", 4, _signed, raw=True),
    _f("odo_at_mm", 4, _signed, raw=True),
    _f("since_progress_ms", 4, _age, "int?"),
    _f("feed_ms", 4),
    _f("odo_flags", 1, _bitfield(ODO_BITS), "bits"),
    _f("ams_state", 1, _enum(AMS_STATE), "enum"),
)
# Bridge firmware 15: the AMS's motor readings during a reverse follower or
# an unload (0x04 reply offsets 22-33), with the command streamed then, the
# odometer and the buffer; the latest few per unit, as many as fit. On the AMS
# 2 Pro, 22-27 follow the hub motor and 28/30/32 are 0 unless the per-slot
# first stage runs.
MOTOR_SAMPLE = (
    _f("slot", 1), _f("age_ms", 4),
    _f("primitive", 1), _f("reference", 1),
    _f("odo_mm", 2, _signed, raw=True), _f("buffer", 2, _signed, raw=True),
    _f("m22", 2, _signed, raw=True), _f("m24", 2, _signed, raw=True),
    _f("m26", 2, _signed, raw=True), _f("m28", 2, _signed, raw=True),
    _f("m30", 2, _signed, raw=True), _f("m32", 2, _signed, raw=True),
)
# One row per unit asked to dry, or drying, since it was bound.
DRYER_V11 = (
    _f("slot", 1), _f("index", 1),
    _f("vent_state", 1, _enum(VENT_STATE), "enum"),
    _f("opened_by", 1, _enum(OPENED_BY), "enum"),
    _f("closed_by", 1, _enum(CLOSED_BY), "enum"),
    _f("pulses", 2),
    _f("vent_flags", 1, _bitfield(VENT_FLAG_BITS), "bits"),
    _f("ended", 1, _bitfield(ENDED_BITS), "bits"),
    _f("last_pulse_ms", 4, _instant, "int?"),
    _f("reply_ms", 4, _instant, "int?"),
    _f("reply_length", 1),
    _f("reply", DRYER_REPLY_SIZE, _reply, "reply", raw=True),
)
# Bridge firmware 12 appends how the last start went: 77 bytes.
# Bridge firmware 20 adds 7: a rotating start refused with filament out.
START = ("none", "confirmed", "sent", "refused_busy", "refused_hub",
         "not_confirmed", "failed", "refused_rotate")
DRYER = DRYER_V11 + (
    _f("start", 1, _enum(START), "enum"),
    _f("start_state", 1),
    _f("start_lanes", 1),
    _f("start_bay", 1, _index, "int?"),
)
# The same 73 bytes from bridge firmware 10 (its moisture loop).
DRYER_V10 = (
    _f("slot", 1), _f("index", 1),
    _f("vent_state", 1, _enum(VENT_STATE_V10), "enum"),
    _f("opened_by", 1, _enum(OPENED_BY), "enum"),
    _f("closed_by", 1, _enum(CLOSED_BY), "enum"),
    _f("openings", 2),
    _f("vent_flags", 1, _bitfield(VENT_FLAG_BITS[:5]), "bits"),
    _f("ended", 1, _bitfield(ENDED_BITS), "bits"),
    _f("ah_mgm3", 4), _f("reply_ms", 4, _instant, "int?"),
    _f("reply_length", 1),
    _f("reply", DRYER_REPLY_SIZE, _reply, "reply", raw=True),
)
REFUSAL_ROW = (
    _f("at_ms", 4, _instant, "int?"), _f("index", 1),
    _f("reason", 1, _enum(REFUSAL), "enum"),
    _f("family", 1, _enum(FAMILY), "enum"),
    _f("model_class", 1, _enum(MODEL_CLASS), "enum"),
    _f("tag_family", 1, _tag_family, "enum"),
    _f("tag_suffix", 4, _suffix, "suffix", raw=True),
)

# id: (name, layout, one row per section?)
SECTIONS = {
    0x01: ("system", SYSTEM, False),
    0x02: ("line", LINE, False),
    0x03: ("master", MASTER, False),
    0x04: ("discovery", DISCOVERY, False),
    0x05: ("host", HOST, False),
    0x10: ("units", UNIT, True),
    0x11: ("candidates", CANDIDATE, True),
    0x12: ("feed_failures", FEED_FAILURE, True),
    0x13: ("dryers", DRYER, True),
    0x14: ("motor_samples", MOTOR_SAMPLE, True),
    0x20: ("refusals", REFUSAL_ROW, True),
}


def layout_size(layout) -> int:
    return sum(field[1] for field in layout)


def _decode_fields(layout, value: bytes, name: str) -> dict:
    if len(value) < layout_size(layout):
        raise SnapshotError("the %s section is %d bytes, shorter than the %d "
                            "this host reads" % (name, len(value),
                                                 layout_size(layout)))
    out = {}
    offset = 0
    for field, width, rule, _ in layout:
        raw = value[offset:offset + width]
        offset += width
        if isinstance(rule, tuple):
            out[field] = rule[1](bytes(raw))
        else:
            out[field] = rule(int.from_bytes(raw, "big"))
    return out


def decode_body(body: bytes) -> dict:
    """The decoded snapshot. Raises SnapshotError for a body this host
    cannot read (another format, a section cut short, ids out of order)."""
    body = bytes(body)
    if not body:
        raise SnapshotError("the snapshot is empty")
    if body[0] != FORMAT_1:
        raise SnapshotError("snapshot format %d is not one this host reads"
                            % body[0])
    out = {"format": FORMAT_1}
    for name, _, table in SECTIONS.values():
        out[name] = [] if table else None
    offset = 1
    last_id = 0
    while offset < len(body):
        if offset + 2 > len(body):
            raise SnapshotError("a section header is cut short")
        section_id, length = body[offset], body[offset + 1]
        value = body[offset + 2:offset + 2 + length]
        if len(value) != length:
            raise SnapshotError("section 0x%02x is cut short" % section_id)
        offset += 2 + length
        if section_id < last_id:
            raise SnapshotError("sections are out of order")
        known = SECTIONS.get(section_id)
        if known is None:
            last_id = section_id
            continue                              # a later firmware's
        name, layout, table = known
        if section_id == 0x13:
            if (out.get("system") or {}).get("app_version") == 10:
                layout = DRYER_V10
            elif len(value) < layout_size(DRYER):
                layout = DRYER_V11                # firmware 11: 73 bytes
        if not table and section_id == last_id:
            raise SnapshotError("the %s section appears twice" % name)
        last_id = section_id
        fields = _decode_fields(layout, value, name)
        if table:
            out[name].append(fields)
        else:
            out[name] = fields
    # Present only when the bridge sent some (bridge firmware 15), as in the
    # firmware's reference decoder.
    if not out.get("motor_samples"):
        out.pop("motor_samples", None)
    return out


def parse_report(payload: bytes) -> dict:
    """``token``, ``status`` (a code) and ``body`` (bytes, only for 0)."""
    payload = bytes(payload)
    if len(payload) < SUPPORT_TOKEN_SIZE + 1:
        raise SnapshotError("the report is too short")
    status = payload[SUPPORT_TOKEN_SIZE]
    body = payload[SUPPORT_TOKEN_SIZE + 1:]
    if status == STATUS_OK and not body:
        raise SnapshotError("an ok report without a snapshot")
    return {"token": payload[:SUPPORT_TOKEN_SIZE], "status": status,
            "body": body if status == STATUS_OK else b""}


def make_request(token: bytes) -> Message:
    if len(token) != SUPPORT_TOKEN_SIZE:
        raise ValueError("a support token is %d bytes" % SUPPORT_TOKEN_SIZE)
    return Message(MessageType.SUPPORT_REQUEST, bytes(token))


# ---------------------------------------------------------------------------
# When to ask
# ---------------------------------------------------------------------------

def gate(bridge, firmware) -> tuple:
    """(allowed, error, message). Only a production bridge (USB manufacturer
    SiForge) whose identity report gave version 9 or later is asked; any
    other bridge would drop its AMS units on the request."""
    bridge = bridge or {}
    firmware = firmware or {}
    if bridge.get("manufacturer") != "SiForge":
        return (False, "unsupported-firmware",
                "this bridge runs lab or unknown firmware, which has no "
                "support snapshot")
    if firmware.get("state") != "known" or \
            not isinstance(firmware.get("version"), int):
        return (False, "firmware-unknown",
                "the bridge has not reported its firmware version yet, so it "
                "is not asked")
    if firmware["version"] < MINIMUM_FIRMWARE:
        return (False, "unsupported-firmware",
                "bridge firmware %d has no support snapshot; version %d or "
                "later has" % (firmware["version"], MINIMUM_FIRMWARE))
    return (True, "", "")


def failure(error: str, message: str, **extra) -> dict:
    reply = {"event": "support_snapshot", "ok": False, "error": error,
             "message": message}
    reply.update(extra)
    return reply


class SnapshotQuery:
    """One snapshot request, with a bounded retry on rate-limited and busy.

    ``due(now)`` returns a request ``Message`` to send, or None;
    ``accept(payload, now)`` takes each SUPPORT_REPORT payload. ``reply`` is
    the control-socket answer once ``finished``."""

    def __init__(self, now: float, *, not_before: float = 0.0,
                 token_source=os.urandom):
        self.token_source = token_source
        self.token = None
        self.sent_at = 0.0
        self.next_at = max(now, not_before)
        self.attempts = 0
        self.reply = None

    @property
    def finished(self) -> bool:
        return self.reply is not None

    def due(self, now: float):
        if self.finished:
            return None
        if self.token is not None:
            if now - self.sent_at < REPLY_TIMEOUT_S:
                return None
            self.token = None
            self.reply = failure("timeout", "the bridge did not answer the "
                                 "snapshot request within %.0f s"
                                 % REPLY_TIMEOUT_S)
            return None
        if now < self.next_at:
            return None
        self.attempts += 1
        self.token = bytes(self.token_source(SUPPORT_TOKEN_SIZE))
        self.sent_at = now
        return make_request(self.token)

    def accept(self, payload: bytes, now: float) -> bool:
        if self.finished or self.token is None or \
                bytes(payload[:SUPPORT_TOKEN_SIZE]) != self.token:
            return False                      # not the answer to our request
        self.token = None
        try:
            report = parse_report(payload)
            if report["status"] == STATUS_OK:
                snapshot = decode_body(report["body"])
                self.reply = {
                    "event": "support_snapshot", "ok": True,
                    "snapshot": snapshot,
                    "raw": base64.b64encode(report["body"]).decode("ascii")}
                return True
        except SnapshotError as exc:
            self.reply = failure("malformed", str(exc))
            return True
        status = report["status"]
        if status in RETRY_STATUSES and self.attempts < MAX_ATTEMPTS:
            self.next_at = now + RETRY_DELAY_S
            return False
        if status in RETRY_STATUSES:
            self.reply = failure(RETRY_STATUSES[status],
                                 "the bridge answered %s %d times" % (
                                     RETRY_STATUSES[status], self.attempts))
        else:
            self.reply = failure("refused", "the bridge answered status %d"
                                 % status)
        return True


# ---------------------------------------------------------------------------
# Reading it
# ---------------------------------------------------------------------------

REFUSAL_TEXT = {
    "unsupported_family": "its model is not one this bridge firmware "
                          "supports",
    "index_mismatch": "it holds an AMS index that does not fit its model, "
                      "often one kept from another printer; power the AMS "
                      "off and on",
    "over_capacity": "the bridge already serves four AMS units",
    "duplicate_identity": "the same AMS answers at two indexes; power the "
                          "AMS off and on",
    "tag_model_mismatch": "its tag and its model code name different AMS "
                          "models",
}
WAITING_TEXT = {
    "model": "it never said which model it is",
    "identity": "it never gave its identity",
    "status": "it never answered a status request",
    "evidence": "it has not yet shown enough to be admitted",
    "uid": "its identity gave no usable unit ID",
}


def _get(data, *path, default=0):
    for key in path:
        if not isinstance(data, dict):
            return default
        data = data.get(key)
    return default if data is None else data


def classify(snapshot) -> dict:
    """The first failure pattern the snapshot matches: ``class`` (an id),
    ``summary`` (one sentence) and ``action`` (what to do). Patterns are
    checked in order: the bridge kept off the line, another controller on
    it, a silent line, bytes that never decode, an AMS that announces but is
    refused or never finishes identification, and an AMS the bridge offers
    that Klipper never uses. ``feed_failures`` adds one sentence for each
    unit whose last load or calibration failed (``feed_failure_lines``)."""
    reading = _classify(snapshot)
    reading["feed_failures"] = feed_failure_lines(snapshot)
    reading["dryers"] = dryer_lines(snapshot)
    reading["motor_samples"] = motor_sample_lines(snapshot)
    return reading


PRIMITIVE_TEXT = {0x02: "stop", 0x03: "pull back or feed (03)",
                  0x07: "follower or hold (07)", 0x09: "hub servo (09)"}


def motor_sample_lines(snapshot) -> list:
    """One sentence per motor sample (bridge firmware 15): the command the
    bridge streamed, the odometer and buffer, and the two motor groups, so a
    bench report shows which command ran the first stage."""
    lines = []
    for row in (snapshot or {}).get("motor_samples") or []:
        if not isinstance(row, dict):
            continue
        first = (row.get("m28"), row.get("m30"), row.get("m32"))
        lines.append(
            "Motor sample, AMS slot %s, %s ms ago: %s, setpoint 0x%02x, "
            "odometer %s mm, buffer %+d; hub %s/%s/%s, first stage %s/%s/%s%s"
            % (row.get("slot"), row.get("age_ms"),
               PRIMITIVE_TEXT.get(row.get("primitive"),
                                  "command 0x%02x" % (row.get("primitive") or 0)),
               row.get("reference") or 0, row.get("odo_mm"),
               row.get("buffer") or 0, row.get("m22"), row.get("m24"),
               row.get("m26"), *first,
               " (first stage running)" if any(first) else ""))
    return lines


START_TEXT = {"confirmed": "was confirmed",
              "sent": "is waiting for the AMS to confirm it",
              "refused_busy": "was not sent: the AMS was busy",
              "refused_hub": "was refused: filament is past the feeder, in "
                             "the hub",
              "not_confirmed": "was sent but the AMS never started drying",
              "failed": "could not be sent",
              "refused_rotate": "was refused: the spools cannot turn while "
                                "filament is out"}
STATE_TEXT = {"drying": "drying", "forcing": "drying, asking the AMS to "
              "vent", "cooldown": "in the sealed cooldown", "idle": "idle",
              "heating": "heating", "closed": "drying, vents closed",
              "open": "drying, vents held open"}
CLOSED_TEXT = {"end": "at the end of the dry", "stopped": "when the dry was "
               "stopped", "disabled": "when the vents were left to the AMS",
               "settled": "once the moisture stopped falling",
               "max_open": "at the longest opening",
               "cold": "because the chamber fell 5 °C below target"}


def dryer_lines(snapshot) -> list:
    """One sentence per dryer row: what the dry is doing, the exhaust requests
    sent (firmware 11) or the openings (firmware 10), whether the AMS's vents
    are open, and how the last dry ended, from the row's own fields."""
    snap = snapshot or {}
    lines = []
    for row in snap.get("dryers") or []:
        if not isinstance(row, dict):
            continue
        text = "Dryer of the AMS at index %s: %s" % (
            row.get("index"), STATE_TEXT.get(row.get("vent_state"),
                                             row.get("vent_state")))
        flags = row.get("vent_flags") or {}
        if isinstance(row.get("pulses"), int):
            text += ", %d exhaust request%s this dry" % (
                row["pulses"], "" if row["pulses"] == 1 else "s")
            if "vents_open" in flags:
                text += ", vents %s" % ("open" if flags["vents_open"]
                                        else "closed")
            if flags.get("open_at_pulse"):
                text += " (the AMS opened them on a request)"
        elif isinstance(row.get("openings"), int):
            text += ", %d opening%s this dry" % (
                row["openings"], "" if row["openings"] == 1 else "s")
            moisture = row.get("ah_mgm3")
            if isinstance(moisture, int) and moisture:
                text += "; moisture %.1f g/m³" % (moisture / 1000.0)
        start = row.get("start")
        if start in START_TEXT:
            text += "; the last start %s" % START_TEXT[start]
            if flags.get("rotate") and start != "refused_rotate":
                text += ", turning the spools"
            if start in ("refused_busy", "not_confirmed") and \
                    isinstance(row.get("start_state"), int) and \
                    row["start_state"] != 255:
                text += " (AMS state %d)" % row["start_state"]
            if start == "refused_hub" and row.get("start_bay") is not None:
                text += " (bay %d)" % row["start_bay"]
        if row.get("closed_by") in CLOSED_TEXT:
            text += "; vents last closed %s" % CLOSED_TEXT[row["closed_by"]]
        ended = row.get("ended") or {}
        if ended.get("cool_down") or ended.get("stop"):
            text += "; the last dry ended %s%s%s" % (
                "with the cool-down request" if ended.get("cool_down")
                else "with the stop",
                ", the vents opened again during the cooldown"
                if ended.get("vents_opened") else "",
                ", at the 60-minute cap" if ended.get("capped") else "")
        lines.append(text + ".")
    return lines


def _seconds(ms: int) -> str:
    return "%g s" % round(ms / 1000.0, 1)


def feed_failure_lines(snapshot) -> list:
    """One sentence per feed-failure row: what the odometer did around the
    failed load or calibration, from the row's own fields and nothing more.
    For example: "Last load of AMS df5d1d75218e failed: the AMS rewound from
    1200 mm to 601 mm and then stopped moving for 4 s, 6.2 s into the feed;
    it was still rewinding. AMS state: moving." """
    snap = snapshot or {}
    units = {unit.get("slot"): unit for unit in snap.get("units") or []
             if isinstance(unit, dict)}
    lines = []
    for row in snap.get("feed_failures") or []:
        if not isinstance(row, dict):
            continue
        unit = units.get(row.get("slot")) or {}
        flags = row.get("odo_flags") or {}
        kind = unit.get("last_failure_kind")
        retract = bool(flags.get("retract"))
        if retract:
            operation = {"calibrate": "calibration"}.get(kind, "unload")
        else:
            operation = {"load": "load", "calibrate": "calibration"}.get(
                kind, "load or calibration")
        who = "AMS %s" % unit["uid"] if unit.get("uid") else \
            "the AMS at index %s" % row.get("index")
        start, low, at = (row.get("odo_start_mm"), row.get("odo_low_mm"),
                          row.get("odo_at_mm"))
        known_start = flags.get("start") and isinstance(start, int)
        known_at = flags.get("at_valid") and isinstance(at, int)
        if retract and known_start and known_at and at >= start - 10:
            motion = "the odometer stood at %d mm while the AMS pulled the " \
                "filament back" % at
        elif retract and known_start and known_at:
            motion = "the AMS pulled the filament back from %d mm to %d mm" \
                % (start, at)
        elif known_start and known_at and (flags.get("rewind_seen") or
                                           at < start):
            motion = "the AMS rewound from %d mm to %d mm" % (start, at)
            if isinstance(low, int) and abs(low - at) >= 10:
                motion += " (lowest %d mm)" % low
        elif known_start and known_at:
            motion = "the odometer read %d mm at the start and %d mm at " \
                "the failure" % (start, at)
        elif known_at:
            motion = "the odometer read %d mm at the failure" % at
        else:
            motion = "the odometer gave no reading at the failure"
        since = row.get("since_progress_ms")
        if isinstance(since, int):
            motion += (", and then it stopped moving for %s" if retract
                       else " and then stopped moving for %s") % _seconds(since)
        feed = row.get("feed_ms")
        if isinstance(feed, int):
            motion += ", %s into the %s" % (
                _seconds(feed), "pull back" if retract else "feed")
        if flags.get("rewinding"):
            motion += "; it was still rewinding"
        state = row.get("ams_state")
        lines.append("Last %s of %s failed: %s.%s" % (
            operation, who, motion,
            " AMS state: %s." % state if state else ""))
    return lines


# An AMS with no index answers the bridge's roll call about twice a second
# until it is given one, normally within a second or two. One still answering
# at the snapshot (within ANNOUNCE_RECENT_MS) after ANNOUNCE_STUCK of them is
# stuck, even while other units run.
ANNOUNCE_RECENT_MS = 10000
ANNOUNCE_STUCK = 30


def _announcing_without_index(snap) -> dict:
    """An AMS that keeps announcing itself without getting an index, or {}.
    Bridge firmware 9 can leave a newly connected AMS without an offer for up
    to 36 minutes once it has run a while (an offer deadline that only moves
    when an offer is made, compared in a timer that wraps every 71.6 min)."""
    last = _get(snap, "discovery", "last_announce_ms", default=None)
    uptime = _get(snap, "system", "uptime_ms", default=None)
    if not isinstance(last, int) or not isinstance(uptime, int) or \
            uptime - last > ANNOUNCE_RECENT_MS or \
            _get(snap, "discovery", "announces") < ANNOUNCE_STUCK:
        return {}
    family = _get(snap, "discovery", "last_announce_tag_family",
                  default=None)
    if family in (None, "unknown"):
        family = _get(snap, "discovery", "last_announce_marker",
                      default=None)
    suffix = _get(snap, "discovery", "last_announce_tag_suffix",
                  default=None)
    details = [FAMILY_TEXT.get(family, "")] if family else []
    if suffix:
        details.append("tag suffix %s" % suffix)
    who = "An AMS" + (" (%s)" % ", ".join(d for d in details if d)
                      if any(details) else "")
    offers = _get(snap, "discovery", "offers")
    if not offers:
        return {"class": "no-index-offered",
                "summary": "%s keeps announcing itself, but the bridge has "
                           "not offered it an index, so it can't be used "
                           "(its LED blinks)." % who,
                "action": "Bridge firmware 9 can take up to 36 minutes to "
                          "offer one when it has been running a while. To "
                          "add it now, switch the printer off (the bridge's "
                          "24 V and USB) for 10 seconds, then run "
                          "FIRMWARE_RESTART once Klipper is up.",
                "tag_suffix": suffix}
    index = _get(snap, "discovery", "last_offer_index", default=None)
    return {"class": "index-not-taken",
            "summary": "%s keeps announcing itself although the bridge "
                       "offered it %s %d time%s." % (
                           who, "index %s" % index if index is not None
                           else "an index", offers,
                           "" if offers == 1 else "s"),
            "action": "Power the AMS off and on; if it stays, send this "
                      "report to support.",
            "tag_suffix": suffix}


def _classify(snapshot) -> dict:
    snap = snapshot or {}
    boot = _get(snap, "system", "boot_status")
    genuine = _get(snap, "system", "genuine_status")
    effective = _get(snap, "system", "policy", "effective", "master",
                     default=True)
    if boot or genuine or not effective:
        return {"class": "bridge-off-line",
                "summary": "The bridge's own start-up checks keep it off the "
                           "AMS line (boot status %d, genuine status %d)."
                           % (boot, genuine),
                "action": "Contact support with this report."}
    line_bytes = _get(snap, "line", "bytes")
    if _get(snap, "system", "role", "foreign_master", default=False) or \
            _get(snap, "system", "role", "tx_fault", default=False) or \
            (_get(snap, "system", "master_acquisitions") == 0 and
             line_bytes > 0):
        return {"class": "other-controller",
                "summary": "The bridge never took control of the AMS line: "
                           "something else talks on it.",
                "action": "Disconnect any printer or second bridge from the "
                          "AMS, then power the bridge off and on."}
    if _get(snap, "master", "sent_messages") > 0 and line_bytes == 0:
        return {"class": "silent-line",
                "summary": "The bridge sends on the AMS data line and "
                           "nothing comes back.",
                "action": "The data pair is open: check the 4-pin BUFFER "
                          "cable and the 6-pin AMS cable for a pushed-back "
                          "pin or a bad crimp, and that the AMS has started. "
                          "Lit lanes prove power, not data."}
    if line_bytes > 0 and _get(snap, "line", "messages") == 0:
        return {"class": "garbled-line",
                "summary": "Bytes arrive from the AMS but none of them "
                           "decode.",
                "action": "Check the data cable: its orientation (the two "
                          "data wires swapped), its crimps, and that it is "
                          "the right cable."}
    candidates = snap.get("candidates") or []
    if _get(snap, "discovery", "admitted") == 0 and (
            _get(snap, "discovery", "announces") > 0 or candidates):
        rows = [(row.get("reason"), row) for row in
                snap.get("refusals") or []][::-1] + \
            [(row.get("refusal"), row) for row in candidates]
        rows = [(reason, row) for reason, row in rows
                if reason and reason != "none"]
        if rows:
            reason, row = rows[0]            # the latest refusal first
            suffix = row.get("tag_suffix") or \
                _get(snap, "discovery", "last_announce_tag_suffix",
                     default=None)
            who = "An AMS"
            if suffix:
                who = "An AMS (tag suffix %s%s)" % (
                    suffix, ", not one this firmware recognises"
                    if row.get("tag_family") == "unknown" else "")
            return {"class": "refused",
                    "summary": "%s answers but the bridge refuses it: %s."
                               % (who, REFUSAL_TEXT.get(reason, reason)),
                    "action": "Send this report to support if the reason "
                              "is not clear.",
                    "reason": reason, "tag_suffix": suffix}
        waiting = [row.get("waiting") for row in candidates
                   if row.get("waiting") not in (None, "none")]
        step = waiting[0] if waiting else "none"
        return {"class": "not-admitted",
                "summary": "An AMS answers but is not admitted yet: %s."
                           % WAITING_TEXT.get(step, "identification did not "
                                              "finish"),
                "action": "Power the AMS off and on; if it stays, send this "
                          "report to support.",
                "waiting": step}
    announcing = _announcing_without_index(snap)
    if announcing:
        return announcing
    for unit in snap.get("units") or []:
        host = unit.get("host") or {}
        if not host.get("configured") or not host.get("heard"):
            uid = unit.get("uid", "")
            return {"class": "unused-by-klipper",
                    "summary": "The bridge offers AMS %s but Klipper does "
                               "not use it." % uid,
                    "action": "Check that an [mcu] section in oams.cfg has "
                              "serial: /dev/oams/%s, that the relay runs, "
                              "then FIRMWARE_RESTART." % uid,
                    "uid": uid}
    return {"class": "none",
            "summary": "The snapshot shows none of the known failure "
                       "patterns.",
            "action": ""}


# ---------------------------------------------------------------------------
# What a report may carry
# ---------------------------------------------------------------------------

_ENUM_TEXT = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_UID_TEXT = re.compile(r"^[0-9a-f]{12}$")
_SUFFIX_TEXT = re.compile(r"^(?:[\x20-\x7e]{4}|0x[0-9a-f]{8})$")
_REPLY_TEXT = re.compile(r"^[0-9a-f]{%d}$" % (2 * DRYER_REPLY_SIZE))

# The keys of the fields that decode to objects, and what each holds.
NESTED = {
    "image": {key: "bool" for key in IMAGE_BITS},
    "role": {key: "bool" for key in ROLE_BITS},
    "flags": {key: "bool" for key in FLAG_BITS},
    "odo_flags": {key: "bool" for key in ODO_BITS},
    "host": dict({key: "bool" for key in HOST_BITS}, latch="enum"),
    "operation": {"active": "bool", "kind": "enum?"},
    "vent_flags": {key: "bool" for key in VENT_FLAG_BITS},
    "ended": {key: "bool" for key in ENDED_BITS},
}


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _allowed(kind: str, value) -> bool:
    if kind.endswith("?") and value is None:
        return True
    kind = kind.rstrip("?")
    if kind == "int":
        return _is_int(value)
    if kind == "bool":
        return isinstance(value, bool)
    if kind == "enum":
        return isinstance(value, str) and bool(_ENUM_TEXT.match(value))
    if kind == "uid":
        return isinstance(value, str) and bool(_UID_TEXT.match(value))
    if kind == "version":
        return isinstance(value, list) and len(value) == 4 and \
            all(_is_int(v) and 0 <= v <= 255 for v in value)
    if kind == "suffix":
        return value is None or (isinstance(value, str) and
                                 bool(_SUFFIX_TEXT.match(value)))
    if kind == "reply":
        return isinstance(value, str) and bool(_REPLY_TEXT.match(value))
    return False


def _allowed_object(field: str, value):
    if not isinstance(value, dict):
        return None
    if field == "policy":                    # its two halves
        return {half: {key: value[half][key] for key in POLICY_BITS
                       if isinstance(value[half].get(key), bool)}
                for half in ("requested", "effective")
                if isinstance(value.get(half), dict)}
    keys = NESTED.get(field, {})
    return {key: value[key] for key, kind in keys.items()
            if key in value and _allowed(kind, value[key])}


def _allowed_fields(row, layout) -> dict:
    out = {}
    if not isinstance(row, dict):
        return out
    for field, _, _, kind in layout:
        if field not in row:
            continue
        value = row[field]
        if kind == "bits":
            kept = _allowed_object(field, value)
            if kept is not None:
                out[field] = kept
        elif _allowed(kind, value):
            out[field] = list(value) if isinstance(value, list) else value
    return out


def allowlisted(snapshot) -> dict:
    """A copy of a decoded snapshot with only the fields this module
    decodes, each of its decoded type (integers, booleans, enum names, the
    12-digit unit ID, the 4-byte AMS version, the 4-character tag suffix).
    Everything else is dropped."""
    snap = snapshot if isinstance(snapshot, dict) else {}
    out = {"format": FORMAT_1 if snap.get("format") == FORMAT_1 else None}
    for name, layout, table in SECTIONS.values():
        value = snap.get(name)
        if name == "motor_samples" and not value:
            continue                          # only present when sent
        if name == "dryers":                  # a firmware 10 row's fields too
            layout = layout + tuple(field for field in DRYER_V10
                                    if field[0] not in
                                    {f[0] for f in layout})
        if table:
            out[name] = [_allowed_fields(row, layout)
                         for row in (value if isinstance(value, list)
                                     else [])]
        else:
            out[name] = _allowed_fields(value, layout) \
                if isinstance(value, dict) else None
    return out


__all__ = [
    "MAX_ATTEMPTS", "MINIMUM_FIRMWARE", "MIN_INTERVAL_S", "REPLY_TIMEOUT_S",
    "RETRY_DELAY_S", "SECTIONS", "SNAPSHOT_METHOD", "SnapshotError",
    "SnapshotQuery",
    "allowlisted", "classify", "decode_body", "enum_name", "failure", "gate",
    "layout_size", "make_request", "parse_report",
]
