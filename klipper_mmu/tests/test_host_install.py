# SPDX-License-Identifier: GPL-3.0-or-later
"""Bounded checks for the optional read-only host extra symlink."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "installer", Path(__file__).parents[1] / "tools/install.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class HostInstallationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "Klipper checkout"
        self.klippy = self.root / "klippy"
        self.extras = self.klippy / "extras"
        self.src = self.root / "src"
        self.extras.mkdir(parents=True)
        self.src.mkdir()
        self.makefile = self.root / "Makefile"
        self.makefile.write_bytes(b"all:\n")
        self.config = self.root / ".config"
        self.config.write_bytes(b"CONFIG_EXAMPLE=y\n")
        self.printer = self.root / "printer.cfg"
        self.printer.write_bytes(b"[printer]\nkinematics: none\n")
        self.src_makefile = self.src / "Makefile"
        self.src_kconfig = self.src / "Kconfig"
        self.src_makefile.write_bytes(b"src-y += existing.c\n")
        self.src_kconfig.write_bytes(b"mainmenu \"Example\"\n")
        self.unrelated = self.extras / "pause_resume.py"
        self.unrelated.write_bytes(b"# unrelated extra\n")
        self.package = self.base / "MMU package"
        self.host = self.package / "host"
        self.host.mkdir(parents=True)
        for name in installer.REQUIRED_HOST_FILES:
            (self.host / name).write_text("# %s\n" % name)
        self.link = self.extras / "klipper_mmu"
        self.preserved = (
            self.makefile, self.config, self.printer, self.src_makefile,
            self.src_kconfig, self.unrelated)
        self.before = {path: path.read_bytes() for path in self.preserved}

    def run_action(self, action, root=None):
        return installer.integrate_host(self.root if root is None else root, action, self.package)

    def assert_preserved(self):
        for path, data in self.before.items():
            self.assertEqual(path.read_bytes(), data)
        self.assertFalse((self.src / "klipper_mmu").exists())
        self.assertFalse(os.path.lexists(self.src / "klipper_mmu"))

    def test_roundtrip_idempotence_target_and_visible_updates(self):
        self.assertEqual(self.run_action("check"), "absent")
        self.assertEqual(self.run_action("install"), "installed")
        self.assertTrue(self.link.is_symlink())
        self.assertEqual(self.link.resolve(), self.host.resolve())
        self.assertEqual((self.link / "protocol.py").read_text(), "# protocol.py\n")
        stamp = self.link.lstat().st_mtime_ns
        self.assertEqual(self.run_action("install"), "installed")
        self.assertEqual(self.link.lstat().st_mtime_ns, stamp)
        self.assertEqual(self.run_action("check"), "installed")
        updated = self.host / "requests.py"
        updated.write_text("# visible through the link\n")
        self.assertEqual((self.link / "requests.py").read_text(), updated.read_text())
        self.assert_preserved()
        self.assertEqual(self.run_action("remove"), "absent")
        self.assertEqual(self.run_action("remove"), "absent")
        self.assertFalse(os.path.lexists(self.link))
        self.assertEqual(updated.read_text(), "# visible through the link\n")
        self.assert_preserved()

    def test_user_files_remain_byte_for_byte(self):
        self.run_action("install")
        self.assertEqual(self.run_action("check"), "installed")
        self.run_action("remove")
        self.assert_preserved()

    def test_foreign_destinations_preserved_for_every_action(self):
        cases = (
            ("directory", lambda: self.link.mkdir() or (self.link / "keep.txt").write_text("keep")),
            ("file", lambda: self.link.write_text("foreign file\n")),
            ("link", lambda: self.link.symlink_to(self.package)),
            ("dangling", lambda: self.link.symlink_to(self.base / "missing other package")),
        )

        def clear_link():
            if self.link.is_symlink() or self.link.is_file():
                self.link.unlink()
            elif self.link.is_dir():
                for child in self.link.iterdir():
                    child.unlink()
                self.link.rmdir()

        for kind, plant in cases:
            with self.subTest(kind=kind):
                plant()
                try:
                    seen = os.readlink(self.link) if self.link.is_symlink() else None
                    body = self.link.read_bytes() if kind == "file" else None
                    for action in ("install", "remove", "check"):
                        with self.assertRaises(RuntimeError):
                            self.run_action(action)
                    self.assertTrue(os.path.lexists(self.link))
                    if seen is not None:
                        self.assertEqual(os.readlink(self.link), seen)
                    if body is not None:
                        self.assertEqual(self.link.read_bytes(), body)
                    if kind == "directory":
                        self.assertEqual((self.link / "keep.txt").read_text(), "keep")
                    self.assert_preserved()
                finally:
                    clear_link()

    def test_py_shadow_preserved_for_every_action(self):
        shadow = self.extras / "klipper_mmu.py"
        real = self.base / "real shadow.py"
        real.write_bytes(b"module body\n")

        def as_file():
            shadow.write_bytes(b"shadow file\n")

        def as_dir():
            shadow.mkdir()
            (shadow / "keep.txt").write_text("keep")

        def as_link():
            shadow.symlink_to(real)

        def as_dangling():
            shadow.symlink_to(self.base / "missing shadow.py")

        def clear():
            if shadow.is_symlink() or shadow.is_file():
                shadow.unlink()
            elif shadow.is_dir():
                for child in shadow.iterdir():
                    child.unlink()
                shadow.rmdir()

        for kind, plant in (("file", as_file), ("directory", as_dir), ("link", as_link), ("dangling", as_dangling)):
            with self.subTest(kind=kind):
                plant()
                marker = os.readlink(shadow) if shadow.is_symlink() else None
                for action in ("install", "remove", "check"):
                    with self.assertRaises(RuntimeError):
                        self.run_action(action)
                self.assertTrue(os.path.lexists(shadow))
                self.assertFalse(os.path.lexists(self.link))
                if marker is not None:
                    self.assertEqual(os.readlink(shadow), marker)
                if kind == "file":
                    self.assertEqual(shadow.read_bytes(), b"shadow file\n")
                if kind == "directory":
                    self.assertEqual((shadow / "keep.txt").read_text(), "keep")
                if kind == "link":
                    self.assertEqual(real.read_bytes(), b"module body\n")
                self.assert_preserved()
                clear()

    def test_missing_package_files_before_install(self):
        for name in installer.REQUIRED_HOST_FILES:
            with self.subTest(name=name):
                path = self.host / name
                path.unlink()
                with self.assertRaises(RuntimeError):
                    self.run_action("install")
                self.assertFalse(os.path.lexists(self.link))
                self.assert_preserved()
                path.write_text("# restored\n")

    def test_incomplete_and_dangling_package_after_install(self):
        self.run_action("install")
        protocol = self.host / "protocol.py"
        protocol.unlink()
        with self.assertRaises(RuntimeError):
            self.run_action("check")
        self.assertTrue(self.link.is_symlink())
        self.assertEqual(self.run_action("remove"), "absent")
        self.assertFalse(os.path.lexists(self.link))
        protocol.write_text("# protocol.py\n")
        self.run_action("install")
        for name in installer.REQUIRED_HOST_FILES:
            (self.host / name).unlink()
        self.host.rmdir()
        with self.assertRaises(RuntimeError):
            self.run_action("check")
        self.assertTrue(self.link.is_symlink())
        self.assertFalse(self.link.exists())
        self.assertEqual(self.run_action("remove"), "absent")
        self.assertFalse(os.path.lexists(self.link))
        self.assert_preserved()

    def test_absent_check_and_remove_without_package(self):
        for name in installer.REQUIRED_HOST_FILES:
            (self.host / name).unlink()
        self.host.rmdir()
        self.package.rmdir()
        self.assertEqual(self.run_action("check"), "absent")
        self.assertEqual(self.run_action("remove"), "absent")
        self.assertFalse(os.path.lexists(self.link))
        self.assert_preserved()

    def test_unknown_action_before_mutation(self):
        missing = self.base / "no such checkout"
        with self.assertRaises(ValueError):
            installer.integrate_host(missing, "explode", self.package)
        self.assertFalse(missing.exists())
        self.assertFalse(os.path.lexists(self.link))
        self.assert_preserved()

    def test_missing_or_symlinked_parents_write_nothing_outside(self):
        self.unrelated.unlink()
        self.extras.rmdir()
        with self.assertRaises(RuntimeError):
            self.run_action("install")
        self.assertFalse(self.extras.exists())
        self.extras.mkdir()
        self.unrelated.write_bytes(self.before[self.unrelated])

        self.makefile.unlink()
        with self.assertRaises(RuntimeError):
            self.run_action("install")
        self.assertFalse(os.path.lexists(self.link))
        self.makefile.write_bytes(self.before[self.makefile])

        outside = self.base / "outside extras"
        outside.mkdir()
        canary = outside / "keep.py"
        canary.write_bytes(b"keep\n")
        selected = self.base / "Selected checkout"
        selected.mkdir()
        (selected / "Makefile").write_bytes(b"all:\n")
        (selected / "klippy").symlink_to(self.klippy)
        # Point the selected klippy at a tree that lives outside that checkout.
        outside_klippy = self.base / "outside klippy"
        outside_extras = outside_klippy / "extras"
        outside_extras.mkdir(parents=True)
        (outside_extras / "keep.py").write_bytes(b"keep\n")
        (selected / "klippy").unlink()
        (selected / "klippy").symlink_to(outside_klippy)
        with self.assertRaises(RuntimeError):
            installer.integrate_host(selected, "install", self.package)
        self.assertEqual(sorted(p.name for p in outside_extras.iterdir()), ["keep.py"])
        self.assertEqual((outside_extras / "keep.py").read_bytes(), b"keep\n")

        tree = self.base / "Tree checkout"
        (tree / "klippy").mkdir(parents=True)
        (tree / "Makefile").write_bytes(b"all:\n")
        (tree / "klippy" / "extras").symlink_to(outside)
        with self.assertRaises(RuntimeError):
            installer.integrate_host(tree, "install", self.package)
        self.assertEqual(list(outside.iterdir()), [canary])
        self.assertEqual(canary.read_bytes(), b"keep\n")
        self.assertFalse(os.path.lexists(self.link))
        self.assert_preserved()

    def test_lock_contention_preserves_checkout(self):
        descriptor = os.open(self.extras, os.O_RDONLY | os.O_DIRECTORY)
        try:
            installer.fcntl.flock(descriptor, installer.fcntl.LOCK_EX | installer.fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                self.run_action("install")
        finally:
            os.close(descriptor)
        self.assertFalse(os.path.lexists(self.link))
        self.assert_preserved()

    def test_injected_link_errors_preserve_other_files(self):
        with patch.object(Path, "symlink_to", side_effect=OSError("injected link")):
            with self.assertRaises(OSError):
                self.run_action("install")
        self.assertFalse(os.path.lexists(self.link))
        self.assert_preserved()
        self.run_action("install")
        with patch.object(Path, "unlink", side_effect=OSError("injected unlink")):
            with self.assertRaises(OSError):
                self.run_action("remove")
        self.assertTrue(self.link.is_symlink())
        self.assertEqual(self.link.resolve(), self.host.resolve())
        self.assert_preserved()

    def test_cli_dispatch_and_exit_codes(self):
        cases = (
            ("install", "integrate", "install", "installed", 0),
            ("remove", "integrate", "remove", "absent", 0),
            ("check", "integrate", "check", "installed", 0),
            ("check", "integrate", "check", "absent", 1),
            ("install-driver-api", "integrate_driver_api", "install", "installed", 0),
            ("remove-driver-api", "integrate_driver_api", "remove", "absent", 0),
            ("check-driver-api", "integrate_driver_api", "check", "installed", 0),
            ("check-driver-api", "integrate_driver_api", "check", "absent", 1),
            ("install-host", "integrate_host", "install", "installed", 0),
            ("remove-host", "integrate_host", "remove", "absent", 0),
            ("check-host", "integrate_host", "check", "installed", 0),
            ("check-host", "integrate_host", "check", "absent", 1),
        )
        handlers = ("integrate", "integrate_driver_api", "integrate_host")
        for command, target, action, result, code in cases:
            with self.subTest(command=command, result=result):
                called = []

                def fake(name, value):
                    def inner(*args):
                        called.append((name, args))
                        return value
                    return inner

                patches = [
                    patch.object(installer, name, side_effect=fake(name, result if name == target else "other"))
                    for name in handlers]
                with patches[0], patches[1], patches[2]:
                    with patch.object(installer.sys, "argv", ["install.py", command, str(self.root)]):
                        self.assertEqual(installer.main(), code)
                self.assertEqual(len(called), 1)
                self.assertEqual(called[0][0], target)
                self.assertEqual(called[0][1][1], action)
                self.assertEqual(called[0][1][0], self.root)

        for command, target in (
                ("install", "integrate"),
                ("check", "integrate"),
                ("install-driver-api", "integrate_driver_api"),
                ("check-driver-api", "integrate_driver_api"),
                ("install-host", "integrate_host"),
                ("check-host", "integrate_host")):
            with self.subTest(command=command, error=True):
                with patch.object(installer, target, side_effect=RuntimeError("boom")):
                    with patch.object(installer.sys, "argv", ["install.py", command, str(self.root)]):
                        self.assertEqual(installer.main(), 2)
        self.assert_preserved()
        self.assertFalse(os.path.lexists(self.link))


if __name__ == "__main__":
    unittest.main()
