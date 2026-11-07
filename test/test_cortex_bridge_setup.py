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
BRIDGE_ON_USB = {"state": "application", "pid": "fe11",
                 "manufacturer": "SiForge", "product": "Cortex Bridge",
                 "serial": "CB0123456789AB", "device": "/dev/ttyACM9"}


def known(version):
    """The relay's firmware object for a bridge that reported ``version``."""
    from cortex_bridge import bridge_info
    return dict(bridge_info.firmware_status("known"), version=version,
                boot_abi=6, genuine="passed")


def found_with(version):
    """`cli.installed_firmware`'s answer for a running production bridge."""
    return {"usb": "application", "serial": "CB0123456789AB",
            "image": "production", "fault": "", "source": "relay",
            "firmware": known(version) if version is not None else None}


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


AMS1 = {"uid": "a1b2c3d4e5f6", "family": "ams1", "bays": 4, "name": "AMS 1"}

# An OpenAMS 1 oams.cfg: one unit on CAN with its sensors and readers, groups
# (one with a spare bay, one empty), a wired FPS, and the printer's own
# sections that must survive.
OPENAMS1_CFG = """[mcu oams_mcu1]
canbus_uuid: 0123456789ab
#canbus_uuid: ba9876543210

[hdc1080]
[temperature_sensor oams]
sensor_type: HDC1080
i2c_mcu: oams_mcu1
i2c_bus: i2c0

[oams oams1]
mcu: oams_mcu1
fps_upper_threshold: 0.7
ptfe_length: 2062
oams_idx: 1
spool_id_1: 1

[filament_group T0]
group: oams1-0

[filament_group T1]
group: oams1-1, oams1-3

[filament_group T3]
group:

[fps]
pin: fps:PA3
reversed: true

[oams_manager]

[filament_switch_sensor runout]
switch_pin: ^!PG13
pause_on_runout: False

[mfrc522 openams]
oams: 1
cs_pin_a: oams_mcu1:PB3
spi_software_sclk_pin: oams_mcu1:PA8

[gcode_macro _oams_macro_variables]
variable_cut_x: 7
gcode:

[include oams_macros.cfg]
"""


class ReplaceOpenAms1Tests(unittest.TestCase):
    """A printer moving from OpenAMS 1 to a Cortex Bridge: the writer must not
    mix the two setups, and --replace-openams1 swaps one for the other."""

    def test_adding_beside_openams1_is_refused_with_the_way_out(self):
        with self.assertRaises(config.ConfigError) as caught:
            config.add_units(OPENAMS1_CFG, [AMS1])
        message = str(caught.exception)
        self.assertIn("[oams oams1] (board oams_mcu1", message)
        self.assertIn("[fps] read from pin fps:PA3", message)
        self.assertIn("--replace-openams1", message)

    def test_replace_comments_the_openams1_setup_and_keeps_the_rest(self):
        text, replaced = config.replace_openams1(OPENAMS1_CFG)
        names = [name for name, _why in replaced]
        self.assertEqual(sorted(names), sorted([
            "mcu oams_mcu1", "hdc1080", "temperature_sensor oams",
            "oams oams1", "filament_group T0", "filament_group T1",
            "filament_group T3", "fps", "mfrc522 openams"]))
        active = {s["name"] for s in config._sections(text.splitlines())}
        self.assertEqual(active, {
            "oams_manager", "filament_switch_sensor runout",
            "gcode_macro _oams_macro_variables", "include oams_macros.cfg"})
        # Nothing is deleted: every old line is still there, commented.
        for line in OPENAMS1_CFG.splitlines():
            if line.strip() and not line.startswith("#"):
                self.assertTrue(line in text.splitlines()
                                or "#" + line in text.splitlines(), line)
        self.assertIn("#spool_id_1: 1", text)
        self.assertIn(config.REPLACED_MARK + "the buffer is read through", text)

    def test_after_replacing_the_bridge_unit_takes_the_place(self):
        text, _ = config.replace_openams1(OPENAMS1_CFG)
        text, names = config.add_units(text, [AMS1])
        self.assertEqual(names, ["ams1"])
        self.assertIn("oams_idx: 1", text)
        for tool in range(4):
            self.assertIn("[filament_group T%d]\ngroup: ams1-%d" % (tool, tool),
                          text)
        self.assertIn("[fps]\nsource_oams: ams1", text)
        self.assertEqual(config.openams1_hardware(text),
                         {"units": [], "wired_fps": [],
                          "bridge_units": ["ams1"]})

    def test_a_mixed_setup_is_left_for_a_human(self):
        mixed, _ = config.add_units(
            (ROOT / "oams_cortex_bridge_sample.cfg").read_text(), [AMS1])
        mixed += "\n[mcu oams_mcu2]\ncanbus_uuid: 0123456789ab\n" \
                 "\n[oams oams2]\nmcu: oams_mcu2\noams_idx: 2\n"
        with self.assertRaises(config.ConfigError) as caught:
            config.replace_openams1(mixed)
        self.assertIn("edited by hand", str(caught.exception))

    def test_the_bridge_sample_needs_no_replacing(self):
        sample = (ROOT / "oams_cortex_bridge_sample.cfg").read_text()
        self.assertEqual(config.replace_openams1(sample), (sample, []))


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

    def test_update_installs_directly_when_no_relay_runs(self):
        import contextlib
        import io
        from unittest import mock
        from cortex_bridge import bridge_info, updater
        seen = {}

        def install(data, progress=None):
            seen["data"] = data
            progress("writing", 50, 100)
            return {"version": 9, "serial_number": "CB0123456789AB",
                    "device": "/dev/ttyACM9"}

        asked = [known(7), known(9)]
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "bridge-9.oac2"
            package.write_bytes(b"OAC2 package")
            with mock.patch.object(cli, "control_request",
                                   side_effect=OSError("no relay")), \
                    mock.patch.object(cli, "printer_state", return_value=None), \
                    mock.patch.object(cli, "usb_devices",
                                      return_value=[BRIDGE_ON_USB]), \
                    mock.patch.object(bridge_info, "query_direct",
                                      side_effect=lambda device: asked.pop(0)), \
                    mock.patch.object(updater, "inspect_package",
                                      return_value={"version": 9}), \
                    mock.patch.object(updater, "update_bridge",
                                      side_effect=install), \
                    contextlib.redirect_stdout(output):
                code = cli.main(["--run-dir", tmp, "update", "--file",
                                 str(package), "--yes"])
        self.assertEqual(code, 0)
        self.assertEqual(seen["data"], b"OAC2 package")
        text = output.getvalue()
        self.assertIn("version 9 (the bridge runs version 7)", text)
        self.assertIn("Writing the firmware (50%)", text)
        self.assertIn("Installed firmware version 9 on bridge CB0123456789AB",
                      text)
        self.assertIn("The bridge now runs version 9.", text)
        self.assertEqual(asked, [], "asked before and after the install")

    def run_cli(self, argv, found=None, relay=None, inspect=None):
        """Run the CLI with USB, the relay and Moonraker faked."""
        import contextlib
        import io
        from unittest import mock
        from cortex_bridge import updater
        out, err = io.StringIO(), io.StringIO()
        calls = []

        def control(request, **kwargs):
            calls.append(request)
            if relay is None:
                raise OSError("no relay")
            return relay(request, **kwargs)

        patches = [
            mock.patch.object(cli, "control_request", side_effect=control),
            mock.patch.object(cli, "printer_state", return_value=None),
            mock.patch.object(cli, "usb_devices",
                              return_value=[BRIDGE_ON_USB]),
        ]
        if found is not None:
            patches.append(mock.patch.object(cli, "installed_firmware",
                                             return_value=found))
        if inspect is not None:
            patches.append(mock.patch.object(updater, "inspect_package",
                                             return_value=inspect))
        with tempfile.TemporaryDirectory() as tmp, \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            package = Path(tmp) / "bridge.oac2"
            package.write_bytes(b"OAC2 package")
            argv = [arg.replace("PACKAGE", str(package)) for arg in argv]
            with contextlib.ExitStack() as stack:
                for patch in patches:
                    stack.enter_context(patch)
                code = cli.main(["--run-dir", tmp] + argv)
        return code, out.getvalue(), err.getvalue(), calls

    def test_version_says_up_to_date_or_what_is_available(self):
        latest = firmware.latest_bundled()["version"]
        code, out, _, _ = self.run_cli(["version"], found=found_with(latest))
        self.assertEqual(code, 0)
        self.assertIn("Installed:  version %d" % latest, out)
        self.assertIn("Up to date.", out)
        code, out, _, _ = self.run_cli(["version"], found=found_with(1))
        self.assertEqual(code, 0)
        self.assertIn("Installed:  version 1", out)
        self.assertIn("Version %d is available" % latest, out)
        self.assertIn("cortex-bridge.py update", out)
        code, out, _, _ = self.run_cli(["version"],
                                       found=found_with(latest + 1))
        self.assertIn("newer firmware than this OpenAMS bundles", out)

    def test_version_explains_why_it_is_unknown(self):
        from cortex_bridge import bridge_info
        lab = dict(found_with(None), image="lab",
                   firmware=bridge_info.firmware_status("unavailable",
                                                        "lab-firmware"))
        code, out, _, _ = self.run_cli(["version"], found=lab)
        self.assertEqual(code, 1)
        self.assertIn("Installed:  unknown: this bridge runs lab", out)
        waiting = dict(found_with(None), usb="receiver", firmware=None)
        code, out, _, _ = self.run_cli(["version"], found=waiting)
        self.assertEqual(code, 1)
        self.assertIn("waiting for firmware", out)
        missing = dict(found_with(None), usb=None, firmware=None)
        code, out, _, _ = self.run_cli(["version"], found=missing)
        self.assertEqual(code, 1)
        self.assertIn("No Cortex Bridge is connected", out)

    def test_version_json_is_for_tools(self):
        latest = firmware.latest_bundled()["version"]
        code, out, _, _ = self.run_cli(["version", "--json"],
                                       found=found_with(1))
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["installed"], 1)
        self.assertEqual(report["latest"], latest)
        self.assertEqual(report["verdict"], "update-available")
        self.assertTrue(report["update_available"])
        self.assertEqual(report["serial"], "CB0123456789AB")
        code, out, _, _ = self.run_cli(["version", "--json"],
                                       found=dict(found_with(None),
                                                  firmware=None))
        self.assertEqual(code, 1)
        self.assertIsNone(json.loads(out)["installed"])

    def test_version_waits_for_the_relay_and_never_opens_the_port(self):
        from unittest import mock
        from cortex_bridge import bridge_info
        with tempfile.TemporaryDirectory() as tmp:
            status = Path(tmp) / "status.json"
            states = [bridge_info.firmware_status("querying", "busy"),
                      known(7)]

            def sleep(_seconds):
                status.write_text(json.dumps({"state": "running",
                                              "firmware": states.pop(0)}))

            sleep(0)
            with mock.patch.object(cli, "usb_devices",
                                   return_value=[BRIDGE_ON_USB]), \
                    mock.patch.object(cli, "relay_running",
                                      return_value=True), \
                    mock.patch.object(bridge_info, "query_direct",
                                      side_effect=AssertionError("opened")):
                found = cli.installed_firmware(Path(tmp), sleep=sleep)
        self.assertEqual(found["source"], "relay")
        self.assertEqual(found["firmware"]["version"], 7)

    def test_update_to_the_running_version_does_not_touch_the_bridge(self):
        code, out, _, calls = self.run_cli(
            ["update", "--file", "PACKAGE", "--yes"], found=found_with(7),
            inspect={"version": 7})
        self.assertEqual(code, 0)
        self.assertIn("The bridge already runs version 7.", out)
        self.assertIn("Nothing was changed.", out)
        self.assertNotIn("update", [c.get("method") for c in calls])

    def test_update_to_an_older_file_says_why_nothing_happens(self):
        code, out, _, _ = self.run_cli(
            ["update", "--file", "PACKAGE", "--yes"], found=found_with(7),
            inspect={"version": 4})
        self.assertEqual(code, 1)
        self.assertIn("runs version 7, which is newer than version 4", out)
        self.assertIn("only installs a version newer", out)

    def test_a_file_that_is_not_firmware_is_explained(self):
        code, _, err, _ = self.run_cli(["update", "--file", "PACKAGE",
                                        "--yes"], found=found_with(4))
        self.assertEqual(code, 1)
        self.assertIn("Update failed: This file cannot be installed: the file "
                      "is truncated", err)
        self.assertIn("Nothing was sent to the bridge.", err)
        self.assertIn("without --file", err)
        self.assertNotIn("Details:", err, "the reason is said once")

    def test_a_relayed_failure_prints_what_to_do(self):
        def relay(request, on_line=None, **kwargs):
            if request["method"] == "status":
                return {"event": "status"}
            return {"event": "result", "ok": False,
                    "error": "UpdateModeRefused", "message": "no receiver",
                    "summary": "The bridge did not switch to update mode.",
                    "outcome": "Nothing was changed; the bridge still runs "
                               "version 4.",
                    "action": "Wait until they are idle."}

        code, _, err, _ = self.run_cli(
            ["update", "--file", "PACKAGE", "--yes"], found=found_with(4),
            relay=relay, inspect={"version": 7})
        self.assertEqual(code, 1)
        self.assertIn("Update failed: The bridge did not switch to update "
                      "mode.", err)
        self.assertIn("still runs version 4", err)
        self.assertIn("Details: no receiver", err)

    def test_status_shows_the_installed_version(self):
        from cortex_bridge import bridge_info
        bundled = [{"version": 7, "ok": True}]
        line = cli.firmware_line(bridge_info.compare(known(7), bundled),
                                 known(7))
        self.assertEqual(line, "version 7, up to date")
        line = cli.firmware_line(bridge_info.compare(known(4), bundled),
                                 known(4))
        self.assertIn("version 4; version 7 is available", line)
        busy = bridge_info.firmware_status("querying", "busy")
        line = cli.firmware_line(bridge_info.compare(busy, bundled), busy)
        self.assertIn("unknown: the bridge is busy", line)
        self.assertIn("newest bundled: version 7", line)

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
