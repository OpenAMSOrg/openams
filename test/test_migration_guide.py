"""End-to-end test for klipper_mmu/docs/migrating-to-openams.md.

Runs the documented migration against a fake printer tree: install klipper-mmu
from a copy of ``klipper_mmu/`` placed somewhere else (the old separate
checkout), remove it again with that checkout's own ``tools/install.py``, then
run this repository's ``install.sh``. The migrated tree must end up exactly as
a fresh ``install.sh`` run leaves it.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.sh"
GCO_URL = "https://github.com/OpenAMSOrg/gco-routines.git"

KLIPPER_KCONFIG = "# upstream Kconfig\nmenu \"General\"\nendmenu\n"
KLIPPER_MAKEFILE = "# upstream Makefile\nall:\n\techo hello\n"

# Stand-in for the gco-routines installer: the real one is a separate checkout.
GCO_INSTALLER = '''import argparse
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument('--klipper', required=True)
a = p.parse_args()
source = Path(__file__).resolve().parents[1] / 'klippy_extra/gco_routines'
dest = Path(a.klipper) / 'klippy/extras/gco_routines'
if not dest.is_symlink():
    dest.symlink_to(source, target_is_directory=True)
assert dest.resolve() == source
'''

# The old checkout is a copy of the package directory, exactly as the separate
# klipper-mmu repository is: no install.sh of its own.
IGNORE = shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc", "build")


def build_klipper_tree(root):
    """The fake Klipper source checkout that install.sh and install.py accept."""
    klipper = root / "klipper"
    (klipper / "klippy/extras").mkdir(parents=True)
    (klipper / "scripts").mkdir()
    (klipper / "src").mkdir()
    (klipper / "Makefile").write_text(KLIPPER_MAKEFILE)
    (klipper / "src/Kconfig").write_text(KLIPPER_KCONFIG)
    (klipper / "src/Makefile").write_text(KLIPPER_MAKEFILE)
    return klipper


def build_moonraker_tree(root):
    moonraker = root / "moonraker"
    (moonraker / "moonraker/components").mkdir(parents=True)
    (moonraker / "moonraker/__init__.py").write_text("# fixture\n")
    return moonraker


def build_config_tree(root):
    config = root / "printer_data/config"
    config.mkdir(parents=True)
    (config / "moonraker.conf").write_text("[server]\n")
    (config / "printer.cfg").write_text("[printer]\nkinematics: none\n")
    return config


def link_snapshot(klipper):
    """Everything the migrated tree and a fresh install must agree on."""
    return {
        "src/Kconfig": (klipper / "src/Kconfig").read_text(),
        "src/Makefile": (klipper / "src/Makefile").read_text(),
        "host extra link": os.readlink(klipper / "klippy/extras/klipper_mmu"),
        "mcu add-on link": os.readlink(klipper / "src/klipper_mmu"),
    }


class MigrationGuideTests(unittest.TestCase):
    def setUp(self):
        if os.geteuid() == 0:
            self.skipTest("install.sh intentionally refuses to run as root")
        self.temp = tempfile.TemporaryDirectory(prefix="openams-migration-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.klipper = build_klipper_tree(self.root)
        self.moonraker = build_moonraker_tree(self.root)
        self.config = build_config_tree(self.root)
        self.gco = self.root / "gco-routines"
        self.remote = self.root / "gco-remote"
        self.env = self._environment()
        self._build_gco_remote()
        self.old = self.root / "klipper-mmu"
        self._old_checkout_install()
        self._documented_removal()
        self._install_sh(self.klipper, self.config, "--with-mcu-addon")

    # ------------------------------------------------------------------
    # Fixture helpers
    # ------------------------------------------------------------------

    def _environment(self):
        gitconfig = self.root / "gitconfig"
        gitconfig.write_text(
            '[user]\nname = Installer Test\nemail = installer@example.invalid\n'
            '[url "file://%s"]\ninsteadOf = %s\n' % (self.remote, GCO_URL))
        return dict(
            os.environ,
            OPENAMS_NO_SERVICE="1",
            NO_TAGLIB_DOWNLOAD="1",
            OPENAMS_TAGLIB_PATH="/nonexistent/taglib",
            OPENAMS_SPOOLMAN_INFO_URL="file:///nonexistent/openams-spoolman",
            MOONRAKER_PATH=str(self.moonraker),
            GIT_CONFIG_GLOBAL=str(gitconfig),
            GIT_CONFIG_NOSYSTEM="1",
            GIT_TERMINAL_PROMPT="0",
            GIT_ALLOW_PROTOCOL="file",
        )

    def _build_gco_remote(self):
        (self.remote / "tools").mkdir(parents=True)
        (self.remote / "klippy_extra/gco_routines").mkdir(parents=True)
        (self.remote / "tools/install_gco_routines.py").write_text(GCO_INSTALLER)
        (self.remote / "klippy_extra/gco_routines/__init__.py").write_text("# fixture\n")
        self.run_cmd("git", "init", "-b", "main", str(self.remote))
        self.run_cmd("git", "-C", str(self.remote), "add", ".")
        self.run_cmd("git", "-C", str(self.remote), "commit", "-m", "fixture")

    def run_cmd(self, *args, **kwargs):
        return subprocess.run(args, env=self.env, text=True, capture_output=True,
                              timeout=300, **kwargs)

    def source_bytes(self):
        """The files a driver-API action could touch; absent stays absent."""
        names = ("src/Kconfig", "src/Makefile", "src/tmcuart.c",
                 "src/stm32/hard_pwm.c", "src/stm32/stm32h7_adc.c")
        return {name: (self.klipper / name).read_bytes()
                if (self.klipper / name).exists() else None for name in names}

    def _mmu_tool(self, checkout):
        return ("python3", str(Path(checkout) / "tools/install.py"))

    def _install_sh(self, klipper, config, *extra):
        result = self.run_cmd("bash", str(INSTALLER),
                          "-k", str(klipper), "-c", str(config),
                          "-s", "klipper-test", "-r", str(self.gco),
                          *extra, check=False, input="n\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    # ------------------------------------------------------------------
    # The three states the migration passes through
    # ------------------------------------------------------------------

    def _old_checkout_install(self):
        """Before: klipper-mmu installed from its own separate repository."""
        shutil.copytree(ROOT / "klipper_mmu", self.old, ignore=IGNORE)
        for action in ("install", "install-host"):
            result = self.run_cmd(*self._mmu_tool(self.old), action,
                              str(self.klipper), check=False)
            self.assertEqual(result.returncode, 0,
                             "%s from the old checkout: %s%s"
                             % (action, result.stdout, result.stderr))
        self.old_targets = {
            "host extra link": os.readlink(self.klipper / "klippy/extras/klipper_mmu"),
            "mcu add-on link": os.readlink(self.klipper / "src/klipper_mmu"),
        }

    def _documented_removal(self):
        """Step 1 of the guide: the old checkout's own tool removes its work."""
        for action in ("remove-host", "remove-driver-api", "remove"):
            before = self.source_bytes()
            result = self.run_cmd(*self._mmu_tool(self.old), action,
                              str(self.klipper), check=False)
            if action == "remove-driver-api":
                # A separate opt-in this checkout never installed. The guide
                # promises such a checkout is left untouched, which holds
                # whatever the action reports, so only that is asserted here.
                self.assertEqual(self.source_bytes(), before)
                continue
            self.assertEqual(result.returncode, 0, "removal reported an error: %s%s"
                             % (action, result.stdout + result.stderr))
        self.after_removal = self.source_bytes()
        self.after_removal_links = [os.path.lexists(self.klipper / "src/klipper_mmu"),
                                    os.path.lexists(self.klipper / "klippy/extras/klipper_mmu")]

    # ------------------------------------------------------------------
    # The migration leaves what a fresh install would
    # ------------------------------------------------------------------

    def test_the_starting_point_is_the_separate_checkout(self):
        """Both links pointed into the old klipper-mmu checkout."""
        self.assertTrue((self.old / "tools/install.py").is_file())
        self.assertFalse((self.old / "install.sh").exists())
        self.assertEqual(self.old_targets["mcu add-on link"], str(self.old.resolve()))
        self.assertEqual(self.old_targets["host extra link"],
                         str((self.old / "host").resolve()))

    def test_documented_removal_undoes_only_the_old_install(self):
        """Step 1 leaves the Klipper checkout exactly as upstream shipped it."""
        self.assertEqual(self.after_removal["src/Kconfig"], KLIPPER_KCONFIG.encode())
        self.assertEqual(self.after_removal["src/Makefile"], KLIPPER_MAKEFILE.encode())
        self.assertEqual(self.after_removal_links, [False, False])

    def test_migrated_tree_matches_a_fresh_install(self):
        """The same links and hooks a fresh install.sh run produces."""
        fresh_klipper = build_klipper_tree(self.root / "fresh")
        self._install_sh(fresh_klipper,
                         build_config_tree(self.root / "fresh"), "--with-mcu-addon")
        self.assertEqual(link_snapshot(self.klipper), link_snapshot(fresh_klipper))

    def test_migrated_links_point_into_the_openams_checkout(self):
        """No link is left pointing at the old klipper-mmu checkout."""
        snapshot = link_snapshot(self.klipper)
        self.assertEqual(snapshot["mcu add-on link"],
                         str((ROOT / "klipper_mmu").resolve()))
        self.assertEqual(snapshot["host extra link"],
                         str((ROOT / "klipper_mmu/host").resolve()))
        for name, target in snapshot.items():
            self.assertNotIn(str(self.old), target, name)

    def test_check_actions_report_the_migration(self):
        """The guide's verification commands return 0 for both check actions."""
        for action in ("check", "check-host"):
            result = self.run_cmd(*self._mmu_tool(ROOT / "klipper_mmu"), action,
                                  str(self.klipper), check=False)
            self.assertEqual(result.returncode, 0,
                             "%s: %s%s" % (action, result.stdout, result.stderr))

    def test_one_update_entry_covers_both_repositories(self):
        """Moonraker needs one [update_manager openams] entry and no other."""
        conf = (self.config / "moonraker.conf").read_text()
        self.assertEqual(conf.count("[update_manager openams]"), 1)
        self.assertNotIn("klipper-mmu", conf)
        self.assertIn("path: %s" % ROOT, conf)


if __name__ == "__main__":
    unittest.main()
