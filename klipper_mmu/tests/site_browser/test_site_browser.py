# SPDX-License-Identifier: GPL-3.0-or-later
"""tests/site_browser/test_site_browser.py: --help always runs; the smoke
test drives a real headless Chrome against a tiny fixture page and is
skipped when no Chrome/Chromium binary is on PATH (or CHROME_PATH is unset
and none of the usual names resolve). No network access is used."""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOL = HERE.parents[1] / 'tools' / 'site_browser.mjs'

FIXTURE = '''<!doctype html>
<html><head><title>fixture</title></head>
<body><h1 id="hi">hello</h1>
<script>window.__ran = 1;</script>
</body></html>
'''


def have_chrome():
    import os
    if os.environ.get('CHROME_PATH'):
        return True
    return any(shutil.which(c) for c in ('google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser'))


class SiteBrowserTest(unittest.TestCase):
    def run_tool(self, args, timeout=60):
        return subprocess.run(
            ['node', str(TOOL)] + args,
            capture_output=True, text=True, timeout=timeout,
        )

    def test_help(self):
        r = self.run_tool(['--help'], timeout=15)
        self.assertEqual(r.returncode, 0)
        self.assertIn('Steps', r.stdout)
        self.assertIn('--scroll', r.stdout)
        self.assertIn('--shot', r.stdout)

    def test_missing_url_is_an_error(self):
        r = self.run_tool(['--size', '800x600'], timeout=15)
        self.assertNotEqual(r.returncode, 0)

    @unittest.skipUnless(have_chrome(), 'no google-chrome/chromium on PATH')
    def test_smoke_eval_and_screenshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp) / 'fixture.html'
            fixture.write_text(FIXTURE)
            shot = Path(tmp) / 'out.png'
            r = self.run_tool([
                'file://' + str(fixture), '--size', '400x300', '--timeout', '30000',
                '--eval', 'document.title',
                '--eval', 'window.__ran',
                '--shot', str(shot),
            ], timeout=45)
            self.assertEqual(r.returncode, 0, r.stderr)
            lines = [l for l in r.stdout.splitlines() if l.strip()]
            self.assertEqual(json.loads(lines[0]), 'fixture')
            self.assertEqual(json.loads(lines[1]), 1)
            self.assertTrue(shot.exists())
            self.assertGreater(shot.stat().st_size, 0)
            self.assertIn('no console errors', r.stderr)


if __name__ == '__main__':
    unittest.main()
