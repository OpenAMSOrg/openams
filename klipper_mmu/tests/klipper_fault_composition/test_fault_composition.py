# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise diagnosed HOST fault recovery against the real OpenAMS reducer."""
import threading
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests' / 'klipper_ams_family_contract'))
import test_fault_hook as fixture  # noqa: E402
from host import HostRecoveryRecord, KlipperMMU  # noqa: E402
from host.openams_managed_unit import OpenAMSManagedUnit  # noqa: E402
from host.protocol import Event, Identity  # noqa: E402

INSTANCE = 0x1020304050607080


class Wrapper:
    def __init__(self, mcu, callback, name):
        self.mcu, self.callback, self.name = mcu, callback, name

    def unregister(self):
        self.mcu.listeners.remove(self)


class MCU:
    def __init__(self, event, ack_result=0):
        self.event, self.ack_result = event, ack_result
        self.listeners, self.sent = [], []

    def register_serial_response(self, callback, message):
        wrapper = Wrapper(self, callback, message.split()[0])
        self.listeners.append(wrapper)
        return wrapper

    def lookup_command(self, message):
        mcu = self
        class Command:
            def send(self, args):
                mcu.sent.append((message.split()[0], args))
                hi, lo = INSTANCE >> 32, INSTANCE & 0xffffffff
                if message.startswith('km_event_peek'):
                    e = mcu.event
                    mcu.emit('km_event', dict(instance_hi=hi, instance_lo=lo,
                        seq=e.seq, timestamp_ms=e.timestamp_ms,
                        client=e.identity.client, session=e.identity.session,
                        request=e.identity.request, operation=e.identity.operation,
                        lane=e.identity.lane, event_type=e.event_type,
                        result_code=e.result_code, value=e.value, flags=e.flags))
                else:
                    mcu.emit('km_command_result', dict(instance_hi=hi,
                        instance_lo=lo, command=6, query=args[-1],
                        result=mcu.ack_result))
        return Command()

    def emit(self, name, params):
        for wrapper in list(self.listeners):
            if wrapper.name == name:
                wrapper.callback(params)


class Completion(fixture.Completion):
    def wait(self, deadline=None):
        return self.result


class Reactor(fixture.Reactor):
    def completion(self):
        return Completion()

    def register_async_callback(self, callback):
        callback(self.monotonic())


class FaultComposition(unittest.TestCase):
    S, Runtime = fixture.S, fixture.runtime_module.Runtime

    def setUp(self):
        self.gcode, self.oam, self.reactor = fixture.Gcode(), fixture.Oam(), Reactor()
        self.runtime = self.Runtime(fixture.Printer(self.reactor, self.gcode), ['fps'],
                                    lambda now: self.S.World(),
                                    lambda idx: self.oam)
        self.waiter = self.runtime.request('fps', self.S.LoadBay('fps', (5, 2)))
        self.event = Event(INSTANCE, Identity(1, 13, 27, 3, 2),
                           42, 100, 12, 3, 9, 0)
        self.mcu = MCU(self.event)
        self.observer = object.__new__(KlipperMMU)
        o = self.observer
        o._mcu_lock, o._event_cycle_in_progress = threading.Lock(), False
        o._host_session_ambiguous = True
        o._host_lease = None
        o._mcu = self.mcu
        o._printer = SimpleNamespace(get_reactor=lambda: self.reactor)
        o._active = o._connected = True
        o._state, o._instance, o._generation = 'attached', INSTANCE, 7
        o._retained_fault_event = self.event
        o._host_recovery_record = HostRecoveryRecord(INSTANCE, 13, 7, 1,
                                                       None, False, ())
        fields = dict(state=6, lane=2, system_fault=9, motion_fault=9,
                      client=1, session=13, request=27, operation=3,
                      client_sessions=(0, 13, 0, 0))
        o.capture_physical_state = lambda: ((7, INSTANCE), 10,
            SimpleNamespace(instance=INSTANCE, values=fields), object())
        self.device = object.__new__(OpenAMSManagedUnit)
        self.device._observer = o
        self.device._state = SimpleNamespace(bay_count=4, fps_name='fps', oams_idx=5)
        self.device._runtime = self.runtime
        o._openams_unit = self.device

    def assert_retained(self):
        self.assertIs(self.observer._retained_fault_event, self.event)
        self.assertTrue(self.observer._host_session_ambiguous)
        self.assertIsNotNone(self.observer._host_recovery_record)
        self.assertIsNone(self.observer._host_lease)
        self.assertEqual(self.mcu.listeners, [])

    def test_safe_fault_pauses_and_exact_ack_only_clears_event(self):
        self.assertTrue(self.device.retry_retained_fault_ack())
        self.assertEqual(self.runtime.get_state().firmware_faults[2][:2], ('fps', 9))
        self.assertEqual(self.waiter.wait().code, 9)
        self.assertFalse(self.runtime.get_state().lanes['fps'].following)
        self.assertEqual(self.gcode.calls, ['PAUSE'])
        self.assertEqual([name for name, _ in self.mcu.sent],
                         ['km_event_peek', 'km_event_ack'])
        self.assertEqual(self.mcu.sent[-1][1],
                         [INSTANCE >> 32, INSTANCE & 0xffffffff,
                          1, 13, 27, 3, 2, 42])
        self.assertIsNone(self.observer._retained_fault_event)
        self.assertTrue(self.observer._host_session_ambiguous)
        self.assertIsNotNone(self.observer._host_recovery_record)
        self.assertEqual(self.mcu.listeners, [])

    def test_failed_pause_retains_event_then_exact_retry_acks(self):
        self.gcode.fail = True
        with self.assertRaises(ValueError):
            self.device.retry_retained_fault_ack()
        self.assert_retained()
        self.assertEqual([name for name, _ in self.mcu.sent], ['km_event_peek'])
        self.gcode.fail = False
        self.assertTrue(self.device.retry_retained_fault_ack())
        self.assertEqual(self.gcode.calls, ['PAUSE', 'PAUSE'])
        self.assertIsNone(self.observer._retained_fault_event)

    def test_stale_ack_retains_event_for_exact_retry(self):
        self.mcu.ack_result = 3
        with self.assertRaisesRegex(ValueError, 'ACK rejected'):
            self.device.retry_retained_fault_ack()
        self.assert_retained()
        self.assertEqual(self.gcode.calls, ['PAUSE'])
        self.mcu.ack_result = 0
        self.assertTrue(self.device.retry_retained_fault_ack())
        self.assertEqual(self.gcode.calls, ['PAUSE'])

    def test_foreign_head_refuses_reducer_and_ack(self):
        self.mcu.event = Event(INSTANCE, self.event.identity, 43, 100,
                               12, 3, 9, 0)
        with self.assertRaisesRegex(ValueError, 'queue head changed'):
            self.device.retry_retained_fault_ack()
        self.assert_retained()
        self.assertEqual(self.runtime.get_state().firmware_faults, {})
        self.assertEqual(self.gcode.calls, [])
        self.assertEqual([name for name, _ in self.mcu.sent], ['km_event_peek'])


if __name__ == '__main__':
    unittest.main()
