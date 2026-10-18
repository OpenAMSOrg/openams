# SPDX-License-Identifier: MIT
"""`cortex-bridge.py`: set up, check and update a Cortex Bridge.

    python3 ~/openams/tools/cortex-bridge.py status
    python3 ~/openams/tools/cortex-bridge.py config --write
    python3 ~/openams/tools/cortex-bridge.py ptfe 2050
    python3 ~/openams/tools/cortex-bridge.py update

Runs with any python3; it needs no extra packages. The relay service does all
USB work, so this command only reads its status and sends it requests.
"""

from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cortex_bridge import config as bridge_config  # noqa: E402
from cortex_bridge import firmware as bridge_firmware  # noqa: E402

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

def gather(args) -> dict:
    status = read_status(Path(args.run_dir))
    return {
        "usb": usb_devices(),
        "relay_service": service_state(SERVICE),
        "klipper_service": service_state(args.klipper_service),
        "relay": status,
        "configured": sorted(configured_uids(Path(args.config_dir))),
        "bundled_firmware": [
            {key: package.get(key) for key in
             ("version", "file", "released", "notes", "ok", "problem")}
            for package in bridge_firmware.bundled_packages()
        ],
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
    return "Everything is connected."


def cmd_status(args) -> int:
    facts = gather(args)
    if args.json:
        print(json.dumps(facts, indent=1, sort_keys=True))
        return 0
    relay = facts["relay"] or {}
    print("Cortex Bridge")
    if not facts["usb"]:
        print("  USB:         not connected")
    for device in facts["usb"]:
        label = {"application": "running", "receiver": "waiting for "
                 "firmware", "fault": "FAULT %s" % device["serial"]}[
                     device["state"]]
        image = {"SiForge": ", production firmware",
                 "OpenAMS": ", lab firmware"}.get(device["manufacturer"], "")
        print("  USB:         %s, serial %s%s%s" % (
            label, device["serial"] or "?", image,
            (" on " + device["device"]) if device["device"] else ""))
    print("  Relay:       %s%s" % (
        facts["relay_service"],
        (" (%s)" % relay.get("detail")) if relay.get("detail") else ""))
    units = relay.get("units", [])
    configured = set(facts["configured"])
    if units:
        print("  AMS units:")
        for unit in units:
            print("    %s  %-7s  %s  %s" % (
                unit["uid"], "online" if unit.get("online") else "offline",
                unit["path"], "configured" if unit["uid"] in configured
                else "NOT in config"))
    else:
        print("  AMS units:   none")
    latest = next((p for p in facts["bundled_firmware"] if p["ok"]), None)
    print("  Firmware:    bundled %s" % (
        "version %s" % latest["version"] if latest else "none yet"))
    update = relay.get("update")
    if update:
        print("  Last update: %s %s" % (
            update.get("result") or update.get("phase"),
            update.get("message") or ""))
    print("  Klipper:     %s" % facts["klipper_service"])
    print()
    print(next_step(facts))
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
    try:
        text, names = bridge_config.add_units(base, described)
    except bridge_config.ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if not names:
        print("Every online AMS is already in %s." % target)
    elif not args.write:
        print()
        print(text[len(base.rstrip("\n")):].strip("\n"))
        print()
        print("Nothing written. Run again with --write to add this to %s."
              % target)
    else:
        backup = bridge_config.write_with_backup(target, text)
        print("Added %s to %s%s." % (", ".join(names), target,
                                     " (old file kept as %s)" % backup.name
                                     if backup else ""))
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
# firmware and update
# ---------------------------------------------------------------------------

def cmd_firmware(args) -> int:
    packages = bridge_firmware.bundled_packages()
    if not packages:
        print("No bridge firmware is bundled with this OpenAMS version yet.")
        print("New firmware arrives with OpenAMS updates (Mainsail: Machine "
              "> Update Manager).")
        return 0
    for package in packages:
        print("version %-4s %-10s %s%s" % (
            package.get("version"), package.get("released", ""),
            package.get("notes", ""),
            "" if package["ok"] else "  [%s]" % package["problem"]))
    return 0


PHASE_TEXT = {
    "entering": "Asking the bridge to enter update mode",
    "waiting-receiver": "Waiting for the bridge's update receiver",
    "authenticating": "The bridge is checking the package",
    "writing": "Writing the firmware",
    "restarting": "The bridge is restarting",
    "done": "Done",
}

ERROR_ADVICE = {
    "UpdateModeRefused": "The bridge said it is busy (loading, unloading or "
                         "drying). Wait until the AMS is idle and run the "
                         "update again.",
    "PackageRefused": "That file is not Cortex Bridge firmware.",
    "ReceiverRejected": "The bridge refused this package, usually because "
                        "it already runs this version or a newer one.",
    "BridgeAmbiguous": "Several bridges are attached. Leave only the one to "
                       "update on USB and run the update again.",
    "BridgeFault": "The bridge reports a fault. Run `cortex-bridge.py "
                   "support` and contact support.",
    "TransferInterrupted": "The transfer stopped part-way. The bridge keeps "
                           "waiting for firmware; run the update again.",
    "BridgeNotFound": "The bridge is not on USB. Check the USB cable.",
}


def update_direct(package_path: Path) -> int:
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

    try:
        report = updater.update_bridge(package_path.read_bytes(),
                                       progress=progress)
    except OSError as exc:
        print("Update failed: %s" % exc, file=sys.stderr)
        return 1
    except updater.UpdateError as exc:
        print("Update failed: %s" % exc, file=sys.stderr)
        advice = ERROR_ADVICE.get(type(exc).__name__)
        if advice:
            print(advice, file=sys.stderr)
        return 1
    print("Installed firmware version %s on bridge %s."
          % (report["version"], report.get("serial_number", "")))
    return 0


def cmd_update(args) -> int:
    run_dir = Path(args.run_dir)
    if args.file:
        package_path = Path(args.file).expanduser().resolve()
        source = "file"
        label = package_path.name
    else:
        package = bridge_firmware.latest_bundled()
        if package is None:
            print("No bridge firmware is bundled with this OpenAMS version "
                  "yet. Update OpenAMS to receive new firmware when it is "
                  "released.")
            return 0
        package_path = Path(package["path"])
        source = "bundled"
        label = "version %s" % package.get("version")
    # With the relay running it owns the bridge's port and installs the
    # package; without it this command talks to the bridge itself.
    try:
        control_request({"method": "status"}, run_dir=run_dir, timeout=10)
        relayed = True
    except OSError:
        relayed = False
    state = printer_state(args.moonraker)
    if state in ("printing", "paused"):
        print("The printer is %s. Update the bridge when it is idle." % state,
              file=sys.stderr)
        return 1
    print("This installs Cortex Bridge firmware %s." % label)
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
        return update_direct(package_path)

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
    if not result.get("ok"):
        error = result.get("error", "error")
        print("Update failed: %s" % result.get("message", error),
              file=sys.stderr)
        advice = ERROR_ADVICE.get(error)
        if advice:
            print(advice, file=sys.stderr)
        return 1
    print("Installed. Waiting for the AMS to come back...")
    units = wait_for_units(run_dir, 90)
    print("  %d AMS online" % len(units))
    if not args.no_restart:
        try:
            moonraker(args.moonraker, "/printer/firmware_restart", post=True)
            print("Klipper restarted (FIRMWARE_RESTART).")
        except (OSError, ValueError, urllib.error.URLError):
            print("Restart Klipper from Mainsail (FIRMWARE RESTART).")
    return 0


# ---------------------------------------------------------------------------
# support
# ---------------------------------------------------------------------------

def _command_output(command: list) -> str:
    try:
        return subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return "(%s: %s)\n" % (" ".join(command), exc)


def cmd_support(args) -> int:
    facts = gather(args)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = Path(args.output) if args.output else \
        default_logs_dir() / ("cortex-bridge-support-%s.txt" % stamp)
    log_path = Path(args.log_file).expanduser()
    lines = []
    try:
        lines = log_path.read_text("utf-8", errors="replace") \
            .splitlines()[-400:]
    except OSError as exc:
        lines = ["(no support log at %s: %s)" % (log_path, exc)]
    report = [
        "Cortex Bridge support report, %s" % stamp,
        "",
        "== facts",
        json.dumps(facts, indent=1, sort_keys=True),
        "",
        "== openams",
        _command_output(["git", "-C", str(OPENAMS_DIR), "describe",
                         "--always", "--dirty", "--tags"]),
        "== relay service",
        _command_output(["systemctl", "status", SERVICE + ".service",
                         "--no-pager", "-n", "40"]),
        "== support log (last %d lines)" % len(lines),
    ] + lines
    path.write_text("\n".join(report) + "\n", encoding="utf-8")
    print("Wrote %s" % path)
    print("Mainsail lists it under Machine > Log files; attach it to your "
          "support request.")
    return 0


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
    configure.add_argument("--file", help="config file (default oams.cfg in "
                           "the config folder)")
    configure.add_argument("--wait", type=float, default=30.0,
                           help="seconds to wait for the AMS (default 30)")
    configure.add_argument("--yes", action="store_true",
                           help="do not ask before stopping Klipper")
    configure.set_defaults(func=cmd_config)

    ptfe = commands.add_parser("ptfe", help="convert a measured path length "
                               "to ptfe_length")
    ptfe.add_argument("millimetres", type=float)
    ptfe.set_defaults(func=cmd_ptfe)

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

    support = commands.add_parser("support", help="write a support report")
    support.add_argument("--output", help="report file")
    support.add_argument(
        "--log-file", default="~/printer_data/logs/cortex-bridge.jsonl",
        help="the relay's support log")
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
