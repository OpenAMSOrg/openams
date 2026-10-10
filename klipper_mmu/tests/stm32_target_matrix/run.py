#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Compile-only Klipper STM32 target survey for the optional MMU add-on."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

PACKAGE = Path(__file__).resolve().parents[2]
PROJECT = PACKAGE.parent
PIN = "ce7002bedf37e938bb483572949f3703ac6476cb"
ARCHIVE = PROJECT / "build" / f"klipper-{PIN}.tar.gz"
ARCHIVE_SHA256 = "da1c6223d90652cda6e826e3cf896cf97d6e11aa13aa4c8feb5232dac068f70b"
REPRESENTATIVE = ("MACH_STM32F072", "MACH_STM32F207",
                  "MACH_STM32F407", "MACH_STM32F765", "MACH_STM32G070",
                  "MACH_STM32G431", "MACH_STM32H723")
# Pinned upstream serial.c does not compile for these register layouts. Their
# selectable USB transport avoids confusing an upstream serial failure with an
# MMU compile failure.
USB_TARGETS = ("MACH_STM32F765",)


def run(*args, cwd, timeout=180):
    result = subprocess.run(args, cwd=cwd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{' '.join(map(str, args))}: {result.stdout[-5000:]}")
    return result.stdout


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def targets(root):
    code = '''import os, sys
sys.path.insert(0, os.path.abspath("lib/kconfiglib"))
import kconfiglib
k = kconfiglib.Kconfig("src/Kconfig")
k.syms["MACH_STM32"].set_value(2)
for name, sym in sorted(k.syms.items()):
    if not name.startswith("MACH_STM32") or name == "MACH_STM32": continue
    if not sym.visibility or not any(node.prompt for node in sym.nodes): continue
    if not sym.set_value(2): continue
    if k.syms["KLIPPER_MMU"].visibility:
        print(name)
    sym.set_value(0)
'''
    names = run(sys.executable, "-c", code, cwd=root).splitlines()
    return names


def configure(root, target, enabled):
    transport = "STM32_USB_PA11_PA12" if target in USB_TARGETS else "STM32_SERIAL_USART1"
    code = '''import os, sys
sys.path.insert(0, os.path.abspath("lib/kconfiglib"))
import kconfiglib
k = kconfiglib.Kconfig("src/Kconfig")
def set_(n, v):
    if not k.syms[n].set_value(v): raise SystemExit("invalid selection: " + n)
set_("MACH_STM32", "y")
set_(sys.argv[1], "y")
set_(sys.argv[3], "y")
set_("KLIPPER_MMU", "y" if sys.argv[2] == "on" else "n")
if sys.argv[2] == "on":
    set_("KLIPPER_MMU_DEVICE_CUSTOM", "y")
    set_("KLIPPER_MMU_BOARD_CUSTOM", "y")
    set_("KLIPPER_MMU_PIN_CONFIG", "y")
    set_("KLIPPER_MMU_LANE_COUNT", "1")
    for role, pin in (("STEP", "PA1"), ("DIR", "PA2"), ("ENABLE", "PA3"),
                      ("UART", "PA4"), ("BAY", "PA5"), ("HUB", "PA6"),
                      ("TOOLHEAD", "PA7"), ("BUFFER_A", "PB0"), ("BUFFER_B", "PB1")):
        set_("KLIPPER_MMU_PINS_" + role, pin)
k.write_config(".config")
'''
    run(sys.executable, "-c", code, target, "on" if enabled else "off",
        transport, cwd=root)
    run("make", "olddefconfig", cwd=root)
    config = (root / ".config").read_text()
    expected = [f"CONFIG_{target}=y", f"CONFIG_{transport}=y"]
    expected += (["CONFIG_KLIPPER_MMU=y", "CONFIG_KLIPPER_MMU_DEVICE_CUSTOM=y",
                  "CONFIG_KLIPPER_MMU_BOARD_CUSTOM=y", "CONFIG_KLIPPER_MMU_LANE_COUNT=1"]
                 if enabled else ["# CONFIG_KLIPPER_MMU is not set"])
    for item in expected:
        if item not in config.splitlines():
            raise RuntimeError(f"Kconfig did not preserve {item}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="build every selectable STM32 model")
    parser.add_argument("--targets", nargs="+", help="Kconfig symbols, e.g. MACH_STM32H723")
    parser.add_argument("--archive", type=Path, default=ARCHIVE)
    parser.add_argument("--build-dir", type=Path, default=PACKAGE / "build/stm32-target-matrix")
    args = parser.parse_args()
    archive, out = args.archive.resolve(), args.build_dir.resolve()
    if out.exists():
        raise SystemExit(f"refusing existing output directory: {out}")
    if not archive.is_file() or sha(archive) != ARCHIVE_SHA256:
        raise SystemExit(f"missing or incorrect pinned Klipper archive: {archive}")
    out.mkdir(parents=True)
    root = out / "klipper"
    root.mkdir()
    with tarfile.open(archive, "r:gz") as src:
        prefix = f"klipper-{PIN}/"
        for member in src.getmembers():
            if (not (member.name == prefix[:-1] and member.isdir()
                    or member.name.startswith(prefix)) or member.issym() or member.islnk()
                    or not (member.isfile() or member.isdir())):
                raise SystemExit(f"unexpected archive member: {member.name}")
            member.name = member.name[len(prefix):]
            if member.name:
                src.extract(member, root, filter="data")
    for action in ("install-driver-api", "install"):
        run(sys.executable, PACKAGE / "tools/install.py", action, root, cwd=PACKAGE, timeout=30)
    available = targets(root)
    selected = args.targets or (available if args.all else list(REPRESENTATIVE))
    results = []
    for target in selected:
        record = {"target": target, "upstream_commit": PIN,
                  "upstream_archive_sha256": ARCHIVE_SHA256,
                  "transport": "USB PA11/PA12" if target in USB_TARGETS else "USART1 PA10/PA9",
                  "support_scope": "core compile-only; physical support not established"}
        try:
            if target not in available:
                raise RuntimeError("target is not selectable by pinned Klipper Kconfig")
            for enabled in (False, True):
                name = "enabled" if enabled else "disabled"
                configure(root, target, enabled)
                run("make", "-j2", cwd=root)
                image = out / "artifacts" / target.lower() / name
                image.mkdir(parents=True)
                for filename in (".config", "klipper.elf", "klipper.bin", "klipper.dict", "autoconf.h"):
                    source = root / filename if filename == ".config" else root / "out" / filename
                    if not source.is_file():
                        raise RuntimeError(f"missing build artifact: {filename}")
                    shutil.copy2(source, image / filename)
                record.setdefault("configurations", {})[name] = {
                    f: sha(image / f) for f in (".config", "klipper.elf", "klipper.bin", "klipper.dict")}
                run("make", "clean", cwd=root, timeout=30)
            record["result"] = "pass"
        except Exception as exc:
            record["result"] = "fail"
            record["failure"] = str(exc)
            try:
                run("make", "clean", cwd=root, timeout=30)
            except Exception:
                pass
        (out / f"{target.lower()}.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
        results.append(record)
        print(f"{target}: {record['result']}{': ' + record['failure'] if 'failure' in record else ''}")
    (out / "summary.json").write_text(json.dumps(results, indent=2, sort_keys=True) + "\n")
    if any(x["result"] != "pass" for x in results):
        raise SystemExit(1)
    print(f"Artifacts: {out}")


if __name__ == "__main__":
    main()
