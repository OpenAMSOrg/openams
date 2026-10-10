"""Run shared Klipper UART binding checks against the package-installed driver."""
# SPDX-License-Identifier: GPL-3.0-or-later
from pathlib import Path
import argparse
import json
import importlib.util
import os
import shutil
import subprocess
import tempfile

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--package',type=Path,default=Path(__file__).resolve().parents[2])
parser.add_argument('results',type=Path)
parser.add_argument('--case',action='append')
parser.add_argument('--clock',type=int,action='append')
args=parser.parse_args()
here=Path(__file__).resolve().parent
repo_package=args.package.resolve()
package=args.package.resolve()
evidence=repo_package/'tests/klipper_tmc_uart'
args.results.mkdir(parents=True,exist_ok=True)
report={'status':'failed','cases':[],'hardware_accessed':False}
try:
    with tempfile.TemporaryDirectory(prefix='km-uart-binding-review-') as temp:
        work=Path(temp)
        # Exercise the product installer and pinned patch, not a replacement
        # driver copy in a review directory. Only actual upstream source bytes
        # are needed by this native GPIO/scheduler model.
        checkout=work/'upstream'
        shutil.copytree(repo_package/'tests/fixtures/shared_tmcuart',checkout)
        (checkout/'Makefile').write_text('# native fixture\n')
        (checkout/'klippy').mkdir()
        spec=importlib.util.spec_from_file_location('installer',package/'tools/install.py')
        installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)
        assert installer.integrate_driver_api(checkout,'install',package=package)=='installed'
        assert installer.integrate_driver_api(checkout,'check',package=package)=='installed'
        upstream=checkout/'src'
        shutil.copytree(evidence/'shim_include',work/'shim')
        with (work/'shim/autoconf.h').open('a') as f:
            f.write('\n#define CONFIG_WANT_TMCUART_LOCAL 1\n#define CONFIG_TMCUART_LOCAL_MAX_CHANNELS 8\n')
        with (work/'shim/command.h').open('a') as f:
            f.write('\n#include <stddef.h>\n#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))\n'
                    '#define DIV_ROUND_CLOSEST(n,d) (((n)+(d)/2)/(d))\n'
                    '#define container_of(p,t,m) ((t *)((char *)(p)-offsetof(t,m)))\n'
                    'void *command_decode_ptr(uint32_t);\n')
        sources=[package/'mcu/tmc_uart_gpio.c',upstream/'tmcuart.c',
                 package/'src/tmc_uart_codec.c',package/'src/tmc2209.c',
                 package/'mcu/clock.c',package/'src/mcu_clock.c',evidence/'shim_tmc.c',evidence/'test_shared_uart.c']
        if (package/'src/tmc_uart_config.c').exists():
            sources.append(package/'src/tmc_uart_config.c')
        command=['gcc','-std=gnu11','-O2','-g','-Wall','-Wextra','-Werror',
                 '-fsanitize=address,undefined','-fno-sanitize-recover=all','-fno-pie','-no-pie']
        command+=['-I'+str(p) for p in [package/'mcu',work/'shim',upstream,package/'include']]
        command += [str(p) for p in sources]
        command += ['-Wl,--wrap=sched_add_timer,--wrap=sched_del_timer,--wrap=timer_read_time,--wrap=tmcuart_local_cancel',
                    '-lm','-o',str(work/'probe')]
        with (args.results/'build.log').open('w') as log:
            subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,timeout=30,check=True)
        scenarios=['pre_ready','setup_irq','ready','wrap','initial_guard','current','invalid_current',
                   'current_irq','restart_irq','crc','silent','starved_pending','starved_diagnostic',
                   'diagnostic','restart','claim','shutdown','current_busy','queries_pure','restart_cancel_failure',
                   'delayed_tasks','multislot','crc_transient','silent_transient',
                   'crc_diagnostic_transient']
        scenarios=args.case or scenarios
        for clock in (args.clock or (2000000,20000000,520000000)):
            for scenario in scenarios:
                for address in (range(4) if scenario=='ready' else (0,)):
                    result=subprocess.run([str(work/'probe'),str(clock),str(address),scenario],
                        capture_output=True,text=True,timeout=5,
                        env={**os.environ,'ASAN_OPTIONS':'detect_leaks=0:halt_on_error=1',
                                          'UBSAN_OPTIONS':'halt_on_error=1'})
                    report['cases'].append({'clock':clock,'address':address,'scenario':scenario,
                                           'returncode':result.returncode,'stdout':result.stdout,'stderr':result.stderr})
        assert all(c['returncode']==0 for c in report['cases'])
        report['status']='passed'
finally:
    (args.results/'results.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'status':report['status'],'passed':sum(c['returncode']==0 for c in report['cases']),
                      'total':len(report['cases'])}))
