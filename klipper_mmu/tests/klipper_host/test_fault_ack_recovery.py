# SPDX-License-Identifier: GPL-3.0-or-later
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from host import HostRecoveryRecord, KlipperMMU
from host.openams_managed_unit import OpenAMSManagedUnit
from host.protocol import Event, Identity


INSTANCE = 0x1020304050607080


def setup(client=1, lane=2):
    observer = object.__new__(KlipperMMU)
    observer._mcu_lock = threading.Lock()
    observer._event_cycle_in_progress = False
    observer._host_session_ambiguous = True
    observer._host_lease = None
    observer._mcu = object()
    reactor = object()
    observer._printer = SimpleNamespace(get_reactor=lambda: reactor)
    observer._active = observer._connected = True
    observer._state = 'attached'
    observer._instance = INSTANCE
    observer._generation = 7
    event = Event(INSTANCE, Identity(client, 13 if client else 0,
                                    27 if client else 0,
                                    3 if client else 0, lane),
                  42, 100, 12, 3, 9, 0)
    observer._retained_fault_event = event
    record = HostRecoveryRecord(INSTANCE, 13, 7, 1, None, False, ())
    observer._host_recovery_record = record
    fields = dict(state=6, lane=lane, system_fault=9, motion_fault=9,
                  client=client, session=13 if client else 0,
                  request=27 if client else 0, operation=3 if client else 0,
                  client_sessions=(0, 13, 0, 0))
    status = SimpleNamespace(instance=INSTANCE, values=fields)
    observer.capture_physical_state = Mock(return_value=((7, INSTANCE), 10,
                                                         status, object()))
    device = object.__new__(OpenAMSManagedUnit)
    device._observer = observer
    device._state = SimpleNamespace(bay_count=4, fps_name='fps', oams_idx=5)
    runtime = SimpleNamespace(accept_firmware_fault=Mock(return_value=True),
                              get_state=lambda: SimpleNamespace(lanes={
                                  'fps': SimpleNamespace(unit=(5, 2))}))
    device._runtime = runtime
    observer._openams_unit = device
    return observer, device, runtime, event, fields


class FaultAckRecoveryTest(unittest.TestCase):
    def test_managed_gcode_confirms_ack_without_releasing_recovery(self):
        observer, device, runtime, event, _ = setup()
        observer._printer.command_error = ValueError
        gcmd = SimpleNamespace(respond_info=Mock())
        with patch('host.peek_event', return_value=event), \
                patch('host.event_delivery.send_pending_ack', return_value=True):
            observer.cmd_KLIPPER_MMU_ACK_FAULT(gcmd)
        gcmd.respond_info.assert_called_once()
        self.assertIn('HOST recovery remain latched',
                      gcmd.respond_info.call_args.args[0])
        self.assertIsNone(observer._retained_fault_event)
        self.assertTrue(observer._host_session_ambiguous)
        self.assertIsNone(observer._host_lease)

    def test_managed_gcode_refusal_preserves_retained_fault(self):
        observer, device, runtime, event, fields = setup()
        observer._printer.command_error = ValueError
        gcmd = SimpleNamespace(respond_info=Mock())
        fields['system_fault'] = 8
        with self.assertRaisesRegex(ValueError, 'ACK_FAULT: Retained fault capture changed'):
            observer.cmd_KLIPPER_MMU_ACK_FAULT(gcmd)
        gcmd.respond_info.assert_not_called()
        self.assertIs(observer._retained_fault_event, event)

    def test_gcode_requires_managed_unit(self):
        observer, _, _, _, _ = setup()
        observer._printer.command_error = ValueError
        observer._openams_unit = None
        with self.assertRaisesRegex(ValueError, 'requires openams_motion'):
            observer.cmd_KLIPPER_MMU_ACK_FAULT(SimpleNamespace())

    def test_accepts_and_confirms_exact_head_without_releasing_recovery(self):
        observer, device, runtime, event, fields = setup()
        with patch('host.peek_event', return_value=event), \
                patch('host.event_delivery.send_pending_ack', return_value=True) as ack:
            self.assertTrue(device.retry_retained_fault_ack())
        self.assertEqual(runtime.accept_firmware_fault.call_count, 1)
        args = runtime.accept_firmware_fault.call_args.args
        self.assertIs(runtime.accept_firmware_fault.call_args.kwargs['from_command'], True)
        self.assertEqual(args[:3], ('fps', 2, 9))
        self.assertEqual(args[3][:2], (INSTANCE, 42))
        self.assertEqual(ack.call_count, 1)
        self.assertIsNone(observer._retained_fault_event)
        self.assertIsNotNone(observer._host_recovery_record)
        self.assertTrue(observer._host_session_ambiguous)
        self.assertIsNone(observer._host_lease)
        # The exact (lane, identity) OpenAMS accepted is kept for a later
        # KLIPPER_MMU_RECONCILE_PATH verified clear.
        self.assertEqual(observer._accepted_firmware_fault, (2, args[3]))

    def test_follow_fault_proves_follower_stop_before_runtime_accepts(self):
        observer, device, runtime, event, _ = setup()
        follow = Event(event.instance, event.identity, event.seq,
                       event.timestamp_ms, 7, event.result_code, event.value,
                       event.flags)
        observer._retained_fault_event = follow
        runtime.get_state = lambda: SimpleNamespace(lanes={'fps': SimpleNamespace(
            unit=(5, 2), following=True)})
        order = []
        runtime.accept_verified_follower = Mock(
            side_effect=lambda *args: order.append('stop') or True)
        runtime.accept_firmware_fault.side_effect = (
            lambda *args, **kwargs: order.append('fault') or True)
        with patch('host.peek_event', return_value=follow), \
                patch('host.event_delivery.send_pending_ack', return_value=True):
            self.assertTrue(device.retry_retained_fault_ack())
        runtime.accept_verified_follower.assert_called_once_with('fps', (5, 2), False)
        self.assertEqual(order, ['stop', 'fault'])

    def test_rejected_consumer_and_ack_failure_keep_exact_event(self):
        for reject in (True, False):
            observer, device, runtime, event, _ = setup()
            if reject:
                runtime.accept_firmware_fault.return_value = False
            with patch('host.peek_event', return_value=event), \
                    patch('host.event_delivery.send_pending_ack',
                          side_effect=TimeoutError) as ack:
                with self.assertRaises((ValueError, TimeoutError)):
                    device.retry_retained_fault_ack()
            self.assertIs(observer._retained_fault_event, event)
            self.assertIsNotNone(observer._host_recovery_record)
            self.assertEqual(ack.call_count, 0 if reject else 1)
            # OpenAMS's latch is real once accept_firmware_fault returns True,
            # regardless of whether the ACK below it then fails.
            if reject:
                self.assertIsNone(getattr(observer, '_accepted_firmware_fault', None))
            else:
                self.assertIsNotNone(observer._accepted_firmware_fault)

    def test_requires_matching_capture_head_and_mapped_owner(self):
        for change in ('fault', 'session', 'owner', 'head', 'type'):
            observer, device, runtime, event, fields = setup()
            if change == 'fault':
                fields['system_fault'] = 8
            elif change == 'session':
                fields['client_sessions'] = (0, 14, 0, 0)
            elif change == 'owner':
                runtime.get_state = lambda: SimpleNamespace(lanes={})
            elif change == 'type':
                observer._retained_fault_event = Event(
                    event.instance, event.identity, event.seq,
                    event.timestamp_ms, 1, event.result_code,
                    event.value, event.flags)
            head = None if change == 'head' else event
            with patch('host.peek_event', return_value=head), \
                    patch('host.event_delivery.send_pending_ack') as ack:
                with self.assertRaises(ValueError):
                    device.retry_retained_fault_ack()
            runtime.accept_firmware_fault.assert_not_called()
            ack.assert_not_called()

    def test_global_sentinel_requires_exact_idle_fault_and_head(self):
        for change in ('none', 'fault', 'head', 'hook'):
            observer, device, runtime, event, fields = setup(client=0, lane=255)
            if change == 'fault':
                fields['motion_fault'] = 8
            if change == 'hook':
                runtime.accept_firmware_fault = None
            head = None if change == 'head' else event
            with patch('host.peek_event', return_value=head), \
                    patch('host.event_delivery.send_pending_ack',
                          return_value=True) as ack:
                if change == 'none':
                    self.assertTrue(device.retry_retained_fault_ack())
                    runtime.accept_firmware_fault.assert_called_once()
                    self.assertEqual(
                        runtime.accept_firmware_fault.call_args.args[:3],
                        (None, 255, 9))
                    ack.assert_called_once()
                else:
                    with self.assertRaises(ValueError):
                        device.retry_retained_fault_ack()
                    ack.assert_not_called()
                    self.assertIs(observer._retained_fault_event, event)

    def test_older_capture_or_existing_terminal_ack_blocks_fault(self):
        for change in ('older', 'pending'):
            observer, device, runtime, event, _ = setup()
            if change == 'older':
                observer._host_recovery_record = HostRecoveryRecord(
                    INSTANCE, 13, 8, 1, None, False, ())
            else:
                observer._host_recovery_record = HostRecoveryRecord(
                    INSTANCE, 13, 7, 1, event, True, ())
            with patch('host.peek_event') as peek:
                with self.assertRaises(ValueError):
                    device.retry_retained_fault_ack()
            peek.assert_not_called()
            runtime.accept_firmware_fault.assert_not_called()


if __name__ == '__main__':
    unittest.main()
