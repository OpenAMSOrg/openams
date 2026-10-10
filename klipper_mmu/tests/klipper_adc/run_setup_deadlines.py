# SPDX-License-Identifier: GPL-3.0-or-later
"""STM32H723 shared ADC setup-deadline regressions.

python3 tests/klipper_adc/run_setup_deadlines.py --upstream /path/to/installed/klipper --results /tmp/klipper-adc-deadline-results
"""
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
CLOCKS = (1000000, 12000000, 520000000)
NONINTEGRAL_HZ = 12000001
CASES = (
    'control', 'legacy', 'ordinary', 'zero', 'overflow', 'half-bound',
    'irq-disabled', 'ordinary-owned', 'expired-replay', 'shutdown-exact',
    'mux-deadline', 'cold', 'wrap', 'regulator-timeout', 'calibration-timeout',
    'ready-timeout', 'shutdown-calibration', 'max-valid',
)
BUILD_TIMEOUT_S = 20
CASE_TIMEOUT_S = 3
LIMITS = (
    'Synthetic initialized and timer-observation-driven CMSIS register model. '
    'Calibration and ready transitions advance only inside timer_read_time. '
    'W1C ready-clear is modeled on the next observation because volatile writes are not trapped. '
    'No enable-bit write-failure coverage and no analog or electrical proof. '
    'CONFIG_WANT_GPIO_LOCAL is off; shared-GPIO composition is not covered. '
    'Initialized legacy and ordinary traces are labeled and are not cold-wait tests. '
    'The deadline does not cover a stalled hardware clock. '
    'No printer, host, or startup configuration is changed.'
)

def text_of(value):
    if value is None:
        return ''
    if isinstance(value, bytes):
        return value.decode('utf-8', 'replace')
    return value

def run_cmd(command, timeout_s):
    try:
        finished = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout_s,
            env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0:halt_on_error=1',
                 'UBSAN_OPTIONS': 'halt_on_error=1'})
        return {'command': command, 'returncode': finished.returncode, 'timed_out': False,
                'timeout_s': timeout_s, 'stderr': finished.stderr, 'stdout': finished.stdout}
    except subprocess.TimeoutExpired as error:
        return {'command': command, 'returncode': None, 'timed_out': True, 'timeout_s': timeout_s,
                'stderr': text_of(error.stderr), 'stdout': text_of(error.stdout)}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--upstream', type=Path, required=True)
    parser.add_argument('--results', type=Path, required=True)
    args = parser.parse_args()
    package, upstream, results = (item.resolve() for item in (args.package, args.upstream, args.results))
    plans = [(freq, CASES) for freq in CLOCKS]
    plans.append((NONINTEGRAL_HZ, ('nonintegral',)))
    required = [{'frequency_hz': freq, 'name': name} for freq, names in plans for name in names]
    if results.exists():
        print(json.dumps({'status': 'failed', 'error': 'results path already exists',
                          'results': str(results), 'required_executions': len(required)}))
        raise SystemExit(1)
    results.mkdir(parents=True, exist_ok=False)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    report = {
        'status': 'failed', 'hardware_access': False, 'coverage': 'software/register-model',
        'limits': LIMITS, 'build_timeout_s': BUILD_TIMEOUT_S, 'case_timeout_s': CASE_TIMEOUT_S,
        'integral_frequencies_hz': list(CLOCKS), 'integral_case_count': len(CASES),
        'preserved_case_count': len(CASES) - 1,
        'cases_at_each_integral_frequency': list(CASES),
        'nonintegral_frequency_hz': NONINTEGRAL_HZ, 'nonintegral_cases': ['nonintegral'],
        'required_executions': len(required), 'executed': 0, 'passed': 0,
        'failed': [], 'not_run': [], 'compile_failures': [], 'compiles': [], 'cases': [],
        'asan_options': 'detect_leaks=0:halt_on_error=1',
        'ubsan_options': 'halt_on_error=1',
    }
    try:
        manifest = json.loads((package / 'upstream' / 'source-hashes.json').read_text())
        driver_path = upstream / 'src/stm32/stm32h7_adc.c'
        header_path = upstream / 'src/stm32/adc_local.h'
        gpio_path = upstream / 'src/stm32/gpio.h'
        cmsis_path = upstream / 'lib/stm32h7/include/stm32h723xx.h'
        fixture_path = HERE / 'check_setup_deadlines.c'
        driver_bytes = driver_path.read_bytes()
        header_bytes = header_path.read_bytes()
        gpio_bytes = gpio_path.read_bytes()
        fixture_bytes = fixture_path.read_bytes()
        driver_hash = hashlib.sha256(driver_bytes).hexdigest()
        header_hash = hashlib.sha256(header_bytes).hexdigest()
        report['source_sha256'] = driver_hash
        report['header_sha256'] = header_hash
        report['fixture_sha256'] = hashlib.sha256(fixture_bytes).hexdigest()
        report['gpio_sha256'] = hashlib.sha256(gpio_bytes).hexdigest()
        report['cmsis_sha256'] = hashlib.sha256(cmsis_path.read_bytes()).hexdigest()
        report['expected_source_sha256'] = manifest['src/stm32/stm32h7_adc.c']['after']
        report['expected_header_sha256'] = manifest['src/stm32/adc_local.h']['after']
        if (driver_hash != report['expected_source_sha256']
                or header_hash != report['expected_header_sha256']):
            raise RuntimeError('upstream source-hashes.json after mismatch before compilation')
        vendor = cmsis_path.read_text()
        structs = '\n'.join(
            re.search(r'typedef struct\s*\{[^}]+\}\s*' + name + r';', vendor).group()
            for name in ('ADC_TypeDef', 'ADC_Common_TypeDef'))
        defines = '\n'.join(line for line in vendor.splitlines() if line.startswith('#define ADC_'))
        for freq, names in plans:
            work = results / str(freq)
            (work / 'board').mkdir(parents=True)
            (work / 'stm32').mkdir()
            (work / 'autoconf.h').write_text(
                '#define CONFIG_MACH_STM32H7 1\n#define CONFIG_MACH_STM32H723 1\n'
                '#define CONFIG_WANT_ADC 1\n#define CONFIG_CLOCK_FREQ %d\n'
                '#define CONFIG_WANT_ADC_LOCAL 1\n' % freq)
            (work / 'internal.h').write_text(
                '#ifndef REVIEW_INTERNAL_H\n#define REVIEW_INTERNAL_H\n#include <stdint.h>\n'
                '#include "autoconf.h"\n#define __IO volatile\n' + structs + '\n' + defines + '''
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
            (work / 'board' / 'irq.h').write_text(
                '#ifndef REVIEW_IRQ_H\n#define REVIEW_IRQ_H\ntypedef unsigned irqstatus_t;\n'
                'irqstatus_t irq_save(void);\nvoid irq_restore(irqstatus_t);\n#endif\n')
            (work / 'board' / 'misc.h').write_text(
                '#include <stdint.h>\nuint32_t timer_read_time(void);\nuint32_t timer_from_us(uint32_t);\n'
                'static inline int timer_is_before(uint32_t a,uint32_t b) { return (int32_t)(a-b)<0; }\n')
            (work / 'command.h').write_text(
                '#include <stddef.h>\n#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))\n'
                '#define DECL_ENUMERATION(...)\n#define DECL_CONSTANT(...)\n'
                '_Noreturn void shutdown(const char *);\n')
            (work / 'sched.h').write_text(
                '#ifndef REVIEW_SCHED_H\n#define REVIEW_SCHED_H\n#include <stdint.h>\n'
                'struct timer { struct timer *next; uint_fast8_t (*func)(struct timer *); uint32_t waketime; };\n'
                'enum { SF_DONE=0, SF_RESCHEDULE=1 };\nuint8_t sched_is_shutdown(void);\n'
                'void sched_add_timer(struct timer *);\nvoid sched_del_timer(struct timer *);\n'
                'void register_shutdown(void (*fn)(void));\n'
                '#define DECL_SHUTDOWN(fn) static void __attribute__((constructor)) register_##fn(void) { register_shutdown(fn); } _Static_assert(1,"register")\n'
                '#endif\n')
            shutil.copy2(gpio_path, work / 'gpio.h')
            shutil.copy2(header_path, work / 'adc_local.h')
            shutil.copy2(driver_path, work / 'stm32h7_adc.c')
            shutil.copy2(header_path, work / 'stm32' / 'adc_local.h')
            shutil.copy2(gpio_path, work / 'stm32' / 'gpio.h')
            (work / 'board' / 'gpio.h').write_text('#include "../gpio.h"\n')
            assert (work / 'gpio.h').read_bytes() == gpio_bytes
            assert (work / 'adc_local.h').read_bytes() == header_bytes
            assert (work / 'stm32h7_adc.c').read_bytes() == driver_bytes
            assert (work / 'stm32' / 'adc_local.h').read_bytes() == header_bytes
            assert (work / 'stm32' / 'gpio.h').read_bytes() == gpio_bytes
            command = ['gcc', '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-O1', '-g',
                       '-Wno-pointer-to-int-cast', '-Wno-sign-compare',
                       '-fsanitize=address,undefined', '-fno-sanitize-recover=all', '-fno-pie', '-no-pie',
                       '-I' + str(work), str(work / 'stm32h7_adc.c'), str(fixture_path),
                       '-o', str(work / 'check')]
            built = run_cmd(command, BUILD_TIMEOUT_S)
            built['frequency_hz'] = freq
            report['compiles'].append(built)
            if built['timed_out'] or built['returncode'] != 0:
                reason = 'compile timed out' if built['timed_out'] else 'compile failed'
                for name in names:
                    report['not_run'].append({'frequency_hz': freq, 'name': name, 'reason': reason})
                continue
            for name in names:
                ran = run_cmd([str(work / 'check'), name], CASE_TIMEOUT_S)
                ran['frequency_hz'] = freq
                ran['name'] = name
                report['cases'].append(ran)
        assert hashlib.sha256(driver_path.read_bytes()).hexdigest() == driver_hash
        assert hashlib.sha256(header_path.read_bytes()).hexdigest() == header_hash
        report['executed'] = len(report['cases'])
        report['passed'] = sum(item['returncode'] == 0 and not item['timed_out'] for item in report['cases'])
        report['failed'] = [{'frequency_hz': item['frequency_hz'], 'name': item['name'],
                             'returncode': item['returncode'], 'timed_out': item['timed_out']}
                            for item in report['cases'] if item['timed_out'] or item['returncode'] != 0]
        report['compile_failures'] = [{'frequency_hz': item['frequency_hz'], 'returncode': item['returncode'],
                                       'timed_out': item['timed_out']} for item in report['compiles']
                                      if item['timed_out'] or item['returncode'] != 0]
        if (report['passed'] == len(required) and report['executed'] == len(required)
                and not report['failed'] and not report['not_run'] and not report['compile_failures']):
            report['status'] = 'passed'
    except Exception as error:
        report['error'] = repr(error)
        report['status'] = 'failed'
    finally:
        if not report['cases'] and not report['compiles']:
            report['not_run'] = [dict(item, reason='not compiled') for item in required]
        report['executed'] = len(report['cases'])
        (results / 'results.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps({'status': report['status'], 'passed': report['passed'],
                          'executed': report['executed'], 'required_executions': report['required_executions'],
                          'integral_frequencies_hz': report['integral_frequencies_hz'],
                          'nonintegral_frequency_hz': report['nonintegral_frequency_hz'],
                          'failed': report['failed'], 'not_run': report['not_run'],
                          'compile_failures': report['compile_failures'], 'error': report.get('error')},
                         indent=2))
    if report['status'] != 'passed':
        raise SystemExit(1)

if __name__ == '__main__':
    main()
