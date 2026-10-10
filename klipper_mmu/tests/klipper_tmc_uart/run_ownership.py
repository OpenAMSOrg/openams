#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Host regression runner for TMC UART pin ownership. Coverage only."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import resource
import subprocess

SCENARIOS = [
    'ordinary-sharing',
    'ordinary-local-0', 'ordinary-local-1', 'ordinary-local-2', 'ordinary-local-3',
    'local-local-0', 'local-local-1', 'local-local-2', 'local-local-3',
    'local-ordinary-0', 'local-ordinary-1', 'local-ordinary-2', 'local-ordinary-3',
    'distinct', 'single-wire', 'full-width', 'cancelled', 'active', 'pending',
    'partial-shutdown', 'invalid-unclaimed',
]
EXPECTED = ['0/ordinary-sharing'] + [f'1/{name}' for name in SCENARIOS]
COMMAND_H = (
    '#include <stddef.h>\n'
    '#include <stdint.h>\n'
    '#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))\n'
    '#define DIV_ROUND_CLOSEST(n,d) (((n)+(d)/2)/(d))\n'
    '#define container_of(p,t,m) ((t *)((char *)(p)-offsetof(t,m)))\n'
    '#define DECL_COMMAND(func, args) _Static_assert(1, args)\n'
    '#define sendf review_sendf\n'
    'void review_sendf(const char *, ...);\n'
    'void *command_decode_ptr(uint32_t);\n'
    '_Noreturn void shutdown(const char *);\n'
)
SCHED_H = (
    '#include <stdint.h>\n'
    'struct timer {\n'
    '    struct timer *next;\n'
    '    uint_fast8_t (*func)(struct timer *);\n'
    '    uint32_t waketime;\n'
    '};\n'
    'struct task_wake { uint8_t wake; };\n'
    'enum { SF_DONE=0, SF_RESCHEDULE=1 };\n'
    '#define DECL_TASK(func) _Static_assert(1, "task")\n'
    '#define DECL_SHUTDOWN(func) _Static_assert(1, "shutdown")\n'
    'void sched_add_timer(struct timer *);\n'
    'void sched_del_timer(struct timer *);\n'
    'void sched_wake_task(struct task_wake *);\n'
    'uint8_t sched_check_wake(struct task_wake *);\n'
    'uint8_t sched_is_shutdown(void);\n'
)

def file_sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def compile_and_run(report, upstream, work, ownership_c, shim_include, enabled, names, env):
    work.mkdir()
    (work / 'autoconf.h').write_text(
        f'#define CONFIG_WANT_TMCUART_LOCAL {enabled}\n'
        '#define CONFIG_TMCUART_LOCAL_MAX_CHANNELS 8\n')
    (work / 'command.h').write_text(COMMAND_H)
    (work / 'sched.h').write_text(SCHED_H)
    for name in ('tmcuart.c', 'tmcuart.h', 'basecmd.h'):
        (work / name).write_bytes((upstream / 'src' / name).read_bytes())
    binary = work / 'ownership-test'
    command = [
        'gcc', '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-O1', '-g',
        '-fno-pie', '-no-pie', '-fsanitize=address,undefined',
        '-fno-sanitize-recover=all', '-I', str(work), '-I', str(shim_include),
        str(work / 'tmcuart.c'), str(ownership_c), '-o', str(binary),
    ]
    with (work / 'build.log').open('w') as log:
        built = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=30)
    report['compiles'][str(enabled)] = built.returncode
    if built.returncode != 0:
        raise RuntimeError(f'compile failed for CONFIG_WANT_TMCUART_LOCAL={enabled}')
    for name in names:
        with (work / (name + '.log')).open('w') as log:
            result = subprocess.run(
                [str(binary), name], stdout=log, stderr=subprocess.STDOUT,
                timeout=5, env=env)
        report['cases'][f'{enabled}/{name}'] = {
            'exit_code': result.returncode,
            'passed': result.returncode == 0,
        }

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path,
                        help='package root (default: directory containing tests/)')
    parser.add_argument('--upstream', type=Path, required=True,
                        help='installed extended Klipper checkout')
    parser.add_argument('--results', type=Path, required=True,
                        help='new directory for logs and results.json')
    args = parser.parse_args()
    package = (args.package if args.package is not None
               else Path(__file__).resolve().parents[2]).resolve()
    upstream = args.upstream.resolve()
    results = args.results.resolve()
    results.mkdir(parents=True, exist_ok=False)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    report = {
        'status': 'failed',
        'hardware_access': False,
        'source_sha256': {},
        'expected_sha256': {},
        'compiles': {},
        'cases': {},
        'failed_cases': [],
    }
    env = os.environ.copy()
    env['ASAN_OPTIONS'] = 'detect_leaks=0:halt_on_error=1'
    env['UBSAN_OPTIONS'] = 'halt_on_error=1'
    try:
        manifest = json.loads((package / 'upstream' / 'source-hashes.json').read_text())
        actual = {}
        expected = {}
        for name in ('tmcuart.c', 'tmcuart.h'):
            actual[name] = file_sha256(upstream / 'src' / name)
            expected[name] = manifest['src/' + name]['after']
        report['source_sha256'] = actual
        report['expected_sha256'] = expected
        mismatched = [name for name in actual if actual[name] != expected[name]]
        if mismatched:
            raise RuntimeError('upstream sha256 mismatch: ' + ', '.join(mismatched))
        ownership_c = package / 'tests' / 'klipper_tmc_uart' / 'ownership.c'
        shim_include = package / 'tests' / 'klipper_tmc_uart' / 'shim_include'
        compile_and_run(report, upstream, results / '0', ownership_c, shim_include,
                        0, ['ordinary-sharing'], env)
        compile_and_run(report, upstream, results / '1', ownership_c, shim_include,
                        1, SCENARIOS, env)
        failed = [key for key, value in report['cases'].items() if not value['passed']]
        missing = [key for key in EXPECTED if key not in report['cases']]
        extra = [key for key in report['cases'] if key not in EXPECTED]
        report['failed_cases'] = failed
        if missing or extra or failed or len(report['cases']) != 22:
            raise RuntimeError(
                'ownership cases incomplete or failed: '
                f'missing={missing} extra={extra} failed={failed}')
        report['status'] = 'passed'
    except Exception as error:
        report['status'] = 'failed'
        report['error'] = repr(error)
        report['failed_cases'] = [
            key for key, value in report['cases'].items() if not value['passed']]
    finally:
        (results / 'results.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report, indent=2))
    if report['status'] != 'passed':
        raise SystemExit(1)

if __name__ == '__main__':
    main()
