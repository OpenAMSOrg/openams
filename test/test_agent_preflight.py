"""tools/agent/preflight.sh: whatever host it runs on, its output is the
KEY=value contract install.agents.md reads, ending in a VERDICT line."""
import os
import re
import subprocess
import unittest

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "tools", "agent", "preflight.sh")
LINE = re.compile(r"^[A-Z][A-Z0-9_a-z]*=.*$")


class PreflightFormatTests(unittest.TestCase):
    def run_preflight(self):
        # A port nothing listens on: Moonraker "does not answer", on any host.
        done = subprocess.run(["bash", SCRIPT, "--url", "http://127.0.0.1:9"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, timeout=120)
        return done.stdout.splitlines()

    def test_every_line_is_key_value_and_the_last_is_the_verdict(self):
        lines = self.run_preflight()
        self.assertTrue(lines)
        for line in lines:
            self.assertRegex(line, LINE)
        self.assertIn(lines[-1], ("VERDICT=OK", "VERDICT=ASK", "VERDICT=STOP"))

    def test_an_unreachable_moonraker_is_a_question_not_a_guess(self):
        lines = self.run_preflight()
        self.assertIn("MOONRAKER_HTTP=no", lines)
        self.assertIn("PRINT_STATE=unknown", lines)
        self.assertTrue(any(l.startswith("ASK=Moonraker does not answer") for l in lines))
        self.assertNotEqual(lines[-1], "VERDICT=OK")

    def test_it_names_the_keys_the_guide_reads(self):
        keys = set(l.split("=", 1)[0] for l in self.run_preflight())
        for key in ("PRINT_STATE", "KLIPPER_SERVICES", "KLIPPER_PATH", "CONFIG_DIR",
                    "OAMS_EXTRA", "FOREIGN_MMU", "NETWORK_GITHUB", "SUDO_NOPASSWD",
                    "CAN_INTERFACES", "DISK_FREE_MB", "KLIPPY_ENV_PYTHON",
                    "GCO_BASELINE", "GCO_ACTIVE", "VERDICT"):
            self.assertIn(key, keys)


class OamsExtraTests(unittest.TestCase):
    """OAMS_EXTRA tells a fresh install from an update: a missing link is
    `none`, never `openams` (readlink -f names a path that does not exist)."""

    def run_with_home(self, home):
        env = dict(os.environ, HOME=home)
        done = subprocess.run(["bash", SCRIPT, "--url", "http://127.0.0.1:9",
                               "--klipper-path", os.path.join(home, "klipper"),
                               "--config-dir", os.path.join(home, "config")],
                              stdout=subprocess.PIPE, universal_newlines=True,
                              env=env, timeout=120)
        return [l for l in done.stdout.splitlines() if l.startswith("OAMS_EXTRA=")]

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        os.makedirs(os.path.join(self.home, "klipper", "klippy", "extras"))
        os.makedirs(os.path.join(self.home, "openams", "src"))

    def tearDown(self):
        self.tmp.cleanup()

    def extras(self, name):
        return os.path.join(self.home, "klipper", "klippy", "extras", name)

    def test_no_link_is_none(self):
        self.assertEqual(self.run_with_home(self.home), ["OAMS_EXTRA=none"])

    def test_a_dangling_link_is_broken(self):
        os.symlink(os.path.join(self.home, "openams", "src", "oams.py"), self.extras("oams.py"))
        lines = self.run_with_home(self.home)
        self.assertTrue(lines[0].startswith("OAMS_EXTRA=broken ("), lines)

    def test_a_live_link_is_openams(self):
        target = os.path.join(self.home, "openams", "src", "oams.py")
        open(target, "w").close()
        os.symlink(target, self.extras("oams.py"))
        self.assertEqual(self.run_with_home(self.home), ["OAMS_EXTRA=openams (%s)" % target])


class GcoRoutinesTests(unittest.TestCase):
    """GCO_BASELINE says whether gco-routines accepts this Klipper, from the
    list in ~/gco-routines; enabling it on one it refuses is a question."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        os.makedirs(os.path.join(self.home, "klipper", "klippy", "extras"))
        os.makedirs(os.path.join(self.home, "config"))
        with open(os.path.join(self.home, "klipper", "klippy", "gcode.py"), "wb") as handle:
            handle.write(b"# a Klipper dispatcher\n")

    def accept(self, sha):
        path = os.path.join(self.home, "gco-routines", "klippy_extra", "gco_routines")
        os.makedirs(path)
        with open(os.path.join(path, "integration.py"), "w") as handle:
            handle.write('KNOWN_GCODE_SHA256 = frozenset({"%s"})\n' % sha)

    def enable(self):
        with open(os.path.join(self.home, "config", "printer.cfg"), "w") as handle:
            handle.write("[gco_routines]\n[include oams.cfg]\n[printer]\n")

    def run_preflight(self):
        done = subprocess.run(["bash", SCRIPT, "--url", "http://127.0.0.1:9",
                               "--klipper-path", os.path.join(self.home, "klipper"),
                               "--config-dir", os.path.join(self.home, "config")],
                              stdout=subprocess.PIPE, universal_newlines=True,
                              env=dict(os.environ, HOME=self.home), timeout=120)
        return done.stdout.splitlines()

    def gco_asks(self, lines):
        return [l for l in lines if l.startswith("ASK=gco-routines")]

    def test_not_cloned_yet_is_unknown(self):
        lines = self.run_preflight()
        self.assertIn("GCO_BASELINE=unknown", lines)
        self.assertIn("GCO_ACTIVE=no", lines)

    def test_an_accepted_klipper_is_yes(self):
        import hashlib
        self.accept(hashlib.sha256(b"# a Klipper dispatcher\n").hexdigest())
        self.enable()
        lines = self.run_preflight()
        self.assertIn("GCO_BASELINE=yes", lines)
        self.assertEqual(self.gco_asks(lines), [])

    def test_enabled_on_a_klipper_it_refuses_is_a_question(self):
        self.accept("a" * 64)
        self.enable()
        lines = self.run_preflight()
        self.assertIn("GCO_BASELINE=no", lines)
        self.assertTrue(any(l.startswith("GCO_ACTIVE=yes (") for l in lines), lines)
        self.assertEqual(len(self.gco_asks(lines)), 1, lines)
        self.assertNotEqual(lines[-1], "VERDICT=OK")

    def test_installed_but_not_enabled_needs_nothing(self):
        self.accept("a" * 64)
        lines = self.run_preflight()
        self.assertIn("GCO_BASELINE=no", lines)
        self.assertIn("GCO_ACTIVE=no", lines)
        self.assertEqual(self.gco_asks(lines), [])


if __name__ == "__main__":
    unittest.main()
