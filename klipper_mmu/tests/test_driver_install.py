#!/usr/bin/env python3
"""Exercise driver API installation, check, remove, rollback and preservation."""
# SPDX-License-Identifier: GPL-3.0-or-later
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

PACKAGE_DIR = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("installer", PACKAGE_DIR / "tools/install.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)

FIXTURES = Path(__file__).resolve().parent / "fixtures/shared_tmcuart"
# Hook-owned src/Kconfig is snapshotted too: refusals must not touch it either.
SNAPSHOT_TARGETS = installer.DRIVER_API_TARGETS + ("src/Kconfig",)
DRIVER_API_SYMBOLS = ("WANT_ADC_LOCAL", "WANT_HARD_PWM_QUERY", "WANT_HARD_PWM_LOCAL_PAIR",
                      "HARD_PWM_LOCAL_MAX_PAIRS", "WANT_TMCUART_LOCAL",
                      "TMCUART_LOCAL_MAX_CHANNELS", "WANT_GPIO_LOCAL",
                      "WANT_STEPPER_LOCAL", "STEPPER_LOCAL_COUNT", "STEPPER_LOCAL_MOVES")
GPIO_SOURCE_TARGETS = (
    "src/stm32/stm32h7_gpio.c",
    "src/stm32/gpio.c",
    "src/stm32/gpioperiph.c",
    "src/stm32/internal.h",
)
GPIO_HEADER_TARGET = "src/stm32/gpio_local.h"
GPIO_NEW_TARGETS = GPIO_SOURCE_TARGETS + (GPIO_HEADER_TARGET,)
WANT_GPIO_LOCAL_STANZA = (
    b'config WANT_GPIO_LOCAL\n'
    b'    bool "Firmware-local exclusive GPIO ownership"\n'
    b'    depends on MACH_STM32H723 || MACH_STM32H743 || MACH_STM32H750 || MACH_STM32F207 || MACH_STM32F405 || MACH_STM32F407 || MACH_STM32F429 || MACH_STM32F765 || MACH_STM32G431 || MACH_STM32G474 || MACH_STM32G0B0 || MACH_STM32G0B1\n'
    b'    default n\n'
    b'    help\n'
    b'        Optional driver API only, with no automatic MMU setup or\n'
    b'        actuation. Boot-lifetime claims and normal pin reservations\n'
    b'        still apply.\n')
WANT_STEPPER_LOCAL_STANZA = (
    b'config WANT_STEPPER_LOCAL\n'
    b'    bool "Support local MCU stepper segments"\n'
    b'    default n\n'
    b'    help\n'
    b"        Let MCU-local code (klipper-mmu's rate core) queue its own step\n"
    b"        segments into Klipper's stepper.c. Each local stepper owns a\n"
    b"        private pool of move nodes, so its segments never consume the\n"
    b"        host's shared move queue and host flow control stays true. This\n"
    b'        is an MCU API only; it is not reachable from host commands.\n'
    b'\n'
    b'config STEPPER_LOCAL_COUNT\n'
    b'    int\n'
    b'    depends on WANT_STEPPER_LOCAL\n'
    b'    default KLIPPER_MMU_LANE_COUNT\n'
    b'    help\n'
    b'        Local steppers in static storage (one per MMU lane).\n'
    b'\n'
    b'config STEPPER_LOCAL_MOVES\n'
    b'    int\n'
    b'    depends on WANT_STEPPER_LOCAL\n'
    b'    range 1 255\n'
    b'    default 224\n'
    b'    help\n'
    b'        Move nodes per local stepper: (2 + 2) x KM_STEP_SEGMENTS_MAX.\n')


class DriverInstallBaseTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix=".driver-install-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "klipper"
        self.root.mkdir()
        self.src = self.root / "src"
        shutil.copytree(FIXTURES / "src", self.src)
        (self.src / "Makefile").write_text("# source hook fixture\n")
        (self.root / "Makefile").write_text("# installer fixture\n")
        (self.root / "klippy").mkdir()

        # Add-on package mock for ordinary hook testing
        self.package = self.base / "klipper_mmu_pkg"
        (self.package / "mcu").mkdir(parents=True)
        for name in installer.REQUIRED_MCU_FILES:
            (self.package / "mcu" / name).write_text("# package\n")
        # Copy immutable upstream assets into the test package directory so package-based asset loading works
        shutil.copytree(PACKAGE_DIR / "upstream", self.package / "upstream")

        self.c_file = self.src / "tmcuart.c"
        self.h_file = self.src / "tmcuart.h"
        self.kconfig_file = self.src / "Kconfig"
        self.pwm_c_file = self.src / "stm32/hard_pwm.c"
        self.pwm_h_file = self.src / "stm32/pwm.h"
        self.adc_c_file = self.src / "stm32/stm32h7_adc.c"
        self.adc_h_file = self.src / "stm32/adc_local.h"

        self.original_c_bytes = self.c_file.read_bytes()
        self.original_c_mode = stat.S_IMODE(self.c_file.stat().st_mode)
        self.original_kconfig_bytes = self.kconfig_file.read_bytes()
        self.original_kconfig_mode = stat.S_IMODE(self.kconfig_file.stat().st_mode)
        self.original_pwm_c_bytes = self.pwm_c_file.read_bytes()
        self.original_pwm_c_mode = stat.S_IMODE(self.pwm_c_file.stat().st_mode)
        self.original_adc_c_bytes = self.adc_c_file.read_bytes()
        self.original_adc_c_mode = stat.S_IMODE(self.adc_c_file.stat().st_mode)
        self.gpio_sources = tuple(self.src / name.split("/", 1)[1] for name in GPIO_SOURCE_TARGETS)
        self.gpio_h_file = self.src / "stm32/gpio_local.h"
        self.original_gpio_bytes = {path: path.read_bytes() for path in self.gpio_sources}
        self.original_gpio_modes = {path: stat.S_IMODE(path.stat().st_mode) for path in self.gpio_sources}

    def run_driver_action(self, action, package=None):
        pkg = package if package is not None else self.package
        return installer.integrate_driver_api(self.root, action, package=pkg)

    def run_ordinary_action(self, action):
        return installer.integrate(self.root, action, package=self.package)

    def assert_pristine(self):
        self.assertEqual(self.c_file.read_bytes(), self.original_c_bytes)
        self.assertEqual(stat.S_IMODE(self.c_file.stat().st_mode), self.original_c_mode)
        self.assertEqual(self.kconfig_file.read_bytes(), self.original_kconfig_bytes)
        self.assertEqual(stat.S_IMODE(self.kconfig_file.stat().st_mode), self.original_kconfig_mode)
        self.assertEqual(self.pwm_c_file.read_bytes(), self.original_pwm_c_bytes)
        self.assertEqual(stat.S_IMODE(self.pwm_c_file.stat().st_mode), self.original_pwm_c_mode)
        self.assertEqual(self.adc_c_file.read_bytes(), self.original_adc_c_bytes)
        self.assertEqual(stat.S_IMODE(self.adc_c_file.stat().st_mode), self.original_adc_c_mode)
        for path in self.gpio_sources:
            self.assertEqual(path.read_bytes(), self.original_gpio_bytes[path])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), self.original_gpio_modes[path])
        self.assertFalse(os.path.lexists(self.h_file))
        self.assertFalse(os.path.lexists(self.pwm_h_file))
        self.assertFalse(os.path.lexists(self.adc_h_file))
        self.assertFalse(os.path.lexists(self.gpio_h_file))

    def snapshot_driver_targets(self):
        return {name: installer.read_driver_file(self.root / name)
                for name in SNAPSHOT_TARGETS}

    def assert_driver_snapshot(self, snapshots):
        self.assertEqual(self.snapshot_driver_targets(), snapshots)

    def reset_driver_tree(self):
        shutil.rmtree(self.src)
        shutil.copytree(FIXTURES / "src", self.src)
        (self.src / "Makefile").write_text("# source hook fixture\n")

    def previous_kconfig(self):
        """The exact src/Kconfig written by the previous twelve-path bundle."""
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            (stage / "src").mkdir()
            (stage / "src/Kconfig").write_bytes(self.original_kconfig_bytes)
            subprocess.run(["patch", "-p1", "--batch", "--fuzz=0"], cwd=stage, check=True,
                           input=(PACKAGE_DIR / "upstream/previous-kconfig.patch").read_bytes(),
                           capture_output=True, timeout=10)
            content = (stage / "src/Kconfig").read_bytes()
        self.assertEqual(installer.hashlib.sha256(content).hexdigest(),
                         installer.PREVIOUS_KCONFIG_HASHES[1])
        return content


class DriverInstallationLifecycleTest(DriverInstallBaseTest):
    def test_initial_state_absent(self):
        self.assertEqual(self.run_driver_action("check"), "absent")

    def test_byte_for_byte_roundtrip(self):
        # 1. Initially absent
        self.assertEqual(self.run_driver_action("check"), "absent")

        # 2. Install
        self.assertEqual(self.run_driver_action("install"), "installed")
        self.assertEqual(self.run_driver_action("check"), "installed")

        # Verify installed files and hashes across every driver API target
        manifest, _ = installer.load_driver_api_assets(self.package)
        for name in installer.DRIVER_API_TARGETS:
            self.assertEqual(installer.hashlib.sha256((self.root / name).read_bytes()).hexdigest(), manifest[name]["after"])
        self.assertEqual(stat.S_IMODE(self.c_file.stat().st_mode), self.original_c_mode)
        self.assertTrue(self.h_file.is_file())
        self.assertFalse(self.h_file.is_symlink())
        self.assertEqual(stat.S_IMODE(self.h_file.stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(self.pwm_c_file.stat().st_mode), self.original_pwm_c_mode)
        self.assertTrue(self.pwm_h_file.is_file())
        self.assertFalse(self.pwm_h_file.is_symlink())
        self.assertEqual(stat.S_IMODE(self.pwm_h_file.stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(self.adc_c_file.stat().st_mode), self.original_adc_c_mode)
        self.assertTrue(self.adc_h_file.is_file())
        self.assertFalse(self.adc_h_file.is_symlink())
        self.assertEqual(stat.S_IMODE(self.adc_h_file.stat().st_mode), 0o644)
        for path in self.gpio_sources:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), self.original_gpio_modes[path])
        self.assertTrue(self.gpio_h_file.is_file())
        self.assertFalse(self.gpio_h_file.is_symlink())
        self.assertEqual(stat.S_IMODE(self.gpio_h_file.stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(self.kconfig_file.stat().st_mode), self.original_kconfig_mode)

        # 3. Remove
        self.assertEqual(self.run_driver_action("remove"), "absent")
        self.assertEqual(self.run_driver_action("check"), "absent")

        # 4. Check byte-for-byte fidelity and permissions
        self.assert_pristine()

    def test_idempotence(self):
        self.assertEqual(self.run_driver_action("install"), "installed")
        self.assertEqual(self.run_driver_action("check"), "installed")
        targets = [self.root / name for name in installer.DRIVER_API_TARGETS]
        before = [path.stat().st_mtime_ns for path in targets]

        # Re-install is a no-op
        self.assertEqual(self.run_driver_action("install"), "installed")
        self.assertEqual(self.run_driver_action("check"), "installed")
        self.assertEqual(before, [path.stat().st_mtime_ns for path in targets])

        # Remove
        self.assertEqual(self.run_driver_action("remove"), "absent")
        # Re-remove is a no-op
        self.assertEqual(self.run_driver_action("remove"), "absent")
        self.assert_pristine()

    def test_driver_api_installed_before_ordinary_hooks(self):
        # 1. Driver API install first
        self.assertEqual(self.run_driver_action("install"), "installed")
        self.assertEqual(self.run_driver_action("check"), "installed")
        self.assertEqual(self.run_ordinary_action("check"), "absent")

        # 2. Ordinary add-on hooks installed second
        self.assertEqual(self.run_ordinary_action("install"), "installed")
        self.assertEqual(self.run_ordinary_action("check"), "installed")
        self.assertEqual(self.run_driver_action("check"), "installed")

        # src/Kconfig carries only the add-on hook; the package declares options.
        self.assertEqual(self.kconfig_file.read_bytes(),
                         self.original_kconfig_bytes + installer.HOOKS["Kconfig"])

        # 3. Remove driver API while ordinary hooks remain
        self.assertEqual(self.run_driver_action("remove"), "absent")
        self.assertEqual(self.run_driver_action("check"), "absent")
        self.assertEqual(self.run_ordinary_action("check"), "installed")
        self.assertEqual(self.kconfig_file.read_bytes(),
                         self.original_kconfig_bytes + installer.HOOKS["Kconfig"])

        # 4. Re-install driver API
        self.assertEqual(self.run_driver_action("install"), "installed")
        self.assertEqual(self.run_driver_action("check"), "installed")
        self.assertEqual(self.run_ordinary_action("check"), "installed")

        # 5. Remove ordinary hooks while driver API remains
        self.assertEqual(self.run_ordinary_action("remove"), "absent")
        self.assertEqual(self.run_ordinary_action("check"), "absent")
        self.assertEqual(self.run_driver_action("check"), "installed")
        self.assertEqual(self.kconfig_file.read_bytes(), self.original_kconfig_bytes)

        # 6. Remove driver API -> pristine
        self.assertEqual(self.run_driver_action("remove"), "absent")
        self.assert_pristine()

    def test_ordinary_hooks_installed_before_driver_api(self):
        # 1. Ordinary add-on hooks installed first
        self.assertEqual(self.run_ordinary_action("install"), "installed")
        self.assertEqual(self.run_driver_action("check"), "absent")

        # 2. Driver API installed second
        self.assertEqual(self.run_driver_action("install"), "installed")
        self.assertEqual(self.run_driver_action("check"), "installed")
        self.assertEqual(self.run_ordinary_action("check"), "installed")

        # 3. Remove ordinary hooks
        self.assertEqual(self.run_ordinary_action("remove"), "absent")
        self.assertEqual(self.run_driver_action("check"), "installed")

        # 4. Remove driver API -> pristine
        self.assertEqual(self.run_driver_action("remove"), "absent")
        self.assert_pristine()


class DriverInstallRefusalTest(DriverInstallBaseTest):
    def test_kconfig_damaged_or_unmanaged_block_refused(self):
        # Damaged marker
        self.kconfig_file.write_bytes(self.original_kconfig_bytes + b"\n# BEGIN klipper-mmu add-on\n")
        with self.assertRaisesRegex(RuntimeError, "Altered or duplicate add-on block"):
            self.run_driver_action("install")
        self.assertEqual(self.c_file.read_bytes(), self.original_c_bytes)
        self.assertFalse(os.path.lexists(self.h_file))

        # Unmanaged reference
        self.kconfig_file.write_bytes(self.original_kconfig_bytes + b'\nsource "src/klipper_mmu/mcu/Kconfig"\n')
        with self.assertRaisesRegex(RuntimeError, "Unmanaged add-on reference"):
            self.run_driver_action("install")
        self.assertEqual(self.c_file.read_bytes(), self.original_c_bytes)
        self.assertFalse(os.path.lexists(self.h_file))

    def test_source_edits_refused_without_mutation(self):
        # Edit tmcuart.c before install
        edited_c = self.original_c_bytes + b"\n// user edit\n"
        self.c_file.write_bytes(edited_c)
        with self.assertRaisesRegex(RuntimeError, "Unsupported, modified or mixed"):
            self.run_driver_action("install")
        self.assertEqual(self.c_file.read_bytes(), edited_c)
        self.assertEqual(self.kconfig_file.read_bytes(), self.original_kconfig_bytes)
        self.assertFalse(os.path.lexists(self.h_file))
        with self.assertRaisesRegex(RuntimeError, "Unsupported, modified or mixed"):
            self.run_driver_action("check")

        # Test edit after install: removal must be refused
        self.c_file.write_bytes(self.original_c_bytes)
        self.run_driver_action("install")
        installed_c = self.c_file.read_bytes()
        self.c_file.write_bytes(installed_c + b"\n// user edit after install\n")
        with self.assertRaisesRegex(RuntimeError, "Unsupported, modified or mixed"):
            self.run_driver_action("remove")
        self.assertTrue(self.c_file.read_bytes().endswith(b"// user edit after install\n"))
        self.assertTrue(self.h_file.is_file())

    def test_foreign_or_symlink_or_hardlink_header_refused(self):
        for h_path in (self.h_file, self.pwm_h_file, self.adc_h_file, self.gpio_h_file):
            outside = self.base / ("outside_" + h_path.name)
            outside.write_text("// outside\n")

            # Foreign header before install
            h_path.write_text("// foreign header\n")
            with self.assertRaisesRegex(RuntimeError, "Unsupported, modified or mixed"):
                self.run_driver_action("install")
            self.assertEqual(self.c_file.read_bytes(), self.original_c_bytes)
            self.assertEqual(self.pwm_c_file.read_bytes(), self.original_pwm_c_bytes)
            self.assertEqual(self.adc_c_file.read_bytes(), self.original_adc_c_bytes)
            h_path.unlink()

            # Symlink header before install
            h_path.symlink_to(outside)
            with self.assertRaisesRegex(RuntimeError, "regular source file with a single hard link"):
                self.run_driver_action("install")
            with self.assertRaisesRegex(RuntimeError, "regular source file with a single hard link"):
                self.run_driver_action("check")
            h_path.unlink()

            # Dangling symlink header before install
            h_path.symlink_to(self.base / "nonexistent.h")
            with self.assertRaisesRegex(RuntimeError, "regular source file with a single hard link"):
                self.run_driver_action("install")
            h_path.unlink()

            # Directory header before install
            h_path.mkdir()
            with self.assertRaisesRegex(RuntimeError, "regular source file with a single hard link"):
                self.run_driver_action("install")
            h_path.rmdir()

            # Hardlinked header before install
            os.link(outside, h_path)
            with self.assertRaisesRegex(RuntimeError, "regular source file with a single hard link"):
                self.run_driver_action("install")
            h_path.unlink()

        # Headers on remove
        self.run_driver_action("install")
        self.pwm_h_file.write_text("// altered pwm.h after install\n")
        with self.assertRaisesRegex(RuntimeError, "Unsupported, modified or mixed"):
            self.run_driver_action("remove")

        outside_rem = self.base / "outside_rem.h"
        outside_rem.write_text("// outside\n")
        self.pwm_h_file.unlink()
        self.pwm_h_file.symlink_to(outside_rem)
        with self.assertRaisesRegex(RuntimeError, "regular source file with a single hard link"):
            self.run_driver_action("remove")

    def test_symlink_or_hardlink_sources_refused(self):
        outside = self.base / "outside_c.c"
        outside.write_bytes(self.original_c_bytes)

        # Symlink tmcuart.c
        self.c_file.unlink()
        self.c_file.symlink_to(outside)
        with self.assertRaisesRegex(RuntimeError, "regular source file with a single hard link"):
            self.run_driver_action("install")
        self.c_file.unlink()

        # Hardlinked tmcuart.c
        os.link(outside, self.c_file)
        with self.assertRaisesRegex(RuntimeError, "single hard link"):
            self.run_driver_action("install")
        self.c_file.unlink()
        shutil.copy2(outside, self.c_file)

        # Symlink Kconfig
        outside_k = self.base / "outside_k"
        outside_k.write_bytes(self.original_kconfig_bytes)
        self.kconfig_file.unlink()
        self.kconfig_file.symlink_to(outside_k)
        with self.assertRaisesRegex(RuntimeError, "single hard link"):
            self.run_driver_action("install")
        self.kconfig_file.unlink()

        # Hardlink Kconfig
        os.link(outside_k, self.kconfig_file)
        with self.assertRaisesRegex(RuntimeError, "single hard link"):
            self.run_driver_action("install")
        self.kconfig_file.unlink()
        shutil.copy2(outside_k, self.kconfig_file)

        # Symlink hard_pwm.c
        outside_pwm = self.base / "outside_pwm.c"
        outside_pwm.write_bytes(self.original_pwm_c_bytes)
        self.pwm_c_file.unlink()
        self.pwm_c_file.symlink_to(outside_pwm)
        with self.assertRaisesRegex(RuntimeError, "regular source file with a single hard link"):
            self.run_driver_action("install")
        self.pwm_c_file.unlink()

        # Hardlink hard_pwm.c
        os.link(outside_pwm, self.pwm_c_file)
        with self.assertRaisesRegex(RuntimeError, "single hard link"):
            self.run_driver_action("install")
        self.pwm_c_file.unlink()
        shutil.copy2(outside_pwm, self.pwm_c_file)

        # Symlink stm32h7_adc.c
        outside_adc = self.base / "outside_adc.c"
        outside_adc.write_bytes(self.original_adc_c_bytes)
        self.adc_c_file.unlink()
        self.adc_c_file.symlink_to(outside_adc)
        with self.assertRaisesRegex(RuntimeError, "regular source file with a single hard link"):
            self.run_driver_action("install")
        self.adc_c_file.unlink()

        # Hardlink stm32h7_adc.c
        os.link(outside_adc, self.adc_c_file)
        with self.assertRaisesRegex(RuntimeError, "single hard link"):
            self.run_driver_action("install")
        self.adc_c_file.unlink()
        shutil.copy2(outside_adc, self.adc_c_file)

        for path, original in self.original_gpio_bytes.items():
            outside_gpio = self.base / ("outside_" + path.name)
            outside_gpio.write_bytes(original)
            path.unlink()
            path.symlink_to(outside_gpio)
            with self.assertRaisesRegex(RuntimeError, "regular source file with a single hard link"):
                self.run_driver_action("install")
            path.unlink()
            os.link(outside_gpio, path)
            with self.assertRaisesRegex(RuntimeError, "single hard link"):
                self.run_driver_action("install")
            path.unlink()
            shutil.copy2(outside_gpio, path)

        # Symlink src directory
        shutil.rmtree(self.src)
        outside_src = self.base / "outside_src"
        outside_src.mkdir()
        self.src.symlink_to(outside_src)
        with self.assertRaisesRegex(RuntimeError, "must be a real directory"):
            self.run_driver_action("install")

    def test_nested_parent_path_validation(self):
        stm32_dir = self.src / "stm32"
        outside_stm32 = self.base / "outside_stm32"
        outside_stm32.mkdir()
        shutil.copy2(self.pwm_c_file, outside_stm32 / "hard_pwm.c")

        # 1. Symlink stm32 directory must be refused and outside targets never touched
        shutil.rmtree(stm32_dir)
        stm32_dir.symlink_to(outside_stm32)
        with self.assertRaisesRegex(RuntimeError, "must be a real directory"):
            self.run_driver_action("install")
        with self.assertRaisesRegex(RuntimeError, "must be a real directory"):
            self.run_driver_action("check")
        # Verify outside file was NOT modified
        self.assertEqual((outside_stm32 / "hard_pwm.c").read_bytes(), self.original_pwm_c_bytes)
        self.assertFalse((outside_stm32 / "pwm.h").exists())

        # 2. Missing stm32 directory must be refused
        stm32_dir.unlink()
        with self.assertRaisesRegex(RuntimeError, "must be a real directory"):
            self.run_driver_action("install")
        with self.assertRaisesRegex(RuntimeError, "must be a real directory"):
            self.run_driver_action("check")

        # 3. Regular file at stm32 parent path must be refused
        stm32_dir.write_text("not a directory\n")
        with self.assertRaisesRegex(RuntimeError, "must be a real directory"):
            self.run_driver_action("install")
        stm32_dir.unlink()

        # Restore
        stm32_dir.mkdir()
        shutil.copy2(outside_stm32 / "hard_pwm.c", stm32_dir / "hard_pwm.c")

    def test_altered_pwm_input_and_header_refused(self):
        # Altered hard_pwm.c before install
        edited_pwm = self.original_pwm_c_bytes + b"\n// altered hard_pwm.c\n"
        self.pwm_c_file.write_bytes(edited_pwm)
        with self.assertRaisesRegex(RuntimeError, "Unsupported, modified or mixed"):
            self.run_driver_action("install")
        with self.assertRaisesRegex(RuntimeError, "Unsupported, modified or mixed"):
            self.run_driver_action("check")
        self.assertEqual(self.pwm_c_file.read_bytes(), edited_pwm)
        self.assertFalse(os.path.lexists(self.pwm_h_file))

        # Restore hard_pwm.c and install
        self.pwm_c_file.write_bytes(self.original_pwm_c_bytes)
        self.run_driver_action("install")

        # Altered pwm.h after install
        self.pwm_h_file.write_bytes(b"// altered pwm.h content\n")
        with self.assertRaisesRegex(RuntimeError, "Unsupported, modified or mixed"):
            self.run_driver_action("remove")
        with self.assertRaisesRegex(RuntimeError, "Unsupported, modified or mixed"):
            self.run_driver_action("check")

    def test_altered_adc_input_and_header_refused(self):
        edited_adc = self.original_adc_c_bytes + b"\n// altered stm32h7_adc.c\n"
        self.adc_c_file.write_bytes(edited_adc)
        with self.assertRaisesRegex(RuntimeError, "Unsupported, modified or mixed"):
            self.run_driver_action("install")
        with self.assertRaisesRegex(RuntimeError, "Unsupported, modified or mixed"):
            self.run_driver_action("check")
        self.assertEqual(self.adc_c_file.read_bytes(), edited_adc)
        self.assertFalse(os.path.lexists(self.adc_h_file))

        self.adc_c_file.write_bytes(self.original_adc_c_bytes)
        self.run_driver_action("install")
        self.adc_h_file.write_bytes(b"// altered adc_local.h content\n")
        with self.assertRaisesRegex(RuntimeError, "Unsupported, modified or mixed"):
            self.run_driver_action("remove")
        with self.assertRaisesRegex(RuntimeError, "Unsupported, modified or mixed"):
            self.run_driver_action("check")

    def test_old_uart_only_bundle_refused(self):
        # Construct the exact historical UART-only state, including its Kconfig.
        manifest, bundle = installer.load_driver_api_assets(self.package)
        before = {name: (self.root / name).read_bytes() if (self.root / name).exists()
                  else None for name in installer.DRIVER_API_TARGETS}
        staged = installer.stage_driver_api(before, "install", manifest, bundle)
        for name in ("src/tmcuart.c", "src/tmcuart.h"):
            (self.root / name).write_bytes(staged[name])
        previous = self.previous_kconfig()
        new_pwm_configs = (
            b'config WANT_HARD_PWM_QUERY\n'
            b'    bool "Expose hardware PWM routes to local MCU modules"\n'
            b'    depends on MACH_STM32 && WANT_HARD_PWM\n    default n\n'
            b'config WANT_HARD_PWM_LOCAL_PAIR\n'
            b'    bool "Support local paired PWM channels"\n'
            b'    depends on MACH_STM32H723 && WANT_HARD_PWM\n    default n\n'
            b'config HARD_PWM_LOCAL_MAX_PAIRS\n'
            b'    int "Maximum local PWM pairs"\n'
            b'    depends on WANT_HARD_PWM_LOCAL_PAIR\n'
            b'    range 1 8\n    default 8\n')
        new_adc_config = (
            b'config WANT_ADC_LOCAL\n'
            b'    bool "Firmware-local exclusive sequential ADC pairs"\n'
            b'    depends on MACH_STM32H723 && WANT_ADC\n'
            b'    default n\n'
            b'    help\n'
            b'        Own one ADC unit exclusively for a firmware-local sequential pin\n'
            b'        pair. Ordinary gpio_adc_setup clients and the local pair never\n'
            b'        share that unit, and the winner keeps it for the boot lifetime.\n'
            b'        Sequential reads run one after another; they are not simultaneous\n'
            b'        or synchronized ADC conversions.\n')
        self.assertEqual(previous.count(new_pwm_configs), 1)
        self.assertEqual(previous.count(new_adc_config), 1)
        self.assertEqual(previous.count(b'\n' + WANT_GPIO_LOCAL_STANZA), 1)
        kconfig = (previous.replace(new_pwm_configs, b'')
                   .replace(new_adc_config, b'')
                   .replace(b'\n' + WANT_GPIO_LOCAL_STANZA, b'', 1)
                   .replace(b'depends on WANT_TMCUART && (MACH_STM32H723 || MACH_STM32H743 || MACH_STM32H750 || MACH_STM32F207 || MACH_STM32F405 || MACH_STM32F407 || MACH_STM32F429 || MACH_STM32F765 || MACH_STM32G431 || MACH_STM32G474 || MACH_STM32G0B0 || MACH_STM32G0B1)',
                            b'depends on WANT_TMCUART && MACH_STM32H723'))
        self.assertEqual(installer.hashlib.sha256(kconfig).hexdigest(),
            "07fdf33faf7aed89e25b937ab3cd5f98e725c562db2c101477aee4d34ec5c23e")
        self.kconfig_file.write_bytes(kconfig)
        snapshots = self.snapshot_driver_targets()
        for action in ("check", "install", "remove"):
            with self.assertRaisesRegex(RuntimeError, "Unsupported, modified or mixed"):
                self.run_driver_action(action)
            self.assert_driver_snapshot(snapshots)

    def test_historical_uart_and_route_bundle_refused_and_preserved(self):
        # Frozen accepted iteration-46 patch and source hashes.  This fixture
        # must not be derived from the current candidate's after-state.
        historical = Path(__file__).resolve().parent / "klipper_pwm_pair/historical46.patch"
        patch_bytes = historical.read_bytes()
        self.assertEqual(installer.hashlib.sha256(patch_bytes).hexdigest(),
            "0fc82df3ecdf5a095fbf09269d63d425c9794294b1bd710850bfd2e49ee8952d")
        result = subprocess.run(["patch", "-p1", "--batch", "--fuzz=0"],
            cwd=self.root, input=patch_bytes, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        accepted46 = {
            "src/tmcuart.c": "6b5242e5a4b4991e640f4b60435f1036228cfd976fabf904451fa86d4a8d6cf8",
            "src/tmcuart.h": "fea749f4b6c9a4abdf00ccbc96bad1060bd0138e8df3e0cd9060be0359b2a10e",
            "src/stm32/hard_pwm.c": "8a4bf93c50df0567b9b6bb892f3b4f165e7f4cd7031d91a84de899c9336617f4",
            "src/stm32/pwm.h": "ea2a0c59eedf5a843c22e044ab031a02b04c285721cfe305d19bfa1601792d5e",
            "src/Kconfig": "e119a514ab068c38d3576868a7c97c72b5cb30b16e38c390b138dc7160b3cc01",
        }
        for name, digest in accepted46.items():
            self.assertEqual(installer.hashlib.sha256((self.root / name).read_bytes()).hexdigest(), digest)
        snapshots = self.snapshot_driver_targets()
        for action in ("check", "install", "remove"):
            with self.assertRaisesRegex(RuntimeError, "Unsupported, modified or mixed"):
                self.run_driver_action(action)
            self.assert_driver_snapshot(snapshots)

    def test_historical_pre_adc_five_target_bundle_refused(self):
        # Frozen prior five-target patch. Not derived from the current after-state.
        historical = FIXTURES / "historical-pre-adc.patch"
        patch_bytes = historical.read_bytes()
        self.assertEqual(installer.hashlib.sha256(patch_bytes).hexdigest(),
            "4bc9b68efa6bcbbfc4e01e3f25195b5bdd395dcbacb9a9d0f2d71741838e9ede")
        result = subprocess.run(["patch", "-p1", "--batch", "--fuzz=0"],
            cwd=self.root, input=patch_bytes, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        snapshots = self.snapshot_driver_targets()
        for action in ("check", "install", "remove"):
            with self.assertRaisesRegex(RuntimeError, "Unsupported, modified or mixed"):
                self.run_driver_action(action)
            self.assert_driver_snapshot(snapshots)

    def test_missing_or_corrupt_assets_refused(self):
        bad_pkg = self.base / "bad_pkg"
        bad_upstream = bad_pkg / "upstream"
        bad_upstream.mkdir(parents=True)

        patch_src = PACKAGE_DIR / "upstream/drivers-local.patch"
        manifest_src = PACKAGE_DIR / "upstream/source-hashes.json"

        # Missing manifest
        shutil.copy2(patch_src, bad_upstream / "drivers-local.patch")
        with self.assertRaisesRegex(RuntimeError, "Missing or altered reviewed driver API asset"):
            self.run_driver_action("install", package=bad_pkg)
        self.assert_pristine()

        # Missing patch
        (bad_upstream / "drivers-local.patch").unlink()
        shutil.copy2(manifest_src, bad_upstream / "source-hashes.json")
        with self.assertRaisesRegex(RuntimeError, "Missing or altered reviewed driver API asset"):
            self.run_driver_action("install", package=bad_pkg)
        self.assert_pristine()

        # Corrupt JSON in manifest
        shutil.copy2(patch_src, bad_upstream / "drivers-local.patch")
        (bad_upstream / "source-hashes.json").write_text("{corrupt json")
        with self.assertRaisesRegex(RuntimeError, "Missing or altered reviewed driver API asset"):
            self.run_driver_action("install", package=bad_pkg)
        self.assert_pristine()

        # Manifest with unexpected key / path traversal
        bad_manifest = json.loads(manifest_src.read_text())
        bad_manifest["src/extra.c"] = {"before": "abc", "after": "def"}
        (bad_upstream / "source-hashes.json").write_text(json.dumps(bad_manifest))
        with self.assertRaisesRegex(RuntimeError, "Missing or altered reviewed driver API asset"):
            self.run_driver_action("install", package=bad_pkg)
        self.assert_pristine()

        # Manifest with invalid hash
        bad_manifest = json.loads(manifest_src.read_text())
        bad_manifest["src/tmcuart.c"]["after"] = "invalid-hex-hash"
        (bad_upstream / "source-hashes.json").write_text(json.dumps(bad_manifest))
        with self.assertRaisesRegex(RuntimeError, "Missing or altered reviewed driver API asset"):
            self.run_driver_action("install", package=bad_pkg)
        self.assert_pristine()

        # Corrupt / empty patch
        shutil.copy2(manifest_src, bad_upstream / "source-hashes.json")
        (bad_upstream / "drivers-local.patch").write_bytes(b"")
        with self.assertRaisesRegex(RuntimeError, "Missing or altered reviewed driver API asset"):
            self.run_driver_action("install", package=bad_pkg)
        self.assert_pristine()

    def test_mixed_state_refused(self):
        # Apply patch to tmcuart.c only
        manifest, _ = installer.load_driver_api_assets(self.package)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            (tmp_p / "src").mkdir()
            (tmp_p / "src/tmcuart.c").write_bytes(self.original_c_bytes)
            patch_content = (PACKAGE_DIR / "upstream/drivers-local.patch").read_bytes()
            start = patch_content.index(b"--- a/src/tmcuart.c\n")
            end = patch_content.find(b"\n--- ", start)
            c_only = patch_content[start:end + 1] if end >= 0 else patch_content[start:]
            subprocess.run(["patch", "-p1", "--batch", "--fuzz=0"], cwd=tmp_p, input=c_only, check=True, timeout=10)
            patched_c = (tmp_p / "src/tmcuart.c").read_bytes()

        self.c_file.write_bytes(patched_c)

        with self.assertRaisesRegex(RuntimeError, "Unsupported, modified or mixed"):
            self.run_driver_action("check")
        with self.assertRaisesRegex(RuntimeError, "Unsupported, modified or mixed"):
            self.run_driver_action("install")
        with self.assertRaisesRegex(RuntimeError, "Unsupported, modified or mixed"):
            self.run_driver_action("remove")

    def test_staged_output_hash_mismatch_refused_before_mutation(self):
        run = installer.subprocess.run

        def corrupt_staged_file(*args, **kwargs):
            result = run(*args, **kwargs)
            (Path(kwargs["cwd"]) / "src/stm32/hard_pwm.c").write_bytes(b"wrong output")
            return result

        with patch.object(installer.subprocess, "run", corrupt_staged_file):
            with self.assertRaisesRegex(RuntimeError, "did not match expected"):
                self.run_driver_action("install")
        self.assert_pristine()

    def test_invalid_action_raises_value_error(self):
        with self.assertRaises(ValueError):
            self.run_driver_action("invalid_action")

    def test_altered_gpio_sources_and_foreign_header_refused(self):
        for path in self.gpio_sources:
            with self.subTest(path=path.name, phase="pristine-edit"):
                edited = self.original_gpio_bytes[path] + b"\n// gpio user edit\n"
                path.write_bytes(edited)
                snapshots = self.snapshot_driver_targets()
                for action in ("install", "check", "remove"):
                    with self.assertRaisesRegex(RuntimeError, "Unsupported, modified or mixed"):
                        self.run_driver_action(action)
                    self.assert_driver_snapshot(snapshots)
                path.write_bytes(self.original_gpio_bytes[path])
        self.gpio_h_file.write_bytes(b"// foreign gpio header\n")
        snapshots = self.snapshot_driver_targets()
        for action in ("install", "check", "remove"):
            with self.assertRaisesRegex(RuntimeError, "Unsupported, modified or mixed"):
                self.run_driver_action(action)
            self.assert_driver_snapshot(snapshots)
        self.gpio_h_file.unlink()

        self.assertEqual(self.run_driver_action("install"), "installed")
        self.assertEqual(self.run_ordinary_action("install"), "installed")
        for path in (*self.gpio_sources, self.gpio_h_file):
            with self.subTest(path=path.name, phase="installed-edit"):
                current = path.read_bytes()
                path.write_bytes(current + b"\n// gpio edit after install\n")
                snapshots = self.snapshot_driver_targets()
                for action in ("remove", "check", "install"):
                    with self.assertRaisesRegex(RuntimeError, "Unsupported, modified or mixed"):
                        self.run_driver_action(action)
                    self.assert_driver_snapshot(snapshots)
                path.write_bytes(current)
        self.assertEqual(self.run_ordinary_action("check"), "installed")
        self.assertEqual(self.run_driver_action("remove"), "absent")
        self.assertEqual(self.run_ordinary_action("check"), "installed")
        self.assertIn(installer.HOOKS["Kconfig"], self.kconfig_file.read_bytes())
        self.assertEqual(self.run_ordinary_action("remove"), "absent")
        self.assert_pristine()

    def test_preceding_seven_target_bundle_refused(self):
        # Frozen prior seven-target patch. Not derived from the current after-state.
        historical = FIXTURES / "historical-pre-gpio.patch"
        manifest_path = FIXTURES / "historical-pre-gpio-hashes.json"
        patch_bytes = historical.read_bytes()
        manifest_bytes = manifest_path.read_bytes()
        self.assertEqual(installer.hashlib.sha256(patch_bytes).hexdigest(),
            "12f371e4743815480d1eaddfaebada5c345f648b0a32f94c54f399cd3631ae56")
        self.assertEqual(installer.hashlib.sha256(manifest_bytes).hexdigest(),
            "b9e36f96350b6c66ab763c9b814788fb19802479f30b2c2887745958b37cb3cd")
        frozen = json.loads(manifest_bytes)
        current, _ = installer.load_driver_api_assets(self.package)
        for hooked in (False, True):
            with self.subTest(hooked=hooked):
                self.reset_driver_tree()
                result = subprocess.run(["patch", "-p1", "--batch", "--fuzz=0"],
                    cwd=self.root, input=patch_bytes, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                if hooked:
                    self.assertEqual(self.run_ordinary_action("install"), "installed")
                for name, entry in frozen.items():
                    data = (self.root / name).read_bytes()
                    if name == "src/Kconfig" and hooked:
                        block = installer.HOOKS["Kconfig"]
                        self.assertEqual(data.count(block), 1)
                        data = data.replace(block, b"", 1)
                    self.assertEqual(installer.hashlib.sha256(data).hexdigest(), entry["after"])
                for name in GPIO_SOURCE_TARGETS:
                    digest = installer.hashlib.sha256((self.root / name).read_bytes()).hexdigest()
                    self.assertEqual(digest, current[name]["before"])
                self.assertIsNone(installer.read_driver_file(self.root / GPIO_HEADER_TARGET)[0])
                snapshots = self.snapshot_driver_targets()
                for action in ("check", "install", "remove"):
                    with self.assertRaisesRegex(RuntimeError, "Unsupported, modified or mixed"):
                        self.run_driver_action(action)
                    self.assert_driver_snapshot(snapshots)
                if hooked:
                    self.assertEqual(self.run_ordinary_action("check"), "installed")
                    self.assertIn(installer.HOOKS["Kconfig"], self.kconfig_file.read_bytes())


class DriverInstallRollbackTest(DriverInstallBaseTest):
    def test_install_failure_on_late_target_rolls_back_all_and_preserves_pristine(self):
        original_replace = installer.replace_file

        def fail_on_pwm_h(path, content, mode):
            if path.name == "pwm.h":
                raise OSError("injected late target pwm.h write error")
            original_replace(path, content, mode)

        with patch.object(installer, "replace_file", fail_on_pwm_h):
            with self.assertRaisesRegex(OSError, "injected late target pwm.h write error"):
                self.run_driver_action("install")

        # Rollback of all four preceding targets must restore pristine checkout
        self.assert_pristine()

    def test_install_failure_on_adc_targets_restores_seven(self):
        original_replace = installer.replace_file
        for name, message in (("stm32h7_adc.c", "injected adc source write error"),
                              ("adc_local.h", "injected adc header write error")):
            def fail(path, content, mode, name=name, message=message):
                if path.name == name:
                    raise OSError(message)
                original_replace(path, content, mode)
            with patch.object(installer, "replace_file", fail):
                with self.assertRaisesRegex(OSError, message):
                    self.run_driver_action("install")
            self.assert_pristine()

    def test_remove_failure_on_late_target_restores_all_installed_and_preserves_header_mode(self):
        self.run_driver_action("install")
        self.assertEqual(self.run_driver_action("check"), "installed")

        # Set specific custom modes on headers
        self.h_file.chmod(0o640)
        self.pwm_h_file.chmod(0o640)

        # Late target during remove: fail on pwm.h unlink
        real_unlink = Path.unlink
        def fail_on_pwm_h_unlink(path):
            if path.name == "pwm.h":
                raise OSError("injected pwm.h unlink error")
            real_unlink(path)

        with patch.object(Path, "unlink", fail_on_pwm_h_unlink):
            with self.assertRaisesRegex(OSError, "injected pwm.h unlink error"):
                self.run_driver_action("remove")

        # Check all targets remain installed with exact modes preserved
        self.assertEqual(self.run_driver_action("check"), "installed")
        self.assertTrue(self.h_file.is_file())
        self.assertEqual(stat.S_IMODE(self.h_file.stat().st_mode), 0o640)
        self.assertTrue(self.pwm_h_file.is_file())
        self.assertEqual(stat.S_IMODE(self.pwm_h_file.stat().st_mode), 0o640)

    def test_remove_failure_on_uart_header_restores_prior_targets(self):
        self.run_driver_action("install")
        unlink = Path.unlink
        def fail_uart_header(path, *args, **kwargs):
            if path == self.h_file:
                raise OSError("injected UART unlink failure")
            return unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", fail_uart_header):
            with self.assertRaisesRegex(OSError, "injected UART unlink failure"):
                self.run_driver_action("remove")
        self.assertEqual(self.run_driver_action("check"), "installed")

    def test_remove_failure_on_adc_source_restores_installed(self):
        self.run_driver_action("install")
        self.adc_c_file.chmod(0o640)
        self.adc_h_file.chmod(0o640)
        snapshots = {name: installer.read_driver_file(self.root / name)
                     for name in installer.DRIVER_API_TARGETS}
        original_replace = installer.replace_file
        def fail(path, content, mode):
            if path.name == "stm32h7_adc.c":
                raise OSError("injected adc source remove error")
            original_replace(path, content, mode)
        with patch.object(installer, "replace_file", fail):
            with self.assertRaisesRegex(OSError, "injected adc source remove error"):
                self.run_driver_action("remove")
        self.assertEqual({name: installer.read_driver_file(self.root / name)
                          for name in installer.DRIVER_API_TARGETS}, snapshots)
        self.assertEqual(self.run_driver_action("check"), "installed")

    def test_remove_failure_on_adc_header_restores_installed(self):
        self.run_driver_action("install")
        self.adc_c_file.chmod(0o640)
        self.adc_h_file.chmod(0o640)
        snapshots = {name: installer.read_driver_file(self.root / name)
                     for name in installer.DRIVER_API_TARGETS}
        unlink = Path.unlink
        def fail(path, *args, **kwargs):
            if path == self.adc_h_file:
                raise OSError("injected adc header unlink error")
            return unlink(path, *args, **kwargs)
        with patch.object(Path, "unlink", fail):
            with self.assertRaisesRegex(OSError, "injected adc header unlink error"):
                self.run_driver_action("remove")
        self.assertEqual({name: installer.read_driver_file(self.root / name)
                          for name in installer.DRIVER_API_TARGETS}, snapshots)
        self.assertEqual(self.run_driver_action("check"), "installed")

    def test_concurrent_edit_not_overwritten_by_rollback(self):
        original_replace = installer.replace_file

        def conflict_on_header(path, content, mode):
            if path.name == "pwm.h":
                # User concurrently modifies hard_pwm.c before failure
                self.pwm_c_file.write_text("// concurrent edit in hard_pwm.c\n")
                raise OSError("injected header failure")
            original_replace(path, content, mode)

        with patch.object(installer, "replace_file", conflict_on_header):
            with self.assertRaisesRegex(RuntimeError, "rollback incomplete"):
                self.run_driver_action("install")

        self.assertEqual(self.pwm_c_file.read_text(), "// concurrent edit in hard_pwm.c\n")
        self.assertFalse(os.path.lexists(self.pwm_h_file))

    def test_ancestor_swapped_before_nested_write_preserves_outside(self):
        outside = self.base / "outside_source"
        shutil.copytree(self.src, outside)
        before = {str(p.relative_to(outside)): p.read_bytes()
                  for p in outside.rglob("*") if p.is_file()}
        write = installer.write_driver_file
        def swap_after_uart_header(path, snapshot):
            write(path, snapshot)
            if path == self.h_file:
                self.src.rename(self.base / "parked_source")
                self.src.symlink_to(outside, target_is_directory=True)
        with patch.object(installer, "write_driver_file", swap_after_uart_header):
            with self.assertRaisesRegex(RuntimeError, "must be a real directory.*rollback incomplete"):
                self.run_driver_action("install")
        after = {str(p.relative_to(outside)): p.read_bytes()
                 for p in outside.rglob("*") if p.is_file()}
        self.assertEqual(after, before)

    def test_directory_flock_concurrency(self):
        descriptor = os.open(self.src, os.O_RDONLY | os.O_DIRECTORY)
        try:
            installer.fcntl.flock(descriptor, installer.fcntl.LOCK_EX | installer.fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                self.run_driver_action("install")
        finally:
            os.close(descriptor)
        self.assert_pristine()

    def test_atomic_write_failure_leaves_no_temporary_file(self):
        with patch.object(installer.os, "replace", side_effect=OSError("injected rename failure")):
            with self.assertRaises(OSError):
                self.run_driver_action("install")
        self.assert_pristine()
        # Verify no .klipper-mmu-* temporary files left behind in src or src/stm32
        temp_files = [p.name for p in self.src.iterdir() if p.name.startswith(".klipper-mmu-")]
        temp_files_stm32 = [p.name for p in (self.src / "stm32").iterdir() if p.name.startswith(".klipper-mmu-")]
        self.assertEqual(temp_files, [])
        self.assertEqual(temp_files_stm32, [])

    def test_gpio_target_failures_roll_back_every_target(self):
        original_replace = installer.replace_file
        real_unlink = Path.unlink
        for hooked in (False, True):
            self.reset_driver_tree()
            if hooked:
                self.assertEqual(self.run_ordinary_action("install"), "installed")
            for path in self.gpio_sources:
                path.chmod(0o640)
            baseline = self.snapshot_driver_targets()
            for name in (Path(item).name for item in GPIO_NEW_TARGETS):
                with self.subTest(hooked=hooked, phase="install", name=name):
                    def fail_write(path, content, mode, name=name):
                        if path.name == name:
                            raise OSError(f"injected {name} install error")
                        original_replace(path, content, mode)
                    with patch.object(installer, "replace_file", fail_write):
                        with self.assertRaisesRegex(OSError, f"injected {name} install error"):
                            self.run_driver_action("install")
                    self.assert_driver_snapshot(baseline)
                self.assertEqual(self.run_driver_action("install"), "installed")
                for path in (*self.gpio_sources, self.gpio_h_file):
                    path.chmod(0o640)
                installed = self.snapshot_driver_targets()
                with self.subTest(hooked=hooked, phase="remove", name=name):
                    if name == "gpio_local.h":
                        def fail_unlink(path, name=name):
                            if path.name == name:
                                raise OSError(f"injected {name} remove error")
                            real_unlink(path)
                        context = patch.object(Path, "unlink", fail_unlink)
                    else:
                        def fail_restore(path, content, mode, name=name):
                            if path.name == name:
                                raise OSError(f"injected {name} remove error")
                            original_replace(path, content, mode)
                        context = patch.object(installer, "replace_file", fail_restore)
                    with context:
                        with self.assertRaisesRegex(OSError, f"injected {name} remove error"):
                            self.run_driver_action("remove")
                    self.assert_driver_snapshot(installed)
                    self.assertEqual(self.run_driver_action("check"), "installed")
                self.assertEqual(self.run_driver_action("remove"), "absent")
                self.assert_driver_snapshot(baseline)
            if hooked:
                self.assertEqual(self.run_ordinary_action("check"), "installed")
                self.assertIn(installer.HOOKS["Kconfig"], self.kconfig_file.read_bytes())


class DriverInstallCLITest(DriverInstallBaseTest):
    def test_cli_check_install_remove_lifecycle(self):
        cmd_base = [
            "python3", "-B", str(PACKAGE_DIR / "tools/install.py")
        ]

        # 1. Check absent: exit code 1
        proc = subprocess.run(cmd_base + ["check-driver-api", str(self.root)], capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("absent", proc.stdout)

        # 2. Install: exit code 0
        proc = subprocess.run(cmd_base + ["install-driver-api", str(self.root)], capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("installed", proc.stdout)

        # 3. Check installed: exit code 0
        proc = subprocess.run(cmd_base + ["check-driver-api", str(self.root)], capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("installed", proc.stdout)

        # 4. Remove: exit code 0
        proc = subprocess.run(cmd_base + ["remove-driver-api", str(self.root)], capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("absent", proc.stdout)

        # 5. Check absent: exit code 1
        proc = subprocess.run(cmd_base + ["check-driver-api", str(self.root)], capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("absent", proc.stdout)

        # 6. Error case: exit code 2
        proc = subprocess.run(cmd_base + ["check-driver-api", str(self.base / "nonexistent")], capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("Klipper-MMU:", proc.stderr)


class DriverInstallIndependentTest(DriverInstallBaseTest):
    def test_nondefault_modes_roundtrip(self):
        self.c_file.chmod(0o640)
        self.pwm_c_file.chmod(0o640)
        self.adc_c_file.chmod(0o640)
        self.kconfig_file.chmod(0o600)
        for path in self.gpio_sources:
            path.chmod(0o640)
        for action in ("install", "remove"):
            self.run_driver_action(action)
            self.assertEqual(stat.S_IMODE(self.c_file.stat().st_mode), 0o640)
            self.assertEqual(stat.S_IMODE(self.pwm_c_file.stat().st_mode), 0o640)
            self.assertEqual(stat.S_IMODE(self.adc_c_file.stat().st_mode), 0o640)
            self.assertEqual(stat.S_IMODE(self.kconfig_file.stat().st_mode), 0o600)
            for path in self.gpio_sources:
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)

    def test_nonempty_corrupt_patch_refused_even_for_check(self):
        path = self.package / "upstream/drivers-local.patch"
        path.write_bytes(path.read_bytes() + b"\nUnexpected trailing data\n")
        for action in ("install", "remove", "check"):
            with self.assertRaisesRegex(RuntimeError, "altered reviewed"):
                self.run_driver_action(action)
        self.assert_pristine()

    def test_assets_cannot_be_redirected_or_replaced_by_root_copies(self):
        for name in installer.DRIVER_API_ASSETS:
            source = self.package / "upstream" / name
            outside = self.base / name
            shutil.copy2(source, outside)
            shutil.copy2(source, self.package / name)
            source.unlink()
            source.symlink_to(outside)
            with self.assertRaisesRegex(RuntimeError, "regular source file"):
                self.run_driver_action("install")
            source.unlink()
            shutil.copy2(outside, source)
        self.assert_pristine()

    def test_patch_timeout_preserves_checkout(self):
        with patch.object(installer.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired("patch", 10)):
            with self.assertRaisesRegex(RuntimeError, "staging failed"):
                self.run_driver_action("install")
        self.assert_pristine()

    def test_missing_patch_tool_preserves_checkout(self):
        with patch.object(installer.subprocess, "run", side_effect=FileNotFoundError("patch")):
            with self.assertRaisesRegex(RuntimeError, "staging failed"):
                self.run_driver_action("install")
        self.assert_pristine()

    def test_concurrent_header_arrival_preserved(self):
        replace = installer.replace_file
        def edit_after_pwm_c(path, content, mode):
            replace(path, content, mode)
            if path.name == "hard_pwm.c":
                self.pwm_h_file.write_bytes(b"user header")
        with patch.object(installer, "replace_file", edit_after_pwm_c):
            with self.assertRaisesRegex(RuntimeError, "changed during"):
                self.run_driver_action("install")
        self.assertEqual(self.pwm_h_file.read_bytes(), b"user header")
        self.assertEqual(self.c_file.read_bytes(), self.original_c_bytes)
        self.assertEqual(self.pwm_c_file.read_bytes(), self.original_pwm_c_bytes)
        self.assertEqual(self.kconfig_file.read_bytes(), self.original_kconfig_bytes)

    def test_concurrent_mode_change_preserved_during_rollback(self):
        replace = installer.replace_file
        def fail_header(path, content, mode):
            if path.name == "pwm.h":
                self.c_file.chmod(0o600)
                raise OSError("pwm.h failed")
            replace(path, content, mode)
        with patch.object(installer, "replace_file", fail_header):
            with self.assertRaisesRegex(RuntimeError, "rollback incomplete"):
                self.run_driver_action("install")
        self.assertEqual(stat.S_IMODE(self.c_file.stat().st_mode), 0o600)
        self.assertEqual(self.kconfig_file.read_bytes(), self.original_kconfig_bytes)
        self.assertEqual(self.pwm_c_file.read_bytes(), self.original_pwm_c_bytes)

    def test_concurrent_header_edit_preserved_during_remove(self):
        self.run_driver_action("install")
        before_c, before_k, before_pwm_c = self.c_file.read_bytes(), self.kconfig_file.read_bytes(), self.pwm_c_file.read_bytes()
        replace = installer.replace_file
        def edit_after_hard_pwm(path, content, mode):
            replace(path, content, mode)
            if path.name == "hard_pwm.c":
                self.pwm_h_file.write_bytes(b"user edit after install")
        with patch.object(installer, "replace_file", edit_after_hard_pwm):
            with self.assertRaisesRegex(RuntimeError, "changed during"):
                self.run_driver_action("remove")
        self.assertEqual(self.pwm_h_file.read_bytes(), b"user edit after install")
        self.assertEqual(self.c_file.read_bytes(), before_c)
        self.assertEqual(self.pwm_c_file.read_bytes(), before_pwm_c)
        self.assertEqual(self.kconfig_file.read_bytes(), before_k)

    def test_unrelated_files_untouched(self):
        paths = [self.root / ".config", self.src / "unrelated.c", self.src / "Makefile"]
        for path in paths:
            path.write_bytes(b"user file\n")
        before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
        self.run_driver_action("install")
        self.run_driver_action("remove")
        self.assertEqual(before, [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths])



class ChangedUpstreamKconfigTest(DriverInstallBaseTest):
    """src/Kconfig is hook-owned: other upstream versions of it are accepted."""

    def changed_kconfig(self):
        # Like Klipper 57a018a against the pinned revision: the thirteen driver
        # targets match, while unrelated src/Kconfig declarations differ.
        changed = self.original_kconfig_bytes.replace(
            b"config WANT_ADC\n", b"config WANT_UPSTREAM_EXAMPLE\n    bool\n    default y\n"
            b"config WANT_ADC\n", 1)
        self.assertNotEqual(changed, self.original_kconfig_bytes)
        return changed

    def test_changed_kconfig_with_matching_targets_round_trips(self):
        for hooked in (False, True):
            with self.subTest(hooked=hooked):
                self.reset_driver_tree()
                changed = self.changed_kconfig()
                self.kconfig_file.write_bytes(changed)
                expected = changed
                if hooked:
                    self.assertEqual(self.run_ordinary_action("install"), "installed")
                    expected += installer.HOOKS["Kconfig"]
                pristine = self.snapshot_driver_targets()
                self.assertEqual(self.run_driver_action("check"), "absent")
                self.assertEqual(self.run_driver_action("install"), "installed")
                self.assertEqual(self.run_driver_action("check"), "installed")
                manifest, _ = installer.load_driver_api_assets(self.package)
                for name in installer.DRIVER_API_TARGETS:
                    self.assertEqual(installer.hashlib.sha256(
                        (self.root / name).read_bytes()).hexdigest(), manifest[name]["after"])
                self.assertEqual(self.kconfig_file.read_bytes(), expected)
                self.assertEqual(self.run_driver_action("remove"), "absent")
                self.assert_driver_snapshot(pristine)
                if hooked:
                    self.assertEqual(self.run_ordinary_action("remove"), "absent")
                    self.assertEqual(self.kconfig_file.read_bytes(), changed)

    def test_kconfig_update_after_install_keeps_driver_state(self):
        self.assertEqual(self.run_ordinary_action("install"), "installed")
        self.assertEqual(self.run_driver_action("install"), "installed")
        updated = self.changed_kconfig() + installer.HOOKS["Kconfig"]
        self.kconfig_file.write_bytes(updated)
        self.assertEqual(self.run_driver_action("check"), "installed")
        self.assertEqual(self.run_ordinary_action("check"), "installed")
        self.assertEqual(self.run_driver_action("remove"), "absent")
        self.assertEqual(self.kconfig_file.read_bytes(), updated)
        self.assertEqual(self.run_ordinary_action("remove"), "absent")
        self.assertEqual(self.kconfig_file.read_bytes(), self.changed_kconfig())

    def test_foreign_option_declaration_refused(self):
        # A second declaration would duplicate the package-owned option.
        for name in DRIVER_API_SYMBOLS:
            with self.subTest(name=name):
                edited = self.original_kconfig_bytes + (
                    b"\nconfig " + name.encode() + b"\n    bool\n")
                self.kconfig_file.write_bytes(edited)
                snapshots = self.snapshot_driver_targets()
                for action in ("check", "install", "remove"):
                    with self.assertRaisesRegex(RuntimeError, "declares driver API options"):
                        self.run_driver_action(action)
                    self.assert_driver_snapshot(snapshots)
        # Mentions outside a declaration are not declarations.
        mention = self.original_kconfig_bytes + b"\n# WANT_GPIO_LOCAL is package-owned\n"
        self.kconfig_file.write_bytes(mention)
        self.assertEqual(self.run_driver_action("install"), "installed")
        self.assertEqual(self.kconfig_file.read_bytes(), mention)


class PreviousBundleTest(DriverInstallBaseTest):
    """The earlier twelve-path bundle declared its options in src/Kconfig."""

    HOOK_VARIANTS = (None, "current", "old")

    def install_previous(self, hook):
        self.reset_driver_tree()
        self.assertEqual(self.run_driver_action("install"), "installed")
        block = b""
        if hook == "current":
            self.assertEqual(self.run_ordinary_action("install"), "installed")
            block = installer.HOOKS["Kconfig"]
        elif hook == "old":
            (self.src / "klipper_mmu").symlink_to(self.package, target_is_directory=True)
            makefile = self.src / "Makefile"
            makefile.write_bytes(makefile.read_bytes() + installer.OLD_HOOKS["Makefile"])
            block = installer.OLD_HOOKS["Kconfig"]
        self.kconfig_file.write_bytes(self.previous_kconfig() + block)
        return block

    def test_previous_assets_are_this_bundle_plus_the_kconfig_hunk(self):
        upstream = PACKAGE_DIR / "upstream"
        hunk = (upstream / "previous-kconfig.patch").read_bytes()
        self.assertEqual(installer.hashlib.sha256(hunk).hexdigest(),
                         installer.PREVIOUS_KCONFIG_PATCH[1])
        bundle = (upstream / "drivers-local.patch").read_bytes()
        split = bundle.index(b"--- /dev/null\n+++ b/src/tmcuart.h\n")
        # This bundle's stepper paths came later: the previous bundle had no
        # such sections, so they are dropped before the Kconfig hunk is added.
        end = bundle.index(b"--- a/src/stepper.c\n")
        self.assertEqual(installer.hashlib.sha256(bundle[:split] + hunk + bundle[split:end]).hexdigest(),
                         "73679da4d1f1db5a5e96c5dc05ccf75ec8495500edc44cf30d9bceedc676bae7")
        manifest = (upstream / "source-hashes.json").read_bytes()
        entry = b'  "src/tmcuart.h": {\n'
        kconfig = ('  "src/Kconfig": {\n    "before": "%s",\n    "after": "%s"\n  },\n'
                   % installer.PREVIOUS_KCONFIG_HASHES).encode()
        # Drop the same two entries and close the object after the shared ones.
        previous = manifest[:manifest.index(b'  "src/stepper.c": {\n')][:-2] + b"\n}\n"
        self.assertEqual(installer.hashlib.sha256(previous.replace(entry, kconfig + entry, 1)).hexdigest(),
                         "283c42c6c9659743b26341822403e0eaa12a1b7a3ff168a77ce4173d619efa12")
        self.assertEqual(installer.hashlib.sha256(self.original_kconfig_bytes).hexdigest(),
                         installer.PREVIOUS_KCONFIG_HASHES[0])

    def test_check_reports_previous_bundle(self):
        for hook in self.HOOK_VARIANTS:
            with self.subTest(hook=hook):
                self.install_previous(hook)
                snapshots = self.snapshot_driver_targets()
                with self.assertRaisesRegex(RuntimeError, "Previous driver API bundle.*"
                                            "install-driver-api.*remove-driver-api"):
                    self.run_driver_action("check")
                self.assert_driver_snapshot(snapshots)
        proc = subprocess.run(["python3", "-B", str(PACKAGE_DIR / "tools/install.py"),
                               "check-driver-api", str(self.root)],
                              capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("Previous driver API bundle", proc.stderr)

    def test_install_upgrades_previous_bundle_in_place(self):
        for hook in self.HOOK_VARIANTS:
            with self.subTest(hook=hook):
                block = self.install_previous(hook)
                self.kconfig_file.chmod(0o640)
                drivers = {name: installer.read_driver_file(self.root / name)
                           for name in installer.DRIVER_API_TARGETS}
                self.assertEqual(self.run_driver_action("install"), "installed")
                self.assertEqual(self.kconfig_file.read_bytes(), self.original_kconfig_bytes + block)
                self.assertEqual(stat.S_IMODE(self.kconfig_file.stat().st_mode), 0o640)
                self.kconfig_file.chmod(self.original_kconfig_mode)
                self.assertEqual({name: installer.read_driver_file(self.root / name)
                                  for name in installer.DRIVER_API_TARGETS}, drivers)
                self.assertEqual(self.run_driver_action("check"), "installed")
                if hook == "old":
                    self.assertEqual(self.run_ordinary_action("install"), "installed")
                    block = installer.HOOKS["Kconfig"]
                self.assertEqual(self.run_driver_action("remove"), "absent")
                self.assertEqual(self.kconfig_file.read_bytes(), self.original_kconfig_bytes + block)
                if hook:
                    self.assertEqual(self.run_ordinary_action("remove"), "absent")
                self.assert_pristine()

    def test_remove_restores_pristine_sources(self):
        for hook in self.HOOK_VARIANTS:
            with self.subTest(hook=hook):
                block = self.install_previous(hook)
                self.assertEqual(self.run_driver_action("remove"), "absent")
                self.assertEqual(self.kconfig_file.read_bytes(), self.original_kconfig_bytes + block)
                self.assertEqual(self.run_driver_action("check"), "absent")
                if hook:
                    self.assertEqual(self.run_ordinary_action("remove"), "absent")
                self.assert_pristine()

    def test_edited_or_partial_previous_state_refused(self):
        self.install_previous(None)
        previous = self.kconfig_file.read_bytes()
        block = installer.HOOKS["Kconfig"]
        split = previous.index(b"config WANT_BUTTONS\n")
        for content, message in (
                (previous + b"\n# user edit\n", "declares driver API options"),
                (previous[:split] + block[1:] + previous[split:], "declares driver API options")):
            with self.subTest(message=message, size=len(content)):
                self.kconfig_file.write_bytes(content)
                snapshots = self.snapshot_driver_targets()
                for action in ("check", "install", "remove"):
                    with self.assertRaisesRegex(RuntimeError, message):
                        self.run_driver_action(action)
                    self.assert_driver_snapshot(snapshots)
        # The previous declarations with pristine drivers are a mixed state.
        self.reset_driver_tree()
        self.kconfig_file.write_bytes(previous)
        snapshots = self.snapshot_driver_targets()
        for action in ("check", "install", "remove"):
            with self.assertRaisesRegex(RuntimeError, "Unsupported, modified or mixed"):
                self.run_driver_action(action)
            self.assert_driver_snapshot(snapshots)

    def test_failed_kconfig_write_rolls_back_every_target(self):
        replace = installer.replace_file
        def fail_kconfig(path, content, mode):
            if path.name == "Kconfig":
                raise OSError("injected Kconfig write error")
            replace(path, content, mode)
        for hook in self.HOOK_VARIANTS:
            for action in ("install", "remove"):
                with self.subTest(hook=hook, action=action):
                    self.install_previous(hook)
                    snapshots = self.snapshot_driver_targets()
                    with patch.object(installer, "replace_file", fail_kconfig):
                        with self.assertRaisesRegex(OSError, "injected Kconfig write error"):
                            self.run_driver_action(action)
                    self.assert_driver_snapshot(snapshots)

    def test_concurrent_kconfig_edit_preserved(self):
        stage = installer.stage_previous_kconfig
        def edit_during_staging(content, package):
            result = stage(content, package)
            self.kconfig_file.write_bytes(b"concurrent user edit\n")
            return result
        for action in ("install", "remove"):
            with self.subTest(action=action):
                self.install_previous("current")
                drivers = {name: installer.read_driver_file(self.root / name)
                           for name in installer.DRIVER_API_TARGETS}
                with patch.object(installer, "stage_previous_kconfig", edit_during_staging):
                    with self.assertRaisesRegex(RuntimeError, "changed during installation"):
                        self.run_driver_action(action)
                self.assertEqual(self.kconfig_file.read_bytes(), b"concurrent user edit\n")
                self.assertEqual({name: installer.read_driver_file(self.root / name)
                                  for name in installer.DRIVER_API_TARGETS}, drivers)

    def test_altered_previous_hunk_refused_but_not_needed_otherwise(self):
        hunk = self.package / "upstream/previous-kconfig.patch"
        hunk.write_bytes(hunk.read_bytes() + b"\n")
        self.install_previous(None)
        snapshots = self.snapshot_driver_targets()
        for action in ("install", "remove"):
            with self.assertRaisesRegex(RuntimeError, "altered reviewed driver API asset"):
                self.run_driver_action(action)
            self.assert_driver_snapshot(snapshots)
        hunk.unlink()
        self.reset_driver_tree()
        self.assertEqual(self.run_driver_action("install"), "installed")
        self.assertEqual(self.run_driver_action("remove"), "absent")
        self.assert_pristine()


class DriverApiKconfigOwnershipTest(DriverInstallBaseTest):
    """The package declares the options exactly as the previous src/Kconfig hunk did."""

    @staticmethod
    def stanzas(text):
        result, name = {}, None
        for line in text.splitlines():
            match = re.match(r"config (\w+)$", line)
            if match:
                name = match.group(1)
                result[name] = []
            elif name and line.strip() and not line.startswith("#"):
                result[name].append(line)
        return result

    def test_package_declares_previous_options_verbatim(self):
        hunk = (PACKAGE_DIR / "upstream/previous-kconfig.patch").read_text()
        added = "\n".join(line[1:] for line in hunk.splitlines()
                          if line.startswith("+") and not line.startswith("+++"))
        package = (PACKAGE_DIR / "mcu/Kconfig.driver_api").read_text()
        expected = self.stanzas(added)
        # Options declared after that bundle are package-owned additions.
        expected.update(self.stanzas(WANT_STEPPER_LOCAL_STANZA.decode()))
        self.assertEqual(sorted(expected), sorted(DRIVER_API_SYMBOLS))
        self.assertEqual(self.stanzas(package), expected)
        self.assertEqual(len(installer.DRIVER_API_DECLARATION.findall(package.encode())),
                         len(DRIVER_API_SYMBOLS))
        self.assertIn("Kconfig.driver_api", installer.REQUIRED_MCU_FILES)

    def test_declarations_are_sourced_outside_the_mmu_menu(self):
        menu = (PACKAGE_DIR / "mcu/Kconfig").read_text()
        source = 'source "src/klipper_mmu/mcu/Kconfig.driver_api"\n'
        self.assertEqual(menu.count(source), 1)
        self.assertLess(menu.index(source), menu.index("menuconfig KLIPPER_MMU\n"))
        for name in DRIVER_API_SYMBOLS:
            self.assertNotRegex(menu, r"(?m)^config %s$" % name)

    def test_disabled_build_equivalence(self):
        # Without the add-on hook these options are undefined. Klipper's
        # autoconf.h otherwise defines disabled options (and their inactive
        # capacities) as 0. Every installed source must preprocess the same.
        self.assertEqual(self.run_driver_action("install"), "installed")
        disabled = {"CONFIG_" + name: "0" for name in DRIVER_API_SYMBOLS}
        enabled = {"CONFIG_" + name: "8" if "MAX" in name else "1" for name in DRIVER_API_SYMBOLS}
        contexts = {
            "none": {},
            "h723": {"CONFIG_MACH_STM32": "1", "CONFIG_MACH_STM32H7": "1",
                     "CONFIG_MACH_STM32H723": "1", "CONFIG_WANT_ADC": "1",
                     "CONFIG_WANT_HARD_PWM": "1", "CONFIG_WANT_TMCUART": "1"},
            "f407": {"CONFIG_MACH_STM32": "1", "CONFIG_MACH_STM32F4": "1",
                     "CONFIG_MACH_STM32F407": "1", "CONFIG_WANT_ADC": "1",
                     "CONFIG_WANT_HARD_PWM": "1", "CONFIG_WANT_TMCUART": "1"},
        }
        work = self.base / "preprocess"
        work.mkdir()

        def preprocess(name, macros):
            # Includes are dropped: their macros would be the same in each run.
            text = re.sub(r"(?m)^[ \t]*#[ \t]*include\b.*$", "",
                          (self.root / name).read_text())
            source = work / "unit.c"
            source.write_text(text)
            result = subprocess.run(
                ["gcc", "-E", "-P", "-x", "c", str(source)]
                + ["-D%s=%s" % item for item in sorted(macros.items())],
                capture_output=True, text=True, timeout=10)
            return result.returncode, result.stdout, result.stderr

        changed = set()
        for context, macros in contexts.items():
            for name in installer.DRIVER_API_TARGETS:
                with self.subTest(context=context, name=name):
                    undefined = preprocess(name, macros)
                    self.assertEqual(undefined[0], 0, name + ": " + undefined[2])
                    self.assertEqual(undefined, preprocess(name, {**macros, **disabled}))
                    # Enabled options may also reach an intended #error guard.
                    if undefined != preprocess(name, {**macros, **enabled}):
                        changed.add(name)
        # Not vacuous: each option group changes some installed source.
        for name in ("src/tmcuart.c", "src/stm32/hard_pwm.c", "src/stm32/stm32h7_adc.c",
                     "src/stm32/stm32h7_gpio.c", "src/stm32/gpio.c", "src/stm32/gpioperiph.c"):
            self.assertIn(name, changed)


if __name__ == "__main__":
    unittest.main()
