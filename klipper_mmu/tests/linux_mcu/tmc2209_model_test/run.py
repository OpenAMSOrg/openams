#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Native unit test for the sim's TMC2209 model (sim/board/tmc2209.c) alone.

Compiles it with a tiny scheduler/GPIO-free shim (shim/) plus the real
firmware CRC/framing helpers it reuses (src/tmc2209.c, src/tmc_uart_codec.c),
and drives it with synthetic edges from test_tmc2209_model.c. No Klipper
build, no klippy, no pty: this is the cheap check tests/linux_mcu/README.md
describes, separate from the full boot gate in build.py/*_boot_check.py.
"""
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
PACKAGE = HERE.parents[2]  # tmc2209_model_test/ -> linux_mcu/ -> tests/ -> package root


def main():
    build_dir = PACKAGE / 'build' / 'tmc2209-model-test'
    build_dir.mkdir(parents=True, exist_ok=True)
    binary = build_dir / 'test_tmc2209_model'

    sources = [
        HERE / 'test_tmc2209_model.c',
        PACKAGE / 'sim' / 'board' / 'tmc2209.c',
        PACKAGE / 'src' / 'tmc2209.c',
        PACKAGE / 'src' / 'tmc_uart_codec.c',
    ]
    includes = [HERE / 'shim', PACKAGE / 'sim' / 'board', PACKAGE / 'include']
    command = ['gcc', '-std=gnu11', '-O1', '-g', '-Wall', '-Wextra',
               '-fsanitize=address,undefined', '-fno-sanitize-recover=all']
    command += ['-I' + str(p) for p in includes]
    # The model answers only in an image with the shared TMC UART driver.
    command += ['-DCONFIG_WANT_TMCUART_LOCAL=1', '-DCONFIG_KLIPPER_MMU_TMC_UART_SHARED=1']
    command += [str(p) for p in sources]
    command += ['-o', str(binary)]
    print('Building:', ' '.join(command))
    subprocess.run(command, check=True, timeout=60)

    print('Running:', binary)
    result = subprocess.run([str(binary)], timeout=15,
                            env={'ASAN_OPTIONS': 'detect_leaks=0:halt_on_error=1',
                                 'UBSAN_OPTIONS': 'halt_on_error=1'})
    if result.returncode != 0:
        print('tmc2209 model test FAILED')
        sys.exit(1)
    print('tmc2209 model test passed.')


if __name__ == '__main__':
    main()
