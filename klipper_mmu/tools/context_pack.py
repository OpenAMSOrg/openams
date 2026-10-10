#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Print a context pack: the exact source an implementer needs before editing.

An implementer otherwise spends most of its budget surveying code. Given
`path:symbol` (a C function or a Python def/class method) or `path:START-END`
(a line range), print each definition's location and signature (and its body
with --with-code), plus a two-line summary of each body unless
--no-model. The model is a local model behind tools/local_doc.py
(LOCAL_DOC_URL); its failure is reported inline and never fails the pack. A
not-found spec sets exit status 1.

  python3 tools/context_pack.py src/operation_core.c:fps_update host/__init__.py:renew_host_lease
  python3 tools/context_pack.py --with-code --no-model src/rate.c:1-40
"""
import argparse
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from local_doc import ask  # noqa: E402

REPO = HERE.parent
SUMMARY_PROMPT = ('In at most two lines: what this function does, its inputs and '
                  'outputs, and what state it changes. Only facts visible in the '
                  'code; no advice.')
MODEL_TIMEOUT, WORKERS = 120, 4


def parse_spec(spec):
    """('path', ('symbol', name)) or ('path', ('range', start, end))."""
    path, _, rest = spec.rpartition(':')
    m = re.fullmatch(r'(\d+)-(\d+)', rest)
    return path, (('range', int(m.group(1)), int(m.group(2))) if m else ('symbol', rest))


def c_definition(lines, symbol):
    """Definitions of C function `symbol`: a `{` before any `;` opens the body,
    which ends at the first line that is exactly `}`. A prototype does not match."""
    pattern = re.compile(r'^\S.*\b%s\s*\(' % re.escape(symbol))
    out = []
    for i, line in enumerate(lines):
        if not pattern.match(line):
            continue
        signature, opened = [], False
        for text in lines[i:]:
            brace = text.find('{')
            if brace >= 0:
                signature.append(text[:brace].rstrip())
                opened = True
                break
            if ';' in text:
                break
            signature.append(text.rstrip())
        if not opened:
            continue
        end = next((j for j in range(i, len(lines)) if lines[j] == '}'), None)
        if end is None:
            continue
        out.append(dict(start=i + 1, end=end + 1, symbol=symbol,
                        signature=[s for s in signature if s.strip()],
                        body=lines[i:end + 1]))
    return out


def py_definition(lines, symbol):
    """Python `def`/`class` `symbol`; the body ends at the first non-blank line
    indented at or below it, and the signature at its closing colon."""
    pattern = re.compile(r'^(\s*)(?:def|class)\s+%s\s*[\(:]' % re.escape(symbol))
    out = []
    for i, line in enumerate(lines):
        m = pattern.match(line)
        if not m:
            continue
        indent = len(m.group(1))
        end = len(lines) - 1
        for j in range(i + 1, len(lines)):
            if lines[j].strip() and len(lines[j]) - len(lines[j].lstrip()) <= indent:
                end = j - 1
                break
        while end > i and not lines[end].strip():
            end -= 1
        signature = []
        for text in lines[i:end + 1]:
            signature.append(text.rstrip())
            if text.rstrip().endswith(':'):
                break
        out.append(dict(start=i + 1, end=end + 1, symbol=symbol,
                        signature=signature, body=lines[i:end + 1]))
    return out


def resolve(spec):
    """(path, matches); a match has start, end, symbol, signature and body."""
    path, (kind, *args) = parse_spec(spec)
    file = REPO / path
    if not file.is_file():
        return path, []
    lines = file.read_text().splitlines()
    if kind == 'range':
        start, end = max(1, args[0]), min(args[1], len(lines))
        if start > len(lines) or end < start:
            return path, []
        body = lines[start - 1:end]
        return path, [dict(start=start, end=end, symbol=None,
                           signature=[body[0].rstrip()], body=body)]
    find = py_definition if path.endswith('.py') else c_definition
    return path, find(lines, args[0])


def summaries(bodies):
    """One summary per body, in order; a failed call stays a visible string."""
    pool = ThreadPoolExecutor(WORKERS)
    futures = [pool.submit(ask, SUMMARY_PROMPT + '\n\n' + body) for body in bodies]
    out = []
    for future in futures:
        try:
            out.append(future.result(timeout=MODEL_TIMEOUT).strip())
        except Exception as exc:  # the deterministic pack is always complete
            out.append('(summary unavailable: %s)' % exc)
    pool.shutdown(wait=False, cancel_futures=True)
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--no-model', action='store_true', help='skip the model summaries')
    parser.add_argument('--with-code', action='store_true', help='include each body')
    parser.add_argument('specs', nargs='+', metavar='SPEC', help='path:symbol or path:START-END')
    args = parser.parse_args(argv)

    packs = [(spec,) + resolve(spec) for spec in args.specs]
    jobs = [match for _, _, matches in packs for match in matches]
    found = [None] * len(jobs) if args.no_model else summaries(
        ['\n'.join(match['body']) for match in jobs])
    for match, summary in zip(jobs, found):
        match['summary'] = summary

    blocks, missing = [], False
    for spec, path, matches in packs:
        if not matches:
            missing = True
            blocks.append('### %s: not found' % spec)
            continue
        for match in matches:
            head = '### %s:%d-%d' % (path, match['start'], match['end'])
            if match['symbol']:
                head += ' ' + match['symbol']
            block = [head] + match['signature']
            if match['summary']:
                block.append(match['summary'])
            if args.with_code:
                block += ['```', *match['body'], '```']
            blocks.append('\n'.join(block))
    print('\n\n'.join(blocks))
    return 1 if missing else 0


if __name__ == '__main__':
    sys.exit(main())
