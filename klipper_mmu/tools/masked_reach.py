#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Static reachability from interrupt (masked) roots in a firmware image.

Counts what can run from a root such as km_controller_timer_event: functions,
instructions, soft-float/libm and VFP sites, 64-bit divide calls, hardware
divides, indirect calls, and the longest call chain in instructions. This is
the evidence behind docs/controller-task-boundary-audit.md (R1): the edge
interrupt must reach no floating point and no planner code.

    tools/masked_reach.py path/to/klipper.elf km_controller_timer_event
    tools/masked_reach.py --list path/to/klipper.elf km_controller_timer_event

Roots also match link-time-optimization clones (km_controller_task matches
km_controller_task.lto_priv.0). Indirect calls are counted, not followed, so
the reachable set is a lower bound where function pointers are used. Needs
arm-none-eabi-objdump (or --objdump). Analysis rules and tests were drafted
by a local model and tightened in review.
"""
import argparse
import re
import subprocess

FLOAT_RE = re.compile(r'^(__aeabi_(d|f)[a-z0-9]*|__aeabi_[ilu]+2[df]|__(add|sub|mul|div|neg|extend|trunc|fix|fixuns|float|floatun|eq|ne|lt|le|gt|ge|un|cmp)[a-z]*[sdt]f[0-9]?|sqrt|sqrtf|llround|lround|round|floor|ceil|fabs|pow|exp|log|fmod|lrint|rint|trunc)$')
DIV64_RE = re.compile(r'^(__aeabi_u?ldivmod|__u?divdi3|__u?moddi3)$')
BRANCH_RE = re.compile(r'^(bl|blx|b|b\.w|b\.n|cbz|cbnz|b(eq|ne|cs|hs|cc|lo|mi|pl|vs|vc|hi|ls|ge|lt|gt|le|al)(\.w|\.n)?)$')


def parse(disasm):
    """{function: {'n', 'edges', 'float', 'div64', 'hw_div', 'indirect'}} from objdump -d text."""
    funcs, cur = {}, None
    for line in disasm.splitlines():
        m = re.match(r'^[0-9a-f]+ <([^>]+)>:$', line)
        if m:
            cur = funcs.setdefault(m.group(1), dict(n=0, edges=[], float=0, div64=0, hw_div=0, indirect=0))
            cur['name'] = m.group(1)
            continue
        m = re.match(r'^\s+[0-9a-f]+:\s+(\S+)\s*(.*)$', line)
        if not m or cur is None or m.group(1).startswith('.'):
            continue
        mnemonic, operands = m.group(1), m.group(2).split('@')[0].strip()
        cur['n'] += 1
        cur['float'] += mnemonic.startswith('v')
        cur['hw_div'] += mnemonic in ('sdiv', 'udiv')
        target = re.search(r'<([^>+]+)(\+0x[0-9a-f]+)?>', operands)
        if BRANCH_RE.match(mnemonic) and target and target.group(1) != cur['name']:
            cur['edges'].append(target.group(1))
            cur['float'] += bool(FLOAT_RE.match(target.group(1)))
            cur['div64'] += bool(DIV64_RE.match(target.group(1)))
        elif (mnemonic == 'blx' and not target) or (mnemonic == 'bx' and operands != 'lr'):
            cur['indirect'] += 1
    return funcs


def analyze(disasm, roots):
    """Metrics for everything reachable from roots (see the module docstring)."""
    funcs = parse(disasm)
    starts = []
    for root in roots:
        hit = [f for f in funcs if f == root or f.startswith(root + '.')]
        if not hit:
            raise ValueError('unknown root: %s' % root)
        starts += hit
    seen, todo = set(), list(starts)
    while todo:
        f = todo.pop()
        if f in funcs and f not in seen:
            seen.add(f)
            todo += funcs[f]['edges']
    memo = {}

    def chain(f, stack):  # longest instruction sum along a call path; recursion ignored
        if f not in memo:
            stack.add(f)
            below = [chain(e, stack) for e in funcs[f]['edges'] if e in funcs and e not in stack]
            stack.discard(f)
            memo[f] = funcs[f]['n'] + max(below, default=0)
        return memo[f]
    total = {k: sum(funcs[f][k] for f in seen) for k in ('n', 'float', 'div64', 'hw_div', 'indirect')}
    return {'functions': len(seen), 'instructions': total['n'], 'float_sites': total['float'],
            'div64_sites': total['div64'], 'hw_div': total['hw_div'],
            'indirect_sites': total['indirect'],
            'longest_chain': max(chain(f, set()) for f in starts), 'reachable': sorted(seen)}


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('elf')
    parser.add_argument('roots', nargs='+')
    parser.add_argument('--objdump', default='arm-none-eabi-objdump')
    parser.add_argument('--list', action='store_true', help='also list the reachable functions')
    args = parser.parse_args()
    disasm = subprocess.run([args.objdump, '-d', '--no-show-raw-insn', args.elf],
                            capture_output=True, text=True, check=True, timeout=300).stdout
    result = analyze(disasm, args.roots)
    for key, value in result.items():
        if key != 'reachable':
            print('%s: %s' % (key, value))
    if args.list:
        print('\n'.join(result['reachable']))


if __name__ == '__main__':
    main()
