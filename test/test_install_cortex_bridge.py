"""install.sh and the Cortex Bridge relay, against fake trees.

The installer runs with OPENAMS_NO_SERVICE=1, OPENAMS_SYSTEM_ROOT pointing at a
scratch folder (where /etc files land instead of the real /etc) and
OPENAMS_SYSFS_USB at a fake USB device tree, so nothing on this machine is
touched and its own USB devices are never looked at.
"""
import getpass
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.sh"


class CortexBridgeInstallTests(unittest.TestCase):
    def setUp(self):
        if os.geteuid() == 0:
            self.skipTest("install.sh intentionally refuses to run as root")
        self.temp = tempfile.TemporaryDirectory(prefix="openams-bridge-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.klipper = self.root / "klipper"
        self.moonraker = self.root / "moonraker"
        self.printer_data = self.root / "printer_data"
        self.config = self.printer_data / "config"
        self.sysroot = self.root / "sysroot"
        self.usb = self.root / "usb"
        (self.klipper / "klippy/extras").mkdir(parents=True)
        (self.klipper / "scripts").mkdir()
        (self.moonraker / "moonraker/components").mkdir(parents=True)
        self.config.mkdir(parents=True)
        (self.printer_data / "logs").mkdir()
        (self.printer_data / "moonraker.asvc").write_text(
            "klipper_mcu\nmoonraker\nklipper\n")
        (self.config / "moonraker.conf").write_text("[server]\n")
        (self.config / "printer.cfg").write_text(
            "[printer]\nkinematics: none\n")
        self.usb.mkdir()
        self.env = dict(
            os.environ,
            OPENAMS_NO_SERVICE="1",
            OPENAMS_SYSTEM_ROOT=str(self.sysroot),
            OPENAMS_SYSFS_USB=str(self.usb),
            OPENAMS_BRIDGE_PYTHON="/usr/bin/python3",
            MOONRAKER_PATH=str(self.moonraker),
        )

    def plug_in_bridge(self, pid="fe11", product="Cortex Bridge"):
        device = self.usb / "1-7"
        device.mkdir()
        for name, value in (("idVendor", "1a86"), ("idProduct", pid),
                            ("product", product),
                            ("serial", "CB0123456789AB")):
            (device / name).write_text(value + "\n")

    def install(self, *extra, answer="n"):
        result = subprocess.run(
            ["bash", str(INSTALLER), "-k", str(self.klipper),
             "-c", str(self.config), "-s", "klipper-test",
             "--skip-gco-routines", "--no-spoolman", *extra],
            env=self.env, input=answer + "\n", universal_newlines=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
        return result

    def system_file(self, path):
        return self.sysroot / path.lstrip("/")

    def assertInstalled(self, result):
        self.assertEqual(result.returncode, 0, result.stdout)
        unit = self.system_file("/etc/systemd/system/cortex-bridge.service")
        text = unit.read_text()
        self.assertIn("User=%s" % getpass.getuser(), text)
        self.assertIn("WorkingDirectory=%s" % ROOT, text)
        self.assertIn(
            "ExecStart=/usr/bin/python3 -m cortex_bridge.relay --log-file "
            "%s/cortex-bridge.jsonl" % (self.printer_data / "logs"), text)
        self.assertIn("Before=klipper-test.service", text)
        self.assertNotIn("@", text.split("[Service]")[1].replace(
            "systemd-tmpfiles-setup", ""), "unrendered placeholder")
        dropin = self.system_file(
            "/etc/systemd/system/klipper-test.service.d/cortex-bridge.conf")
        self.assertIn("Wants=cortex-bridge.service", dropin.read_text())
        self.assertNotIn("Requires=", dropin.read_text())
        self.assertTrue(self.system_file(
            "/etc/udev/rules.d/99-cortex-bridge.rules").is_file())
        self.assertIn("d /dev/oams 2775 root dialout", self.system_file(
            "/etc/tmpfiles.d/cortex-bridge.conf").read_text())
        conf = (self.config / "moonraker.conf").read_text()
        self.assertEqual(conf.count("[openams_bridge]"), 1)
        self.assertIn("managed_services: cortex-bridge klipper", conf)
        link = self.moonraker / "moonraker/components/openams_bridge"
        self.assertEqual(link.resolve(), (ROOT / "moonraker/openams_bridge"))
        asvc = (self.printer_data / "moonraker.asvc").read_text().split()
        self.assertEqual(asvc.count("cortex-bridge"), 1)
        self.assertIn("[skipped: OPENAMS_NO_SERVICE=1] systemctl enable "
                      "cortex-bridge", result.stdout)

    def test_a_connected_bridge_gets_its_relay_and_sample(self):
        self.plug_in_bridge()
        result = self.install()
        self.assertInstalled(result)
        oams = (self.config / "oams.cfg").read_text()
        self.assertIn("printer with a Cortex Bridge", oams)
        self.assertNotIn("canbus_uuid", oams)
        self.assertIn("config --write --yes", result.stdout)

    def test_a_bridge_waiting_for_firmware_also_counts(self):
        self.plug_in_bridge(pid="fe10", product="Cortex Updater")
        self.assertInstalled(self.install())

    def test_no_bridge_means_no_relay(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("No Cortex Bridge connected", result.stdout)
        self.assertFalse(self.sysroot.exists())
        self.assertIn("canbus_uuid", (self.config / "oams.cfg").read_text())
        self.assertNotIn("openams_bridge",
                         (self.config / "moonraker.conf").read_text())

    def test_flags_override_detection(self):
        result = self.install("--cortex-bridge")
        self.assertInstalled(result)

    def test_no_cortex_bridge_skips_a_connected_bridge(self):
        self.plug_in_bridge()
        result = self.install("--no-cortex-bridge")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertFalse(self.sysroot.exists())

    def test_the_two_flags_cannot_be_combined(self):
        result = self.install("--cortex-bridge", "--no-cortex-bridge")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot be combined", result.stdout)

    def test_running_it_again_changes_nothing_twice(self):
        self.plug_in_bridge()
        self.assertInstalled(self.install())
        conf = (self.config / "moonraker.conf").read_text()
        asvc = (self.printer_data / "moonraker.asvc").read_text()
        self.assertInstalled(self.install())
        self.assertEqual((self.config / "moonraker.conf").read_text(), conf)
        self.assertEqual((self.printer_data / "moonraker.asvc").read_text(),
                         asvc)

    def test_an_installed_relay_is_kept_up_to_date_when_unplugged(self):
        self.plug_in_bridge()
        self.assertInstalled(self.install())
        (self.usb / "1-7" / "idProduct").write_text("0000\n")
        self.assertInstalled(self.install())

    def test_uninstall_removes_the_relay(self):
        self.plug_in_bridge()
        self.assertInstalled(self.install())
        result = self.install("-u", answer="n")
        self.assertEqual(result.returncode, 0, result.stdout)
        for path in ("/etc/systemd/system/cortex-bridge.service",
                     "/etc/systemd/system/klipper-test.service.d/"
                     "cortex-bridge.conf",
                     "/etc/udev/rules.d/99-cortex-bridge.rules",
                     "/etc/tmpfiles.d/cortex-bridge.conf"):
            self.assertFalse(self.system_file(path).exists(), path)
        self.assertFalse((self.moonraker /
                          "moonraker/components/openams_bridge").exists())
        self.assertNotIn("openams_bridge",
                         (self.config / "moonraker.conf").read_text())
        self.assertNotIn("cortex-bridge",
                         (self.printer_data / "moonraker.asvc").read_text())


if __name__ == "__main__":
    unittest.main()
