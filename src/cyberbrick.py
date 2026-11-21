# OpenAMS: the CyberBrick Time-lapse Kit through a Cortex Bridge relay.
#
# Copyright (C) 2026 JR Lomas <lomas.jr@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
# A Cortex Bridge from firmware 40 serves the kit; this host only asks its
# relay (cortex_bridge/relay.py, control socket) to turn the kit service on,
# to fire a shot, and for the kit's status. This module is the Klipper side:
#
#   CYBERBRICK_SHOT [P=1] [QUIET=1]   fire the kit
#   M1004 S5 P<n>                     the same, for Bambu-sliced G-code (any
#                                     other M1004 does nothing; S5 without P
#                                     sends nothing, as on the printer)
#   printer.cyberbrick                available, present, shots, kit_shots,
#                                     last_shot_time, last_shot_result,
#                                     rate_limited
#
# and, with cyberbrick_timelapse_frames, each moonraker-timelapse frame fires
# the kit too. The kit service is turned on when Klipper is ready and after
# every bridge re-attach, and turned off once at ready with cyberbrick: False.
# The kit is optional: with no relay, a bridge before 40, lab firmware or no
# kit, nothing is sent, nothing pauses and nothing is logged but the kit coming
# and going.
#
# A shot fires when the command runs, which is before the moves queued ahead
# of it: put M400 before it where the head must be still (Bambu's time-lapse
# G-code and moonraker-timelapse's parked frame already do).

import json
import logging
import os
import socket
import sys
import threading
import time

# The relay's definitions (cortex_bridge/kit.py) live at the root of this
# checkout; they need nothing but the standard library.
_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if _ROOT not in sys.path:
    sys.path.append(_ROOT)
try:
    from cortex_bridge import kit as kit_protocol
except Exception:                                  # pragma: no cover
    kit_protocol = None

# The relay's run directory (cortex_bridge/cli.py RUN_DIR), its control socket
# and its status file.
RUN_DIR = "/run/cortex-bridge"
CONTROL_SOCKET = "control.sock"
STATUS_FILE = "status.json"

# How often the relay's status file is read, how long a request to the relay
# may take, and how often the kit service is asked for again while the relay
# has no report of the kit.
STATUS_EVERY_S = 2.0
REQUEST_TIMEOUT_S = 3.0
SERVICE_RETRY_S = 30.0
# The shortest gap between shots the host sends.
MIN_INTERVAL_S = 0.5
TIMELAPSE_COMMAND = "_TIMELAPSE_NEW_FRAME"
# What the status object and the log may show of the relay's report.
STATUS_FIELDS = ("present", "shots")


class RelayClient(object):
    """The relay's status file and control socket, used from a worker thread
    (each call blocks for at most REQUEST_TIMEOUT_S)."""

    def __init__(self, run_dir=RUN_DIR):
        self.run_dir = run_dir

    def available(self):
        """Cheap, for the reactor: whether a relay runs here at all."""
        return os.path.isdir(self.run_dir)

    def read_status(self):
        try:
            with open(os.path.join(self.run_dir, STATUS_FILE)) as handle:
                status = json.load(handle)
        except (OSError, ValueError):
            return None
        return status if isinstance(status, dict) else None

    def request(self, request):
        """One control request; its final reply line, or an error reply."""
        path = os.path.join(self.run_dir, CONTROL_SOCKET)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(REQUEST_TIMEOUT_S)
                sock.connect(path)
                sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
                buffer, last = b"", None
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        if line.strip():
                            last = json.loads(line.decode("utf-8"))
                return last or {"ok": False, "error": "no-answer"}
        except (OSError, ValueError) as exc:
            return {"ok": False, "error": "relay-unreachable",
                    "message": str(exc)}


class ThreadWorker(object):
    """Runs blocking calls one at a time on a daemon thread and hands each
    result back on the reactor (register_async_callback)."""

    def __init__(self, reactor):
        self.reactor = reactor
        self._jobs = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread = None

    def submit(self, call, done):
        with self._lock:
            self._jobs.append((call, done))
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name="openams-cyberbrick", daemon=True)
                self._thread.start()
        self._wake.set()

    def _run(self):
        while True:
            self._wake.wait()
            with self._lock:
                if not self._jobs:
                    self._wake.clear()
                    continue
                call, done = self._jobs.pop(0)
            try:
                result = call()
            except Exception:
                logging.exception("OpenAMS: CyberBrick relay call failed")
                result = None
            self.reactor.register_async_callback(
                lambda eventtime, done=done, result=result: done(result))


def supported(status):
    """Whether the relay's bridge serves the kit (the relay's kit_supported:
    a running production bridge on firmware 40 or later)."""
    if not status or status.get("state") != "running" or kit_protocol is None:
        return False
    bridge = status.get("bridge") or {}
    firmware = status.get("firmware") or {}
    version = firmware.get("version") if firmware.get("state") == "known" \
        else None
    return (bridge.get("image") == "production" and isinstance(version, int)
            and version >= kit_protocol.MIN_FIRMWARE)


class CyberBrickKit(object):
    def __init__(self, printer, min_interval=MIN_INTERVAL_S,
                 timelapse_frames=False, client=None, worker=None):
        self.printer = printer
        self.reactor = printer.get_reactor()
        self.min_interval = min_interval
        self.timelapse_frames = timelapse_frames
        self.client = client if client is not None else RelayClient()
        self.worker = worker if worker is not None else ThreadWorker(
            self.reactor)
        self.gcode = None
        # What the relay said last.
        self._status = None
        self.supported = False
        self.kit = None
        self.present = None
        # Shots.
        self.shots = 0
        self.rate_limited = 0
        self.last_shot_time = None
        self.last_shot_result = None
        self._last_fire = None
        # The kit service: asked for at ready and after a re-attach, and
        # again (no faster than SERVICE_RETRY_S) while the relay has no
        # report of the kit.
        self._service_wanted = False
        self._service_tried = None
        self._service_busy = False
        self._reading = False
        self._timer = None
        self.m1004 = False
        self.timelapse_hooked = False
        printer.add_object("cyberbrick", self)
        printer.register_event_handler("klippy:connect", self._connect)
        printer.register_event_handler("klippy:ready", self._ready)

    # ------------------------------------------------------------ G-code

    def register_commands(self, gcode):
        self.gcode = gcode
        gcode.register_command("CYBERBRICK_SHOT", self.cmd_CYBERBRICK_SHOT,
                               desc=self.cmd_CYBERBRICK_SHOT_help)

    def _connect(self):
        """After every config object registered its commands: M1004, unless
        the printer's config has its own (a macro that silences Bambu
        G-code, say), and the moonraker-timelapse frame hook."""
        gcode = self.gcode or self.printer.lookup_object("gcode")
        self.gcode = gcode
        exists = getattr(gcode, "ready_gcode_handlers", None)
        if exists is not None and "M1004" in exists:
            logging.info("OpenAMS: M1004 is the printer config's own;"
                         " CYBERBRICK_SHOT fires the CyberBrick kit")
        else:
            gcode.register_command("M1004", self.cmd_M1004,
                                   desc=self.cmd_M1004_help)
            self.m1004 = True
        if self.timelapse_frames:
            self._hook_timelapse(gcode)

    def _hook_timelapse(self, gcode):
        """moonraker-timelapse signals each frame with its
        _TIMELAPSE_NEW_FRAME macro (after parking and M400 when it parks):
        run it, then fire the kit. Their files stay as they are; the hook
        is this option."""
        desc = getattr(gcode, "gcode_help", {}).get(TIMELAPSE_COMMAND)
        previous = gcode.register_command(TIMELAPSE_COMMAND, None)
        if previous is None:
            logging.info("OpenAMS: cyberbrick_timelapse_frames is set, but"
                         " moonraker-timelapse's %s macro is not loaded",
                         TIMELAPSE_COMMAND)
            return

        def frame(gcmd):
            previous(gcmd)
            self.fire(1)
        gcode.register_command(TIMELAPSE_COMMAND, frame, desc=desc)
        self.timelapse_hooked = True
        logging.info("OpenAMS: moonraker-timelapse frames fire the"
                     " CyberBrick kit")

    cmd_CYBERBRICK_SHOT_help = ("Fire the CyberBrick Time-lapse Kit through"
                                " the Cortex Bridge (P=1)")

    def cmd_CYBERBRICK_SHOT(self, gcmd):
        p = gcmd.get_int("P", 1, minval=0, maxval=255)
        quiet = gcmd.get_int("QUIET", 0, minval=0, maxval=1)
        why = self.fire(p)
        if not quiet:
            gcmd.respond_info("CyberBrick: %s" % (
                "shot sent (P=%d)" % p if why is None else why))

    cmd_M1004_help = "Bambu Lab extension board: S5 P<n> fires the CyberBrick"

    def cmd_M1004(self, gcmd):
        # Bambu's extension board command; S5 fires the kit. Any other S,
        # and S5 without P, does nothing, as on the printer.
        if gcmd.get_int("S", None) != 5:
            return
        p = gcmd.get_int("P", None)
        if p is None or not 0 <= p <= 255:
            return
        self.fire(p)

    # ------------------------------------------------------------ shots

    def fire(self, p):
        """Send one shot. Returns None when it was sent, else why not (the
        status object keeps the result either way). Never waits."""
        if not self.supported:
            self.last_shot_result = "unavailable"
            return "not available (%s)" % self._unavailable_reason()
        now = self.reactor.monotonic()
        if (self._last_fire is not None
                and now - self._last_fire < self.min_interval):
            self.rate_limited += 1
            self.last_shot_result = "rate-limited"
            return "too soon after the last shot (at most one every %.1f s)" \
                % self.min_interval
        self._last_fire = now
        self.worker.submit(
            lambda: self.client.request({"method": kit_protocol.METHOD,
                                         "shot": int(p)}),
            self._shot_done)
        return None

    def _shot_done(self, reply):
        reply = reply or {}
        if reply.get("ok"):
            self.shots += 1
            self.last_shot_time = time.time()
            self.last_shot_result = reply.get("shot") or "queued"
        else:
            self.last_shot_result = (reply.get("shot")
                                     or reply.get("error") or "failed")

    def _unavailable_reason(self):
        status = self._status or {}
        if not status:
            return "no Cortex Bridge relay is running"
        if status.get("state") != "running":
            return "the Cortex Bridge is not connected"
        if (status.get("bridge") or {}).get("image") != "production":
            return "the Cortex Bridge runs lab firmware"
        return "the kit needs Cortex Bridge firmware %d or later" % (
            kit_protocol.MIN_FIRMWARE if kit_protocol else 40)

    # ------------------------------------------------------------ service

    def bridge_reattached(self):
        """A bridge session (re)attached (ready, a re-attach, a unit back):
        ask for the kit service again."""
        self._service_wanted = True
        self._service_tried = None

    def _ask_service(self, now):
        if (not self._service_wanted or self._service_busy
                or not self.supported
                or (self._service_tried is not None
                    and now - self._service_tried < SERVICE_RETRY_S)):
            return
        self._service_busy = True
        self._service_tried = now
        self.worker.submit(
            lambda: self.client.request({"method": kit_protocol.METHOD,
                                         "service": True}),
            self._service_done)

    def _service_done(self, reply):
        self._service_busy = False
        if (reply or {}).get("ok"):
            self._service_wanted = False
            self._take_kit(reply)

    # ------------------------------------------------------------ status

    def _ready(self):
        self.bridge_reattached()
        if self._timer is None:
            self._timer = self.reactor.register_timer(
                self._read_relay, self.reactor.NOW)

    def _read_relay(self, eventtime):
        """Reactor timer: read the relay's status file on the worker (never
        here), and only where a relay runs at all."""
        if not self._reading:
            if self.client.available():
                self._reading = True
                self.worker.submit(self.client.read_status, self._status_read)
            elif self._status is not None or self.supported:
                self._status_read(None)
        return eventtime + STATUS_EVERY_S

    def _status_read(self, status):
        self._reading = False
        was = self.supported
        self._status = status
        self.supported = supported(status)
        if self.supported and not was:
            self.bridge_reattached()
        kit = (status or {}).get("kit") if self.supported else None
        if self.supported and kit is None:
            # No report of the kit this session: ask for the service.
            self._service_wanted = True
        self._take_kit(kit)
        self._ask_service(self.reactor.monotonic())

    def _take_kit(self, kit):
        """The kit's presence and shot count from the relay. Logs the kit
        coming and going, once each."""
        if isinstance(kit, dict) and "present" in kit:
            self.kit = {key: kit[key] for key in STATUS_FIELDS if key in kit}
            present = bool(kit["present"])
        elif self.supported:
            present = self.present      # no report yet this session
        else:
            self.kit = None
            present = None              # nothing serves the kit
        if present and not self.present:
            logging.info("OpenAMS: the CyberBrick kit is connected")
        elif self.present and not present:
            logging.info("OpenAMS: the CyberBrick kit is gone")
        self.present = present

    def get_status(self, eventtime=None):
        kit = self.kit or {}
        return {
            "available": self.supported,
            "present": bool(self.present),
            "kit_shots": kit.get("shots"),
            "shots": self.shots,
            "rate_limited": self.rate_limited,
            "last_shot_time": self.last_shot_time,
            "last_shot_result": self.last_shot_result,
        }


class ServiceOff(object):
    """cyberbrick: False: at ready, turn the kit service off once where a
    relay's bridge serves the kit; nothing else is sent or registered."""

    def __init__(self, printer, client=None, worker=None):
        self.reactor = printer.get_reactor()
        self.client = client if client is not None else RelayClient()
        self.worker = worker if worker is not None else ThreadWorker(
            self.reactor)
        printer.register_event_handler("klippy:ready", self._ready)

    def _ready(self):
        if self.client.available():
            self.worker.submit(self._turn_off, lambda result: None)

    def _turn_off(self):
        if supported(self.client.read_status()):
            return self.client.request({"method": kit_protocol.METHOD,
                                        "service": False})
        return None


def load(config, printer, gcode):
    """The kit's host side, from [oams_manager] options; None when it is
    turned off or the relay's definitions are missing from this checkout.

        cyberbrick: True                   the kit support at all (False:
                                           the service is turned off once,
                                           and nothing is registered)
        cyberbrick_min_interval: 0.5       the shortest gap between shots, s
        cyberbrick_timelapse_frames: False moonraker-timelapse frames fire
                                           the kit
    """
    if kit_protocol is None:
        return None
    if not config.getboolean("cyberbrick", True):
        ServiceOff(printer)
        return None
    kit = CyberBrickKit(
        printer,
        min_interval=config.getfloat("cyberbrick_min_interval",
                                     MIN_INTERVAL_S, minval=MIN_INTERVAL_S),
        timelapse_frames=config.getboolean("cyberbrick_timelapse_frames",
                                           False))
    kit.register_commands(gcode)
    return kit
