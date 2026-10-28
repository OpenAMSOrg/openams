"""End-to-end tests for install.sh against fake Klipper, Moonraker and printer_data trees.

install.sh is run with OPENAMS_NO_SERVICE=1, so no systemd unit is touched and
the whole installer can be exercised without a printer.
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
TAGLIB_URL = "https://github.com/queengooborg/Bambu-Lab-RFID-Library.git"

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


class InstallShTests(unittest.TestCase):
    def setUp(self):
        if os.geteuid() == 0:
            self.skipTest("install.sh intentionally refuses to run as root")
        self.temp = tempfile.TemporaryDirectory(prefix="openams-install-sh-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.klipper = self.root / "klipper"
        self.moonraker = self.root / "moonraker"
        self.config = self.root / "printer_data/config"
        self.checkout = self.root / "gco-routines"
        self.remote = self.root / "gco-remote"
        self._build_klipper()
        self._build_moonraker()
        self._build_config()
        self.gitconfig = self.root / "gitconfig"
        self.taglib_remote = self.root / "taglib-remote"
        self.taglib = self.root / "taglib"
        self.gitconfig.write_text(
            '[user]\nname = Installer Test\nemail = installer@example.invalid\n'
            '[url "file://%s"]\ninsteadOf = %s\n'
            '[url "file://%s"]\ninsteadOf = %s\n'
            % (self.remote, GCO_URL, self.taglib_remote, TAGLIB_URL))
        self.env = dict(
            os.environ,
            OPENAMS_NO_SERVICE="1",
            OPENAMS_SPOOLMAN_INFO_URL="file:///nonexistent/openams-spoolman",
            MOONRAKER_PATH=str(self.moonraker),
            OPENAMS_TAGLIB_PATH=str(self.taglib),
            GIT_CONFIG_GLOBAL=str(self.gitconfig),
            GIT_CONFIG_NOSYSTEM="1",
            GIT_TERMINAL_PROMPT="0",
            GIT_ALLOW_PROTOCOL="file",
        )
        self._build_gco_remote()
        self._build_taglib_remote()

    def _build_klipper(self):
        (self.klipper / "klippy/extras").mkdir(parents=True)
        (self.klipper / "scripts").mkdir()
        (self.klipper / "src").mkdir()
        (self.klipper / "Makefile").write_text(KLIPPER_MAKEFILE)
        (self.klipper / "src/Kconfig").write_text(KLIPPER_KCONFIG)
        (self.klipper / "src/Makefile").write_text(KLIPPER_MAKEFILE)

    def _build_moonraker(self):
        (self.moonraker / "moonraker/components").mkdir(parents=True)
        (self.moonraker / "moonraker/__init__.py").write_text("# fixture\n")

    def _build_config(self):
        self.config.mkdir(parents=True)
        (self.config / "moonraker.conf").write_text("[server]\n")
        (self.config / "printer.cfg").write_text("[printer]\nkinematics: none\n")

    def _build_gco_remote(self):
        (self.remote / "tools").mkdir(parents=True)
        (self.remote / "klippy_extra/gco_routines").mkdir(parents=True)
        (self.remote / "tools/install_gco_routines.py").write_text(GCO_INSTALLER)
        (self.remote / "klippy_extra/gco_routines/__init__.py").write_text("# fixture\n")
        self.run_cmd("git", "init", "-b", "main", str(self.remote))
        self.run_cmd("git", "-C", str(self.remote), "add", ".")
        self.run_cmd("git", "-C", str(self.remote), "commit", "-m", "fixture")

    def _build_taglib_remote(self):
        (self.taglib_remote / "Bambu Lab").mkdir(parents=True)
        (self.taglib_remote / "Bambu Lab/README").write_text("fixture\n")
        self.run_cmd("git", "init", "-b", "main", str(self.taglib_remote))
        self.run_cmd("git", "-C", str(self.taglib_remote), "add", ".")
        self.run_cmd("git", "-C", str(self.taglib_remote), "commit", "-m", "fixture")

    def run_cmd(self, *args, **kwargs):
        return subprocess.run(args, env=self.env, text=True, capture_output=True,
                              timeout=120, **kwargs)

    def install(self, *extra, answer="n"):
        return self.run_cmd(
            "bash", str(INSTALLER),
            "-k", str(self.klipper), "-c", str(self.config),
            "-s", "klipper-test", "-r", str(self.checkout),
            *extra, check=False, input=answer + "\n")

    # ------------------------------------------------------------------
    # One installer: openams plus the klipper-mmu host extra
    # ------------------------------------------------------------------

    def test_host_extra_is_linked(self):
        """install.sh links the klipper-mmu host extra into klippy/extras."""
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        link = self.klipper / "klippy/extras/klipper_mmu"
        self.assertTrue(link.is_symlink(), "klippy/extras/klipper_mmu should be a symlink")
        self.assertEqual(link.resolve(), (ROOT / "klipper_mmu/host").resolve())

    def test_openams_links_are_created(self):
        """install.sh links the OpenAMS modules, scripts and Moonraker component."""
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for name in ("oams.py", "oams_manager.py", "fps.py"):
            self.assertTrue((self.klipper / "klippy/extras" / name).is_symlink(), name)
        for name in ("canbus_logger.py", "flash_bootloader.py"):
            self.assertTrue((self.klipper / "scripts" / name).is_symlink(), name)
        component = self.moonraker / "moonraker/components/openams_spoolman"
        self.assertEqual(component.resolve(), (ROOT / "moonraker/openams_spoolman").resolve())
        taglib = self.moonraker / "moonraker/components/openams_taglib"
        self.assertEqual(taglib.resolve(), (ROOT / "moonraker/openams_taglib").resolve())

    def test_no_service_action_is_taken(self):
        """OPENAMS_NO_SERVICE=1 reports each service action and runs none."""
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("[skipped: OPENAMS_NO_SERVICE=1] systemctl stop klipper-test", result.stdout)
        self.assertIn("[skipped: OPENAMS_NO_SERVICE=1] systemctl start klipper-test", result.stdout)
        self.assertIn("[skipped: OPENAMS_NO_SERVICE=1] systemctl restart moonraker", result.stdout)

    def configured_moonraker(self):
        """A moonraker.conf that already has every OpenAMS section, as on a
        printer whose OS was reinstalled with its old config restored."""
        (self.config / "moonraker.conf").write_text(
            "[server]\n\n"
            "[update_manager openams]\ntype: git_repo\npath: %s\n"
            "origin: https://github.com/OpenAMSOrg/openams.git\n\n"
            "[spoolman]\nserver: http://localhost:7912\n\n"
            "[openams_spoolman]\n" % ROOT)

    def test_a_new_component_link_restarts_moonraker(self):
        """Moonraker loads components at start: a new link must restart it even
        when moonraker.conf needs no change (otherwise spool editing stays off)."""
        self.configured_moonraker()
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(
            result.stdout.count("[skipped: OPENAMS_NO_SERVICE=1] systemctl restart moonraker"), 1,
            result.stdout)

    def test_an_unchanged_install_leaves_moonraker_alone(self):
        """Running the installer again with everything in place restarts nothing."""
        self.configured_moonraker()
        self.assertEqual(self.install().returncode, 0)
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("systemctl restart moonraker", result.stdout)

    def test_taglib_section_is_added_once(self):
        """[openams_taglib] goes into moonraker.conf only when absent."""
        self.configured_moonraker()
        self.assertEqual(self.install().returncode, 0)
        conf = (self.config / "moonraker.conf").read_text()
        self.assertEqual(conf.count("[openams_taglib]"), 1)
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual((self.config / "moonraker.conf").read_text(), conf)

    def test_uninstall_removes_the_taglib_link_and_section(self):
        """-u removes the openams_taglib link and section, and only those."""
        self.configured_moonraker()
        self.assertEqual(self.install().returncode, 0)
        result = self.install("-u")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        conf = (self.config / "moonraker.conf").read_text()
        self.assertNotIn("[openams_taglib]", conf)
        self.assertIn("[spoolman]", conf)
        components = self.moonraker / "moonraker/components"
        self.assertFalse((components / "openams_taglib").exists())
        self.assertFalse((components / "openams_spoolman").exists())

    UPDATER = "[update_manager Bambu-Lab-RFID-Library]"

    def conf_text(self):
        return (self.config / "moonraker.conf").read_text()

    def test_taglib_is_downloaded_and_added_to_the_update_manager(self):
        """A missing library is cloned, then its update manager entry is added."""
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.taglib / ".git").is_dir())
        self.assertIn("about 200 MB", result.stdout)
        conf = self.conf_text()
        self.assertEqual(conf.count(self.UPDATER), 1)
        self.assertIn(
            "%s\ntype: git_repo\npath: %s\n"
            "origin: https://github.com/queengooborg/Bambu-Lab-RFID-Library.git\n"
            "primary_branch: main\nis_system_service: False\n"
            % (self.UPDATER, self.taglib), conf)

    def test_taglib_path_option_is_the_checkout(self):
        """path: in [openams_taglib] names the checkout, ~ expanded."""
        (self.config / "moonraker.conf").write_text(
            "[server]\n\n[openams_taglib]\npath: %s\n" % self.taglib)
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.taglib / ".git").is_dir())
        self.assertIn("path: %s\n" % self.taglib, self.conf_text())

    def test_an_existing_checkout_is_not_cloned_again(self):
        self.taglib.mkdir()
        (self.taglib / "mine").write_text("x")
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.taglib / ".git").exists())
        self.assertIn(self.UPDATER, self.conf_text())

    def test_no_taglib_download_adds_no_update_manager_entry(self):
        result = self.install("--no-taglib-download")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.taglib.exists())
        self.assertNotIn(self.UPDATER, self.conf_text())
        self.assertIn("[openams_taglib]", self.conf_text())

    def test_a_failed_taglib_download_is_only_a_warning(self):
        shutil.rmtree(self.taglib_remote)
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("[WARNING] could not download it", result.stdout)
        self.assertFalse(self.taglib.exists())
        self.assertNotIn(self.UPDATER, self.conf_text())
        # online again: the next run downloads it and adds the entry
        self._build_taglib_remote()
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(self.conf_text().count(self.UPDATER), 1)

    def test_taglib_update_manager_entry_is_added_once(self):
        self.assertEqual(self.install().returncode, 0)
        first = self.conf_text()
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.conf_text(), first)
        self.assertNotIn("systemctl restart moonraker", result.stdout)

    def test_uninstall_removes_the_update_manager_entry_and_keeps_the_checkout(self):
        self.assertEqual(self.install().returncode, 0)
        result = self.install("-u")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        conf = self.conf_text()
        self.assertNotIn(self.UPDATER, conf)
        self.assertNotIn("[openams_taglib]", conf)
        self.assertTrue((self.taglib / ".git").is_dir())
        self.assertIn("checkout was left in place", result.stdout)

    # ------------------------------------------------------------------
    # The MCU add-on is opt-in
    # ------------------------------------------------------------------

    def test_mcu_addon_is_not_installed_by_default(self):
        """Without --with-mcu-addon, Klipper's src/ is untouched."""
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.klipper / "src/klipper_mmu").exists())
        self.assertEqual((self.klipper / "src/Kconfig").read_text(), KLIPPER_KCONFIG)
        self.assertEqual((self.klipper / "src/Makefile").read_text(), KLIPPER_MAKEFILE)
        self.assertNotIn("BEGIN klipper-mmu add-on", result.stdout)

    def test_with_mcu_addon_adds_the_source_hooks(self):
        """--with-mcu-addon adds the marked hooks and the src/klipper_mmu link."""
        result = self.install("--with-mcu-addon")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        kconfig = (self.klipper / "src/Kconfig").read_text()
        self.assertIn("# BEGIN klipper-mmu add-on", kconfig)
        self.assertIn('osource "src/klipper_mmu/mcu/Kconfig"', kconfig)
        self.assertIn("# END klipper-mmu add-on", kconfig)
        self.assertTrue(kconfig.startswith(KLIPPER_KCONFIG), "upstream Kconfig text must survive")
        makefile = (self.klipper / "src/Makefile").read_text()
        self.assertIn("# BEGIN klipper-mmu add-on", makefile)
        self.assertIn("-include src/klipper_mmu/mcu/Makefile", makefile)
        self.assertTrue(makefile.startswith(KLIPPER_MAKEFILE), "upstream Makefile must survive")
        link = self.klipper / "src/klipper_mmu"
        self.assertTrue(link.is_symlink(), "src/klipper_mmu should be a symlink")
        self.assertEqual(link.resolve(), (ROOT / "klipper_mmu").resolve())

    def test_with_mcu_addon_explains_the_change_and_the_undo(self):
        """The output names the edited files and how to undo them."""
        result = self.install("--with-mcu-addon")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for expected in ("src/Kconfig", "src/Makefile", "install.sh -u"):
            self.assertIn(expected, result.stdout)

    # ------------------------------------------------------------------
    # Uninstall
    # ------------------------------------------------------------------

    def test_uninstall_removes_everything_it_installed(self):
        """-u removes the OpenAMS links, the host extra and the MCU add-on hooks."""
        self.assertEqual(self.install("--with-mcu-addon").returncode, 0)
        result = self.install("-u")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for name in ("oams.py", "oams_manager.py", "fps.py"):
            self.assertFalse((self.klipper / "klippy/extras" / name).is_symlink(), name)
        self.assertFalse((self.klipper / "klippy/extras/klipper_mmu").is_symlink())
        self.assertFalse((self.klipper / "klippy/extras/klipper_mmu").exists())
        self.assertFalse((self.klipper / "src/klipper_mmu").is_symlink())
        self.assertEqual((self.klipper / "src/Kconfig").read_text(), KLIPPER_KCONFIG)
        self.assertEqual((self.klipper / "src/Makefile").read_text(), KLIPPER_MAKEFILE)

    def test_uninstall_without_the_mcu_addon_still_cleans_up(self):
        """-u removes the host extra even when the add-on was never installed."""
        self.assertEqual(self.install().returncode, 0)
        result = self.install("-u")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.klipper / "klippy/extras/klipper_mmu").is_symlink())
        self.assertNotIn("[FAILED]", result.stdout)

    def test_reinstall_after_uninstall(self):
        """The installer is idempotent across an install/uninstall/install cycle."""
        self.assertEqual(self.install("--with-mcu-addon").returncode, 0)
        self.assertEqual(self.install("-u").returncode, 0)
        result = self.install("--with-mcu-addon")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.klipper / "klippy/extras/klipper_mmu").is_symlink())
        self.assertTrue((self.klipper / "src/klipper_mmu").is_symlink())
        self.assertEqual((self.config / "moonraker.conf").read_text().count("[update_manager openams]"), 1)

    # ------------------------------------------------------------------
    # The shipped macro files are linked, so updates take effect
    # ------------------------------------------------------------------

    MACROS = ("oams_macros.cfg", "oams_macros_ordered.cfg")

    def assert_linked(self, name):
        path = self.config / name
        self.assertTrue(path.is_symlink(), name)
        self.assertEqual(os.readlink(path), str(ROOT / name))

    def older_shipped(self, name):
        commits = subprocess.run(
            ["git", "-C", str(ROOT), "log", "--format=%H", "--", name],
            text=True, capture_output=True).stdout.split()
        for commit in reversed(commits):
            shown = subprocess.run(
                ["git", "-C", str(ROOT), "show", "%s:%s" % (commit, name)],
                capture_output=True)
            if shown.returncode == 0 and shown.stdout != (ROOT / name).read_bytes():
                return shown.stdout
        self.skipTest("no older shipped version of %s in this checkout" % name)

    def test_fresh_install_links_the_macros(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for name in self.MACROS:
            self.assert_linked(name)
        self.assertTrue((self.config / "oams.cfg").is_file())
        self.assertFalse((self.config / "oams.cfg").is_symlink())

    def test_identical_old_copy_becomes_a_link(self):
        for name in self.MACROS:
            shutil.copy(ROOT / name, self.config / name)
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for name in self.MACROS:
            self.assert_linked(name)
        self.assertEqual(list(self.config.glob("*.bak-*")), [])
        self.assertIn("unmodified copy", result.stdout)

    def test_copy_of_an_older_shipped_version_becomes_a_link(self):
        (self.config / "oams_macros.cfg").write_bytes(self.older_shipped("oams_macros.cfg"))
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_linked("oams_macros.cfg")
        self.assertEqual(list(self.config.glob("*.bak-*")), [])

    def test_customized_copy_is_backed_up_and_replaced(self):
        custom = (ROOT / "oams_macros.cfg").read_text() + "\n[gcode_macro MINE]\ngcode:\n  M117 mine\n"
        (self.config / "oams_macros.cfg").write_text(custom)
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_linked("oams_macros.cfg")
        backups = list(self.config.glob("oams_macros.cfg.bak-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), custom)
        self.assertIn("was customized", result.stdout)
        self.assertIn("_OAMS_HOOK_AFTER_CUT", result.stdout)
        self.assertIn("Customizing the toolchange", result.stdout)

    def test_a_link_elsewhere_is_replaced(self):
        other = self.root / "other.cfg"
        other.write_text("# other\n")
        (self.config / "oams_macros.cfg").symlink_to(other)
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_linked("oams_macros.cfg")
        self.assertEqual(other.read_text(), "# other\n")

    def test_macro_linking_is_idempotent(self):
        self.assertEqual(self.install().returncode, 0)
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for name in self.MACROS:
            self.assert_linked(name)
        self.assertEqual(list(self.config.glob("*.bak-*")), [])
        self.assertIn("already linked", result.stdout)

    def test_existing_oams_cfg_is_left_alone(self):
        (self.config / "oams.cfg").write_text("# mine\n")
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual((self.config / "oams.cfg").read_text(), "# mine\n")

    def test_uninstall_removes_the_macro_links_but_not_the_checkout_files(self):
        self.assertEqual(self.install().returncode, 0)
        result = self.install("-u", answer="y")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for name in self.MACROS:
            self.assertFalse(os.path.lexists(self.config / name), name)
            self.assertTrue((ROOT / name).is_file(), name)

    # ------------------------------------------------------------------
    # One update entry covers klipper_mmu/ too
    # ------------------------------------------------------------------

    def test_update_manager_entry_covers_the_whole_checkout(self):
        """A single [update_manager openams] entry covers the whole checkout, klipper_mmu/ included."""
        self.assertEqual(self.install().returncode, 0)
        conf = (self.config / "moonraker.conf").read_text()
        self.assertEqual(conf.count("[update_manager openams]"), 1)
        # No install_script: Moonraker must never run the installer (it uses sudo and restarts Klipper).
        self.assertNotIn("install_script", conf)
        self.assertIn("path: %s" % ROOT, conf)
        self.assertIn("managed_services: klipper", conf)
        self.assertIn("OpenAMSOrg/openams.git", conf)
        self.assertTrue((ROOT / "klipper_mmu").is_dir(), "the updated checkout holds klipper_mmu/")

    # ------------------------------------------------------------------
    # The klipper_mmu name must never be taken by an OpenAMS module
    # ------------------------------------------------------------------

    def test_existing_klipper_mmu_py_stops_the_install(self):
        """klippy/extras/klipper_mmu.py is refused before anything is linked."""
        shadow = self.klipper / "klippy/extras/klipper_mmu.py"
        shadow.write_text("# someone else's module\n")
        result = self.install()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("klipper_mmu.py", result.stdout + result.stderr)
        self.assertFalse((self.klipper / "klippy/extras/oams.py").is_symlink())
        self.assertFalse((self.klipper / "klippy/extras/klipper_mmu").is_symlink())

    def test_klipper_mmu_module_in_src_stops_the_install(self):
        """A src/klipper_mmu*.py module would collide, so the run is refused."""
        checkout = self.root / "openams-copy"
        checkout.mkdir()
        for name in ("install.sh", "file_templates", "install_helpers", "src", "scripts"):
            source = ROOT / name
            if source.is_dir():
                shutil.copytree(source, checkout / name, ignore=shutil.ignore_patterns("__pycache__"))
            else:
                shutil.copy2(source, checkout / name)
        (checkout / "src/klipper_mmu.py").write_text("# a module with a reserved name\n")
        result = self.run_cmd("bash", str(checkout / "install.sh"),
                              "-k", str(self.klipper), "-c", str(self.config),
                              "-s", "klipper-test", "-r", str(self.checkout),
                              check=False, input="n\n")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("collides with the klipper_mmu Klipper extra", result.stdout + result.stderr)
        self.assertFalse((self.klipper / "klippy/extras/klipper_mmu.py").is_symlink())
        self.assertFalse((self.klipper / "klippy/extras/oams.py").is_symlink())

    # ------------------------------------------------------------------
    # gco-routines is optional: a foreign install is left alone
    # ------------------------------------------------------------------
    def _legacy_layout(self):
        """Non-git ~/gco-routines plus an extras symlink into it."""
        (self.checkout / "gco_routines").mkdir(parents=True)
        (self.checkout / "gco_routines/__init__.py").write_text("# legacy\n")
        link = self.klipper / "klippy/extras/gco_routines"
        link.symlink_to(self.checkout / "gco_routines", target_is_directory=True)
        return link

    def _snapshot(self, link):
        files = sorted((str(p.relative_to(self.checkout)), p.read_text())
                       for p in self.checkout.rglob("*") if p.is_file())
        return files, os.readlink(link)

    def _openams_linked(self):
        return all((self.klipper / "klippy/extras" / n).is_symlink()
                   for n in ("oams.py", "oams_manager.py", "fps.py"))

    def test_legacy_gco_routines_is_left_alone_and_openams_installs(self):
        link = self._legacy_layout()
        before = self._snapshot(link)
        result = self.install()
        out = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, out)
        self.assertTrue(self._openams_linked())
        self.assertEqual(self._snapshot(link), before)
        self.assertFalse((self.checkout / ".git").exists())
        self.assertEqual(out.count("[WARNING] gco-routines: "), 2, out)
        self.assertIn("already exists and is not this checkout's symlink", out)
        self.assertIn("left untouched", out)

    def test_uninstall_never_touches_a_foreign_gco_routines(self):
        link = self._legacy_layout()
        before = self._snapshot(link)
        self.assertEqual(self.install().returncode, 0)
        result = self.install("-u")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self._openams_linked())
        self.assertEqual(self._snapshot(link), before)

    def test_skip_gco_routines_takes_no_gco_action(self):
        # An unreachable origin would fail a clone, so success proves no network.
        self.gitconfig.write_text("[user]\nname = x\nemail = x@example.invalid\n")
        result = self.install("--skip-gco-routines", "--no-taglib-download")
        out = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, out)
        self.assertTrue(self._openams_linked())
        self.assertFalse(self.checkout.exists())
        self.assertFalse((self.klipper / "klippy/extras/gco_routines").exists())
        self.assertNotIn("[WARNING]", out)

    def test_require_gco_routines_fails_on_legacy_before_any_change(self):
        link = self._legacy_layout()
        before = self._snapshot(link)
        result = self.install("--require-gco-routines")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("[ERROR] gco-routines:", result.stderr)
        self.assertFalse(self._openams_linked())
        self.assertFalse((self.klipper / "klippy/extras/klipper_mmu").exists())
        self.assertEqual(self._snapshot(link), before)

    def test_clone_failure_warns_and_continues(self):
        shutil.rmtree(self.remote)
        result = self.install()
        out = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, out)
        self.assertTrue(self._openams_linked())
        self.assertIn("could not clone", out)
        self.assertFalse((self.klipper / "klippy/extras/gco_routines").exists())

    def test_clone_failure_is_fatal_with_require(self):
        shutil.rmtree(self.remote)
        result = self.install("--require-gco-routines")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not clone", result.stderr)
        self.assertFalse(self._openams_linked())

    def test_fetch_failure_warns_and_continues(self):
        self.assertEqual(self.install().returncode, 0)
        shutil.rmtree(self.remote)
        result = self.install()
        out = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, out)
        self.assertIn("could not fetch", out)
        self.assertTrue(self._openams_linked())

    def test_skip_and_require_conflict(self):
        result = self.install("--skip-gco-routines", "--require-gco-routines")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self._openams_linked())

    # ------------------------------------------------------------------
    # --mainsail-panel: the OpenAMS Mainsail build
    # ------------------------------------------------------------------

    def _mainsail_fixture(self, stock=True, section=True):
        import json
        import zipfile
        self.mainsail = self.root / "mainsail"
        if stock:
            self.mainsail.mkdir()
            (self.mainsail / "index.html").write_text("stock\n")
            (self.mainsail / "config.json").write_text('{"instancesDB": "moonraker", "mine": 1}\n')
            (self.mainsail / "release_info.json").write_text(json.dumps(
                {"project_name": "mainsail", "project_owner": "mainsail-crew", "version": "v2.19.0"}))
        if section:
            with open(self.config / "moonraker.conf", "a") as conf:
                conf.write("\n[update_manager mainsail]\ntype: web\nchannel: stable\n"
                           "repo: mainsail-crew/mainsail\npath: ~/mainsail\n")
        release = self.root / "mainsail-release.zip"
        with zipfile.ZipFile(release, "w") as zf:
            zf.writestr("index.html", "openams\n")
            zf.writestr("config.json", '{"instancesDB": "moonraker"}\n')
            zf.writestr("release_info.json", json.dumps(
                {"project_name": "mainsail", "project_owner": "OpenAMSOrg", "version": "v2.19.0-openams.1"}))
        self.env.update(OPENAMS_MAINSAIL_PATH=str(self.mainsail),
                        OPENAMS_MAINSAIL_RELEASE_ZIP=str(release))

    def _mainsail_repo(self):
        text = (self.config / "moonraker.conf").read_text()
        section = text.split("[update_manager mainsail]", 1)[1].split("\n[", 1)[0]
        return [line.split(":", 1)[1].strip() for line in section.splitlines()
                if line.startswith("repo:")]

    def test_mainsail_panel_installs_and_keeps_the_stock_copy(self):
        self._mainsail_fixture()
        result = self.install("--mainsail-panel")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.mainsail / "index.html").read_text(), "openams\n")
        self.assertIn('"mine": 1', (self.mainsail / "config.json").read_text())
        self.assertEqual((self.root / "mainsail.stock/index.html").read_text(), "stock\n")
        self.assertEqual(self._mainsail_repo(), ["OpenAMSOrg/mainsail"])
        # A second run updates in place and keeps the one stock copy.
        result = self.install("--mainsail-panel")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.root / "mainsail.stock/index.html").read_text(), "stock\n")
        self.assertEqual(self._mainsail_repo(), ["OpenAMSOrg/mainsail"])

    def test_uninstall_restores_the_stock_mainsail(self):
        self._mainsail_fixture()
        self.assertEqual(self.install("--mainsail-panel").returncode, 0)
        result = self.install("-u")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.mainsail / "index.html").read_text(), "stock\n")
        self.assertFalse((self.root / "mainsail.stock").exists())
        self.assertEqual(self._mainsail_repo(), ["mainsail-crew/mainsail"])

    def test_mainsail_panel_adds_the_updater_when_missing(self):
        self._mainsail_fixture(stock=False, section=False)
        result = self.install("--mainsail-panel")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.mainsail / "index.html").read_text(), "openams\n")
        self.assertEqual(self._mainsail_repo(), ["OpenAMSOrg/mainsail"])

    def test_mainsail_panel_leaves_an_own_build_alone(self):
        self._mainsail_fixture(stock=False)
        own = self.root / "my-mainsail"
        own.mkdir()
        (own / "index.html").write_text("mine\n")
        self.mainsail.symlink_to(own, target_is_directory=True)
        result = self.install("--mainsail-panel")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("left alone", result.stdout)
        self.assertEqual((own / "index.html").read_text(), "mine\n")
        self.assertEqual(self._mainsail_repo(), ["mainsail-crew/mainsail"])

    def test_without_the_flag_mainsail_is_untouched(self):
        self._mainsail_fixture()
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.mainsail / "index.html").read_text(), "stock\n")
        self.assertEqual(self._mainsail_repo(), ["mainsail-crew/mainsail"])


if __name__ == "__main__":
    unittest.main()
