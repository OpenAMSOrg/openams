# SPDX-License-Identifier: GPL-3.0-or-later
"""Real runtime waiter through the managed retired recovery transaction."""
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests/klipper_openams_hook'))
sys.path.insert(0, str(ROOT / 'tests/klipper_openams_device'))
from test_runtime_hook import Printer, Oam, S as state, runtime_module
from test_managed_unit import Observer, INSTANCE
from host import HostRecoveryRecord, KlipperMMU
from host.openams_managed_unit import OpenAMSManagedUnit
from host.event_delivery import EventDelivery
from host.openams_session_bootstrap import PreparedHostSession
from host.protocol import Event, Snapshot
from host.requests import Requests, LOAD


class RetiredIntegration(unittest.TestCase):
    def test_retired_waiter_requires_disarmed_capture_and_exact_head(self):
        observer = Observer()
        observer.retry_retained_host_ack = types.MethodType(
            KlipperMMU.retry_retained_host_ack, observer)
        observer.operation_stats = types.SimpleNamespace(record_acked=lambda e: None)
        oam = Oam()
        runtime = runtime_module.Runtime(
            Printer(), ['fps'], lambda now: state.World(),
            lambda index: oam if index == 1 else None)
        waiter = runtime.request('fps', state.LoadBay('fps', (1, 0)))
        token = oam.gen
        requests = Requests(INSTANCE, 13, 41)
        identity = requests.issue(LOAD, 0, 'fps', token)
        head = Event(INSTANCE, identity, 1, 100, 1, 0, 200, 0)
        prepared = PreparedHostSession(requests, EventDelivery(INSTANCE, 3), 12)
        unit = OpenAMSManagedUnit('box', observer, 1, 'fps', 'boxturtle',
                                  4, 800., host_watchdog_ms=1000,
                                  lease_max_age_ms=400)
        # An older OpenAMS runtime lacks the retired/fault hooks.
        old = types.SimpleNamespace(**{name: getattr(runtime, name) for name in (
            'dispatch', 'accept_terminal', 'accept_reload_terminal',
            'accept_verified_follower')})
        with self.assertRaisesRegex(ValueError, 'accept_retired_terminal.*oams_runtime'):
            unit.bind_runtime(old, 'fps')
        unit.bind_runtime(runtime, 'fps')
        unit.bind_runtime(runtime, 'fps', prepared, observer._mcu,
                          observer.reactor)
        old_generation = unit._event_binding[4][0]
        old_request_generation = requests.generation
        observer._host_recovery_record = HostRecoveryRecord(
            INSTANCE, 13, old_generation, old_request_generation,
            None, False, (('fps', identity, token),))
        observer._host_session_ambiguous = True
        unit.retire_event_binding()
        observer._prepared_host_session = None
        observer._host_lease = None
        observer.physical_connection_identity = lambda: (4, INSTANCE)
        observer.owner = identity
        observer.armed = True
        observer.follower_state = 1
        observer.query = 13

        def capture():
            connection, now, status, sensors = Observer.capture_physical_state(observer)
            fields = dict(status.values, queued_events=1, head_event_seq=head.seq,
                          velocity=0, client_requests=(0, identity.request, 0, 0),
                          state=4, stage=3)
            return observer.physical_connection_identity(), now, Snapshot(INSTANCE, observer.query,
                                             fields), sensors

        observer.capture_physical_state = capture
        with patch('host.openams_managed_unit.peek_event', return_value=head):
            with self.assertRaisesRegex(ValueError, 'physical STOP'):
                unit.recover_retired_waiter_terminal(timeout=.1)
        self.assertFalse(waiter.test())
        self.assertIsNone(unit._retired_pending_ack)

        observer.armed = False
        observer.follower_state = 0
        observer.query += 1
        wrong = Event(INSTANCE, identity, 2, 101, 1, 0, 200, 0)
        with patch('host.openams_managed_unit.peek_event', return_value=wrong):
            with self.assertRaisesRegex(ValueError, 'queue head'):
                unit.recover_retired_waiter_terminal(timeout=.1)
        self.assertFalse(waiter.test())

        def fresh_capture():
            result = capture()
            observer.query += 1
            return result

        observer.capture_physical_state = fresh_capture
        with patch('host.openams_managed_unit.peek_event', return_value=head), \
             patch('host.peek_event', return_value=head), \
             patch('host.event_delivery.send_pending_ack',
                   side_effect=TimeoutError('uncertain ACK')):
            with self.assertRaisesRegex(TimeoutError, 'uncertain ACK'):
                unit.recover_retired_waiter_terminal(timeout=.1)
        self.assertTrue(waiter.test())
        self.assertTrue(waiter.wait().ok)
        self.assertEqual(runtime.system.lanes['fps'].op, state.OP_LOADED)
        self.assertEqual(unit._retired_pending_ack, head)
        self.assertTrue(unit._retired_pending_ack_eligible)
        self.assertEqual(requests.pending, {})
        with patch('host.openams_managed_unit.peek_event', return_value=head):
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                unit.recover_retired_waiter_terminal(timeout=.1)
        with patch('host.peek_event', return_value=wrong):
            with self.assertRaisesRegex(ValueError, 'head changed'):
                unit.retry_retired_ack(timeout=.1)
        self.assertEqual(unit._retired_pending_ack, head)
        with patch('host.peek_event', return_value=head), \
             patch('host.event_delivery.send_pending_ack') as send:
            self.assertTrue(unit.retry_retired_ack(timeout=.1))
            send.assert_called_once()
        self.assertIsNone(unit._retired_pending_ack)
        self.assertIsNone(observer.host_recovery_record().pending_ack)


if __name__ == '__main__':
    unittest.main()
