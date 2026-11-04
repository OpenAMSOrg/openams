# SPDX-License-Identifier: MIT
"""Which firmware version a Cortex Bridge runs, and whether it is current.

Production bridge firmware answers an identity request on the same USB stream
as the AMS traffic. The request carries a 4-byte token; the report echoes it
with a status byte and, when the status is 0, this body:

    format(1)=1  app_kind(1)  version(4)  boot_abi(2)  genuine(1)
    reset_flags(1)  uptime_ms(4)  ...identifiers and a certificate

All integers are big-endian. ``version`` is the release number, the same
number as a package's version, so the two compare as integers. Only the fields
above are read; the identifiers and the certificate after them are not kept.

Lab (development) firmware has no such request: it must never be sent one,
so the relay asks only a bridge whose USB manufacturer string says it runs
production firmware.

The relay asks once per USB session (``IdentityQuery``) and writes the answer
to its status file as the ``firmware`` object (``firmware_status``). Without a
relay, ``query_direct`` asks over the bridge's port itself.
"""

from __future__ import annotations

import datetime
import os
import time

from cortex_bridge.cortexd import (
    IDENTITY_TOKEN_SIZE,
    Message,
    MessageType,
)

FORMAT_1 = 1
FORMAT_1_FIELDS = 14            # format .. uptime_ms, the part read here
APP_KIND_NAMES = {1: "bridge"}

STATUS_OK = 0
STATUS_NAMES = {
    1: "rate-limited",
    2: "not-ready",
    3: "not-genuine",
    4: "busy",
    5: "boot-failed",
}
RETRY_STATUSES = frozenset((1, 2, 4))

GENUINE_PASSED = 0
GENUINE_PENDING = 0xFF

# reset_flags bits, most telling first.
RESET_CAUSES = (
    (0x60, "watchdog"),          # independent or window watchdog
    (0x10, "software"),          # a restart the firmware asked for
    (0x08, "power-on"),
    (0x04, "reset-pin"),
    (0x80, "low-power"),
)

REPLY_TIMEOUT_S = 5.0           # the bridge answers within about 2 s
FIRST_DELAY_S = 0.5             # let the first roster settle
MAX_SILENT = 3                  # unanswered requests before giving up
RETRY_MIN_S = 1.0
RETRY_MAX_S = 30.0

# What each state or reason means, in the words a customer reads.
REASON_TEXT = {
    "querying": "the bridge has not answered the version request yet",
    "busy": "the bridge is busy (an AMS is working) and is asked again "
            "when it is idle",
    "rate-limited": "the bridge was asked too often and is asked again "
                    "in a moment",
    "not-ready": "the bridge is still starting and is asked again in a "
                 "moment",
    "lab-firmware": "this bridge runs lab (development) firmware, which has "
                    "no release version",
    "no-answer": "the bridge did not answer the version request",
    "boot-failed": "the bridge could not read its own identity: its boot "
                   "did not answer",
    "not-genuine": "the bridge failed its authenticity check",
    "unreadable-report": "the bridge answered in a format this OpenAMS does "
                         "not know (update OpenAMS)",
    "disconnected": "the bridge is not connected",
    "unknown-image": "the relay cannot tell from USB whether the bridge runs "
                     "production firmware",
    "no-pyserial": "pyserial is missing, so this command cannot open the "
                   "bridge's port; start the relay (sudo systemctl start "
                   "cortex-bridge) and try again",
}


class ReportError(ValueError):
    """The bytes are not an identity report this module understands."""


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")


def make_request(token: bytes) -> Message:
    if len(token) != IDENTITY_TOKEN_SIZE:
        raise ValueError("an identity token is %d bytes" % IDENTITY_TOKEN_SIZE)
    return Message(MessageType.IDENTITY_REQUEST, bytes(token))


def reset_cause(flags: int) -> str:
    for mask, name in RESET_CAUSES:
        if flags & mask:
            return name
    return "unknown"


def parse_report(payload: bytes) -> dict:
    """Decode an identity report payload. Returns ``token``, ``status`` (a
    name: "ok" or one of ``STATUS_NAMES``) and, for "ok", the fields."""
    if len(payload) < IDENTITY_TOKEN_SIZE + 1:
        raise ReportError("the report is too short")
    token = bytes(payload[:IDENTITY_TOKEN_SIZE])
    status = payload[IDENTITY_TOKEN_SIZE]
    body = payload[IDENTITY_TOKEN_SIZE + 1:]
    if status != STATUS_OK:
        return {"token": token, "status": STATUS_NAMES.get(
            status, "status-%d" % status), "status_code": status}
    if len(body) < FORMAT_1_FIELDS or body[0] != FORMAT_1:
        raise ReportError("unknown report format")
    genuine = body[8]
    return {
        "token": token,
        "status": "ok",
        "status_code": STATUS_OK,
        "app_kind": APP_KIND_NAMES.get(body[1], "kind-%d" % body[1]),
        "version": int.from_bytes(body[2:6], "big"),
        "boot_abi": int.from_bytes(body[6:8], "big"),
        "genuine": "passed" if genuine == GENUINE_PASSED else
                   "pending" if genuine == GENUINE_PENDING else "failed",
        "genuine_code": genuine,
        "reset_flags": body[9],
        "reset_cause": reset_cause(body[9]),
        "uptime_ms": int.from_bytes(body[10:14], "big"),
    }


def firmware_status(state: str, reason: str = "", report: dict | None = None,
                    ) -> dict:
    """The relay's ``firmware`` status object."""
    result = {
        "state": state,               # querying | known | unavailable
        "reason": reason,
        "version": None,
        "app_kind": None,
        "boot_abi": None,
        "genuine": None,
        "genuine_code": None,
        "reset_cause": None,
        "reset_flags": None,
        "uptime_ms": None,
        "read_at": None,
    }
    if report is not None:
        for key in ("version", "app_kind", "boot_abi", "genuine",
                    "genuine_code", "reset_cause", "reset_flags",
                    "uptime_ms"):
            result[key] = report[key]
        result["read_at"] = _now_iso()
    return result


class IdentityQuery:
    """Ask the bridge once per USB session, retrying while it is busy.

    ``due(now)`` returns a request ``Message`` to send, or None;
    ``accept(payload, now)`` takes each identity report and returns True when
    the result changed. ``firmware`` is the status object at any time.
    """

    def __init__(self, now: float, token_source=os.urandom):
        self.token_source = token_source
        self.token = None
        self.sent_at = 0.0
        self.next_at = now + FIRST_DELAY_S
        self.silent = 0
        self.retries = 0
        self.firmware = firmware_status("querying", "querying")

    @property
    def finished(self) -> bool:
        return self.firmware["state"] != "querying"

    def due(self, now: float):
        if self.finished:
            return None
        if self.token is not None:
            if now - self.sent_at < REPLY_TIMEOUT_S:
                return None
            self.token = None
            self.silent += 1
            if self.silent >= MAX_SILENT:
                self.firmware = firmware_status("unavailable", "no-answer")
                return None
            self.next_at = now + RETRY_MIN_S
        if now < self.next_at:
            return None
        self.token = bytes(self.token_source(IDENTITY_TOKEN_SIZE))
        self.sent_at = now
        return make_request(self.token)

    def accept(self, payload: bytes, now: float) -> bool:
        if self.finished or self.token is None or \
                bytes(payload[:IDENTITY_TOKEN_SIZE]) != self.token:
            return False                     # not the answer to our request
        self.token = None
        try:
            report = parse_report(payload)
        except ReportError:
            self.firmware = firmware_status("unavailable", "unreadable-report")
            return True
        if report["status"] == "ok":
            self.firmware = firmware_status("known", "", report)
            return True
        if report["status_code"] in RETRY_STATUSES:
            self.retries += 1
            delay = min(RETRY_MAX_S, RETRY_MIN_S * (2 ** min(self.retries, 5)))
            self.next_at = now + delay
            changed = self.firmware["reason"] != report["status"]
            self.firmware = firmware_status("querying", report["status"])
            return changed
        self.firmware = firmware_status("unavailable", report["status"])
        return True


def query_direct(device: str, *, serial_module=None, timeout: float = 15.0,
                 clock=time.monotonic, sleep=time.sleep) -> dict:
    """Ask the bridge on ``device`` for its version without the relay.

    Opens the port (which the relay must not hold), waits for the roster,
    asks, and closes. Returns a ``firmware_status`` object; a bridge that
    does not answer in ``timeout`` seconds gives state "unavailable"."""
    from cortex_bridge import relay
    from cortex_bridge.cortexd import CleartextMuxTransport, Direction
    if serial_module is None:
        import serial as serial_module
    port = relay.open_bridge_port(serial_module, device)
    transport = None
    try:
        initial = relay.wait_for_roster(port, timeout=min(timeout, 10.0))
        port.timeout = 0
        port.write_timeout = 0
        transport = CleartextMuxTransport.connect(port, initial)
        port = None
        deadline = clock() + timeout
        query = IdentityQuery(clock())
        while not query.finished and clock() < deadline:
            now = clock()
            request = query.due(now)
            if request is not None:
                transport.send_message(request.encode(Direction.HOST_TO_BRIDGE))
            for encoded in transport.recv_messages():
                message = Message.decode(encoded, Direction.BRIDGE_TO_HOST)
                if message.kind == MessageType.IDENTITY_REPORT:
                    query.accept(message.payload, clock())
            transport.service()
            sleep(0.05)
        if not query.finished:
            reason = query.firmware["reason"]
            return firmware_status("unavailable", reason if reason not in
                                   ("", "querying") else "no-answer")
        return query.firmware
    finally:
        if transport is not None:
            transport.close()
        elif port is not None:
            port.close()


# ---------------------------------------------------------------------------
# Installed against bundled
# ---------------------------------------------------------------------------

def compare(firmware: dict | None, bundled: list) -> dict:
    """Compare what the bridge runs with the newest usable bundled package.

    Returns ``installed`` (int or None), ``latest`` (the package dict or
    None) and ``verdict``: "up-to-date", "update-available",
    "newer-than-bundled", "nothing-bundled" or "unknown"."""
    latest = next((p for p in bundled if p.get("ok")), None)
    installed = None
    if firmware and firmware.get("state") == "known":
        installed = firmware.get("version")
    if installed is None:
        verdict = "unknown"
    elif latest is None:
        verdict = "nothing-bundled"
    else:
        newest = int(latest.get("version") or 0)
        verdict = "up-to-date" if installed == newest else \
            "update-available" if installed < newest else "newer-than-bundled"
    return {"installed": installed, "latest": latest, "verdict": verdict}


def unknown_reason(firmware: dict | None) -> str:
    """Why the installed version is not known, in plain words."""
    if not firmware:
        return REASON_TEXT["disconnected"]
    reason = firmware.get("reason") or firmware.get("state") or ""
    return REASON_TEXT.get(reason, "the bridge reported %s" % reason)


__all__ = [
    "IdentityQuery", "ReportError", "compare", "firmware_status",
    "make_request", "parse_report", "query_direct", "reset_cause",
    "unknown_reason",
]
