# SPDX-License-Identifier: MIT
"""The Cortex Bridge relay: one Klipper serial port per AMS unit.

The bridge carries one Klipper serial session per AMS unit over its single USB
serial port. This service owns that port and publishes each unit as a PTY,
linked as /dev/oams/<uid>, so Klipper sees an ordinary `[mcu]` serial device
per AMS. The bridge itself never appears in printer.cfg.

It also:
- writes a small status file (state, bridge serial, units) that the
  `cortex_bridge` command and the Moonraker component read;
- accepts firmware-update requests on a local socket and runs them itself,
  because it is the only process allowed to hold the bridge's port;
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
import signal
import socket
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cortex_bridge.cortexd import (  # noqa: E402
    CleartextMuxTransport,
    CortexCore,
    CortexDaemon,
    MessageType,
    ProtocolError,
    ROSTER_PAYLOAD_SIZE,
    VERSION as RUNTIME_MUX_VERSION,
)
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

ROSTER_TIMEOUT = 30.0
READY_WAIT = 60.0
SUMMARY_INTERVAL = 60.0
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
            "units": [],
            "update": None,
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

    Requests: {"method": "status"} and {"method": "update", "file": PATH}.
    Replies are JSON lines; an update streams progress lines and ends with a
    line whose "event" is "result".
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
    def __init__(self, connection, request):
        super().__init__("update requested")
        self.connection = connection
        self.request = request


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
                        detail=str(error), bridge=None)
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
        try:
            while True:
                try:
                    self.session()
                    continue
                except UpdateRequested as request:
                    self.update(request.connection, request.request)
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
                    self.update(request.connection, request.request)
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
            raise UpdateRequested(connection, request)
        else:
            send_line(connection, {"event": "result", "ok": False,
                                   "error": "unknown-method",
                                   "message": "unknown method %r" % method})
            connection.close()

    # -- one USB session --------------------------------------------------

    def session(self) -> None:
        if self.args.port == "auto":
            bridge = find_bridge(self.list_ports, serial_pattern=self.args.serial)
        else:
            bridge = {"device": self.args.port, "serial": "", "pid": None,
                      "manufacturer": "", "product": ""}
        bridge["image"] = IMAGE_BY_MANUFACTURER.get(bridge["manufacturer"],
                                                    "unknown")
        self.bridge = bridge
        self.status.set(state="connecting", reason="", bridge=bridge,
                        detail="connecting to the bridge")
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
        summary_at = time.monotonic() + SUMMARY_INTERVAL
        status_at = 0.0
        try:
            while True:
                daemon.step(0.1)
                now = time.monotonic()
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
                self.serve_control(running=True)
        finally:
            self.log.record("traffic_summary", **self.core.support_snapshot())
            daemon.detach()
            self.status.set(units=self.units())

    # -- firmware update --------------------------------------------------

    def update(self, connection, request) -> None:
        """Install one firmware package while no session holds the port."""
        try:
            from cortex_bridge import updater
        except ImportError as exc:  # pragma: no cover - packaging error
            send_line(connection, {"event": "result", "ok": False,
                                   "error": "no-updater",
                                   "message": str(exc)})
            connection.close()
            return
        version = 0
        source = request.get("source", "file")
        try:
            path = Path(str(request.get("file", ""))).expanduser()
            if not path.is_file():
                raise updater.PackageRefused("no such file: %s" % path)
            if path.stat().st_size > MAX_PACKAGE_SIZE:
                raise updater.PackageRefused("file is too large: %s" % path)
            data = path.read_bytes()
            info = updater.inspect_package(data)
            version = int(info.get("version") or 0)
            self.log.record("update_start", firmware_version=version,
                            source=source if source in ("bundled", "file")
                            else "file")
            self.status.set(state="updating", detail="installing firmware",
                            update={"version": version, "phase": "starting",
                                    "done": 0, "total": 0, "result": None,
                                    "message": "", "started": _now_iso()})
            send_line(connection, {"event": "accepted", "version": version})

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
            report = updater.update_bridge(
                data, serial_number=serial_number, serial_module=self.serial,
                progress=progress)
            update = dict(self.status.state.get("update") or {})
            update.update(result="installed", finished=_now_iso(),
                          message="installed version %d" % version)
            self.status.set(state="waiting", detail="firmware installed",
                            update=update)
            self.log.record("update_result", firmware_version=version,
                            result="installed", reason="none")
            send_line(connection, {"event": "result", "ok": True,
                                   "version": version, "report": report})
        except Exception as exc:  # report every failure to the client
            reason = getattr(exc, "reason", None) or \
                _reason_code(exc.__class__.__name__)
            update = dict(self.status.state.get("update") or {})
            update.update(result="failed", finished=_now_iso(),
                          message=str(exc), version=version)
            self.status.set(state="waiting", detail="firmware update failed",
                            update=update)
            self.log.record("update_result", firmware_version=version,
                            result="failed", reason=reason)
            send_line(connection, {"event": "result", "ok": False,
                                   "error": exc.__class__.__name__,
                                   "message": str(exc)})
        finally:
            connection.close()


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
