# SPDX-License-Identifier: MIT
"""One AMS firmware update through the bridge (bridge firmware 23 and later).

The bridge updates an AMS unit's own MCU the way a Bambu printer does: it
puts the unit in its loader, answers the loader's requests for the image
header and blocks, waits for the loader's verdict and the reboot, then reads
the unit's new version. The host owns the image (Bambu's file, checked by
``cortex_bridge.ams_firmware``) and streams it over two runtime-mux control
messages; the bridge keeps only a 4 KiB window of it.

Wire format (the bridge's docs/host-integration.md, "AMS firmware update"):

- type 10 AmsUpdateRequest, host to bridge: token(4) op(1) body. Integers big
  endian. BEGIN = index(1) family(1) size(4) confirm(12) model_len(1)
  model; DATA = offset(4) bytes(1..256); ABORT = no body.
- type 11 AmsUpdateReport, bridge to host: token(4) status(1) kind(1) body.
  STATUS (kind 1) = a 72-byte little-endian record; TEXT (kind 2) =
  source(2, LE) + one loader line.

``AmsUpdateDriver`` is the host's side of one session, free of I/O: ``due``
returns the requests to send now, ``accept`` takes each report, and
``events`` collects what the user should see.
"""

from __future__ import annotations

import dataclasses
import os
import struct

# Bridge 22 has the update mode, but its isolation check stops every update
# while an AMS 1 is on the bus; 23 polls each unit the way the bus does.
MINIMUM_FIRMWARE = 23

REQUEST_TYPE = 10
REPORT_TYPE = 11
TOKEN_SIZE = 4
REQUEST_MAX_PAYLOAD = 265
REPORT_MIN_PAYLOAD = TOKEN_SIZE + 2
REPORT_MAX_PAYLOAD = 78

OP_BEGIN, OP_DATA, OP_ABORT, OP_RESUME = 1, 2, 3, 4
# RESUME (bridge 24): BEGIN's body; when the bridge holds neither the
# loader's banner nor the unit's model code (it restarted while the unit
# waited in its loader), the host's model stands.
RESUME_MINIMUM_FIRMWARE = 24
KIND_STATUS, KIND_TEXT = 1, 2
CONFIRM = b"ERASE-AMS-FW"
DATA_MAX = 256
STATUS_SIZE = 72

FAMILY_CODES = {"ams1": 1, "ams2": 2, "ams_ht": 3}

PHASES = ("idle", "settle", "enter", "isolate", "transfer", "verify",
          "reboot", "rejoin", "done", "failed")
PHASE_TEXT = {
    "settle": "Quieting the bus and checking the image",
    "enter": "Asking the AMS to enter its loader",
    "isolate": "Checking that only this AMS entered its loader",
    "transfer": "Sending the firmware",
    "verify": "The AMS is checking the firmware",
    "reboot": "The AMS is restarting",
    "rejoin": "Waiting for the AMS to answer again",
    "done": "Done",
    "failed": "Stopped",
}
CODES = ("ok", "no-session", "busy", "not-master", "confirm", "no-unit",
         "family", "model-mismatch", "model-unknown", "bad-image",
         "too-large", "block-size", "no-loader", "loader-silent",
         "host-stalled", "verify-timeout", "aborted", "host-lost",
         "master-lost", "no-rejoin", "isolation")
CODE_TEXT = {
    "no-session": "the bridge has no update session for this request",
    "busy": "the bridge is busy: an AMS is loading, unloading, drying or "
            "moving, or another update runs. Wait until it is idle",
    "not-master": "the bridge is not driving the AMS bus right now",
    "confirm": "the bridge did not get the confirmation it needs",
    "no-unit": "no AMS of that kind can sit at that position",
    "family": "the AMS at that position is a different model",
    "model-mismatch": "the AMS's loader names a different model or board "
                      "than this firmware is for; nothing was written",
    "model-unknown": "the bridge could not tell which model the AMS is",
    "bad-image": "the bridge rejected the image header",
    "too-large": "the image is larger than the AMS's loader accepts",
    "block-size": "the AMS's loader asked for an unexpected block size",
    "no-loader": "the AMS did not enter its loader (no answer for 15 s); "
                 "its firmware is unchanged",
    "loader-silent": "the AMS's loader stopped asking for data",
    "host-stalled": "this computer did not send the image in time",
    "verify-timeout": "the AMS gave no verdict after the last block",
    "aborted": "the update was stopped",
    "host-lost": "the link between this computer and the bridge dropped",
    "master-lost": "the bridge lost control of the AMS bus",
    "no-rejoin": "the AMS accepted the firmware but did not answer within "
                 "60 s; power it off and on",
    "isolation": "another AMS stopped answering or entered its loader "
                 "too, so the bridge stopped before erasing anything",
}
UNIT_STATES = ("empty", "application", "silent", "loader")

# The host gives up when the bridge sends nothing for this long (it reports
# at least once a second while a session runs).
REPORT_SILENCE_S = 15.0
# A DATA the bridge has not acknowledged by moving `next` is sent again
# after this long (it drops out-of-order data and says where to go on).
RESEND_AFTER_S = 1.0


class AmsUpdateError(RuntimeError):
    pass


# ----------------------------------------------------------------- encoding

def make_begin(token: bytes, index: int, family: str, size: int,
               model: str, *, resume: bool = False) -> bytes:
    code = FAMILY_CODES.get(family)
    if code is None:
        raise AmsUpdateError("unknown AMS family %r" % family)
    encoded_model = model.encode("ascii")
    if not 1 <= len(encoded_model) <= 16:
        raise AmsUpdateError("bad model name %r" % model)
    if not 0 <= index <= 0xFF or not 0 < size < 1 << 32:
        raise AmsUpdateError("bad index or size")
    payload = token + bytes((OP_RESUME if resume else OP_BEGIN, index,
                             code)) + \
        size.to_bytes(4, "big") + CONFIRM + bytes((len(encoded_model),)) + \
        encoded_model
    assert len(payload) <= REQUEST_MAX_PAYLOAD
    return payload


def make_data(token: bytes, offset: int, data: bytes) -> bytes:
    if not 1 <= len(data) <= DATA_MAX:
        raise AmsUpdateError("a DATA carries 1 to %d bytes" % DATA_MAX)
    return token + bytes((OP_DATA,)) + offset.to_bytes(4, "big") + data


def make_abort(token: bytes) -> bytes:
    return token + bytes((OP_ABORT,))


def parse_status(body: bytes) -> dict:
    if len(body) < STATUS_SIZE:
        raise AmsUpdateError("short STATUS")
    (phase, code, index, family, nxt, window, size, slot, block, want,
     served, retries, dropped, blocker) = struct.unpack_from(
         "<BBBBIIIIHHHHHB", body, 0)
    units = []
    for i in range(4):
        unit_index, state = body[64 + 2 * i], body[65 + 2 * i]
        if state:
            units.append({"index": unit_index,
                          "state": UNIT_STATES[state]
                          if state < len(UNIT_STATES) else str(state)})
    return {
        "phase": PHASES[phase] if phase < len(PHASES) else str(phase),
        "code": CODES[code] if code < len(CODES) else str(code),
        "index": index, "family": family, "next": nxt, "window": window,
        "size": size, "slot": slot, "block": block,
        "want": None if want == 0xFFFF else want, "served": served,
        "retries": retries, "dropped": dropped,
        "blocker": None if blocker == 0xFF else blocker,
        "before": version_text(body[32:36]),
        "after": version_text(body[36:40]),
        "banner": _cstring(body[40:56]),
        "model_code": _cstring(body[56:64]),
        "units": units,
    }


def parse_report(payload: bytes) -> dict:
    if not REPORT_MIN_PAYLOAD <= len(payload) <= REPORT_MAX_PAYLOAD:
        raise AmsUpdateError("bad AMS update report length")
    token, status, kind = payload[:4], payload[4], payload[5]
    body = payload[6:]
    report = {"token": token,
              "status": CODES[status] if status < len(CODES) else str(status)}
    if kind == KIND_STATUS:
        report["kind"] = "status"
        report.update(parse_status(body))
    elif kind == KIND_TEXT:
        if len(body) < 2:
            raise AmsUpdateError("short TEXT")
        report["kind"] = "text"
        report["source"] = int.from_bytes(body[:2], "little")
        report["line"] = body[2:].decode("ascii", "replace")
    else:
        report["kind"] = "unknown"
    return report


def version_text(raw: bytes) -> str:
    """The 0x0103 version bytes, lowest first (1d 15 00 03 = 03.00.21.29);
    empty when all zero."""
    if not any(raw):
        return ""
    return "%02d.%02d.%02d.%02d" % tuple(reversed(raw))


def _cstring(raw: bytes) -> str:
    return raw.split(b"\0", 1)[0].decode("ascii", "replace")


def model_for(family: str, hardware: str) -> str:
    """What the AMS's loader (or its 0x0103 model code) calls the board the
    image is for: N3F05 (AMS 2 Pro rev5), N3S05 (AMS HT rev5), AMS08."""
    prefix = {"ams1": "AMS", "ams2": "N3F", "ams_ht": "N3S"}.get(family)
    digits = hardware[3:] if hardware.startswith("rev") else ""
    if prefix is None or not digits.isdigit():
        raise AmsUpdateError("no model name for %s %s" % (family, hardware))
    return "%s%02d" % (prefix, int(digits))


# ----------------------------------------------------------------- the driver

@dataclasses.dataclass
class Outcome:
    ok: bool
    code: str
    message: str
    before: str = ""
    after: str = ""
    status: dict | None = None


class AmsUpdateDriver:
    """The host's side of one update session."""

    def __init__(self, image: bytes, *, index: int, family: str, model: str,
                 now: float, token: bytes | None = None,
                 probe: bool = False, resume: bool = False):
        self.image = image
        # A probe opens a session only to read the unit's model code from
        # the bridge's first reply, then aborts during Settle: no loader
        # command is ever sent.
        self.probe = probe
        self.resume = resume
        self.index = index
        self.family = family
        self.model = model
        self.token = token or os.urandom(TOKEN_SIZE)
        self.started = now
        self.last_report = now
        self.begun = False          # BEGIN sent
        self.accepted = False       # BEGIN answered Ok
        self.sent = 0               # next offset this host will send
        self.window = 0
        self.bridge_next = 0
        self.last_data_at = 0.0
        self.status: dict | None = None
        self.outcome: Outcome | None = None
        self.events: list = []      # dicts for the user, drained by the caller
        self.abort_requested = False
        self._abort_sent = False
        self.final_units: list | None = None
        self.probed: dict | None = None

    @property
    def finished(self) -> bool:
        return self.outcome is not None

    def abort(self) -> None:
        self.abort_requested = True

    def _finish(self, ok: bool, code: str, message: str) -> None:
        if self.outcome is None:
            status = self.status or {}
            self.outcome = Outcome(ok, code, message,
                                   before=status.get("before", ""),
                                   after=status.get("after", ""),
                                   status=self.status)
            self.events.append({"event": "result", "ok": ok, "code": code,
                                "message": message,
                                "before": self.outcome.before,
                                "after": self.outcome.after,
                                "units": status.get("units", [])})

    def due(self, now: float) -> list:
        """Request payloads (type 10) to send now."""
        if self.finished:
            return []
        if now - self.last_report > REPORT_SILENCE_S:
            self._finish(False, "bridge-silent",
                         "the bridge stopped reporting for %d s"
                         % REPORT_SILENCE_S)
            return [make_abort(self.token)] if self.accepted else []
        if not self.begun:
            self.begun = True
            return [make_begin(self.token, self.index, self.family,
                               len(self.image), self.model,
                               resume=self.resume)]
        if self.abort_requested and not self._abort_sent:
            self._abort_sent = True
            return [make_abort(self.token)]
        if not self.accepted or self._abort_sent:
            return []
        # Data the bridge did not take (it moved neither `next` nor said
        # anything new) is sent again from where it says to go on.
        if self.sent > self.bridge_next and \
                now - self.last_data_at > RESEND_AFTER_S:
            self.sent = self.bridge_next
        out = []
        limit = min(self.window, len(self.image))
        while self.sent < limit:
            chunk = self.image[self.sent:min(self.sent + DATA_MAX, limit)]
            out.append(make_data(self.token, self.sent, chunk))
            self.sent += len(chunk)
            self.last_data_at = now
        return out

    def accept(self, payload: bytes, now: float) -> None:
        try:
            report = parse_report(payload)
        except AmsUpdateError:
            return
        self.last_report = now
        if report["kind"] == "text":
            self.events.append({"event": "loader", "source": report["source"],
                                "line": report["line"]})
            return
        if report["kind"] != "status":
            return
        if report["token"] != self.token:
            # Another host's session, or the bridge's after-session units
            # report for an earlier one; only the units list matters then.
            return
        previous = self.status
        self.status = report
        if self.finished:
            # About 3 s after a session ends the bridge reports every unit
            # again, once the bus has polled them: the isolation proof.
            if report["units"]:
                self.final_units = report["units"]
                event = {"event": "units", "units": report["units"]}
                # Bridge 24 reads the rebooted unit's version the way its
                # admission does before this report: the version it runs.
                if report["after"]:
                    event["after"] = report["after"]
                    if self.outcome is not None and self.outcome.ok:
                        self.outcome.after = report["after"]
                self.events.append(event)
            return
        if not self.accepted:
            if report["status"] != "ok":
                self._finish(False, report["status"],
                             CODE_TEXT.get(report["status"],
                                           report["status"]))
                return
            self.accepted = True
            if self.probe:
                self.abort_requested = True
                self.probed = {"model_code": report["model_code"],
                               "before": report["before"]}
                self.events.append({"event": "probe", **self.probed})
                return
        if self.probe:
            if report["phase"] == "failed":
                self._finish(True, "probe", "model code %r"
                             % (self.probed or {}).get("model_code", ""))
                self.outcome.status = dict(report, **(self.probed or {}))
            return
        self.bridge_next = report["next"]
        self.window = report["window"]
        if report["next"] > self.sent:
            self.sent = report["next"]
        if previous is None or previous["phase"] != report["phase"]:
            self.events.append({"event": "phase", "phase": report["phase"],
                                "text": PHASE_TEXT.get(report["phase"], ""),
                                "banner": report["banner"]})
        if report["phase"] == "transfer" and report["want"] is not None and \
                (previous is None or previous.get("want") != report["want"]):
            block = max(1, report["block"] or 1024)
            total = max(1, -(-(len(self.image) - 416) // block))
            self.events.append({"event": "progress", "block": report["want"],
                                "blocks": total,
                                "retries": report["retries"]})
        if report["phase"] == "done":
            if report["code"] == "no-rejoin":
                # The loader accepted the image; the unit did not answer
                # within the bridge's wait, and its version is read once
                # it does.
                self._finish(True, "no-rejoin",
                             "the AMS accepted the firmware but has not "
                             "answered yet; if it stays offline for two "
                             "minutes, power it off and on")
            else:
                self._finish(True, "ok", "the AMS now runs %s"
                             % (report["after"] or "the new firmware"))
        elif report["phase"] == "failed":
            code = report["code"]
            text = CODE_TEXT.get(code, code)
            if code == "isolation" and report["blocker"] is not None:
                text += " (the AMS at index %d)" % report["blocker"]
            self._finish(False, code, text)


class AmsListener:
    """Listen to the AMS loaders on the bus for a while, without a session.

    The bridge forwards loader lines only to a host that has sent it an AMS
    update request in this USB session, so the listener sends one ABORT
    with a fresh token: with no session running the bridge answers
    NoSession and changes nothing. Then every loader line on the bus comes
    through, which shows a unit left waiting in its loader."""

    def __init__(self, *, now: float, seconds: float,
                 token: bytes | None = None):
        self.token = token or os.urandom(TOKEN_SIZE)
        self.deadline = now + seconds
        self.sent = False
        self.events: list = []
        self.outcome: Outcome | None = None
        self.final_units: list | None = None
        self.lines = 0
        self.status: dict | None = None

    @property
    def finished(self) -> bool:
        return self.outcome is not None

    def abort(self) -> None:
        self.deadline = 0.0

    def _finish(self, ok: bool, code: str, message: str) -> None:
        if self.outcome is None:
            self.outcome = Outcome(ok, code, message)
            self.final_units = []
            self.events.append({"event": "result", "ok": ok, "code": code,
                                "message": message, "lines": self.lines})

    def due(self, now: float) -> list:
        if self.finished:
            return []
        if now >= self.deadline:
            self._finish(True, "ok", "%d loader line(s) heard" % self.lines)
            return []
        if not self.sent:
            self.sent = True
            return [make_abort(self.token)]
        return []

    def accept(self, payload: bytes, now: float) -> None:
        try:
            report = parse_report(payload)
        except AmsUpdateError:
            return
        if report["kind"] == "text":
            self.lines += 1
            self.events.append({"event": "loader", "source": report["source"],
                                "line": report["line"]})
        elif report["kind"] == "status":
            self.status = report
            self.events.append({"event": "status", "phase": report["phase"],
                                "code": report["code"],
                                "answer": report["status"],
                                "units": report["units"]})
