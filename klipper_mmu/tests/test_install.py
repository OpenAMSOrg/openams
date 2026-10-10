# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise preservation and failure behavior on disposable source checkouts."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("installer", Path(__file__).parents[1] / "tools/install.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class InstallationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "Klipper checkout"
        self.src = self.root / "src"
        self.src.mkdir(parents=True)
        (self.root / "klippy").mkdir()
        (self.root / "Makefile").write_text("all:\n")
        self.original = {"Kconfig": b'mainmenu "Example"\n# user settings\n',
                         "Makefile": b"src-y += existing.c\n# no final newline"}
        for name, data in self.original.items():
            (self.src / name).write_bytes(data)
            (self.src / name).chmod(0o640)
        self.package = self.base / "MMU package"
        (self.package / "mcu").mkdir(parents=True)
        for name in installer.REQUIRED_MCU_FILES:
            (self.package / "mcu" / name).write_text("# package\n")
        self.link = self.src / "klipper_mmu"

    def run_action(self, action):
        return installer.integrate(self.root, action, self.package)

    def test_missing_menus_refused_before_install_and_after_update(self):
        for name in ("Kconfig.devices", "Kconfig.control", "Kconfig.pins", "Kconfig.feeder", "Kconfig.tmc", "Kconfig.switch", "Kconfig.rewind", "Kconfig.adc"):
            with self.subTest(name=name):
                menu = self.package / "mcu" / name
                menu.unlink()
                with self.assertRaisesRegex(RuntimeError, name):
                    self.run_action("install")
                self.assert_original()
                menu.write_text("# restored menu\n")
                self.run_action("install")
                menu.unlink()
                with self.assertRaisesRegex(RuntimeError, "incomplete"):
                    self.run_action("check")
                self.assertEqual(self.run_action("remove"), "absent")
                self.assert_original()
                menu.write_text("# restored menu\n")

    def assert_original(self):
        for name, data in self.original.items():
            self.assertEqual((self.src / name).read_bytes(), data)
            self.assertEqual((self.src / name).stat().st_mode & 0o777, 0o640)
        self.assertFalse(os.path.lexists(self.link))
        self.assertEqual(sorted(p.name for p in self.src.iterdir()), sorted(self.original))

    def test_roundtrip_idempotence_and_user_edits(self):
        self.assertEqual(self.run_action("check"), "absent")
        self.assertEqual(self.run_action("install"), "installed")
        timestamps = {p.name: p.lstat().st_mtime_ns for p in self.src.iterdir()}
        self.assertEqual(self.run_action("install"), "installed")
        self.assertEqual(timestamps, {p.name: p.lstat().st_mtime_ns for p in self.src.iterdir()})
        self.assertEqual(self.run_action("check"), "installed")
        self.assertEqual(self.link.resolve(), self.package)
        for name in self.original:
            extra = b"\n# Added by user after installation\n"
            with (self.src / name).open("ab") as output:
                output.write(extra)
            self.original[name] += extra
        self.assertEqual(self.run_action("remove"), "absent")
        self.assertEqual(self.run_action("remove"), "absent")
        self.assert_original()

    def test_source_updates_visible_without_copy(self):
        self.run_action("install")
        new = self.package / "mcu/clock.c"
        new.write_text("// updated source\n")
        self.assertEqual((self.link / "mcu/clock.c").read_bytes(), new.read_bytes())

    def test_invalid_second_hook_leaves_first_untouched(self):
        for bad in [installer.BEGIN, installer.END.rstrip(), installer.HOOKS["Makefile"] * 2,
                    installer.HOOKS["Makefile"].replace(b"include ", b"changed ")]:
            with self.subTest(bad=bad):
                (self.src / "Makefile").write_bytes(bad)
                with self.assertRaises(RuntimeError):
                    self.run_action("install")
                self.assertEqual((self.src / "Kconfig").read_bytes(), self.original["Kconfig"])
                self.assertEqual((self.src / "Makefile").read_bytes(), bad)
                self.assertFalse(os.path.lexists(self.link))

    def test_foreign_directory_and_dangling_symlink_refused(self):
        self.link.mkdir()
        with self.assertRaises(RuntimeError):
            self.run_action("install")
        self.link.rmdir()
        self.link.symlink_to(self.base / "missing-other-package")
        for action in ["install", "remove", "check"]:
            with self.assertRaises(RuntimeError):
                self.run_action(action)
        self.assertTrue(self.link.is_symlink())
        self.assertEqual((self.src / "Kconfig").read_bytes(), self.original["Kconfig"])

    def test_symlink_or_hardlink_hook_refused(self):
        target = self.base / "outside"
        target.write_text("preserve\n")
        hook = self.src / "Makefile"
        hook.unlink()
        hook.symlink_to(target)
        with self.assertRaises(RuntimeError):
            self.run_action("install")
        hook.unlink()
        os.link(target, hook)
        with self.assertRaises(RuntimeError):
            self.run_action("install")
        self.assertEqual(target.read_text(), "preserve\n")
        self.assertFalse(os.path.lexists(self.link))

    def test_partial_install_can_be_repaired_or_removed(self):
        hook = self.src / "Kconfig"
        hook.write_bytes(self.original["Kconfig"] + installer.HOOKS["Kconfig"])
        with self.assertRaises(RuntimeError):
            self.run_action("check")
        self.run_action("install")
        self.assertEqual(self.run_action("check"), "installed")
        self.link.unlink()
        with self.assertRaises(RuntimeError):
            self.run_action("check")
        self.run_action("remove")
        self.assert_original()

    def test_incomplete_package_rejected_before_mutation(self):
        (self.package / "mcu/Kconfig").unlink()
        with self.assertRaises(RuntimeError):
            self.run_action("install")
        self.assert_original()

    def test_unmanaged_reference_refused(self):
        manual = b'\nsource "src/klipper_mmu/mcu/Kconfig"\n'
        (self.src / "Kconfig").write_bytes(self.original["Kconfig"] + manual)
        with self.assertRaisesRegex(RuntimeError, "Unmanaged"):
            self.run_action("install")
        self.assertEqual((self.src / "Kconfig").read_bytes(), self.original["Kconfig"] + manual)
        self.assertFalse(os.path.lexists(self.link))

    def test_missing_installed_package_can_still_be_removed(self):
        self.run_action("install")
        (self.package / "mcu/Kconfig").unlink()
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            self.run_action("check")
        self.run_action("remove")
        self.assert_original()

    def test_experimental_hooks_refused(self):
        (self.src / "Makefile").write_bytes(b"# BEGIN klipper-mmu integration experiment\n")
        with self.assertRaises(RuntimeError):
            self.run_action("install")
        self.assertEqual((self.src / "Kconfig").read_bytes(), self.original["Kconfig"])
        self.assertFalse(os.path.lexists(self.link))

    def test_failed_install_rolls_back_hooks_and_link(self):
        original = installer.replace_file
        count = 0

        def fail_second(*args):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("injected write failure")
            original(*args)

        with patch.object(installer, "replace_file", fail_second):
            with self.assertRaisesRegex(OSError, "injected"):
                self.run_action("install")
        self.assert_original()

    def test_failed_remove_restores_installed_state(self):
        self.run_action("install")
        original = installer.replace_file
        count = 0

        def fail_second(*args):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("injected removal failure")
            original(*args)

        with patch.object(installer, "replace_file", fail_second):
            with self.assertRaisesRegex(OSError, "injected"):
                self.run_action("remove")
        self.assertEqual(self.run_action("check"), "installed")

    def test_concurrent_edit_not_overwritten_by_rollback(self):
        original = installer.replace_file
        count = 0

        def conflict(*args):
            nonlocal count
            count += 1
            if count == 2:
                (self.src / "Kconfig").write_text("concurrent user edit\n")
                raise OSError("injected failure")
            original(*args)

        with patch.object(installer, "replace_file", conflict):
            with self.assertRaisesRegex(RuntimeError, "rollback incomplete"):
                self.run_action("install")
        self.assertEqual((self.src / "Kconfig").read_text(), "concurrent user edit\n")
        self.assertFalse(os.path.lexists(self.link))

    def test_atomic_write_failure_leaves_no_temporary_file(self):
        with patch.object(installer.os, "replace", side_effect=OSError("injected rename failure")):
            with self.assertRaises(OSError):
                self.run_action("install")
        self.assert_original()

    def test_install_and_remove_preserve_source_extended_attributes(self):
        hook = self.src / "Kconfig"
        attribute = "user.klipper_mmu_fixture"
        try:
            os.setxattr(hook, attribute, b"keep")
        except OSError as error:
            self.skipTest(f"extended attributes unavailable: {error}")
        self.run_action("install")
        self.assertEqual(os.getxattr(hook, attribute), b"keep")
        self.run_action("remove")
        self.assertEqual(os.getxattr(hook, attribute), b"keep")
        self.assert_original()

    def test_hooks_are_optional_includes(self):
        self.run_action("install")
        kconfig = (self.src / "Kconfig").read_bytes()
        makefile = (self.src / "Makefile").read_bytes()
        self.assertEqual(kconfig, self.original["Kconfig"] + installer.HOOKS["Kconfig"])
        self.assertEqual(makefile, self.original["Makefile"] + installer.HOOKS["Makefile"])
        self.assertIn(b'\nosource "src/klipper_mmu/mcu/Kconfig"\n', kconfig)
        self.assertIn(b"\n-include src/klipper_mmu/mcu/Makefile\n", makefile)
        self.assertNotIn(b'\nsource "src/klipper_mmu/', kconfig)
        self.assertNotIn(b"\ninclude src/klipper_mmu/", makefile)

    def test_optional_makefile_hook_tolerates_a_missing_package(self):
        # A moved or deleted package must not stop builds from this checkout.
        self.run_action("install")
        self.link.unlink()
        self.link.symlink_to(self.base / "moved package")
        (self.src / "Makefile").write_bytes(
            b"all:\n\t@echo stock\n" + installer.HOOKS["Makefile"])
        env = {key: value for key, value in os.environ.items()
               if key not in ("MAKEFLAGS", "MAKELEVEL", "MFLAGS")}
        result = installer.subprocess.run(["make", "--no-print-directory", "-f", "src/Makefile"],
                                          cwd=self.root, env=env, capture_output=True,
                                          text=True, timeout=10)
        self.assertEqual((result.returncode, result.stdout), (0, "stock\n"), result.stderr)

    def test_old_mandatory_hooks_upgraded_in_place_or_removed(self):
        tail = b"\n# upstream text after the hook\n"
        for action in ("install", "remove"):
            with self.subTest(action=action):
                for name, data in self.original.items():
                    (self.src / name).write_bytes(data + installer.OLD_HOOKS[name] + tail)
                self.link.symlink_to(self.package, target_is_directory=True)
                with self.assertRaisesRegex(RuntimeError, "Earlier mandatory add-on hooks"):
                    self.run_action("check")
                self.assertEqual(self.run_action(action), "installed" if action == "install" else "absent")
                for name, data in self.original.items():
                    block = installer.HOOKS[name] if action == "install" else b""
                    self.assertEqual((self.src / name).read_bytes(), data + block + tail)
                    self.assertEqual((self.src / name).stat().st_mode & 0o777, 0o640)
                if action == "install":
                    self.assertEqual(self.run_action("check"), "installed")
                    self.assertEqual(self.run_action("remove"), "absent")
                for name, data in self.original.items():
                    self.assertEqual((self.src / name).read_bytes(), data + tail)
                    (self.src / name).write_bytes(data)
                self.assert_original()

    def test_mixed_old_and_current_hooks_are_upgraded(self):
        (self.src / "Kconfig").write_bytes(self.original["Kconfig"] + installer.OLD_HOOKS["Kconfig"])
        (self.src / "Makefile").write_bytes(self.original["Makefile"] + installer.HOOKS["Makefile"])
        self.link.symlink_to(self.package, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "Earlier mandatory add-on hooks"):
            self.run_action("check")
        self.assertEqual(self.run_action("install"), "installed")
        self.assertEqual(self.run_action("check"), "installed")
        self.assertEqual(self.run_action("remove"), "absent")
        self.assert_original()

    def test_both_hook_generations_in_one_file_refused(self):
        both = self.original["Kconfig"] + installer.OLD_HOOKS["Kconfig"] + installer.HOOKS["Kconfig"]
        (self.src / "Kconfig").write_bytes(both)
        for action in ("install", "remove", "check"):
            with self.assertRaisesRegex(RuntimeError, "Altered or duplicate"):
                self.run_action(action)
        self.assertEqual((self.src / "Kconfig").read_bytes(), both)
        self.assertFalse(os.path.lexists(self.link))

    def test_parallel_installer_lock(self):
        descriptor = os.open(self.src, os.O_RDONLY | os.O_DIRECTORY)
        try:
            installer.fcntl.flock(descriptor, installer.fcntl.LOCK_EX | installer.fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                self.run_action("install")
        finally:
            os.close(descriptor)
        self.assert_original()


if __name__ == "__main__":
    unittest.main()
