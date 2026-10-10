# SPDX-License-Identifier: GPL-3.0-or-later
import unittest
from unittest.mock import patch

from host.openams_command_device import OpenAMSCommandDevice
from host.event_delivery import EventDelivery
from host.openams_operation_bridge import (OpenAMSLoadCommandBridge,
                                           OpenAMSPTFECalibrationCommandBridge)
from host.openams_session_bootstrap import PreparedHostSession
from host.protocol import Admission
from host.requests import LOAD, Requests


INSTANCE = 0x123456789


class Observer:
    generation = 3

    def physical_connection_identity(self):
        return (self.generation, INSTANCE)

    def physical_monotonic(self):
        return 10.0

    def capture_physical_state(self):
        return None


class CommandDeviceTests(unittest.TestCase):
    def setUp(self):
        self.requests = Requests(INSTANCE, 17, 41)
        self.mcu = object()
        self.reactor = object()
        self.load = OpenAMSLoadCommandBridge(
            self.mcu, self.reactor, self.requests, 'fps', 4,
            is_current=lambda: True, motion_permitted=lambda: True,
            accept_terminal=lambda *args: True)

    def device(self, **bridges):
        return OpenAMSCommandDevice('oams 7', Observer(), 7, 'fps',
                                    'boxturtle', 4, 842.5, **bridges)

    def bind(self, device):
        runtime = type('Runtime', (), {
            'accept_terminal': lambda *args: True,
            'accept_reload_terminal': lambda *args: False})()
        prepared = PreparedHostSession(self.requests, EventDelivery(INSTANCE, 3), 9)
        device.bind_runtime(runtime, 'fps', prepared, self.mcu, self.reactor)

    def test_load_admission_keeps_exact_waiter_pending(self):
        device = self.device(load=self.load)
        self.bind(device)
        self.assertFalse(device.hardware_snapshot_ready)
        self.assertEqual(device.oams_idx, 7)
        def admitted(mcu, reactor, instance, identity, current, **kwargs):
            self.assertIs(mcu, self.mcu)
            self.assertTrue(current())
            self.assertEqual((identity.request, identity.lane), (42, 2))
            return Admission(instance, identity, 0, 0)
        with patch('host.openams_operation_bridge.send_operation',
                   side_effect=admitted):
            result = device.start_load_spool(2, gen=5)
        self.assertEqual(result.identity.request, 42)
        self.assertIn('fps', self.requests.pending)
        self.assertFalse(device.hardware_snapshot_ready)

    def test_ptfe_calibration_uses_guarded_bridge(self):
        calibrate = OpenAMSPTFECalibrationCommandBridge(
            self.mcu, self.reactor, self.requests, 'fps', 4,
            is_current=lambda: True, motion_permitted=lambda: True,
            accept_terminal=lambda *args: True)
        device = self.device(calibrate=calibrate)
        self.bind(device)

        def admitted(mcu, reactor, instance, identity, current, **kwargs):
            self.assertEqual((identity.operation, identity.lane), (4, 1))
            self.assertTrue(current())
            return Admission(instance, identity, 0, 0)

        with patch('host.openams_operation_bridge.send_operation',
                   side_effect=admitted):
            result = device.start_calibrate('ptfe', 1, gen=7)
        self.assertEqual(result.identity.operation, 4)
        self.assertIn('fps', self.requests.pending)
        with self.assertRaisesRegex(ValueError, 'Unsupported'):
            device.start_calibrate('hub_hes', 1, gen=8)

    def test_missing_bridges_and_unsupported_lifecycle_fail_closed(self):
        device = self.device()
        for action in (lambda: device.start_load_spool(0, 1),
                       lambda: device.start_unload_spool(1),
                       device.load_spool_cancel,
                       lambda: device.set_oams_follower(1, 0),
                       device.service_event_once,
                       device.clear_errors,
                       lambda: device.start_calibrate('ptfe_length', 0, 1),
                       lambda: device.set_led_error(0, 1)):
            with self.subTest(action=action):
                with self.assertRaises(RuntimeError):
                    action()
        with self.assertRaisesRegex(ValueError, 'Invalid OpenAMS event binding'):
            device.bind_runtime(object(), 'fps')

    def test_rejects_other_path_and_connection(self):
        wrong_path = OpenAMSLoadCommandBridge(
            self.mcu, self.reactor, self.requests, 'other', 4,
            is_current=lambda: True, motion_permitted=lambda: True,
            accept_terminal=lambda *args: True)
        with self.assertRaisesRegex(ValueError, 'topology'):
            self.device(load=wrong_path)
        from host.openams_cancel_bridge import OpenAMSLoadCancelBridge
        other_connection = OpenAMSLoadCancelBridge(
            self.mcu, self.reactor, Requests(INSTANCE, 17, 41), 'fps',
            is_current=lambda: True)
        with self.assertRaisesRegex(ValueError, 'one connection'):
            self.device(load=self.load, cancel=other_connection)

    def test_command_requires_live_event_binding(self):
        device = self.device(load=self.load)
        with patch('host.openams_operation_bridge.send_operation') as send:
            with self.assertRaisesRegex(RuntimeError, 'event connection'):
                device.start_load_spool(2, 5)
            self.bind(device)
            device._observer.generation = 4
            with self.assertRaisesRegex(RuntimeError, 'event connection'):
                device.start_load_spool(2, 5)
            send.assert_not_called()

    def test_one_explicit_event_cycle_on_same_prepared_connection(self):
        observer = Observer()
        device = OpenAMSCommandDevice('oams 7', observer, 7, 'fps',
                                      'boxturtle', 4, 842.5, load=self.load)
        prepared = PreparedHostSession(self.requests, EventDelivery(INSTANCE, 3), 9)
        runtime = type('Runtime', (), {
            'accept_terminal': lambda *args: True,
            'accept_reload_terminal': lambda *args: False})()
        device.bind_runtime(runtime, 'fps', prepared, self.mcu, self.reactor)
        with patch('host.openams_command_device.run_event_cycle',
                   return_value='blocked') as cycle:
            self.assertEqual(device.service_event_once(timeout=0.2), 'blocked')
            self.assertEqual(cycle.call_count, 1)
            self.assertTrue(cycle.call_args.args[5]())
            self.assertIsNone(cycle.call_args.args[8])  # No ACK accounting hook.
            observer.record_acked = lambda event: None
            device.service_event_once()
            self.assertIs(cycle.call_args.args[8], observer)
            observer.generation = 4
            with self.assertRaisesRegex(ValueError, 'connection changed'):
                device.service_event_once()
            self.assertEqual(cycle.call_count, 2)

    def test_event_binding_rejects_mismatched_bridge_and_generation(self):
        device = self.device(load=self.load)
        runtime = type('Runtime', (), {
            'accept_terminal': lambda *args: True,
            'accept_reload_terminal': lambda *args: False})()
        prepared = PreparedHostSession(self.requests, EventDelivery(INSTANCE, 3), 9)
        with self.assertRaisesRegex(ValueError, 'command bridge'):
            device.bind_runtime(runtime, 'fps', prepared, object(), self.reactor)
        stale = PreparedHostSession(self.requests, EventDelivery(INSTANCE, 2), 9)
        with self.assertRaisesRegex(ValueError, 'physical unit'):
            device.bind_runtime(runtime, 'fps', stale, self.mcu, self.reactor)
        device.bind_runtime(runtime, 'fps', prepared, self.mcu, self.reactor)
        with self.assertRaisesRegex(RuntimeError, 'already bound'):
            device.bind_runtime(runtime, 'fps', prepared, self.mcu, self.reactor)
        self.requests.disconnect()
        with self.assertRaisesRegex(ValueError, 'connection changed'):
            device.service_event_once()

    def test_retire_preserves_request_matches_and_requires_fresh_binding(self):
        observer = Observer()
        device = OpenAMSCommandDevice('oams 7', observer, 7, 'fps',
                                      'boxturtle', 4, 842.5, load=self.load)
        delivery = EventDelivery(INSTANCE, 3)
        prepared = PreparedHostSession(self.requests, delivery, 9)
        runtime = type('Runtime', (), {
            'accept_terminal': lambda *args: True,
            'accept_reload_terminal': lambda *args: False})()
        waiter = self.requests.issue(LOAD, 2, 'fps', 7)
        accepted_head = object()
        delivery.pending = accepted_head
        device.bind_runtime(runtime, 'fps', prepared, self.mcu, self.reactor)
        observer.generation = 4
        with patch('host.openams_command_device.run_event_cycle') as cycle:
            with self.assertRaisesRegex(ValueError, 'connection changed'):
                device.service_event_once()
            old_requests, pending_ack = device.retire_event_binding()
            self.assertIs(old_requests, self.requests)
            self.assertIs(pending_ack, accepted_head)
            self.assertFalse(old_requests.connected)
            self.assertEqual(old_requests.pending['fps'][0], waiter)
            self.assertFalse(delivery.connected)
            self.assertIsNone(delivery.pending)
            with self.assertRaisesRegex(RuntimeError, 'not bound'):
                device.service_event_once()
            with self.assertRaisesRegex(RuntimeError, 'not configured'):
                device.start_load_spool(2, 8)
            cycle.assert_not_called()
            new_requests = Requests(INSTANCE, 18, 42)
            fresh = PreparedHostSession(new_requests, EventDelivery(INSTANCE, 4), 10)
            with self.assertRaisesRegex(RuntimeError, 'require reconciliation'):
                device.bind_runtime(runtime, 'fps', fresh, self.mcu, self.reactor)
            # The owner settles this old waiter as unknown before rebinding.
            old_requests.pending.clear()
            with self.assertRaisesRegex(RuntimeError, 'ACK head requires reconciliation'):
                device.bind_runtime(runtime, 'fps', fresh, self.mcu, self.reactor)
            cycle.assert_not_called()

    def test_rebind_after_retiring_connection_without_unresolved_head(self):
        observer = Observer()
        device = OpenAMSCommandDevice('oams 7', observer, 7, 'fps',
                                      'boxturtle', 4, 842.5, load=self.load)
        runtime = type('Runtime', (), {
            'accept_terminal': lambda *args: True,
            'accept_reload_terminal': lambda *args: False})()
        prepared = PreparedHostSession(self.requests, EventDelivery(INSTANCE, 3), 9)
        device.bind_runtime(runtime, 'fps', prepared, self.mcu, self.reactor)
        observer.generation = 4
        old_requests, pending_ack = device.retire_event_binding()
        self.assertIs(old_requests, self.requests)
        self.assertIsNone(pending_ack)
        new_requests = Requests(INSTANCE, 18, 42)
        fresh = PreparedHostSession(new_requests, EventDelivery(INSTANCE, 4), 10)
        with patch('host.openams_command_device.run_event_cycle',
                   return_value='empty') as cycle:
            device.bind_runtime(runtime, 'fps', fresh, self.mcu, self.reactor)
            self.assertEqual(device.service_event_once(), 'empty')
            self.assertEqual(cycle.call_count, 1)
            self.assertIs(cycle.call_args.args[3], new_requests)


if __name__ == '__main__':
    unittest.main()
