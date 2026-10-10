# Tests for tools/agent/config_audit.py, the static OpenAMS config audit.
#
#   cd openams && python3 -m unittest test.test_agent_config_audit -v

import configparser
import contextlib
import io
import json
import os
import shutil
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools", "agent"))

import config_audit as A

CONFIGS = os.path.join(ROOT, "test", "fixtures", "agent", "configs")
IDLE_HELP = os.path.join(ROOT, "test", "fixtures", "agent", "idle", "gcode_help.json")

# Fixtures that include the shipped OpenAMS files. They are copied from the
# repo root so that the tests follow the shipped files.
SHIPPED_MACROS = ("clean", "custom_macros", "fresh_sample", "lanes", "macros")
# A stale copy is the shipped macro with its formatting changed, so the case
# that reports one writes the copy here.
STALE_COPY = ("custom_macros", "gcode_macro SAFE_UNLOAD_FILAMENT",
              "stale_macros.cfg")


def case_dir(case):
    return os.path.join(CONFIGS, case)


def write_stale_copy(case, section, name):
    """Write a copy of one shipped macro into the case as the owner's own
    file: the same commands, re-indented and with a comment, which is what
    copying a macro out of an old install looks like. Written from the shipped
    file so the case follows it."""
    parser = configparser.RawConfigParser(inline_comment_prefixes=("#", ";"))
    parser.read(os.path.join(case_dir(case), "oams_macros.cfg"))
    body = parser[section]["gcode"]
    lines = ["[%s]" % (section,), "gcode:",
             "    # copied from an older oams_macros.cfg"]
    lines += ["  " + line.strip() if line.strip() else "" for line in body.splitlines()]
    with open(os.path.join(case_dir(case), name), "w") as handle:
        handle.write("\n".join(lines) + "\n")


def line_of(case, name, text, nth=1):
    """The 1-based line of the nth line in a case's file that, stripped,
    equals `text`: the shipped macro and sample files change, so a finding's
    line is looked up instead of being written into the test."""
    with open(os.path.join(case_dir(case), name)) as handle:
        hits = [number for number, line in enumerate(handle, 1)
                if line.strip() == text]
    return hits[nth - 1]


def run(case, *args):
    """Audit a fixture; returns (exit code, stdout)."""
    argv = ["--config-dir", case_dir(case)] + list(args)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        status = A.main(argv)
    return status, out.getvalue()


def locations(text):
    """``CODE file:line`` of every finding line in a report."""
    found = []
    for line in text.splitlines():
        parts = line.split(" ", 3)
        if len(parts) == 4 and parts[0] in ("ERROR", "WARN", "MANUAL", "INFO"):
            found.append("%s %s:%s" % (parts[1], parts[2].split(":")[0],
                                       parts[2].split(":")[1]))
    return found


def codes_with(text, code):
    """Every ``CODE file:line`` in a report, for one code."""
    return [item for item in locations(text) if item.startswith(code + " ")]


def errors(text):
    """Every error finding, in report order."""
    return [item for item in locations(text) if item.startswith("E_")]


class ConfigAuditTest(unittest.TestCase):
    def setUp(self):
        for case in SHIPPED_MACROS:
            shutil.copyfile(os.path.join(ROOT, "oams_macros.cfg"),
                            os.path.join(case_dir(case), "oams_macros.cfg"))
        shutil.copyfile(os.path.join(ROOT, "oams_sample.cfg"),
                        os.path.join(case_dir("fresh_sample"), "oams.cfg"))
        write_stale_copy(*STALE_COPY)

    def assertSummary(self, text, errors=None, warnings=None, manual=None):
        last = text.rstrip("\n").split("\n")[-1]
        if errors is not None:
            self.assertEqual(last, "AUDIT errors=%d warnings=%d manual=%d"
                             % (errors, warnings, manual))
        return last


class CleanConfigTest(ConfigAuditTest):
    def test_no_errors(self):
        status, text = run("clean")
        self.assertEqual(status, A.EXIT_OK)
        self.assertEqual(errors(text), [])
        self.assertSummary(text, errors=0, warnings=0, manual=3)

    def test_summary_lists_units_groups_lanes(self):
        status, text = run("clean")
        summary = [f for f in locations(text) if f.startswith("I_SUMMARY")]
        self.assertEqual(summary, ["I_SUMMARY printer.cfg:1"] * 3)
        self.assertIn("units: oams1 idx=1 kind=ams1 lane=fps", text)
        self.assertIn("groups: T0=[oams1-0]", text)
        self.assertIn("lanes: fps", text)

    def test_manual_findings(self):
        status, text = run("clean")
        self.assertEqual(codes_with(text, "M_CUTTER"), ["M_CUTTER oams.cfg:9"])
        self.assertIn("variable_pre_cut_x=20", text)
        self.assertIn("must be confirmed by a human", text)
        self.assertEqual(codes_with(text, "M_FPS_ORIENTATION"),
                         ["M_FPS_ORIENTATION oams.cfg:17",
                          "M_FPS_ORIENTATION oams.cfg:22"])


class FreshSampleTest(ConfigAuditTest):
    def test_placeholders(self):
        status, text = run("fresh_sample")
        self.assertEqual(status, A.EXIT_ERRORS)
        self.assertEqual(codes_with(text, "E_PLACEHOLDER"),
                         ["E_PLACEHOLDER oams.cfg:10",
                          "E_PLACEHOLDER oams.cfg:13",
                          "E_PLACEHOLDER oams.cfg:%d" % line_of(
                              "fresh_sample", "oams.cfg",
                              "key: <32_HEX_CHARACTERS>")])

    def test_no_other_unexpected_errors(self):
        status, text = run("fresh_sample")
        self.assertEqual(sorted(set(item.split(" ")[0] for item in errors(text))),
                         ["E_MCU_TRANSPORT", "E_PLACEHOLDER", "E_TAG_KEY"])


class IncludeTest(ConfigAuditTest):
    def test_include_findings(self):
        status, text = run("includes")
        self.assertEqual(
            errors(text),
            ["E_INCLUDE_CYCLE loop_a.cfg:1",
             "E_GROUP_BAY_DUP parts/a.cfg:12",
             "E_INCLUDE_MISSING printer.cfg:4"])
        self.assertSummary(text, errors=3, warnings=0, manual=1)

    def test_cycle_is_reported_once(self):
        status, text = run("includes")
        self.assertEqual(len(codes_with(text, "E_INCLUDE_CYCLE")), 1)
        self.assertIn("loop_b.cfg -> loop_a.cfg -> loop_b.cfg", text)

    def test_inline_comments_and_commented_sections(self):
        status, text = run("includes")
        # device_kind has a trailing ';' comment and oams_idx uses '='.
        self.assertEqual(codes_with(text, "E_DEVICE_KIND"), [])
        self.assertNotIn("ghost", text)
        self.assertIn("units: oams1 idx=1 kind=ams1 lane=unset", text)

    def test_autosave_block_is_parsed(self):
        status, text = run("includes")
        # The #*# block in printer.cfg is the first [filament_group T9]; its
        # member collides with the one in parts/a.cfg.
        self.assertIn("groups: T9=[oams1-0], T0=[oams1-0]", text)
        self.assertIn("E_GROUP_BAY_DUP parts/a.cfg:12 bay 'oams1-0' is already "
                      "in [filament_group T9] at printer.cfg:8", text)


class BrokenConfigTest(ConfigAuditTest):
    def test_error_locations(self):
        status, text = run("errors")
        self.assertEqual(status, A.EXIT_ERRORS)
        self.assertEqual(
            errors(text),
            ["E_MCU_TRANSPORT printer.cfg:4",
             "E_MCU_TRANSPORT printer.cfg:10",
             "E_HDC1080 printer.cfg:13",
             "E_FPS_LANE printer.cfg:16",
             "E_OAMS_MCU printer.cfg:17",
             "E_DEVICE_KIND printer.cfg:19",
             "E_FPS_LANE printer.cfg:21",
             "E_OAMS_MCU printer.cfg:21",
             "E_UNIT_IDX_DUP printer.cfg:22",
             "E_FPS_LANE printer.cfg:27",
             "E_TAG_KEY printer.cfg:30",
             "E_TAG_KEY printer.cfg:32",
             "E_FPS_SOURCE printer.cfg:37",
             "E_FPS_SOURCE printer.cfg:39",
             "E_GROUP_BAY_DUP printer.cfg:44",
             "E_GROUP_BAY_DUP printer.cfg:44",
             "E_GROUP_MEMBER printer.cfg:47",
             "E_GROUP_MEMBER printer.cfg:50",
             "E_GROUP_MEMBER printer.cfg:53",
             "E_GROUP_NAME_DUP printer.cfg:58"])
        self.assertSummary(text, errors=20, warnings=0, manual=6)

    def test_messages_name_the_problem(self):
        status, text = run("errors")
        self.assertIn("[mcu fpz] has neither serial nor canbus_uuid", text)
        self.assertIn("canbus_uuid 'not-a-uuid' is not 12 hex digits", text)
        self.assertIn("names mcu 'missing_mcu'", text)
        self.assertIn("[oams oams2] has no mcu:", text)
        self.assertIn("reuses index 1 of [oams oams1] at printer.cfg:18", text)
        self.assertIn("device_kind 'ams3' is not one of", text)
        self.assertIn("no [fps nosuchlane] is defined", text)
        self.assertIn("key has 8 hex digits, not 32", text)
        self.assertIn("[bambu_lab_tag_processor reader] has no key:", text)
        self.assertIn("needs exactly one of pin: and source_oams:", text)
        self.assertIn("member 'oams2-4' is out of range for [oams oams2] (0-3)", text)
        self.assertIn("member 'nosuch-0' names no [oams nosuch]", text)
        self.assertIn("member 'oams1' is not <unit>-<bay>", text)
        self.assertIn("bay 'oams1-0' is listed twice", text)
        self.assertIn("already in [filament_group T0] at printer.cfg:44", text)
        self.assertIn("[filament_group T4] is already defined at printer.cfg:55", text)


class TwoLaneTest(ConfigAuditTest):
    def test_error_locations(self):
        status, text = run("lanes")
        self.assertEqual(errors(text),
                         ["E_FPS_LANE oams.cfg:35",
                          "E_OAMS_MCU oams.cfg:35",
                          "E_GROUP_LANE_MIX oams.cfg:38",
                          "E_GROUP_MEMBER oams.cfg:41"])

    def test_lanes_resolve_per_unit(self):
        status, text = run("lanes")
        self.assertIn("lanes: fps1, fps2", text)
        self.assertIn("units: oams1 idx=1 kind=auto lane=fps1, "
                      "oams2 idx=2 kind=auto lane=fps2, "
                      "ht1 idx=3 kind=ams_ht lane=fps1", text)

    def test_messages(self):
        status, text = run("lanes")
        self.assertIn("[oams free1] has no fps: but 2 fps lanes are defined", text)
        self.assertIn("[filament_group T0] mixes fps lanes fps1 and fps2", text)
        self.assertIn("member 'ht1-1' is out of range for [oams ht1] (0-0)", text)

    def test_same_lane_group_is_accepted(self):
        status, text = run("lanes")
        # t2 spans oams1-2 (fps1) and ht1-0 (fps1): one lane, no finding.
        self.assertNotIn("t2", "".join(codes_with(text, "E_GROUP_LANE_MIX")))


class MacroTest(ConfigAuditTest):
    def test_a_stale_copy_keeps_the_collision_code(self):
        status, text = run("custom_macros")
        self.assertEqual(codes_with(text, "E_MACRO_COLLISION"),
                         ["E_MACRO_COLLISION oams_macros.cfg:%d" % line_of(
                             "custom_macros", "oams_macros.cfg",
                             "[gcode_macro SAFE_UNLOAD_FILAMENT]")])
        self.assertIn("[gcode_macro SAFE_UNLOAD_FILAMENT] is also defined at "
                      "stale_macros.cfg:1 with the same body", text)
        self.assertIn("comment out that copy", text)

    def test_a_customization_names_the_override_to_move_it_into(self):
        status, text = run("custom_macros")
        self.assertEqual(codes_with(text, "E_MACRO_CUSTOMIZED"),
                         ["E_MACRO_CUSTOMIZED oams_macros.cfg:%d" % line_of(
                             "custom_macros", "oams_macros.cfg",
                             "[gcode_macro CUT_FILAMENT]")])
        self.assertIn("[gcode_macro CUT_FILAMENT] is also defined at "
                      "user_macros.cfg:%d with a different body"
                      % line_of("custom_macros", "user_macros.cfg",
                                "[gcode_macro CUT_FILAMENT]"), text)
        self.assertIn("move the added lines into OAMS_CUT in your own file "
                      "and comment out that copy", text)

    def test_the_owner_own_macros_and_hooks_are_no_findings(self):
        status, text = run("custom_macros")
        # OAMS_CUT, the hook and the macro the hook calls are the printer's
        # own, and OpenAMS ships none of them: a macro finding is reported in
        # oams_macros.cfg, where the shipped copy is.
        for item in errors(text):
            if item.startswith("E_MACRO_"):
                self.assertIn("oams_macros.cfg", item)

    def test_collision_names_both_locations(self):
        status, text = run("macros")
        self.assertEqual(codes_with(text, "E_MACRO_COLLISION"), [])
        self.assertEqual(codes_with(text, "E_MACRO_CUSTOMIZED"),
                         ["E_MACRO_CUSTOMIZED oams_macros.cfg:%d" % line_of(
                             "macros", "oams_macros.cfg",
                             "[gcode_macro CUT_FILAMENT]")])
        self.assertIn("[gcode_macro cut_filament] is also defined at "
                      "extra_macros.cfg:1", text)
        self.assertIn("move the added lines into OAMS_CUT", text)

    def test_pause_resume_missing(self):
        status, text = run("macros")
        self.assertEqual(codes_with(text, "E_PAUSE_RESUME"),
                         ["E_PAUSE_RESUME oams_macros.cfg:%d" % line_of(
                             "macros", "oams_macros.cfg",
                             "[gcode_macro _TX]")])

    def test_tool_macro_outside_the_macros_file(self):
        status, text = run("macros")
        self.assertEqual(codes_with(text, "W_TOOL_MACRO"),
                         ["W_TOOL_MACRO extra_macros.cfg:5"])
        self.assertIn("which does not define t4", text)

    def test_unverified_without_commands(self):
        status, text = run("macros")
        self.assertEqual(codes_with(text, "M_MACRO_UNVERIFIED"),
                         ["M_MACRO_UNVERIFIED oams_macros.cfg:%d" % line_of(
                             "macros", "oams_macros.cfg", "CLEAN_NOZZLE")])
        self.assertIn("verify at runtime", text)
        self.assertEqual(codes_with(text, "E_MACRO_MISSING"), [])

    def test_missing_command_with_commands(self):
        status, text = run("macros", "--commands",
                           os.path.join(case_dir("macros"), "commands.json"))
        self.assertEqual(codes_with(text, "E_MACRO_MISSING"),
                         ["E_MACRO_MISSING oams_macros.cfg:%d" % line_of(
                             "macros", "oams_macros.cfg", "CLEAN_NOZZLE")])
        self.assertIn("CLEAN_NOZZLE", text)
        self.assertEqual(codes_with(text, "M_MACRO_UNVERIFIED"), [])

    def test_real_printer_commands_register_everything(self):
        status, text = run("fresh_sample", "--commands", IDLE_HELP)
        self.assertEqual(codes_with(text, "E_MACRO_MISSING"), [])
        self.assertEqual(codes_with(text, "M_MACRO_UNVERIFIED"), [])


class ForeignSystemTest(ConfigAuditTest):
    def test_warning_locations(self):
        status, text = run("foreign")
        self.assertEqual(
            [item for item in locations(text) if item.startswith("W_")],
            ["W_FOREIGN_MMU AFC/afc.cfg:1",
             "W_FOREIGN_MMU printer.cfg:2",
             "W_FOREIGN_MMU printer.cfg:3",
             "W_FOREIGN_MMU printer.cfg:4",
             "W_FOREIGN_MMU printer.cfg:11",
             "W_FOREIGN_MMU printer.cfg:14",
             "W_FOREIGN_MMU printer.cfg:17",
             "W_FOREIGN_MMU printer.cfg:19",
             "W_FOREIGN_MMU printer.cfg:21",
             "W_TOOL_MACRO printer.cfg:27"])
        self.assertSummary(text, errors=1, warnings=10, manual=6)

    def test_include_paths_are_reported_verbatim(self):
        status, text = run("foreign")
        self.assertIn("include 'AFC/afc.cfg' pulls in another MMU system", text)

    def test_manual_findings(self):
        status, text = run("foreign")
        self.assertEqual(codes_with(text, "M_PTFE_ZERO"),
                         ["M_PTFE_ZERO printer.cfg:43"])
        self.assertEqual(codes_with(text, "M_RFID_FIRMWARE"),
                         ["M_RFID_FIRMWARE printer.cfg:51"])
        self.assertEqual(codes_with(text, "M_CUTTER"),
                         ["M_CUTTER printer.cfg:31"])
        self.assertIn("needs mainboard firmware 2.0.25 or later", text)

    def test_follower_bay_range(self):
        status, text = run("foreign")
        self.assertEqual(errors(text),
                         ["E_GROUP_MEMBER printer.cfg:55"])
        self.assertIn("out of range for [follower belay] (0-0)", text)


class OutputFormatTest(ConfigAuditTest):
    def test_json_shape(self):
        status, text = run("clean", "--json")
        data = json.loads(text)
        self.assertEqual(sorted(data), ["findings", "summary"])
        self.assertEqual(data["summary"], {"errors": 0, "warnings": 0, "manual": 3})
        for finding in data["findings"]:
            self.assertEqual(sorted(finding),
                             ["code", "file", "level", "line", "message"])
            self.assertIsInstance(finding["line"], int)
        self.assertEqual([f["code"] for f in data["findings"]],
                         ["M_CUTTER", "M_FPS_ORIENTATION", "M_FPS_ORIENTATION",
                          "I_SUMMARY", "I_SUMMARY", "I_SUMMARY"])
        self.assertNotIn("AUDIT", text)

    def test_findings_are_sorted(self):
        status, text = run("foreign")
        rank = {"ERROR": 0, "WARN": 1, "MANUAL": 2, "INFO": 3}
        keys = []
        for line in text.splitlines():
            parts = line.split(" ", 3)
            if len(parts) == 4 and parts[0] in rank:
                file, _, line_no = parts[2].rpartition(":")
                keys.append((rank[parts[0]], file, int(line_no)))
        self.assertEqual(keys, sorted(keys))

    def test_alternate_printer_cfg_name(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            status, text = run("clean", "--printer-cfg", "nope.cfg")
        self.assertEqual(status, A.EXIT_USAGE)
        self.assertEqual(text, "")
        self.assertIn("nope.cfg", err.getvalue())


class ExitCodeTest(ConfigAuditTest):
    def test_clean_config_exits_zero(self):
        status, text = run("clean")
        self.assertEqual(status, A.EXIT_OK)

    def test_errors_exit_one(self):
        status, text = run("errors")
        self.assertEqual(status, A.EXIT_ERRORS)

    def test_missing_config_dir_exits_two(self):
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = A.main(["--config-dir", os.path.join(CONFIGS, "nosuchdir")])
        self.assertEqual(status, A.EXIT_USAGE)
        self.assertIn("does not exist", err.getvalue())

    def test_unreadable_commands_file_exits_two(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            status = A.main(["--config-dir", case_dir("clean"),
                             "--commands", os.path.join(CONFIGS, "errors",
                                                        "nosuch.json")])
        self.assertEqual(status, A.EXIT_USAGE)
        self.assertIn("commands file", err.getvalue())

class RealConfigShapesTest(unittest.TestCase):
    """Shapes found on a real printer that must not be reported."""

    def audit(self, files):
        import tempfile
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        for name, text in files.items():
            with open(os.path.join(tmp, name), "w") as f:
                f.write(text)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = A.main(["--config-dir", tmp])
        return status, out.getvalue()

    def test_board_pin_aliases_name_power_pins(self):
        # Klipper's [board_pins] aliases call the board's power pins <GND>
        # and <5V>: they are not template placeholders.
        status, text = self.audit({"printer.cfg": (
            "[board_pins]\n"
            "aliases:\n"
            "    EXP1_1=PE8, EXP1_2=PE7,\n"
            "    EXP1_9=<GND>, EXP1_10=<5V>\n")})
        self.assertEqual(codes_with(text, "E_PLACEHOLDER"), [], text)
        self.assertEqual(status, 0, text)

    def test_the_ordered_overlay_redefines_tx_on_purpose(self):
        # oams_macros_ordered.cfg only adds render_mode: ordered to the
        # shipped _TX (README, "Enabling concurrent toolchanges").
        status, text = self.audit({
            "printer.cfg": "[include oams_macros.cfg]\n[include oams_macros_ordered.cfg]\n",
            "oams_macros.cfg": "[gcode_macro _TX]\ngcode:\n    M400\n",
            "oams_macros_ordered.cfg": "[gcode_macro _TX]\nrender_mode: ordered\n",
        })
        self.assertEqual(codes_with(text, "E_MACRO_COLLISION"), [], text)
