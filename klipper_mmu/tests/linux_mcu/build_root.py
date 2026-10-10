# SPDX-License-Identifier: GPL-3.0-or-later
"""Locate the whole-stack Linux-MCU fixtures: the pinned Klipper tarball
and the integration klippy virtualenv.

Both live under one build root, which defaults to ``<klipper_mmu>/build``
and is overridable with ``KLIPPER_MMU_BUILD_ROOT``.  Paths are computed
from this file's location, so the checks work from any working directory
and whether or not klipper_mmu is nested in a larger repository.
"""
import os
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
BUILD_ROOT = Path(os.environ.get("KLIPPER_MMU_BUILD_ROOT") or PACKAGE_ROOT / "build")
PINNED_ARCHIVE = "klipper-ce7002bedf37e938bb483572949f3703ac6476cb.tar.gz"
KLIPPY_PYTHON = BUILD_ROOT / "integration-env" / "klippy" / "bin" / "python"

# What each missing input is, so a skipped check can say what to provide.
PROVIDE = (
    "the pinned Klipper archive %s" % (BUILD_ROOT / PINNED_ARCHIVE),
    "arm-none-eabi-gcc",
    "the integration klippy venv at %s" % KLIPPY_PYTHON,
)


def missing():
    """Inputs the whole-stack checks need that this machine does not have."""
    gaps = []
    if not (BUILD_ROOT / PINNED_ARCHIVE).is_file():
        gaps.append("pinned Klipper archive %s" % (BUILD_ROOT / PINNED_ARCHIVE))
    if not KLIPPY_PYTHON.exists():
        gaps.append("integration klippy venv at %s" % KLIPPY_PYTHON)
    from shutil import which
    if which("arm-none-eabi-gcc") is None:
        gaps.append("arm-none-eabi-gcc")
    return gaps


def skip_unless_available(check):
    """Exit 0 with an explanation when a required input is absent.

    The whole-stack checks are optional in `make check`: tools/check.py
    lists them as needing the pinned archive, the ARM toolchain and the
    integration venv, so a machine without them must skip, not fail.
    """
    gaps = missing()
    if not gaps:
        return
    print("%s: skipped, needs %s" % (check, ", ".join(gaps)), file=sys.stderr)
    print("Provide them under %s (or set KLIPPER_MMU_BUILD_ROOT); %s"
          % (BUILD_ROOT, "tools/check.py lists this as an optional check."),
          file=sys.stderr)
    raise SystemExit(0)
