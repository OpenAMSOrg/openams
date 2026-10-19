"""install_helpers/gco_baseline.py: whether gco-routines accepts a Klipper's
klippy/gcode.py, read from the checkout's own list without running it."""
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest

HELPER = os.path.join(os.path.dirname(__file__), "..", "install_helpers", "gco_baseline.py")
GCODE_PY = b"# a Klipper dispatcher\n"
GCODE_SHA = hashlib.sha256(GCODE_PY).hexdigest()
OTHER_SHA = "a" * 64


def integration_py(hashes):
    return ("import os\n"
            "# c0c7ef2 contains the local CAN changes; ad425fc is the upstream pin.\n"
            "KNOWN_GCODE_SHA256 = frozenset({\n%s})\n" % "".join(
                '    "%s",\n' % h for h in hashes))


class GcoBaselineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.klipper = os.path.join(self.tmp.name, "klipper")
        self.gco = os.path.join(self.tmp.name, "gco-routines")
        os.makedirs(os.path.join(self.klipper, "klippy"))
        os.makedirs(os.path.join(self.gco, "klippy_extra", "gco_routines"))
        with open(os.path.join(self.klipper, "klippy", "gcode.py"), "wb") as handle:
            handle.write(GCODE_PY)

    def write_list(self, text):
        with open(os.path.join(self.gco, "klippy_extra", "gco_routines",
                               "integration.py"), "w") as handle:
            handle.write(text)

    def run_helper(self):
        done = subprocess.run([sys.executable, HELPER, "--klipper", self.klipper,
                               "--gco-routines", self.gco],
                              stdout=subprocess.PIPE, universal_newlines=True,
                              timeout=30)
        self.assertEqual(done.returncode, 0)
        return dict(line.split("=", 1) for line in done.stdout.splitlines())

    def test_an_accepted_dispatcher_is_yes(self):
        self.write_list(integration_py([OTHER_SHA, GCODE_SHA]))
        self.assertEqual(self.run_helper(),
                         {"GCODE_PY_SHA256": GCODE_SHA, "GCO_BASELINE": "yes"})

    def test_any_other_dispatcher_is_no(self):
        self.write_list(integration_py([OTHER_SHA]))
        self.assertEqual(self.run_helper()["GCO_BASELINE"], "no")

    def test_no_checkout_is_unknown(self):
        self.assertEqual(self.run_helper(),
                         {"GCODE_PY_SHA256": GCODE_SHA, "GCO_BASELINE": "unknown"})

    def test_a_list_it_cannot_read_is_unknown_never_no(self):
        for text in ("KNOWN_GCODE_SHA256 = frozenset(load())\n",
                     "KNOWN_GCODE_SHA256 = frozenset({'not a hash'})\n",
                     "RENAMED = frozenset({'%s'})\n" % GCODE_SHA,
                     "def broken(:\n"):
            with self.subTest(text=text):
                self.write_list(text)
                self.assertEqual(self.run_helper()["GCO_BASELINE"], "unknown")

    def test_the_list_is_parsed_not_run(self):
        marker = os.path.join(self.tmp.name, "ran")
        self.write_list("open(%r, 'w').close()\n" % marker
                        + integration_py([GCODE_SHA]))
        self.assertEqual(self.run_helper()["GCO_BASELINE"], "yes")
        self.assertFalse(os.path.exists(marker))

    def test_no_gcode_py_is_unknown(self):
        self.write_list(integration_py([GCODE_SHA]))
        os.unlink(os.path.join(self.klipper, "klippy", "gcode.py"))
        self.assertEqual(self.run_helper(),
                         {"GCODE_PY_SHA256": "", "GCO_BASELINE": "unknown"})


if __name__ == "__main__":
    unittest.main()
