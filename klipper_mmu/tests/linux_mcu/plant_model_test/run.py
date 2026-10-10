#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native unit tests for the sim plant's selector mode (sim/board/plant.c),
next to tests/linux_mcu/tmc2209_model_test's own native check of the board
model. Two standalone binaries, no Kconfig/Klipper build, no klippy, no
pty:

  test_plant_default.c  -- the plant's original four-lane, no-selector mode
                            (BoxTurtle/NightOwl/EMU) still behaves exactly
                            as before the selector mode was added next to it.
  test_plant_five_lane.c -- a non-selector image with more than four lanes
                            (EMU, 5): the real fifth lane drives on its own
                            extended pins without disturbing lanes 0-3, and
                            vice versa.
  test_plant_no_bay.c   -- a device with no per-lane BAY switch at all
                            (Angry Beaver/3MS/QuattroBox 1.0/1.1/2): a lane
                            starting "empty" (filament at the spool, no BAY
                            switch to have been inserted up to) still moves
                            on its first STEP pulse and can feed all the way
                            to the shared HUB.
  test_plant_selector.c -- the new mode, shaped like the ERCF 2.0 sim image
                            (LINEAR selector + grip servo, 8 lanes):
                            homing, per-gate engagement (axis position AND,
                            for LINEAR, the servo's pulse), the shared gear
                            only ever moving the currently engaged gate, and
                            the ground-truth wire format's three new fields.
  test_plant_indexed.c  -- INDEXED, shaped like the BTT ViViD sim image (a
                            real gate_order, no servo, no gate position
                            table): home lands right on gate 0's own index
                            switch (unlike LINEAR/ROTARY's CAD-offset gate
                            0), the drum wraps both forward and reverse
                            (including a negative raw axis position), and
                            the shared gear still only ever moves the
                            currently engaged gate.

Each is compiled against its own fake autoconf.h (shim_default/,
shim_selector/) reusing tmc2209_model_test's own scheduler/GPIO-free shim
for sched.h/board/misc.h/stm32/internal.h, plus a `klipper_mmu` symlink to
the package root (the same trick tools/install.py's own `src/klipper_mmu`
symlink uses) so plant.c's `#include "klipper_mmu/mcu/...h"` lines resolve
without a real Klipper checkout.
"""
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
PACKAGE = HERE.parents[2]  # plant_model_test/ -> linux_mcu/ -> tests/ -> package root
TMC_SHIM = PACKAGE / 'tests' / 'linux_mcu' / 'tmc2209_model_test' / 'shim'

BINARIES = [
    ('default', 'test_plant_default.c', 'test_plant_default'),
    ('five_lane', 'test_plant_five_lane.c', 'test_plant_five_lane'),
    ('no_bay', 'test_plant_no_bay.c', 'test_plant_no_bay'),
    ('selector', 'test_plant_selector.c', 'test_plant_selector'),
    ('indexed', 'test_plant_indexed.c', 'test_plant_indexed'),
]


def build_and_run(build_dir, own_shim, source_name, binary_name):
    build_dir.mkdir(parents=True, exist_ok=True)
    link = build_dir / 'klipper_mmu'
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(PACKAGE)

    binary = build_dir / binary_name
    sources = [
        HERE / source_name,
        HERE / 'timer_shim.c',
        PACKAGE / 'sim' / 'board' / 'plant.c',
    ]
    includes = [own_shim, TMC_SHIM, PACKAGE / 'sim' / 'board',
               build_dir, PACKAGE / 'include']
    command = ['gcc', '-std=gnu11', '-O1', '-g', '-Wall', '-Wextra',
               '-fsanitize=address,undefined', '-fno-sanitize-recover=all']
    command += ['-I' + str(p) for p in includes]
    command += [str(p) for p in sources]
    command += ['-o', str(binary)]
    print('Building:', ' '.join(command))
    subprocess.run(command, check=True, timeout=60)

    print('Running:', binary, build_dir)
    result = subprocess.run([str(binary), str(build_dir)], timeout=15,
                            env={'ASAN_OPTIONS': 'detect_leaks=0:halt_on_error=1',
                                 'UBSAN_OPTIONS': 'halt_on_error=1'})
    return result.returncode == 0


def main():
    build_root = PACKAGE / 'build' / 'plant-model-test'
    failed = []
    for name, source_name, binary_name in BINARIES:
        own_shim = HERE / ('shim_%s' % name)
        build_dir = build_root / name
        ok = build_and_run(build_dir, own_shim, source_name, binary_name)
        if not ok:
            failed.append(name)
    if failed:
        print('plant model test(s) FAILED: %s' % ', '.join(failed))
        sys.exit(1)
    print('plant model tests passed: %s' % ', '.join(n for n, _, _ in BINARIES))


if __name__ == '__main__':
    main()
