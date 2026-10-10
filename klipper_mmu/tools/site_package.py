#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Check the docs site and zip it for Netlify.

Every internal href/src in site/*.html must name an existing file, and every
#fragment an existing id in that file; every chapter href in the nav manifest
(assets/site.js) must exist. Then the whole site directory is zipped with
index.html at the root, ready for Netlify Drop or `netlify deploy --dir`.

    python3 tools/site_package.py                 # site/ -> build/netlify/klipper-mmu-docs.zip
    python3 tools/site_package.py --check-only    # links only, no zip
Exit 1 with one line per broken reference.
"""
import argparse
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REF = re.compile(r'(?:href|src)="([^"]+)"')
IDS = re.compile(r'\bid="([^"]+)"')
MANUAL_HREF = re.compile(r"href: '([^']+)'")


def broken_refs(site):
    problems = []
    pages = sorted(site.glob('*.html'))
    ids = {p.name: set(IDS.findall(p.read_text())) for p in pages}
    refs = [(p.name, r) for p in pages for r in REF.findall(p.read_text())]
    manual = site / 'assets' / 'site.js'
    if manual.exists():
        refs += [('assets/site.js', r) for r in MANUAL_HREF.findall(manual.read_text())]
    for source, ref in refs:
        if re.match(r'[a-z]+:', ref) or ref.startswith('//'):
            continue
        path, _, frag = ref.partition('#')
        target = path.lstrip('/') or source
        if path in ('', './', '/'):
            target = source if path == '' else 'index.html'
        if not (site / target).is_file():
            problems.append('%s: %s: no such file' % (source, ref))
        elif frag and target.endswith('.html') and frag not in ids.get(target, set()):
            problems.append('%s: %s: no id "%s"' % (source, ref, frag))
    return problems


def package(site, out):
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        for f in sorted(site.rglob('*')):
            if f.is_file() and not f.name.startswith('.'):
                z.write(f, f.relative_to(site).as_posix())
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--site', type=Path, default=ROOT / 'site')
    ap.add_argument('--out', type=Path, default=ROOT / 'build' / 'netlify' / 'klipper-mmu-docs.zip')
    ap.add_argument('--check-only', action='store_true')
    args = ap.parse_args(argv)
    problems = broken_refs(args.site)
    for p in problems:
        print(p)
    if problems:
        return 1
    if not args.check_only:
        print(package(args.site, args.out))
    return 0


if __name__ == '__main__':
    sys.exit(main())
