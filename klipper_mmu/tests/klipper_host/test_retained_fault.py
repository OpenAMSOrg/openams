# SPDX-License-Identifier: GPL-3.0-or-later
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from host import KlipperMMU
from host.event_delivery import EventDelivery
from host.openams_command_device import OpenAMSCommandDevice
from host.openams_session_bootstrap import PreparedHostSession
from host.protocol import Event, Identity
from host.requests import Requests


INSTANCE = 0x1020304050607080


class Printer:
    def __init__(self, printing=True, fail=False):
        self.reactor = SimpleNamespace(monotonic=lambda: 10.0)
        self.calls = []
        self.printing = printing
        self.fail = fail

    def get_reactor(self):
        return self.reactor

    def lookup_object(self, name, default=None):
        if name == 'idle_timeout':
            def status(eventtime):
                if eventtime is None:
                    raise TypeError('Klipper idle status needs reactor time')
                return {'state': 'Printing' if self.printing else 'Ready'}
            return SimpleNamespace(get_status=status)
        if name == 'pause_resume':
            return self
        return default

    def send_pause_command(self):
        self.calls.append('PAUSE')
        if self.fail:
            raise RuntimeError('pause failed')


def pair(printing=True, fail=False):
    observer = object.__new__(KlipperMMU)
    observer._printer = Printer(printing, fail)
    observer._mcu = object()
    observer._active = observer._connected = True
    observer._state = 'attached'
    observer._instance = INSTANCE
    observer._generation = 7
    observer._mcu_lock = threading.Lock()
    observer._last_query = observer._snapshot_query = 77
    observer._host_session_ambiguous = False
    observer._host_recovery_record = None
    observer._host_lease = None
    observer.retained_fault_diagnostic = None
    prepared = PreparedHostSession(Requests(INSTANCE, 13),
                                   EventDelivery(INSTANCE, 7), 77)
    observer._prepared_host_session = prepared
    device = object.__new__(OpenAMSCommandDevice)
    device._observer = observer
    device._state = SimpleNamespace(bay_count=4, hardware_snapshot_ready=True)
    device._bound = (7, INSTANCE)
    device._captured_at = 9.0
    device._cache_ttl = 5.0
    device._captured_status = SimpleNamespace(instance=INSTANCE,
                                               values={'lane': 2})
    device._event_binding = (object(), prepared, observer._mcu,
                             observer._printer.reactor, (7, INSTANCE),
                             prepared.requests.generation)
    return observer, device


def fault(**changes):
    fields = dict(instance=INSTANCE, identity=Identity(1, 13, 27, 3, 2),
                  seq=42, timestamp_ms=100, event_type=12,
                  result_code=3, value=9, flags=0)
    fields.update(changes)
    return Event(**fields)


class RetainedFaultTest(unittest.TestCase):
    def test_current_fault_records_identity_revokes_motion_and_pauses_once(self):
        observer, device = pair()
        with patch('host.peek_event', return_value=fault()) as peek:
            self.assertEqual(observer.peek_retained_fault_once(device), 'fault')
            self.assertEqual(observer.peek_retained_fault_once(device), 'fault')
        self.assertEqual(peek.call_count, 2)
        self.assertEqual(observer._printer.calls, ['PAUSE'])
        self.assertEqual(observer.retained_fault_diagnostic['seq'], 42)
        self.assertEqual(observer.retained_fault_diagnostic['lane'], 2)
        self.assertEqual(observer.retained_fault_diagnostic['pause'], 'requested')
        self.assertTrue(observer._host_session_ambiguous)
        self.assertFalse(observer._prepared_host_session.requests.connected)

    def test_foreign_and_stale_heads_remain_retained(self):
        for event in (fault(identity=Identity(1, 14, 27, 3, 2)),
                      fault(identity=Identity(1, 13, 27, 3, 1)),
                      fault(event_type=1)):
            observer, device = pair()
            with patch('host.peek_event', return_value=event):
                self.assertEqual(observer.peek_retained_fault_once(device),
                                 'foreign')
            self.assertIsNone(observer.retained_fault_diagnostic)
            self.assertEqual(observer._printer.calls, [])

    def test_global_fault_with_matching_fresh_capture_pauses_once(self):
        for lane in (2, 255):
            observer, device = pair()
            device._captured_status.values.update(
                lane=lane, state=6, client=0, session=0, request=0,
                operation=0, system_fault=9, motion_fault=9)
            event = fault(identity=Identity(0, 0, 0, 0, lane), value=9)
            with patch('host.peek_event', return_value=event):
                self.assertEqual(observer.peek_retained_fault_once(device), 'fault')
                self.assertEqual(observer.peek_retained_fault_once(device), 'fault')
            self.assertEqual(observer._printer.calls, ['PAUSE'])
            self.assertEqual(observer.retained_fault_diagnostic['lane'], lane)
            self.assertEqual(observer.retained_fault_diagnostic['session'], 0)
            self.assertFalse(observer._prepared_host_session.requests.connected)

    def test_global_fault_requires_fresh_coherent_fault_capture(self):
        base = dict(lane=255, state=6, client=0, session=0, request=0,
                    operation=0, system_fault=9, motion_fault=9)
        event = fault(identity=Identity(0, 0, 0, 0, 255), value=9)
        for changes in (dict(state=0), dict(client=1), dict(session=13),
                        dict(request=1), dict(operation=3), dict(lane=2),
                        dict(system_fault=0), dict(motion_fault=0),
                        dict(system_fault=8)):
            observer, device = pair()
            device._captured_status.values.update(base, **changes)
            with patch('host.peek_event', return_value=event):
                self.assertEqual(observer.peek_retained_fault_once(device), 'foreign')
            self.assertEqual(observer._printer.calls, [])
        observer, device = pair()
        device._captured_status.values.update(base)
        device._captured_at = 0.0
        with patch('host.peek_event', return_value=event):
            self.assertEqual(observer.peek_retained_fault_once(device), 'foreign')
        self.assertEqual(observer._printer.calls, [])

    def test_pause_failure_keeps_fault_and_revocation(self):
        observer, device = pair(fail=True)
        with patch('host.peek_event', return_value=fault(event_type=7)):
            self.assertEqual(observer.peek_retained_fault_once(device), 'fault')
        self.assertIn('pause failed', observer.retained_fault_diagnostic['pause'])
        self.assertTrue(observer._host_session_ambiguous)
        # A follow fault is kept for KLIPPER_MMU_ACK_FAULT like FAULT_ENTERED.
        self.assertEqual(observer._retained_fault_event, fault(event_type=7))

    def test_idle_fault_does_not_pause(self):
        observer, device = pair(printing=False)
        with patch('host.peek_event', return_value=fault()):
            self.assertEqual(observer.peek_retained_fault_once(device), 'fault')
        self.assertEqual(observer._printer.calls, [])


if __name__ == '__main__':
    unittest.main()
