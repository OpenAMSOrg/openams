"""After an OpenAMS update: Moonraker's update entry, and whether the bridge
relay still runs the code it started with."""

from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cortex_bridge import code_version, moonraker_asvc, moonraker_conf  # noqa: E402,E501

OLD_ENTRY = """\
[server]
host: 0.0.0.0

[update_manager openams]
type: git_repo
path: ~/openams
origin: https://github.com/OpenAMSOrg/openams.git
primary_branch: main
managed_services: cortex-bridge klipper

[update_manager mainsail]
type: web
"""


class UpdateEntryTests(unittest.TestCase):
    def test_the_relay_moonraker_refuses_is_replaced_by_moonraker(self):
        text = moonraker_conf.fixed(OLD_ENTRY)
        self.assertIn("managed_services: klipper moonraker\n", text)
        self.assertNotIn("cortex-bridge", text)
        self.assertEqual(text.replace("managed_services: klipper moonraker",
                                      "managed_services: cortex-bridge "
                                      "klipper"), OLD_ENTRY,
                         "nothing else changes")

    def test_it_is_done_once(self):
        once = moonraker_conf.fixed(OLD_ENTRY)
        self.assertEqual(moonraker_conf.fixed(once), once)

    def test_an_entry_without_the_line_gets_it_inside_the_entry(self):
        text = moonraker_conf.fixed(
            "[update_manager openams]\ntype: git_repo\nprimary_branch: main\n"
            "\n[update_manager mainsail]\ntype: web\n")
        entry = text.split("[update_manager mainsail]")[0]
        self.assertIn("primary_branch: main\nmanaged_services: klipper "
                      "moonraker\n", entry)
        self.assertNotIn("managed_services",
                         text.split("[update_manager mainsail]")[1])

    def test_klipper_alone_gains_moonraker(self):
        self.assertIn("managed_services: klipper moonraker",
                      moonraker_conf.fixed("[update_manager openams]\n"
                                           "managed_services: klipper\n"))

    def test_a_value_over_several_lines_and_other_sections_are_handled(self):
        text = moonraker_conf.fixed(
            "[update_manager openams]\nmanaged_services:\n  klipper\n"
            "  cortex-bridge\n# a note\n[update_manager timelapse]\n"
            "managed_services: klipper moonraker other\n")
        self.assertEqual(text, "[update_manager openams]\nmanaged_services: "
                         "klipper moonraker\n# a note\n[update_manager "
                         "timelapse]\nmanaged_services: klipper moonraker "
                         "other\n")

    def test_no_entry_changes_nothing(self):
        self.assertEqual(moonraker_conf.fixed("[server]\n"), "[server]\n")

    def test_the_file_keeps_a_dated_backup_and_only_when_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "moonraker.conf")
            path.write_text(OLD_ENTRY)
            result = moonraker_conf.fix_file(path)
            self.assertTrue(result["changed"])
            self.assertEqual(result["line"],
                             "managed_services: klipper moonraker")
            self.assertEqual(result["backup"].read_text(), OLD_ENTRY)
            again = moonraker_conf.fix_file(path)
            self.assertFalse(again["changed"])
            self.assertIsNone(again["backup"])
            self.assertEqual(len(list(Path(tmp).glob("moonraker.conf.bak-*"))),
                             1)
            self.assertFalse(moonraker_conf.fix_file(
                Path(tmp, "missing.conf"))["changed"])


# Moonraker's own default list, written without a final newline.
DEFAULT_ASVC = ("klipper_mcu\nwebcamd\nMoonCord\nKlipperScreen\n"
                "moonraker-telegram-bot\nsonar\ncrowsnest")
# What an earlier installer left after two runs on such a file.
GLUED_ASVC = DEFAULT_ASVC + "cortex-bridge\ncortex-bridge\n"
FIXED_ASVC = DEFAULT_ASVC + "\ncortex-bridge\n"


class AllowedServicesTests(unittest.TestCase):
    """moonraker.asvc: never glue a name on, and repair one glued on."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name, "moonraker.asvc")

    def backups(self):
        return list(Path(self.tmp.name).glob("moonraker.asvc.bak-*"))

    def test_adding_to_a_file_without_a_final_newline(self):
        self.path.write_text(DEFAULT_ASVC)
        result = moonraker_asvc.allow(self.path)
        self.assertTrue(result["added"])
        self.assertEqual(self.path.read_text(), FIXED_ASVC)
        again = moonraker_asvc.allow(self.path)
        self.assertFalse(again["added"])
        self.assertFalse(again["changed"])
        self.assertEqual(self.path.read_text(), FIXED_ASVC)

    def test_the_glued_name_is_split_and_the_repeat_dropped(self):
        self.path.write_text(GLUED_ASVC)
        result = moonraker_asvc.repair(self.path)
        self.assertTrue(result["changed"])
        self.assertEqual(self.path.read_text(), FIXED_ASVC)
        self.assertEqual([b.read_text() for b in self.backups()],
                         [GLUED_ASVC])
        self.assertFalse(moonraker_asvc.repair(self.path)["changed"])
        self.assertEqual(len(self.backups()), 1)

    def test_allow_repairs_first_and_adds_nothing_twice(self):
        self.path.write_text(DEFAULT_ASVC + "cortex-bridge\n")
        result = moonraker_asvc.allow(self.path)
        self.assertFalse(result["added"], "the name was there, glued")
        self.assertEqual(self.path.read_text(), FIXED_ASVC)

    def test_names_on_one_line_go_one_per_line(self):
        self.path.write_text("klipper_mcu webcamd crowsnestcortex-bridge "
                             "cortex-bridge")
        moonraker_asvc.repair(self.path)
        self.assertEqual(self.path.read_text(),
                         "klipper_mcu\nwebcamd\ncrowsnest\ncortex-bridge\n")

    def test_repeats_are_dropped_in_order(self):
        self.path.write_text("sonar\ncortex-bridge\nsonar\ncrowsnest\n"
                             "cortex-bridge\n")
        moonraker_asvc.repair(self.path)
        self.assertEqual(self.path.read_text(),
                         "sonar\ncortex-bridge\ncrowsnest\n")

    def test_a_clean_file_stays_byte_identical(self):
        for text in (FIXED_ASVC, DEFAULT_ASVC, "klipper\n\nmoonraker\n",
                     "crowsnest.service\ncortex-bridge\n"):
            self.path.write_bytes(text.encode())
            self.assertFalse(moonraker_asvc.repair(self.path)["changed"])
            self.assertEqual(self.path.read_bytes(), text.encode())
        self.assertEqual(self.backups(), [])

    def test_only_the_exact_relay_suffix_is_split(self):
        text = "crowsnestwebcamd\ncortex-bridge-gateway\n"
        self.assertEqual(moonraker_asvc.repaired(text), text)

    def test_a_missing_file_stays_missing(self):
        for action in (moonraker_asvc.repair, moonraker_asvc.allow,
                       moonraker_asvc.disallow):
            self.assertFalse(action(self.path)["changed"])
        self.assertFalse(self.path.exists())

    def test_disallow_takes_the_relay_out_glued_or_not(self):
        self.path.write_text(GLUED_ASVC)
        moonraker_asvc.disallow(self.path)
        self.assertEqual(self.path.read_text(), DEFAULT_ASVC + "\n")


class CodeVersionTests(unittest.TestCase):
    def tree(self, tmp):
        package = Path(tmp, "cortex_bridge")
        package.mkdir()
        for name in ("relay.py", "cortexd.py", "updater.py"):
            (package / name).write_text("# %s\n" % name)
        return Path(tmp)

    def status(self, root, files=("cortex_bridge/relay.py",
                                  "cortex_bridge/cortexd.py")):
        return {"state": "running", "code": {
            "root": str(root), "files": list(files),
            "fingerprint": code_version.fingerprint(files, root)}}

    def test_the_same_files_are_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.tree(tmp)
            self.assertIs(code_version.outdated(self.status(root)), False)

    def test_a_changed_or_missing_file_is_outdated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.tree(tmp)
            status = self.status(root)
            (root / "cortex_bridge/cortexd.py").write_text("# changed\n")
            self.assertIs(code_version.outdated(status), True)
            status = self.status(root)
            (root / "cortex_bridge/relay.py").unlink()
            self.assertIs(code_version.outdated(status), True)

    def test_a_file_the_relay_does_not_use_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.tree(tmp)
            status = self.status(root)
            (root / "cortex_bridge/cli.py").write_text("# new\n")
            self.assertIs(code_version.outdated(status), False)

    def test_a_relay_from_before_the_record_is_outdated(self):
        self.assertIs(code_version.outdated({"state": "running"}), True)

    def test_no_relay_is_neither(self):
        self.assertIsNone(code_version.outdated(None))
        self.assertIsNone(code_version.outdated({"state": "stopped"}))

    def test_the_relay_lists_its_own_modules(self):
        import cortex_bridge.relay  # noqa: F401
        snapshot = code_version.snapshot()
        for name in ("cortex_bridge/relay.py", "cortex_bridge/cortexd.py",
                     "cortex_bridge/updater.py",
                     "cortex_bridge/code_version.py"):
            self.assertIn(name, snapshot["files"])
        self.assertIs(code_version.outdated(dict(
            state="running", code=snapshot)), False)


if __name__ == "__main__":
    unittest.main()
