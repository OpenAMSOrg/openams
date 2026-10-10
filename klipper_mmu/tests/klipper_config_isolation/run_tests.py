#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise native Klipper config/output isolation in a disposable source copy."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

PACKAGE = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--klipper-source', type=Path, required=True)
    parser.add_argument('--results', type=Path, required=True)
    args = parser.parse_args()
    results = args.results.resolve()
    results.mkdir(parents=True, exist_ok=False)
    source = args.klipper_source.resolve()
    work = results / 'klipper'
    shutil.copytree(source, work, ignore=shutil.ignore_patterns('.git', 'out', '__pycache__'))
    commands = []

    def run(*command):
        result = subprocess.run(list(map(str, command)), cwd=work,
                                text=True, capture_output=True, timeout=60)
        commands.append({'argv': list(map(str, command)), 'returncode': result.returncode})
        with (results / 'commands.log').open('a') as log:
            log.write(result.stdout + result.stderr)
        result.check_returncode()

    for action in ('install-driver-api', 'install'):
        run(sys.executable, PACKAGE / 'tools/install.py', action, work)
    printer_config = work / '.config'
    printer_config.write_text('# Printer configuration must remain byte-for-byte unchanged\n')
    printer_out = work / 'out'
    printer_out.mkdir(exist_ok=True)
    (printer_out / 'autoconf.h').write_text('/* Printer build must not be overwritten */\n')
    preserved = {p: p.read_bytes() for p in (printer_config, printer_out / 'autoconf.h')}
    mmu_config = results / 'mmu.config'
    mmu_config.write_text('CONFIG_MACH_STM32=y\nCONFIG_MACH_STM32H723=y\n'
                          'CONFIG_STM32_USB_PA11_PA12=y\nCONFIG_KLIPPER_MMU=y\n')
    mmu_out = results / 'mmu-out'
    common = ('make', f'KCONFIG_CONFIG={mmu_config}', f'OUT={mmu_out}/')
    run(*common, 'olddefconfig', mmu_out / 'autoconf.h')
    saved = mmu_config.read_bytes()
    assert b'CONFIG_KLIPPER_MMU=y' in saved
    assert '#define CONFIG_KLIPPER_MMU 1' in (mmu_out / 'autoconf.h').read_text()
    run(*common, 'olddefconfig')
    assert mmu_config.read_bytes() == saved, 'native configuration round trip changed values'
    for path, original in preserved.items():
        assert path.read_bytes() == original, f'overwrote printer file: {path}'
    # Exercise the documented clean target; distclean is deliberately excluded.
    run(*common, 'clean')
    assert not mmu_out.exists()
    assert mmu_config.read_bytes() == saved
    for path, original in preserved.items():
        assert path.read_bytes() == original, f'clean overwrote printer file: {path}'
    (results / 'summary.json').write_text(json.dumps({
        'status': 'passed', 'hardware_access': False, 'commands': commands,
        'mmu_config_sha256': hashlib.sha256(saved).hexdigest(),
        'checks': ['dedicated_config', 'dedicated_header', 'native_roundtrip',
                   'printer_config_preserved', 'printer_output_preserved',
                   'isolated_clean_preserves_config'],
        'limitations': 'Kconfig/header isolation; no ARM firmware link or interactive UI test.',
    }, indent=2) + '\n')
    print(f'PASS: native configuration/output isolation; {results}/summary.json')


if __name__ == '__main__':
    main()
