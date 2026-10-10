"""Cortex Bridge setup tools: config writer, identify, firmware list, CLI."""

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
import zlib

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cortex_bridge import cli, config, firmware, identify  # noqa: E402

AMS2 = {"uid": "0a1b2c3d4e5f", "family": "ams2", "bays": 4,
        "name": "AMS 2 Pro"}
HT = {"uid": "5f4e3d2c1b0a", "family": "ams_ht", "bays": 1,
      "name": "AMS HT"}


class ConfigWriterTests(unittest.TestCase):
    def test_units_get_family_names_tools_and_one_fps_lane(self):
        text, names = config.render_units([AMS2, HT])
        self.assertEqual(names, ["ams2", "ams_ht"])
        self.assertIn("[mcu ams2]\nserial: /dev/oams/0a1b2c3d4e5f", text)
        self.assertIn("device_kind: ams2", text)
        self.assertIn("f1s_hes_on: 0.5, 0.5, 0.5, 0.5", text)
        self.assertIn("hub_hes_on: 0.5\n", text)
        for tool, group in ((0, "ams2-0"), (3, "ams2-3"), (4, "ams_ht-0")):
            self.assertIn("[filament_group T%d]\ngroup: %s" % (tool, group),
                          text)
        self.assertEqual(text.count("[fps]"), 1)
        self.assertIn("source_oams: ams2", text)
        self.assertIn("oams_idx: 2", text)

    def test_two_of_a_family_get_distinct_names(self):
        second = dict(AMS2, uid="0123456789ab")
        _, names = config.render_units([AMS2, second])
        self.assertEqual(names, ["ams2", "ams2_2"])

    def test_adding_a_unit_keeps_what_is_configured(self):
        sample = (ROOT / "oams_cortex_bridge_sample.cfg").read_text()
        first, names = config.add_units(sample, [AMS2])
        self.assertEqual(names, ["ams2"])
        calibrated = first.replace("ptfe_length: 0", "ptfe_length: 2337", 1)
        again, names = config.add_units(calibrated, [AMS2])
        self.assertEqual((again, names), (calibrated, []))
        second, names = config.add_units(calibrated, [AMS2, HT])
        self.assertEqual(names, ["ams_ht"])
        self.assertTrue(second.startswith(calibrated.rstrip("\n")))
        self.assertIn("ptfe_length: 2337", second)
        self.assertIn("[filament_group T4]\ngroup: ams_ht-0", second)
        self.assertIn("oams_idx: 2", second)
        self.assertEqual(second.count("[fps]"), 1)

    def test_the_can_sample_is_never_appended_to(self):
        can_sample = (ROOT / "oams_sample.cfg").read_text()
        with self.assertRaises(config.ConfigError):
            config.add_units(can_sample, [AMS2])

    def test_ptfe_length_is_in_encoder_clicks(self):
        self.assertEqual(config.mm_to_clicks(2050), 2337)
        with self.assertRaises(config.ConfigError):
            config.mm_to_clicks(0)

    def test_writing_keeps_a_symlinked_config_a_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "real.cfg"
            real.write_text("old\n")
            link = Path(tmp) / "oams.cfg"
            link.symlink_to(real)
            backup = config.write_with_backup(link, "new\n")
            self.assertTrue(link.is_symlink())
            self.assertEqual(real.read_text(), "new\n")
            self.assertEqual(backup.read_text(), "old\n")

    def test_the_bridge_sample_has_no_can_hardware(self):
        sample = (ROOT / "oams_cortex_bridge_sample.cfg").read_text()
        for absent in ("canbus_uuid", "[mcu fps]", "oams_mcu1", "mfrc522",
                       "[hdc1080]"):
            self.assertNotIn(absent, sample)
        self.assertIn("[include oams_macros.cfg]", sample)
        self.assertIn("toolhead_unload_macro: _OAMS_TOOLHEAD_EJECT", sample)


def fake_mcu(fd, dictionary, *, nak_first=False, stop):
    """Answer Klipper identify requests on `fd` like an AMS would."""
    data = zlib.compress(json.dumps(dictionary).encode())
    buffer = bytearray()
    expected = 0
    naked = not nak_first
    while not stop.is_set():
        try:
            chunk = os.read(fd, 256)
        except OSError:
            return
        if not chunk:
            return
        buffer.extend(chunk)
        for block in identify.split_blocks(buffer):
            sequence = block[1] & 0x0F
            if not naked:
                naked = True
                expected = 5
                os.write(fd, identify.encode_block(expected, b""))
                continue
            if sequence != expected:
                os.write(fd, identify.encode_block(expected, b""))
                continue
            payload = block[2:-3]
            _msg, pos = identify.decode_vlq(payload, 0)
            offset, pos = identify.decode_vlq(payload, pos)
            count, pos = identify.decode_vlq(payload, pos)
            piece = data[offset:offset + count]
            expected = (expected + 1) & 0x0F
            reply = identify.encode_vlq(0) + identify.encode_vlq(offset) + \
                bytes((len(piece),)) + piece
            os.write(fd, identify.encode_block(expected, b""))
            os.write(fd, identify.encode_block(expected, reply))


class IdentifyTests(unittest.TestCase):
    def describe(self, dictionary, nak_first=False):
        master, slave = os.openpty()
        import tty
        tty.setraw(master)
        tty.setraw(slave)
        stop = threading.Event()
        thread = threading.Thread(target=fake_mcu, args=(master, dictionary),
                                  kwargs={"nak_first": nak_first,
                                          "stop": stop}, daemon=True)
        thread.start()
        try:
            return identify.describe_unit(os.ttyname(slave))
        finally:
            stop.set()
            os.close(slave)
            os.close(master)

    def test_family_and_bays_come_from_the_dictionary(self):
        facts = self.describe({"config": {"AMS_FAMILY": "ams2",
                                          "AMS_BAYS": 4},
                               "version": "cortex-bridge-1"})
        self.assertEqual(facts, {"family": "ams2", "bays": 4,
                                 "name": "AMS 2 Pro",
                                 "version": "cortex-bridge-1"})

    def test_a_sequence_correction_is_followed(self):
        facts = self.describe({"config": {"BOARD_NAME": "ams-ht",
                                          "AMS_BAYS": 1}}, nak_first=True)
        self.assertEqual((facts["family"], facts["bays"]), ("ams_ht", 1))


class FirmwareListTests(unittest.TestCase):
    def test_packages_are_checked_and_sorted(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "bridge-2.oac2").write_bytes(b"two")
            (folder / "bridge-3.oac2").write_bytes(b"three")
            manifest = {"schema": 1, "packages": [
                {"version": 2, "file": "bridge-2.oac2",
                 "sha256": hashlib.sha256(b"two").hexdigest()},
                {"version": 3, "file": "bridge-3.oac2", "sha256": "0" * 64},
                {"version": 4, "file": "missing.oac2", "sha256": "0" * 64},
            ]}
            (folder / "manifest.json").write_text(json.dumps(manifest))
            packages = firmware.bundled_packages(folder)
            self.assertEqual([p["version"] for p in packages], [4, 3, 2])
            self.assertEqual([p["problem"] for p in packages],
                             ["file missing", "checksum mismatch", ""])
            self.assertEqual(firmware.latest_bundled(folder)["version"], 2)

    def test_the_shipped_manifest_is_valid(self):
        for package in firmware.bundled_packages():
            self.assertTrue(package["ok"], package)


def fake_usb(root, name, pid, product, serial, manufacturer="SiForge",
             tty="ttyACM1"):
    device = Path(root) / name
    (device / (name + ":1.0") / "tty" / tty).mkdir(parents=True)
    for key, value in (("idVendor", "1a86"), ("idProduct", pid),
                       ("product", product), ("serial", serial),
                       ("manufacturer", manufacturer)):
        (device / key).write_text(value + "\n")


class CommandLineTests(unittest.TestCase):
    def test_usb_states_come_from_sysfs(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_usb(tmp, "1-7", "fe11", "Cortex Bridge", "CB0123456789AB",
                     manufacturer="OpenAMS")
            fake_usb(tmp, "1-8", "fe10", "Cortex Fault", "F-BOARD-01",
                     tty="ttyACM2")
            fake_usb(tmp, "1-9", "fe10", "Another Gadget", "x",
                     tty="ttyACM3")
            devices = cli.usb_devices(Path(tmp))
        self.assertEqual([(d["state"], d["device"]) for d in devices],
                         [("application", "/dev/ttyACM1"),
                          ("fault", "/dev/ttyACM2")])

    def facts(self, **overrides):
        facts = {
            "usb": [{"state": "application"}],
            "relay_service": "active",
            "relay": {"units": [{"uid": AMS2["uid"], "online": True}]},
            "configured": [AMS2["uid"]],
        }
        facts.update(overrides)
        return facts

    def test_next_step_names_the_first_problem(self):
        cases = (
            ({"relay_service": "not-installed"}, "install.sh"),
            ({"usb": []}, "USB-C cable"),
            ({"usb": [{"state": "fault"}]}, "fault"),
            ({"usb": [{"state": "receiver"}]}, "cortex-bridge.py update"),
            ({"relay_service": "failed"}, "systemctl start cortex-bridge"),
            ({"relay": {"units": []}}, "24 V"),
            ({"configured": []}, "config --write"),
            ({}, "Everything is connected"),
        )
        for overrides, expected in cases:
            self.assertIn(expected, cli.next_step(self.facts(**overrides)),
                          overrides)

    def test_ptfe_prints_the_click_count(self):
        import contextlib
        import io
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.main(["ptfe", "2050"])
        self.assertEqual(code, 0)
        self.assertIn("ptfe_length: 2337", output.getvalue())


if __name__ == "__main__":
    unittest.main()
