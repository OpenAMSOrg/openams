# SPDX-License-Identifier: MIT
"""`cortex-bridge.py`: set up, check and update a Cortex Bridge.

    python3 ~/openams/tools/cortex-bridge.py status
    python3 ~/openams/tools/cortex-bridge.py version
    python3 ~/openams/tools/cortex-bridge.py config --write
    python3 ~/openams/tools/cortex-bridge.py ptfe 2050
    python3 ~/openams/tools/cortex-bridge.py update
    python3 ~/openams/tools/cortex-bridge.py support [--submit]

Runs with any python3; it needs no extra packages. The relay service does all
USB work, so this command only reads its status and sends it requests. When
the relay is not running, `version` and `update` open the bridge's port
themselves (that needs pyserial, which the relay's install provides).
"""

from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cortex_bridge import ams_firmware  # noqa: E402
from cortex_bridge import ams_update  # noqa: E402
from cortex_bridge import bridge_info  # noqa: E402
from cortex_bridge import config as bridge_config  # noqa: E402
from cortex_bridge import firmware as bridge_firmware  # noqa: E402
from cortex_bridge import update_messages  # noqa: E402

OPENAMS_DIR = Path(__file__).resolve().parents[1]
SAMPLE = OPENAMS_DIR / "oams_cortex_bridge_sample.cfg"
SERVICE = "cortex-bridge"
RUN_DIR = Path("/run/cortex-bridge")
PTY_DIR = Path("/dev/oams")
SYSFS_USB = Path("/sys/bus/usb/devices")
BRIDGE_VID = "1a86"
PRODUCT_IDS = {"fe11": "application", "fe10": "receiver"}
DEFAULT_MOONRAKER = "http://127.0.0.1:7125"


def default_config_dir() -> Path:
    for candidate in ("~/printer_data/config", "~/klipper_config"):
        path = Path(candidate).expanduser()
        if path.is_dir():
            return path
    return Path("~/printer_data/config").expanduser()


def default_logs_dir() -> Path:
    path = Path("~/printer_data/logs").expanduser()
    return path if path.is_dir() else Path.home()


# ---------------------------------------------------------------------------
# What the computer can see
# ---------------------------------------------------------------------------

def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def usb_devices(root: Path = SYSFS_USB) -> list:
    """Cortex Bridge USB devices from sysfs, in any state."""
    found = []
    for device in sorted(root.glob("*")):
        if _read(device / "idVendor") != BRIDGE_VID:
            continue
        pid = _read(device / "idProduct")
        if pid not in PRODUCT_IDS:
            continue
        product = _read(device / "product")
        state = PRODUCT_IDS[pid]
        if pid == "fe10" and product.endswith("Fault"):
            state = "fault"
        elif pid == "fe10" and not product.endswith("Updater"):
            continue  # another product on the same IDs, not a bridge
        ttys = sorted(glob.glob(str(device / "*" / "tty" / "tty*")))
        found.append({
            "state": state,
            "pid": pid,
            "manufacturer": _read(device / "manufacturer"),
            "product": product,
            "serial": _read(device / "serial"),
            "device": "/dev/" + os.path.basename(ttys[0]) if ttys else "",
        })
    return found


def service_state(name: str) -> str:
    try:
        result = subprocess.run(
            ["systemctl", "is-active", name + ".service"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            universal_newlines=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    state = result.stdout.strip() or "unknown"
    if state in ("inactive", "unknown"):
        try:
            listed = subprocess.run(
                ["systemctl", "list-unit-files", name + ".service",
                 "--no-legend"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                universal_newlines=True, timeout=10)
            if not listed.stdout.strip():
                return "not-installed"
        except (OSError, subprocess.SubprocessError):
            pass
    return state


def read_status(run_dir: Path = RUN_DIR):
    try:
        return json.loads((run_dir / "status.json").read_text("utf-8"))
    except (OSError, ValueError):
        return None


def control_request(request: dict, *, run_dir: Path = RUN_DIR,
                    on_line=None, timeout: float = 600.0) -> dict:
    """Send one request to the relay; return its final reply line."""
    path = run_dir / "control.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(path))
        sock.sendall((json.dumps(request) + "\n").encode("utf-8"))
        buffer = b""
        last = {}
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if not line.strip():
                    continue
                last = json.loads(line.decode("utf-8"))
                if on_line is not None:
                    on_line(last)
        return last


def moonraker(url: str, path: str, *, post: bool = False, timeout=5.0):
    request = urllib.request.Request(
        url.rstrip("/") + path, data=b"" if post else None,
        method="POST" if post else "GET")
    with urllib.request.urlopen(request, timeout=timeout) as reply:
        return json.loads(reply.read().decode("utf-8"))


def printer_state(url: str):
    """Klipper's print state through Moonraker, or None if unreachable."""
    try:
        data = moonraker(url, "/printer/objects/query?print_stats")
    except (OSError, ValueError, urllib.error.URLError):
        return None
    return data.get("result", {}).get("status", {}) \
        .get("print_stats", {}).get("state")


def online_units(status) -> list:
    if not status:
        return []
    return [unit for unit in status.get("units", []) if unit.get("online")]


def configured_uids(config_dir: Path) -> set:
    uids = set()
    for path in config_dir.glob("*.cfg"):
        uids |= bridge_config.configured_uids(_read(path))
    return uids


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def bundled_list() -> list:
    return [
        {key: package.get(key) for key in
         ("version", "file", "released", "notes", "ok", "problem")}
        for package in bridge_firmware.bundled_packages()
    ]


def gather(args) -> dict:
    status = read_status(Path(args.run_dir))
    bundled = bundled_list()
    firmware = (status or {}).get("firmware") \
        if (status or {}).get("state") in ("running", "connecting") else None
    return {
        "usb": usb_devices(),
        "relay_service": service_state(SERVICE),
        "klipper_service": service_state(args.klipper_service),
        "relay": status,
        "configured": sorted(configured_uids(Path(args.config_dir))),
        "bundled_firmware": bundled,
        "installed_firmware": bridge_info.compare(firmware, bundled),
    }


def next_step(facts: dict) -> str:
    usb = facts["usb"]
    relay = facts["relay"] or {}
    if facts["relay_service"] == "not-installed":
        return ("The bridge relay is not installed. Run ~/openams/install.sh "
                "with the bridge connected (or with --cortex-bridge).")
    if not usb:
        return ("The bridge is not on USB. Check the USB-C cable (a data "
                "cable, not charge-only) and that the white LED is on.")
    states = {device["state"] for device in usb}
    if "fault" in states:
        return ("The bridge reports a fault. Unplug USB for 10 seconds and "
                "plug it back in; if the fault stays, contact support with "
                "`cortex-bridge.py support`.")
    if "receiver" in states and "application" not in states:
        return ("The bridge is waiting for firmware, usually after an "
                "interrupted update. Run `cortex-bridge.py update`.")
    if facts["relay_service"] != "active":
        return "Start the relay: sudo systemctl start cortex-bridge"
    units = online_units(relay)
    if not units:
        # Bridge firmware 9 and later say what they saw on the AMS line.
        reading = (facts.get("snapshot") or {}).get("reading") or {}
        if reading.get("class") not in (None, "none"):
            return ("No AMS answers. The bridge reports: %s %s" % (
                reading["summary"], reading.get("action") or "")).strip()
        return ("No AMS answers. Check 24 V at PWR IN (red LED on), the "
                "4-pin BUFFER cable, and the 6-pin cable from the AMS to the "
                "buffer.")
    # Bridge firmware 9 and later: the relay's roster can still list an AMS
    # the bridge no longer hears (unplugged, switched off, its own mains out,
    # moved to another printer). Only a configured one needs the user.
    silent = silent_units(facts)
    missing = [unit for unit in units
               if unit["uid"] not in facts["configured"]
               and unit["uid"] not in silent]
    if missing:
        return ("%d AMS online but not in your config. Run "
                "`cortex-bridge.py config --write`." % len(missing))
    silent = {uid: quiet for uid, quiet in silent.items()
              if uid in set(facts["configured"])}
    if silent:
        uid, quiet_s = sorted(silent.items())[0]
        return ("AMS %s has not answered the bridge for %s. Check its power "
                "(an AMS HT has its own) and its cables; it comes back by "
                "itself once it answers, then run FIRMWARE_RESTART." % (
                    uid, _duration(quiet_s)))
    # Bridge firmware 9 and later: another AMS may be connected but have no
    # index yet, while the others run.
    reading = (facts.get("snapshot") or {}).get("reading") or {}
    if reading.get("class") in ("no-index-offered", "index-not-taken"):
        return ("%s %s" % (reading["summary"], reading["action"])).strip()
    return ALL_CONNECTED


def firmware_line(comparison: dict, firmware) -> str:
    """One line: the installed version against the newest bundled one."""
    latest = comparison["latest"]
    newest = "version %s" % latest["version"] if latest else "none"
    verdict = comparison["verdict"]
    installed = comparison["installed"]
    if verdict == "up-to-date":
        return "version %d, up to date" % installed
    if verdict == "update-available":
        return ("version %d; version %s is available "
                "(`cortex-bridge.py update`)" % (installed, latest["version"]))
    if verdict == "newer-than-bundled":
        return ("version %d, newer than this OpenAMS bundles (%s): update "
                "OpenAMS" % (installed, newest))
    if verdict == "nothing-bundled":
        return "version %d (no firmware bundled with this OpenAMS)" % installed
    if firmware is None:
        return ("installed version unknown (`cortex-bridge.py version` asks "
                "the bridge); newest bundled: %s" % newest)
    return "installed version unknown: %s; newest bundled: %s" % (
        bridge_info.unknown_reason(firmware), newest)


def status_lines(facts: dict) -> list:
    """What `status` prints above the next step (the support report starts
    with the same lines)."""
    lines = ["Cortex Bridge"]
    relay = facts["relay"] or {}
    if not facts["usb"]:
        lines.append("  USB:         not connected")
    for device in facts["usb"]:
        label = {"application": "running", "receiver": "waiting for "
                 "firmware", "fault": "FAULT %s" % device["serial"]}[
                     device["state"]]
        image = {"SiForge": ", production firmware",
                 "OpenAMS": ", lab firmware"}.get(device["manufacturer"], "")
        lines.append("  USB:         %s, serial %s%s%s" % (
            label, device["serial"] or "?", image,
            (" on " + device["device"]) if device["device"] else ""))
    lines.append("  Relay:       %s%s" % (
        facts["relay_service"],
        (" (%s)" % relay.get("detail")) if relay.get("detail") else ""))
    units = relay.get("units", [])
    configured = set(facts["configured"])
    silent = silent_units(facts)
    if units:
        lines.append("  AMS units:")
        for unit in units:
            state = "online" if unit.get("online") else "offline"
            if unit["uid"] in silent:
                state = "silent"
            lines.append("    %s  %-7s  %s  %s" % (
                unit["uid"], state,
                unit["path"], "configured" if unit["uid"] in configured
                else "NOT in config"))
    else:
        lines.append("  AMS units:   none")
    firmware = relay.get("firmware") if facts["relay_service"] == "active" \
        else None
    lines.append("  Firmware:    %s" % firmware_line(
        facts["installed_firmware"], firmware))
    update = relay.get("update")
    if update:
        state = update.get("result") or update.get("phase") or ""
        message = update.get("message") or ""
        # The message usually repeats the state ("installed version 9").
        if not message.startswith(state):
            message = ("%s %s" % (state, message)).strip()
        lines.append("  Last update: %s" % message)
    lines.append("  Klipper:     %s" % facts["klipper_service"])
    return lines


ALL_CONNECTED = "Everything is connected."
SUPPORT_HINT = ("If that does not fix it, `cortex-bridge.py support` writes a "
                "report for support. It stays on this printer until you send "
                "it.")


def silent_units(facts) -> dict:
    """{uid: seconds since its last answer} for each admitted AMS the
    bridge's snapshot (firmware 9 or later) shows not answering, while the
    relay may still list it online."""
    snapshot = (facts.get("snapshot") or {}).get("snapshot") or {}
    uptime = (snapshot.get("system") or {}).get("uptime_ms")
    out = {}
    for unit in snapshot.get("units") or []:
        if not isinstance(unit, dict) or unit.get("link") != "silent":
            continue
        last = unit.get("last_answer_ms")
        quiet = (uptime - last) / 1000.0 if isinstance(uptime, int) and \
            isinstance(last, int) and uptime >= last else None
        out[str(unit.get("uid"))] = quiet
    return out


def _duration(seconds) -> str:
    if seconds is None:
        return "a while"
    if seconds < 90:
        return "%d s" % seconds
    return "%d min" % round(seconds / 60.0)


def status_snapshot(args):
    """The bridge's snapshot, asked whenever the relay runs: it names an AMS
    that answers nothing, and one that is connected but has no index while
    others run. The relay answers at once for a bridge that has none (older
    than firmware 9)."""
    from cortex_bridge import support_report
    return support_report.bridge_support_snapshot(
        Path(getattr(args, "run_dir", RUN_DIR)))


def cmd_status(args) -> int:
    facts = gather(args)
    if facts["relay_service"] == "active":
        facts["snapshot"] = status_snapshot(args)
    if args.json:
        print(json.dumps(facts, indent=1, sort_keys=True))
        return 0
    for line in status_lines(facts):
        print(line)
    print()
    step = next_step(facts)
    print(step)
    if step != ALL_CONNECTED:
        print(SUPPORT_HINT)
    return 0


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

def wait_for_units(run_dir: Path, seconds: float) -> list:
    deadline = time.monotonic() + seconds
    while True:
        units = online_units(read_status(run_dir))
        if units or time.monotonic() >= deadline:
            return units
        time.sleep(1.0)


def sudo_systemctl(action: str, service: str) -> None:
    print("  sudo systemctl %s %s" % (action, service))
    subprocess.run(["sudo", "systemctl", action, service], check=True)


def confirm(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        answer = input(question + " [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


# `config` could not change oams.cfg without a decision from the user (an
# OpenAMS 1 setup is still configured); the installer says so instead of
# blaming the AMS connection.
CONFIG_NEEDS_DECISION = 3


def cmd_config(args) -> int:
    from cortex_bridge import identify

    run_dir = Path(args.run_dir)
    units = wait_for_units(run_dir, args.wait)
    # An AMS the bridge no longer hears is left out (bridge firmware 9 and
    # later say so in their snapshot); the relay's roster may still list it.
    silent = silent_units({"snapshot": status_snapshot(args)}) if units \
        else {}
    for unit in [u for u in units if u["uid"] in silent]:
        print("  %s: left out, the bridge has not heard it for %s"
              % (unit["uid"], _duration(silent[unit["uid"]])))
    units = [u for u in units if u["uid"] not in silent]
    if not units:
        print("No AMS is online behind the bridge. Run `cortex-bridge.py "
              "status` to see why.", file=sys.stderr)
        return 1
    target = Path(args.file) if args.file else \
        Path(args.config_dir) / "oams.cfg"
    klipper_was_active = service_state(args.klipper_service) == "active"
    if klipper_was_active:
        if not confirm("Klipper is running. Stop it while the AMS units are "
                       "identified (about 10 s), then start it again?",
                       args.yes):
            print("Stopped. Nothing was changed.")
            return 1
        sudo_systemctl("stop", args.klipper_service)
    try:
        return _write_config(args, units, target, identify)
    finally:
        if klipper_was_active:
            sudo_systemctl("start", args.klipper_service)


def _write_config(args, units, target, identify) -> int:
    described = []
    for unit in units:
        try:
            facts = identify.describe_unit(unit["path"])
        except (OSError, identify.IdentifyError) as exc:
            print("  %s: could not identify (%s); using device_kind "
                  "auto with 4 bays" % (unit["uid"], exc))
            facts = {"family": "unknown", "bays": 4,
                     "name": "AMS (unidentified)"}
        facts["uid"] = unit["uid"]
        print("  %s  %s, %d bay%s" % (unit["uid"], facts["name"],
                                     facts["bays"],
                                     "" if facts["bays"] == 1 else "s"))
        described.append(facts)

    fresh = args.fresh or not target.exists()
    base = SAMPLE.read_text("utf-8") if fresh else target.read_text("utf-8")
    replaced = []
    try:
        start = base
        if getattr(args, "replace_openams1", False):
            wired = bridge_config.openams1_hardware(base)["wired_fps"]
            start, replaced = bridge_config.replace_openams1(base)
        text, names = bridge_config.add_units(start, described)
    except bridge_config.ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return CONFIG_NEEDS_DECISION
    if replaced:
        print("OpenAMS 1 sections %s:" % ("commented out" if args.write
                                          else "to comment out"))
        for name, why in replaced:
            print("  [%s]: %s" % (name, why))
        for _section, pin in wired:
            board = pin.lstrip("^~!").split(":", 1)[0]
            if ":" in pin and re.search(r"^\[mcu %s\]" % re.escape(board),
                                        base, re.MULTILINE):
                print("  [mcu %s] is left as it is: keep it while that board "
                      "is connected (for example as the USB-to-CAN bridge "
                      "for a toolhead board), comment it out if it is gone."
                      % board)
    if not names and not replaced:
        print("Every online AMS is already in %s." % target)
    elif not args.write:
        print()
        print(text[len(start.rstrip("\n")):].strip("\n"))
        print()
        print("Nothing written. Run again with --write to change %s."
              % target)
    else:
        backup = bridge_config.write_with_backup(target, text)
        print("%s %s%s." % (
            "Added %s to" % ", ".join(names) if names else "Updated",
            target, " (old file kept as %s)" % backup.name if backup else ""))
        print(next_step_after_config(described))
    return 0


def next_step_after_config(described: list) -> str:
    """What to do about ptfe_length once the units are in oams.cfg. An AMS on
    bridge firmware 8 or later measures its own path; older firmware needs the
    path measured by hand and converted."""
    # One bridge runs one firmware, so one AMS that measures means they all
    # can (an AMS that could not be identified has no answer).
    if any(unit.get("ptfe_calibration") for unit in described):
        return ("Next: once Klipper is ready, let each AMS measure its "
                "filament path, with someone watching: "
                "`OAMS_CALIBRATE_PTFE_LENGTH OAMS=<n> SPOOL=<bay>` once per "
                "AMS, on any bay with filament: every bay shares the AMS's "
                "path (oams_idx is in oams.cfg, bays count from 0).")
    return ("Next: measure each AMS's filament path and set ptfe_length "
            "(`cortex-bridge.py ptfe <millimetres>`), or update the bridge "
            "firmware (`cortex-bridge.py update`) and let the AMS measure it.")


# ---------------------------------------------------------------------------
# ptfe
# ---------------------------------------------------------------------------

def cmd_ptfe(args) -> int:
    try:
        clicks = bridge_config.mm_to_clicks(args.millimetres)
    except bridge_config.ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print("%.0f mm of filament path is ptfe_length: %d" %
          (args.millimetres, clicks))
    print("Put that line in the AMS's [oams] section in oams.cfg, then "
          "restart Klipper.")
    return 0


# ---------------------------------------------------------------------------
# installed firmware, version, firmware and update
# ---------------------------------------------------------------------------

RELAY_ANSWER_WAIT = 20.0        # how long `version` waits for the relay's query


RELAY_SERVICE = "cortex-bridge"
RELAY_RESTART_WAIT = 60.0       # how long the relay gets to come back running


def relay_lacks(status, method: str) -> bool:
    """True when the running relay is older than this checkout. Tools run from
    the checkout each time, but the relay keeps the code it started with until
    its service restarts (an OpenAMS update through Moonraker restarts it; a
    plain `git pull`, or an update entry without `managed_services`, does not).
    The relay lists the control methods it answers in its status file, and a
    relay from before that list writes none. A stopped relay is not older."""
    if not status or status.get("state") in (None, "stopped"):
        return False
    return method not in (status.get("methods") or ())


def restart_relay(args, method: str, *, sleep=time.sleep,
                  clock=time.monotonic) -> bool:
    """Restart the relay through Moonraker (moonraker.asvc allows it) and
    wait until it runs the new code; False, with what to do, otherwise."""
    by_hand = ("Run: sudo systemctl restart %s, then FIRMWARE RESTART in "
               "Mainsail, then this command again." % RELAY_SERVICE)
    try:
        moonraker(args.moonraker,
                  "/machine/services/restart?service=" + RELAY_SERVICE,
                  post=True, timeout=RELAY_RESTART_WAIT)
    except (OSError, ValueError, urllib.error.URLError) as exc:
        print("Moonraker could not restart it (%s). %s" % (exc, by_hand),
              file=sys.stderr)
        return False
    deadline = clock() + RELAY_RESTART_WAIT
    while clock() < deadline:
        status = read_status(Path(args.run_dir))
        if status and status.get("state") == "running" \
                and method in (status.get("methods") or ()):
            print("The relay restarted with the new code.")
            return True
        sleep(1.0)
    print("The relay has not come back running the new code. %s" % by_hand,
          file=sys.stderr)
    return False


def ensure_relay_current(args, method: str, *, sleep=time.sleep,
                         clock=time.monotonic) -> bool:
    """True when the relay answers `method`. A relay that still runs older
    code is restarted, after a yes and never during a print, then Klipper is
    restarted to reconnect to the AMS units."""
    status = read_status(Path(args.run_dir))
    if not relay_lacks(status, method):
        return True
    started = (status.get("started") or "")[:16].replace("T", " ")
    print("The bridge relay (the %s service) still runs the OpenAMS code it "
          "started with%s, from before OpenAMS was updated, so it cannot do "
          "this yet. A restart loads the new code."
          % (RELAY_SERVICE, " at %s UTC" % started if started else ""))
    state = printer_state(args.moonraker)
    if state in ("printing", "paused"):
        print("Klipper is %s: when no print runs, run: sudo systemctl restart "
              "%s, then FIRMWARE RESTART in Mainsail, then this command again."
              % (state, RELAY_SERVICE), file=sys.stderr)
        return False
    if not confirm("Restart the relay now? Klipper reconnects to the AMS "
                   "units afterwards.", args.yes):
        print("Nothing was changed. To do it yourself: sudo systemctl restart "
              "%s, then FIRMWARE RESTART in Mainsail." % RELAY_SERVICE)
        return False
    if not restart_relay(args, method, sleep=sleep, clock=clock):
        return False
    restart_klipper(args.moonraker, why=" to reconnect to the AMS units",
                    sleep=sleep, clock=clock)
    return True


def relay_running(run_dir: Path) -> bool:
    try:
        control_request({"method": "status"}, run_dir=run_dir, timeout=10)
    except (OSError, ValueError):
        return False
    return True


def installed_firmware(run_dir: Path, *, wait: float = RELAY_ANSWER_WAIT,
                       direct: bool = True, sleep=time.sleep,
                       clock=time.monotonic) -> dict:
    """What the bridge says it runs.

    Returns ``usb`` (the bridge's USB state: application, receiver, fault or
    None), ``serial``, ``image``, ``fault`` (a code or ""), ``source``
    ("relay", "direct" or "") and ``firmware`` (a ``bridge_info`` firmware
    status object, or None when no running bridge was asked). With the relay
    running its answer is used, waiting up to ``wait`` seconds while it is
    still asking; without it the bridge is asked directly when ``direct``."""
    devices = usb_devices()
    result = {"usb": None, "serial": "", "image": "", "fault": "",
              "source": "", "firmware": None}
    for state in ("fault", "receiver", "application"):
        found = [d for d in devices if d["state"] == state]
        if found:
            device = found[0]
            result.update(usb=state, serial=device["serial"],
                          image={"SiForge": "production",
                                 "OpenAMS": "lab"}.get(
                                     device["manufacturer"], "unknown"),
                          fault=device["serial"] if state == "fault" else "")
            break
    if result["usb"] != "application":
        return result
    if relay_running(run_dir):
        result["source"] = "relay"
        deadline = clock() + wait
        while True:
            status = read_status(run_dir) or {}
            firmware = status.get("firmware")
            if firmware and firmware.get("state") != "querying":
                result["firmware"] = firmware
                return result
            if clock() >= deadline:
                result["firmware"] = firmware
                return result
            sleep(0.5)
    if not direct:
        return result
    if result["image"] != "production":
        result["firmware"] = bridge_info.firmware_status(
            "unavailable", "lab-firmware" if result["image"] == "lab"
            else "unknown-image")
        return result
    device = next(d for d in devices if d["state"] == "application")
    result["source"] = "direct"
    try:
        result["firmware"] = bridge_info.query_direct(device["device"])
    except ImportError:
        result["firmware"] = bridge_info.firmware_status(
            "unavailable", "no-pyserial")
    except Exception as exc:  # noqa: BLE001 - any failure is "unknown"
        result["firmware"] = bridge_info.firmware_status(
            "unavailable", "no-answer")
        result["error"] = str(exc)
    return result


def version_report(found: dict, bundled: list) -> dict:
    comparison = bridge_info.compare(found["firmware"], bundled)
    firmware = found["firmware"] or {}
    latest = comparison["latest"]
    return {
        "serial": found["serial"],
        "usb_state": found["usb"],
        "fault": found["fault"],
        "image": found["image"],
        "source": found["source"],
        "installed": comparison["installed"],
        "installed_state": firmware.get("state"),
        "reason": firmware.get("reason") or "",
        "reason_text": "" if comparison["installed"] is not None else
        bridge_info.unknown_reason(found["firmware"]),
        "boot_abi": firmware.get("boot_abi"),
        "genuine": firmware.get("genuine"),
        "latest": latest["version"] if latest else None,
        "latest_released": latest.get("released") if latest else None,
        "latest_notes": latest.get("notes") if latest else None,
        "verdict": comparison["verdict"],
        "update_available": None if comparison["installed"] is None or
        latest is None else comparison["verdict"] == "update-available",
    }


def cmd_version(args) -> int:
    found = installed_firmware(Path(args.run_dir), wait=args.wait)
    report = version_report(found, bundled_list())
    if args.json:
        print(json.dumps(report, indent=1, sort_keys=True))
        return 0 if report["installed"] is not None else 1
    if found["usb"] is None:
        print("No Cortex Bridge is connected over USB.")
        print("Check the USB-C cable (a data cable, not charge-only) and that "
              "the bridge's white LED is on.")
        return 1
    if found["usb"] == "fault":
        print("Cortex Bridge: the bridge reports fault %s." % found["fault"])
        print("It does not run firmware in this state. Unplug USB and 24 V for "
              "10 seconds and reconnect them; if the fault comes back, run "
              "`cortex-bridge.py support` and send support the report.")
        return 1
    print("Cortex Bridge %s" % (found["serial"] or ""))
    newest = "none bundled with this OpenAMS"
    if report["latest"] is not None:
        newest = "version %s%s, bundled with this OpenAMS" % (
            report["latest"], " (released %s)" % report["latest_released"]
            if report["latest_released"] else "")
    if found["usb"] == "receiver":
        print("  Installed:  none: the bridge is waiting for firmware (an "
              "update did not finish)")
        print("  Newest:     %s" % newest)
        print("Run `cortex-bridge.py update` to install it.")
        return 1
    if report["installed"] is None:
        print("  Installed:  unknown: %s" % report["reason_text"])
        print("  Newest:     %s" % newest)
        return 1
    print("  Installed:  version %d" % report["installed"])
    print("  Newest:     %s" % newest)
    if report["genuine"] == "failed":
        print("  The bridge failed its authenticity check. Run "
              "`cortex-bridge.py support` and send support the report.")
    verdict = report["verdict"]
    if verdict == "up-to-date":
        print("Up to date.")
    elif verdict == "update-available":
        print("Version %s is available: %s" % (report["latest"],
                                                report["latest_notes"] or ""))
        print("Install it with `cortex-bridge.py update` while the printer and "
              "the AMS are idle.")
    elif verdict == "newer-than-bundled":
        print("The bridge runs newer firmware than this OpenAMS bundles. Update "
              "OpenAMS (Mainsail: Machine > Update Manager).")
    return 0


def cmd_firmware(args) -> int:
    packages = bridge_firmware.bundled_packages()
    if not packages:
        print("No bridge firmware is bundled with this OpenAMS version yet.")
        print("New firmware arrives with OpenAMS updates (Mainsail: Machine "
              "> Update Manager).")
        return 0
    found = installed_firmware(Path(args.run_dir), wait=5.0, direct=False)
    comparison = bridge_info.compare(found["firmware"], bundled_list())
    installed = comparison["installed"]
    if installed is not None:
        print("The bridge runs version %d." % installed)
    for package in packages:
        version = package.get("version")
        mark = ""
        if installed is not None and isinstance(version, int):
            mark = "  (installed)" if version == installed else \
                "  (newer than installed)" if version > installed else ""
        print("version %-4s %-10s %s%s%s" % (
            version, package.get("released", ""), package.get("notes", ""),
            "" if package["ok"] else "  [%s]" % package["problem"], mark))
    return 0


# ---------------------------------------------------------------------------
# AMS firmware (the AMS units' own firmware, not the bridge's)

def ams_units(args) -> list:
    """The AMS units the bridge reports, with their firmware versions, or []
    when the relay or the bridge cannot say."""
    facts = status_snapshot(args)
    if not facts.get("available"):
        return []
    return ams_firmware.units_from_snapshot(facts.get("snapshot") or {})


def cmd_ams_firmware(args) -> int:
    try:
        images = ams_firmware.load_catalog()
    except ams_firmware.AmsFirmwareError as exc:
        print("AMS firmware list: %s" % exc, file=sys.stderr)
        return 1
    if args.action == "fetch":
        return ams_firmware_fetch(args, images)
    if args.action == "import":
        return ams_firmware_import(args, images)
    if args.action == "update":
        return ams_firmware_update(args, images)
    if args.action == "listen":
        return ams_firmware_listen(args)
    units = ams_units(args)
    # Bridge 24 and later read a unit's version again after an update, so
    # their reading stands; older ones keep the version from admission.
    firmware = (read_status(Path(args.run_dir)) or {}).get("firmware") or {}
    bridge_version = firmware.get("version")
    installed = ams_firmware.installed_versions() \
        if not isinstance(bridge_version, int) or bridge_version < 24 else {}
    if units:
        print("Connected AMS units:")
        for unit in units:
            family = unit["family"]
            name = ams_firmware.FAMILIES.get(family, "unknown AMS")
            listed = ams_firmware.versions(images, family) if family else []
            note = ""
            done = (installed.get(unit["uid"]) or {}).get("version")
            if done and unit["version"] and done != unit["version"]:
                # The bridge still holds the version it read at admission.
                print("  %-12s %-10s %-12s installed %s; the bridge reads "
                      "the new version after its next restart" % (
                          unit["uid"], name, done, done))
                continue
            if unit["version"] and listed:
                step = ams_firmware.compare(unit["version"], listed[0])
                note = {"same": "the newest listed",
                        "upgrade": "%s is listed" % listed[0],
                        "downgrade": "newer than any listed"}[step]
            print("  %-12s %-10s %-12s %s%s" % (
                unit["uid"], name, unit["version"] or "version unknown",
                note, "" if unit["online"] else "  (offline)"))
    else:
        print("No AMS versions to show: the relay or the bridge did not "
              "answer (cortex-bridge.py status).")
    print()
    print("Listed AMS firmware (from Bambu Lab; * = on this computer):")
    for family, name in ams_firmware.FAMILIES.items():
        listed = ams_firmware.versions(images, family)
        if not listed:
            print("  %-10s none listed yet" % name)
            continue
        for version in listed:
            chosen = [i for i in images
                      if i.family == family and i.version == version]
            boards = ", ".join(
                i.hardware + ("*" if ams_firmware.cached_path(i).exists()
                              else "") for i in chosen)
            where = chosen[0].source or ""
            if not chosen[0].url:
                where += " offline package" if where else "offline package"
            print("  %-10s %s  %-12s %s" % (name, version, boards, where))
    return 0


def pick_unit(units: list, wanted: str):
    """The unit named by uid or bus index, or (None, why)."""
    if not wanted:
        return None, ("name the AMS with --unit (its id or index): %s"
                      % ", ".join("%s (index %s, %s)" % (
                          u["uid"], u["index"],
                          ams_firmware.FAMILIES.get(u["family"], "unknown"))
                          for u in units))
    for unit in units:
        if unit["uid"] == wanted or str(unit["index"]) == wanted:
            return unit, ""
    return None, "no AMS %s is connected to the bridge" % wanted


def wait_unit_version(args, unit: dict, version: str, seconds: float,
                      *, sleep=time.sleep, clock=time.monotonic) -> str:
    """The version the bridge reads for `unit` once it is online and reports
    `version`, or "" after `seconds`."""
    deadline = clock() + seconds
    while clock() < deadline:
        for other in ams_units(args):
            if other["uid"] == unit["uid"] and other["online"] and \
                    other["version"] == version:
                return version
        sleep(3.0)
    return ""


def probe_board(args, unit: dict, chosen: list):
    """([the image for this AMS's board], "") or ([], why). The bridge opens
    a session, reports the unit's model code in its first answer, and the
    session is aborted before any loader command (nothing is written)."""
    try:
        path = ams_firmware.fetch(chosen[0])
    except ams_firmware.AmsFirmwareError as exc:
        return [], str(exc)
    seen = {}

    def on_line(event):
        if event.get("event") == "probe":
            seen.update(event)
        elif event.get("event") == "result":
            seen["result"] = event
    try:
        control_request(
            {"method": "ams_update", "image": str(path),
             "index": unit["index"], "family": unit["family"],
             "model": ams_update.model_for(unit["family"],
                                           chosen[0].hardware),
             "probe": True},
            run_dir=Path(args.run_dir), on_line=on_line, timeout=60.0)
    except OSError as exc:
        return [], "the relay did not answer (%s)" % (exc.strerror or exc)
    code = seen.get("model_code", "")
    if not code:
        result = seen.get("result") or {}
        return [], ("the bridge did not report which board this AMS has (%s)"
                    % (result.get("message") or "no model code"))
    for image in chosen:
        if ams_update.model_for(unit["family"], image.hardware) == code:
            print("The AMS reports board %s." % code)
            return [image], ""
    return [], ("the AMS reports board %r, which no listed image is for "
                "(%s); nothing was changed"
                % (code, ", ".join(i.hardware for i in chosen)))


def ams_firmware_update(args, images: list) -> int:
    if not ensure_relay_current(args, "ams_update"):
        return 1
    units = ams_units(args)
    if not units and not args.resume:
        print("The bridge did not report its AMS units (cortex-bridge.py "
              "status).", file=sys.stderr)
        return 1
    unit, why = pick_unit(units, args.unit)
    if unit is None and args.resume:
        # An AMS waiting in its loader is not in the bridge's list: it is
        # named by its bus index and model instead.
        if not args.unit.isdigit() or not args.family:
            print("--resume needs --unit <bus index> and --family",
                  file=sys.stderr)
            return 2
        unit = {"uid": "index %s" % args.unit, "index": int(args.unit),
                "family": args.family, "online": False, "version": ""}
    if unit is None:
        print(why, file=sys.stderr)
        return 2
    family = unit["family"]
    name = ams_firmware.FAMILIES.get(family, "unknown AMS")
    if family is None:
        print("The bridge does not know which AMS model %s is."
              % unit["uid"], file=sys.stderr)
        return 1
    try:
        chosen = ams_firmware.choose(images, family, args.version)
    except ams_firmware.AmsFirmwareError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if args.board:
        chosen = [i for i in chosen if i.hardware == args.board]
    if len(chosen) > 1 and not args.resume:
        # Several board revisions (the AMS 1's rev7 and rev8): ask the bridge
        # which board this AMS is, without sending it to its loader.
        chosen, why = probe_board(args, unit, chosen)
        if not chosen:
            print(why, file=sys.stderr)
            return 1
    if len(chosen) != 1:
        print("%s %s comes for boards %s: name one with --board"
              % (name, chosen[0].version if chosen else args.version,
                 ", ".join(i.hardware for i in chosen) or "(none)"),
              file=sys.stderr)
        return 2
    image = chosen[0]
    try:
        path = ams_firmware.fetch(image)
    except ams_firmware.AmsFirmwareError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    installed = unit["version"]
    step = ams_firmware.compare(installed, image.version) if installed \
        else "update"
    if step == "same" and not args.force:
        print("%s %s already runs %s (--force to install it again)."
              % (name, unit["uid"], installed))
        return 0
    state = printer_state(args.moonraker)
    if state in ("printing", "paused"):
        print("Klipper is %s: update the AMS when no print runs." % state,
              file=sys.stderr)
        return 1
    print("%s %s (index %s): %s -> %s (%s)" % (
        name, unit["uid"], unit["index"], installed or "unknown",
        image.version, step))
    print("The AMS stops answering for a minute or two while it updates; "
          "Klipper stays connected. Do not power the AMS or the bridge off "
          "until this says Done.")
    if not confirm("Update it now?", args.yes):
        print("Nothing was changed.")
        return 1
    model = ams_update.model_for(family, image.hardware)
    shown = {"block": -10, "result": None}
    started = time.monotonic()

    def stamp():
        return "%6.2fs " % (time.monotonic() - started) if args.verbose \
            else ""

    def on_line(event):
        kind = event.get("event")
        if kind == "result":
            shown["result"] = event
        elif kind == "phase":
            text = event.get("text") or event.get("phase")
            if event.get("banner") and event.get("phase") == "isolate":
                text += " (loader: %s)" % event["banner"]
            print("  " + stamp() + text, flush=True)
        elif kind == "progress":
            block, total = event["block"], event["blocks"]
            if block - shown["block"] >= max(1, total // 10) or \
                    block + 1 == total:
                shown["block"] = block
                print("    block %d of %d%s" % (
                    block + 1, total, ", %d repeats" % event["retries"]
                    if event.get("retries") else ""), flush=True)
        elif kind == "loader" and args.verbose:
            print("    %sloader 0x%04x: %s" % (stamp(), event["source"],
                                            event["line"]), flush=True)
        elif kind == "units":
            states = ", ".join("index %s %s" % (u["index"], u["state"])
                               for u in event["units"])
            print("  After the update: " + states, flush=True)
            if event.get("after"):
                shown["after"] = event["after"]

    # --wait-loader: an AMS left in its loader names its board only when it
    # powers up, so it is switched on while the bridge keeps asking; each
    # attempt that finds no loader (15 s) is safe and is repeated.
    give_up = time.monotonic() + max(0, args.wait_loader)
    while True:
        shown["result"] = None
        try:
            last = control_request(
                {"method": "ams_update", "image": str(path),
                 "index": unit["index"], "family": family, "model": model,
                 "resume": bool(args.resume)},
                run_dir=Path(args.run_dir), on_line=on_line, timeout=900.0)
        except OSError as exc:
            print("The relay did not answer (%s): is cortex-bridge running?"
                  % (exc.strerror or exc), file=sys.stderr)
            return 1
        # The result comes before the after-update unit states.
        result = shown["result"] or last or {}
        if result.get("code") != "no-loader" or time.monotonic() > give_up:
            break
        print("  No loader yet; asking again (switch the AMS on now if it "
              "is off).", flush=True)
    if result.get("ok"):
        if not unit["uid"].startswith("index "):
            try:
                ams_firmware.record_installed(unit["uid"], image.version)
            except OSError:
                pass
        after = shown.get("after") or result.get("after")
        if result.get("code") == "no-rejoin" and not after:
            # The loader accepted the image; the unit took longer than the
            # bridge's wait to answer. The bridge reads its version as soon
            # as it does.
            print("  The AMS is restarting; waiting for it to answer...",
                  flush=True)
            after = wait_unit_version(args, unit, image.version, 60.0)
        if result.get("code") == "no-rejoin" and not after:
            print("Done: %s %s accepted %s but has not answered yet; if it "
                  "stays offline for two minutes, power it off and on."
                  % (name, unit["uid"], image.version))
        else:
            print("Done: %s %s now runs %s." % (name, unit["uid"],
                                                after or image.version))
        # Some of what Klipper knows about an AMS follows its firmware
        # version (an AMS HT on 04.x feeds while it dries); Klipper reads it
        # again at FIRMWARE_RESTART.
        state = printer_state(args.moonraker)
        if args.no_restart or state in ("printing", "paused"):
            print("Run FIRMWARE_RESTART (Mainsail: FIRMWARE RESTART) when no "
                  "print runs, so Klipper sees what the new firmware can do.")
        else:
            restart_klipper(args.moonraker,
                            why=" to read what the new firmware can do")
        return 0
    print("Update failed: %s" % (result.get("message") or
                                 result.get("code") or "no answer"),
          file=sys.stderr)
    if result.get("error") == "unknown-method":
        print("The bridge relay runs older OpenAMS code: run sudo systemctl "
              "restart %s, then FIRMWARE RESTART in Mainsail, then this "
              "command again." % RELAY_SERVICE, file=sys.stderr)
    return 1


def ams_firmware_listen(args) -> int:
    """Show every AMS loader line on the bus for a while: an AMS that waits
    in its loader (after an interrupted update) prints one every second."""
    if not ensure_relay_current(args, "ams_listen"):
        return 1
    started = time.monotonic()
    heard = {"lines": 0, "result": None}

    def on_line(event):
        kind = event.get("event")
        if kind == "loader":
            heard["lines"] += 1
            print("  %6.2fs  0x%04x: %s" % (time.monotonic() - started,
                                           event["source"], event["line"]),
                  flush=True)
        elif kind == "result":
            heard["result"] = event

    print("Listening for AMS loaders for %d s..." % args.seconds)
    try:
        last = control_request({"method": "ams_listen",
                                "seconds": args.seconds},
                               run_dir=Path(args.run_dir), on_line=on_line,
                               timeout=args.seconds + 30)
    except OSError as exc:
        print("The relay did not answer (%s)." % (exc.strerror or exc),
              file=sys.stderr)
        return 1
    result = heard["result"] or last or {}
    if not result.get("ok"):
        print("Could not listen: %s" % (result.get("message") or "no answer"),
              file=sys.stderr)
        return 1
    print("No AMS loader is talking." if not heard["lines"] else
          "%d loader line(s): an AMS waits in its loader; run the update "
          "for it again." % heard["lines"])
    return 0


def ams_firmware_import(args, images: list) -> int:
    if not args.package:
        print("import needs a Bambu offline package: ams-firmware import "
              "<file.zip>", file=sys.stderr)
        return 2
    try:
        results = ams_firmware.import_package(Path(args.package).expanduser(),
                                              images)
    except ams_firmware.AmsFirmwareError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if not results:
        print("%s holds no AMS firmware this OpenAMS lists." % args.package)
        return 1
    status = 0
    for image, path, error in results:
        if path:
            print("%s: checked, %d bytes, %s" % (image.label, image.size,
                                                 path))
        else:
            print("%s: not imported: %s" % (image.label, error),
                  file=sys.stderr)
            status = 1
    return status


def ams_firmware_fetch(args, images: list) -> int:
    try:
        chosen = ams_firmware.choose(images, args.family or "ams_ht",
                                     args.version)
    except ams_firmware.AmsFirmwareError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    status = 0
    for image in chosen:
        if args.board and image.hardware != args.board:
            continue
        try:
            path = ams_firmware.fetch(image)
        except ams_firmware.AmsFirmwareError as exc:
            print("%s: %s" % (image.label, exc), file=sys.stderr)
            status = 1
            continue
        print("%s: checked, %d bytes, %s" % (image.label, image.size, path))
    return status


PHASE_TEXT = {
    "entering": "Asking the bridge to enter update mode",
    "waiting-receiver": "Waiting for the bridge's update receiver",
    "authenticating": "The bridge is checking the package",
    "writing": "Writing the firmware",
    "restarting": "The bridge is restarting",
    "done": "Done",
}


def print_failure(explanation: dict, detail: str = "") -> None:
    lines = update_messages.lines(explanation, detail)
    print("Update failed: %s" % lines[0], file=sys.stderr)
    for line in lines[1:]:
        print(line, file=sys.stderr)


def print_explanation(explanation: dict) -> None:
    for line in update_messages.lines(explanation):
        print(line)


def confirm_installed(version: int, firmware) -> int:
    """Report what the bridge says after an install; 1 on a mismatch."""
    if firmware and firmware.get("state") == "known":
        if firmware.get("version") == version:
            print("The bridge now runs version %d." % version)
            return 0
        print("The bridge restarted but reports version %s, not %d. Run "
              "`cortex-bridge.py support` and send support the report."
              % (firmware.get("version"), version), file=sys.stderr)
        return 1
    print("The bridge restarted; its version could not be read yet (%s). "
          "Check it with `cortex-bridge.py version`."
          % bridge_info.unknown_reason(firmware))
    return 0


def update_direct(package_path: Path, version: int, running) -> int:
    """Install a package with no relay running: this process opens the
    bridge's port itself (python3 and pyserial are all it needs)."""
    from . import updater
    shown = [None]

    def progress(phase, done, total):
        # One line per phase, then every 10 %.
        step = (phase, 10 * done // total if total else 0)
        if step == shown[0]:
            return
        shown[0] = step
        text = PHASE_TEXT.get(phase, phase)
        if total:
            text += " (%d%%)" % (100 * done // total)
        print("  " + text, flush=True)

    def repeating(attempt, attempts, error):
        print("  The bridge did not accept the update this time; repeating it "
              "(attempt %d of %d)." % (attempt + 1, attempts), flush=True)

    def running_version():
        # Asked only when a repeated update ends in a refusal: the version the
        # one attached bridge reports now, or None.
        try:
            running = [b for b in updater.find_bridges()
                       if b.state == updater.STATE_APPLICATION]
            if len(running) != 1:
                return None
            firmware = bridge_info.query_direct(running[0].device)
        except Exception:  # noqa: BLE001 - then the refusal stands
            return None
        if firmware and firmware.get("state") == "known":
            return firmware.get("version")
        return None

    try:
        report = updater.update_bridge_with_retry(package_path.read_bytes(),
                                                  progress=progress,
                                                  on_retry=repeating,
                                                  confirm_version=running_version)
    except OSError as exc:
        print_failure(update_messages.explain("OSError", detail=str(exc),
                                              running=running,
                                              package=version))
        return 1
    except updater.UpdateError as exc:
        print_failure(update_messages.explain_exception(
            exc, running=running, package=version),
            update_messages.technical_detail(type(exc).__name__, str(exc)))
        return 1
    print("Installed firmware version %s on bridge %s."
          % (report["version"], report.get("serial_number", "")))
    device = report.get("device")
    if not device:
        return 0
    try:
        firmware = bridge_info.query_direct(device)
    except Exception:  # noqa: BLE001 - the install itself succeeded
        firmware = None
    return confirm_installed(int(report["version"]), firmware)


def wait_for_firmware(run_dir: Path, seconds: float):
    """The relay's firmware answer after a restart, or None."""
    deadline = time.monotonic() + seconds
    while True:
        firmware = (read_status(run_dir) or {}).get("firmware")
        if firmware and firmware.get("state") != "querying":
            return firmware
        if time.monotonic() >= deadline:
            return firmware
        time.sleep(1.0)


def cmd_update(args) -> int:
    from . import updater
    run_dir = Path(args.run_dir)
    if args.file:
        package_path = Path(args.file).expanduser().resolve()
        source = "file"
    else:
        package = bridge_firmware.latest_bundled()
        if package is None:
            print("No bridge firmware is bundled with this OpenAMS version "
                  "yet. Update OpenAMS to receive new firmware when it is "
                  "released.")
            return 0
        package_path = Path(package["path"])
        source = "bundled"
    try:
        info = updater.inspect_package(package_path.read_bytes())
    except OSError as exc:
        print_failure(update_messages.explain(
            "PackageRefused", detail="it cannot be read (%s)" % exc.strerror))
        return 1
    except updater.UpdateError as exc:
        print_failure(update_messages.explain_exception(exc),
                      update_messages.technical_detail(type(exc).__name__,
                                                       str(exc)))
        return 1
    version = int(info["version"])

    state = printer_state(args.moonraker)
    if state in ("printing", "paused"):
        print_failure(update_messages.explain("PrinterBusy", detail=state))
        return 1

    # With the relay running it owns the bridge's port and installs the
    # package; without it this command talks to the bridge itself.
    relayed = relay_running(run_dir)
    found = installed_firmware(run_dir)
    if found["usb"] is None:
        print_failure(update_messages.explain("BridgeNotFound"))
        return 1
    if found["usb"] == "fault":
        print_failure(update_messages.explain(
            "BridgeFault", fault_code=found["fault"]))
        return 1
    firmware = found["firmware"] or {}
    running = firmware.get("version") if firmware.get("state") == "known" \
        else None
    if running is not None and version <= running:
        if version == running:
            print_explanation(update_messages.explain(
                "AlreadyInstalled", running=running, package=version))
            return 0
        print_explanation(update_messages.explain(
            "OlderThanInstalled", running=running, package=version))
        return 0 if source == "bundled" else 1

    if found["usb"] == "receiver":
        print("The bridge is waiting for firmware. This installs Cortex Bridge "
              "firmware version %d." % version)
    elif running is not None:
        print("This installs Cortex Bridge firmware version %d (the bridge "
              "runs version %d)." % (version, running))
    else:
        print("This installs Cortex Bridge firmware version %d (the version "
              "the bridge runs is unknown: %s)."
              % (version, bridge_info.unknown_reason(found["firmware"])))
    if relayed:
        print("Klipper loses the AMS for about a minute and is restarted "
              "afterwards. Keep the bridge powered until it finishes.")
    else:
        print("The bridge restarts and is back within a minute. Keep it "
              "powered until it finishes.")
    if not confirm("Continue?", args.yes):
        print("Cancelled.")
        return 1
    if not relayed:
        return update_direct(package_path, version, running)

    def show(line):
        event = line.get("event")
        if event == "progress":
            phase = line.get("phase", "")
            total = line.get("total") or 0
            text = PHASE_TEXT.get(phase, phase)
            if total:
                text += " (%d%%)" % (100 * line.get("done", 0) // total)
            print("  " + text, flush=True)

    result = control_request({"method": "update", "file": str(package_path),
                              "source": source}, run_dir=run_dir,
                             on_line=show)
    explanation = {key: result[key] for key in ("summary", "outcome", "action")
                   if key in result}
    if not result.get("ok"):
        if len(explanation) != 3:
            explanation = update_messages.explain(
                result.get("error", "error"), detail=result.get("message", ""),
                running=running, package=version)
        print_failure(explanation, update_messages.technical_detail(
            result.get("error", ""), result.get("message", "")))
        return 1
    if not result.get("changed", True):
        print_explanation(explanation)
        return 0
    print("Installed. Waiting for the AMS to come back...")
    units = wait_for_units(run_dir, 90)
    print("  %d AMS online" % len(units))
    code = confirm_installed(version, wait_for_firmware(run_dir, 30))
    if not args.no_restart:
        restart_klipper(args.moonraker)
    return code


# ---------------------------------------------------------------------------
# klipper-started
# ---------------------------------------------------------------------------

SHUTDOWN_AT_START = "config as it is shutdown"


def klipper_info(url: str):
    """(state, state_message) from Moonraker, or (None, "") while Klipper or
    Moonraker does not answer yet."""
    try:
        result = moonraker(url, "/printer/info").get("result", {})
    except (OSError, ValueError, urllib.error.URLError):
        return None, ""
    return result.get("state"), result.get("state_message", "")


def wait_klipper_settled(url: str, seconds: float, *, sleep=time.sleep,
                         clock=time.monotonic):
    """Wait until Klipper is ready, in error or shut down (or `seconds` pass);
    returns its last (state, state_message)."""
    deadline = clock() + seconds
    while True:
        state, message = klipper_info(url)
        if state in ("ready", "error", "shutdown") or clock() >= deadline:
            return state, message
        sleep(2.0)


def restart_klipper(url: str, *, why: str = "", sleep=time.sleep,
                    clock=time.monotonic) -> bool:
    """FIRMWARE_RESTART, then wait for Klipper; when it is not ready (the
    restart came while an AMS was still coming back, and Mainsail still asks
    for FIRMWARE_RESTART), send it once more. True when Klipper is ready."""
    for attempt in (1, 2):
        try:
            moonraker(url, "/printer/firmware_restart", post=True)
        except (OSError, ValueError, urllib.error.URLError):
            print("Restart Klipper from Mainsail (FIRMWARE RESTART).")
            return False
        if attempt == 1:
            print("Klipper restarted (FIRMWARE_RESTART)%s." % why)
        else:
            print("Klipper was not ready yet; sent FIRMWARE_RESTART again.")
        sleep(5.0)
        state, message = wait_klipper_settled(url, 60.0, sleep=sleep,
                                              clock=clock)
        if state == "ready":
            print("Klipper is ready.")
            return True
    print("Klipper is %s. If Mainsail still asks for FIRMWARE_RESTART, press "
          "FIRMWARE RESTART there." % (state or "not answering"))
    return False


def cmd_klipper_started(args, *, sleep=time.sleep, clock=time.monotonic) -> int:
    """After a Klipper start (the installer's): the bridge stops an AMS while
    Klipper is stopped, so Klipper can come up in error with "Can not update
    MCU '...' config as it is shutdown". That needs FIRMWARE_RESTART and
    nothing else; send it once, and say what happened."""
    state, message = wait_klipper_settled(args.moonraker, args.wait,
                                          sleep=sleep, clock=clock)
    if state == "ready":
        print("Klipper is ready.")
        return 0
    if state == "error" and SHUTDOWN_AT_START in message:
        print("Klipper started with an AMS still stopped by the bridge; "
              "sending FIRMWARE_RESTART.")
        try:
            moonraker(args.moonraker, "/printer/firmware_restart", post=True)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            print("Could not send it (%s). Run FIRMWARE RESTART in Mainsail."
                  % exc)
            return 1
        sleep(5.0)
        state, message = wait_klipper_settled(args.moonraker, args.wait,
                                              sleep=sleep, clock=clock)
        if state == "ready":
            print("Klipper is ready.")
            return 0
    print("Klipper is %s: %s" % (state or "not answering",
                                 (message or "").strip().splitlines()[0]
                                 if (message or "").strip() else "no message"))
    return 1


# ---------------------------------------------------------------------------
# support
# ---------------------------------------------------------------------------

def cmd_support(args, *, sources=None, ask=input, interactive=None,
                opener=None) -> int:
    """Write a redacted support report; with --submit, offer to send it.

    Nothing is ever sent without a configured destination and a yes typed
    in a terminal (``cortex_bridge.support_submit``)."""
    from cortex_bridge import support_report, support_submit
    sources = sources or support_report.Sources(args)
    if args.file:
        if not args.submit:
            print("--file names a report to send; add --submit.",
                  file=sys.stderr)
            return 2
        path = Path(args.file).expanduser()
        try:
            text = path.read_text("utf-8", errors="replace")
        except OSError as exc:
            print("Cannot read %s: %s" % (path, exc.strerror),
                  file=sys.stderr)
            return 1
        findings = support_report.check_text(text, sources)
        if findings:
            print("Nothing was sent: %s still has something that looks "
                  "private:" % path)
            for check, number, shown in findings[:10]:
                print("  line %d: %s (%s)" % (number, check, shown))
            print("Edit those lines out, or write a new report with "
                  "`cortex-bridge.py support`.")
            return 1
    else:
        report = support_report.build(sources)
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = Path(args.output).expanduser() if args.output else \
            sources.logs_dir / ("cortex-bridge-support-%s.txt" % stamp)
        path.write_text(report.text, encoding="utf-8")
        print("Wrote %s" % path)
        print("  It holds: %s." % ", ".join(report.sections))
        print("  Removed before writing: %s." % report.removed_summary())
        print("  Next step `status` names: %s" % report.next_step)
        if args.print:
            print()
            print(report.text)
    destination = support_submit.configured(Path(args.config_dir), args.to)
    if not args.submit:
        print("Nothing was sent. Mainsail lists the report under Machine > "
              "Log files; attach it to your support request.")
        if destination is not None:
            print("To send it to %s instead, run `cortex-bridge.py support "
                  "--submit`." % destination.url)
        return 0
    print()
    return support_submit.submit(path, destination, ask=ask,
                                 interactive=interactive, opener=opener)


# ---------------------------------------------------------------------------

def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(
        prog="cortex-bridge.py",
        description="Set up, check and update a Cortex Bridge.")
    top.add_argument("--run-dir", default=str(RUN_DIR),
                     help=argparse.SUPPRESS)
    top.add_argument("--config-dir", default=str(default_config_dir()),
                     help="Klipper config folder (default %(default)s)")
    top.add_argument("--klipper-service", default="klipper",
                     help="Klipper's systemd service (default %(default)s)")
    top.add_argument("--moonraker", default=DEFAULT_MOONRAKER,
                     help="Moonraker URL (default %(default)s)")
    commands = top.add_subparsers(dest="command")

    status = commands.add_parser("status", help="what is connected, and the "
                                 "next step")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    configure = commands.add_parser(
        "config", help="add the connected AMS units to oams.cfg")
    configure.add_argument("--write", action="store_true",
                           help="write the file (default: only show)")
    configure.add_argument("--fresh", action="store_true",
                           help="start from the Cortex Bridge sample config")
    configure.add_argument("--replace-openams1", action="store_true",
                           help="comment out the OpenAMS 1 units, their "
                           "boards, groups and wired FPS, and add the "
                           "bridge's AMS in their place")
    configure.add_argument("--file", help="config file (default oams.cfg in "
                           "the config folder)")
    configure.add_argument("--wait", type=float, default=30.0,
                           help="seconds to wait for the AMS (default 30)")
    configure.add_argument("--yes", action="store_true",
                           help="do not ask before stopping Klipper")
    configure.set_defaults(func=cmd_config)

    started = commands.add_parser(
        "klipper-started", help="after a Klipper start, wait for it and "
        "FIRMWARE_RESTART once if an AMS was left stopped by the bridge")
    started.add_argument("--wait", type=float, default=60.0,
                         help="seconds to wait for Klipper (default 60)")
    started.set_defaults(func=cmd_klipper_started)

    ptfe = commands.add_parser("ptfe", help="convert a measured path length "
                               "to ptfe_length")
    ptfe.add_argument("millimetres", type=float)
    ptfe.set_defaults(func=cmd_ptfe)

    version = commands.add_parser(
        "version", help="the firmware version the bridge runs, and whether "
        "it is the newest")
    version.add_argument("--json", action="store_true",
                         help="machine-readable output; exit status 0 when "
                         "the installed version is known, else 1")
    version.add_argument("--wait", type=float, default=RELAY_ANSWER_WAIT,
                         help="seconds to wait for the bridge's answer "
                         "(default %(default)s)")
    version.set_defaults(func=cmd_version)

    firmware = commands.add_parser("firmware", help="list the bundled bridge "
                                   "firmware")
    firmware.set_defaults(func=cmd_firmware)

    update = commands.add_parser("update", help="install bridge firmware")
    update.add_argument("--file", help="an .oac2 package (default: the "
                        "newest bundled one)")
    update.add_argument("--yes", action="store_true",
                        help="do not ask for confirmation")
    update.add_argument("--no-restart", action="store_true",
                        help="do not restart Klipper afterwards")
    update.set_defaults(func=cmd_update)

    amsfw = commands.add_parser(
        "ams-firmware", help="the AMS units' own firmware: what they run and "
        "what Bambu Lab publishes")
    amsfw.add_argument("action", nargs="?", default="list",
                       choices=("list", "fetch", "import", "update",
                                "listen"),
                       help="list (default): versions installed and listed; "
                       "fetch: download and check an image; import: take "
                       "the listed images out of a Bambu offline package; "
                       "update: install a version on one AMS (upgrade or "
                       "downgrade); listen: show AMS loader lines on the "
                       "bus")
    amsfw.add_argument("package", nargs="?", default="",
                       help="with import: the offline package (.zip) from "
                       "bambulab.com > Support > Firmware download")
    amsfw.add_argument("--family", choices=sorted(ams_firmware.FAMILIES),
                       default="", help="with fetch or update --resume: "
                       "ams1, ams2 (AMS 2 Pro) or ams_ht (fetch default "
                       "ams_ht)")
    amsfw.add_argument("--version", default="latest",
                       help="with fetch: a version like 03.00.21.29, or "
                       "latest (default)")
    amsfw.add_argument("--board", default="",
                       help="with fetch or update: only this board revision "
                       "(rev7, rev8, ...)")
    amsfw.add_argument("--unit", default="",
                       help="with update: the AMS, by its id or bus index "
                       "(see list)")
    amsfw.add_argument("--yes", action="store_true",
                       help="with update: do not ask for confirmation")
    amsfw.add_argument("--force", action="store_true",
                       help="with update: install even the version it runs")
    amsfw.add_argument("--resume", action="store_true",
                       help="with update: the AMS waits in its loader and is "
                       "not listed; name it with --unit <bus index> and "
                       "--family")
    amsfw.add_argument("--wait-loader", type=int, default=0, metavar="S",
                       help="with update: keep asking for the AMS's loader "
                       "for up to S seconds (switch the AMS on meanwhile)")
    amsfw.add_argument("--no-restart", action="store_true",
                       help="with update: do not FIRMWARE_RESTART Klipper "
                       "afterwards")
    amsfw.add_argument("--seconds", type=int, default=15,
                       help="with listen: how long (default %(default)s)")
    amsfw.add_argument("--verbose", action="store_true",
                       help="with update: also print the AMS loader's lines")
    amsfw.set_defaults(func=cmd_ams_firmware)

    support = commands.add_parser(
        "support", help="write a support report with private details "
        "removed; --submit offers to send it")
    support.add_argument("--output", help="report file (default "
                         "cortex-bridge-support-<date>.txt in the log "
                         "folder)")
    support.add_argument("--logs-dir", default=None,
                         help="Klipper's log folder (default %s)" %
                         default_logs_dir())
    support.add_argument(
        "--log-file", default="~/printer_data/logs/cortex-bridge.jsonl",
        help="the relay's support log")
    support.add_argument("--print", action="store_true",
                         help="also print the report")
    support.add_argument("--submit", action="store_true",
                         help="after writing it, show what would be sent "
                         "and send it if you answer yes (needs a "
                         "destination: support_url in [openams_bridge], "
                         "moonraker.conf, or --to)")
    support.add_argument("--to", metavar="URL",
                         help="with --submit: send to this https URL")
    support.add_argument("--file", help="with --submit: send this existing "
                         "report instead of writing a new one")
    support.set_defaults(func=cmd_support)
    return top


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    if not getattr(args, "func", None):
        parser().print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
