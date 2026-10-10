# SPDX-License-Identifier: GPL-3.0-or-later
"""Installed extra exposes an explicitly configured physical-only unit."""
import threading
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from host import KlipperMMU


class ConfigError(Exception):
    pass


class Printer:
    config_error = ConfigError

    def __init__(self):
        self.mcu = object()
        self.events = []
        self.gcode = types.SimpleNamespace(
            register_mux_command=lambda *args, **kwargs: None)

    def lookup_objects(self, name):
        return []

    def lookup_object(self, name):
        assert name == 'gcode'
        return self.gcode

    get_reactor = lambda self: types.SimpleNamespace(mutex=threading.Lock)
    def register_event_handler(self, event, callback):
        self.events.append(event)


class Config:
    def __init__(self, printer, options):
        self.printer, self.options = printer, options

    def get_printer(self):
        return self.printer

    def get_name(self):
        return 'klipper_mmu box'

    def get(self, name, default=None):
        return self.options.get(name, default)

    def getboolean(self, name, default=False):
        return default


OPTIONS = dict(openams_idx='7', openams_fps='fps_left',
               openams_family='boxturtle', openams_bays='4',
               openams_path_length_mm='842.5')


def make(options):
    printer = Printer()
    module = types.SimpleNamespace(get_printer_mcu=lambda p, n: p.mcu)
    with patch.dict(sys.modules, {'mcu': module}):
        return printer, KlipperMMU(Config(printer, options))


class RegistrationTests(unittest.TestCase):
    def test_default_has_no_openams_unit(self):
        printer, extra = make({})
        self.assertIsNone(extra.get_openams_unit())
        self.assertEqual(extra.get_mcu(), printer.mcu)

    def test_explicit_topology_and_physical_cache(self):
        _, extra = make(OPTIONS)
        unit = extra.get_openams_unit()
        self.assertEqual((unit.oams_idx, unit.fps_name, unit.device_family,
                          unit.bay_count, unit.path_length_mm),
                         (7, 'fps_left', 'boxturtle', 4, 842.5))
        self.assertIs(unit._observer, extra)
        self.assertFalse(unit.motion_available)
        self.assertFalse(unit.hardware_snapshot_ready)
        runtime = types.SimpleNamespace(dispatch=lambda action: None)
        unit.bind_runtime(runtime, 'fps_left')
        self.assertIs(unit._runtime, runtime)
        with self.assertRaises(ValueError):
            unit.bind_runtime(runtime, 'other_fps')
        with self.assertRaises(ValueError):
            unit.bind_runtime(object(), 'fps_left')

    def test_partial_and_invalid_metadata_rejected_before_side_effects(self):
        for options in ({'openams_idx': '7'},
                        dict(OPTIONS, openams_bays='0'),
                        dict(OPTIONS, openams_path_length_mm='nan'),
                        dict(OPTIONS, openams_idx='1.5'),
                        dict(OPTIONS, openams_family='')):
            with self.subTest(options=options):
                printer = Printer()
                module = types.SimpleNamespace(
                    get_printer_mcu=lambda p, n: self.fail('MCU allocated'))
                with patch.dict(sys.modules, {'mcu': module}):
                    with self.assertRaises(ConfigError):
                        KlipperMMU(Config(printer, options))
                self.assertEqual(printer.events, [])

    def test_read_only_unit_fails_closed_for_every_command(self):
        _, extra = make(OPTIONS)
        unit = extra.get_openams_unit()
        for method, args in (
                ('start_load_spool', (0, 1)),
                ('start_unload_spool', (1,)),
                ('load_spool_cancel', ()),
                ('set_oams_follower', (True, 0)),
                ('start_calibrate', ('ptfe', 0, 1)),
                ('clear_errors', ()),
                ('set_led_error', (0, True))):
            with self.subTest(method=method):
                with self.assertRaises(RuntimeError):
                    getattr(unit, method)(*args)


if __name__ == '__main__':
    unittest.main()
