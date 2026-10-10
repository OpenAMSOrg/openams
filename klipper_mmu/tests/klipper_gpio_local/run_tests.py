#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Host regression for STM32H723 and STM32F407 local GPIO ownership.

Compiles unchanged Klipper sources with mocked IRQ, scheduler, and GPIO
registers. This does not qualify MMIO hardware and does not start motion.
"""
from pathlib import Path
import argparse
import hashlib
import json
import os
import resource
import subprocess
import sys
import tarfile

INSTALLED = (
    'stm32h7_gpio.c',
    'gpioperiph.c',
    'internal.h',
    'gpio_local.h',
)
F4_INSTALLED = ('gpio.c', 'gpioperiph.c', 'internal.h', 'gpio_local.h')
BASELINE_SOURCES = (
    'stm32h7_gpio.c',
    'gpioperiph.c',
    'internal.h',
)
F4_BASELINE_SOURCES = ('gpio.c', 'gpioperiph.c', 'internal.h')
PREPROCESS_SOURCES = ('stm32h7_gpio.c', 'gpioperiph.c')
COMPILE_TIMEOUT = 30
RUN_TIMEOUT = 10
F4_ARCHIVE_SHA256 = 'da1c6223d90652cda6e826e3cf896cf97d6e11aa13aa4c8feb5232dac068f70b'

HEADERS = {
    'board/irq.h': (
        '#pragma once\n'
        'typedef unsigned irqstatus_t;'
        'irqstatus_t irq_save(void);'
        'void irq_restore(irqstatus_t);\n'
    ),
    'sched.h': (
        '#pragma once\n'
        '#include <stdint.h>\n'
        'uint8_t sched_is_shutdown(void);\n'
        '#define DECL_SHUTDOWN(F) void review_run_shutdown(void) { F(); }\n'
    ),
    'command.h': (
        '#pragma once\n'
        '#include <stddef.h>\n'
        '#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))\n'
        '#define __aligned(x) __attribute__((aligned(x)))\n'
        '#define DECL_ENUMERATION_RANGE(a,b,c,d)\n'
        '_Noreturn void review_shutdown(const char*);\n'
        '#define shutdown(s) review_shutdown(s)\n'
    ),
    'stm32h7xx.h': (
        '#pragma once\n'
        '#include <stdint.h>\n'
        'typedef struct {volatile uint32_t MODER,OTYPER,OSPEEDR,PUPDR,'
        'IDR,ODR,BSRR,AFR[2];} GPIO_TypeDef;\n'
        'extern GPIO_TypeDef review_ports[9];\n'
        '#define GPIOA (&review_ports[0])\n'
        '#define GPIOB (&review_ports[1])\n'
        '#define GPIOC (&review_ports[2])\n'
        '#define GPIOE (&review_ports[4])\n'
        '#define GPIOF (&review_ports[5])\n'
        '#define GPIOG (&review_ports[6])\n'
        '#define GPIOH (&review_ports[7])\n'
        '#define GPIOI (&review_ports[8])\n'
    ),
}

F4_HEADER = HEADERS['stm32h7xx.h'].replace(
    '#define GPIOC (&review_ports[2])',
    '#define GPIOC (&review_ports[2])\n#define GPIOD (&review_ports[3])')

# Signed `0x0f << af_shift` in unchanged gpioperiph.c is UB at shift 28.
# Shift checks are off in every mode. ASan and the other UBSan checks stay.
SANITIZE = [
    '-fsanitize=address,undefined',
    '-fno-sanitize=shift',
    '-fno-sanitize-recover=all',
]


def native_cases():
    cases = [[name] for name in (
        'local_success', 'largest', 'repeat', 'irq_restore',
        'global_shutdown', 'closed',
    )]
    for name in ('ordinary_first', 'local_first', 'unreserved', 'invalid_local'):
        for kind in range(3):
            cases.append([name, str(kind)])
    for kind in range(6):
        cases.append(['batch', str(kind)])
    if len(cases) != 24:
        raise RuntimeError('planned native case list is not 24')
    return cases


def file_sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def blank_report():
    cases = []
    for args in native_cases():
        cases.append({
            'case': args,
            'ran': False,
            'passed': False,
            'exit_code': None,
            'log': None,
        })
    return {
        'status': 'failed',
        'hardware_access': False,
        'automatic_motion': False,
        'shift_sanitizer': 'disabled',
        'shift_sanitizer_reason': (
            'Unchanged upstream gpioperiph.c evaluates signed 0x0f << af_shift, '
            'and af_shift is 28 on the last pin of each AFR nibble. '
            '-fno-sanitize=shift is required for every mode, including baseline. '
            'AddressSanitizer and the rest of UndefinedBehaviorSanitizer stay on. '
            'This is not full sanitizer coverage.'
        ),
        'warnings': (
            'The linked build uses -Wall -Wextra without -Werror, with '
            '-Wno-pointer-to-int-cast and -Wno-unused-function for existing '
            'upstream diagnostics. It is not a strict-warnings build. '
            'gpioperiph.c is compiled again with -Wmissing-prototypes -Werror.'
        ),
        'source_sha256': {'installed': {}, 'baseline': {}},
        'expected_sha256': {'installed_after': {}, 'baseline_before': {}},
        'compiles': {},
        'ordinary_traces_equal': False,
        'disabled_preprocessed_equal': False,
        'prototype_ok': False,
        'cases': cases,
        'unran_cases': [],
        'failed_cases': [],
    }


def seal(report):
    cases = report['cases']
    report['unran_cases'] = [item['case'] for item in cases if not item['ran']]
    report['failed_cases'] = [
        item['case'] for item in cases if item['ran'] and not item['passed']]
    complete = (
        len(cases) == 24
        and not report['unran_cases']
        and not report['failed_cases']
        and report['ordinary_traces_equal'] is True
        and report['disabled_preprocessed_equal'] is True
        and report['prototype_ok'] is True
        and all(report['compiles'].get(name) == 0 for name in (
            'baseline', 'off', 'on', 'gpioperiph_prototypes'))
    )
    if report.get('status') == 'passed' and not complete:
        report['status'] = 'failed'
        report.setdefault('error', 'pass gate rejected')
    elif not complete:
        report['status'] = 'failed'


def run_cmd(command, timeout, env):
    try:
        return subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ''
        stderr = exc.stderr or ''
        if isinstance(stdout, bytes):
            stdout = stdout.decode(errors='replace')
        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors='replace')
        return subprocess.CompletedProcess(
            command, 124, stdout, stderr + '\nTIMEOUT\n')


def write_log(path, result):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(result.stdout + result.stderr)


def copy_file(src, dst):
    if not src.is_file():
        raise RuntimeError('missing source: ' + str(src))
    dst.write_bytes(src.read_bytes())


def manifest_hash(manifest, name, side):
    key = 'src/stm32/' + name
    try:
        value = manifest[key][side]
    except (KeyError, TypeError) as exc:
        raise RuntimeError('manifest missing %s %s' % (key, side)) from exc
    if not isinstance(value, str) or not value:
        raise RuntimeError('manifest missing %s %s' % (key, side))
    return value


def check_hashes(report, package, upstream, results, family, archive):
    manifest_path = package / 'upstream' / 'source-hashes.json'
    if not manifest_path.is_file():
        raise RuntimeError('missing manifest: ' + str(manifest_path))
    manifest = json.loads(manifest_path.read_text())
    fixture = package / 'tests' / 'fixtures' / 'shared_tmcuart' / 'src' / 'stm32'
    if family == 'f4':
        fixture = results / 'baseline-source'
        fixture.mkdir()
        commit = 'ce7002bedf37e938bb483572949f3703ac6476cb'
        if archive is None or not archive.is_file():
            raise RuntimeError('F4 baseline requires --archive with pinned Klipper source')
        if file_sha256(archive) != F4_ARCHIVE_SHA256:
            raise RuntimeError('F4 baseline archive sha256 mismatch')
        with tarfile.open(archive, 'r:gz') as source:
            for name in F4_BASELINE_SOURCES:
                entry = source.getmember('klipper-' + commit + '/src/stm32/' + name)
                (fixture / name).write_bytes(source.extractfile(entry).read())
    installed = upstream / 'src' / 'stm32'
    gpio_h = package / 'tests' / 'fixtures' / 'shared_tmcuart' / 'src' / 'stm32' / 'gpio.h'
    if not gpio_h.is_file():
        raise RuntimeError('missing pristine gpio.h: ' + str(gpio_h))
    mismatched = []
    for name in (F4_INSTALLED if family == 'f4' else INSTALLED):
        expected = manifest_hash(manifest, name, 'after')
        actual = file_sha256(installed / name) if (installed / name).is_file() else None
        report['expected_sha256']['installed_after'][name] = expected
        report['source_sha256']['installed'][name] = actual
        if actual != expected:
            mismatched.append('installed ' + name)
    for name in (F4_BASELINE_SOURCES if family == 'f4' else BASELINE_SOURCES):
        expected = manifest_hash(manifest, name, 'before')
        actual = file_sha256(fixture / name) if (fixture / name).is_file() else None
        report['expected_sha256']['baseline_before'][name] = expected
        report['source_sha256']['baseline'][name] = actual
        if actual != expected:
            mismatched.append('baseline ' + name)
    if mismatched:
        raise RuntimeError('sha256 mismatch: ' + ', '.join(mismatched))
    return fixture, installed, gpio_h


def prepare_work(work, origin, gpio_h, local_header, enabled, family):
    work.mkdir()
    for name in (F4_BASELINE_SOURCES if family == 'f4' else BASELINE_SOURCES):
        copy_file(origin / name, work / name)
    copy_file(gpio_h, work / 'gpio.h')
    copy_file(local_header, work / 'gpio_local.h')
    headers = dict(HEADERS)
    if family == 'f4':
        del headers['stm32h7xx.h']
        headers['stm32f4xx.h'] = F4_HEADER
    for rel, data in headers.items():
        dest = work / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(data)
    (work / 'autoconf.h').write_text(
        ('#define CONFIG_MACH_STM32F4 1\n#define CONFIG_MACH_STM32F407 1\n'
         if family == 'f4' else
         '#define CONFIG_MACH_STM32H7 1\n#define CONFIG_MACH_STM32H723 1\n')
        +
        '#define CONFIG_MACH_STM32F0 0\n'
        '#define CONFIG_WANT_GPIO_LOCAL %d\n' % enabled)


def compile_linked(work, fixture_c, log_path, env, family):
    binary = work / 'fixture'
    command = [
        'gcc', '-std=gnu11', '-Wall', '-Wextra',
        '-Wno-unused-function', '-Wno-pointer-to-int-cast',
        *SANITIZE, '-fno-pie', '-no-pie', '-g',
        '-I', str(work),
        str(work / ('gpio.c' if family == 'f4' else 'stm32h7_gpio.c')),
        str(work / 'gpioperiph.c'),
        str(fixture_c),
        '-o', str(binary),
    ]
    result = run_cmd(command, COMPILE_TIMEOUT, env)
    write_log(log_path, result)
    return result, binary


def preprocess(work, name, env):
    source = work / name
    result = run_cmd(
        ['gcc', '-E', '-P', '-I', str(work), str(source)],
        RUN_TIMEOUT, env)
    (work / (name + '.pp')).write_text(result.stdout)
    (work / (name + '.pp.log')).write_text(result.stderr)
    if result.returncode != 0:
        raise RuntimeError('preprocess failed for ' + str(source))
    return result.stdout


def compact(text):
    return ''.join(text.split())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path,
                        help='package root (default: directory containing tests/)')
    parser.add_argument('--upstream', type=Path, required=True,
                        help='installed extended Klipper checkout')
    parser.add_argument('--results', type=Path, required=True,
                        help='new directory for summary.json and logs')
    parser.add_argument('--family', choices=('h7', 'f4'), default='h7')
    parser.add_argument('--archive', type=Path,
                        help='pinned upstream Klipper archive for F407 baseline')
    args = parser.parse_args()
    package = (args.package if args.package is not None
               else Path(__file__).resolve().parents[2]).resolve()
    upstream = args.upstream.resolve()
    results = args.results.resolve()
    try:
        results.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        sys.stderr.write('results directory already exists: %s\n' % results)
        raise SystemExit(1)
    report = blank_report()
    report['family'] = args.family
    env = os.environ.copy()
    env['ASAN_OPTIONS'] = 'detect_leaks=0:halt_on_error=1'
    env['UBSAN_OPTIONS'] = 'halt_on_error=1'
    problems = []
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if not upstream.is_dir():
            raise RuntimeError('upstream is not a directory: ' + str(upstream))
        fixture_c = Path(__file__).resolve().parent / 'fixture.c'
        if not fixture_c.is_file():
            raise RuntimeError('missing fixture.c: ' + str(fixture_c))
        fixture_stm32, installed_stm32, gpio_h = check_hashes(
            report, package, upstream, results, args.family,
            args.archive.resolve() if args.archive is not None else None)
        local_header = installed_stm32 / 'gpio_local.h'
        modes = (
            ('baseline', 0, fixture_stm32),
            ('off', 0, installed_stm32),
            ('on', 1, installed_stm32),
        )
        traces = {}
        preprocessed = {}
        binaries = {}
        for mode, enabled, origin in modes:
            work = results / mode
            prepare_work(work, origin, gpio_h, local_header, enabled, args.family)
            compiled, binary = compile_linked(
                work, fixture_c, work / 'build.log', env, args.family)
            report['compiles'][mode] = compiled.returncode
            if compiled.returncode != 0:
                raise RuntimeError('compile failed for ' + mode)
            binaries[mode] = binary
            traced = run_cmd([str(binary), 'trace'], RUN_TIMEOUT, env)
            write_log(work / 'trace.log', traced)
            traces[mode] = traced
            if mode != 'on':
                preprocessed[mode] = {
                    name: preprocess(work, name, env)
                    for name in (('gpio.c', 'gpioperiph.c') if args.family == 'f4'
                                 else PREPROCESS_SOURCES)}
        baseline_out = traces['baseline'].stdout
        report['ordinary_traces_equal'] = (
            all(traces[mode].returncode == 0 for mode in ('baseline', 'off', 'on'))
            and baseline_out == traces['off'].stdout == traces['on'].stdout)
        if not report['ordinary_traces_equal']:
            problems.append('ordinary traces differ or failed')
        report['disabled_preprocessed_equal'] = all(
            compact(preprocessed['baseline'][name]) == compact(preprocessed['off'][name])
            for name in (('gpio.c', 'gpioperiph.c') if args.family == 'f4'
                         else PREPROCESS_SOURCES))
        if not report['disabled_preprocessed_equal']:
            problems.append('disabled preprocessed sources differ')
        on_work = results / 'on'
        prototype = run_cmd([
            'gcc', '-std=gnu11', '-Wall', '-Wextra',
            '-Wmissing-prototypes', '-Werror',
            *SANITIZE, '-c', str(on_work / 'gpioperiph.c'),
            '-I', str(on_work), '-o', str(on_work / 'gpioperiph-prototypes.o'),
        ], COMPILE_TIMEOUT, env)
        write_log(on_work / 'gpioperiph-prototypes.log', prototype)
        report['compiles']['gpioperiph_prototypes'] = prototype.returncode
        report['prototype_ok'] = prototype.returncode == 0
        if not report['prototype_ok']:
            problems.append('gpioperiph prototype check failed')
        case_dir = on_work / 'cases'
        for record in report['cases']:
            log_path = case_dir / ('-'.join(record['case']) + '.log')
            record['log'] = log_path.relative_to(results).as_posix()
            ran = run_cmd(
                [str(binaries['on']), *record['case']], RUN_TIMEOUT, env)
            write_log(log_path, ran)
            record['ran'] = True
            record['exit_code'] = ran.returncode
            record['passed'] = ran.returncode == 0
            if not record['passed']:
                problems.append('case failed: ' + ' '.join(record['case']))
        missing = [item['case'] for item in report['cases'] if not item['ran']]
        if missing or len(report['cases']) != 24:
            problems.append('missing native cases')
        if problems:
            raise RuntimeError('; '.join(problems))
        report['status'] = 'passed'
    except Exception as error:
        report['status'] = 'failed'
        report['error'] = repr(error)
    finally:
        seal(report)
        (results / 'summary.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report, indent=2))
    if report['status'] != 'passed':
        raise SystemExit(1)


if __name__ == '__main__':
    main()
