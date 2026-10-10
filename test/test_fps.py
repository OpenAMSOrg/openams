import importlib
import os
import sys
import types
import unittest


_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if "oamspkg" not in sys.modules:
    pkg = types.ModuleType("oamspkg")
    pkg.__path__ = [_SRC]
    sys.modules["oamspkg"] = pkg
FPS = importlib.import_module("oamspkg.fps").FPS


class FakeADC:
    def __init__(self):
        self.sample_args = None
        self.callback = None

    def setup_adc_sample(self, report_time, sample_time=None,
                         sample_count=None):
        self.sample_args = (report_time, sample_time, sample_count)

    def setup_adc_callback(self, callback):
        self.callback = callback


class FakePins:
    def __init__(self):
        self.adc = FakeADC()
        self.calls = []

    def setup_pin(self, pin_type, pin):
        self.calls.append((pin_type, pin))
        return self.adc


class FakePrinter:
    def __init__(self):
        self.reactor = object()
        self.pins = FakePins()
        self.extruder = object()
        self.objects = {"pins": self.pins, "extruder": self.extruder}
        self.handlers = {}

    def get_reactor(self):
        return self.reactor

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)

    def register_event_handler(self, event, handler):
        self.handlers[event] = handler

    def config_error(self, message):
        return RuntimeError(message)


class FakeConfig:
    def __init__(self, values):
        self.values = values
        self.printer = FakePrinter()

    def get_printer(self):
        return self.printer

    def get_name(self):
        return "fps"

    def get(self, name, default=None):
        return self.values.get(name, default)

    def getint(self, name, default=None):
        return int(self.values.get(name, default))

    def getfloat(self, name, default=None):
        return float(self.values.get(name, default))

    def getboolean(self, name, default=None):
        return bool(self.values.get(name, default))

    def error(self, message):
        return ValueError(message)


class FPSConfigTests(unittest.TestCase):
    def test_requires_exactly_one_value_source(self):
        with self.assertRaisesRegex(ValueError, "exactly one"):
            FPS(FakeConfig({}))
        with self.assertRaisesRegex(ValueError, "exactly one"):
            FPS(FakeConfig({"pin": "mcu:PA2", "source_oams": "ht"}))

    def test_source_oams_resolves_and_tracks_live_telemetry(self):
        config = FakeConfig({"source_oams": "ht"})
        source = types.SimpleNamespace(fps_value=0.24)
        config.printer.objects["oams ht"] = source
        fps = FPS(config)

        self.assertEqual(config.printer.pins.calls, [])
        fps._handle_connect()
        self.assertIs(fps.extruder, config.printer.extruder)
        self.assertAlmostEqual(fps.get_status(0.)["fps_value"], 0.24)
        source.fps_value = 0.81
        self.assertAlmostEqual(fps.get_value(), 0.81)

    def test_source_oams_honors_reversed(self):
        config = FakeConfig({"source_oams": "oams ht", "reversed": True})
        config.printer.objects["oams ht"] = types.SimpleNamespace(
            fps_value=0.18)
        fps = FPS(config)
        fps._handle_connect()
        self.assertAlmostEqual(fps.get_value(), 0.82)

    def test_mainline_adc_mode_is_unchanged(self):
        config = FakeConfig({"pin": "fps:PA2"})
        fps = FPS(config)
        self.assertEqual(config.printer.pins.calls, [("adc", "fps:PA2")])
        self.assertEqual(config.printer.pins.adc.sample_args,
                         (0.1, 0.005, 5))
        config.printer.pins.adc.callback([(1.0, 0.12), (1.1, 0.43)])
        self.assertAlmostEqual(fps.get_value(), 0.43)

    def test_missing_source_is_reported_at_connect(self):
        fps = FPS(FakeConfig({"source_oams": "missing"}))
        with self.assertRaisesRegex(RuntimeError, "was not found"):
            fps._handle_connect()


if __name__ == "__main__":
    unittest.main(verbosity=2)
