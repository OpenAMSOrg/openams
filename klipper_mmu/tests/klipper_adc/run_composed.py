# SPDX-License-Identifier: GPL-3.0-or-later
"""MCU-reader/shared-driver composition regressions; initialized register model."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import shutil
import subprocess

HERE = Path(__file__).resolve().parent
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--package', type=Path, default=HERE.parents[1])
p.add_argument('--upstream', type=Path, required=True)
p.add_argument('--results', type=Path, required=True)
args = p.parse_args()
package, upstream, results = (x.resolve() for x in (args.package, args.upstream, args.results))
results.mkdir(parents=True, exist_ok=False)
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
report = {'status': 'failed', 'cases': {}, 'hardware_access': False,
          'coverage': 'software/register-model',
          'limits': 'Software/register-model coverage. Real ADC driver/reader/pair core; stubbed clock and initialized register model; explicit completion and DR read-clear; no analog calibration.'}
REQUIRED = (
    '0/off',
    '1/control', '1/sample', '1/ordinary-owned', '1/local-owned', '1/restart',
    '1/rebind', '1/adc2', '1/adc3', '1/invalid', '1/shutdown-first',
    '1/shutdown-second', '1/clock-loss', '1/active-stop', '1/active-shutdown',
)

def run(command, log):
    with log.open('w') as out:
        return subprocess.run(command, stdout=out, stderr=subprocess.STDOUT, timeout=30,
            env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0:halt_on_error=1',
                 'UBSAN_OPTIONS': 'halt_on_error=1'}).returncode

try:
    adc = upstream / 'src/stm32/stm32h7_adc.c'
    manifest = json.loads((package / 'upstream' / 'source-hashes.json').read_text())
    expected_driver = manifest['src/stm32/stm32h7_adc.c']['after']
    driver_bytes = adc.read_bytes()
    driver_hash = hashlib.sha256(driver_bytes).hexdigest()
    assert driver_hash == expected_driver
    local_bytes = (upstream / 'src/stm32/adc_local.h').read_bytes()
    gpio_bytes = (upstream / 'src/stm32/gpio.h').read_bytes()
    reader_bytes = (package / 'mcu/adc.c').read_bytes()
    pair_bytes = (package / 'src/adc_pair.c').read_bytes()
    harness_bytes = (HERE / 'composed_reader.c').read_bytes()
    report['driver_sha256'] = driver_hash
    report['reader_sha256'] = hashlib.sha256(reader_bytes).hexdigest()
    report['pair_sha256'] = hashlib.sha256(pair_bytes).hexdigest()
    report['adc_local_sha256'] = hashlib.sha256(local_bytes).hexdigest()
    report['composed_reader_sha256'] = hashlib.sha256(harness_bytes).hexdigest()
    vendor = (upstream/'lib/stm32h7/include/stm32h723xx.h').read_text()
    structs = '\n'.join(re.search(r'typedef struct\s*\{[^}]+\}\s*'+name+r';', vendor).group()
                         for name in ('ADC_TypeDef', 'ADC_Common_TypeDef'))
    defines = '\n'.join(line for line in vendor.splitlines() if line.startswith('#define ADC_'))
    for local in (0, 1):
        work = results/('local-'+str(local)); (work/'board').mkdir(parents=True); (work/'stm32').mkdir()
        (work/'autoconf.h').write_text('#define CONFIG_MACH_STM32H7 1\n#define CONFIG_MACH_STM32H723 1\n'
            '#define CONFIG_WANT_ADC 1\n#define CONFIG_CLOCK_FREQ 520000000\n'
            f'#define CONFIG_WANT_ADC_LOCAL {local}\n')
        (work/'internal.h').write_text('''#ifndef REVIEW_INTERNAL_H
#define REVIEW_INTERNAL_H
#include <stdint.h>
#include "autoconf.h"
#define __IO volatile
'''+structs+'\n'+defines+'''
extern ADC_TypeDef test_adcs[3];
extern ADC_Common_TypeDef test_common[2];
#define ADC1 (&test_adcs[0])
#define ADC2 (&test_adcs[1])
#define ADC3 (&test_adcs[2])
#define ADC12_COMMON (&test_common[0])
#define ADC3_COMMON (&test_common[1])
#define GPIO(port,bit) (((port)-'A')*16+(bit))
#define GPIO_ANALOG 3
#define MODIFY_REG(reg,clear,set) ((reg)=((reg)&~(clear))|(set))
void gpio_peripheral(uint32_t,uint32_t,int32_t);
int is_enabled_pclock(uint32_t);
void enable_pclock(uint32_t);
#endif
''')
        (work/'board/irq.h').write_text('''#ifndef REVIEW_IRQ_H
#define REVIEW_IRQ_H
typedef unsigned irqstatus_t;
irqstatus_t irq_save(void);
void irq_restore(irqstatus_t);
#endif
''')
        (work/'board/misc.h').write_text('''#include <stdint.h>
uint32_t timer_read_time(void);
uint32_t timer_from_us(uint32_t);
static inline int timer_is_before(uint32_t a,uint32_t b) { return (int32_t)(a-b)<0; }
''')
        (work/'command.h').write_text('''#include <stddef.h>
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#define DECL_ENUMERATION(...)
#define DECL_CONSTANT(...)
_Noreturn void shutdown(const char *);
''')
        (work/'sched.h').write_text('''#ifndef REVIEW_SCHED_H
#define REVIEW_SCHED_H
#include <stdint.h>
struct timer { struct timer *next; uint_fast8_t (*func)(struct timer *); uint32_t waketime; };
enum { SF_DONE=0, SF_RESCHEDULE=1 };
uint8_t sched_is_shutdown(void);
void sched_add_timer(struct timer *);
void sched_del_timer(struct timer *);
void register_shutdown(void (*fn)(void));
#define DECL_SHUTDOWN(fn) static void __attribute__((constructor)) register_##fn(void) { register_shutdown(fn); } _Static_assert(1,"register")
#endif
''')
        shutil.copy2(upstream/'src/stm32/gpio.h', work/'gpio.h')
        assert (work/'gpio.h').read_bytes() == gpio_bytes
        (work/'board/gpio.h').write_text('#include "../gpio.h"\n')
        shutil.copy2(adc, work/'stm32h7_adc.c')
        assert (work/'stm32h7_adc.c').read_bytes() == driver_bytes
        # The shared source includes its local header even with the API off.
        # The consumer's stm32/ include path remains absent in the off test.
        shutil.copy2(upstream/'src/stm32/adc_local.h', work/'adc_local.h')
        assert (work/'adc_local.h').read_bytes() == local_bytes
        if local:
            # Preserve the actual header's same-directory gpio.h include.
            shutil.copy2(upstream/'src/stm32/adc_local.h', work/'stm32/adc_local.h')
            shutil.copy2(upstream/'src/stm32/gpio.h', work/'stm32/gpio.h')
            assert (work/'stm32/adc_local.h').read_bytes() == local_bytes
            assert (work/'stm32/gpio.h').read_bytes() == gpio_bytes
        else:
            assert not (work/'stm32'/'adc_local.h').exists()
        flags = ['gcc', '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-O1', '-g',
                 '-fsanitize=address,undefined', '-fno-sanitize-recover=all', '-fno-pie',
                 '-I'+str(work), '-I'+str(package/'include'), '-I'+str(package/'mcu')]
        units = [(work/'stm32h7_adc.c', ['-Wno-pointer-to-int-cast','-Wno-sign-compare']),
                 (package/'mcu/adc.c', ['-pedantic','-Wconversion','-Wsign-conversion']),
                 (package/'src/adc_pair.c', ['-pedantic','-Wconversion','-Wsign-conversion']),
                 (HERE/'composed_reader.c', [])]
        objects = []
        for i, (source, extra) in enumerate(units):
            obj=work/f'unit{i}.o'; objects.append(str(obj))
            assert run([*flags,*extra,'-c',str(source),'-o',str(obj)],work/f'compile{i}.log')==0, f'compile {local}/{i}'
        binary=work/'test'
        assert run([*flags,'-no-pie',*objects,'-o',str(binary)],work/'link.log')==0, f'link {local}'
        cases = ['off'] if not local else ['control','sample','ordinary-owned','local-owned','restart',
            'rebind','adc2','adc3','invalid','shutdown-first','shutdown-second','clock-loss','active-stop','active-shutdown']
        for case in cases:
            code=run([str(binary),case],work/(case+'.log'))
            report['cases'][f'{local}/{case}']={'exit_code':code,'passed':code==0}
    bad=[name for name, result in report['cases'].items() if not result['passed']]
    report['failed_cases']=bad
    assert list(report['cases']) == list(REQUIRED), sorted(report['cases'])
    assert not bad, bad
    report['status']='passed'
except Exception as error:
    report['error']=repr(error)
finally:
    (results/'results.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report,indent=2))
if report['status']=='failed': raise SystemExit(1)
