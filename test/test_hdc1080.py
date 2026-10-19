"""src/hdc1080.py registers its sensor type in any section order: the
installer puts [include oams.cfg], whose first section is [hdc1080], above
[printer], so Klipper reads it before any heater section exists."""
import importlib
import os
import sys
import types
import unittest

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if "oamspkg" not in sys.modules:
    _pkg = types.ModuleType("oamspkg")
    _pkg.__path__ = [_SRC]
    sys.modules["oamspkg"] = _pkg
sys.modules.setdefault("oamspkg.bus", types.ModuleType("oamspkg.bus"))
hdc1080 = importlib.import_module("oamspkg.hdc1080")


class ConfigError(Exception):
    pass


class FakeHeaters:
    def __init__(self):
        self.sensor_factories = {}

    def add_sensor_factory(self, sensor_type, factory):
        self.sensor_factories[sensor_type] = factory


class FakePrinter:
    """Klipper's object registry: lookup_object fails for an object no
    section has created yet; load_object creates it on first use."""

    def __init__(self):
        self.objects = {}

    def lookup_object(self, name):
        if name not in self.objects:
            raise ConfigError("Unknown config object '%s'" % (name,))
        return self.objects[name]

    def load_object(self, config, section):
        if section not in self.objects:
            self.objects[section] = {"heaters": FakeHeaters}[section]()
        return self.objects[section]


class FakeConfig:
    def __init__(self, printer):
        self.printer = printer

    def get_printer(self):
        return self.printer


class LoadOrderTests(unittest.TestCase):
    def test_registers_before_any_heater_section(self):
        printer = FakePrinter()
        hdc1080.load_config(FakeConfig(printer))
        self.assertIs(printer.objects["heaters"].sensor_factories["HDC1080"],
                      hdc1080.HDC1080)

    def test_registers_into_the_existing_heaters(self):
        printer = FakePrinter()
        heaters = printer.load_object(None, "heaters")
        hdc1080.load_config(FakeConfig(printer))
        self.assertIs(printer.objects["heaters"], heaters)
        self.assertIn("HDC1080", heaters.sensor_factories)


if __name__ == "__main__":
    unittest.main()
