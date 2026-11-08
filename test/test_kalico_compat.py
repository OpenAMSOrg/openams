"""OpenAMS on Kalico, and on mainline Klipper before 2026-02.

Mainline Klipper renamed ``MCU.register_response(callback, name, oid)`` to
``register_serial_response(callback, msgformat, oid)`` and added
``check_valid_response`` on 2026-02-26, and changed the ADC API on 2026-02-14.
Kalico still has the older calls. On Kalico the driver failed at connect with
``'MCU' object has no attribute 'register_serial_response'`` (2026-11-08).

These tests check the register and dictionary adapters in oams.py,
follower.py and external_irq.py, and the FPS ADC setup, against both APIs.
test_vams_kalico.py runs the real driver's negotiation and scenarios through
an MCU that has only the older calls.
"""

import importlib
import os
import sys
import types
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _path in (os.path.join(_ROOT, "src"), _HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from test_oams_driver import oams                   # noqa: E402

if "oamspkg" not in sys.modules:
    _pkg = types.ModuleType("oamspkg")
    _pkg.__path__ = [os.path.join(_ROOT, "src")]
    sys.modules["oamspkg"] = _pkg
sys.modules.setdefault("mcu", types.ModuleType("mcu"))
follower = importlib.import_module("oamspkg.follower")
external_irq = importlib.import_module("oamspkg.external_irq")
fps = importlib.import_module("oamspkg.fps")


class _MainlineMCU(object):
    def __init__(self):
        self.calls = []

    def register_serial_response(self, callback, msgformat, oid=None):
        self.calls.append((msgformat, oid))


class _OldMCU(object):
    def __init__(self):
        self.calls = []

    def register_response(self, callback, name, oid=None):
        self.calls.append((name, oid))


class RegisterAdapterTests(unittest.TestCase):
    FORMAT = "follower_stats oid=%c pre=%c"

    def test_each_module_uses_the_call_the_klipper_has(self):
        for module in (oams, follower, external_irq):
            register = getattr(module, "register_serial_response", None) \
                or module._register_serial_response
            with self.subTest(module=module.__name__):
                new = _MainlineMCU()
                register(new, None, self.FORMAT, 3)
                self.assertEqual(new.calls, [(self.FORMAT, 3)])
                old = _OldMCU()
                register(old, None, self.FORMAT, 3)
                self.assertEqual(old.calls, [("follower_stats", 3)])

    def test_check_valid_response_without_the_mainline_call(self):
        mcu = types.SimpleNamespace(
            try_lookup_command=lambda fmt: object() if fmt == "a x=%c"
            else None)
        self.assertTrue(oams.check_valid_response(mcu, "a x=%c"))
        self.assertFalse(oams.check_valid_response(mcu, "a x=%u"))
        self.assertFalse(oams.check_valid_response(object(), "a x=%c"))


class _KalicoADC(object):
    """Kalico's MCU_adc (and mainline 2024-06 .. 2026-02)."""

    def __init__(self):
        self.sample_args = self.report_time = self.callback = None

    def setup_adc_sample(self, sample_time, sample_count, minval=0.0,
                         maxval=1.0, range_check_count=0):
        self.sample_args = (sample_time, sample_count)

    def setup_adc_callback(self, report_time, callback):
        self.report_time, self.callback = report_time, callback


class _MinmaxADC(object):
    """Mainline before 2024-06."""

    def __init__(self):
        self.sample_args = self.report_time = self.callback = None

    def setup_minmax(self, sample_time, sample_count, minval=0.0, maxval=1.0,
                     range_check_count=0):
        self.sample_args = (sample_time, sample_count)

    def setup_adc_callback(self, report_time, callback):
        self.report_time, self.callback = report_time, callback


class FpsAdcTests(unittest.TestCase):
    def make(self, adc):
        from test_fps import FakeConfig
        config = FakeConfig({"pin": "fps:PA2"})
        config.printer.pins.adc = adc
        return fps.FPS(config)

    def test_kalico_adc(self):
        adc = _KalicoADC()
        sensor = self.make(adc)
        self.assertEqual(adc.sample_args, (0.005, 5))
        self.assertEqual(adc.report_time, 0.1)
        adc.callback(1.0, 0.37)
        self.assertAlmostEqual(sensor.get_value(), 0.37)

    def test_pre_2024_adc(self):
        adc = _MinmaxADC()
        sensor = self.make(adc)
        self.assertEqual(adc.sample_args, (0.005, 5))
        adc.callback(1.0, 0.61)
        self.assertAlmostEqual(sensor.get_value(), 0.61)


if __name__ == "__main__":
    unittest.main()
