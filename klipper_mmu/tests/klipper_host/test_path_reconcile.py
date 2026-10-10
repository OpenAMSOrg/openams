# SPDX-License-Identifier: GPL-3.0-or-later
import threading
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from host import KlipperMMU
from host.path_reconciliation import (decode_path_reconcile_result,
                                      send_path_reconcile)

INSTANCE = 0x1020304050607080


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
    def completion(self):
        return Completion()

    def monotonic(self):
        return 1.

    def register_async_callback(self, callback):
        callback(1.)


class MCU:
    """Records sends; respond(mcu, args) emits the firmware reply."""
    def __init__(self, respond):
        self.respond, self.listeners, self.sent, self.formats = respond, [], [], []

    def lookup_command(self, fmt):
        self.formats.append(fmt)
        return self

    def send(self, args):
        self.sent.append(args)
        self.respond(self, args)

    def register_serial_response(self, callback, fmt):
        wrapper = SimpleNamespace(name=fmt.split()[0], callback=callback)
        wrapper.unregister = lambda: self.listeners.remove(wrapper)
        self.listeners.append(wrapper)
        return wrapper

    def emit(self, **changes):
        params = dict(instance_hi=INSTANCE >> 32, instance_lo=INSTANCE & 0xffffffff,
                      query=self.sent[-1][2], result=0, status=0)
        params.update(changes)
        for wrapper in list(self.listeners):
            if wrapper.name == 'km_path_reconcile_result':
                wrapper.callback(params)


def reply(**changes):
    return lambda mcu, args: mcu.emit(**changes)


class HelperTest(unittest.TestCase):
    def run_send(self, respond, lane, current=lambda: True):
        mcu = MCU(respond)
        try:
            return send_path_reconcile(mcu, Reactor(), INSTANCE, 9, lane, current), mcu
        finally:
            self.assertEqual(mcu.listeners, [])

    def test_clear_and_adopt_wire_arguments(self):
        result, mcu = self.run_send(reply(), None)
        self.assertEqual((result.result, result.status, result.query), (0, 0, 9))
        self.assertEqual(mcu.sent, [[INSTANCE >> 32, INSTANCE & 0xffffffff, 9, 0, 255]])
        self.assertEqual(mcu.formats, ['km_path_reconcile instance_hi=%u instance_lo=%u '
                                       'query=%u occupied=%c lane=%c'])
        result, mcu = self.run_send(reply(status=5), 3)
        self.assertEqual(result.status, 5)
        self.assertEqual(mcu.sent[0][3:], [1, 3])

    def test_rejections_and_cleanup(self):
        for respond, message in (
                (reply(instance_lo=1), 'instance changed'),
                (reply(result=2, status=1), 'Unsupported'),
                (reply(status=7), 'Unsupported'),
                (reply(result=8), 'Unsupported'),
                (reply(status='5'), 'Malformed'),
                (lambda mcu, args: None, 'timed out')):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                self.run_send(respond, 1)
        ignored = lambda mcu, args: (mcu.emit(query=8), mcu.emit(result=4))
        result, _ = self.run_send(ignored, None)
        self.assertEqual(result.result, 4)
        with self.assertRaisesRegex(ValueError, 'connection changed'):
            self.run_send(reply(), None, lambda: False)
        for lane in (-1, 8, 255, True):
            with self.assertRaisesRegex(ValueError, 'Invalid'):
                send_path_reconcile(MCU(reply()), Reactor(), INSTANCE, 9, lane, lambda: True)
        with self.assertRaisesRegex(ValueError, 'Invalid'):
            send_path_reconcile(MCU(reply()), Reactor(), INSTANCE, 0, None, lambda: True)

    def test_decode_bounds(self):
        base = dict(instance_hi=1, instance_lo=2, query=3, result=0, status=6)
        self.assertEqual(decode_path_reconcile_result(base).instance, (1 << 32) | 2)
        for change in ({'status': 9}, {'instance_hi': 1 << 32}, {'result': -1}):
            with self.assertRaises(ValueError):
                decode_path_reconcile_result(dict(base, **change))


class GCode:
    def __init__(self, **params):
        self.params = params
        self.respond_info = Mock()

    def get(self, name, default=None):
        return self.params.get(name, default)

    def get_int(self, name, minval=None, maxval=None):
        value = int(self.params[name])
        if value < minval or value > maxval:
            raise ValueError('%s out of range' % name)
        return value


def observer(respond, state=0):
    mmu = object.__new__(KlipperMMU)
    mmu._name = 'unit'
    mmu._printer = SimpleNamespace(command_error=RuntimeError,
                                   get_reactor=lambda: Reactor())
    mmu._mcu_lock = threading.Lock()
    mmu._active = mmu._connected = True
    mmu._state = 'attached'
    mmu._instance = INSTANCE
    mmu._generation = 3
    mmu._challenge = 40
    mmu._mcu = MCU(respond)
    def capture():
        mmu._challenge += 1
        return ((mmu._generation, mmu._instance), 0,
                SimpleNamespace(query=mmu._challenge, values={'state': state}), object())
    mmu.capture_physical_state = Mock(side_effect=capture)
    return mmu


def openams_unit(clear, post_values):
    """A bound OpenAMS unit whose refresh captures post_values."""
    return SimpleNamespace(
        _runtime=SimpleNamespace(accept_firmware_fault_cleared=clear),
        refresh=Mock(return_value=True), _bound=(3, INSTANCE),
        _captured_status=SimpleNamespace(values=post_values))


class GCodeTest(unittest.TestCase):
    def test_clear_and_adopt_report_only_confirmed_outcome(self):
        mmu = observer(reply())
        gcmd = GCode(ACTION='clear')
        mmu.cmd_KLIPPER_MMU_RECONCILE_PATH(gcmd)
        self.assertEqual(mmu._mcu.sent[-1][2:], [41, 0, 255])
        self.assertIn('path cleared; no motion', gcmd.respond_info.call_args.args[0])
        gcmd = GCode(ACTION='ADOPT', LANE='2')
        mmu.cmd_KLIPPER_MMU_RECONCILE_PATH(gcmd)
        self.assertEqual(mmu._mcu.sent[-1][2:], [42, 1, 2])
        self.assertIn('adopted by lane 2', gcmd.respond_info.call_args.args[0])
        self.assertNotIn('fault cleared', gcmd.respond_info.call_args.args[0])
        self.assertFalse(mmu._mcu_lock.locked())
        self.assertEqual(mmu._mcu.listeners, [])

    def test_reconciling_a_stopped_fault_reports_it(self):
        mmu = observer(reply(), state=6)
        gcmd = GCode(ACTION='CLEAR')
        mmu.cmd_KLIPPER_MMU_RECONCILE_PATH(gcmd)
        self.assertIn('fault cleared; path cleared; no motion',
                      gcmd.respond_info.call_args.args[0])
        # No OpenAMS unit accepted this fault, so no OpenAMS-specific note.
        self.assertNotIn('OpenAMS', gcmd.respond_info.call_args.args[0])

    def test_reconciling_clears_a_latched_openams_fault(self):
        mmu = observer(reply(), state=6)
        clear = Mock(return_value=True)
        mmu._accepted_firmware_fault = (0, ('id',))
        mmu._openams_unit = openams_unit(clear, {'state': 2, 'system_fault': 0})
        gcmd = GCode(ACTION='ADOPT', LANE='0')
        mmu.cmd_KLIPPER_MMU_RECONCILE_PATH(gcmd)
        clear.assert_called_once_with(0, ('id',))
        self.assertIn('fault cleared; path adopted by lane 0; no motion performed'
                      '; OpenAMS lane cleared', gcmd.respond_info.call_args.args[0])
        self.assertIsNone(mmu._accepted_firmware_fault)

    def test_reconciling_reports_restart_when_hook_is_missing(self):
        mmu = observer(reply(), state=6)
        mmu._accepted_firmware_fault = (0, ('id',))
        mmu._openams_unit = SimpleNamespace(_runtime=SimpleNamespace())  # no hook
        gcmd = GCode(ACTION='ADOPT', LANE='0')
        mmu.cmd_KLIPPER_MMU_RECONCILE_PATH(gcmd)
        self.assertIn('; OpenAMS lane still latched, restart Klipper before using it',
                      gcmd.respond_info.call_args.args[0])
        self.assertEqual(mmu._accepted_firmware_fault, (0, ('id',)))  # kept for a retry

    def test_reconciling_reports_restart_when_openams_refuses_the_clear(self):
        mmu = observer(reply(), state=6)
        clear = Mock(return_value=False)  # e.g. identity no longer matches
        mmu._accepted_firmware_fault = (0, ('id',))
        mmu._openams_unit = openams_unit(clear, {'state': 2, 'system_fault': 0})
        gcmd = GCode(ACTION='ADOPT', LANE='0')
        mmu.cmd_KLIPPER_MMU_RECONCILE_PATH(gcmd)
        self.assertIn('restart Klipper before using it',
                      gcmd.respond_info.call_args.args[0])
        self.assertEqual(mmu._accepted_firmware_fault, (0, ('id',)))

    def test_reconciling_reports_restart_when_post_capture_still_shows_fault(self):
        # The RECONCILE_PATH command itself succeeded, but the fresh
        # post-capture required before trusting OpenAMS still shows FAULT
        # (or a changed identity/generation): never call the hook.
        mmu = observer(reply(), state=6)
        clear = Mock(return_value=True)
        mmu._accepted_firmware_fault = (0, ('id',))
        mmu._openams_unit = openams_unit(clear, {'state': 6, 'system_fault': 9})
        gcmd = GCode(ACTION='ADOPT', LANE='0')
        mmu.cmd_KLIPPER_MMU_RECONCILE_PATH(gcmd)
        clear.assert_not_called()
        self.assertIn('restart Klipper before using it',
                      gcmd.respond_info.call_args.args[0])

    def test_reconciling_an_idle_path_never_touches_openams(self):
        mmu = observer(reply(), state=0)
        clear = Mock(return_value=True)
        mmu._accepted_firmware_fault = (0, ('id',))
        mmu._openams_unit = SimpleNamespace(
            _runtime=SimpleNamespace(accept_firmware_fault_cleared=clear))
        gcmd = GCode(ACTION='CLEAR')
        mmu.cmd_KLIPPER_MMU_RECONCILE_PATH(gcmd)
        clear.assert_not_called()
        self.assertNotIn('OpenAMS', gcmd.respond_info.call_args.args[0])
        self.assertEqual(mmu._accepted_firmware_fault, (0, ('id',)))

    def test_refusals_are_errors_without_success_message(self):
        for respond, message in ((reply(status=5), 'sensor_conflict'),
                                 (reply(status=6), 'nothing_to_reconcile'),
                                 (reply(result=2), 'result=2'),
                                 (lambda mcu, args: None, 'timed out')):
            mmu = observer(respond)
            gcmd = GCode(ACTION='ADOPT', LANE='1')
            with self.subTest(message=message), self.assertRaisesRegex(RuntimeError, message):
                mmu.cmd_KLIPPER_MMU_RECONCILE_PATH(gcmd)
            gcmd.respond_info.assert_not_called()
            self.assertFalse(mmu._mcu_lock.locked())

    def test_invalid_arguments_or_connection_send_nothing(self):
        for params in ({}, {'ACTION': 'CLEAR', 'LANE': '1'}, {'ACTION': 'ADOPT'},
                       {'ACTION': 'ADOPT', 'LANE': '8'}, {'ACTION': 'RESET'}):
            mmu = observer(reply())
            with self.subTest(params=params), self.assertRaises(RuntimeError):
                mmu.cmd_KLIPPER_MMU_RECONCILE_PATH(GCode(**params))
            self.assertEqual(mmu._mcu.sent, [])
        for field, value in (('_connected', False),
                             ('_state', 'shutdown'), ('_event_cycle_in_progress', True)):
            mmu = observer(reply())
            setattr(mmu, field, value)
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, 'unavailable'):
                mmu.cmd_KLIPPER_MMU_RECONCILE_PATH(GCode(ACTION='CLEAR'))
            self.assertEqual(mmu._mcu.sent, [])

    def test_missing_or_changed_capture_sends_no_mutation(self):
        for capture in (None, ((4, INSTANCE), 0, SimpleNamespace(query=41), object())):
            mmu = observer(reply())
            mmu.capture_physical_state = Mock(return_value=capture)
            with self.assertRaisesRegex(RuntimeError, 'fresh capture'):
                mmu.cmd_KLIPPER_MMU_RECONCILE_PATH(GCode(ACTION='CLEAR'))
            self.assertEqual(mmu._mcu.sent, [])
            self.assertFalse(mmu._mcu_lock.locked())

    def test_connection_change_during_exchange(self):
        def reconnect(mcu, args):
            mmu._generation += 1
            mcu.emit()
        mmu = observer(reconnect)
        gcmd = GCode(ACTION='CLEAR')
        with self.assertRaisesRegex(RuntimeError, 'connection changed|Stale'):
            mmu.cmd_KLIPPER_MMU_RECONCILE_PATH(gcmd)
        gcmd.respond_info.assert_not_called()


if __name__ == '__main__':
    unittest.main()
