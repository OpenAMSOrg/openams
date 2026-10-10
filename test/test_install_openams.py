"""Installer tests for the openams entry-point and migration from klipper_openams.

Covers:
  - Fresh install via install.sh
  - install-openams.sh shim
  - Upgrade from a simulated klipper_openams install:
      - [update_manager openams] section rewritten when path/origin reference
        the old klipper_openams repository
      - Stale symlinks into a klipper_openams checkout that have no matching
        file in src/ or scripts/ are removed
      - Symlinks for files still present in src/ are re-pointed by the normal
        link step (not removed)
      - Symlinks that do not reference klipper_openams are never touched
  - Uninstall via install.sh
"""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "install_helpers/gco-routines.sh"
URL = "https://github.com/OpenAMSOrg/gco-routines.git"

INSTALLER = '''import argparse
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


class OpenAMSInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="openams-install-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.remote = self.root / "remote"
        self.checkout = self.root / "checkout"
        self.klipper = self.root / "klipper"
        self.config = self.root / "config"
        (self.klipper / "klippy/extras").mkdir(parents=True)
        (self.klipper / "scripts").mkdir()
        self.config.mkdir()
        (self.config / "moonraker.conf").write_text("[server]\n")
        (self.config / "printer.cfg").write_text("[printer]\nkinematics: none\n")
        self.original_macros = "# Existing printer tuning must survive\n[gcode_macro USER]\ngcode:\n    G4 P0\n"
        (self.config / "oams_macros.cfg").write_text(self.original_macros)
        (self.config / "oams.cfg").write_text("# existing OAMS config\n")
        git_config = self.root / "gitconfig"
        git_config.write_text(
            '[user]\nname = Installer Test\nemail = installer@example.invalid\n'
            '[url "file://%s"]\ninsteadOf = %s\n' % (self.remote, URL)
        )
        self.env = dict(
            os.environ,
            GIT_CONFIG_GLOBAL=str(git_config),
            GIT_CONFIG_NOSYSTEM="1",
            GIT_TERMINAL_PROMPT="0",
            GIT_ALLOW_PROTOCOL="file",
            GCO_ROUTINES_PATH=str(self.checkout),
            KLIPPER_PATH=str(self.klipper),
            MOONRAKER_PATH=str(self.root / "no-moonraker"),
            OPENAMS_SPOOLMAN_INFO_URL="file:///nonexistent/openams-spoolman",
            OPENAMS_SPOOLMAN_INSTALLER="/dev/null/none",
            # Never look at this machine's USB devices or /etc: a Cortex
            # Bridge plugged into it must not change what is tested.
            OPENAMS_SYSFS_USB=str(self.root / "no-usb"),
            OPENAMS_SYSTEM_ROOT=str(self.root / "sysroot"),
        )
        self.run_cmd("git", "init", "-b", "main", str(self.remote))
        self.write_remote("tools/install_gco_routines.py", INSTALLER)
        self.write_remote("klippy_extra/gco_routines/__init__.py", "# fixture\n")
        self.commit_remote("Initial fixture")

    def run_cmd(self, *args, check=True, **kwargs):
        return subprocess.run(args, env=self.env, text=True, capture_output=True,
                              check=check, timeout=20, **kwargs)

    def write_remote(self, name, text):
        path = self.remote / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def commit_remote(self, message):
        self.run_cmd("git", "-C", str(self.remote), "add", ".")
        self.run_cmd("git", "-C", str(self.remote), "commit", "-m", message)

    def fake_services(self):
        binary = self.root / "bin"
        binary.mkdir(exist_ok=True)
        sudo = binary / "sudo"
        sudo.write_text('''#!/bin/bash
printf '%s\\n' "$*" >> "$OPENAMS_TEST_SERVICE_LOG"
if [[ "$*" == *list-units* ]]; then
    echo "klipper-test.service loaded active running"
fi
''')
        sudo.chmod(0o755)
        self.service_log = self.root / "services.log"
        self.env.update(PATH=str(binary) + os.pathsep + self.env["PATH"],
                        OPENAMS_TEST_SERVICE_LOG=str(self.service_log))

    def install(self, *extra, answer="n", script="install.sh"):
        if os.geteuid() == 0:
            self.skipTest("The actual OpenAMS installer intentionally rejects root")
        return self.run_cmd("bash", str(ROOT / script),
                            "-k", str(self.klipper), "-c", str(self.config),
                            "-s", "klipper-test", "-r", str(self.checkout), *extra,
                            check=False, input=answer + "\n")

    # ------------------------------------------------------------------
    # Fresh install / shim
    # ------------------------------------------------------------------

    def test_fresh_install_via_install_sh(self):
        """install.sh installs cleanly on a fresh configuration."""
        self.fake_services()
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # gco-routines symlink created
        self.assertTrue((self.klipper / "klippy/extras/gco_routines").is_symlink())
        # OpenAMS Python sources linked
        for name in ["oams.py", "oams_manager.py"]:
            self.assertTrue((self.klipper / "klippy/extras" / name).is_symlink(), name)
        # update_manager section added
        conf = (self.config / "moonraker.conf").read_text()
        self.assertIn("[update_manager openams]", conf)
        self.assertIn("OpenAMSOrg/openams.git", conf)
        # klipper service stopped and started once each
        log = self.service_log.read_text()
        self.assertEqual(log.count("systemctl stop klipper-test"), 1)
        self.assertEqual(log.count("systemctl start klipper-test"), 1)

    def test_fresh_install_via_shim(self):
        """install-openams.sh shim delegates to install.sh."""
        self.fake_services()
        result = self.install(script="install-openams.sh")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.klipper / "klippy/extras/gco_routines").is_symlink())
        conf = (self.config / "moonraker.conf").read_text()
        self.assertIn("[update_manager openams]", conf)

    def test_uninstall_via_install_sh(self):
        """install.sh -u removes OpenAMS files (keeping gco-routines and configs when declined)."""
        self.fake_services()
        self.assertEqual(self.install().returncode, 0)
        result = self.install("-u", answer="n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # gco-routines checkout and symlink are independent; uninstall leaves them
        self.assertTrue((self.klipper / "klippy/extras/gco_routines").is_symlink())
        # OpenAMS sources removed
        for name in ["oams.py", "oams_manager.py"]:
            self.assertFalse((self.klipper / "klippy/extras" / name).exists(), name)

    # ------------------------------------------------------------------
    # Moonraker update_manager migration
    # ------------------------------------------------------------------

    def _write_old_updater(self, path_line="path: ~/klipper_openams",
                           origin_line="origin: https://github.com/openamsorg/klipper_openams.git"):
        """Append a klipper_openams-style [update_manager openams] to moonraker.conf."""
        with open(self.config / "moonraker.conf", "a") as f:
            f.write("\n[update_manager openams]\n")
            f.write("type: git_repo\n")
            f.write(path_line + "\n")
            f.write(origin_line + "\n")
            f.write("is_system_service: False\n")

    def test_migrate_updater_old_path_is_rewritten(self):
        """An [update_manager openams] with a klipper_openams path is rewritten."""
        self.fake_services()
        self._write_old_updater()
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        conf = (self.config / "moonraker.conf").read_text()
        # Section appears exactly once
        self.assertEqual(conf.count("[update_manager openams]"), 1)
        # Old repository references are gone
        self.assertNotIn("klipper_openams", conf)
        # New origin is present
        self.assertIn("OpenAMSOrg/openams.git", conf)
        # Deprecated key is gone
        self.assertNotIn("is_system_service", conf)

    def test_migrate_updater_idempotent_on_rerun(self):
        """Running the installer twice after migration does not duplicate the section."""
        self.fake_services()
        self._write_old_updater()
        self.assertEqual(self.install().returncode, 0)
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        conf = (self.config / "moonraker.conf").read_text()
        self.assertEqual(conf.count("[update_manager openams]"), 1)

    def test_migrate_updater_already_current_not_touched(self):
        """A section already referencing the new repository is left alone."""
        self.fake_services()
        # Pre-install so moonraker.conf gets a correct section
        self.assertEqual(self.install().returncode, 0)
        conf_before = (self.config / "moonraker.conf").read_text()
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.config / "moonraker.conf").read_text(), conf_before)

    def test_migrate_updater_output_shows_old_and_new(self):
        """Migration output shows both the old section and the replacement."""
        self.fake_services()
        self._write_old_updater()
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        combined = result.stdout + result.stderr
        self.assertIn("klipper_openams", combined)
        self.assertIn("openams.git", combined)

    # ------------------------------------------------------------------
    # Symlink migration
    # ------------------------------------------------------------------

    def _old_klipper_openams_dir(self):
        """Create a fake old klipper_openams directory with some source files."""
        old = self.root / "klipper_openams"
        (old / "src").mkdir(parents=True)
        (old / "scripts").mkdir()
        # openams_api.py: a file that existed in the old repo but is gone from src/
        (old / "src" / "openams_api.py").write_text("# old file\n")
        # oams.py: still present in src/; symlink should be re-pointed, not removed
        (old / "src" / "oams.py").write_text("# old oams\n")
        return old

    def test_stale_extras_symlink_for_removed_file_is_deleted(self):
        """A symlink into klipper_openams for a file no longer in src/ is removed."""
        self.fake_services()
        old = self._old_klipper_openams_dir()
        stale = self.klipper / "klippy/extras/openams_api.py"
        stale.symlink_to(old / "src/openams_api.py")
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(stale.exists(), "stale symlink should have been removed")
        self.assertFalse(stale.is_symlink(), "stale symlink should have been removed")

    def test_stale_scripts_symlink_for_removed_file_is_deleted(self):
        """A symlink in scripts/ into klipper_openams for a removed file is deleted."""
        self.fake_services()
        old = self._old_klipper_openams_dir()
        (old / "scripts" / "old_tool.py").write_text("# removed script\n")
        stale = self.klipper / "scripts/old_tool.py"
        stale.symlink_to(old / "scripts/old_tool.py")
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(stale.is_symlink(), "stale scripts symlink should be removed")

    def test_extras_symlink_for_active_src_file_is_repointed_not_removed(self):
        """A symlink in extras/ for a file still in src/ is re-pointed, not removed."""
        self.fake_services()
        old = self._old_klipper_openams_dir()
        # Point oams.py at the old klipper_openams checkout
        link = self.klipper / "klippy/extras/oams.py"
        link.symlink_to(old / "src/oams.py")
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # Symlink must now point to our src/, not the old location
        self.assertTrue(link.is_symlink(), "oams.py symlink should still exist")
        real_target = link.resolve()
        self.assertEqual(real_target, (ROOT / "src/oams.py").resolve())

    def test_foreign_extras_symlink_is_not_removed(self):
        """A symlink in extras/ that does NOT reference klipper_openams is left alone."""
        self.fake_services()
        unrelated = self.root / "other_project/extras"
        unrelated.mkdir(parents=True)
        (unrelated / "some_plugin.py").write_text("# unrelated plugin\n")
        foreign = self.klipper / "klippy/extras/some_plugin.py"
        foreign.symlink_to(unrelated / "some_plugin.py")
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # Foreign symlink must survive untouched
        self.assertTrue(foreign.is_symlink(), "foreign symlink should not be removed")
        self.assertEqual(Path(os.readlink(foreign)), unrelated / "some_plugin.py")


class ConfigRewriteSafetyTests(OpenAMSInstallerTests):
    """Config-file rewrite preserves mode, symlinks, and leaves no temp files."""

    def test_mode_preserved_on_rewrite(self):
        """A 0644 config file keeps its mode (not 0600) after the installer rewrites it."""
        self.fake_services()
        moonraker = self.config / "moonraker.conf"
        printer = self.config / "printer.cfg"
        moonraker.chmod(0o644)
        printer.chmod(0o644)
        self._write_old_updater()  # triggers migrate_updater which rewrites moonraker.conf
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(moonraker.stat().st_mode & 0o777, 0o644,
                         "moonraker.conf should keep 0644 after rewrite")
        self.assertEqual(printer.stat().st_mode & 0o777, 0o644,
                         "printer.cfg should keep 0644 after rewrite")
        # Verify content was actually updated
        self.assertIn("[update_manager openams]", moonraker.read_text())
        self.assertIn("[include oams.cfg]", printer.read_text())

    def test_symlink_preserved_on_rewrite(self):
        """Symlinked configs stay symlinks after install; their targets receive the new content."""
        self.fake_services()
        targets = self.root / "config_repo"
        targets.mkdir()
        moonraker_target = targets / "moonraker.conf"
        printer_target = targets / "printer.cfg"
        moonraker_target.write_text("[server]\n")
        printer_target.write_text("[printer]\nkinematics: none\n")
        # Replace config files with symlinks pointing to the targets dir
        (self.config / "moonraker.conf").unlink()
        (self.config / "moonraker.conf").symlink_to(moonraker_target)
        (self.config / "printer.cfg").unlink()
        (self.config / "printer.cfg").symlink_to(printer_target)
        # _write_old_updater opens via the symlink (follows it), so the target gets the append
        self._write_old_updater()
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # The config-dir entries must still be symlinks
        self.assertTrue((self.config / "moonraker.conf").is_symlink(),
                        "moonraker.conf should remain a symlink after rewrite")
        self.assertTrue((self.config / "printer.cfg").is_symlink(),
                        "printer.cfg should remain a symlink after rewrite")
        # The symlink targets should have received the new content
        self.assertIn("[update_manager openams]", moonraker_target.read_text())
        self.assertIn("[include oams.cfg]", printer_target.read_text())

    def test_no_temp_files_left_after_install(self):
        """The installer removes every temporary file it creates."""
        self.fake_services()
        self._write_old_updater()  # triggers migrate_updater rewrite of moonraker.conf
        tmpdir = self.root / "tmpdir"
        tmpdir.mkdir()
        self.env["TMPDIR"] = str(tmpdir)
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        leaked = list(tmpdir.iterdir())
        self.assertEqual(leaked, [], f"Temp files not cleaned up: {leaked}")


if __name__ == "__main__":
    unittest.main()
