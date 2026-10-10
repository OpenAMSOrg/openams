# SPDX-License-Identifier: GPL-3.0-or-later
import threading
import unittest
from unittest.mock import patch

from host import KlipperMMU
from host.event_delivery import EventDelivery
from host.openams_session_bootstrap import PreparedHostSession
from host.protocol import CommandResult, SensorSample, SensorSnapshot, Snapshot
from host.requests import Requests


INSTANCE = 0x1020304050607080


class Reactor:
    now = 1.0

    def monotonic(self):
        return self.now


class Printer:
    reactor = Reactor()

    def get_reactor(self):
        return self.reactor


def ready_capture(device, **changes):
    values = dict(motor_ready=True, host_connected=True,
                  client_sessions=(0, 13, 0, 0), system_fault=0,
                  motion_fault=0, follower_fault=0,
                  position_uncertain=False, pending_fault_event=False)
    values.update(changes)
    sensor = SensorSample(True, False, True, True, 100)
    return ((device._generation, INSTANCE), 1.0,
            Snapshot(INSTANCE, 77, values),
            SensorSnapshot(INSTANCE, 77, 2, 100,
                           (sensor, sensor), sensor, sensor))


def observer():
    device = object.__new__(KlipperMMU)
    device._printer = Printer()
    device._mcu = object()
    device._active = device._connected = True
    device._state = 'attached'
    device._generation, device._instance = 7, INSTANCE
    device._mcu_lock = threading.Lock()
    device._last_query = device._snapshot_query = 77
    device._firmware_actuation = 1
    device._firmware_host_lease_ms = 500
    device._startup_state = 'ready'
    device._startup_result = 0
    device._host_session_ambiguous = False
    device._host_recovery_record = None
    device._host_lease = None
    device._prepared_host_session = PreparedHostSession(
        Requests(INSTANCE, 13), EventDelivery(INSTANCE, 7), 77)
    device.capture_physical_state = lambda: ready_capture(device)
    return device


class HostLeaseEntryTest(unittest.TestCase):
    def test_watchdog_must_match_advertised_firmware_lease(self):
        device = observer()
        device._openams_unit = type('Unit', (), {
            '_host_watchdog_ms': 500, '_lease_max_age_ms': 200})()
        self.assertTrue(device._host_lease_configuration_matches())
        device._firmware_host_lease_ms = 400
        self.assertFalse(device._host_lease_configuration_matches())
        device._firmware_host_lease_ms = 500
        with patch('host.openams_host_lease.send_heartbeat') as beat:
            for value in (None, 400, 60000):
                device._firmware_host_lease_ms = value
                with self.subTest(value=value), self.assertRaisesRegex(
                        ValueError, 'unavailable'):
                    device.start_host_lease(device._prepared_host_session,
                                            500, 200)
            beat.assert_not_called()

    def test_renewal_waits_for_a_concurrent_exchange(self):
        device = observer()
        with patch('host.openams_host_lease.send_heartbeat',
                   return_value=CommandResult(INSTANCE, 3, 13, 0)) as beat:
            lease = device.start_host_lease(device._prepared_host_session,
                                            500, 200)
            device._printer.reactor.now = 1.1
            device._mcu_lock.acquire()  # Another MCU exchange is in progress.
            renewal = threading.Thread(target=device.renew_host_lease)
            renewal.start()
            renewal.join(0.05)
            self.assertEqual(beat.call_count, 1)
            device._mcu_lock.release()
            renewal.join(5)
            self.assertEqual(beat.call_count, 2)
            self.assertTrue(lease.available)
            self.assertFalse(device._host_session_ambiguous)

    def test_pre_start_counter_is_previous_session_until_first_admission(self):
        device = observer()
        device.capture_physical_state = lambda: ready_capture(
            device, client_sessions=(0, 12, 0, 0))
        with patch('host.openams_host_lease.send_heartbeat',
                   return_value=CommandResult(INSTANCE, 3, 13, 0)):
            lease = device.start_host_lease(device._prepared_host_session,
                                            500, 200)
            self.assertTrue(lease.available)
            device.capture_physical_state = lambda: ready_capture(device)
            device._printer.reactor.now = 1.1
            self.assertTrue(device.renew_host_lease())

    def test_foreign_counter_never_sends_lease_heartbeat(self):
        for observed in (11, 14):
            with self.subTest(observed=observed):
                device = observer()
                device.capture_physical_state = lambda: ready_capture(
                    device, client_sessions=(0, observed, 0, 0))
                with patch('host.openams_host_lease.send_heartbeat') as beat:
                    with self.assertRaisesRegex(ConnectionError, 'readiness'):
                        device.start_host_lease(device._prepared_host_session,
                                                500, 200)
                    beat.assert_not_called()

    def test_explicit_start_and_renew_use_one_prepared_session(self):
        device = observer()
        with patch('host.openams_host_lease.send_heartbeat',
                   return_value=CommandResult(INSTANCE, 3, 13, 0)) as beat:
            lease = device.start_host_lease(device._prepared_host_session,
                                            500, 200)
            self.assertIs(device._host_lease, lease)
            self.assertFalse(device._mcu_lock.locked())
            device._printer.reactor.now = 1.1
            self.assertTrue(device.renew_host_lease())
            self.assertEqual(beat.call_count, 2)

    def test_readiness_failure_never_sends_and_latches_closed(self):
        device = observer()
        device.capture_physical_state = lambda: ready_capture(
            device, motor_ready=False)
        with patch('host.openams_host_lease.send_heartbeat') as beat:
            with self.assertRaisesRegex(ConnectionError, 'readiness'):
                device.start_host_lease(device._prepared_host_session, 500, 200)
            beat.assert_not_called()
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                device.renew_host_lease()
        self.assertTrue(device._host_session_ambiguous)
        self.assertFalse(device._prepared_host_session.requests.connected)

    def test_timeout_or_reconnect_cannot_retry_same_session(self):
        for outcome in ('timeout', 'reconnect'):
            with self.subTest(outcome=outcome):
                device = observer()

                def reply(*args, **kwargs):
                    if outcome == 'timeout':
                        raise ValueError('MMU command timed out')
                    device._generation += 1
                    return CommandResult(INSTANCE, 3, 13, 0)

                with patch('host.openams_host_lease.send_heartbeat',
                           side_effect=reply) as beat:
                    with self.assertRaises((ValueError, ConnectionError)):
                        device.start_host_lease(
                            device._prepared_host_session, 500, 200)
                    with self.assertRaises(ValueError):
                        device.renew_host_lease()
                    self.assertEqual(beat.call_count, 1)
                self.assertFalse(device._mcu_lock.locked())
                self.assertTrue(device._host_session_ambiguous)

    def test_faulted_readiness_services_the_event_before_retaining_recovery(self):
        # A board fault on this live session must deliver the interrupted
        # operation's retained terminal before the recovery record freezes the
        # queue head, and must never send the renewal heartbeat itself.
        for changes in (dict(system_fault=9, motion_fault=9),
                        dict(motor_ready=False, system_fault=9)):
            with self.subTest(changes=changes):
                device = observer()
                unit = object()
                device._openams_unit = unit
                with patch('host.openams_host_lease.send_heartbeat',
                           return_value=CommandResult(INSTANCE, 3, 13, 0)):
                    lease = device.start_host_lease(
                        device._prepared_host_session, 500, 200)
                self.assertTrue(lease.available)
                device.capture_physical_state = (
                    lambda: ready_capture(device, **changes))
                calls = []

                def service(candidate, timeout=5.0):
                    # At call time HOST motion is already revoked, the session
                    # is still unambiguous, and no MCU exchange is in flight.
                    self.assertIs(candidate, unit)
                    self.assertFalse(device._host_session_ambiguous)
                    self.assertIsNone(lease.confirmed_at)
                    self.assertFalse(device._mcu_lock.locked())
                    calls.append(timeout)

                with patch.object(device, 'service_openams_event_once',
                                  side_effect=service), \
                        patch('host.openams_host_lease.send_heartbeat') as beat:
                    with self.assertRaisesRegex(ConnectionError, 'readiness'):
                        device.renew_host_lease()
                    beat.assert_not_called()
                self.assertEqual(len(calls), 1)
                self.assertTrue(device._host_session_ambiguous)
                self.assertIsNone(device._host_lease)
                self.assertIsNotNone(device._host_recovery_record)
        # Only a fault field earns the one event service. A plain readiness
        # change, a foreign identity, or a foreign counter still refuses
        # renewal without touching the event queue.
        for name, changes, bump in (
                ('motor not ready without a fault', dict(motor_ready=False), 0),
                ('host disconnected', dict(host_connected=False), 0),
                ('foreign HOST session', dict(client_sessions=(0, 14, 0, 0)), 0),
                ('different identity', {}, 1)):
            with self.subTest(name=name):
                device = observer()
                device._openams_unit = object()
                with patch('host.openams_host_lease.send_heartbeat',
                           return_value=CommandResult(INSTANCE, 3, 13, 0)):
                    device.start_host_lease(
                        device._prepared_host_session, 500, 200)
                device._generation += bump
                device.capture_physical_state = (
                    lambda: ready_capture(device, **changes))
                with patch.object(device, 'service_openams_event_once') as service, \
                        patch('host.openams_host_lease.send_heartbeat') as beat:
                    with self.assertRaisesRegex(ConnectionError, 'readiness'):
                        device.renew_host_lease()
                    service.assert_not_called()
                    beat.assert_not_called()
                self.assertTrue(device._host_session_ambiguous)
                self.assertIsNone(device._host_lease)

    def test_foreign_prepared_session_and_invalid_bound_send_nothing(self):
        device = observer()
        other = PreparedHostSession(Requests(INSTANCE, 13),
                                    EventDelivery(INSTANCE, 7), 77)
        with patch('host.openams_host_lease.send_heartbeat') as beat:
            with self.assertRaises(ValueError):
                device.start_host_lease(other, 500, 200)
            with self.assertRaises(ValueError):
                device.start_host_lease(device._prepared_host_session,
                                        500, 500)
            beat.assert_not_called()


if __name__ == '__main__':
    unittest.main()
