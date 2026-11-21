# SPDX-License-Identifier: MIT
"""The Cortex Bridge relay: one Klipper serial port per AMS unit.

The bridge carries one Klipper serial session per AMS unit over its single USB
serial port. This service owns that port and publishes each unit as a PTY,
linked as /dev/oams/<uid>, so Klipper sees an ordinary `[mcu]` serial device
per AMS. The bridge itself never appears in printer.cfg.

It also:
- asks a bridge running production firmware which firmware version it runs,
  once per USB connection (``cortex_bridge.bridge_info``);
- writes a small status file (state, bridge serial, firmware version, units)
  that the `cortex_bridge` command and the Moonraker component read;
- accepts firmware-update requests on a local socket and runs them itself,
  because it is the only process allowed to hold the bridge's port. It
  refuses up front a package that is not newer than the running version, and
  every result carries plain-language text (``cortex_bridge.update_messages``);
- logs to a bounded JSONL support log that never contains serial data.

Run it from the OpenAMS checkout:  python3 -m cortex_bridge.relay --help
"""

from __future__ import annotations

import argparse
import datetime
import fnmatch
import json
import os
from pathlib import Path
import select
import signal
import socket
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cortex_bridge.cortexd import (  # noqa: E402
    Message,
    CleartextMuxTransport,
    CortexCore,
    CortexDaemon,
    MessageType,
    ProtocolError,
    ROSTER_PAYLOAD_SIZE,
    VERSION as RUNTIME_MUX_VERSION,
)
from cortex_bridge import ams_update  # noqa: E402
from cortex_bridge import bridge_info, bridge_snapshot  # noqa: E402
from cortex_bridge import code_version  # noqa: E402
from cortex_bridge import kit as kit_protocol  # noqa: E402
from cortex_bridge import update_messages  # noqa: E402
from cortex_bridge.support_log import GatewaySupportLog  # noqa: E402

DAEMON_VERSION = 2
BRIDGE_VID = 0x1A86
BRIDGE_PID = 0xFE11
BRIDGE_SERIAL_PATTERN = "CB*"
UPDATE_RECEIVER_PID = 0xFE10

DEFAULT_PTY_DIR = "/dev/oams"
DEFAULT_RUN_DIR = "/run/cortex-bridge"
STATUS_FILE = "status.json"
CONTROL_SOCKET = "control.sock"
AMS_UPDATE_METHOD = "ams_update"
AMS_LISTEN_METHOD = "ams_listen"
# The control methods this relay answers, listed in its status file. A command
# from a newer checkout tells by it that the relay still runs older code (it
# was not restarted after an OpenAMS update) before asking it for something
# it lacks; a relay from before this list writes none.
CONTROL_METHODS = ("status", "update", bridge_snapshot.SNAPSHOT_METHOD,
                   AMS_UPDATE_METHOD, AMS_LISTEN_METHOD,
                   bridge_info.BUSY_METHOD)
AMS_IMAGE_MAX = 512 * 1024
AMS_UNITS_WAIT_S = 5.0

ROSTER_TIMEOUT = 30.0
READY_WAIT = 60.0
SUMMARY_INTERVAL = 60.0
# How long the relay waits for the bridge to answer a kit request.
KIT_ANSWER_WAIT = 2.0
STATUS_INTERVAL = 1.0
RETRY_INTERVAL = 1.0
MAX_PACKAGE_SIZE = 1024 * 1024

# What the manufacturer string says about the bridge's firmware image.
IMAGE_BY_MANUFACTURER = {"SiForge": "production", "OpenAMS": "lab"}


class RelayError(RuntimeError):
    """A recoverable condition: log it, wait and try again."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


class RelayStop(Exception):
    """The service manager asked the relay to stop."""


def _request_stop(_signum, _frame) -> None:
    raise RelayStop()


def systemd_notify(message: str) -> bool:
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return False
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notifier:
            notifier.connect(address)
            notifier.sendall(message.encode("utf-8"))
    except OSError:
        return False
    return True


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Finding the bridge
# ---------------------------------------------------------------------------

def describe_ports(list_ports, vid: int = BRIDGE_VID) -> list:
    """Every USB serial port with the bridge's vendor ID, as plain dicts."""
    found = []
    for item in list_ports.comports():
        if item.vid != vid:
            continue
        found.append({
            "device": item.device,
            "pid": item.pid,
            "serial": item.serial_number or "",
            "manufacturer": item.manufacturer or "",
            "product": item.product or "",
        })
    return found


def find_bridge(list_ports, *, vid: int = BRIDGE_VID, pid: int = BRIDGE_PID,
                serial_pattern: str | None = BRIDGE_SERIAL_PATTERN) -> dict:
    """Return the one running bridge, or raise RelayError saying why not."""
    ports = describe_ports(list_ports, vid)
    running = [
        port for port in ports
        if port["pid"] == pid and (
            serial_pattern is None or
            fnmatch.fnmatchcase(port["serial"], serial_pattern))
    ]
    if len(running) == 1:
        return running[0]
    if len(running) > 1:
        raise RelayError(
            "several-bridges",
            "%d Cortex Bridges are connected; give each relay one with "
            "--serial" % len(running))
    for port in ports:
        if port["pid"] != UPDATE_RECEIVER_PID:
            continue
        if port["product"].endswith("Fault"):
            raise RelayError(
                "bridge-fault",
                "the bridge reports a fault (%s); see the troubleshooting "
                "guide" % (port["serial"] or "no code"))
        if port["product"].endswith("Updater"):
            raise RelayError(
                "bridge-in-update-mode",
                "the bridge is waiting for firmware; run "
                "`cortex-bridge.py update` to finish the update")
    raise RelayError("no-bridge", "no Cortex Bridge is connected over USB")


def _keep_input_on_open(serial_class):
    """Return `serial_class` without the input flush at the end of open().

    Opening the port raises DTR, which is how the bridge knows the relay has
    claimed it, and the bridge answers at once with its roster. pyserial's
    POSIX open() raises DTR first and flushes input afterwards, which could
    discard that first message. Nothing stale can be queued: cdc_acm stops
    reading while the tty is closed.
    """
    if not hasattr(serial_class, "_reset_input_buffer"):
        return serial_class

    class KeepInputOnOpen(serial_class):
        _opening = False

        def open(self):
            self._opening = True
            try:
                super().open()
            finally:
                self._opening = False

        def _reset_input_buffer(self):
            if not self._opening:
                super()._reset_input_buffer()

    return KeepInputOnOpen


def open_bridge_port(serial_module, device: str):
    return _keep_input_on_open(serial_module.Serial)(
        device, 115200, timeout=0.25, write_timeout=5, exclusive=True)


def wait_for_roster(port, *, clock=time.monotonic,
                    timeout: float = ROSTER_TIMEOUT) -> bytes:
    """Read the four-byte header of the bridge's first roster message.

    Nothing is written: the bridge announces its roster as soon as the port
    is claimed.
    """
    deadline = clock() + timeout
    prefix = bytearray()
    while len(prefix) < 4:
        if clock() >= deadline:
            raise RelayError("no-roster",
                             "the bridge did not announce its AMS units")
        chunk = port.read(4 - len(prefix))
        if chunk:
            prefix.extend(chunk)
        elif getattr(port, "timeout", None) is None:
            raise RelayError("disconnected", "the bridge disconnected")
    if prefix[0] != RUNTIME_MUX_VERSION or \
            prefix[1] != MessageType.ROSTER or \
            int.from_bytes(prefix[2:4], "big") > ROSTER_PAYLOAD_SIZE:
        raise RelayError(
            "unsupported-firmware",
            "the bridge did not start with a unit roster; its firmware may "
            "be newer than this OpenAMS (update OpenAMS)")
    return bytes(prefix)


# ---------------------------------------------------------------------------
# Status file and control socket
# ---------------------------------------------------------------------------

class StatusFile:
    """The relay's state as one JSON file, replaced atomically."""

    def __init__(self, path: Path | None):
        self.path = path
        self.state = {
            "schema": 1,
            "daemon_version": DAEMON_VERSION,
            "pid": os.getpid(),
            "started": _now_iso(),
            "state": "starting",
            "detail": "",
            "reason": "",
            "bridge": None,
            # bridge_info.firmware_status(): what the connected bridge said
            # about its firmware; None while no bridge is connected.
            "firmware": None,
            "units": [],
            "update": None,
            "methods": list(CONTROL_METHODS),
            # The code this relay runs (code_version.snapshot()): an OpenAMS
            # update that changes it needs a relay restart.
            "code": None,
            # An AMS firmware update or listen session is running.
            "ams_update": False,
        }

    def set(self, **fields) -> None:
        self.state.update(fields)
        self.write()

    def write(self) -> None:
        if self.path is None:
            return
        self.state["updated"] = _now_iso()
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(self.state, handle, indent=1, sort_keys=True)
                handle.write("\n")
            os.chmod(temporary, 0o644)
            os.replace(temporary, self.path)
        except OSError as exc:
            print("cortex-bridge: cannot write status file: %s" % exc,
                  file=sys.stderr, flush=True)


class ControlServer:
    """A Unix socket taking one JSON request per connection.

    Requests: {"method": "status"}, {"method": "update", "file": PATH} and
    {"method": "support_snapshot"}. Replies are JSON lines; an update streams
    progress lines and ends with a line whose "event" is "result"; a support
    snapshot is one line whose "event" is "support_snapshot".
    """

    def __init__(self, path: Path | None):
        self.path = path
        self.sock = None
        if path is None:
            return
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(path))
            os.chmod(path, 0o660)
            sock.listen(4)
        except OSError as exc:
            # The AMS ports matter more than the control socket: keep
            # serving them, without status requests or updates.
            sock.close()
            print("cortex-bridge: no control socket at %s (%s); firmware "
                  "updates are unavailable" % (path, exc), file=sys.stderr,
                  flush=True)
            return
        sock.setblocking(False)
        self.sock = sock

    def poll(self):
        """Return (connection, request) for a waiting client, else None."""
        if self.sock is None:
            return None
        try:
            connection, _ = self.sock.accept()
        except (BlockingIOError, InterruptedError):
            return None
        connection.settimeout(2.0)
        try:
            data = b""
            while not data.endswith(b"\n") and len(data) < 4096:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                data += chunk
            request = json.loads(data.decode("utf-8") or "{}")
            if not isinstance(request, dict):
                raise ValueError("request is not an object")
        except (OSError, ValueError) as exc:
            send_line(connection, {"event": "result", "ok": False,
                                   "error": "bad-request",
                                   "message": str(exc)})
            connection.close()
            return None
        return connection, request

    def close(self) -> None:
        if self.sock is None:
            return
        self.sock.close()
        self.sock = None
        try:
            self.path.unlink()
        except (FileNotFoundError, AttributeError):
            pass


def _peer_closed(connection) -> bool:
    """True once the client of a control connection has gone away. Never
    waits: the connection keeps the 2 s timeout ControlServer gave it."""
    try:
        readable, _, _ = select.select([connection], [], [], 0)
        if not readable:
            return False
        return connection.recv(1, socket.MSG_PEEK) == b""
    except (BlockingIOError, InterruptedError, socket.timeout):
        return False
    except (OSError, ValueError):
        return True


def send_line(connection, payload: dict) -> None:
    try:
        connection.sendall((json.dumps(payload, sort_keys=True) + "\n")
                           .encode("utf-8"))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# The relay
# ---------------------------------------------------------------------------

class UpdateRequested(Exception):
    """A checked package is ready to install: leave the session first."""

    def __init__(self, connection, request, prepared=None):
        super().__init__("update requested")
        self.connection = connection
        self.request = request
        self.prepared = prepared


class Relay:
    def __init__(self, args, *, serial_module=None, list_ports=None):
        self.args = args
        self.serial = serial_module
        self.list_ports = list_ports
        self.pty_dir = Path(args.pty_dir)
        run_dir = Path(args.run_dir) if args.run_dir else None
        if run_dir is not None:
            run_dir.mkdir(parents=True, exist_ok=True)
        self.status = StatusFile(run_dir / STATUS_FILE if run_dir else None)
        self.control = ControlServer(run_dir / CONTROL_SOCKET
                                     if run_dir else None)
        self.log = GatewaySupportLog(Path(args.log_file).expanduser())
        self.core = None
        self.started = time.monotonic()
        self.ready_sent = False
        self.last_reason = None
        self.bridge = None
        # The support snapshot being asked for, the control connections
        # waiting for it, and when a request last went to the bridge.
        self.snapshot_query = None
        self.snapshot_waiters = []
        self.snapshot_sent_at = None
        self.ams = None              # the running AMS firmware update
        # A fresh "is the bridge busy?" question (bridge_info.BusyProbe), the
        # control connections waiting for it, and when an identity request
        # last went to the bridge (it answers RateLimited under 250 ms).
        self.busy_probe = None
        self.busy_waiters = []
        self.identity_sent_at = None
        self.attest_sent_at = None
        self.identity_query = None   # the session's own version query
        # An update request waiting for that answer: (connection, request,
        # prepared). A busy bridge would refuse update mode, and leaving the
        # session for it costs Klipper its AMS, so it is asked first.
        self.pending_update = None
        # The CyberBrick kit (cortex_bridge.kit, bridge 40 and later):
        # whether its service was asked for (it is then turned on at each
        # session), whether this session has it, the requests to send, the
        # control connections waiting for their answer, and the latest
        # report.
        self.kit_service = False
        self.kit_given = False
        self.kit_outbox = []
        self.kit_waiters = []
        self.kit_state = None

    # -- lifecycle --------------------------------------------------------

    def notify_ready(self, status: str) -> None:
        if not self.ready_sent:
            systemd_notify("READY=1\nSTATUS=%s" % status)
            self.ready_sent = True
        else:
            systemd_notify("STATUS=%s" % status)

    def waiting(self, error: RelayError) -> None:
        """Record a wait reason once per change, not once per retry."""
        if error.reason != self.last_reason:
            self.log.record("bridge_waiting", reason=error.reason)
            print("cortex-bridge: %s" % error, file=sys.stderr, flush=True)
            self.last_reason = error.reason
        self.status.set(state="waiting", reason=error.reason,
                        detail=str(error), bridge=None, firmware=None)
        # A printer without a reachable bridge must not hold Klipper back.
        if error.reason != "no-roster" or \
                time.monotonic() - self.started >= READY_WAIT:
            self.notify_ready("waiting: %s" % error)

    def units(self) -> list:
        if self.core is None:
            return []
        return [
            {"uid": uid.hex(), "online": endpoint.online,
             "path": str(endpoint.path)}
            for uid, endpoint in sorted(self.core.nodes.items())
        ]

    def run(self) -> None:
        self.log.record(
            "service_start", daemon_version=DAEMON_VERSION,
            runtime_protocol=RUNTIME_MUX_VERSION,
            port_selection="auto" if self.args.port == "auto" else
            "configured", pty_directory=str(self.pty_dir.resolve()))
        self.core = CortexCore(self.pty_dir.resolve(),
                               report_event=self.log.record)
        self.status.set(code=code_version.snapshot())
        try:
            while True:
                try:
                    self.session()
                    continue
                except UpdateRequested as request:
                    self.update(request.connection, request.prepared)
                    continue
                except RelayError as exc:
                    self.waiting(exc)
                except (ProtocolError, OSError) as exc:
                    reason = "runtime-protocol" if \
                        isinstance(exc, ProtocolError) else "disconnected"
                    self.log.record("session_retry", phase="runtime",
                                    reason=reason)
                    self.waiting(RelayError(reason, str(exc)))
                except Exception as exc:
                    if exc.__class__.__name__ != "SerialException":
                        raise
                    self.log.record("session_retry", phase="connect",
                                    reason="serial-error")
                    self.waiting(RelayError("serial-error", str(exc)))
                # Outside the handlers: a control request that arrives while
                # waiting must reach the update path, not escape the loop.
                try:
                    self.idle(RETRY_INTERVAL)
                except UpdateRequested as request:
                    self.update(request.connection, request.prepared)
        finally:
            self.status.set(state="stopped", detail="relay stopped",
                            units=[])
            self.core.close()
            self.control.close()
            self.log.record("service_stop")
            self.log.close()

    def idle(self, seconds: float) -> None:
        """Sleep, answering control requests meanwhile."""
        deadline = time.monotonic() + seconds
        while True:
            self.serve_control(running=False)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.1, remaining))

    def serve_control(self, *, running: bool) -> None:
        polled = self.control.poll()
        if polled is None:
            return
        connection, request = polled
        method = request.get("method")
        if method == "status":
            send_line(connection, dict(self.status.state, event="status",
                                       units=self.units()))
            connection.close()
        elif method == "update":
            # Check the request while the session still serves the AMS: a
            # request that needs nothing from the bridge must not cost
            # Klipper its connection to the units.
            prepared = self.prepare_update(connection, request)
            if prepared is None:
                return
            if self.pending_update is not None:
                send_line(connection, {
                    "event": "result", "ok": False, "error": "busy",
                    "message": "another update request is being checked"})
                connection.close()
                return
            if running and self.core is not None and \
                    (self.bridge or {}).get("image") == "production":
                self.pending_update = (connection, request, prepared)
                self.start_busy_probe()
                return
            raise UpdateRequested(connection, request, prepared)
        elif method == bridge_snapshot.SNAPSHOT_METHOD:
            self.request_snapshot(connection, running=running)
        elif method == AMS_UPDATE_METHOD:
            self.start_ams_update(connection, request, running=running)
        elif method == bridge_info.BUSY_METHOD:
            self.request_busy(connection, running=running)
        elif method == AMS_LISTEN_METHOD:
            self.start_ams_listen(connection, request, running=running)
        elif method == kit_protocol.METHOD:
            self.request_kit(connection, request, running=running)
        else:
            send_line(connection, {"event": "result", "ok": False,
                                   "error": "unknown-method",
                                   "message": "unknown method %r" % method})
            connection.close()

    # -- the CyberBrick kit (bridge 40 and later) -----------------------------

    def kit_supported(self) -> bool:
        firmware = self.status.state.get("firmware") or {}
        version = firmware.get("version") \
            if firmware.get("state") == "known" else None
        return (self.bridge or {}).get("image") == "production" and \
            isinstance(version, int) and version >= kit_protocol.MIN_FIRMWARE

    def request_kit(self, connection, request, *, running: bool) -> None:
        """Answer {"method": "kit"} with the latest report, {"method":
        "kit", "service": true|false} once the bridge turned the kit service
        on or off, and {"method": "kit", "shot": p} with the shot's status.
        A bridge before 40 is never sent a kit request."""
        def refuse(error, message):
            self._answer(connection, {"event": "kit", "ok": False,
                                      "error": error, "message": message})
        if not running or self.core is None:
            return refuse("not-connected", "no bridge session is running")
        if not self.kit_supported():
            firmware = self.status.state.get("firmware") or {}
            return refuse("unsupported-firmware",
                          "bridge firmware %s has no CyberBrick support; "
                          "version %d or later has"
                          % (firmware.get("version", "unknown"),
                             kit_protocol.MIN_FIRMWARE))
        try:
            if "service" in request:
                on = bool(request["service"])
                self.kit_service = on
                self.kit_given = True
                message, event = kit_protocol.service(on), "service"
            elif "shot" in request:
                message, event = kit_protocol.shot(
                    int(request.get("shot", 1))), "shot"
            else:
                state = self.kit_state
                if state is None:
                    return refuse("no-report", "the bridge has not reported "
                                  "the kit yet (its service is not on)")
                return self._answer(connection,
                                    dict(state, event="kit", ok=True))
        except (TypeError, ValueError) as exc:
            return refuse("bad-request", str(exc))
        self.kit_outbox.append(message)
        self.kit_waiters.append((connection, event,
                                 time.monotonic() + KIT_ANSWER_WAIT))

    def drive_kit(self, daemon, now: float) -> None:
        if not self.kit_supported():
            return
        if not self.kit_given and self.kit_service:
            # A new session: the kit's service again, as it was asked for.
            self.kit_outbox.insert(0, kit_protocol.service())
            self.kit_given = True
        while self.kit_outbox:
            daemon.send_request(self.kit_outbox.pop(0))
        late = [w for w in self.kit_waiters if w[2] <= now]
        if late:
            self.kit_waiters = [w for w in self.kit_waiters if w[2] > now]
            for connection, _event, _deadline in late:
                self._answer(connection, {
                    "event": "kit", "ok": False, "error": "no-answer",
                    "message": "the bridge did not answer the kit request"})

    def accept_kit(self, payload: bytes) -> None:
        """A KitReport: the status file's "kit" entry (presence, shots, the
        time of the last shot), the answer to whoever waits for it, and one
        log line when the kit comes or goes."""
        state = kit_protocol.decode(payload)
        previous = self.kit_state or {}
        if state["shots"] != previous.get("shots", state["shots"]) or \
                (state["shots"] and "last_shot" not in previous):
            state["last_shot"] = _now_iso()
        elif "last_shot" in previous:
            state["last_shot"] = previous["last_shot"]
        self.kit_state = {k: v for k, v in state.items() if k != "event"}
        self.status.set(kit=self.kit_state)
        if state["present"] != previous.get("present", False):
            self.log.record("kit_presence", present=state["present"])
        event = state["event"]
        for i, (connection, wanted, _deadline) in enumerate(self.kit_waiters):
            if wanted == event:
                del self.kit_waiters[i]
                reply = dict(self.kit_state, event="kit")
                reply["ok"] = event != "shot" or state.get("shot") == "queued"
                if event == "shot":
                    reply["shot"] = state.get("shot")
                self._answer(connection, reply)
                break

    def end_kit(self) -> None:
        self.kit_given = False
        self.kit_outbox = []
        self.kit_state = None
        self.status.set(kit=None)
        waiters, self.kit_waiters = self.kit_waiters, []
        for connection, _event, _deadline in waiters:
            self._answer(connection, {
                "event": "kit", "ok": False, "error": "not-connected",
                "message": "the bridge session ended before it answered"})

    # -- the support snapshot ----------------------------------------------

    def request_snapshot(self, connection, *, running: bool) -> None:
        """Answer {"method": "support_snapshot"}: ask the bridge, or say why
        not. Only a bridge the gate allows is ever sent the request; any
        other would drop its AMS units on it."""
        if not running or self.core is None:
            self._answer(connection, bridge_snapshot.failure(
                "not-connected", "no bridge session is running"))
            return
        allowed, error, message = bridge_snapshot.gate(
            self.bridge, self.status.state.get("firmware"))
        if not allowed:
            extra = {"minimum_firmware": bridge_snapshot.MINIMUM_FIRMWARE} \
                if error == "unsupported-firmware" else {}
            self._answer(connection, bridge_snapshot.failure(
                error, message, **extra))
            return
        self.snapshot_waiters.append(connection)
        if self.snapshot_query is None:
            not_before = 0.0 if self.snapshot_sent_at is None else \
                self.snapshot_sent_at + bridge_snapshot.MIN_INTERVAL_S
            self.snapshot_query = bridge_snapshot.SnapshotQuery(
                time.monotonic(), not_before=not_before)

    def drive_snapshot(self, daemon, now: float) -> None:
        query = self.snapshot_query
        if query is None:
            return
        request = query.due(now)
        if request is not None:
            daemon.send_request(request)
            self.snapshot_sent_at = now
        if query.finished:
            self.finish_snapshot(query.reply)

    def accept_snapshot(self, payload: bytes) -> None:
        if self.snapshot_query is not None:
            self.snapshot_query.accept(payload, time.monotonic())

    def finish_snapshot(self, reply: dict) -> None:
        self.log.record("bridge_snapshot", result="ok" if reply.get("ok")
                        else reply.get("error") or "failed")
        waiters, self.snapshot_waiters = self.snapshot_waiters, []
        self.snapshot_query = None
        for connection in waiters:
            self._answer(connection, reply)

    # -- is the bridge busy now? -------------------------------------------

    def request_busy(self, connection, *, running: bool) -> None:
        """Answer {"method": "bridge_busy"}: whether an AMS operation is
        active on the bridge now, which would make it refuse update mode.
        Asked before an update, so a busy bridge never costs Klipper its
        AMS. Only production firmware knows the identity request; any other
        would treat it as a stream error."""
        def refuse(error, message):
            self._answer(connection, {"event": "bridge_busy", "ok": False,
                                      "error": error, "message": message})
        if not running or self.core is None:
            return refuse("not-connected", "no bridge session is running")
        if (self.bridge or {}).get("image") != "production":
            return refuse("not-production", "only a bridge running "
                          "production firmware can be asked")
        self.busy_waiters.append(connection)
        self.start_busy_probe()

    def start_busy_probe(self) -> None:
        """Identity for bridges 36 and 37 (or an unknown version), an
        attestation from bridge 38 (bridge_info.BusyProbe)."""
        if self.busy_probe is not None:
            return
        firmware = self.status.state.get("firmware") or {}
        version = firmware.get("version") \
            if firmware.get("state") == "known" else None
        attest = isinstance(version, int) and \
            version >= bridge_info.ATTEST_MIN_VERSION
        last = self.attest_sent_at if attest else self.identity_sent_at
        gap = bridge_info.ATTEST_GAP_S if attest else \
            bridge_info.IDENTITY_MIN_GAP_S
        self.busy_probe = bridge_info.BusyProbe(
            time.monotonic(), version=version,
            not_before=0.0 if last is None else last + gap)

    def drive_busy(self, daemon, now: float) -> None:
        probe = self.busy_probe
        if probe is None:
            return
        query = self.identity_query
        # The bridge takes one request at a time and drops another.
        request = None if query is not None and query.token is not None \
            else probe.due(now)
        if request is not None:
            daemon.send_request(request)
            if probe.attest:
                self.attest_sent_at = now
            else:
                self.identity_sent_at = now
        if not probe.finished:
            return
        reply = probe.reply
        self.finish_busy(reply)
        pending, self.pending_update = self.pending_update, None
        if pending is None:
            return
        connection, request, prepared = pending
        # Busy: turned down with nothing touched. Idle, or the bridge could
        # not say: the update runs (the bridge's own refusal is the
        # backstop).
        if reply.get("ok") and reply.get("busy"):
            self.refuse_busy(connection, prepared)
        else:
            raise UpdateRequested(connection, request, prepared)

    def finish_busy(self, reply: dict) -> None:
        """Answer everyone waiting for the probe."""
        waiters, self.busy_waiters = self.busy_waiters, []
        self.busy_probe = None
        for connection in waiters:
            self._answer(connection, reply)

    def refuse_busy(self, connection, prepared) -> None:
        """Turn an update down because the bridge is busy, before anything
        is touched: Klipper keeps its AMS."""
        version, running = prepared["version"], prepared["running"]
        explanation = update_messages.explain(
            "BridgeBusy", running=running, package=version)
        update = dict(self.status.state.get("update") or {})
        update.update(result="failed", finished=_now_iso(),
                      message="the bridge is busy", version=version,
                      **explanation)
        self.status.set(update=update)
        self.log.record("update_result", firmware_version=version,
                        result="refused", reason="bridge-busy")
        line = {"event": "result", "ok": False, "error": "BridgeBusy",
                "message": "the bridge is busy: an AMS operation is active",
                "reason_code": "busy", "post_state": "application",
                "running_version": running, "package_version": version}
        line.update(explanation)
        send_line(connection, line)
        connection.close()

    def accept_attest(self, payload: bytes) -> None:
        if self.busy_probe is not None:
            self.busy_probe.accept_attest(payload, time.monotonic())

    def accept_identity(self, query, payload: bytes) -> None:
        """An identity report: the session's version query or a busy probe,
        whichever asked (each knows its own token)."""
        now = time.monotonic()
        if query is not None:
            query.accept(payload, now)
        probe = self.busy_probe
        if probe is not None and probe.accept(payload, now) and \
                probe.report is not None and query is not None and \
                not query.finished:
            # An idle answer is a whole report: the version the session's
            # own query (still waiting while the bridge was busy) asks for.
            query.firmware = bridge_info.firmware_status("known", "",
                                                         probe.report)

    @staticmethod
    def _answer(connection, reply: dict) -> None:
        send_line(connection, reply)
        connection.close()

    # -- an AMS firmware update ------------------------------------------

    def _ams_gate(self, connection, running: bool) -> bool:
        """Refuse (and answer) unless this bridge can take AMS update
        messages; True when it can."""
        def refuse(error, message):
            self._answer(connection, {"event": "result", "ok": False,
                                      "code": error, "message": message})
            return False
        if not running or self.core is None:
            return refuse("not-connected", "no bridge session is running")
        if self.ams is not None:
            return refuse("busy", "another AMS firmware update is running")
        firmware = self.status.state.get("firmware") or {}
        bridge = self.bridge or {}
        version = firmware.get("version")
        if bridge.get("manufacturer") != "SiForge" or \
                firmware.get("state") != "known" or \
                not isinstance(version, int):
            return refuse("firmware-unknown", "the bridge has not reported "
                          "its firmware version, so it is not asked")
        if version < ams_update.MINIMUM_FIRMWARE:
            return refuse("unsupported-firmware",
                          "bridge firmware %d cannot update an AMS; version "
                          "%d or later can (cortex-bridge.py update)"
                          % (version, ams_update.MINIMUM_FIRMWARE))
        return True

    def start_ams_listen(self, connection, request, *, running: bool) -> None:
        """Answer {"method": "ams_listen", "seconds": n}: stream every AMS
        loader line on the bus for n seconds (1-120), no session opened."""
        if not self._ams_gate(connection, running):
            return
        try:
            seconds = min(120.0, max(1.0, float(request.get("seconds", 10))))
        except (TypeError, ValueError):
            seconds = 10.0
        listener = ams_update.AmsListener(now=time.monotonic(),
                                          seconds=seconds)
        self.status.set(ams_update=True)
        self.ams = {"driver": listener, "connection": connection,
                    "finished_at": None, "listen": True}
        self.core.ams_update_handler = \
            lambda payload: listener.accept(payload, time.monotonic())

    def start_ams_update(self, connection, request, *, running: bool) -> None:
        """Answer {"method": "ams_update", "image": path, "index": n,
        "family": "ams2", "model": "N3F05"}: run one AMS firmware update
        through the bridge, streaming its progress as JSON lines. The image
        was checked by the caller (cortex_bridge.ams_firmware); it is read
        here and only its BIMH magic and size are checked again."""
        def refuse(error, message):
            self._answer(connection, {"event": "result", "ok": False,
                                      "code": error, "message": message})
        if not running or self.core is None:
            return refuse("not-connected", "no bridge session is running")
        if self.ams is not None:
            return refuse("busy", "another AMS firmware update is running")
        firmware = self.status.state.get("firmware") or {}
        bridge = self.bridge or {}
        version = firmware.get("version")
        if bridge.get("manufacturer") != "SiForge" or \
                firmware.get("state") != "known" or \
                not isinstance(version, int):
            return refuse("firmware-unknown", "the bridge has not reported "
                          "its firmware version, so it is not asked")
        if version < ams_update.MINIMUM_FIRMWARE:
            return refuse("unsupported-firmware",
                          "bridge firmware %d cannot update an AMS; version "
                          "%d or later can (cortex-bridge.py update)"
                          % (version, ams_update.MINIMUM_FIRMWARE))
        try:
            index = int(request["index"])
            family = str(request["family"])
            model = str(request["model"])
            with open(str(request["image"]), "rb") as handle:
                image = handle.read(AMS_IMAGE_MAX + 1)
        except (KeyError, TypeError, ValueError) as exc:
            return refuse("bad-request", "bad AMS update request: %s" % exc)
        except OSError as exc:
            return refuse("bad-image", "cannot read the image: %s"
                          % (exc.strerror or exc))
        if len(image) > AMS_IMAGE_MAX or len(image) < 512 or \
                image[:4] != b"BIMH" or \
                int.from_bytes(image[8:12], "little") != len(image):
            return refuse("bad-image", "the file is not a complete AMS image")
        resume = bool(request.get("resume"))
        if resume and version < ams_update.RESUME_MINIMUM_FIRMWARE:
            return refuse("unsupported-firmware",
                          "bridge firmware %d cannot resume an AMS waiting in "
                          "its loader; version %d or later can"
                          % (version, ams_update.RESUME_MINIMUM_FIRMWARE))
        try:
            driver = ams_update.AmsUpdateDriver(
                image, index=index, family=family, model=model,
                now=time.monotonic(), probe=bool(request.get("probe")),
                resume=resume)
            ams_update.make_begin(driver.token, index, family, len(image),
                                  model)
        except ams_update.AmsUpdateError as exc:
            return refuse("bad-request", str(exc))
        self.status.set(ams_update=True)
        self.ams = {"driver": driver, "connection": connection,
                    "finished_at": None}
        self.core.ams_update_handler = \
            lambda payload: driver.accept(payload, time.monotonic())
        self.log_ams("ams_update_start", index=index,
                     family=family.replace("_", "-"), model=model.lower(),
                     size=len(image))

    def log_ams(self, event: str, **fields) -> None:
        """A support-log line that can never stop an update."""
        try:
            self.log.record(event, **fields)
        except (ValueError, OSError):
            pass

    def drive_ams_update(self, daemon, now: float) -> None:
        state = self.ams
        if state is None:
            return
        driver = state["driver"]
        if not driver.finished and _peer_closed(state["connection"]):
            driver.abort()        # the command was interrupted
        for payload in driver.due(now):
            daemon.send_request(Message(MessageType.AMS_UPDATE_REQUEST,
                                        payload))
        events, driver.events = driver.events, []
        for event in events:
            try:
                send_line(state["connection"], event)
            except OSError:
                pass
        if driver.finished:
            if state["finished_at"] is None and state.get("listen"):
                state["finished_at"] = now
            if state["finished_at"] is None:
                state["finished_at"] = now
                outcome = driver.outcome
                self.log_ams("ams_update_end",
                             result="ok" if outcome.ok else "failed",
                             reason=outcome.code or "none",
                             before=outcome.before or "none",
                             after=outcome.after or "none")
            # Wait for the bridge's after-session units report, then close.
            if driver.final_units is not None or \
                    now - state["finished_at"] > AMS_UNITS_WAIT_S:
                self.end_ams_update()

    def end_ams_update(self, *, reason: str = "") -> None:
        state, self.ams = self.ams, None
        if state is None:
            return
        self.status.set(ams_update=False)
        if self.core is not None:
            self.core.ams_update_handler = None
        driver = state["driver"]
        if not driver.finished:
            driver._finish(False, "host-lost", reason or
                           "the bridge session ended during the update")
        for event in driver.events:
            try:
                send_line(state["connection"], event)
            except OSError:
                pass
        driver.events = []
        try:
            state["connection"].close()
        except OSError:
            pass

    # -- one USB session --------------------------------------------------

    def _configured_port(self) -> dict:
        """The --port device, with its USB strings when they can be read."""
        bridge = {"device": self.args.port, "serial": "", "pid": None,
                  "manufacturer": "", "product": ""}
        try:
            wanted = os.path.realpath(self.args.port)
            for port in describe_ports(self.list_ports):
                if os.path.realpath(port["device"]) == wanted:
                    bridge.update(port, device=self.args.port)
                    break
        except Exception:  # noqa: BLE001 - the strings are optional here
            pass
        return bridge

    def session(self) -> None:
        if self.args.port == "auto":
            bridge = find_bridge(self.list_ports, serial_pattern=self.args.serial)
        else:
            bridge = self._configured_port()
        bridge["image"] = IMAGE_BY_MANUFACTURER.get(bridge["manufacturer"],
                                                    "unknown")
        self.bridge = bridge
        # Only production firmware knows the identity request; lab firmware
        # treats it as a stream error, so it is never sent one.
        if bridge["image"] == "production":
            firmware = bridge_info.firmware_status("querying", "querying")
        else:
            firmware = bridge_info.firmware_status(
                "unavailable", "lab-firmware" if bridge["image"] == "lab"
                else "unknown-image")
        self.status.set(state="connecting", reason="", bridge=bridge,
                        firmware=firmware, detail="connecting to the bridge")
        self.log.record("usb_connect", device=bridge["device"])
        port = open_bridge_port(self.serial, bridge["device"])
        try:
            self.log.record("usb_connected", device=bridge["device"])
            initial = wait_for_roster(port)
            port.timeout = 0
            port.write_timeout = 0
            transport = CleartextMuxTransport.connect(port, initial)
            port = None
        finally:
            if port is not None:
                port.close()
        self.log.record("runtime_started",
                        runtime_protocol=RUNTIME_MUX_VERSION)
        self.last_reason = None
        daemon = CortexDaemon(transport, self.pty_dir.resolve(),
                              core=self.core)
        query = None
        if bridge["image"] == "production":
            query = bridge_info.IdentityQuery(time.monotonic())
            self.identity_query = query
            self.core.identity_handler = \
                lambda payload: self.accept_identity(query, payload)
            self.core.attest_handler = self.accept_attest
            self.core.support_handler = self.accept_snapshot
            self.core.kit_handler = self.accept_kit
        reported = query.firmware if query is not None else None
        summary_at = time.monotonic() + SUMMARY_INTERVAL
        status_at = 0.0
        try:
            while True:
                daemon.step(0.1)
                now = time.monotonic()
                if query is not None:
                    probe = self.busy_probe
                    # One request at a time: not while a probe's is out.
                    request = query.due(now) if probe is None or \
                        probe.token is None else None
                    if request is not None:
                        daemon.send_request(request)
                        self.identity_sent_at = now
                    if query.firmware is not reported:
                        reported = query.firmware
                        self.status.set(firmware=reported)
                        if query.finished:
                            self.log_identity(reported)
                units = self.units()
                online = sum(1 for unit in units if unit["online"])
                if online or now - self.started >= READY_WAIT:
                    self.notify_ready("%d AMS online" % online)
                if now >= status_at:
                    detail = "%d AMS online" % online if online else \
                        "bridge connected; no AMS answering"
                    self.status.set(state="running", reason="", units=units,
                                    detail=detail)
                    status_at = now + STATUS_INTERVAL
                if now >= summary_at:
                    self.log.record("traffic_summary",
                                    **self.core.support_snapshot())
                    summary_at = now + SUMMARY_INTERVAL
                self.drive_snapshot(daemon, now)
                self.drive_busy(daemon, now)
                self.drive_ams_update(daemon, now)
                self.drive_kit(daemon, now)
                self.serve_control(running=True)
        finally:
            self.end_ams_update(reason="the bridge session ended during the "
                                "update; the AMS may wait in its loader: run "
                                "the update again")
            self.core.identity_handler = None
            self.core.attest_handler = None
            self.identity_query = None
            self.core.support_handler = None
            self.core.kit_handler = None
            self.end_kit()
            if self.snapshot_query is not None:
                self.finish_snapshot(bridge_snapshot.failure(
                    "not-connected", "the bridge session ended before it "
                    "answered"))
            if self.busy_probe is not None:
                self.finish_busy({"event": "bridge_busy", "ok": False,
                                  "error": "not-connected",
                                  "message": "the bridge session ended before "
                                  "it answered"})
            if self.pending_update is not None:
                connection = self.pending_update[0]
                self.pending_update = None
                send_line(connection, {
                    "event": "result", "ok": False, "error": "not-connected",
                    "message": "the bridge session ended before the update "
                               "started; nothing was changed: run it again"})
                connection.close()
            self.identity_sent_at = None
            self.log.record("traffic_summary", **self.core.support_snapshot())
            daemon.detach()
            self.status.set(units=self.units())

    def log_identity(self, firmware: dict) -> None:
        known = firmware["state"] == "known"
        self.log.record(
            "bridge_identity",
            result="known" if known else (firmware["reason"] or "unavailable"),
            firmware_version=int(firmware["version"] or 0) if known else 0,
            boot_abi=int(firmware["boot_abi"] or 0) if known else 0,
            genuine_status=int(firmware["genuine_code"] or 0) if known else 0,
            reset_flags=int(firmware["reset_flags"] or 0) if known else 0,
            uptime_ms=int(firmware["uptime_ms"] or 0) if known else 0)

    # -- firmware update --------------------------------------------------

    def running_version(self):
        """The version the connected bridge said it runs, or None."""
        firmware = self.status.state.get("firmware") or {}
        if firmware.get("state") != "known":
            return None
        return firmware.get("version")

    def prepare_update(self, connection, request):
        """Check an update request while the session still runs.

        Returns what ``update`` needs, or None after answering the client
        itself: a file that is not bridge firmware, or a version that is not
        newer than the running one, never takes the AMS offline."""
        try:
            from cortex_bridge import updater
        except ImportError as exc:  # pragma: no cover - packaging error
            send_line(connection, {"event": "result", "ok": False,
                                   "error": "no-updater",
                                   "message": str(exc)})
            connection.close()
            return None
        running = self.running_version()
        version = 0
        try:
            path = Path(str(request.get("file", ""))).expanduser()
            if not path.is_file():
                raise _refused(updater, "there is no file %s" % path)
            if path.stat().st_size > MAX_PACKAGE_SIZE:
                raise _refused(updater, "%s is too large to be bridge "
                               "firmware" % path)
            data = path.read_bytes()
            info = updater.inspect_package(data)
            version = int(info.get("version") or 0)
        except Exception as exc:  # report every failure to the client
            self.update_failed(connection, exc, running, version,
                               touched=False)
            connection.close()
            return None
        if running is not None and version <= running:
            self.not_newer(connection, version, running)
            connection.close()
            return None
        source = request.get("source", "file")
        return {"updater": updater, "data": data, "version": version,
                "running": running,
                "source": source if source in ("bundled", "file") else "file"}

    def update(self, connection, prepared) -> None:
        """Install one checked package while no session holds the port."""
        updater = prepared["updater"]
        version = prepared["version"]
        running = prepared["running"]
        try:
            self.log.record("update_start", firmware_version=version,
                            source=prepared["source"])
            self.status.set(state="updating", detail="installing firmware",
                            update={"version": version, "phase": "starting",
                                    "done": 0, "total": 0, "result": None,
                                    "message": "", "started": _now_iso(),
                                    "previous_version": running})
            send_line(connection, {"event": "accepted", "version": version,
                                   "running_version": running})

            last_phase = [None]

            def progress(phase, done, total):
                update = dict(self.status.state.get("update") or {})
                update.update(phase=phase, done=done, total=total)
                self.status.set(update=update,
                                detail="installing firmware: %s" % phase)
                if phase != last_phase[0]:
                    self.log.record("update_phase", phase=phase)
                    last_phase[0] = phase
                send_line(connection, {"event": "progress", "phase": phase,
                                       "done": done, "total": total})

            serial_number = (self.bridge or {}).get("serial") or None

            def repeating(attempt, attempts, error):
                # A reason code, as the schema allows: the error's text may
                # name devices or paths the support log never carries.
                self.log.record("update_retry", attempt=attempt,
                                attempts=attempts,
                                reason=_reason_code(error.__class__.__name__))
                self.status.set(detail="the bridge did not accept the update "
                                "this time; repeating it (attempt %d of %d)"
                                % (attempt + 1, attempts))
                send_line(connection, {"event": "retry", "attempt": attempt + 1,
                                       "attempts": attempts})

            report = updater.update_bridge_with_retry(
                prepared["data"], serial_number=serial_number,
                serial_module=self.serial, progress=progress,
                on_retry=repeating)
            update = dict(self.status.state.get("update") or {})
            update.update(result="installed", finished=_now_iso(),
                          message="installed version %d" % version)
            # The new firmware is asked for its version when it reconnects.
            self.status.set(state="waiting", detail="firmware installed",
                            update=update, firmware=None)
            self.log.record("update_result", firmware_version=version,
                            result="installed", reason="none")
            send_line(connection, {"event": "result", "ok": True,
                                   "changed": True, "version": version,
                                   "previous_version": running,
                                   "report": report})
        except Exception as exc:  # report every failure to the client
            self.update_failed(connection, exc, running, version, touched=True)
        finally:
            connection.close()

    def update_failed(self, connection, exc, running, version: int, *,
                      touched: bool) -> None:
        reason = getattr(exc, "reason", None) or \
            _reason_code(exc.__class__.__name__)
        explanation = update_messages.explain_exception(
            exc, running=running, package=version or None)
        update = dict(self.status.state.get("update") or {})
        update.update(result="failed", finished=_now_iso(),
                      message=str(exc), version=version, **explanation)
        if touched:
            self.status.set(state="waiting", detail="firmware update failed",
                            update=update)
        else:
            self.status.set(update=update)
        self.log.record("update_result", firmware_version=version,
                        result="failed", reason=reason)
        line = {"event": "result", "ok": False,
                "error": exc.__class__.__name__, "message": str(exc),
                "reason_code": getattr(exc, "reason_code", "") or "",
                "post_state": getattr(exc, "post_state", "") or "",
                "running_version": running,
                "package_version": version or None}
        line.update(explanation)
        send_line(connection, line)

    def not_newer(self, connection, version: int, running: int) -> None:
        """Answer an update to a version that is not newer than the running
        one without touching the bridge: its boot would refuse it."""
        same = version == running
        error = "AlreadyInstalled" if same else "OlderThanInstalled"
        explanation = update_messages.explain(error, running=running,
                                              package=version)
        self.log.record("update_result", firmware_version=version,
                        result="not-needed" if same else "refused",
                        reason="already-installed" if same
                        else "older-than-installed")
        line = {"event": "result", "ok": same, "changed": False,
                "version": version, "running_version": running,
                "package_version": version}
        if not same:
            line.update(error=error, message="the bridge runs version %d, "
                        "newer than version %d" % (running, version))
        line.update(explanation)
        send_line(connection, line)


def _refused(updater, detail: str):
    error = updater.PackageRefused("refused: %s. Nothing was sent to the "
                                   "bridge" % detail)
    error.detail = detail
    return error


def _reason_code(name: str) -> str:
    out = []
    for char in name:
        if char.isupper() and out:
            out.append("-")
        out.append(char.lower())
    return "".join(out)[:63] or "error"


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Publish each AMS behind a Cortex Bridge as a Klipper "
                    "serial port.")
    result.add_argument("--port", default="auto",
                        help="the bridge's serial device, or auto (default)")
    result.add_argument("--serial", default=BRIDGE_SERIAL_PATTERN,
                        help="USB serial number pattern of the bridge to "
                             "serve (default %(default)s)")
    result.add_argument("--pty-dir", default=DEFAULT_PTY_DIR,
                        help="directory for the per-AMS links "
                             "(default %(default)s)")
    result.add_argument("--run-dir", default=DEFAULT_RUN_DIR,
                        help="directory for the status file and control "
                             "socket (default %(default)s); empty to disable")
    result.add_argument("--log-file", required=True,
                        help="support log (JSON lines, rotated at 2 MiB)")
    return result


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    import serial
    from serial.tools import list_ports

    previous = signal.signal(signal.SIGTERM, _request_stop)
    try:
        Relay(args, serial_module=serial, list_ports=list_ports).run()
    except RelayStop:
        pass
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGTERM, previous)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
