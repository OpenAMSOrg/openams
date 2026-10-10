# SPDX-License-Identifier: GPL-3.0-or-later
"""Test the route adapter with actual reviewed upstream PWM source, no hardware."""
from pathlib import Path
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import argparse
import importlib.util

HERE = Path(__file__).resolve().parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--package', type=Path, default=HERE.parents[1])
parser.add_argument('results', type=Path)
args = parser.parse_args()
CANDIDATE = args.package.resolve()
RESULTS = args.results.resolve()
RESULTS.mkdir(parents=True, exist_ok=True)
report = {'status': 'failed', 'modes': {}, 'hardware_accessed': False}
# Independent expected claims recorded before removing the old resource table.
expected = json.loads((HERE / 'expected_routes.json').read_text())
assert len(expected) == 49
try:
    with tempfile.TemporaryDirectory(prefix='km-resource-route-') as temp:
        work = Path(temp)
        checkout = work / 'upstream'
        shutil.copytree(CANDIDATE/'tests/fixtures/shared_tmcuart', checkout)
        (checkout/'Makefile').write_text('# native fixture\n')
        (checkout/'klippy').mkdir()
        spec = importlib.util.spec_from_file_location('installer', CANDIDATE/'tools/install.py')
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        assert installer.integrate_driver_api(checkout, 'install', CANDIDATE) == 'installed'
        assert installer.integrate_driver_api(checkout, 'check', CANDIDATE) == 'installed'
        upstream = checkout / 'src/stm32'
        report['upstream_sha256'] = {name: hashlib.sha256((upstream/name).read_bytes()).hexdigest()
                                    for name in ('hard_pwm.c', 'pwm.h', 'gpio.h')}
        for mode in ('off', 'on'):
            build = work / mode
            shutil.copytree(HERE / 'shim', build)
            shutil.copy2(build/'stm32/internal.h', build/'internal.h')
            shutil.copy2(upstream/'pwm.h', build/'stm32/pwm.h')
            for name in ('hard_pwm.c', 'pwm.h', 'gpio.h'):
                shutil.copy2(upstream/name, build/name)
            (build/'autoconf.h').write_text(
                '#define CONFIG_MACH_STM32H7 1\n#define CONFIG_MACH_STM32H723 1\n'
                '#define CONFIG_KLIPPER_MMU_PIN_CONFIG 0\n#define CONFIG_CLOCK_FREQ 520000000\n'
                f'#define CONFIG_WANT_HARD_PWM_QUERY {int(mode == "on")}\n')
            flags = ['gcc','-std=gnu11','-Wall','-Wextra','-Werror','-g','-O2',
                     '-fsanitize=address,undefined','-fno-sanitize-recover=all','-fno-pie',
                     '-I'+str(build),'-I'+str(CANDIDATE/'mcu'),'-I'+str(CANDIDATE/'include')]
            objects=[]
            with (RESULTS/(mode+'-build.log')).open('w') as log:
                for name, src, extra in (
                    ('driver', build/'hard_pwm.c', ['-Wno-pointer-to-int-cast']),
                    ('adapter', CANDIDATE/'mcu/h723_resources.c', ['-Wconversion','-Wsign-conversion']),
                    ('test', HERE/'test_routes.c', ['-Wconversion','-Wsign-conversion'])):
                    obj=build/(name+'.o');objects.append(str(obj))
                    subprocess.run([*flags,*extra,'-c',str(src),'-o',str(obj)],
                                   stdout=log,stderr=subprocess.STDOUT,timeout=20,check=True)
                subprocess.run(['gcc','-fsanitize=address,undefined','-no-pie',*objects,
                                '-Wl,--wrap=gpio_pwm_get_route','-o',str(build/'test')],
                               stdout=log,stderr=subprocess.STDOUT,timeout=20,check=True)
            result=subprocess.run([str(build/'test')],capture_output=True,text=True,timeout=15,
                env={**os.environ,'ASAN_OPTIONS':'detect_leaks=0:halt_on_error=1',
                                  'UBSAN_OPTIONS':'halt_on_error=1'})
            (RESULTS/(mode+'-run.log')).write_text(result.stdout+result.stderr)
            assert result.returncode == 0, result.stderr
            routes=sorted([[int(v) for v in line.split()] for line in result.stdout.splitlines()])
            assert routes == (expected if mode=='on' else [])
            report['modes'][mode]={'pin_cases':65540,'valid_routes':len(routes),
                                   'null_preserved_and_no_io':True,'unknown_route_probes':5 if mode=='on' else 0}
        report['status']='passed'
finally:
    report['source_sha256'] = {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (CANDIDATE/'mcu/h723_resources.c', CANDIDATE/'mcu/h723_resources.h',
                  HERE/'test_routes.c')}
    (RESULTS/'results.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report))
