"""A lane's host FPS reading that looks reversed warns once per session.

The host reads the pressure buffer through the [fps] ADC pin (optionally
inverted by `reversed:`), while the unit reads the same buffer itself and
drives loading from its own value. Nothing misbehaves when the two are
inverses -- the firmware is right and every UI draws the pressure backwards --
so the manager samples both on a loaded, idle lane and warns once.

Run with either of::

    python3 -m unittest discover -s test -p 'test_fps_orientation.py' -v
    python3 -m unittest discover -s test
"""

import unittest

from test_oams_manager import (FakeRuntime, S, build_manager,
                               single_lane_objects, single_lane_sections)

SAMPLES = 10


class FakeGcode:
    """Just the console half of Klipper's gcode object."""

    def __init__(self):
        self.responses = []

    def respond_info(self, message):
        self.responses.append(message)


def loaded_manager(host=0.21, unit=0.82, source=None, set_unit=True):
    """A single-lane manager whose lane is loaded and idle, with a host FPS
    reading `host` and a loaded unit reporting `unit`. `source` sets the
    [fps] source_oams_name (the lane then reads the unit, not a pin);
    set_unit=False leaves the unit with no reading of its own."""
    objects = single_lane_objects()
    fps = objects["fps"][0][1]
    fps.get_value = lambda: host
    fps.source_oams_name = source
    oam = objects["oams"][0][1]
    if set_unit:
        oam.fps_value = unit
    lanes = {"fps1": S.LaneState(op=S.OP_LOADED, unit=(1, 0))}
    mgr = build_manager(objects, single_lane_sections(), lanes=lanes)
    gcode = FakeGcode()
    mgr.printer.lookup_object = lambda name: gcode
    mgr.gcode = gcode
    mgr._published_pressure = {}
    return mgr


def sample(mgr, times=1):
    for _ in range(times):
        mgr._check_fps_orientation(0.0)


def set_lane(mgr, lane):
    mgr.runtime = FakeRuntime({"fps1": lane})


def set_host_reading(mgr, value):
    mgr.fpss["fps1"].get_value = lambda: value


class FpsOrientationTests(unittest.TestCase):
    def test_agreeing_readings_never_warn(self):
        mgr = loaded_manager(host=0.82, unit=0.82)
        sample(mgr, 30)
        self.assertEqual(mgr.gcode.responses, [])

    def test_inverse_readings_warn_once_on_the_tenth_sample(self):
        mgr = loaded_manager()
        sample(mgr, SAMPLES - 1)                 # nine samples: too few
        self.assertEqual(mgr.gcode.responses, [])
        with self.assertLogs(level="WARNING") as captured:
            sample(mgr, 1)
        self.assertEqual(len(mgr.gcode.responses), 1)
        message = mgr.gcode.responses[0]
        self.assertIn("[fps] 'fps1' reads 0.21", message)
        self.assertIn("oams1 reads its own pressure as 0.82", message)
        self.assertIn("reversed: true", message)
        self.assertTrue(any(message in line for line in captured.output))
        sample(mgr, 30)                          # never again this session
        self.assertEqual(len(mgr.gcode.responses), 1)

    def test_the_count_restarts_after_a_non_matching_sample(self):
        mgr = loaded_manager()
        sample(mgr, SAMPLES - 1)
        self.assertEqual(mgr.gcode.responses, [])
        set_host_reading(mgr, 0.80)               # the two now agree
        sample(mgr, 1)
        self.assertEqual(mgr.gcode.responses, [])
        set_host_reading(mgr, 0.21)               # inverse again
        sample(mgr, SAMPLES - 1)
        self.assertEqual(mgr.gcode.responses, [])
        sample(mgr, 1)
        self.assertEqual(len(mgr.gcode.responses), 1)

    def test_an_unloaded_or_busy_lane_is_not_sampled(self):
        for lane in (S.LaneState(),
                     S.LaneState(op=S.OP_LOADING, unit=(1, 0)),
                     S.LaneState(op=S.OP_UNLOADING, unit=(1, 0)),
                     S.LaneState(op=S.OP_LOADED, unit=None)):
            mgr = loaded_manager()
            set_lane(mgr, lane)
            sample(mgr, 30)
            self.assertEqual(mgr.gcode.responses, [])

    def test_leaving_loaded_and_idle_resets_the_count(self):
        mgr = loaded_manager()
        sample(mgr, SAMPLES - 1)
        set_lane(mgr, S.LaneState(op=S.OP_UNLOADED))
        sample(mgr, 1)
        set_lane(mgr, S.LaneState(op=S.OP_LOADED, unit=(1, 0)))
        sample(mgr, SAMPLES - 1)
        self.assertEqual(mgr.gcode.responses, [])
        sample(mgr, 1)
        self.assertEqual(len(mgr.gcode.responses), 1)

    def test_a_lane_reading_its_unit_is_skipped(self):
        mgr = loaded_manager(source="oams1")
        sample(mgr, 30)
        self.assertEqual(mgr.gcode.responses, [])

    def test_a_unit_without_a_reading_is_skipped(self):
        mgr = loaded_manager(set_unit=False)
        sample(mgr, 30)
        self.assertEqual(mgr.gcode.responses, [])

    def test_the_check_changes_no_lane_state_or_reading(self):
        mgr = loaded_manager()
        before_state = mgr.runtime.get_state()
        before_pressure = mgr._lane_pressure("fps1")
        sample(mgr, 30)
        self.assertEqual(mgr.runtime.get_state(), before_state)
        self.assertEqual(mgr._lane_pressure("fps1"), before_pressure)


if __name__ == "__main__":
    unittest.main()
