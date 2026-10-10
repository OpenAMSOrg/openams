# SPDX-License-Identifier: GPL-3.0-or-later
import threading
import unittest

from host import KlipperMMU
from host.event_delivery import EventDelivery
from host.openams_command_device import OpenAMSCommandDevice
from host.openams_session_bootstrap import PreparedHostSession
from host.requests import Requests


INSTANCE = 0x1020304050607080


class Printer:
    def __init__(self):
        self.reactor = object()

    def get_reactor(self):
        return self.reactor


def pair():
    observer = object.__new__(KlipperMMU)
    observer._printer = Printer()
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
    prepared = PreparedHostSession(Requests(INSTANCE, 13),
                                   EventDelivery(INSTANCE, 7), 77)
    observer._prepared_host_session = prepared
    device = object.__new__(OpenAMSCommandDevice)
    device._observer = observer
    device._event_binding = (object(), prepared, observer._mcu,
                             observer._printer.reactor, (7, INSTANCE),
                             prepared.requests.generation)
    return observer, device


class EventEntryTest(unittest.TestCase):
    def test_one_cycle_is_serialized_and_returns_result(self):
        observer, device = pair()
        called = []

        def service(timeout, consumer_boundary):
            called.append((timeout, observer._mcu_lock.locked()))
            with consumer_boundary():
                self.assertFalse(observer._mcu_lock.locked())
                with self.assertRaisesRegex(ValueError, 'in progress'):
                    observer.service_openams_event_once(device)
            return 'blocked'

        device.service_event_once = service
        self.assertEqual(observer.service_openams_event_once(device, 0.2),
                         'blocked')
        self.assertEqual(called, [(0.2, True)])
        self.assertFalse(observer._mcu_lock.locked())

    def test_reentrant_and_foreign_binding_send_nothing(self):
        observer, device = pair()
        calls = []
        device.service_event_once = lambda timeout, consumer_boundary: calls.append(timeout)
        # Another cycle's consumer released the mutex but holds the latch.
        observer._event_cycle_in_progress = True
        with self.assertRaisesRegex(ValueError, 'in progress'):
            observer.service_openams_event_once(device)
        observer._event_cycle_in_progress = False
        device._observer = object()
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            observer.service_openams_event_once(device)
        device._observer = observer
        foreign = PreparedHostSession(Requests(INSTANCE, 13),
                                      EventDelivery(INSTANCE, 7), 77)
        device._event_binding = (object(), foreign, observer._mcu,
                                 observer._printer.reactor, (7, INSTANCE), 0)
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            observer.service_openams_event_once(device)
        self.assertEqual(calls, [])

    def test_connection_change_during_cycle_latches_without_retry(self):
        observer, device = pair()
        pending = object()
        observer._prepared_host_session.delivery.pending = pending
        calls = []

        def changed(timeout, consumer_boundary):
            calls.append(timeout)
            observer._generation += 1
            return 'acked'

        device.service_event_once = changed
        with self.assertRaisesRegex(ConnectionError, 'changed'):
            observer.service_openams_event_once(device)
        self.assertTrue(observer._host_session_ambiguous)
        self.assertIs(observer.host_recovery_record().pending_ack, pending)
        self.assertFalse(observer._mcu_lock.locked())
        with self.assertRaises(ValueError):
            observer.service_openams_event_once(device)
        self.assertEqual(len(calls), 1)

    def test_swallowed_follower_stop_failure_cannot_ack(self):
        observer, device = pair()
        calls = []

        def stop(enable, direction):
            calls.append((enable, direction, observer._mcu_lock.locked()))
            raise RuntimeError('command rejected')

        device.set_oams_follower = stop

        def service(timeout, consumer_boundary):
            with consumer_boundary():
                # The pinned OpenAMS runtime catches effect exceptions and
                # settles the waiter; the boundary must still reject ACK.
                try:
                    device.set_oams_follower(0, 1)
                except RuntimeError:
                    pass
            return 'acked'

        device.service_event_once = service
        with self.assertRaisesRegex(RuntimeError, 'STOP failed'):
            observer.service_openams_event_once(device)
        self.assertEqual(calls, [(0, 1, False)])
        self.assertFalse(observer._mcu_lock.locked())
        self.assertFalse(observer._event_cycle_in_progress)


if __name__ == '__main__':
    unittest.main()
