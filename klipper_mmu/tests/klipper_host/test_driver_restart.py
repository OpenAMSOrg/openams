# SPDX-License-Identifier: GPL-3.0-or-later
"""Host checks for the explicit stopped-lane TMC driver restart."""
import threading
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from host import KlipperMMU
from host.path_reconciliation import (DRIVER_RESTART_SELECTOR, RESTART_WAIT,
                                      decode_driver_restart_result,
                                      send_driver_restart)

INSTANCE = 0x1020304050607080
FORMAT = 'km_driver_restart instance_hi=%u instance_lo=%u query=%u lane=%c'


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
        self.now, self.pauses = 1., []

    def completion(self):
        return Completion()

    def monotonic(self):
        return self.now

    def pause(self, waketime):
        self.pauses.append(waketime - self.now)
        self.now = waketime

    def register_async_callback(self, callback):
        callback(self.now)


class MCU:
    """Records sends; each send pops the next scripted firmware reply."""
    def __init__(self, replies):
        self.replies, self.listeners, self.sent, self.formats = list(replies), [], [], []

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

    def emit(self, **changes):
        params = dict(instance_hi=INSTANCE >> 32, instance_lo=INSTANCE & 0xffffffff,
                      query=self.sent[-1][2], result=0, status=0)
        params.update(changes)
        for wrapper in list(self.listeners):
            if wrapper.name == 'km_driver_restart_result':
                wrapper.callback(params)


class HelperTest(unittest.TestCase):
    def run_send(self, replies, lane=2, query=9, current=lambda: True):
        mcu = MCU(replies)
        try:
            return send_driver_restart(mcu, Reactor(), INSTANCE, query, lane, current), mcu
        finally:
            self.assertEqual(mcu.listeners, [])

    def test_wire_arguments_and_statuses(self):
        for status in (0, 2, 3, 4, 8):
            result, mcu = self.run_send([dict(status=status)])
            self.assertEqual((result.result, result.status, result.query), (0, status, 9))
            self.assertEqual(mcu.sent, [[INSTANCE >> 32, INSTANCE & 0xffffffff, 9, 2]])
            self.assertEqual(mcu.formats, [FORMAT])

    def test_selector_sentinel_bypasses_lane_bounds(self):
        result, mcu = self.run_send([dict(status=8)], lane=DRIVER_RESTART_SELECTOR)
        self.assertEqual((result.result, result.status), (0, 8))
        self.assertEqual(mcu.sent, [[INSTANCE >> 32, INSTANCE & 0xffffffff, 9,
                                     DRIVER_RESTART_SELECTOR]])

    def test_rejections_and_cleanup(self):
        for replies, message in (([dict(instance_lo=1)], 'instance changed'),
                                 ([dict(result=2, status=3)], 'Unsupported'),
                                 ([dict(status=5)], 'Unsupported'),
                                 ([dict(status='3')], 'Malformed'),
                                 ([None], 'timed out')):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                self.run_send(replies)
        with self.assertRaisesRegex(ValueError, 'connection changed'):
            self.run_send([{}], current=lambda: False)
        for lane in (-1, 8, None, True):
            with self.assertRaisesRegex(ValueError, 'Invalid'):
                send_driver_restart(MCU([{}]), Reactor(), INSTANCE, 9, lane, lambda: True)
        with self.assertRaisesRegex(ValueError, 'Invalid'):
            send_driver_restart(MCU([{}]), Reactor(), INSTANCE, 0, 1, lambda: True)

    def test_decode_bounds(self):
        base = dict(instance_hi=1, instance_lo=2, query=3, result=0, status=3)
        self.assertEqual(decode_driver_restart_result(base).instance, (1 << 32) | 2)
        for change in ({'status': 6}, {'result': 4, 'status': 3}, {'query': -1}):
            with self.assertRaises(ValueError):
                decode_driver_restart_result(dict(base, **change))


class GCode:
    _unset = object()

    def __init__(self, **params):
        self.params = params
        self.respond_info = Mock()

    def get_int(self, name, default=_unset, minval=None, maxval=None):
        if name not in self.params:
            if default is self._unset:
                raise KeyError(name)
            return default
        value = int(self.params[name])
        if (minval is not None and value < minval
                or maxval is not None and value > maxval):
            raise ValueError('%s out of range' % name)
        return value


def observer(replies, state=0):
    mmu = object.__new__(KlipperMMU)
    reactor = Reactor()
    mmu._name = 'unit'
    mmu._printer = SimpleNamespace(command_error=RuntimeError,
                                   get_reactor=lambda: reactor)
    mmu._mcu_lock = threading.Lock()
    mmu._active = mmu._connected = True
    mmu._state = 'attached'
    mmu._instance = INSTANCE
    mmu._generation = 3
    mmu._challenge = 40
    mmu._mcu = MCU(replies)

    def capture():
        mmu._challenge += 1
        return ((mmu._generation, mmu._instance), 0,
                SimpleNamespace(query=mmu._challenge, values={'state': state}), object())
    mmu.capture_physical_state = Mock(side_effect=capture)
    return mmu, reactor


class GCodeTest(unittest.TestCase):
    def test_restart_waits_for_ready_with_a_fresh_capture_per_step(self):
        mmu, reactor = observer([dict(status=3), dict(status=3), {}], state=6)
        gcmd = GCode(LANE='1')
        mmu.cmd_KLIPPER_MMU_RESTART_DRIVER(gcmd)
        self.assertEqual([args[2:] for args in mmu._mcu.sent], [[41, 1], [42, 1], [43, 1]])
        self.assertEqual(len(reactor.pauses), 2)
        message = gcmd.respond_info.call_args.args[0]
        self.assertIn('lane 1 driver ready; no motion performed', message)
        self.assertIn('RECONCILE_PATH clears the stopped fault', message)
        self.assertFalse(mmu._mcu_lock.locked())
        self.assertEqual(mmu._mcu.listeners, [])
        mmu, _ = observer([{}])
        gcmd = GCode(LANE='0')
        mmu.cmd_KLIPPER_MMU_RESTART_DRIVER(gcmd)
        self.assertNotIn('RECONCILE_PATH', gcmd.respond_info.call_args.args[0])

    def test_selector_restart_names_the_selector_not_a_lane(self):
        mmu, reactor = observer([dict(status=3), {}])
        gcmd = GCode(SELECTOR='1')
        mmu.cmd_KLIPPER_MMU_RESTART_DRIVER(gcmd)
        self.assertEqual([args[2:] for args in mmu._mcu.sent],
                         [[41, DRIVER_RESTART_SELECTOR], [42, DRIVER_RESTART_SELECTOR]])
        self.assertEqual(len(reactor.pauses), 1)
        message = gcmd.respond_info.call_args.args[0]
        self.assertIn('selector driver ready; no motion performed', message)

    def test_refusals_and_bounded_wait_are_errors(self):
        waiting = [dict(status=3)] * int(RESTART_WAIT / 0.1 + 5)
        for replies, message in (([dict(status=2)], 'busy'),
                                 ([dict(status=3), dict(status=4)], 'faulted'),
                                 ([dict(status=8)], 'not_a_tmc_feeder'),
                                 ([dict(status=1)], 'invalid_lane'),
                                 ([dict(result=4)], 'result=4'),
                                 ([None], 'timed out'),
                                 (waiting, 'still restarting')):
            mmu, _ = observer(replies)
            gcmd = GCode(LANE='1')
            with self.subTest(message=message), self.assertRaisesRegex(RuntimeError, message):
                mmu.cmd_KLIPPER_MMU_RESTART_DRIVER(gcmd)
            gcmd.respond_info.assert_not_called()
            self.assertFalse(mmu._mcu_lock.locked())
            self.assertEqual(mmu._mcu.listeners, [])

    def test_invalid_lane_connection_or_capture_sends_nothing(self):
        for params in ({}, {'LANE': '8'}, {'LANE': '-1'}, {'LANE': '1', 'SELECTOR': '1'}):
            mmu, _ = observer([{}])
            with self.subTest(params=params), self.assertRaises(RuntimeError):
                mmu.cmd_KLIPPER_MMU_RESTART_DRIVER(GCode(**params))
            self.assertEqual(mmu._mcu.sent, [])
        mmu, _ = observer([{}])
        with self.assertRaisesRegex(RuntimeError, 'exactly one of LANE or SELECTOR'):
            mmu.cmd_KLIPPER_MMU_RESTART_DRIVER(GCode(LANE='1', SELECTOR='1'))
        for field, value in (('_connected', False),
                             ('_state', 'shutdown'), ('_event_cycle_in_progress', True)):
            mmu, _ = observer([{}])
            setattr(mmu, field, value)
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, 'unavailable'):
                mmu.cmd_KLIPPER_MMU_RESTART_DRIVER(GCode(LANE='1'))
            self.assertEqual(mmu._mcu.sent, [])
        for capture in (None, ((4, INSTANCE), 0, SimpleNamespace(query=41), object())):
            mmu, _ = observer([{}])
            mmu.capture_physical_state = Mock(return_value=capture)
            with self.assertRaisesRegex(RuntimeError, 'fresh capture'):
                mmu.cmd_KLIPPER_MMU_RESTART_DRIVER(GCode(LANE='1'))
            self.assertEqual(mmu._mcu.sent, [])

    def test_connection_change_during_exchange(self):
        mmu, _ = observer([])
        def reconnect(args):
            mmu._mcu.sent.append(args)
            mmu._generation += 1
            mmu._mcu.emit()
        mmu._mcu.send = reconnect
        gcmd = GCode(LANE='1')
        with self.assertRaisesRegex(RuntimeError, 'connection changed|Stale'):
            mmu.cmd_KLIPPER_MMU_RESTART_DRIVER(gcmd)
        gcmd.respond_info.assert_not_called()


if __name__ == '__main__':
    unittest.main()
