# SPDX-License-Identifier: GPL-3.0-or-later
"""tools/device_index.py test."""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

class DeviceIndexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = Path(self.tmp.name)
        fixture_src = ROOT / "tests/device_index/fixtures/test_device.json"
        shutil.copy(fixture_src, self.src / "test_device.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_device_index(self):
        run = subprocess.run([sys.executable, str(ROOT / 'tools/device_index.py'),
                              '--dir', str(self.src)],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0)
        
        readme = (self.src / "README.md").read_text()
        self.assertIn("| [test_device](test_device.md) | 5 | filament_compression | analog | mmu_exit_0 -1.0 | mmu_exit_0 -1.0 | 2 |", readme)

    def test_check_fails_when_stale(self):
        run = subprocess.run([sys.executable, str(ROOT / 'tools/device_index.py'),
                              '--check', '--dir', str(self.src)],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 1)
        self.assertIn("stale:", run.stdout)
        
        subprocess.run([sys.executable, str(ROOT / 'tools/device_index.py'),
                        '--dir', str(self.src)], check=True)
                        
        run2 = subprocess.run([sys.executable, str(ROOT / 'tools/device_index.py'),
                               '--check', '--dir', str(self.src)],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(run2.returncode, 0)

    def test_help(self):
        run = subprocess.run([sys.executable, str(ROOT / 'tools/device_index.py'),
                              '--help'], capture_output=True, text=True, timeout=30)
        self.assertEqual(run.returncode, 0)
        self.assertIn("docs/devices/README.md", run.stdout)

if __name__ == '__main__':
    unittest.main()
