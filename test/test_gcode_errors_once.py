"""An error raised several scripts deep is reported once (voron350,
2026-11-12: one unload failure showed five times in the console and the log).

Klipper's GCodeDispatch._process_commands reports a command's error, then
raises the same exception on when it runs inside another script; FakeGCode
does the same, so each nesting level would report it again."""
import unittest

import test_oams_manager  # noqa: F401  (sets up the oamspkg package)
import importlib

report_gcode_errors_once = importlib.import_module(
    "oamspkg.oams_manager").report_gcode_errors_once


class FakeGCode:
    class error(Exception):
        pass

    def __init__(self):
        self.reported = []
        self.commands = {}

    def _respond_error(self, msg):
        self.reported.append(msg)

    def run_script_from_command(self, script):
        # klippy/gcode.py: report, then raise on (need_ack=False).
        for line in script.split("\n"):
            try:
                self.commands[line]()
            except self.error as e:
                self._respond_error(str(e))
                raise


def nested(gcode, depth, make_error):
    """`depth` scripts, the innermost command raising make_error()."""
    def fail():
        raise make_error()
    gcode.commands["L0"] = fail
    for level in range(1, depth):
        gcode.commands["L%d" % level] = (
            lambda inner="L%d" % (level - 1):
            gcode.run_script_from_command(inner))
    with_top = gcode.error
    try:
        gcode.run_script_from_command("L%d" % (depth - 1))
    except with_top:
        pass


class ReportOnceTests(unittest.TestCase):
    def test_without_it_every_level_reports(self):
        gcode = FakeGCode()
        nested(gcode, 5, lambda: gcode.error("Spool unloading failed"))
        self.assertEqual(len(gcode.reported), 5)

    def test_an_error_raised_five_scripts_deep_is_reported_once(self):
        gcode = FakeGCode()
        self.assertTrue(report_gcode_errors_once(gcode))
        nested(gcode, 5, lambda: gcode.error("Spool unloading failed"))
        self.assertEqual(gcode.reported, ["Spool unloading failed"])

    def test_the_next_failure_with_the_same_text_is_reported_again(self):
        gcode = FakeGCode()
        report_gcode_errors_once(gcode)
        for _ in range(2):
            nested(gcode, 3, lambda: gcode.error("Spool unloading failed"))
        self.assertEqual(gcode.reported, ["Spool unloading failed"] * 2)

    def test_a_new_error_raised_on_the_way_out_is_reported(self):
        # A level that catches the error and raises its own (a toolchange
        # resuming a print, say) reports both.
        gcode = FakeGCode()
        report_gcode_errors_once(gcode)

        def wrap():
            try:
                gcode.run_script_from_command("L0")
            except gcode.error:
                raise gcode.error("the retry failed")
        gcode.commands["L0"] = lambda: (_ for _ in ()).throw(
            gcode.error("feed failed"))
        gcode.commands["W"] = wrap
        try:
            gcode.run_script_from_command("W")
        except gcode.error:
            pass
        self.assertEqual(gcode.reported, ["feed failed", "the retry failed"])

    def test_a_report_outside_an_except_block_always_goes_out(self):
        gcode = FakeGCode()
        report_gcode_errors_once(gcode)
        gcode._respond_error("plain")
        gcode._respond_error("plain")
        self.assertEqual(gcode.reported, ["plain", "plain"])

    def test_installed_once_and_harmless_without_the_method(self):
        gcode = FakeGCode()
        self.assertTrue(report_gcode_errors_once(gcode))
        self.assertFalse(report_gcode_errors_once(gcode))
        self.assertFalse(report_gcode_errors_once(object()))


if __name__ == "__main__":
    unittest.main()
