#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Materialize a patched Klipper `src/` for tests that link real stepper.c.

Copies tests/fixtures/shared_tmcuart into TARGET, then runs
tools/install.py's integrate_driver_api() on it so TARGET/src/stepper.c (and
stepper.h) carry the WANT_STEPPER_LOCAL extension from
upstream/drivers-local.patch, same as tools/install.py install-driver-api.

Used as a Makefile prerequisite by test suites that compile the real,
patched stepper.c against a header shim (tests/klipper_stepper_local/shim,
tests/klipper_stepdir/shim_include) instead of a hand-written fake stepper.c:
see tests/klipper_stepper_local/run_tests.py for the pattern this factors out
for tests/klipper_controller and tests/klipper_stepdir.

Usage: python3 tools/klipper_src_fixture.py TARGET_DIR [--package DIR]

TARGET_DIR is removed and recreated on every call (cheap, and avoids a stale
patch surviving a source change); print TARGET_DIR/src on success so a
Makefile recipe can capture it if needed, though most callers just depend on
TARGET_DIR/src/stepper.c existing.
"""
import argparse
import importlib.util
import shutil
import sys
from pathlib import Path


def build(target: Path, package: Path) -> Path:
    fixture = package / "tests/fixtures/shared_tmcuart"
    if not fixture.is_dir():
        raise SystemExit(f"missing fixture: {fixture}")
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(fixture, target)
    # integrate_driver_api() expects a product checkout layout.
    (target / "Makefile").write_text("# native fixture\n")
    (target / "klippy").mkdir(exist_ok=True)

    spec = importlib.util.spec_from_file_location(
        "klipper_mmu_install", package / "tools/install.py")
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    action = installer.integrate_driver_api(target, "install", package=package)
    if action != "installed":
        raise SystemExit(f"driver API bundle install returned {action!r}")
    return target / "src"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("target", type=Path,
                        help="directory to (re)create as a patched Klipper checkout")
    parser.add_argument("--package", type=Path,
                        default=Path(__file__).resolve().parents[1],
                        help="klipper-mmu checkout root (default: derived)")
    args = parser.parse_args()
    src = build(args.target.resolve(), args.package.resolve())
    print(src)
    return 0


if __name__ == "__main__":
    sys.exit(main())
