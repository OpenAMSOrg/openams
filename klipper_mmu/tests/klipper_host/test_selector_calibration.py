# SPDX-License-Identifier: GPL-3.0-or-later
"""Host checks for selector gate/angle calibration: the file, extrapolation,
the two runtime wire exchanges, and the KLIPPER_MMU_SERVO/CALIBRATE_SELECTOR
commands built on them."""
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from host import KlipperMMU
from host.openams_managed_unit import OpenAMSManagedUnit
from host.protocol import Admission, Event, Identity
from host.requests import Requests
from host.selector_calibration import (SelectorCalibration, extrapolate_linear,
                                       send_selector_gate_set, send_selector_motors_off,
                                       send_selector_servo_test, spaced_angles)
from host.wire import AdmissionResult, CommandCode, EventType, Fault, OpCode, OpType

INSTANCE = 0x1020304050607080
CONSTANTS = {'KLIPPER_MMU_DEVICE_ID': 2, 'KLIPPER_MMU_LANE_COUNT': 4,
            'KLIPPER_MMU_CONFIG_ENCODER': 0}
GATE_SET_FORMAT = 'km_selector_gate_set instance_hi=%u instance_lo=%u gate=%c value=%i'
SERVO_TEST_FORMAT = 'km_selector_servo_test instance_hi=%u instance_lo=%u degrees=%i'
MOTORS_OFF_FORMAT = 'km_selector_motors_off instance_hi=%u instance_lo=%u'


class Completion:
    def __init__(self):
        self.value = None

    def test(self):
        return self.value is not None

    def complete(self, value):
        self.value = value

    def wait(self, deadline):
        return self.value


class Reactor:
    def __init__(self):
        self.now = 1.

    def completion(self):
        return Completion()

    def monotonic(self):
        return self.now

    def pause(self, waketime):
        self.now = waketime

    def register_async_callback(self, callback):
        callback(self.now)


class MCU:
    """Records sends; each send pops the next scripted firmware reply."""
    def __init__(self, replies, constants=None):
        self.replies, self.listeners, self.sent, self.formats = list(replies), [], [], []
        self.cmds = []
        self.oids = 0
        self.constants = constants if constants is not None else CONSTANTS

    def lookup_command(self, fmt):
        self.formats.append(fmt)
        return self

    def send(self, args):
        self.sent.append(args)
        if self.replies:
            reply = self.replies.pop(0)
            if reply is not None:
                self.emit(**reply)

    def register_serial_response(self, callback, fmt):
        wrapper = SimpleNamespace(name=fmt.split()[0], callback=callback)
        wrapper.unregister = lambda: self.listeners.remove(wrapper)
        self.listeners.append(wrapper)
        return wrapper

    def emit(self, name=None, **changes):
        params = dict(instance_hi=INSTANCE >> 32, instance_lo=INSTANCE & 0xffffffff,
                      result=0, status=0)
        params.update(changes)
        for wrapper in list(self.listeners):
            if name is None or wrapper.name == name:
                wrapper.callback(params)

    def get_constants(self):
        return self.constants

    def is_fileoutput(self):
        return False

    def try_lookup_command(self, cmd):
        return object()

    def create_oid(self):
        self.oids += 1
        return self.oids

    def add_config_cmd(self, cmd):
        self.cmds.append(cmd)


class FilePrinter:
    def config_error(self, msg):
        return ValueError(msg)


class SelectorCalibrationFileTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, 'selector.json')

    def tearDown(self):
        self.directory.cleanup()

    def test_labeled_tagged_and_round_trips(self):
        store = SelectorCalibration(self.path)
        self.assertIsNone(store.status())
        self.assertEqual(store.gates_for('box', CONSTANTS), {})
        store.save('box', CONSTANTS, {0: 320, 5: -1})
        expected = {'kind': 'selector_gate', 'mmu': 'box',
                   'firmware': {k: v for k, v in CONSTANTS.items()
                                if k.startswith('KLIPPER_MMU_')},
                   'gates': {'0': 320, '5': -1}}
        self.assertEqual(store.status(), expected)
        self.assertEqual(SelectorCalibration(self.path).gates_for('box', CONSTANTS),
                         {0: 320, 5: -1})
        with open(self.path) as source:
            self.assertEqual(json.load(source), {'calibration': expected})

    def test_save_merges_per_gate_and_replaces_on_firmware_change(self):
        store = SelectorCalibration(self.path)
        store.save('box', CONSTANTS, {0: 320})
        store.save('box', CONSTANTS, {7: 13200})
        self.assertEqual(store.gates_for('box', CONSTANTS), {0: 320, 7: 13200})
        other = dict(CONSTANTS, KLIPPER_MMU_LANE_COUNT=8)
        self.assertEqual(store.gates_for('box', other), {})
        store.save('box', other, {0: 4})
        self.assertEqual(store.gates_for('box', other), {0: 4})
        self.assertEqual(store.gates_for('box', CONSTANTS), {})

    def test_gates_for_requires_matching_mmu_name(self):
        store = SelectorCalibration(self.path)
        store.save('box', CONSTANTS, {0: 320})
        self.assertEqual(store.gates_for('other', CONSTANTS), {})

    def test_invalid_file_or_values_rejected(self):
        store = SelectorCalibration(self.path)
        store.save('box', CONSTANTS, {0: 320})
        with open(self.path, 'rb') as source:
            valid = json.loads(source.read())['calibration']
        for changes in ({'kind': 'other'}, {'mmu': ''}, {'firmware': {}},
                        {'firmware': {'CLOCK_FREQ': 1}}, {'gates': {'x': 1}},
                        {'gates': {'0': 'x'}}, {'gates': {'0': 0x10000}},
                        {'gates': {'0': -2}}, {'extra': 1}):
            with self.subTest(changes=changes):
                with open(self.path, 'w') as output:
                    json.dump({'calibration': dict(valid, **changes)}, output)
                with self.assertRaises(ValueError):
                    SelectorCalibration(self.path)
        with self.assertRaisesRegex(ValueError, 'absolute'):
            SelectorCalibration('selector.json')


class ExtrapolationTest(unittest.TestCase):
    def test_extrapolate_matches_docs_example(self):
        # docs/selectors.md's own selector_gate_steps example, both ends measured.
        self.assertEqual(
            extrapolate_linear({0: 320}, 8, 7, 13200),
            {0: 320, 1: 2160, 2: 4000, 3: 5840, 4: 7680, 5: 9520, 6: 11360, 7: 13200})

    def test_single_gate_without_the_other_end(self):
        self.assertEqual(extrapolate_linear({}, 8, 3, 4000), {3: 4000})
        self.assertEqual(extrapolate_linear({0: 320}, 8, 3, 4000), {3: 4000})

    def test_middle_gate_never_extrapolates(self):
        self.assertEqual(extrapolate_linear({0: 320, 7: 13200}, 8, 3, 9999), {3: 9999})

    def test_cleared_other_end_does_not_extrapolate(self):
        self.assertEqual(extrapolate_linear({0: -1}, 8, 7, 13200), {7: 13200})

    def test_spaced_angles_anchors_at_the_given_gate(self):
        self.assertEqual(spaced_angles(1, 0, 30, 4), {0: -30, 1: 0, 2: 30, 3: 60})
        self.assertEqual(spaced_angles(0, 60, 25, 4), {0: 60, 1: 85, 2: 110, 3: 135})


class WireExchangeTest(unittest.TestCase):
    def test_gate_set_sends_gate_and_value(self):
        mcu = MCU([dict(status=0)])
        reply = send_selector_gate_set(mcu, Reactor(), INSTANCE, lambda: True, 3, -1)
        self.assertEqual((reply.result, reply.status), (CommandCode.OK, 0))
        self.assertEqual(mcu.sent, [[INSTANCE >> 32, INSTANCE & 0xffffffff, 3, -1]])
        self.assertEqual(mcu.formats, [GATE_SET_FORMAT])
        self.assertEqual(mcu.listeners, [])

    def test_servo_test_sends_degrees(self):
        mcu = MCU([dict(status=0)])
        reply = send_selector_servo_test(mcu, Reactor(), INSTANCE, lambda: True, 45)
        self.assertEqual((reply.result, reply.status), (CommandCode.OK, 0))
        self.assertEqual(mcu.sent, [[INSTANCE >> 32, INSTANCE & 0xffffffff, 45]])
        self.assertEqual(mcu.formats, [SERVO_TEST_FORMAT])

    def test_motors_off_sends_no_extra_arguments(self):
        mcu = MCU([dict(status=0)])
        reply = send_selector_motors_off(mcu, Reactor(), INSTANCE, lambda: True)
        self.assertEqual((reply.result, reply.status), (CommandCode.OK, 0))
        self.assertEqual(mcu.sent, [[INSTANCE >> 32, INSTANCE & 0xffffffff]])
        self.assertEqual(mcu.formats, [MOTORS_OFF_FORMAT])

    def test_refusal_statuses_and_rejections(self):
        for status in (1, 2, 3):
            reply = send_selector_gate_set(MCU([dict(status=status)]), Reactor(),
                                           INSTANCE, lambda: True, 0, 5)
            self.assertEqual((reply.result, reply.status), (CommandCode.OK, status))
        for replies, message in (([dict(instance_lo=1)], 'instance changed'),
                                 ([dict(result=2, status=3)], 'Unsupported'),
                                 ([None], 'timed out')):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                send_selector_gate_set(MCU(replies), Reactor(), INSTANCE, lambda: True, 0, 5)
        with self.assertRaisesRegex(ValueError, 'Invalid'):
            send_selector_gate_set(MCU([{}]), Reactor(), INSTANCE, lambda: True, 300, 5)
        with self.assertRaisesRegex(ValueError, 'Invalid'):
            send_selector_servo_test(MCU([{}]), Reactor(), INSTANCE, lambda: True, 'x')


class GCode:
    def __init__(self, **params):
        self.params = params
        self.respond_info = Mock()

    _sentinel = object()

    def get(self, name, default=_sentinel):
        if name not in self.params:
            if default is self._sentinel:
                raise KeyError(name)
            return default
        return self.params[name]

    def get_int(self, name, default=_sentinel, minval=None, maxval=None):
        value = self.get(name, default)
        if value is default and default is not self._sentinel:
            return value
        value = int(value)
        if (minval is not None and value < minval) or (maxval is not None and value > maxval):
            raise ValueError('%s out of range' % name)
        return value


def observer(replies, servo_kind=True, selector_capable=False, calibration=None,
             constants=None):
    mmu = object.__new__(KlipperMMU)
    reactor = Reactor()
    mmu._name = 'box'
    mmu._printer = SimpleNamespace(command_error=RuntimeError, get_reactor=lambda: reactor)
    mmu._mcu_lock = threading.Lock()
    mmu._active = mmu._connected = True
    mmu._state = 'attached'
    mmu._instance = INSTANCE
    mmu._generation = 3
    mmu._mcu = MCU(replies, constants)
    mmu._selector_servo_kind = servo_kind
    mmu._selector_capable = selector_capable
    mmu._selector_calibration = calibration
    mmu.physical_connection_identity = Mock(return_value=(3, INSTANCE))
    return mmu, reactor


class ServoCommandTest(unittest.TestCase):
    def test_moves_to_the_requested_angle(self):
        mmu, _ = observer([dict(status=0)])
        gcmd = GCode(ANGLE='83')
        mmu.cmd_KLIPPER_MMU_SERVO(gcmd)
        self.assertEqual(mmu._mcu.sent, [[INSTANCE >> 32, INSTANCE & 0xffffffff, 83]])
        self.assertIn('83 deg', gcmd.respond_info.call_args.args[0])
        self.assertIn('not saved', gcmd.respond_info.call_args.args[0])

    def test_refused_on_a_non_servo_build(self):
        mmu, _ = observer([{}], servo_kind=False)
        with self.assertRaisesRegex(RuntimeError, 'SERVO-kind'):
            mmu.cmd_KLIPPER_MMU_SERVO(GCode(ANGLE='0'))

    def test_firmware_refusal_is_reported(self):
        mmu, _ = observer([dict(status=1)])
        with self.assertRaisesRegex(RuntimeError, 'invalid'):
            mmu.cmd_KLIPPER_MMU_SERVO(GCode(ANGLE='999'))


class MotorsOffCommandTest(unittest.TestCase):
    """Happy Hare's MMU_MOTORS_OFF equivalent: available on either kind,
    refused while an operation runs, with a clear message."""

    def test_releases_the_selector(self):
        mmu, _ = observer([dict(status=0)])
        gcmd = GCode()
        mmu.cmd_KLIPPER_MMU_MOTORS_OFF(gcmd)
        self.assertEqual(mmu._mcu.sent, [[INSTANCE >> 32, INSTANCE & 0xffffffff]])
        message = gcmd.respond_info.call_args.args[0]
        self.assertIn('motors off', message)
        self.assertIn('home again', message)

    def test_available_on_the_stepper_kind_too(self):
        mmu, _ = observer([dict(status=0)], servo_kind=False, selector_capable=True)
        mmu.cmd_KLIPPER_MMU_MOTORS_OFF(GCode())
        self.assertEqual(mmu._mcu.sent, [[INSTANCE >> 32, INSTANCE & 0xffffffff]])

    def test_refused_without_a_selector_firmware_build(self):
        mmu, _ = observer([{}], servo_kind=False, selector_capable=False)
        with self.assertRaisesRegex(RuntimeError, 'selector firmware'):
            mmu.cmd_KLIPPER_MMU_MOTORS_OFF(GCode())

    def test_refused_while_an_operation_runs(self):
        mmu, _ = observer([dict(status=2)])  # KM_ERR_BUSY.
        with self.assertRaisesRegex(RuntimeError, 'busy'):
            mmu.cmd_KLIPPER_MMU_MOTORS_OFF(GCode())


class CalibrateSelectorServoTest(unittest.TestCase):
    def test_single_gate_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'selector.json')
            calibration = SelectorCalibration(path)
            mmu, _ = observer([dict(status=0)], calibration=calibration)
            gcmd = GCode(GATE='3', ANGLE='83')
            mmu.cmd_KLIPPER_MMU_CALIBRATE_SELECTOR(gcmd)
            self.assertEqual(mmu._mcu.sent, [[INSTANCE >> 32, INSTANCE & 0xffffffff, 3, 83]])
            self.assertEqual(calibration.gates_for('box', CONSTANTS), {3: 83})
            self.assertIn('saved', gcmd.respond_info.call_args.args[0])

    def test_spacing_fills_every_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            calibration = SelectorCalibration(os.path.join(directory, 'selector.json'))
            mmu, _ = observer([dict(status=0)] * 4, calibration=calibration)
            mmu.cmd_KLIPPER_MMU_CALIBRATE_SELECTOR(GCode(GATE='0', ANGLE='60', SPACING='25'))
            self.assertEqual(calibration.gates_for('box', CONSTANTS),
                             {0: 60, 1: 85, 2: 110, 3: 135})

    def test_not_saved_without_save_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            calibration = SelectorCalibration(os.path.join(directory, 'selector.json'))
            mmu, _ = observer([dict(status=0)], calibration=calibration)
            gcmd = GCode(GATE='3', ANGLE='83', SAVE='0')
            mmu.cmd_KLIPPER_MMU_CALIBRATE_SELECTOR(gcmd)
            self.assertEqual(calibration.gates_for('box', CONSTANTS), {})
            self.assertIn('not saved', gcmd.respond_info.call_args.args[0])

    def test_save_without_a_configured_file_is_an_error(self):
        mmu, _ = observer([dict(status=0)], calibration=None)
        with self.assertRaisesRegex(RuntimeError, 'selector_calibration_file'):
            mmu.cmd_KLIPPER_MMU_CALIBRATE_SELECTOR(GCode(GATE='3', ANGLE='83'))

    def test_a_refused_gate_saves_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            calibration = SelectorCalibration(os.path.join(directory, 'selector.json'))
            calibration.save('box', CONSTANTS, {5: 40})
            mmu, _ = observer([dict(status=1)], calibration=calibration)
            with self.assertRaisesRegex(RuntimeError, 'invalid'):
                mmu.cmd_KLIPPER_MMU_CALIBRATE_SELECTOR(GCode(GATE='3', ANGLE='999'))
            self.assertEqual(calibration.gates_for('box', CONSTANTS), {5: 40})

    def test_angle_refused_on_a_stepper_build(self):
        mmu, _ = observer([{}], servo_kind=False)
        with self.assertRaisesRegex(RuntimeError, 'SERVO-kind'):
            mmu.cmd_KLIPPER_MMU_CALIBRATE_SELECTOR(GCode(GATE='0', ANGLE='0'))


class CalibrateSelectorStepperTest(unittest.TestCase):
    """One call, like Happy Hare's own MMU_CALIBRATE_SELECTOR: the command
    starts HOME and waits for its terminal itself (a reactor.pause loop),
    instead of making the caller poll a second time."""

    def unit(self, requests):
        unit = Mock(spec=OpenAMSManagedUnit)
        unit._event_binding = None
        unit._retired_requests = requests
        return unit

    def start_home(self, requests):
        def start():
            identity = requests.issue_home()
            return identity, Admission(INSTANCE, identity, AdmissionResult.ACCEPTED, Fault.NONE)
        return Mock(side_effect=start)

    def settle_on_first_pause(self, reactor, requests, event_type, code, value):
        """Deliver HOME's terminal the first time the command pauses to wait
        for it, the way an event callback would land between reactor turns."""
        real_pause = reactor.pause
        calls = []

        def pause(waketime):
            if not calls:
                terminal = Event(INSTANCE, requests.pending_home, 1, 100, event_type,
                                 code, value, 0)
                requests.accept_home(terminal, requests.generation)
            calls.append(waketime)
            real_pause(waketime)
        reactor.pause = pause

    def test_measures_homes_and_saves_in_one_call(self):
        with tempfile.TemporaryDirectory() as directory:
            constants = dict(CONSTANTS, KLIPPER_MMU_LANE_COUNT=8)
            calibration = SelectorCalibration(os.path.join(directory, 'selector.json'))
            calibration.save('box', constants, {0: 320})
            mmu, reactor = observer([dict(status=0)] * 8, servo_kind=False, selector_capable=True,
                                    calibration=calibration, constants=constants)
            requests = Requests(INSTANCE, 13)
            mmu._openams_unit = self.unit(requests)
            mmu._openams_unit.start_home = self.start_home(requests)
            self.settle_on_first_pause(reactor, requests, EventType.HOME_COMPLETE,
                                       OpCode.SUCCESS, 13200)
            gcmd = GCode(GATE='7')
            mmu.cmd_KLIPPER_MMU_CALIBRATE_SELECTOR(gcmd)
            message = gcmd.respond_info.call_args.args[0]
            self.assertIn('measured 13200 step', message)
            self.assertIn('saved', message)
            self.assertIsNone(requests.pending_home)
            # Happy Hare's own rule: both ends known now, every gate fills in.
            self.assertEqual(calibration.gates_for('box', constants),
                             {0: 320, 1: 2160, 2: 4000, 3: 5840, 4: 7680, 5: 9520,
                              6: 11360, 7: 13200})

    def test_a_failed_home_reports_and_saves_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            calibration = SelectorCalibration(os.path.join(directory, 'selector.json'))
            mmu, reactor = observer([{}], servo_kind=False, selector_capable=True,
                                    calibration=calibration)
            requests = Requests(INSTANCE, 13)
            mmu._openams_unit = self.unit(requests)
            mmu._openams_unit.start_home = self.start_home(requests)
            self.settle_on_first_pause(reactor, requests, EventType.HOME_FAILED,
                                       OpCode.ERROR_UNSPECIFIED, 0)
            with self.assertRaisesRegex(RuntimeError, 'did not complete'):
                mmu.cmd_KLIPPER_MMU_CALIBRATE_SELECTOR(GCode(GATE='0'))
            self.assertEqual(calibration.gates_for('box', CONSTANTS), {})

    def test_timeout_reports_and_saves_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            calibration = SelectorCalibration(os.path.join(directory, 'selector.json'))
            mmu, _ = observer([{}], servo_kind=False, selector_capable=True,
                              calibration=calibration)
            requests = Requests(INSTANCE, 13)
            mmu._openams_unit = self.unit(requests)
            mmu._openams_unit.start_home = self.start_home(requests)
            # No terminal ever arrives; the poll loop must give up, not hang.
            with self.assertRaisesRegex(RuntimeError, 'timed out'):
                mmu.cmd_KLIPPER_MMU_CALIBRATE_SELECTOR(GCode(GATE='0'))
            self.assertEqual(calibration.gates_for('box', CONSTANTS), {})

    def test_pending_home_is_refused_not_restarted(self):
        mmu, _ = observer([{}], servo_kind=False, selector_capable=True)
        requests = Requests(INSTANCE, 13)
        mmu._openams_unit = self.unit(requests)
        requests.issue_home()
        mmu._openams_unit.start_home = Mock(side_effect=AssertionError('must not restart'))
        with self.assertRaisesRegex(RuntimeError, 'already pending'):
            mmu.cmd_KLIPPER_MMU_CALIBRATE_SELECTOR(GCode(GATE='0'))

    def test_refused_admission_reports_and_saves_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            calibration = SelectorCalibration(os.path.join(directory, 'selector.json'))
            mmu, _ = observer([{}], servo_kind=False, selector_capable=True,
                              calibration=calibration)
            requests = Requests(INSTANCE, 13)
            mmu._openams_unit = self.unit(requests)

            def start():
                identity = requests.issue_home()
                requests.pending_home = None
                return identity, Admission(INSTANCE, identity, AdmissionResult.PATH_OCCUPIED,
                                           Fault.NONE)
            mmu._openams_unit.start_home = Mock(side_effect=start)
            with self.assertRaisesRegex(RuntimeError, 'admission'):
                mmu.cmd_KLIPPER_MMU_CALIBRATE_SELECTOR(GCode(GATE='0'))
            self.assertEqual(calibration.gates_for('box', CONSTANTS), {})

    def test_requires_openams_motion(self):
        mmu, _ = observer([{}], servo_kind=False, selector_capable=True)
        mmu._openams_unit = None
        with self.assertRaisesRegex(RuntimeError, 'openams_motion'):
            mmu.cmd_KLIPPER_MMU_CALIBRATE_SELECTOR(GCode(GATE='0'))

    def test_requires_selector_capable_firmware(self):
        mmu, _ = observer([{}], servo_kind=False, selector_capable=False)
        mmu._openams_unit = self.unit(Requests(INSTANCE, 13))
        with self.assertRaisesRegex(RuntimeError, 'selector-stepper'):
            mmu.cmd_KLIPPER_MMU_CALIBRATE_SELECTOR(GCode(GATE='0'))


if __name__ == '__main__':
    unittest.main()
