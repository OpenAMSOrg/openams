# SPDX-License-Identifier: GPL-3.0-or-later
import threading
import json
import os
from contextlib import nullcontext
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))
from host import KlipperMMU
from host.bowden_estimate import BowdenEstimate
from host.protocol import Event, Identity

INSTANCE = 0x1020304050607080
CONSTANTS = {'KLIPPER_MMU_DEVICE_ID': 2, 'KLIPPER_MMU_LANE_COUNT': 4,
             'KLIPPER_MMU_CONFIG_ENCODER': 0, 'KLIPPER_MMU_CONFIG_DIFF_BUFFER': 1,
             'KLIPPER_MMU_HOST_LEASE_MS': 2000, 'CLOCK_FREQ': 520000000,
             'RESERVE_PINS_klipper_mmu_hub': 'PA1'}
DIGEST = 'ab' * 32
FPS = dict(CONSTANTS, KLIPPER_MMU_FPS_MIN_STEPS=68817, KLIPPER_MMU_FPS_MAX_STEPS=1307527)
# oams_runtime's own OpenAMS state module, for the runtime class defined below.
S = types.SimpleNamespace(Calibrate=lambda *fields: ('Calibrate',) + fields)


def calibration(value=12345, flags=2, event_type=8, operation=4, code=0, seq=1):
    return Event(INSTANCE, Identity(1, 9, 10, operation, 1), seq, 100, event_type,
                 code, value, flags)


class BowdenEstimateTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, 'bowden.json')

    def tearDown(self):
        self.directory.cleanup()

    def test_estimate_is_labeled_and_tagged_with_firmware_identity(self):
        store = BowdenEstimate(self.path)
        self.assertIsNone(store.status())
        self.assertTrue(store.record(calibration(), 'box', CONSTANTS, DIGEST))
        expected = {'kind': 'step_estimate', 'unit': 'feeder_microsteps',
                    'span': 'hub_to_fps_arrival', 'steps': 12345, 'lane': 1,
                    'mmu': 'box', 'diff_calibration_sha256': DIGEST,
                    'firmware': {key: value for key, value in CONSTANTS.items()
                                 if key.startswith('KLIPPER_MMU_')}}
        self.assertEqual(store.status(), expected)
        self.assertEqual(BowdenEstimate(self.path).status(), expected)
        with open(self.path) as source:
            self.assertEqual(json.load(source), {'estimate': expected})
        snapshot = store.status()
        snapshot['firmware']['KLIPPER_MMU_LANE_COUNT'] = 8
        self.assertEqual(store.status()['firmware']['KLIPPER_MMU_LANE_COUNT'], 4)

    def test_only_flagged_calibration_success_is_retained(self):
        store = BowdenEstimate(self.path)
        for event in (calibration(value=2000, flags=0),  # Encoder measurement.
                      calibration(flags=1),
                      calibration(event_type=9, code=6, flags=2),
                      calibration(event_type=10, code=7, flags=2),
                      calibration(event_type=1, operation=1, flags=2),
                      calibration(flags=6), object()):
            with self.subTest(event=event):
                self.assertFalse(store.record(event, 'box', CONSTANTS, None))
        self.assertFalse(os.path.exists(self.path))
        self.assertIsNone(store.status())

    def test_invalid_file_or_result_is_rejected(self):
        store = BowdenEstimate(self.path)
        store.record(calibration(), 'box', CONSTANTS, None)
        with open(self.path, 'rb') as source:
            valid = json.loads(source.read())['estimate']
        for changes in ({'unit': 'encoder_clicks'}, {'kind': 'measurement'},
                        {'steps': 0}, {'steps': True}, {'lane': 8}, {'mmu': ''},
                        {'firmware': {}}, {'firmware': {'CLOCK_FREQ': 1}},
                        {'diff_calibration_sha256': 'x' * 64}, {'extra': 1}):
            with self.subTest(changes=changes):
                with open(self.path, 'w') as output:
                    json.dump({'estimate': dict(valid, **changes)}, output)
                with self.assertRaises(ValueError):
                    BowdenEstimate(self.path)
        for contents in (b'{"estimate":{},"estimate":{}}', b' ' * 8193):
            with open(self.path, 'wb') as output:
                output.write(contents)
            with self.assertRaises(ValueError):
                BowdenEstimate(self.path)
        with self.assertRaisesRegex(ValueError, 'absolute'):
            BowdenEstimate('bowden.json')
        with self.assertRaisesRegex(ValueError, 'invalid estimate values'):
            BowdenEstimate(os.path.join(self.directory.name, 'b.json')).record(
                calibration(), 'box', {'CLOCK_FREQ': 1}, None)

    def test_replace_failure_preserves_file_and_memory(self):
        store = BowdenEstimate(self.path)
        store.record(calibration(value=100), 'box', CONSTANTS, None)
        with open(self.path, 'rb') as source:
            before = source.read()
        with patch('host.bowden_estimate.os.replace', side_effect=OSError('disk failure')):
            with self.assertRaisesRegex(OSError, 'disk failure'):
                store.record(calibration(value=200), 'box', CONSTANTS, None)
        self.assertEqual(store.status()['steps'], 100)
        with open(self.path, 'rb') as source:
            self.assertEqual(source.read(), before)
        self.assertEqual(os.listdir(self.directory.name), ['bowden.json'])


class FpsStepsTest(unittest.TestCase):
    def store(self, steps=None):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store = BowdenEstimate(os.path.join(directory.name, 'bowden.json'))
        if steps is not None:
            store.record(calibration(value=steps), 'box', FPS, DIGEST)
        return store

    def test_matching_estimate_within_outer_limits_is_restored(self):
        for steps in (1000000, 68817, 1307527):  # Including the outer limits.
            with self.subTest(steps=steps):
                self.assertEqual(self.store(steps).fps_steps('box', FPS, DIGEST), steps)

    def test_estimate_outside_outer_limits_is_none(self):
        # The firmware's own config command would otherwise shut the MCU
        # down for a stale estimate from before the printer's path changed.
        self.assertIsNone(self.store(68816).fps_steps('box', FPS, DIGEST))
        self.assertIsNone(self.store(1307528).fps_steps('box', FPS, DIGEST))

    def test_absent_or_mismatched_estimate_is_none(self):
        self.assertIsNone(self.store().fps_steps('box', FPS, DIGEST))
        store = self.store(1000000)
        for args in (('other', FPS, DIGEST),                        # Another MMU.
                     ('box', dict(FPS, KLIPPER_MMU_LANE_COUNT=3), DIGEST),  # Firmware.
                     ('box', dict(FPS, KLIPPER_MMU_FPS_MAX_STEPS=2000000), DIGEST),
                     ('box', FPS, 'cd' * 32), ('box', FPS, None),   # Calibration.
                     ('box', CONSTANTS, DIGEST)):                   # No FPS arrival.
            with self.subTest(args=args):
                self.assertIsNone(store.fps_steps(*args))

    def test_config_sends_steps_after_calibration(self):
        constants = dict(FPS, KLIPPER_MMU_PROTOCOL_VERSION=1, KLIPPER_MMU_ACTUATION=0,
                         KLIPPER_MMU_INTEGRATION_VERSION=1, KLIPPER_MMU_DIFF_CALIBRATION=1)
        store = self.store()
        store.record(calibration(value=1000000), 'box', constants, DIGEST)
        for estimate, expected in ((None, []), (self.store(), []), (store, [
                'config_km_fps_steps oid=1 steps=1000000'])):
            commands = []
            device = KlipperMMU.__new__(KlipperMMU)
            device.__dict__.update(
                _name='box', _diff_probe=False, _diagnostic_startup=False,
                _calibration_data=b'\x01', _calibration_sha256=DIGEST,
                _bowden_estimate=estimate,
                _mcu=types.SimpleNamespace(
                    is_fileoutput=lambda: False, get_constants=lambda: constants,
                    create_oid=iter(range(4)).__next__,
                    add_config_cmd=lambda command, is_init=False: commands.append(command)))
            with self.assertLogs(level='WARNING') if not expected else nullcontext():
                device._config_calibration_and_startup()
            self.assertEqual(commands, ['config_km_diff_calibration oid=0 data=01'] + expected)


class BowdenEstimateHostTest(unittest.TestCase):
    def make_observer(self, path):
        class Printer:
            config_error = ValueError
            command_error = ValueError

            def __init__(self):
                self.mcu = types.SimpleNamespace(register_config_callback=Mock(),
                                                 get_constants=lambda: dict(CONSTANTS))

            def lookup_objects(self, name):
                return []

            def lookup_object(self, name):
                return types.SimpleNamespace(register_mux_command=Mock())

            get_reactor = lambda self: types.SimpleNamespace(mutex=threading.Lock)
            def register_event_handler(self, *args):
                pass

        class Config:
            def __init__(self, printer):
                self.printer = printer

            def get_printer(self):
                return self.printer

            def get_name(self):
                return 'klipper_mmu box'

            def get(self, key, default=None):
                return {'bowden_estimate_file': path}.get(key, default)

            def getboolean(self, key, default=False):
                return default

            def getfloat(self, key, default=None, **limits):
                return default

        with patch.dict(sys.modules, {'mcu': types.SimpleNamespace(
                get_printer_mcu=lambda p, n: p.mcu)}):
            return KlipperMMU(Config(Printer()))

    def test_confirmed_ack_retains_estimate_and_counts_calibration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'bowden.json')
            observer = self.make_observer(path)
            self.assertIsNone(observer.get_status(None)['bowden_estimate'])
            observer.record_acked(calibration(value=2000, flags=0, seq=1))
            self.assertIsNone(observer.get_status(None)['bowden_estimate'])
            observer.record_acked(calibration(seq=2))
            status = observer.get_status(None)['bowden_estimate']
            self.assertEqual((status['steps'], status['unit'], status['mmu']),
                             (12345, 'feeder_microsteps', 'box'))
            self.assertEqual(status['firmware']['KLIPPER_MMU_DEVICE_ID'], 2)
            self.assertEqual(self.make_observer(path).get_status(None)['bowden_estimate'],
                             status)
            self.assertEqual(observer.operation_stats.snapshot(),
                             {('calibrate', 1, 'success'): 2})

    def test_write_failure_is_reported_without_reversing_accounting(self):
        with tempfile.TemporaryDirectory() as directory:
            observer = self.make_observer(os.path.join(directory, 'bowden.json'))
            with patch('host.bowden_estimate.os.replace', side_effect=OSError('full')), \
                    self.assertLogs(level='WARNING'):
                observer.record_acked(calibration())
            self.assertIsNone(observer.get_status(None)['bowden_estimate'])
            self.assertIn('bowden estimate not saved', observer._last_error)
            self.assertEqual(observer.operation_stats.snapshot(),
                             {('calibrate', 1, 'success'): 1})

    def test_calibrate_gcode_runs_through_runtime_and_reports_estimate(self):
        with tempfile.TemporaryDirectory() as directory:
            observer = self.make_observer(os.path.join(directory, 'bowden.json'))
            requests = []
            done = types.SimpleNamespace(wait=lambda: types.SimpleNamespace(ok=True))

            class Runtime:
                def request(self, fps, action):
                    requests.append((fps, action))
                    # Its event cycle stores the ACKed estimate for the waiter.
                    observer.record_acked(calibration(seq=len(requests)))
                    return done

            observer._openams_unit = types.SimpleNamespace(
                _runtime=Runtime(), bay_count=4, fps_name='fps', oams_idx=3)
            gcmd = types.SimpleNamespace(get_int=lambda name, default=None, minval=None, maxval=None: 1 if name == "BAY" else default,
                                         respond_info=Mock())
            observer.cmd_KLIPPER_MMU_CALIBRATE(gcmd)
            self.assertEqual(requests, [('fps', ('Calibrate', 'fps', 3, 1, 'ptfe'))])
            self.assertIn('12345 feeder microsteps', gcmd.respond_info.call_args.args[0])
            Runtime.request = lambda self, fps, action: done  # Nothing stored.
            with self.assertRaisesRegex(ValueError, 'stored no step estimate'):
                observer.cmd_KLIPPER_MMU_CALIBRATE(gcmd)

    def test_calibrate_reports_millimetres_when_steps_per_m_is_known(self):
        with tempfile.TemporaryDirectory() as directory:
            observer = self.make_observer(os.path.join(directory, 'bowden.json'))
            # BoxTurtle: 16 microsteps, 200 steps/rev, 4.65 mm rotation distance.
            observer._mcu.get_constants = lambda: dict(
                CONSTANTS, KLIPPER_MMU_FEEDER_STEPS_PER_M=688172)
            requests = []
            done = types.SimpleNamespace(wait=lambda: types.SimpleNamespace(ok=True))

            class Runtime:
                def request(self, fps, action):
                    requests.append((fps, action))
                    observer.record_acked(calibration(seq=len(requests)))
                    return done

            observer._openams_unit = types.SimpleNamespace(
                _runtime=Runtime(), bay_count=4, fps_name='fps', oams_idx=3)
            gcmd = types.SimpleNamespace(get_int=lambda name, default=None, minval=None, maxval=None: 1 if name == "BAY" else default,
                                         respond_info=Mock())
            observer.cmd_KLIPPER_MMU_CALIBRATE(gcmd)
            message = gcmd.respond_info.call_args.args[0]
            self.assertIn('12345 feeder microsteps', message)
            self.assertIn('17.9 mm', message)
            self.assertIn('openams_path_length_mm', message)

    def test_calibrate_save0_keeps_the_stored_estimate_and_suggests_rotation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'bowden.json')
            observer = self.make_observer(path)
            observer._mcu.get_constants = lambda: dict(
                CONSTANTS, KLIPPER_MMU_FEEDER_STEPS_PER_M=688172)
            observer._restore_calibrated_arrival = Mock()
            observer.record_acked(calibration(seq=1))
            observer._feeder_rotation_distances_mm = [22.0, 22.0, 22.0, 22.0]
            requests = []
            done = types.SimpleNamespace(wait=lambda: types.SimpleNamespace(ok=True))

            class Runtime:
                def request(self, fps, action):
                    requests.append((fps, action))
                    observer.record_acked(calibration(value=12839,
                                                      seq=len(requests) + 1))
                    return done

            observer._openams_unit = types.SimpleNamespace(
                _runtime=Runtime(), bay_count=4, fps_name='fps', oams_idx=3)
            gcmd = types.SimpleNamespace(
                get_int=lambda name, default=None, minval=None, maxval=None:
                    1 if name == "BAY" else (0 if name == "SAVE" else default),
                respond_info=Mock())
            observer.cmd_KLIPPER_MMU_CALIBRATE(gcmd)
            message = gcmd.respond_info.call_args.args[0]
            self.assertEqual(
                observer.get_status(None)['bowden_estimate']['steps'], 12345)
            self.assertEqual(
                self.make_observer(path).get_status(None)['bowden_estimate']['steps'],
                12345)
            observer._restore_calibrated_arrival.assert_called_once_with(12345)
            self.assertIn('not saved', message)
            self.assertIn('set it to %.4f mm' % (22.0 * 12839 / 12345), message)

    def test_calibrate_save0_without_a_stored_estimate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'bowden.json')
            observer = self.make_observer(path)
            observer._mcu.get_constants = lambda: dict(
                CONSTANTS, KLIPPER_MMU_FEEDER_STEPS_PER_M=688172)
            observer._restore_calibrated_arrival = Mock()
            requests = []
            done = types.SimpleNamespace(wait=lambda: types.SimpleNamespace(ok=True))

            class Runtime:
                def request(self, fps, action):
                    requests.append((fps, action))
                    observer.record_acked(calibration(seq=len(requests)))
                    return done

            observer._openams_unit = types.SimpleNamespace(
                _runtime=Runtime(), bay_count=4, fps_name='fps', oams_idx=3)
            gcmd = types.SimpleNamespace(
                get_int=lambda name, default=None, minval=None, maxval=None:
                    1 if name == "BAY" else (0 if name == "SAVE" else default),
                respond_info=Mock())
            observer.cmd_KLIPPER_MMU_CALIBRATE(gcmd)
            message = gcmd.respond_info.call_args.args[0]
            observer._restore_calibrated_arrival.assert_called_once_with(0)
            self.assertIsNone(observer.get_status(None)['bowden_estimate'])
            self.assertIn('none to compare against', message)

    def test_invalid_file_is_config_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'bowden.json')
            with open(path, 'w') as output:
                output.write('{"estimate":{"unit":"encoder_clicks"}}')
            with self.assertRaisesRegex(ValueError, 'invalid bowden_estimate_file'):
                self.make_observer(path)


if __name__ == '__main__':
    unittest.main()
