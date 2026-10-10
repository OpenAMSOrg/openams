#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Draft or check the G-code command reference with a local model.

`draft` asks a local OpenAI-compatible server (llama-server with a local model)
for complete documents grounded only in facts this script extracts from the
code, generates several in parallel and keeps the one with the fewest failures
of checks derived from the same code. `check` runs those checks on any
existing document. See docs/local-doc.md; a person still reviews meaning.
"""
import argparse
import json
import os
import re
import shlex
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = (REPO / 'host/__init__.py').read_text()
LINES = SRC.splitlines()
WIRE = (REPO / 'host/wire.py').read_text()
UNIT = (REPO / 'host/openams_managed_unit.py').read_text()
DOCS = ('host/connection.md', 'docs/path-reconciliation.md', 'docs/diagnostics.md',
        'docs/fps-arrival.md', 'docs/boxturtle-quick-start.md')
URL = os.environ.get('LOCAL_DOC_URL', 'http://127.0.0.1:18080/v1/chat/completions')
# The brief forbids these internal terms; checked in every draft.
FORBIDDEN = ('retired', 'reservation', 'guarded', 'waiter', 'exact terminal', 'exact retained')
# What those terms mean here (host/connection.md), so a rewrite keeps the meaning.
GLOSSARY = {
    'retired': 'left over from before a disconnect or restart',
    'waiter': 'a request still waiting for its result',
    'terminal': 'the final result of an operation',
    'reservation': 'filament held ready in a bay',
    'guarded': 'protected by safety checks',
    'exact': 'the specific one',
    'ACK': 'acknowledgement',
}
AUDIENCE = ('Everyday if a BoxTurtle owner uses it during normal printing, loading or unloading '
            'spools, or recovering from a fault; Advanced for setup, calibration, commissioning, '
            'debugging internals, or metadata')
# Parameters whose numeric values are wire.py enumerations.
ENUM_PARAMS = {'CLIENT': 'Client', 'OPERATION': 'OpType'}


def commands():
    return re.findall(r"register_mux_command\(\s*'KLIPPER_MMU_([A-Z_]+)'", SRC)


def method(source, name):
    m = re.search(r"\n    def %s\(.*?(?=\n    def |\n\S|\Z)" % re.escape(name), source, re.S)
    return m.group(0) if m else ''


def handler(cmd):
    """The handler and the private helpers it calls, one level deep."""
    body = method(SRC, 'cmd_KLIPPER_MMU_' + cmd)
    helpers = dict.fromkeys(re.findall(r'self\.(_[a-z]\w*)\(', body))
    return body + ''.join(method(SRC, h) for h in helpers)


def registration(cmd):
    """The if-conditions around the registration, and where a conditional
    attribute comes from in the configuration."""
    i = next(n for n, l in enumerate(LINES) if "'KLIPPER_MMU_%s'" % cmd in l)
    indent, conds = len(LINES[i]) - len(LINES[i].lstrip()), []
    for line in reversed(LINES[:i]):
        lead = len(line) - len(line.lstrip())
        if line.strip() and lead < indent:
            if line.strip().startswith('def '):
                break
            if line.strip().startswith('if '):
                conds.append(line.strip())
            indent = lead
    if not conds:
        return 'Registered unconditionally: available in every configuration.'
    text = 'Registered only when: ' + ' and '.join(reversed(conds))
    for attr in re.findall(r'self\.(_\w+)', ' '.join(conds)):
        found = [n for n, l in enumerate(LINES) if 'self.%s = ' % attr in l]
        if found:
            end = found[1] if len(found) > 1 and found[1] - found[0] < 20 else found[0]
            text += '\nWhere self.%s comes from:\n%s' % (attr, '\n'.join(LINES[found[0]:end + 1]))
    return text


def config_key(cmd):
    """The printer.cfg option the registration depends on, if any."""
    reg = registration(cmd)
    m = (re.search(r"config\.getboolean\('([a-z_]+)'", reg)
         or re.search(r"config\.get\('([a-z_]+_file)'", reg))
    if m:
        return m.group(1)
    if 'openams_motion' in reg:
        return 'openams_motion'
    m = re.search(r'self\.(_\w+)', reg)
    return m.group(1).lstrip('_') if m else None


def rejections(cmd):
    msgs = []
    for m in re.finditer(r"raise [\w.]+\(\s*((?:'[^']*'\s*)+)", handler(cmd)):
        msg = ''.join(re.findall(r"'([^']*)'", m.group(1))).strip()
        if msg and msg not in msgs:
            msgs.append(msg)
    return msgs


def params(cmd):
    """{name: (low, high) or None}: every parameter the handler reads,
    including names read in a loop over a tuple of string literals."""
    body, out = handler(cmd), {}
    for var, names in re.findall(r"for (\w+) in \(([^)]*)\)", body):
        if re.search(r"gcmd\.get\w*\(%s\b" % var, body):
            out.update((n.upper(), None) for n in re.findall(r"'([a-zA-Z_]+)'", names))
    for name, rest in re.findall(r"gcmd\.get(?:_int|_float)?\('([A-Z_]+)'([^)]*)\)", body):
        lo, hi = re.search(r'minval=(\d+)', rest), re.search(r'maxval=(0x[0-9a-f]+|\d+)\b(?!\s*-)', rest)
        rng = (int(lo.group(1)), int(hi.group(1), 0)) if lo and hi else None
        out[name] = out.get(name) or rng
    return out


def required(cmd):
    """Read with get_int and no default, and never probed with a None default."""
    body = method(SRC, 'cmd_KLIPPER_MMU_' + cmd)
    maybe = set(re.findall(r"gcmd\.get\('([A-Z_]+)', None\)", body))
    return set(re.findall(r"gcmd\.get_int\('([A-Z_]+)', min", body)) - maybe


def enum(name):
    body = re.search(r'class %s\b.*?\n\n' % name, WIRE, re.S).group(0)
    return {int(v): k for k, v in re.findall(r'^\s+([A-Z_]+) = (\d+)', body, re.M)}


def facts(cmd, names):
    lines = ['Command: KLIPPER_MMU_' + cmd]
    desc = re.search(r"'KLIPPER_MMU_%s', 'MMU', self\._name,\s*self\.\w+,\s*desc='([^']*)'" % cmd, SRC)
    lines.append('Registered description: ' + (desc.group(1) if desc else '-'))
    lines.append('Handler source (host/__init__.py):\n' + handler(cmd)[:7000])
    for p, cls in ENUM_PARAMS.items():
        rng = params(cmd).get(p)
        if rng:
            values = enum(cls)
            lines.append('%s values (host/wire.py %s): %s' % (p, cls, ', '.join(
                '%d=%s' % (n, values[n]) for n in range(rng[0], rng[1] + 1) if n in values)))
    for start in dict.fromkeys(re.findall(r'unit\.(start_\w+)\(', handler(cmd))):
        checks = [l.strip() for l in method(UNIT, start).splitlines() if 'raise' in l or 'if ' in l]
        lines.append('The managed unit (host/openams_managed_unit.py %s) checks:\n%s'
                     % (start, '\n'.join(checks[:10])))
    lines.append(registration(cmd))
    lines.append('The handler rejects the command with these errors (its parameter rules):\n'
                 + ('\n'.join('- ' + r for r in rejections(cmd)) or '- none'))
    paras = [p.strip() for d in DOCS for p in (REPO / d).read_text().split('\n\n')
             if re.search(r'\bKLIPPER_MMU_%s\b' % cmd, p)]
    lines.append('Documentation that mentions it:\n' + ('\n\n'.join(paras)[:3500] or '-'))
    lines.append('This is one of exactly %d commands: %s. No other KLIPPER_MMU_ command exists.'
                 % (len(names), ', '.join('KLIPPER_MMU_' + n for n in names)))
    return '\n\n'.join(lines)


def prompt(names, mmu):
    return ('\n\n---\n\n'.join(facts(c, names) for c in names) + '\n\n'
            'Write docs/g-code-commands.md for BoxTurtle owners covering all %d commands above, using '
            'only these facts; never invent a command, parameter, value, option or behaviour, and write '
            '"undocumented" if unsure. Start with an "Everyday commands" list. For each command write a '
            'section headed "### `KLIPPER_MMU_NAME` — Everyday" or "— Advanced" (%s) with: Purpose; '
            'Parameters (each marked required or optional, with its allowed values and any rule about '
            'combining it with another parameter, from the rejection errors); Example (one invocation '
            'with MMU=%s and values the code accepts; quote values that contain spaces); When to use it '
            '(one concrete situation); Behavior; "Motor movement: Yes/No"; Configuration (the '
            'printer.cfg options from the registration condition; "none" only when registered '
            'unconditionally). MMU=%s is required on every command: never list MMU as a parameter. '
            'Plain language, no internal terms such as %s. Output only the Markdown.'
            % (len(names), AUDIENCE, mmu, mmu, ', '.join(FORBIDDEN)))


def ask(text, temperature=0.6):
    body = {'model': 'local', 'messages': [{'role': 'user', 'content': text}],
            'temperature': temperature, 'max_tokens': 14000,
            'chat_template_kwargs': {'enable_thinking': False}}
    req = urllib.request.Request(URL, data=json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=1800) as r:
        return json.load(r)['choices'][0]['message']['content']


def fix_examples(md, mmu):
    """Mechanical slips only: a doubled command name, and MMU= missing from an
    invocation (backticked with parameters, on an Example line, or a code
    line). Headings and bare mentions are left alone."""
    def fix(inv):
        inv = re.sub(r'\b(KLIPPER_MMU_[A-Z_]+)(\s+\1\b)+', r'\1', inv)
        return inv if 'MMU=' in inv else re.sub(r'^(KLIPPER_MMU_[A-Z_]+)', r'\1 MMU=' + mmu, inv)

    out = []
    for line in md.splitlines():
        dedup = re.sub(r'\b(KLIPPER_MMU_[A-Z_]+)(\s+\1\b)+', r'\1', line)
        if line.startswith('#'):
            out.append(dedup)
        elif re.match(r'KLIPPER_MMU_[A-Z_]+', line):
            out.append(fix(line))
        else:
            example = 'Example' in line
            out.append(re.sub(r'`(KLIPPER_MMU_[A-Z_]+(?:[ \t]+[^`\n]*)?)`', lambda m: '`%s`' % (
                fix(m.group(1)) if example or '=' in m.group(1) else m.group(1)), dedup))
    return '\n'.join(out) + ('\n' if md.endswith('\n') else '')


def plain_language(md):
    """Rewrite each line that uses a forbidden term, one line per request.
    A rewrite is kept only if the terms are gone and every backticked token
    and command name survives; otherwise the line stays for the reviewer."""
    def term(line):
        return any(re.search(r'\b%s\b' % w, re.sub(r'KLIPPER_MMU_\w+', '', line), re.I) for w in FORBIDDEN)

    def rewrite(line):
        if line.startswith('#') or not term(line):
            return line
        keep = set(re.findall(r'`[^`]+`|KLIPPER_MMU_\w+', line))
        for temperature in (0.2, 0.7):  # a second, looser attempt
            new = ask('Rewrite this Markdown line in plain language for 3D-printer owners, keeping its '
                      'meaning, formatting and every `backticked` item unchanged, and without the words: '
                      '%s. What the terms mean here: %s. Reply with the rewritten line only.\n\n%s'
                      % (', '.join(FORBIDDEN), '; '.join('%s = %s' % kv for kv in GLOSSARY.items()), line),
                      temperature=temperature).strip()
            if new and '\n' not in new and not term(new) and keep <= set(
                    re.findall(r'`[^`]+`|KLIPPER_MMU_\w+', new)):
                return new
        return line

    lines = md.splitlines()
    with ThreadPoolExecutor(4) as pool:
        lines = list(pool.map(rewrite, lines))
    return '\n'.join(lines) + ('\n' if md.endswith('\n') else '')


def sections(md):
    out, cur = {}, None
    for line in md.splitlines():
        m = re.match(r'#{2,4} .*?KLIPPER_MMU_([A-Z_]+)', line)
        if m:
            cur = m.group(1)
            out.setdefault(cur, [])
        elif line.startswith('#'):
            cur = None
        if cur:
            out[cur].append(line)
    for line in md.splitlines():  # table rows describe their command too
        for c in set(re.findall(r'KLIPPER_MMU_([A-Z_]+)', line)) if line.startswith('|') else ():
            out.setdefault(c, []).append(line)
    return {k: '\n'.join(v) for k, v in out.items()}


def invocation_problems(cmd, inv):
    try:
        tokens = shlex.split(inv)
    except ValueError:
        return ['unbalanced quotes']
    known, problems = params(cmd), []
    for t in tokens[1:]:
        if '=' not in t:
            problems.append('stray word %r (quote values with spaces)' % t)
            continue
        key, value = t.split('=', 1)
        if key == 'MMU':
            continue
        if key not in known:
            problems.append('unknown parameter %s' % key)
        elif known[key] and re.fullmatch(r'-?\d+', value) and not known[key][0] <= int(value) <= known[key][1]:
            problems.append('%s=%s outside %d..%d' % (key, value, *known[key]))
    if 'MMU=' not in inv:
        problems.append('no MMU=')
    return problems


def check(md):
    """Problems found by checks derived from the code (and the brief's word list)."""
    names, secs, fails = commands(), sections(md), []
    fails += [(c, 'not a registered command') for c in secs if c not in names]
    client, optype = enum('Client'), enum('OpType')
    for c in names:
        s = secs.get(c)
        if s is None:
            fails.append((c, 'missing'))
            continue
        runnable = []
        for line in s.splitlines():
            if re.match(r'KLIPPER_MMU_%s\b' % c, line):
                runnable.append(line.strip())
            elif not line.startswith('#'):
                runnable += [i for i in re.findall(r'`(KLIPPER_MMU_%s\b[^`\n]*)`' % c, line)
                             if '=' in i or 'Example' in line]
        good = False
        for inv in runnable:
            problems = invocation_problems(c, inv)
            fails += [(c, '%s: %s' % (inv, p)) for p in problems]
            good = good or not problems
        if not good:
            fails.append((c, 'no valid example with MMU='))
        key = config_key(c)
        if key and not re.search(r'\b%s\b' % key, s):
            fails.append((c, 'does not name its option %s' % key))
        if re.search(r'(?m)^\s*[-*|][^\n]*\bMMU\b[^\n]*optional', s):
            fails.append((c, 'MMU shown as optional'))
        for p in sorted(required(c)):
            line = re.search(r'(?m)^\s*[-*][^\n]*\b%s\b[^\n]*' % p, s)
            if line and 'optional' in line.group(0).lower():
                fails.append((c, '%s shown as optional' % p))
        for n, name in re.findall(r'\b(\d)\s*[=(]\s*([A-Z_]{3,})\b', s):
            for values, label in ((optype, 'OPERATION'), (client, 'CLIENT')):
                if name in values.values() and values.get(int(n)) != name:
                    fails.append((c, '%s %s=%s is wrong (wire.py: %s)' % (label, n, name, values.get(int(n)))))
        plain = re.sub(r'KLIPPER_MMU_[A-Z_]+', '', s)
        fails += [(c, 'forbidden term "%s"' % w) for w in FORBIDDEN
                  if re.search(r'\b%s\b' % w, plain, re.I)]
    return fails


def draft(out, runs, mmu, force):
    if out.exists() and not force:
        sys.exit('%s exists; pass --force to replace it' % out)
    names = commands()
    text = prompt(names, mmu)
    with ThreadPoolExecutor(runs) as pool:
        docs = [fix_examples(d, mmu) for d in pool.map(lambda _: ask(text), range(runs))]
    scored = sorted(((len(check(d)), i) for i, d in enumerate(docs)))
    best = plain_language(docs[scored[0][1]])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(best)
    report = ['check failures per run: %s; kept run %d' % ([f for f, _ in sorted(scored, key=lambda s: s[1])],
                                                            scored[0][1] + 1)]
    report += ['%s: %s' % f for f in check(best)] or ['no check failures']
    report.append('Checks cover structure and facts derivable from code. Review meaning before use: '
                  'behaviour descriptions, omitted caveats, and parameter combinations.')
    out.with_name(out.name + '.report').write_text('\n'.join(report) + '\n')
    print('\n'.join(report))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    d = sub.add_parser('draft', help='generate a checked draft of the command reference')
    d.add_argument('--out', type=Path, required=True)
    d.add_argument('--runs', type=int, default=3)
    d.add_argument('--mmu', default='box')
    d.add_argument('--force', action='store_true')
    c = sub.add_parser('check', help='check an existing document against the code')
    c.add_argument('file', type=Path)
    args = parser.parse_args()
    if args.action == 'draft':
        draft(args.out, args.runs, args.mmu, args.force)
        return
    fails = check(args.file.read_text())
    print('\n'.join('%s: %s' % f for f in fails) or 'no check failures')
    sys.exit(1 if fails else 0)


if __name__ == '__main__':
    main()
