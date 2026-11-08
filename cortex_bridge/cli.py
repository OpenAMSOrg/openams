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
        return ("No AMS answers. Check 24 V at PWR IN (red LED on), the "
                "4-pin BUFFER cable, and the 6-pin cable from the AMS to the "
                "buffer.")
    missing = [unit for unit in units
               if unit["uid"] not in facts["configured"]]
    if missing:
        return ("%d AMS online but not in your config. Run "
                "`cortex-bridge.py config --write`." % len(missing))
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
    if units:
        lines.append("  AMS units:")
        for unit in units:
            lines.append("    %s  %-7s  %s  %s" % (
                unit["uid"], "online" if unit.get("online") else "offline",
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
        lines.append("  Last update: %s %s" % (
            update.get("result") or update.get("phase"),
            update.get("message") or ""))
    lines.append("  Klipper:     %s" % facts["klipper_service"])
    return lines


ALL_CONNECTED = "Everything is connected."
SUPPORT_HINT = ("If that does not fix it, `cortex-bridge.py support` writes a "
                "report for support. It stays on this printer until you send "
                "it.")


def cmd_status(args) -> int:
    facts = gather(args)
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
        print("Next: measure each AMS's filament path and set ptfe_length "
              "(`cortex-bridge.py ptfe <millimetres>`).")
    return 0


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
        try:
            moonraker(args.moonraker, "/printer/firmware_restart", post=True)
            print("Klipper restarted (FIRMWARE_RESTART).")
        except (OSError, ValueError, urllib.error.URLError):
            print("Restart Klipper from Mainsail (FIRMWARE RESTART).")
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
