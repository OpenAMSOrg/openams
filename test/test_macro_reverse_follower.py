"""The shipped macros ask for a reverse follower only of a unit that has one
(C1 AMS_CAP_REVERSE_FOLLOWER).

The macro text is rendered with jinja2 configured the way Klipper's
gcode_macro does (``Environment('{%', '%}', '{', '}')``) against a stub of the
printer objects it reads. Run with::

    python3 -m unittest discover -s test -p 'test_macro_reverse_follower.py' -v
"""

import configparser
import os
import re
import unittest

try:
    import jinja2
except ImportError:                                # pragma: no cover
    jinja2 = None

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MACRO_FILES = ("oams_macros.cfg", "oams_macros_ordered.cfg")
REVERSE_ENABLE = re.compile(
    r"\bOAMS(?:M)?_FOLLOWER\b(?=.*\bENABLE=1\b)(?=.*\bDIRECTION=0\b)")


def macros():
    """{name: gcode text} of every macro the repository ships."""
    found = {}
    for name in MACRO_FILES:
        path = os.path.join(_ROOT, name)
        if not os.path.exists(path):
            continue
        parser = configparser.RawConfigParser(
            inline_comment_prefixes=("#", ";"), strict=False)
        with open(path) as handle:
            parser.read_string(handle.read())
        for section in parser.sections():
            if section.startswith("gcode_macro ") and parser.has_option(
                    section, "gcode"):
                found[section.split(" ", 1)[1]] = parser.get(section, "gcode")
    return found


class AttrDict(dict):
    """A printer object as a template reads it: keys as attributes."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)


def render(text, printer, params):
    env = jinja2.Environment("{%", "%}", "{", "}")

    def raise_error(message):
        raise RuntimeError(message)

    return env.from_string(text).render(
        printer=printer, params=params, action_raise_error=raise_error,
        rawparams="", **{})


def printer_stub(capabilities):
    """Two units; the lane's loaded unit is idx 1 and its capabilities are
    ``capabilities`` (a dict; ``{}`` means the constant is absent)."""
    return AttrDict({
        "gcode_macro _oams_macro_variables": AttrDict(
            retract_length=30, extrusion_unload_length=60, unload_speed=1000),
        "oams_manager": AttrDict(
            lanes_by_fps=AttrDict(fps1=AttrDict(
                extruder="extruder", unit=[1, 0], current_slot=0)),
            topology=AttrDict(oams=AttrDict(
                u0=AttrDict(idx=0), u1=AttrDict(idx=1))),
            devices=AttrDict(
                u0=AttrDict(capabilities=AttrDict()),
                u1=AttrDict(capabilities=AttrDict(capabilities)))),
        "toolhead": AttrDict(extruder="extruder", homed_axes="xyz"),
        "pause_resume": AttrDict(is_paused=False),
    })


@unittest.skipIf(jinja2 is None, "jinja2 is not installed")
class SafeUnloadRenderTests(unittest.TestCase):
    LINE = "OAMSM_FOLLOWER ENABLE=1 DIRECTION=0 FPS=fps1"

    def output(self, capabilities):
        text = macros()["SAFE_UNLOAD_FILAMENT"]
        return render(text, printer_stub(capabilities),
                      {"FPS": "fps1", "STRICT": "1"})

    def test_a_unit_without_a_reverse_follower_is_not_asked_for_one(self):
        out = self.output({"reverse_follower": False})
        self.assertNotIn("OAMSM_FOLLOWER ENABLE=1", out)
        # ... and the rest of the unload is untouched.
        for step in ("OAMSM_STAGE NAME=cut FPS=fps1", "CUT_FILAMENT",
                     "_OAMS_FINISH_UNLOAD"):
            self.assertIn(step, out)

    def test_a_unit_reporting_one_is_asked(self):
        self.assertIn(self.LINE, self.output({"reverse_follower": True}))

    def test_a_unit_that_does_not_report_it_is_asked(self):
        self.assertIn(self.LINE, self.output({}))

    def test_only_the_lanes_own_unit_matters(self):
        printer = printer_stub({"reverse_follower": True})
        printer.oams_manager.devices.u0.capabilities["reverse_follower"] = False
        out = render(macros()["SAFE_UNLOAD_FILAMENT"], printer,
                     {"FPS": "fps1", "STRICT": "1"})
        self.assertIn(self.LINE, out)       # the loaded unit is u1

    def test_a_status_without_capabilities_or_devices_still_renders(self):
        # Real Klipper's template raises on an attribute chain through a
        # missing key, so the macro reads them with defaults.
        for mutate in (
                lambda m: m.devices.u1.pop("capabilities"),
                lambda m: m.devices.pop("u1"),
                lambda m: m.pop("devices")):
            printer = printer_stub({"reverse_follower": False})
            mutate(printer.oams_manager)
            out = render(macros()["SAFE_UNLOAD_FILAMENT"], printer,
                         {"FPS": "fps1", "STRICT": "1"})
            self.assertIn(self.LINE, out)

    def test_a_lane_with_nothing_loaded_still_renders(self):
        printer = printer_stub({"reverse_follower": False})
        printer.oams_manager.lanes_by_fps.fps1["unit"] = None
        out = render(macros()["SAFE_UNLOAD_FILAMENT"], printer,
                     {"FPS": "fps1", "STRICT": "1"})
        self.assertIn(self.LINE, out)


@unittest.skipIf(jinja2 is None, "jinja2 is not installed")
class EveryShippedMacroTests(unittest.TestCase):
    def test_every_reverse_follower_enable_is_guarded(self):
        """No shipped macro asks a unit for a reverse follower without
        checking the capability first; a new one must add the same guard."""
        checked = 0
        for name, text in macros().items():
            lines = text.splitlines()
            for number, line in enumerate(lines):
                if (REVERSE_ENABLE.search(line)
                        and not line.lstrip().startswith("#")):
                    checked += 1
                    before = "\n".join(lines[:number])
                    self.assertIn("reverse_follower", before,
                                  "%s: unguarded reverse follower: %s"
                                  % (name, line.strip()))
                    guard = [l for l in lines[:number] if "{% if" in l]
                    self.assertTrue(guard and "RF.ok" in guard[-1],
                                    "%s: not inside the guard: %s"
                                    % (name, line.strip()))
        self.assertGreaterEqual(checked, 1)     # the collector found it

    def test_the_stop_commands_are_not_reverse_enables(self):
        # ENABLE=0 is always accepted and is never guarded.
        for name, text in macros().items():
            for line in text.splitlines():
                if "ENABLE=0" in line:
                    self.assertIsNone(REVERSE_ENABLE.search(line), name)

    def test_the_ordered_macro_file_adds_no_follower_calls(self):
        path = os.path.join(_ROOT, "oams_macros_ordered.cfg")
        with open(path) as handle:
            text = handle.read()
        self.assertNotIn("FOLLOWER", text)


if __name__ == "__main__":
    unittest.main()
