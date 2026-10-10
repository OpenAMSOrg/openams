# SPDX-License-Identifier: GPL-3.0-or-later
"""tests/site_package/test_site_package.py: the link check catches missing
files, missing #ids and missing nav chapters, and a clean site zips with its
pages at the root."""
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

TOOL = Path(__file__).resolve().parents[2] / 'tools' / 'site_package.py'


def run(*args):
    return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True, timeout=60)


def make_site(root, index, other='<h2 id="a">A</h2>', manual="href: 'other.html'"):
    (root / 'assets').mkdir(parents=True)
    (root / 'index.html').write_text(index)
    (root / 'other.html').write_text(other)
    (root / 'assets' / 'site.js').write_text("var MANUAL = [{ %s }];" % manual)
    (root / 'assets' / 'site.css').write_text('')


class SitePackageTest(unittest.TestCase):
    def test_help(self):
        r = run('--help')
        self.assertEqual(r.returncode, 0)
        self.assertIn('Netlify', r.stdout)

    def test_clean_site_zips(self):
        with tempfile.TemporaryDirectory() as d:
            site, out = Path(d) / 'site', Path(d) / 'out' / 'docs.zip'
            make_site(site, '<link href="assets/site.css"><a href="other.html#a">x</a><a href="https://example.com">e</a>')
            r = run('--site', str(site), '--out', str(out))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            names = zipfile.ZipFile(out).namelist()
            self.assertIn('index.html', names)
            self.assertIn('assets/site.js', names)

    def test_broken_references(self):
        with tempfile.TemporaryDirectory() as d:
            site = Path(d) / 'site'
            make_site(site, '<a href="missing.html">m</a><a href="other.html#nope">n</a>', manual="href: 'gone.html'")
            r = run('--site', str(site), '--check-only')
            self.assertEqual(r.returncode, 1)
            self.assertIn('missing.html: no such file', r.stdout)
            self.assertIn('no id "nope"', r.stdout)
            self.assertIn('gone.html: no such file', r.stdout)


if __name__ == '__main__':
    unittest.main()
