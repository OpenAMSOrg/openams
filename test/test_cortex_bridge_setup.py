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

    def test_the_ptfe_comment_names_what_this_ams_accepts(self):
        measures = dict(AMS2, ptfe_calibration=True)
        text, _ = config.render_units([measures])
        self.assertIn("OAMS_CALIBRATE_PTFE_LENGTH OAMS=1 SPOOL=<bay>", text)
        self.assertNotIn("cortex-bridge.py ptfe", text)
        text, _ = config.render_units([AMS2])
        self.assertIn("cortex-bridge.py ptfe <millimetres>", text)
        self.assertNotIn("OAMS_CALIBRATE_PTFE_LENGTH", text)

    def test_the_next_step_follows_the_bridge_firmware(self):
        # Firmware 8 or later: the AMS measures, so no millimetres to convert.
        line = cli.next_step_after_config(
            [dict(AMS2, ptfe_calibration=True), dict(HT)])
        self.assertIn("OAMS_CALIBRATE_PTFE_LENGTH OAMS=<n> SPOOL=<bay>", line)
        self.assertNotIn("cortex-bridge.py ptfe", line)
        line = cli.next_step_after_config([AMS2])
        self.assertIn("cortex-bridge.py ptfe <millimetres>", line)
        self.assertIn("cortex-bridge.py update", line)

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
                                 "version": "cortex-bridge-1",
                                 "ptfe_calibration": False})

    def test_bridge_firmware_8_says_the_ams_measures_its_path(self):
        # Bridge firmware 8 keeps the older constant at 0 and publishes the
        # new one; the new one wins, as in src/ams_family.py.
        facts = self.describe({"config": {"AMS_FAMILY": "ams2",
                                          "AMS_BAYS": 4,
                                          "AMS_HT_CAP_PTFE_CALIBRATION": 0,
                                          "AMS_CAP_PTFE_CALIBRATION": 1}})
        self.assertIs(facts["ptfe_calibration"], True)
        self.assertIs(identify.measures_own_path(
            {"AMS_CAP_PTFE_CALIBRATION": "0",
             "AMS_HT_CAP_PTFE_CALIBRATION": "1"}), False)
        self.assertIs(identify.measures_own_path(
            {"AMS_HT_CAP_PTFE_CALIBRATION": "1"}), True)
        self.assertIs(identify.measures_own_path({}), False)

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
            ({"snapshot": {"reading": {
                "class": "no-index-offered",
                "summary": "An AMS (AMS HT) keeps announcing itself.",
                "action": "Switch the printer off."}}},
             "An AMS (AMS HT) keeps announcing itself. Switch the printer"),
            ({"snapshot": {"reading": {"class": "none", "summary": "x",
                                       "action": ""}}},
             "Everything is connected"),
            # The relay still lists it online; the bridge no longer hears it.
            ({"snapshot": {"reading": {"class": "none", "summary": "x",
                                       "action": ""},
                           "snapshot": {"system": {"uptime_ms": 3043170},
                                        "units": [{
                                            "uid": AMS2["uid"],
                                            "link": "silent",
                                            "last_answer_ms": 1604919}]}}},
             "AMS %s has not answered the bridge for 24 min" % AMS2["uid"]),
            # A silent AMS that isn't configured (moved to another printer)
            # is neither "not in your config" nor a problem here.
            ({"relay": {"units": [{"uid": AMS2["uid"], "online": True},
                                  {"uid": HT["uid"], "online": True}]},
              "snapshot": {"snapshot": {"system": {"uptime_ms": 100000},
                                        "units": [{"uid": HT["uid"],
                                                   "link": "silent",
                                                   "last_answer_ms": 1000}]}}},
             "Everything is connected"),
        )
        for overrides, expected in cases:
            self.assertIn(expected, cli.next_step(self.facts(**overrides)),
                          overrides)

    def test_a_silent_unit_shows_as_silent(self):
        facts = self.facts(snapshot={"snapshot": {
            "system": {"uptime_ms": 100000},
            "units": [{"uid": AMS2["uid"], "link": "silent",
                       "last_answer_ms": 40000},
                      {"uid": HT["uid"], "link": "online",
                       "last_answer_ms": 99900}]}})
        self.assertEqual(cli.silent_units(facts), {AMS2["uid"]: 60.0})
        facts.update(usb=[BRIDGE_ON_USB], relay_service="active",
                     klipper_service="active",
                     installed_firmware={"latest": None,
                                         "verdict": "nothing-bundled",
                                         "installed": 11})
        facts["relay"]["units"][0]["path"] = "/dev/oams/" + AMS2["uid"]
        lines = "\n".join(cli.status_lines(facts))
        self.assertIn("%s  silent " % AMS2["uid"], lines)
        self.assertEqual(cli.silent_units(self.facts()), {})

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
                    mock.patch.object(cli, "printer_activity",
                                      return_value=(None, None)), \
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
            mock.patch.object(cli, "printer_activity",
                              return_value=(None, None)),
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

    def started(self, answers):
        """Run klipper-started against scripted Moonraker answers."""
        import contextlib
        import io
        sent = []
        replies = iter(answers)

        def fake(url, path, post=False, timeout=5.0):
            sent.append((path, post))
            if post:
                return {"result": "ok"}
            return {"result": next(replies)}
        original = cli.moonraker
        cli.moonraker = fake
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                code = cli.cmd_klipper_started(
                    types.SimpleNamespace(moonraker="http://x", wait=60.0),
                    sleep=lambda s: None, clock=lambda: 0.0)
        finally:
            cli.moonraker = original
        return code, sent, output.getvalue()

    def test_klipper_started_ready_sends_nothing(self):
        code, sent, text = self.started([{"state": "ready"}])
        self.assertEqual(code, 0)
        self.assertNotIn(("/printer/firmware_restart", True), sent)

    def test_klipper_started_restarts_an_ams_the_bridge_stopped(self):
        shutdown = {"state": "error", "state_message":
                    "Can not update MCU 'ams1' config as it is shutdown\n"}
        code, sent, text = self.started(
            [{"state": "startup"}, shutdown, {"state": "ready"}])
        self.assertEqual(code, 0)
        self.assertEqual(sent.count(("/printer/firmware_restart", True)), 1)
        self.assertIn("FIRMWARE_RESTART", text)

    def test_klipper_started_leaves_other_errors_alone(self):
        code, sent, text = self.started(
            [{"state": "error", "state_message": "Option 'x' must be set"}])
        self.assertEqual(code, 1)
        self.assertNotIn(("/printer/firmware_restart", True), sent)
        self.assertIn("Option 'x' must be set", text)

    def test_ptfe_prints_the_click_count(self):
        import contextlib
        import io
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.main(["ptfe", "2050"])
        self.assertEqual(code, 0)
        self.assertIn("ptfe_length: 2337", output.getvalue())


FAST_UID = "0b55800206c9"     # an AMS 2 Pro, back first after the restart
SLOW_UID = "c5bc6f8de4ae"     # the second unit, back later
TWO_UNITS_CFG = """\
[mcu ams2]
serial: /dev/oams/%s
restart_method: command

[oams ams2]
mcu: ams2
device_kind: ams2

[mcu ams1]
serial: /dev/oams/%s  # the older AMS
restart_method: command

[oams ams1]
mcu: ams1
device_kind: ams1
""" % (FAST_UID, SLOW_UID)
SHUT_DOWN = {"state": "error", "state_message":
             "Can not update MCU 'ams2' config as it is shutdown\n"
             "Once the underlying issue is corrected, use the\n"}


class UpdateReconnectTests(unittest.TestCase):
    """`update` after the install: it waits for every AMS that ran before it,
    and restarts Klipper once, before the AMS are back (a customer's update
    to bridge 29 printed "1 AMS online" with two units, and needed a second
    FIRMWARE_RESTART)."""

    def run_update(self, back_at, after_restart=(), extra=(), before=None,
                   relay_outdated=False, moonraker_conf=None, asvc=None,
                   busy=(False,), result=None, accepted=True,
                   activity=("standby", "Ready"), hold=(True, "")):
        """`update` with the relay, its status file, Moonraker and the clock
        faked. ``back_at``: seconds after the install at which each unit is
        online again (None: never). ``after_restart``: per FIRMWARE_RESTART,
        the printer.info answers Moonraker gives after it, the last repeated.
        ``before``: the units online before the update (default: all).
        ``relay_outdated``: the relay runs code from before an OpenAMS
        update until Moonraker restarts it. ``moonraker_conf``: the text of
        moonraker.conf. ``busy``: the bridge's answers to "busy?" in turn,
        the last repeated (None: the relay cannot ask). ``result``: the
        relay's update result (default: installed), sent after an
        "accepted" event when ``accepted``. ``activity``: print_stats and
        idle_timeout states. ``hold``: OpenAMS's answer to the follower
        hold. Returns (code, output, restart times); self.result holds the
        POSTs to Moonraker and moonraker.conf afterwards, self.steps what
        happened in order."""
        import contextlib
        import io
        from unittest import mock
        from cortex_bridge import updater
        now = [0.0]
        installed_at = [None]
        restarts = []
        posts = []
        outdated = [relay_outdated]
        before = list(back_at) if before is None else before
        steps = self.steps = []
        answers = list(busy)

        def bridge_busy(run_dir):
            steps.append("busy?")
            return answers.pop(0) if len(answers) > 1 else answers[0]

        def run_gcode(url, script):
            steps.append(script)
            if script.endswith("ENABLE=1"):
                return hold
            return True, ""

        def status(run_dir=None):
            if installed_at[0] is None:
                online = set(before)
                firmware = known(29)
            else:
                since = now[0] - installed_at[0]
                online = {uid for uid, at in back_at.items()
                          if at is not None and since >= at}
                firmware = known(30)
            return {"state": "running", "firmware": firmware,
                    "units": [{"uid": uid, "online": uid in online,
                               "path": "/dev/oams/" + uid}
                              for uid in sorted(back_at)]}

        def control(request, on_line=None, **kwargs):
            if request["method"] == "status":
                return {"event": "status"}
            if request["method"] == "support_snapshot":
                return {"event": "support_snapshot", "ok": False,
                        "error": "not-connected"}
            steps.append("update")
            if accepted:
                on_line({"event": "accepted", "version": 30})
                now[0] += 11.0      # the install, or the attempt
                installed_at[0] = now[0]
            if result == "raise":
                raise OSError("connection reset")
            return result or {"event": "result", "ok": True, "changed": True,
                              "version": 30}

        def moonraker(url, path, post=False, timeout=5.0):
            if post:
                posts.append(path)
                if path == "/machine/services/restart?service=cortex-bridge":
                    self.assertIsNone(installed_at[0], "before the install")
                    outdated[0] = False
                    return {"result": "ok"}
                if path == "/server/restart":
                    return {"result": "ok"}
                self.assertEqual(path, "/printer/firmware_restart")
                steps.append("FIRMWARE_RESTART")
                restarts.append(now[0] - (installed_at[0] or 0.0))
                return {"result": {}}
            if not restarts:
                return {"result": {"state": "shutdown"}}
            answers = list(after_restart[len(restarts) - 1]) \
                if len(restarts) <= len(after_restart) else [{"state": "ready"}]
            seen = asked.setdefault(len(restarts), [])
            seen.append(1)
            return {"result": answers[min(len(seen), len(answers)) - 1]}
        asked = {}

        def sleep(seconds):
            now[0] += seconds

        clock = types.SimpleNamespace(sleep=sleep, monotonic=lambda: now[0])
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as data:
            # printer_data/config, with moonraker.asvc in printer_data
            tmp = os.path.join(data, "config")
            os.mkdir(tmp)
            Path(tmp, "oams.cfg").write_text(TWO_UNITS_CFG)
            if moonraker_conf is not None:
                Path(tmp, "moonraker.conf").write_text(moonraker_conf)
            if asvc is not None:
                Path(data, "moonraker.asvc").write_text(asvc)
            package = Path(tmp, "bridge-30.oac2")
            package.write_bytes(b"OAC2 package")
            with mock.patch.object(cli, "control_request",
                                   side_effect=control), \
                    mock.patch.object(cli, "read_status", side_effect=status), \
                    mock.patch.object(cli, "moonraker", side_effect=moonraker), \
                    mock.patch.object(cli, "printer_activity",
                                      return_value=activity), \
                    mock.patch.object(cli, "bridge_busy",
                                      side_effect=bridge_busy), \
                    mock.patch.object(cli, "run_gcode",
                                      side_effect=run_gcode), \
                    mock.patch.object(cli, "installed_firmware",
                                      return_value=found_with(29)), \
                    mock.patch.object(updater, "inspect_package",
                                      return_value={"version": 30}), \
                    mock.patch.object(cli, "time", clock), \
                    mock.patch.object(cli.code_version, "outdated",
                                      side_effect=lambda status: outdated[0]), \
                    contextlib.redirect_stdout(out):
                code = cli.main(["--run-dir", tmp, "--config-dir", tmp,
                                 "--moonraker", "http://x", "update",
                                 "--file", str(package), "--yes"]
                                + list(extra))
            conf = Path(tmp, "moonraker.conf")
            conf_after = conf.read_text() if conf.exists() else None
            services = Path(data, "moonraker.asvc")
            self.asvc_after = services.read_text() \
                if services.exists() else None
        self.result = (posts, conf_after)
        return code, out.getvalue(), restarts

    def test_the_wait_counts_every_unit_that_was_online(self):
        # The early exit: the second unit came back after the first, and the
        # update said "1 AMS online" while `status` showed both moments
        # later. The older AMS re-admits more slowly here.
        code, out, _ = self.run_update({FAST_UID: 1.8, SLOW_UID: 9.0})
        self.assertEqual(code, 0)
        self.assertNotIn("1 AMS online", out)
        self.assertIn("  %s (AMS 2 Pro) is back (2 s)" % FAST_UID, out)
        self.assertIn("  %s (AMS 1) is back (9 s)" % SLOW_UID, out)
        self.assertIn("2 of 2 AMS back online.", out)
        lines = out.splitlines()
        self.assertLess(lines.index("2 of 2 AMS back online."),
                        lines.index("The bridge now runs version 30."))

    def test_the_customers_timing_also_waits_for_the_second(self):
        # The customer's relay log: an AMS 2 Pro back 1.8 s after the
        # install, an AMS HT 1 s later. The status file is read once a
        # second, so the first read saw one.
        code, out, _ = self.run_update({FAST_UID: 1.8, SLOW_UID: 2.8})
        self.assertIn("2 of 2 AMS back online.", out)

    def test_klipper_is_restarted_once_before_the_units_are_back(self):
        code, out, restarts = self.run_update(
            {FAST_UID: 1.8, SLOW_UID: 9.0},
            after_restart=[[{"state": "startup"}, {"state": "ready"}]])
        self.assertEqual(code, 0)
        self.assertEqual(len(restarts), 1, out)
        self.assertLess(restarts[0], 1.8, "sent before any AMS was back")
        self.assertIn("Klipper restarted (FIRMWARE_RESTART); it connects to "
                      "each AMS as it comes back.", out)
        self.assertIn("Klipper is ready.", out)
        self.assertNotIn("once more", out)
        self.assertNotIn("again", out)

    def test_a_stop_left_on_an_ams_gets_one_more_restart(self):
        # The customer's second FIRMWARE_RESTART: both units were back, and
        # the restarted Klipper found the AMS 2 Pro's port still stopped.
        code, out, restarts = self.run_update(
            {FAST_UID: 1.8, SLOW_UID: 2.8},
            after_restart=[[{"state": "startup"}, SHUT_DOWN],
                           [{"state": "startup"}, {"state": "ready"}]])
        self.assertEqual(code, 0)
        self.assertEqual(len(restarts), 2)
        self.assertIn("Klipper stopped with \"Can not update MCU 'ams2' config "
                      "as it is shutdown\": an AMS still held the stop from "
                      "before the restart. Sent FIRMWARE_RESTART once more.",
                      out)
        self.assertIn("Klipper is ready.", out)

    def test_a_second_stop_is_left_to_the_user(self):
        code, out, restarts = self.run_update(
            {FAST_UID: 1.8, SLOW_UID: 2.8},
            after_restart=[[SHUT_DOWN], [SHUT_DOWN]])
        self.assertEqual(len(restarts), 2, "bounded: one retry")
        self.assertIn("Klipper is in error: Can not update MCU 'ams2' config "
                      "as it is shutdown. Press FIRMWARE RESTART in Mainsail; "
                      "if it stops the same way again, run "
                      "`cortex-bridge.py support`.", out)

    def test_a_unit_still_missing_is_named_and_klipper_not_retried(self):
        lost = {"state": "error",
                "state_message": "mcu 'ams1': Unable to connect\n"}
        code, out, restarts = self.run_update(
            {FAST_UID: 1.8, SLOW_UID: None}, after_restart=[[lost]])
        self.assertEqual(code, 0, "the install itself succeeded")
        self.assertIn("1 of 2 AMS back after 60 s: %s (AMS 1) not yet."
                      % SLOW_UID, out)
        self.assertIn("run `cortex-bridge.py status` in a minute", out)
        self.assertIn("power that AMS off and on", out)
        self.assertNotIn("2 of 2", out)
        self.assertEqual(len(restarts), 1, "no retry while an AMS is missing")
        self.assertIn("Klipper is in error: mcu 'ams1': Unable to connect. "
                      "It starts once every AMS in its config answers: when "
                      "%s (AMS 1) is back, press FIRMWARE RESTART in Mainsail."
                      % SLOW_UID, out)

    def test_other_klipper_errors_are_not_retried(self):
        code, out, restarts = self.run_update(
            {FAST_UID: 1.0, SLOW_UID: 2.0}, after_restart=[[
                {"state": "error",
                 "state_message": "Option 'x' in section 'y' must be set"}]])
        self.assertEqual(len(restarts), 1)
        self.assertIn("Klipper is in error: Option 'x' in section 'y' must be "
                      "set.", out)

    def test_no_restart_leaves_klipper_and_says_so(self):
        code, out, restarts = self.run_update(
            {FAST_UID: 1.0, SLOW_UID: 2.0}, extra=["--no-restart"])
        self.assertEqual(restarts, [])
        self.assertIn("2 of 2 AMS back online.", out)
        self.assertIn("press FIRMWARE RESTART in Mainsail", out)

    def test_with_none_online_before_the_configured_units_are_awaited(self):
        # A bridge that ran no firmware (an interrupted update) lists none.
        code, out, _ = self.run_update({FAST_UID: 1.0, SLOW_UID: 4.0},
                                       before=[])
        self.assertIn("2 of 2 AMS back online.", out)

    def test_a_new_unit_is_listed_beside_the_count(self):
        code, out, _ = self.run_update({FAST_UID: 1.0, SLOW_UID: 2.0},
                                       before=[FAST_UID])
        self.assertIn("1 of 1 AMS back online.", out)

    def test_a_relay_on_old_code_is_restarted_before_the_install(self):
        code, out, restarts = self.run_update(
            {FAST_UID: 1.0, SLOW_UID: 2.0}, relay_outdated=True)
        posts, _ = self.result
        self.assertEqual(code, 0)
        self.assertEqual(posts[0],
                         "/machine/services/restart?service=cortex-bridge")
        self.assertIn("The bridge relay still runs OpenAMS code from before "
                      "an update; restarting it first.", out)
        self.assertIn("The relay restarted with the new code.", out)
        self.assertEqual(len(restarts), 1)

    def test_the_update_entry_is_corrected_once_and_moonraker_restarted(self):
        entry = ("[server]\n\n[update_manager openams]\ntype: git_repo\n"
                 "managed_services: cortex-bridge klipper\n")
        code, out, _ = self.run_update({FAST_UID: 1.0, SLOW_UID: 2.0},
                                       moonraker_conf=entry)
        posts, conf = self.result
        self.assertIn("managed_services: klipper moonraker\n", conf)
        self.assertNotIn("cortex-bridge", conf)
        self.assertIn("moonraker.conf: the OpenAMS update entry now has "
                      "'managed_services: klipper moonraker'", out)
        self.assertEqual(posts[-1], "/server/restart", "after everything")
        code, out, _ = self.run_update({FAST_UID: 1.0, SLOW_UID: 2.0},
                                       moonraker_conf=conf)
        posts, again = self.result
        self.assertEqual(again, conf)
        self.assertNotIn("/server/restart", posts)
        self.assertNotIn("moonraker.conf:", out)

    def test_a_glued_allowed_service_is_repaired_and_moonraker_restarted(
            self):
        code, out, _ = self.run_update(
            {FAST_UID: 1.0, SLOW_UID: 2.0},
            asvc="klipper_mcu\ncrowsnestcortex-bridge\n")
        posts, _ = self.result
        self.assertEqual(self.asvc_after,
                         "klipper_mcu\ncrowsnest\ncortex-bridge\n")
        self.assertIn("moonraker.asvc: one service name per line again", out)
        self.assertEqual(posts[-1], "/server/restart")

    def test_a_relay_restart_moonraker_refuses_does_not_stop_the_update(
            self):
        import contextlib
        import io
        from unittest import mock
        err = io.StringIO()
        with mock.patch.object(cli, "restart_relay", return_value=False), \
                contextlib.redirect_stderr(err):
            code, out, restarts = self.run_update(
                {FAST_UID: 1.0, SLOW_UID: 2.0}, relay_outdated=True)
        self.assertEqual(code, 0)
        self.assertEqual(len(restarts), 1)
        self.assertIn("2 of 2 AMS back online.", out)

    # -- the follower hold and a busy bridge (bridge 36, filament loaded) --

    HOLD_ON = "OAMSM_UPDATE_HOLD ENABLE=1"
    HOLD_OFF = "OAMSM_UPDATE_HOLD ENABLE=0"

    def test_the_followers_are_held_before_the_bridge_is_asked(self):
        code, out, restarts = self.run_update({FAST_UID: 1.0, SLOW_UID: 2.0})
        self.assertEqual(code, 0)
        self.assertEqual(self.steps[:3], [self.HOLD_ON, "busy?", "update"])
        self.assertNotIn(self.HOLD_OFF, self.steps,
                         "the FIRMWARE_RESTART ends the hold")
        self.assertIn("FIRMWARE_RESTART", self.steps)
        self.assertIn("OpenAMS holds the AMS followers off", out)

    def test_busy_for_a_moment_after_the_hold_then_the_update_runs(self):
        code, out, _ = self.run_update({FAST_UID: 1.0, SLOW_UID: 2.0},
                                       busy=(True, True, False))
        self.assertEqual(code, 0)
        self.assertEqual(self.steps.count("busy?"), 3)
        self.assertIn("update", self.steps)

    def test_still_busy_after_the_hold_is_turned_down_untouched(self):
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, out, restarts = self.run_update(
                {FAST_UID: 1.0, SLOW_UID: 2.0}, busy=(True,))
        self.assertEqual(code, 1)
        self.assertNotIn("update", self.steps, "the bridge was never asked")
        self.assertEqual(self.steps[-1], self.HOLD_OFF, "the hold released")
        self.assertEqual(restarts, [], "Klipper untouched")
        text = err.getvalue()
        self.assertIn("The bridge is still busy", text)
        self.assertIn("the followers re-arm", text)
        self.assertIn("holds the followers of loaded lanes off by itself",
                      text)
        self.assertIn("a dryer starting or stopping", text)
        self.assertIn("OpenAMS re-arms the AMS followers.", out)

    def test_never_during_a_print_or_a_pause(self):
        import contextlib
        import io
        for activity in (("printing", "Printing"), ("paused", "Ready"),
                         ("standby", "Printing")):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code, out, restarts = self.run_update(
                    {FAST_UID: 1.0, SLOW_UID: 2.0}, activity=activity)
            self.assertEqual(code, 1, activity)
            self.assertEqual(self.steps, [], "nothing asked, nothing held")
            self.assertEqual(restarts, [])
            self.assertIn("The printer is", err.getvalue())

    def test_openams_refusing_the_hold_for_a_print_stops_the_update(self):
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()):
            code, out, _ = self.run_update(
                {FAST_UID: 1.0, SLOW_UID: 2.0},
                hold=(False, "OpenAMS: no update hold while a print is "
                      "printing"))
        self.assertEqual(code, 1)
        self.assertEqual(self.steps, [self.HOLD_ON])

    def test_an_openams_without_the_hold_still_updates(self):
        code, out, _ = self.run_update(
            {FAST_UID: 1.0, SLOW_UID: 2.0},
            hold=(False, "Unknown command:\"OAMSM_UPDATE_HOLD\""))
        self.assertEqual(code, 0)
        self.assertIn("OpenAMS could not hold the followers", out)
        self.assertNotIn(self.HOLD_OFF, self.steps)

    def refused_after_detach(self, post_state="application"):
        from cortex_bridge import update_messages
        result = {"event": "result", "ok": False,
                  "error": "UpdateModeRefused", "message": "no receiver",
                  "reason_code": "", "post_state": post_state}
        result.update(update_messages.explain(
            "UpdateModeRefused", running=29, package=30,
            post_state=post_state))
        return result

    def test_a_refusal_after_the_detach_brings_klipper_back(self):
        # bridge 36 on voron350: busy, the update refused after the detach,
        # every AMS timed out in Klipper; "Nothing was changed" was untrue.
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, out, restarts = self.run_update(
                {FAST_UID: 1.0, SLOW_UID: 2.0},
                result=self.refused_after_detach(),
                after_restart=[[SHUT_DOWN], [{"state": "ready"}]])
        self.assertEqual(code, 1)
        self.assertNotIn("Nothing was changed", err.getvalue())
        self.assertIn("The bridge was not updated; it still runs version 29",
                      err.getvalue())
        self.assertIn("Klipper lost the AMS while the bridge was asked to "
                      "update; bringing it back.", out)
        self.assertIn("2 of 2 AMS back online.", out)
        self.assertIn("Klipper is ready.", out)
        self.assertEqual(len(restarts), 2, "with the bounded retry")
        self.assertNotIn(self.HOLD_OFF, self.steps,
                         "the restart ends the hold")

    def test_a_bridge_left_waiting_for_firmware_is_explained(self):
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()):
            code, out, restarts = self.run_update(
                {FAST_UID: None, SLOW_UID: None},
                result=self.refused_after_detach("receiver"))
        self.assertEqual(code, 1)
        self.assertEqual(restarts, [])
        self.assertIn("they stay offline until the bridge runs its firmware "
                      "again", out)

    def test_the_relay_turning_a_busy_bridge_down_releases_the_hold(self):
        import contextlib
        import io
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, out, restarts = self.run_update(
                {FAST_UID: 1.0, SLOW_UID: 2.0}, accepted=False,
                result={"event": "result", "ok": False,
                        "error": "BridgeBusy", "message": "busy"})
        self.assertEqual(code, 1)
        self.assertEqual(self.steps[-1], self.HOLD_OFF)
        self.assertEqual(restarts, [])
        self.assertIn("The bridge is still busy", err.getvalue())

    def test_a_lost_relay_connection_after_the_detach_brings_klipper_back(
            self):
        import contextlib
        import io
        with contextlib.redirect_stderr(io.StringIO()):
            code, out, restarts = self.run_update(
                {FAST_UID: 1.0, SLOW_UID: 2.0}, result="raise")
        self.assertEqual(code, 1)
        self.assertEqual(len(restarts), 1)
        self.assertIn("bringing it back", out)

    def test_restart_klipper_retries_only_what_a_restart_fixes(self):
        # Also used after a relay restart and after an AMS update.
        import contextlib
        import io
        for answers, posts in (([{"state": "startup"}] * 40, 1),
                               ([SHUT_DOWN, {"state": "ready"}], 2),
                               ([{"state": "ready"}], 1)):
            sent = []
            replies = iter(answers)

            def fake(url, path, post=False, timeout=5.0):
                if post:
                    sent.append(path)
                    return {"result": {}}
                return {"result": next(replies, answers[-1])}
            now = [0.0]
            original = cli.moonraker
            cli.moonraker = fake
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    cli.restart_klipper(
                        "http://x", sleep=lambda s: now.__setitem__(
                            0, now[0] + s), clock=lambda: now[0])
            finally:
                cli.moonraker = original
            self.assertEqual(len(sent), posts, answers[0])


class ConfigWaitTests(unittest.TestCase):
    """`config --write` waits for every AMS like `update`, so it never
    writes one of two units that are still coming back."""

    def run_config(self, online_at, configured=""):
        """`config` with the relay status and the clock faked; returns the
        units it went on to write and its output. ``online_at``: seconds at
        which each unit is online (None: never)."""
        import contextlib
        import io
        from unittest import mock
        now = [0.0]
        written = []

        def status(run_dir=None):
            return {"state": "running", "units": [
                {"uid": uid, "online": at is not None and now[0] >= at,
                 "path": "/dev/oams/" + uid}
                for uid, at in sorted(online_at.items())]}

        def write(args, units, target, identify):
            written.extend(unit["uid"] for unit in units)
            return 0

        def sleep(seconds):
            now[0] += seconds

        clock = types.SimpleNamespace(sleep=sleep, monotonic=lambda: now[0])
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "oams.cfg").write_text(configured)
            with mock.patch.object(cli, "read_status", side_effect=status), \
                    mock.patch.object(cli, "status_snapshot",
                                      return_value=None), \
                    mock.patch.object(cli, "service_state",
                                      return_value="inactive"), \
                    mock.patch.object(cli, "_write_config",
                                      side_effect=write), \
                    mock.patch.object(cli, "time", clock), \
                    contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(out):
                code = cli.main(["--run-dir", tmp, "--config-dir", tmp,
                                 "config", "--write", "--yes"])
        return code, sorted(written), out.getvalue(), now[0]

    def test_units_coming_back_one_by_one_are_all_written(self):
        # A bridge that just started: nothing online or configured yet, the
        # second unit about 4 s after the first.
        code, written, _, took = self.run_config({FAST_UID: 1.0,
                                                  SLOW_UID: 5.0})
        self.assertEqual(code, 0)
        self.assertEqual(written, [FAST_UID, SLOW_UID])
        self.assertLess(took, 20.0)

    def test_a_configured_unit_is_waited_for(self):
        code, written, _, _ = self.run_config(
            {FAST_UID: 0.0, SLOW_UID: 12.0}, configured=TWO_UNITS_CFG)
        self.assertEqual(written, [FAST_UID, SLOW_UID])

    def test_units_all_online_are_written_at_once(self):
        code, written, _, took = self.run_config({FAST_UID: 0.0,
                                                  SLOW_UID: 0.0})
        self.assertEqual(written, [FAST_UID, SLOW_UID])
        self.assertEqual(took, 0.0)

    def test_a_configured_unit_that_never_comes_is_left_out_and_said(self):
        code, written, out, took = self.run_config(
            {FAST_UID: 0.0, SLOW_UID: None}, configured=TWO_UNITS_CFG)
        self.assertEqual(written, [FAST_UID])
        self.assertIn("%s: not online after 30 s; left out" % SLOW_UID, out)


class ConfiguredKindsTests(unittest.TestCase):
    def test_each_bridge_port_gets_its_device_kind(self):
        self.assertEqual(config.configured_kinds(TWO_UNITS_CFG),
                         {FAST_UID: "ams2", SLOW_UID: "ams1"})

    def test_a_unit_without_a_kind_or_port_is_left_out(self):
        text = ("[mcu main]\nserial: /dev/ttyACM0\n\n[oams x]\nmcu: main\n"
                "device_kind: ams2\n\n[mcu a]\nserial: /dev/oams/%s\n\n"
                "[oams a]\nmcu: a\n" % FAST_UID)
        self.assertEqual(config.configured_kinds(text), {})


if __name__ == "__main__":
    unittest.main()
