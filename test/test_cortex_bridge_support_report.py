"""The Cortex Bridge support report: contents, redaction, the bridge snapshot
placeholder, and opt-in submission.

The realistic printer below is built at test time: its fake secrets are put
together from pieces so that no credential-shaped string sits in a tracked
file. Nothing in these tests opens a network connection: every test runs with
urllib's opener replaced by one that fails the test.
"""

import contextlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cortex_bridge import cli, redact, relay  # noqa: E402
from cortex_bridge import support_report, support_submit  # noqa: E402

SALT = bytes(range(16))
UID = "0a1b2c3d4e5f"                 # the AMS 2 Pro behind the bridge
OLD_UID = "5f4e3d2c1b0a"             # an AMS seen earlier, now gone
BRIDGE_SERIAL = "CB0123456789AB"
HOSTNAME = "voron-garage"
USERNAME = "jdoeprinter"

# Fake secrets, assembled so the source never holds one whole.
TAG_KEY = "a3f1c29e" + "7b6d4e5f" + "8a9b0c1d" + "2e3f4a5b"
MQTT_PASSWORD = "Mq77-" + "hunter-" + "s3cr3t"
SECRETS_FILE_PASSWORD = "Sp00l-" + "vault-" + "9931"
GITHUB_TOKEN = "gh" + "p_" + "Zq3T9vX2LmR8kP4wN7yB1cD5fG6hJ0sA2eU8"
JWT = "ey" + "JhbGciOiJIUzI1NiJ9" + ".ey" + "JzdWIiOiJwcmludGVyIn0" + \
    ".c2lnbmF0dXJlLXZhbHVlLTEyMzQ"
TELEGRAM_TOKEN = "123456789:" + "AAF-" + "telegramBotTokenValue42"
DISCORD_HOOK = "https://discord.com/api/webhooks/1122334455/" + \
    "DiScOrDwEbHoOkToKeN-abcdefghijklmnop"
MOONRAKER_API_KEY = "9f8e7d6c" + "5b4a3928" + "17069f8e" + "7d6c5b4a"
WIFI_SSID = "GarageNet-5G"
WIFI_PSK = "wifi-" + "pass-" + "phrase-77"
SSH_PUBLIC = "ssh-" + "ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOo" \
    "qKLsabgH5C9okWi0dh2l9GKJl " + USERNAME + "@" + HOSTNAME
PRIVATE_KEY = "-----BEGIN " + "OPENSSH PRIVATE" + " KEY-----\n" + \
    "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW\n" \
    "QyNTUxOQAAACDjKqp5Fc65tEnRulDqCiirGoB+QvaJFotHYdpfRiiZQAAAAJh4bRSueG0U\n" \
    "-----END " + "OPENSSH PRIVATE" + " KEY-----"
AMS_SERIAL = "19C06A4B2700123"
TAG_UID = "04A1B2C3D4E5F6"
LOGGED_TAG_UID = "04c1d2e3f4a5b6"      # as src/mfrc522.py logs a card
CURL_PASSWORD = "Cu" + "rlPass" + "-77"
UNIT_IDENTITY = "0F1E2D3C" + "4B5A6978" + "8796A5B4"
EMAIL = "someone" + "@" + "example.com"

PRIVATE = {
    "the Bambu tag key": TAG_KEY,
    "the MQTT password": MQTT_PASSWORD,
    "the secrets-file password": SECRETS_FILE_PASSWORD,
    "a GitHub token": GITHUB_TOKEN,
    "a JWT": JWT,
    "a Telegram token": TELEGRAM_TOKEN,
    "a Discord webhook token": "DiScOrDwEbHoOkToKeN",
    "a Moonraker API key": MOONRAKER_API_KEY,
    "the Wi-Fi name": WIFI_SSID,
    "the Wi-Fi passphrase": WIFI_PSK,
    "an SSH public key": "AAAAC3NzaC1lZDI1NTE5",
    "an SSH private key": "b3BlbnNzaC1rZXktdjEA",
    "the host name": HOSTNAME,
    "the user name": USERNAME,
    "a LAN address": "192.168.1.",
    "a public address": "203.0.113.7",
    "a MAC address": "dc:a6:32:12:34:56",
    "an IPv6 address": "fe80::dea6",
    "a custom domain": "printer.example.net",
    "an mDNS name": "spoolhost.local",
    "an AMS serial": AMS_SERIAL,
    "a tag UID": TAG_UID,
    "an e-mail address": EMAIL,
    "a tag UID a reader logged": LOGGED_TAG_UID,
    "a password on a command line": CURL_PASSWORD,
    "an AMS identity in free text": UNIT_IDENTITY,
    "a spool's name": "Galaxy Black",
    "a spool's vendor": "Prusament",
    "a spool's lot": "LOT-998877",
    "a spool's comment": "bought at the shop",
}

HOME = "/home/" + USERNAME

MOONRAKER_CONF = """\
[server]
host: 0.0.0.0
port: 7125

[authorization]
trusted_clients:
    192.168.1.0/24
    127.0.0.1

[secrets]
secrets_path: {home}/printer_data/moonraker.secrets

[mqtt]
address: 192.168.1.40
username: printer
password: {mqtt}

[notifier telegram]
url: tgram://{telegram}/987654321

[notifier discord]
url: {discord}

[spoolman]
server: http://192.168.1.20:7912

[update_manager openams]
type: git_repo
path: ~/openams
origin: https://github.com/OpenAMSOrg/openams.git

[openams_bridge]

[openams_spoolman]
spoolman_url: http://spoolhost.local:7912
""".format(home=HOME, mqtt=MQTT_PASSWORD, telegram=TELEGRAM_TOKEN,
           discord=DISCORD_HOOK)

SECRETS_FILE = """\
[spoolman_credentials]
user: spool
password: {password}
""".format(password=SECRETS_FILE_PASSWORD)

OAMS_CFG = """\
# OpenAMS configuration for a printer with a Cortex Bridge.
[gcode_macro _oams_macro_variables]
variable_pre_cut_x: 20
gcode:

[oams_manager]

[bambu_lab_tag_processor]
key: {key}

# --- Cortex Bridge units ---
# AMS 2 Pro on the Cortex Bridge
[mcu ams2pro]
serial: /dev/oams/{uid}
restart_method: command

[oams ams2pro]
mcu: ams2pro
device_kind: ams2
ptfe_length: 0
oams_idx: 1

[filament_group T0]
group: ams2pro-0
""".format(key=TAG_KEY, uid=UID)

PRINTER_CFG = """\
[include oams.cfg]
[include mainsail.cfg]

[mcu]
serial: /dev/serial/by-id/usb-Klipper_stm32h723xx_1A0021000F51323532393433-if00

[extruder]
max_extrude_only_distance: 300

[exclude_object]

[gcode_shell_command backup_cfg]
command: curl -s -H "Authorization: Bearer {jwt}" https://printer.example.net/up?token=abcdef0123456789

[gcode_shell_command push_cfg]
command: curl -u admin:{curl} https://printer.example.net/push
"""

KLIPPY_LOG = """\
Starting Klippy...
Args: ['{home}/klipper/klippy/klippy.py', '{home}/printer_data/config/printer.cfg', '-l', '{home}/printer_data/logs/klippy.log']
Git version: 'v0.12.0-396-g8a2e0e8b'
Start printer at Sat Nov  7 09:00:00 2026 (1762506000.0 3.1)
Stats 4.1: gcodein=0 mcu: mcu_awake=0.001
Start printer at Sat Nov  8 10:00:01 2026 (1762596001.1 12.3)
===== Config file =====
[mcu]
serial = /dev/serial/by-id/usb-Klipper_stm32h723xx_1A0021000F51323532393433-if00

[mcu ams2pro]
serial = /dev/oams/{uid}

[mcu old_ams]
serial = /dev/oams/{old_uid}

[bambu_lab_tag_processor]
key = {key}

[gcode_shell_command backup_cfg]
command = curl -s -H "Authorization: Bearer {jwt}" https://printer.example.net/up?token=abcdef0123456789

[gcode_shell_command push_cfg]
command = curl -u admin:{curl} https://printer.example.net/push
=======================
{stats}mfrc522: read tag_uid={tag_uid} payload=0102030405060708090a0b0c0d0e0f10
MFRC522 mfrc522 oams1_0 card {logged_tag}
OAMS[1]: unit identity {identity}
webhooks client 547884: New connection from 192.168.1.50
Spoolman: active spool {{"id": 7, "filament": {{"id": 3, "name": "Galaxy Black PETG", "vendor": {{"id": 2, "name": "Prusament"}}}}, "lot_nr": "LOT-998877", "comment": "bought at the shop", "remaining_weight": 812.5}}
Config error
Traceback (most recent call last):
  File "{home}/klipper/klippy/klippy.py", line 131, in _connect
    self._read_config()
configparser.Error: No [fps] section found; OpenAMS requires at least one FPS lane.
""".format(home=HOME, uid=UID, old_uid=OLD_UID, key=TAG_KEY, jwt=JWT,
           tag_uid=TAG_UID, curl=CURL_PASSWORD, logged_tag=LOGGED_TAG_UID,
           identity=UNIT_IDENTITY,
           stats="".join("Stats %d.1: gcodein=0 mcu: mcu_awake=0.001\n" % n
                         for n in range(20, 60)))

MOONRAKER_LOG = """\
2026-11-08 10:00:00,001 [server.py:main()] - Starting Moonraker on (0.0.0.0, 7125), Hostname: {host}
2026-11-08 10:00:00,002 [confighelper.py:log_config()] -
#################### Moonraker Configuration ####################

[mqtt]
password = {mqtt}

#################################################################
2026-11-08 10:00:00,100 [machine.py:_init()] - network: wlan0 mac_address: dc:a6:32:12:34:56 ip: 192.168.1.15 ip6: fe80::dea6:32ff:fe12:3456 ssid: {ssid}
2026-11-08 10:00:01,000 [application.py:open()] - Websocket Opened: ID: 1, Proxied: False, User Agent: Mozilla/5.0, Host Name: 192.168.1.50
2026-11-08 10:00:02,000 [update_manager.py:_check()] - Fetching https://203.0.113.7/releases with api_key={api_key}
2026-11-08 10:00:03,000 [openams_spoolman.py:_sync()] - Spoolman spool 7: {{"id": 7, "filament": {{"id": 3, "name": "Galaxy Black PETG"}}, "location": "Garage shelf"}}
2026-11-08 10:00:04,000 [notifier.py:notify()] - sent to {discord}
2026-11-08 10:00:05,000 [authorization.py:check()] - login from {email} via {host}.local
2026-11-08 10:00:05,500 [openams_bridge.py:_info()] - ams_serial: {ams_serial} tag suffix N573 firmware 00.00.06.49 (0.0.6.49)
2026-11-08 10:00:06,000 [klippy_connection.py:_check_ready()] - Klipper error: No [fps] section found; OpenAMS requires at least one FPS lane.
""".format(host=HOSTNAME, mqtt=MQTT_PASSWORD, ssid=WIFI_SSID,
           api_key=MOONRAKER_API_KEY, discord=DISCORD_HOOK, email=EMAIL,
           ams_serial=AMS_SERIAL)

INSTALL_LOG = """\
Klipper installation found at {home}/klipper
Moonraker configuration found at {home}/printer_data/config
[WARNING] gco-routines: Unsupported Klipper GCodeDispatch baseline; continuing without it
Cortex Bridge relay installed [OK]
Looking for AMS units behind the bridge (up to 45 s)...
No AMS is online behind the bridge. Run `cortex-bridge.py status` to see why.
[NOTE] No AMS was added to oams.cfg yet.
pasted by mistake:
{private_key}
wpa_passphrase="{psk}"
{ssh}
""".format(home=HOME, private_key=PRIVATE_KEY, psk=WIFI_PSK, ssh=SSH_PUBLIC)

# As support_log.GatewaySupportLog writes it: compact JSON lines.
SUPPORT_LOG = "\n".join(json.dumps(record, sort_keys=True,
                                   separators=(",", ":")) for record in (
    {"record": "service_start", "schema": 1, "daemon_version": 2,
     "runtime_protocol": 4, "port_selection": "auto",
     "pty_directory": "/dev/oams"},
    {"record": "node_created", "schema": 1, "node": OLD_UID,
     "device_link": "/dev/oams/" + OLD_UID},
    {"record": "bridge_identity", "schema": 1, "result": "known",
     "firmware_version": 2, "boot_abi": 6, "genuine_status": 0,
     "reset_flags": 8, "uptime_ms": 120000},
    {"record": "traffic_summary", "schema": 1, "generation": 3, "nodes": 0,
     "online": 0, "bridge_to_host_messages": 4, "bridge_to_host_bytes": 80,
     "host_to_bridge_messages": 0, "host_to_bridge_bytes": 0,
     "invalid_host_frames": 0, "klipper_resync_markers": 0,
     "klipper_startup_preambles": 0},
))

RELAY_SERVICE = """\
● cortex-bridge.service - OpenAMS Cortex Bridge relay
     Active: active (running) since Sat 2026-11-08 09:59:50 UTC
Nov 08 09:59:50 {host} python3[812]: cortex-bridge: bridge connected; no AMS answering
Nov 08 09:59:51 {host} sudo[901]: {user} : TTY=pts/0 ; PWD={home}/openams
""".format(host=HOSTNAME, user=USERNAME, home=HOME)


def firmware(version):
    return {"state": "known", "reason": "", "version": version,
            "app_kind": "bridge", "boot_abi": 6, "genuine": "passed",
            "genuine_code": 0, "reset_cause": "power-on", "reset_flags": 8,
            "uptime_ms": 120000, "read_at": "2026-11-08T09:59:52Z"}


def facts_no_ams():
    """The 2026-11-08 case: USB and relay fine, no AMS answering."""
    status = {
        "schema": 1, "daemon_version": 2, "pid": 812,
        "started": "2026-11-08T09:59:50Z", "state": "running",
        "detail": "bridge connected; no AMS answering", "reason": "",
        "bridge": {"device": "/dev/ttyACM1", "serial": BRIDGE_SERIAL,
                   "pid": 0xFE11, "manufacturer": "SiForge",
                   "product": "Cortex Bridge", "image": "production"},
        "firmware": firmware(2), "units": [], "update": None,
    }
    bundled = [{"version": 7, "file": "cortex-bridge-7.oac2",
                "released": "2026-10-30", "notes": "", "ok": True,
                "problem": ""}]
    from cortex_bridge import bridge_info
    return {
        "usb": [{"state": "application", "pid": "fe11",
                 "manufacturer": "SiForge", "product": "Cortex Bridge",
                 "serial": BRIDGE_SERIAL, "device": "/dev/ttyACM1"}],
        "relay_service": "active", "klipper_service": "active",
        "relay": status, "configured": [UID], "bundled_firmware": bundled,
        "installed_firmware": bridge_info.compare(firmware(2), bundled),
    }


class FakeSources(support_report.Sources):
    """The fixture printer: files under ``root``, the rest scripted."""

    def __init__(self, root, *, facts=None, snapshot_reply=None, salt=SALT):
        args = types.SimpleNamespace(
            config_dir=str(root / "printer_data/config"),
            logs_dir=str(root / "printer_data/logs"),
            run_dir=str(root / "run"), moonraker="http://127.0.0.1:7125",
            log_file=str(root / "printer_data/logs/cortex-bridge.jsonl"),
            klipper_service="klipper")
        super().__init__(args)
        self._facts = facts if facts is not None else facts_no_ams()
        self._snapshot_reply = snapshot_reply
        self._salt = salt

    def facts(self):
        return self._facts

    def openams(self):
        return {"describe": "v2.6.0-41-gf8343016",
                "commit": "f8343016" + "0" * 32, "branch": "main",
                "committed": "2026-11-07T18:00:00+00:00",
                "changed_files": 0,
                "origin": "https://github.com/OpenAMSOrg/openams.git"}

    def system(self):
        return {"os": "Debian GNU/Linux 12 (bookworm)",
                "kernel": "6.1.21-v8+", "machine": "aarch64",
                "python": "3.11.2"}

    def klipper(self):
        return {"printer": {"state": "error", "state_message":
                            "No [fps] section found; OpenAMS requires at "
                            "least one FPS lane.",
                            "software_version": "v0.12.0-396-g8a2e0e8b",
                            "hostname": HOSTNAME},
                "moonraker": {"klippy_state": "error",
                              "moonraker_version": "v0.9.3-20",
                              "warnings": ["%s/printer_data/config/x.cfg "
                                           "missing" % HOME]}}

    def snapshot(self):
        reply = self._snapshot_reply or {
            "event": "result", "ok": False, "error": "unknown-method",
            "message": "unknown method 'support_snapshot'"}
        return support_report.bridge_support_snapshot(
            self.run_dir, request=lambda request, **kw: reply)

    def relay_service(self):
        return RELAY_SERVICE

    def hostnames(self):
        return [HOSTNAME]

    def usernames(self):
        return [USERNAME]

    def wifi_networks(self):
        return [WIFI_SSID]

    def salt(self):
        return self._salt


def build_printer(root: Path) -> None:
    config = root / "printer_data/config"
    logs = root / "printer_data/logs"
    config.mkdir(parents=True)
    logs.mkdir(parents=True)
    (root / "run").mkdir()
    (config / "moonraker.conf").write_text(MOONRAKER_CONF)
    (config / "oams.cfg").write_text(OAMS_CFG)
    (config / "printer.cfg").write_text(PRINTER_CFG.format(
        jwt=JWT, curl=CURL_PASSWORD))
    (config / "printer-20261101_120000.cfg").write_text(
        "[old_backup_section]\n")
    (root / "printer_data/moonraker.secrets").write_text(SECRETS_FILE)
    (logs / "klippy.log").write_text(KLIPPY_LOG)
    (logs / "moonraker.log").write_text(MOONRAKER_LOG)
    (logs / "openams-install.log").write_text(INSTALL_LOG)
    (logs / "cortex-bridge.jsonl").write_text(SUPPORT_LOG + "\n")


def no_network(*args, **kwargs):
    raise AssertionError("a test tried to open a network connection")


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        build_printer(self.root)
        # The fixture's secrets file is named by moonraker.conf with the
        # fixture's own home path; point that path at the real file.
        conf = self.root / "printer_data/config/moonraker.conf"
        conf.write_text(conf.read_text().replace(
            HOME + "/printer_data/moonraker.secrets",
            str(self.root / "printer_data/moonraker.secrets")))
        patcher = mock.patch.object(urllib.request, "urlopen", no_network)
        patcher.start()
        self.addCleanup(patcher.stop)

    def sources(self, **kwargs):
        return FakeSources(self.root, **kwargs)


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------

class StatusUpdateLineTests(unittest.TestCase):
    def line(self, update):
        facts = facts_no_ams()
        facts["relay"] = dict(facts["relay"], update=update)
        return [l for l in cli.status_lines(facts)
                if l.startswith("  Last update:")]

    def test_a_message_that_repeats_the_state_is_shown_once(self):
        # Seen on hardware after a bridge-9 install, 2026-11-07.
        self.assertEqual(
            self.line({"result": "installed",
                       "message": "installed version 9"}),
            ["  Last update: installed version 9"])

    def test_a_different_message_keeps_the_state(self):
        self.assertEqual(
            self.line({"phase": "writing", "message": "42%"}),
            ["  Last update: writing 42%"])

    def test_no_update_no_line(self):
        self.assertEqual(self.line(None), [])


class OpenAMSVersionTests(unittest.TestCase):
    """The openams section reads the checkout's git state."""

    def sources(self, root):
        args = types.SimpleNamespace(
            config_dir=str(root), logs_dir=str(root), run_dir=str(root),
            moonraker="http://127.0.0.1:7125",
            log_file=str(root / "cortex-bridge.jsonl"))
        return support_report.Sources(args)

    def test_a_copy_without_git_says_so_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(cli, "OPENAMS_DIR", root):
                section = self.sources(root).openams()
        self.assertEqual(list(section), ["checkout"])
        self.assertIn("not a git checkout", section["checkout"])

    def test_a_git_checkout_reports_its_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = dict(os.environ,
                       GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
                       GIT_COMMITTER_NAME="t",
                       GIT_COMMITTER_EMAIL="t@example.invalid")
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "f").write_text("x")
            subprocess.run(["git", "-C", str(root), "add", "f"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "c"],
                           check=True, env=env)
            with mock.patch.object(cli, "OPENAMS_DIR", root):
                section = self.sources(root).openams()
        self.assertRegex(section["commit"], r"^[0-9a-f]{40}$")
        self.assertEqual(section["changed_files"], 0)


class ReportContentsTests(Fixture):
    def setUp(self):
        super().setUp()
        self.report = support_report.build(self.sources())
        self.text = self.report.text

    def test_no_private_detail_survives(self):
        for what, value in PRIVATE.items():
            self.assertNotIn(value.lower(), self.text.lower(),
                             "%s is still in the report" % what)

    def test_the_last_check_finds_nothing_left(self):
        redactor = support_report.make_redactor(self.sources(),
                                                facts_no_ams())
        self.assertEqual(redactor.find_leaks(self.text), [])
        self.assertEqual(support_report.check_text(self.text, self.sources()),
                         [], "the whole file, footer included, is clean")
        self.assertEqual(self.report.removed_lines, 0,
                         "the redactor itself must catch everything")

    def test_it_shows_what_diagnosed_the_case_at_once(self):
        text = self.text
        # OpenAMS version and commit
        self.assertIn("v2.6.0-41-gf8343016", text)
        # bridge firmware 2 against bundled 7
        self.assertIn("version 2; version 7 is available", text)
        self.assertIn('"verdict": "update-available"', text)
        # relay and USB
        self.assertIn("bridge connected; no AMS answering", text)
        self.assertIn("running, serial %s, production firmware on "
                      "/dev/ttyACM1" % BRIDGE_SERIAL, text)
        self.assertIn("AMS units:   none", text)
        # the next step `status` names
        self.assertIn("Next step: No AMS answers. Check 24 V at PWR IN", text)
        # Klipper's state and the klippy.log error
        self.assertIn('"state": "error"', text)
        self.assertIn("configparser.Error: No [fps] section found", text)
        # the config sections
        self.assertIn("[oams ams2pro]", text)
        self.assertIn("ptfe_length: 0", text)
        # install.sh's last output, gco-routines warning included
        self.assertIn("Unsupported Klipper GCodeDispatch baseline", text)
        # the snapshot is absent, and says why
        self.assertIn("this OpenAMS relay does not ask the bridge for a "
                      "snapshot", text)

    def test_the_bridge_serial_and_public_project_urls_are_kept(self):
        self.assertIn(BRIDGE_SERIAL, self.text)
        self.assertIn("Starting Moonraker on (0.0.0.0, 7125)", self.text)
        self.assertIn("https://github.com/OpenAMSOrg/openams.git", self.text)
        self.assertIn('command = curl -s -H "Authorization: <redacted>" '
                      'https://<host-', self.text)

    def test_ams_unit_ids_appear_as_they_are(self):
        """The owner's decision: the roster UID `status` prints is kept."""
        self.assertIn("serial: /dev/oams/%s" % UID, self.text)    # oams.cfg
        self.assertIn('"configured": [\n  "%s"' % UID, self.text)
        self.assertIn("/dev/oams/%s" % OLD_UID, self.text)        # klippy
        self.assertIn('"node":"%s"' % OLD_UID, self.text)         # relay log

    def test_ams_serials_hash_alike_in_two_reports_from_one_printer(self):
        name = redact.Redactor(salt=SALT).serial_hash(AMS_SERIAL)
        self.assertIn("ams_serial: %s" % name, self.text)
        again = support_report.build(self.sources()).text
        other = support_report.build(self.sources(salt=b"another salt!!!!"))
        self.assertIn(name, again)
        self.assertNotIn(name, other.text)
        self.assertNotIn(AMS_SERIAL, other.text)

    def test_the_tag_suffix_and_ams_version_stay(self):
        self.assertIn("tag suffix N573 firmware 00.00.06.49 (0.0.6.49)",
                      self.text)

    def test_spools_keep_only_their_ids(self):
        self.assertIn('"id": 7', self.text)
        self.assertIn('"id": 3', self.text)
        self.assertNotIn("812.5", self.text)

    def test_klippy_log_starts_at_the_last_start_without_stats_noise(self):
        section = self.text.split("== klippy.log", 1)[1].split("\n== ", 1)[0]
        stats = [line for line in section.splitlines()
                 if line.startswith("Stats ")]
        self.assertEqual(len(stats), 3)
        self.assertTrue(stats[-1].startswith("Stats 59.1:"))
        self.assertIn("periodic Stats lines left out", section)
        self.assertIn("Start printer at Sat Nov  8", section)

    def test_moonraker_config_dump_is_left_out(self):
        section = self.text.split("== moonraker.log", 1)[1] \
            .split("\n== ", 1)[0]
        self.assertNotIn("Moonraker Configuration", section)
        self.assertIn("configuration dump left out", section)

    def test_config_sections_and_names(self):
        self.assertIn('"printer.cfg": [', self.text)
        self.assertIn('"exclude_object"', self.text)
        self.assertNotIn("old_backup_section", self.text)
        self.assertIn("[spoolman]\nserver: http://<lan-ip-", self.text)
        self.assertIn("[openams_bridge]", self.text)
        self.assertNotIn("[mqtt]", self.text.split(
            "== moonraker.conf: OpenAMS sections", 1)[1].split("\n== ")[0])

    def test_the_removed_summary_counts_by_kind(self):
        footer = self.text.rsplit("== removed", 1)[1]
        for kind in ("secrets", "ip addresses", "ams serials", "spool details",
                     "tag data", "host names", "user names"):
            self.assertIn(kind, footer)

    def test_a_line_the_redactor_missed_is_removed(self):
        redactor = redact.Redactor(salt=SALT, secrets=["leftover-secret-1"])
        text, removed = support_report.remove_leaks(
            "fine\nhere is leftover-secret-1 again\nfine\n", redactor)
        self.assertEqual(removed, 1)
        self.assertNotIn("leftover-secret-1", text)
        self.assertIn("<line removed by the last check: secrets>", text)


# ---------------------------------------------------------------------------
# Redaction rules
# ---------------------------------------------------------------------------

class RedactorTests(unittest.TestCase):
    def redactor(self, **kwargs):
        options = dict(salt=SALT, hostnames=[HOSTNAME], usernames=[USERNAME],
                       bridge_serials=[BRIDGE_SERIAL])
        options.update(kwargs)
        return redact.Redactor(**options)

    def test_it_is_idempotent(self):
        redactor = self.redactor()
        once = redactor.redact_text(KLIPPY_LOG + MOONRAKER_LOG + INSTALL_LOG)
        self.assertEqual(redactor.redact_text(once), once)

    def test_addresses_get_numbered_placeholders(self):
        redactor = self.redactor()
        text = redactor.redact_text(
            "a 192.168.1.20 b 10.0.0.5 c 192.168.1.20 d 8.8.4.4 "
            "e 127.0.0.1 f 0.0.0.0 g 255.255.255.0 h 192.168.1.0/24 "
            "i http://127.0.0.1:7125/printer/info")
        self.assertEqual(text, "a <lan-ip-1> b <lan-ip-2> c <lan-ip-1> d "
                         "<ip-1> e 127.0.0.1 f 0.0.0.0 g 255.255.255.0 h "
                         "<lan-ip-3>/24 i http://127.0.0.1:7125/printer/info")

    def test_versions_and_times_are_not_addresses(self):
        redactor = self.redactor()
        text = "v0.12.0-396 at 10:00:01,123 python 3.11.2 2026-11-08"
        self.assertEqual(redactor.redact_text(text), text)

    def test_secret_options_in_any_form(self):
        redactor = self.redactor()
        cases = {
            "password: hunter2hunter2": "password: <redacted>",
            # Split so the public export's leak scan never sees the shape.
            'api_key = "' + 'abcdef123456"':
                'api_key = ' + '"<redacted>"',
            '{"bot_token": "abcdef123456"}': '{"bot_token": "<redacted>"}',
            "key: %s" % TAG_KEY: "key: <redacted>",
            "Authorization: Bearer abcdefghijklmnopq":
                "Authorization: <redacted>",
            "wifi_ssid=MyNet": "wifi_ssid=<redacted>",
        }
        for given, expected in cases.items():
            self.assertEqual(redactor.redact_text(given), expected, given)

    def test_json_objects_under_a_secret_word_stay_json(self):
        redactor = self.redactor()
        text = '"webhooks": {"state": "ready"}'
        self.assertEqual(redactor.redact_text(text), text)

    def test_urls(self):
        redactor = self.redactor()
        self.assertEqual(
            redactor.redact_text("https://user:pw@printer.example.net:8080/"
                                 "a?token=x1&page=2"),
            "https://<redacted>@<host-1>:8080/a?token=<redacted>&page=2")
        self.assertEqual(redactor.redact_text("tgram://%s/1" % TELEGRAM_TOKEN),
                         "tgram://<redacted>")
        self.assertEqual(redactor.redact_text(DISCORD_HOOK),
                         "https://discord.com/<redacted>")
        kept = "https://github.com/OpenAMSOrg/openams/blob/main/README.md"
        self.assertEqual(redactor.redact_text(kept), kept)

    def test_ams_unit_ids_stay_and_tag_uids_go(self):
        redactor = self.redactor()
        text = "/dev/oams/%s uid=%s uid: 04 A1 B2 C3 tag_suffix=N573 " \
            "ams_version=[0, 0, 6, 49]" % (UID, UID)
        self.assertEqual(redactor.redact_text(text),
                         "/dev/oams/%s uid=%s uid: <tag> tag_suffix=N573 "
                         "ams_version=[0, 0, 6, 49]" % (UID, UID))

    def test_ams_serials_hash_but_the_bridge_serial_stays(self):
        redactor = self.redactor()
        text = redactor.redact_text('{"serial": "%s", "ams_serial": "%s"}'
                                    % (BRIDGE_SERIAL, AMS_SERIAL))
        self.assertIn(BRIDGE_SERIAL, text)
        self.assertNotIn(AMS_SERIAL, text)
        self.assertRegex(text, r'"ams_serial": "sn-[0-9a-f]{8}"')

    def test_structured_fields(self):
        redactor = self.redactor()
        data = redactor.redact_data({
            "hostname": HOSTNAME, "uid": UID, "serial": AMS_SERIAL,
            "bridge": {"serial": BRIDGE_SERIAL},
            "tag_uid": TAG_UID, "mqtt_password": "abcdefgh",
            "spool": {"id": 4, "comment": "mine", "filament": {"id": 2,
                                                               "name": "x"}},
            "path": HOME + "/openams", "ok": True, "count": 3})
        self.assertEqual(data["hostname"], "<host>")
        self.assertEqual(data["uid"], UID)
        self.assertTrue(data["serial"].startswith("sn-"))
        self.assertEqual(data["bridge"]["serial"], BRIDGE_SERIAL)
        self.assertEqual(data["tag_uid"], "<tag>")
        self.assertEqual(data["mqtt_password"], "<redacted>")
        self.assertEqual(data["spool"], {"id": 4, "comment": "<redacted>",
                                         "filament": {"id": 2,
                                                      "name": "<redacted>"}})
        self.assertEqual(data["path"], "/home/<user>/openams")
        self.assertEqual((data["ok"], data["count"]), (True, 3))

    def test_names_joined_by_punctuation_are_found(self):
        redactor = self.redactor()
        text = redactor.redact_text("/tmp/-home-%s-Projects/x %s_old "
                                    "%s.lan" % (USERNAME, USERNAME, HOSTNAME))
        self.assertEqual(text, "/tmp/-home-<user>-Projects/x <user>_old "
                         "<host>")
        self.assertEqual(redactor.find_leaks(text), [])

    def test_a_common_host_name_stays_a_word(self):
        redactor = self.redactor(hostnames=["klipper"])
        text = redactor.redact_text(
            "Nov 08 09:59:50 klipper python3[812]: ~/klipper/klippy started")
        self.assertEqual(text, "Nov 08 09:59:50 <host> python3[812]: "
                         "~/klipper/klippy started")

    def test_the_last_check_is_the_redaction_itself(self):
        """Finding 8: find_leaks flags exactly the lines redact_text would
        change, naming what it would remove, and masks what it shows."""
        redactor = self.redactor(secrets=[MQTT_PASSWORD])
        planted = (
            "ok line", "addr 192.168.1.9", "mac dc:a6:32:12:34:56",
            HOME + "/x", "token: abcdefghi", MQTT_PASSWORD, "at " + HOSTNAME,
            GITHUB_TOKEN, TAG_KEY, EMAIL,
            "-----BEGIN " + "RSA PRIVATE KEY-----",
            "MFRC522 mfrc522 oams1_0 card 04a1b2c3d4e5f6",
            "command = curl -u admin:" + MQTT_PASSWORD[:4] + "x1 "
            "https://github.com/a",
            "kept: v2.6.0-41-gf8343016 at head f8343016 /dev/oams/" + UID)
        findings = redactor.find_leaks("\n".join(planted))
        flagged = {number for _, number, _ in findings}
        for number, line in enumerate(planted, 1):
            self.assertEqual(number in flagged,
                             self.redactor(secrets=[MQTT_PASSWORD])
                             .redact_text(line) != line, line)
        self.assertEqual(flagged, set(range(2, 14)))
        kinds = dict((number, kind) for kind, number, _ in findings)
        self.assertEqual(kinds[2], "ip addresses")
        self.assertEqual(kinds[12], "tag data")
        self.assertEqual(kinds[13], "secrets")
        self.assertNotIn(MQTT_PASSWORD, repr(findings),
                         "findings are masked")
        self.assertEqual(redactor.counts, {}, "checking counts nothing")

    def test_tag_uids_in_free_text(self):
        """Finding 1: every format the host code logs a tag in."""
        redactor = self.redactor()
        cases = {
            # src/mfrc522.py: logging.info("MFRC522 %s card %s", ...)
            "MFRC522 mfrc522 oams1_0 card 04a1b2c3d4e5f6":
                "MFRC522 mfrc522 oams1_0 card <tag>",
            "MFRC522 r card 04a1b2c3": "MFRC522 r card <tag>",
            "MFRC522 r card 04a1b2c3d4e5f6a7b8c9": "MFRC522 r card <tag>",
            # src/mfrc522.py: the OAMSM_RFID_READ answer
            "OAMSM_RFID_READ OAMS=1 RFID_CARD=0 STATUS=PRESENT UID=04A1B2C3 "
            "BLOCK=0102030405060708090A0B0C0D0E0F10 VERSION=0x92":
                "OAMSM_RFID_READ OAMS=1 RFID_CARD=0 STATUS=PRESENT UID=<tag> "
                "BLOCK=<tag> VERSION=0x92",
            # Spoolman's extra field (moonraker/openams_spoolman)
            '"card_uids": "04a1b2c3"': '"card_uids": "<tag>"',
            '{"tray_uid": "0f0e0d0c0b0a09080706050403020100"}':
                '{"tray_uid": "<tag>"}',
        }
        for given, expected in cases.items():
            self.assertEqual(redactor.redact_text(given), expected, given)
        kept = ("Git version: 'v0.12.0-396-g8a2e0e8b' describe "
                "v2.6.0-41-gf8343016, head f8343016, uid=%s" % UID)
        self.assertEqual(redactor.redact_text(kept), kept)

    def test_credentials_on_command_lines(self):
        """Finding 2: what a shell command in klippy.log's config dump
        carries."""
        redactor = self.redactor()
        cases = {
            "command = curl -u admin:S3cretPass https://x.example.net/a":
                "command = curl -u <redacted> https://<host-1>/a",
            "curl --user admin:S3cretPass -X POST":
                "curl --user <redacted> -X POST",
            "curl --user=admin:S3cretPass": "curl --user=<redacted>",
            "tool --password hunter22 --verbose":
                "tool --password <redacted> --verbose",
            "tool --token abc123 --api-key=xyz789 --http-password 'a b'":
                "tool --token <redacted> --api-key=<redacted> "
                "--http-password <redacted>",
            "rsync -a backup:S3cret@nas.example.net:/x /y":
                "rsync -a <redacted>@<host-2>:/x /y",
        }
        for given, expected in cases.items():
            self.assertEqual(redactor.redact_text(given), expected, given)
        kept = ("moonraker -p 7125 -u /tmp/sock; systemctl --user status "
                "klipper; docker login --password-stdin")
        self.assertEqual(redactor.redact_text(kept), kept)

    def test_secret_values_go_whole(self):
        """Finding 3: a value of several words, and escaped quotes."""
        redactor = self.redactor()
        cases = {
            "password: my pass word": "password: <redacted>",
            "wpa_passphrase = two words here  ": "wpa_passphrase = <redacted>",
            '{"password": "pa\\"ss", "x": 1}':
                '{"password": "<redacted>", "x": 1}',
            '{"password": 12345, "x": 1}': '{"password": <redacted>, "x": 1}',
            # Split so the public export's leak scan never sees the shape.
            "api_key=" + "'it''s'": "api_key=" + "'<redacted>'",
        }
        for given, expected in cases.items():
            self.assertEqual(redactor.redact_text(given), expected, given)

    def test_serials_of_digits_and_in_free_text_are_hashed(self):
        """Finding 4: all-digit serials, and the 24-hex unit identity."""
        redactor = self.redactor()
        identity = "0F1E2D3C4B5A69788796A5B4"
        hashed = redactor.serial_hash(identity)
        self.assertRegex(redactor.redact_text('"serial": "123456789012"'),
                         r'^"serial": "sn-[0-9a-f]{8}"$')
        self.assertEqual(redactor.redact_text(
            "OAMS[1]: unit serial %s" % identity),
            "OAMS[1]: unit serial %s" % hashed)
        self.assertEqual(redactor.redact_text(
            '"identity": {"serial": "%s"}' % identity),
            '"identity": {"serial": "%s"}' % hashed)
        words = [int.from_bytes(bytes.fromhex(identity)[i:i + 4], "little")
                 for i in (0, 4, 8)]
        self.assertEqual(redactor.redact_text(
            "Receive: ams_identity uid0=%d uid1=%d uid2=%d" % tuple(words)),
            "Receive: ams_identity serial=%s" % hashed)
        self.assertEqual(redactor.redact_text(
            "serial: " + BRIDGE_SERIAL), "serial: " + BRIDGE_SERIAL)

    def test_ordinary_words_are_not_taken_for_secrets_or_hosts(self):
        """Finding 6."""
        redactor = self.redactor(hostnames=["oams"])
        kept = ("webhooks: Error processing data\n"
                "tokens=5 secrets_path: ~/printer_data/moonraker.secrets\n"
                "[oams ams2pro] serial: /dev/oams/%s oams.cfg OAMS[1]" % UID)
        self.assertEqual(redactor.redact_text(kept), kept)
        self.assertEqual(redactor.redact_text(
            "Nov 08 09:59:50 oams python3[1]: up\npi@oams:~ $ ping "
            "oams.local"),
            "Nov 08 09:59:50 <host> python3[1]: up\npi@<host>:~ $ ping "
            "<host>")
        self.assertFalse(redact.distinctive_hostname("garage"))
        self.assertTrue(redact.distinctive_hostname("voron-garage"))
        self.assertTrue(redact.distinctive_hostname("printer24"))


class CollectSecretsTests(Fixture):
    def test_secrets_come_from_config_and_the_secrets_file(self):
        found = redact.collect_secrets(self.root / "printer_data/config")
        self.assertIn(TAG_KEY, found)
        self.assertIn(MQTT_PASSWORD, found)
        self.assertIn(SECRETS_FILE_PASSWORD, found)

    def test_placeholders_and_templates_are_not_secrets(self):
        found = redact.secrets_in_ini(
            "[bambu_lab_tag_processor]\nkey: <32_HEX_CHARACTERS>\n"
            "[mqtt]\npassword: {secrets.mqtt.password}\nport_password: 1\n")
        self.assertEqual(found, set())

    def test_a_json_secrets_file(self):
        found = redact.secrets_in_secrets_file(json.dumps(
            {"mqtt": {"password": "json-secret-1"}, "port": 1883}))
        self.assertEqual(found, {"json-secret-1"})

    def test_the_salt_is_created_once_and_kept_private(self):
        path = self.root / "salt/support-report-salt"
        first = redact.load_or_create_salt(path)
        self.assertEqual(redact.load_or_create_salt(path), first)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)


# ---------------------------------------------------------------------------
# The bridge snapshot placeholder
# ---------------------------------------------------------------------------

class SnapshotTests(Fixture):
    def snapshot(self, reply=None, error=None):
        def request(payload, **kwargs):
            self.assertEqual(payload, {"method": "support_snapshot"})
            if error is not None:
                raise error
            return reply
        return support_report.bridge_support_snapshot(
            self.root / "run", request=request)

    def test_no_relay(self):
        result = self.snapshot(error=FileNotFoundError(2, "No such file"))
        self.assertFalse(result["available"])
        self.assertEqual(result["reason"], "relay-not-running")
        self.assertIsNone(result["snapshot"])

    def test_a_relay_without_the_method(self):
        result = self.snapshot({"event": "result", "ok": False,
                                "error": "unknown-method"})
        self.assertEqual(result["reason"], "relay-without-snapshot")
        self.assertTrue(result["text"].startswith("unavailable: "))

    def test_bridge_firmware_too_old(self):
        result = self.snapshot({"ok": False, "error": "unsupported-firmware",
                                "minimum_firmware": 9})
        self.assertEqual(result["reason"], "firmware-too-old")
        self.assertEqual(result["text"],
                         "unavailable: bridge firmware older than 9")

    def test_a_malformed_answer(self):
        self.assertEqual(self.snapshot({"ok": True})["reason"], "malformed")
        self.assertEqual(self.snapshot({})["reason"], "no-reply")

    def test_a_relay_without_a_bridge_session_says_so(self):
        """The relay itself, on a real control socket, between sessions."""
        run_dir = self.root / "relayrun"
        args = relay.parser().parse_args([
            "--pty-dir", str(self.root / "pty"), "--run-dir", str(run_dir),
            "--log-file", str(self.root / "relay.jsonl")])
        server = relay.Relay(args, serial_module=None, list_ports=None)
        self.addCleanup(server.log.close)
        self.addCleanup(server.control.close)
        stop = threading.Event()

        def serve():
            while not stop.is_set():
                server.serve_control(running=False)
                time.sleep(0.01)
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            result = support_report.bridge_support_snapshot(run_dir,
                                                            timeout=5)
        finally:
            stop.set()
            thread.join(5)
        self.assertEqual(result["reason"], "not-connected")
        self.assertIn("no bridge session", result["text"])

    def vector(self, name):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_cortex_bridge_snapshot import VECTORS
        return VECTORS[name]["decoded"]

    def snapshot_section(self, decoded):
        reply = {"event": "support_snapshot", "ok": True,
                 "snapshot": decoded, "raw": "AQ=="}
        report = support_report.build(self.sources(snapshot_reply=reply))
        return report, report.text.split("== bridge snapshot", 1)[1] \
            .split("\n== ", 1)[0]

    def test_the_report_reads_the_snapshot_first(self):
        report, section = self.snapshot_section(self.vector("silent_line"))
        first = section.splitlines()[1]
        self.assertTrue(first.startswith("Reading: The bridge sends on the "
                                         "AMS data line and nothing comes "
                                         "back."), first)
        self.assertIn("What to do: The data pair is open", section)
        self.assertIn('"class": "silent-line"', section)
        self.assertIn('"sent_messages": 149', section)
        self.assertNotIn('"raw"', section, "the raw body is not carried")
        self.assertIn("Next step: No AMS answers. The bridge reports: The "
                      "bridge sends on the AMS data line", report.text)

    def test_a_refusal_names_the_reason_and_the_suffix(self):
        _, section = self.snapshot_section(
            self.vector("refused_at_ht_index"))
        self.assertIn("Reading: An AMS (tag suffix N700, not one this "
                      "firmware recognises) answers but the bridge refuses "
                      "it: it holds an AMS index that does not fit its "
                      "model", section)
        self.assertIn('"ams_version": [\n', section)
        self.assertIn('"last_offer_basis": "marker_ht"', section)

    def test_a_failed_load_shows_its_odometer_profile(self):
        _, section = self.snapshot_section(
            self.vector("ams1_load_stalled_rewinding"))
        lines = section.splitlines()
        self.assertEqual(lines[1], "Reading: The snapshot shows none of the "
                         "known failure patterns.")
        self.assertEqual(lines[2], "Last load of AMS df5d1d75218e failed: "
                         "the AMS rewound from 1200 mm to 601 mm and then "
                         "stopped moving for 4 s, 6.2 s into the feed; it "
                         "was still rewinding. AMS state: moving.")
        for field in ('"odo_start_mm": 1200', '"odo_low_mm": 600',
                      '"since_progress_ms": 4000', '"feed_ms": 6193',
                      '"rewinding": true'):
            self.assertIn(field, section)

    def test_only_allowlisted_fields_reach_the_report(self):
        decoded = self.vector("admitted_not_configured")
        decoded = dict(decoded, serial=AMS_SERIAL, bridge={
            "serial": BRIDGE_SERIAL})
        decoded["units"] = [dict(decoded["units"][0], tag_uid=TAG_UID,
                                 ams_serial=AMS_SERIAL)]
        _, section = self.snapshot_section(decoded)
        self.assertIn('"uid": "151a58f19349"', section)
        self.assertIn('"tag_suffix": "N573"', section)
        self.assertRegex(section, r'"ams_version": \[\n +29,')
        for dropped in (AMS_SERIAL, TAG_UID, BRIDGE_SERIAL, '"bridge"'):
            self.assertNotIn(dropped, section)
        self.assertIn("Reading: The bridge offers AMS 151a58f19349 but "
                      "Klipper does not use it.", section)


# ---------------------------------------------------------------------------
# Submission: opt-in, with consent
# ---------------------------------------------------------------------------

class FakeReply:
    status = 200

    def __init__(self, body=b"received: case 1234"):
        self.body = body

    def read(self, limit=-1):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class SubmitTests(Fixture):
    def setUp(self):
        super().setUp()
        self.report = self.root / "report.txt"
        self.report.write_text("OpenAMS report\n== summary\nfine\n")
        self.sent = []
        self.out = []

    def opener(self, request, timeout=None):
        self.sent.append((request.full_url, request.data,
                          dict(request.header_items())))
        return FakeReply()

    def submit(self, destination, answers=("y",), interactive=True):
        answers = list(answers)
        return support_submit.submit(
            self.report, destination, ask=lambda prompt: answers.pop(0),
            out=self.out.append, interactive=interactive,
            opener=self.opener)

    def destination(self, url="https://support.example.org/reports"):
        return support_submit.Destination(url, "a test")

    def test_no_destination_is_set_up_by_default(self):
        self.assertIsNone(support_submit.configured(
            self.root / "printer_data/config"))
        self.assertEqual(self.submit(None), 1)
        self.assertEqual(self.sent, [])
        self.assertIn(support_submit.NO_DESTINATION, self.out)

    def test_the_destination_comes_from_moonraker_conf(self):
        conf = self.root / "printer_data/config/moonraker.conf"
        conf.write_text(conf.read_text().replace(
            "[openams_bridge]\n",
            "[openams_bridge]\nsupport_url: https://support.example.org/r\n"))
        found = support_submit.configured(self.root / "printer_data/config")
        self.assertEqual(found.url, "https://support.example.org/r")
        override = support_submit.configured(
            self.root / "printer_data/config", "https://other.example.org")
        self.assertEqual(override.url, "https://other.example.org")

    def test_nothing_is_sent_without_a_terminal(self):
        self.assertEqual(self.submit(self.destination(), interactive=False), 1)
        self.assertEqual(self.sent, [])
        self.assertIn(support_submit.NOT_INTERACTIVE, self.out)

    def test_anything_but_yes_sends_nothing(self):
        for answer in ("", "n", "no", "nope", "send"):
            self.assertEqual(self.submit(self.destination(), [answer]), 1)
        self.assertEqual(self.sent, [])

    def test_end_of_input_sends_nothing(self):
        def ask(prompt):
            raise EOFError
        code = support_submit.submit(self.report, self.destination(),
                                     ask=ask, out=self.out.append,
                                     interactive=True, opener=self.opener)
        self.assertEqual(code, 1)
        self.assertEqual(self.sent, [])

    def test_it_shows_what_goes_where_and_sends_exactly_the_file(self):
        self.assertEqual(self.submit(self.destination(), ["v", "y"]), 0)
        shown = "\n".join(self.out)
        self.assertIn(str(self.report), shown)
        self.assertIn("https://support.example.org/reports", shown)
        self.assertIn("It contains: summary.", shown)
        self.assertIn("OpenAMS report\n== summary\nfine\n", self.out)
        self.assertEqual(len(self.sent), 1)
        url, data, headers = self.sent[0]
        self.assertEqual(url, "https://support.example.org/reports")
        self.assertEqual(data, self.report.read_bytes())
        self.assertEqual(headers["Content-type"], support_submit.CONTENT_TYPE)
        self.assertIn("received: case 1234", shown)

    def test_plain_http_leaves_only_for_this_computer(self):
        self.assertEqual(self.submit(
            self.destination("http://support.example.org/r")), 1)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.submit(
            self.destination("http://127.0.0.1:8000/r")), 0)
        self.assertEqual(len(self.sent), 1)

    def test_other_destinations_are_refused_until_a_sender_exists(self):
        for url in ("mailto:support@example.org", "discord://hook/token",
                    "https://user:pw@support.example.org/r"):
            self.assertEqual(self.submit(self.destination(url)), 1, url)
        self.assertEqual(self.sent, [])

    def test_a_failed_send_keeps_the_report(self):
        def refusing(request, timeout=None):
            raise OSError("connection refused")
        code = support_submit.submit(
            self.report, self.destination(), ask=lambda prompt: "y",
            out=self.out.append, interactive=True, opener=refusing)
        self.assertEqual(code, 1)
        self.assertTrue(self.report.exists())
        self.assertIn("could not be reached", self.out[-1])

    def test_a_redirect_is_a_failure_that_names_its_target(self):
        """Finding 5: a 3xx is never reported as sent."""
        import email.message
        import urllib.error

        def redirecting(request, timeout=None):
            headers = email.message.Message()
            headers["Location"] = "https://elsewhere.example.org/drop"
            raise urllib.error.HTTPError(request.full_url, 302, "Found",
                                         headers, None)
        code = support_submit.submit(
            self.report, self.destination(), ask=lambda prompt: "y",
            out=self.out.append, interactive=True, opener=redirecting)
        self.assertEqual(code, 1)
        self.assertIn("redirect (HTTP 302) to elsewhere.example.org",
                      self.out[-1])
        self.assertNotIn("Sent.", "\n".join(self.out))

    def test_the_default_sender_does_not_follow_a_redirect(self):
        """Against a server on this computer: it answers the POST with a
        302, and no second request (urllib's body-less GET) reaches it."""
        import http.server
        seen = []

        class Redirecting(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append(("POST", self.path))
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:%d/other"
                                 % self.server.server_port)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                seen.append(("GET", self.path))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Redirecting)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            code = support_submit.submit(
                self.report, self.destination(
                    "http://127.0.0.1:%d/reports" % server.server_port),
                ask=lambda prompt: "y", out=self.out.append,
                interactive=True)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(code, 1)
        self.assertEqual(seen, [("POST", "/reports")])
        self.assertIn("redirect (HTTP 302) to 127.0.0.1", self.out[-1])


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------

class CommandTests(Fixture):
    def run_support(self, *argv, answers=("y",), interactive=True):
        args = cli.parser().parse_args(
            ["--config-dir", str(self.root / "printer_data/config"),
             "--run-dir", str(self.root / "run"), "support"] + list(argv))
        answers = list(answers)
        sent = []

        def opener(request, timeout=None):
            sent.append(request.data)
            return FakeReply()
        output = io.StringIO()
        with contextlib.redirect_stdout(output), \
                contextlib.redirect_stderr(output):
            code = cli.cmd_support(args, sources=self.sources(),
                                   ask=lambda prompt: answers.pop(0),
                                   interactive=interactive, opener=opener)
        return code, output.getvalue(), sent

    def reports(self):
        return sorted((self.root / "printer_data/logs").glob(
            "cortex-bridge-support-*.txt"))

    def test_writing_a_report_sends_nothing(self):
        code, out, sent = self.run_support()
        self.assertEqual(code, 0)
        self.assertEqual(sent, [])
        self.assertEqual(len(self.reports()), 1)
        self.assertIn("Nothing was sent.", out)
        self.assertIn("Removed before writing:", out)
        self.assertIn("Next step `status` names: No AMS answers", out)
        self.assertNotIn("--submit", out, "no destination, no offer")

    def test_submit_without_a_destination_sends_nothing(self):
        code, out, sent = self.run_support("--submit")
        self.assertEqual(code, 1)
        self.assertEqual(sent, [])
        self.assertIn(support_submit.NO_DESTINATION, out)

    def test_submit_to_a_destination_after_a_yes(self):
        code, out, sent = self.run_support(
            "--submit", "--to", "https://support.example.org/r")
        self.assertEqual(code, 0, out)
        self.assertEqual(sent, [self.reports()[0].read_bytes()])

    def test_submit_after_a_no_sends_nothing(self):
        code, out, sent = self.run_support(
            "--submit", "--to", "https://support.example.org/r",
            answers=("n",))
        self.assertEqual(code, 1)
        self.assertEqual(sent, [])
        self.assertIn("Not sent.", out)

    def test_a_configured_destination_is_offered_not_used(self):
        conf = self.root / "printer_data/config/moonraker.conf"
        conf.write_text(conf.read_text().replace(
            "[openams_bridge]\n",
            "[openams_bridge]\nsupport_url: https://support.example.org/r\n"))
        code, out, sent = self.run_support()
        self.assertEqual((code, sent), (0, []))
        self.assertIn("run `cortex-bridge.py support --submit`", out)

    def test_an_edited_report_with_a_leak_is_not_sent(self):
        edited = self.root / "edited.txt"
        edited.write_text("report\nmy password: %s\n" % MQTT_PASSWORD)
        code, out, sent = self.run_support(
            "--submit", "--file", str(edited),
            "--to", "https://support.example.org/r")
        self.assertEqual(code, 1)
        self.assertEqual(sent, [])
        self.assertIn("still has something that looks private", out)
        self.assertNotIn(MQTT_PASSWORD, out)

    def run_status(self, facts, snapshot=None):
        asked = []

        def fake_snapshot(args):
            asked.append(True)
            return snapshot or support_report._unavailable(
                "firmware-too-old", "bridge firmware 2 has no support "
                "snapshot")
        output = io.StringIO()
        with mock.patch.object(cli, "gather", lambda args: facts), \
                mock.patch.object(cli, "status_snapshot", fake_snapshot), \
                contextlib.redirect_stdout(output):
            cli.cmd_status(types.SimpleNamespace(json=False))
        return output.getvalue(), bool(asked)

    def test_status_offers_the_report_when_something_is_wrong(self):
        text, asked = self.run_status(facts_no_ams())
        self.assertTrue(asked, "no AMS online: the snapshot is asked for")
        self.assertIn("No AMS answers. Check 24 V", text)
        self.assertIn(cli.SUPPORT_HINT, text)
        connected = facts_no_ams()
        connected["relay"]["units"] = [{"uid": UID, "online": True,
                                        "path": "/dev/oams/" + UID}]
        text, asked = self.run_status(connected)
        # Asked even with an AMS online: another may be connected with no
        # index yet. Nothing found there is still all connected.
        self.assertTrue(asked, "the snapshot is asked for with an AMS online")
        self.assertIn(cli.ALL_CONNECTED, text)
        self.assertNotIn(cli.SUPPORT_HINT, text)

    def test_status_names_what_the_bridge_saw(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from test_cortex_bridge_snapshot import VECTORS
        snapshot = support_report.bridge_support_snapshot(
            self.root / "run", request=lambda request, **kw: {
                "event": "support_snapshot", "ok": True,
                "snapshot": VECTORS["silent_line"]["decoded"], "raw": ""})
        text, _ = self.run_status(facts_no_ams(), snapshot)
        self.assertIn("No AMS answers. The bridge reports: The bridge sends "
                      "on the AMS data line and nothing comes back. The data "
                      "pair is open", text)


if __name__ == "__main__":
    unittest.main()
