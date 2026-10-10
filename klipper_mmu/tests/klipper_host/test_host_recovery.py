# SPDX-License-Identifier: GPL-3.0-or-later
import threading
import dataclasses
import types
import unittest
from unittest.mock import patch
from unittest.mock import Mock

from host import KlipperMMU
from host.event_delivery import EventDelivery
from host.openams_host_lease import HostLease
from host.openams_managed_unit import OpenAMSManagedUnit
from host.operation_stats import OperationStats
from host.openams_session_bootstrap import PreparedHostSession
from host.protocol import CommandResult, Event, Identity, SensorSample, SensorSnapshot, Snapshot
from host.requests import Requests
from host.wire import Command, CommandCode, EventType, OpCode, OpType


INSTANCE = 0x1020304050607080


class Reactor:
    def monotonic(self):
        return 1.0


class Printer:
    reactor = Reactor()

    def get_reactor(self):
        return self.reactor


def observer():
    mmu = object.__new__(KlipperMMU)
    mmu._generation, mmu._instance = 7, INSTANCE
    mmu._connected = mmu._active = True
    mmu._mcu_lock = threading.Lock()
    mmu._state = 'attached'
    mmu._boot_transition = None
    mmu._last_query = 77
    mmu._snapshot_query = 77
    mmu._challenge = 0
    mmu._last_attached_instance = INSTANCE
    mmu._startup_state = 'ready'
    mmu._startup_result = 0
    mmu._snapshot = None
    mmu._transport_query = None
    mmu._startup_query = None
    mmu._last_error = None
    mmu._host_lease = None
    mmu._printer = Printer()
    mmu._mcu = object()
    mmu._firmware_actuation = 1
    mmu._diagnostic_startup = True
    mmu._host_session_ambiguous = False
    mmu._host_recovery_record = None
    mmu.operation_stats = OperationStats()
    mmu._prepared_host_session = PreparedHostSession(
        Requests(INSTANCE, 13), EventDelivery(INSTANCE, 7), 77)
    return mmu


def recovered_capture(mmu, **changes):
    values = dict(state=0, stage=0, operation=0, client=0,
                  queued_events=0, head_event_seq=0, system_fault=0,
                  motion_fault=0, follower_fault=0, motor_ready=True,
                  host_connected=True, velocity=0, lane_active=False,
                  terminal_pending=False, path_occupied=False,
                  position_uncertain=False, pending_fault_event=False,
                  owner_lane=255, occupant_lane=255, acquired_ms=100,
                  client_sessions=(0, 13, 0, 0),
                  client_requests=(0, 0, 0, 0))
    values.update(changes)
    clear = SensorSample(True, False, True, True, 100)
    return ((mmu._generation, mmu._instance), 1.0,
            Snapshot(mmu._instance, 78, values),
            SensorSnapshot(mmu._instance, 78, 2, 100,
                           (clear, clear), clear, clear))


def repeated_capture(mmu, **changes):
    query = [78]
    def capture():
        result = recovered_capture(mmu, **changes)
        current = query[0]
        query[0] += 1
        return (result[0], result[1],
                dataclasses.replace(result[2], query=current),
                dataclasses.replace(result[3], query=current))
    return capture


def preloaded_recovery_capture(mmu, **changes):
    identity, started, status, sensors = recovered_capture(
        mmu, state=8, path_occupied=True, occupant_lane=1,
        session=0, request=0, lane=255)
    present = dataclasses.replace(sensors.bays[1], present=True)
    sensors = dataclasses.replace(
        sensors, bays=(sensors.bays[0], present),
        loads=(sensors.bays[0], present))
    status = dataclasses.replace(status, values=dict(status.values, **changes))
    return identity, started, status, sensors


def restore_attached(mmu):
    mmu._connected = mmu._active = True
    mmu._state = 'attached'
    mmu._instance = INSTANCE
    mmu._startup_state = 'ready'
    mmu._startup_result = 0


class ServiceReactor:
    """One controllable instant for a unit whose service cycle is driven by hand."""

    NEVER = 1e30

    def __init__(self):
        self.now = 1.0

    def monotonic(self):
        return self.now

    def register_timer(self, callback, when):
        return None


class FaultedRuntime:
    """The runtime hooks a bound OpenAMS unit requires, without any motion."""

    def __init__(self):
        self.terminals = []

    def dispatch(self, *args, **kwargs):
        return None

    def accept_terminal(self, fps, code, value, generation):
        self.terminals.append((fps, code, value, generation))
        return True

    def accept_reload_terminal(self, *args, **kwargs):
        return True

    def accept_verified_follower(self, *args, **kwargs):
        return True

    def accept_retired_terminal(self, *args, **kwargs):
        return True

    def accept_firmware_fault(self, *args, **kwargs):
        return True


class HostRecoveryTest(unittest.TestCase):
    def test_repeated_recovery_retains_first_record_without_re_disconnect(self):
        mmu = observer()
        requests = mmu._prepared_host_session.requests
        delivery = mmu._prepared_host_session.delivery
        mmu._retain_host_recovery()
        record = mmu.host_recovery_record()
        generations = (requests.generation, delivery.generation)
        mmu._retain_host_recovery()
        self.assertIs(mmu.host_recovery_record(), record)
        self.assertEqual((requests.generation, delivery.generation), generations)

    def test_retired_waiter_settles_before_exact_ack(self):
        for operation, event_type in ((1, 1), (2, 5), (4, 8)):
            with self.subTest(operation=operation):
                mmu = observer()
                requests = mmu._prepared_host_session.requests
                identity = requests.issue(operation, 1, 'fps', 4)
                mmu._disconnect()
                restore_attached(mmu)
                mmu._generation += 1
                path = (dict(path_occupied=True, occupant_lane=1,
                             lane_active=True, state=4, stage=3,
                             operation=1, client=1, owner_lane=1)
                        if operation == 1 else
                        dict(path_occupied=True, occupant_lane=1, state=2)
                        if operation == 2 else {})
                mmu.capture_physical_state = repeated_capture(
                    mmu, queued_events=1, head_event_seq=9,
                    follower_state=0, follower_fault=0, velocity=0,
                    session=13, request=identity.request, lane=1,
                    client_requests=(0, identity.request, 0, 0), **path)
                unit = object.__new__(OpenAMSManagedUnit)
                unit._observer = mmu
                unit._state = types.SimpleNamespace(bay_count=2, oams_idx=0,
                                                    fps_name='fps')
                unit._event_binding = None
                unit._retired_requests = requests
                unit._retired_pending_ack = None
                unit._retired_pending_ack_eligible = False
                unit._runtime = types.SimpleNamespace(
                    accept_retired_terminal=Mock(return_value=True))
                event = Event(INSTANCE, identity, 9, 100, event_type, 0, 42, 0)
                original = mmu.host_recovery_record()
                with patch('host.openams_managed_unit.peek_event', return_value=event), \
                        patch('host.peek_event', return_value=event), \
                        patch('host.event_delivery.send_pending_ack',
                              side_effect=ValueError('ACK timed out')):
                    with self.assertRaisesRegex(ValueError, 'timed out'):
                        unit.recover_retired_waiter_terminal()
                unit._runtime.accept_retired_terminal.assert_called_once_with(
                    'fps', (0, 1), 4, 6 if operation == 2 else 0, 42,
                    ((0, 1), 4, True))
                self.assertNotIn('fps', requests.pending)
                self.assertIs(mmu.host_recovery_record().pending_ack, event)
                self.assertEqual(mmu.host_recovery_record().pending_requests, ())
                self.assertIsNot(mmu.host_recovery_record(), original)

    def test_retired_waiter_requires_physical_stop_before_runtime(self):
        mmu = observer()
        requests = mmu._prepared_host_session.requests
        identity = requests.issue(1, 1, 'fps', 4)
        mmu._disconnect()
        restore_attached(mmu)
        mmu._generation += 1
        mmu.capture_physical_state = repeated_capture(
            mmu, queued_events=1, head_event_seq=9, follower_state=1,
            follower_fault=0, velocity=0,
            session=13, request=identity.request, lane=1,
            client_requests=(0, identity.request, 0, 0))
        unit = object.__new__(OpenAMSManagedUnit)
        unit._observer = mmu
        unit._state = types.SimpleNamespace(bay_count=2, oams_idx=0,
                                            fps_name='fps')
        unit._event_binding = None
        unit._retired_requests = requests
        unit._retired_pending_ack = None
        unit._retired_pending_ack_eligible = False
        unit._runtime = types.SimpleNamespace(
            accept_retired_terminal=Mock(return_value=True))
        original = mmu.host_recovery_record()
        with patch('host.openams_managed_unit.peek_event') as peek:
            with self.assertRaisesRegex(ValueError, 'STOP'):
                unit.recover_retired_waiter_terminal()
            peek.assert_not_called()
        unit._runtime.accept_retired_terminal.assert_not_called()
        self.assertIs(mmu.host_recovery_record(), original)
        self.assertEqual(requests.pending['fps'], (identity, 4))

    def test_retired_waiter_outcome_requires_matching_path_occupancy(self):
        cases = ((1, 1, False), (1, 2, True), (1, 3, True),
                 (2, 4, True), (2, 5, False), (2, 6, False),
                 (4, 8, True), (4, 9, True), (4, 10, True))
        for operation, event_type, occupied in cases:
            with self.subTest(operation=operation, event_type=event_type):
                mmu = observer()
                requests = mmu._prepared_host_session.requests
                identity = requests.issue(operation, 1, 'fps', 4)
                mmu._disconnect()
                restore_attached(mmu)
                mmu._generation += 1
                mmu.capture_physical_state = repeated_capture(
                    mmu, queued_events=1, head_event_seq=9,
                    follower_state=0, follower_fault=0, velocity=0,
                    session=13, request=identity.request, lane=1,
                    state=2 if occupied else 0,
                    path_occupied=occupied,
                    occupant_lane=1 if occupied else 255,
                    client_requests=(0, identity.request, 0, 0))
                unit = object.__new__(OpenAMSManagedUnit)
                unit._observer = mmu
                unit._state = types.SimpleNamespace(bay_count=2, oams_idx=0,
                                                    fps_name='fps')
                unit._event_binding = None
                unit._retired_requests = requests
                unit._retired_pending_ack = None
                unit._retired_pending_ack_eligible = False
                unit._runtime = types.SimpleNamespace(
                    accept_retired_terminal=Mock(return_value=True))
                event = Event(INSTANCE, identity, 9, 100, event_type,
                              0 if event_type in (1, 4, 8) else 6, 42, 0)
                original = mmu.host_recovery_record()
                with patch('host.openams_managed_unit.peek_event', return_value=event):
                    with self.assertRaisesRegex(ValueError,
                                                'contradicts physical path'):
                        unit.recover_retired_waiter_terminal()
                unit._runtime.accept_retired_terminal.assert_not_called()
                self.assertIs(mmu.host_recovery_record(), original)
                self.assertEqual(requests.pending['fps'], (identity, 4))

    def test_retired_waiter_occupied_load_and_unload_owner_checks(self):
        for operation, event_type, changes in (
                (1, 1, dict(path_occupied=True, lane_active=True,
                            state=4, stage=3, operation=1, client=1,
                            session=13, owner_lane=1, occupant_lane=1,
                            lane=1)),
                (2, 6, dict(path_occupied=True, lane_active=False,
                            state=2, stage=0, operation=0, client=0,
                            occupant_lane=1))):
            with self.subTest(operation=operation):
                mmu = observer()
                requests = mmu._prepared_host_session.requests
                identity = requests.issue(operation, 1, 'fps', 4)
                mmu._disconnect()
                restore_attached(mmu)
                mmu._generation += 1
                status = dict(changes, queued_events=1, head_event_seq=9,
                              follower_state=0, follower_fault=0, velocity=0,
                              session=13, request=identity.request, lane=1,
                              client_requests=(0, identity.request, 0, 0))
                mmu.capture_physical_state = repeated_capture(mmu, **status)
                unit = object.__new__(OpenAMSManagedUnit)
                unit._observer = mmu
                unit._state = types.SimpleNamespace(bay_count=2, oams_idx=0,
                                                    fps_name='fps')
                unit._event_binding = None
                unit._retired_requests = requests
                unit._retired_pending_ack = None
                unit._retired_pending_ack_eligible = False
                unit._runtime = types.SimpleNamespace(
                    accept_retired_terminal=Mock(return_value=True))
                event = Event(INSTANCE, identity, 9, 100, event_type, 0, 0, 0)
                with patch('host.openams_managed_unit.peek_event', return_value=event), \
                        patch.object(unit, 'retry_retired_ack', return_value=True):
                    self.assertTrue(unit.recover_retired_waiter_terminal())
                unit._runtime.accept_retired_terminal.assert_called_once()

                if operation == 1:
                    for wrong_type in (2, 3):
                        with self.subTest(wrong_type=wrong_type):
                            fresh = observer()
                            pending = fresh._prepared_host_session.requests
                            same = pending.issue(1, 1, 'fps', 4)
                            fresh._disconnect()
                            restore_attached(fresh)
                            fresh._generation += 1
                            fresh.capture_physical_state = repeated_capture(
                                fresh, **status)
                            unit._observer = fresh
                            unit._retired_requests = pending
                            unit._retired_pending_ack = None
                            unit._retired_pending_ack_eligible = False
                            unit._runtime.accept_retired_terminal.reset_mock()
                            wrong = Event(INSTANCE, same, 9, 100,
                                          wrong_type, 6, 0, 0)
                            with patch('host.openams_managed_unit.peek_event',
                                       return_value=wrong):
                                with self.assertRaisesRegex(ValueError,
                                                            'contradicts physical path'):
                                    unit.recover_retired_waiter_terminal()
                            unit._runtime.accept_retired_terminal.assert_not_called()
                            self.assertEqual(pending.pending['fps'], (same, 4))

                # A different physical owner or an unknown occupied bay cannot
                # prove this old request, even if the event identity matches.
                for changed in (dict(occupant_lane=0),
                                # Even a zero-velocity UNLOADING report must
                                # not settle or ACK a retired terminal.
                                dict(state=3, stage=0, lane_active=False,
                                     operation=0, client=0),
                                dict(state=2, stage=1, lane_active=True)):
                    with self.subTest(changed=changed):
                        mmu = observer()
                        requests = mmu._prepared_host_session.requests
                        identity = requests.issue(operation, 1, 'fps', 4)
                        mmu._disconnect()
                        restore_attached(mmu)
                        mmu._generation += 1
                        status2 = dict(status, **changed)
                        mmu.capture_physical_state = repeated_capture(mmu, **status2)
                        unit._observer = mmu
                        unit._retired_requests = requests
                        unit._retired_pending_ack = None
                        unit._retired_pending_ack_eligible = False
                        unit._runtime.accept_retired_terminal.reset_mock()
                        with patch('host.openams_managed_unit.peek_event') as peek:
                            with self.assertRaises(ValueError):
                                unit.recover_retired_waiter_terminal()
                            peek.assert_not_called()
                        unit._runtime.accept_retired_terminal.assert_not_called()

    def test_retired_standalone_terminal_commits_before_uncertain_ack(self):
        for name in ('PRELOAD', 'UNPRELOAD', 'EJECT', 'HOME'):
            with self.subTest(name=name):
                mmu = observer()
                requests = mmu._prepared_host_session.requests
                target = (requests.issue_preload(1) if name == 'PRELOAD'
                          else requests.issue_eject(1) if name == 'EJECT'
                          else requests.issue_home() if name == 'HOME'
                          else requests.issue_unpreload(1))
                mmu._disconnect()
                restore_attached(mmu)
                mmu._generation += 1
                mmu.capture_physical_state = lambda: recovered_capture(
                    mmu, queued_events=1, head_event_seq=9)
                unit = object.__new__(OpenAMSManagedUnit)
                unit._observer = mmu
                unit._state = types.SimpleNamespace(bay_count=2)
                unit._event_binding = None
                unit._retired_requests = requests
                unit._retired_pending_ack = None
                unit._retired_pending_ack_eligible = False
                event = Event(INSTANCE, target, 9, 100,
                              {'PRELOAD': 14, 'UNPRELOAD': 5, 'EJECT': 17, 'HOME': 20}[name],
                              6, 0, 0)
                pending = 'pending_' + name.lower()
                terminal = name.lower() + '_terminal'
                original = mmu.host_recovery_record()
                with patch('host.openams_managed_unit.peek_event', return_value=
                           dataclasses.replace(event, seq=10)):
                    with self.assertRaisesRegex(ValueError, 'head changed'):
                        unit.recover_retired_local_terminal()
                self.assertIs(mmu.host_recovery_record(), original)
                self.assertEqual(getattr(requests, pending), target)
                with patch('host.openams_managed_unit.peek_event', return_value=
                           dataclasses.replace(event, identity=dataclasses.replace(
                               target, request=target.request + 1))):
                    with self.assertRaisesRegex(ValueError, 'head changed'):
                        unit.recover_retired_local_terminal()
                with patch('host.openams_managed_unit.peek_event', return_value=
                           dataclasses.replace(event, result_code=8)):
                    with self.assertRaisesRegex(ValueError, 'Unsupported'):
                        unit.recover_retired_local_terminal()
                self.assertIs(mmu.host_recovery_record(), original)
                self.assertEqual(getattr(requests, pending), target)
                with patch('host.openams_managed_unit.peek_event', return_value=event), \
                        patch('host.peek_event', return_value=event), \
                        patch('host.event_delivery.send_pending_ack',
                              side_effect=ValueError('ACK timed out')):
                    with self.assertRaisesRegex(ValueError, 'timed out'):
                        unit.recover_retired_local_terminal()
                committed = mmu.host_recovery_record()
                self.assertIs(committed.pending_ack, event)
                self.assertTrue(committed.pending_ack_eligible)
                self.assertEqual(committed.pending_requests, ())
                self.assertIs(unit._retired_pending_ack, event)
                self.assertIsNone(getattr(requests, pending))
                self.assertIs(getattr(requests, terminal), event)
                self.assertTrue(mmu._host_session_ambiguous)
                with patch('host.peek_event', return_value=event), \
                        patch('host.event_delivery.send_pending_ack', return_value=True):
                    self.assertTrue(unit.retry_retired_ack())
                self.assertIsNone(mmu.host_recovery_record().pending_ack)
                self.assertIsNone(unit._retired_pending_ack)
                self.assertTrue(mmu._host_session_ambiguous)
                self.assertIsNone(mmu._host_lease)
                self.assertIsNone(mmu._prepared_host_session)

    def test_retired_local_recovery_rejects_changed_session_stale_generation_and_work(self):
        for case in ('changed session', 'stale generation',
                     'OpenAMS waiter', 'pending ACK'):
            with self.subTest(case=case):
                mmu = observer()
                requests = mmu._prepared_host_session.requests
                target = requests.issue_preload(1)
                if case == 'OpenAMS waiter':
                    requests.issue(1, 0, 'fps', 4)
                if case == 'pending ACK':
                    delivery = mmu._prepared_host_session.delivery
                    delivery.pending = Event(INSTANCE, Identity(1, 13, 1, 1, 0),
                                             8, 100, 1, 0, 0, 0)
                    delivery.ack_eligible = True
                mmu._disconnect()
                restore_attached(mmu)
                if case == 'stale generation':
                    mmu._generation = mmu.host_recovery_record().generation
                else:
                    mmu._generation += 1
                session = 14 if case == 'changed session' else 13
                captured = recovered_capture(
                    mmu, client_sessions=(0, session, 0, 0),
                    queued_events=1, head_event_seq=9)
                if case == 'stale generation':
                    captured = (captured[0], captured[1],
                                dataclasses.replace(captured[2], query=77),
                                dataclasses.replace(captured[3], query=77))
                mmu.capture_physical_state = lambda: captured
                unit = object.__new__(OpenAMSManagedUnit)
                unit._observer = mmu
                unit._state = types.SimpleNamespace(bay_count=2)
                unit._event_binding = None
                unit._retired_requests = requests
                unit._retired_pending_ack = mmu.host_recovery_record().pending_ack
                unit._retired_pending_ack_eligible = (
                    mmu.host_recovery_record().pending_ack_eligible)
                original = mmu.host_recovery_record()
                with patch('host.openams_managed_unit.peek_event') as peek:
                    with self.assertRaises(ValueError):
                        unit.recover_retired_local_terminal()
                    peek.assert_not_called()
                self.assertIs(mmu.host_recovery_record(), original)
                self.assertEqual(requests.pending_preload, target)
                self.assertIsNone(requests.preload_terminal)
                self.assertTrue(mmu._host_session_ambiguous)

    def test_same_connection_local_recovery_retires_the_stale_event_binding(self):
        # A host-comm-lost fault (or any lease loss with the serial link
        # otherwise intact) never runs _invalidate()/_fail(), so nothing ever
        # retired this unit's own event binding or cleared
        # _prepared_host_session on its own; RECOVER_LOCAL/RECOVER_WAITER
        # used to additionally require _prepared_host_session already None,
        # so an in-flight local PRELOAD stranded exactly this way (not by a
        # dropped connection) could never be reconciled at all.
        mmu = observer()
        requests = mmu._prepared_host_session.requests
        delivery = mmu._prepared_host_session.delivery
        target = requests.issue_preload(1)
        unit = object.__new__(OpenAMSManagedUnit)
        unit._observer = mmu
        unit._state = types.SimpleNamespace(bay_count=2)
        unit._event_binding = (object(), mmu._prepared_host_session, object(),
                               object(), (delivery.generation, delivery.instance),
                               requests.generation)
        unit._retired_requests = None
        unit._retired_pending_ack = None
        unit._retired_pending_ack_eligible = False
        mmu._openams_unit = unit

        mmu._retain_host_recovery()  # A firmware fault revoked the lease.
        self.assertIsNone(unit._event_binding)
        self.assertIs(unit._retired_requests, requests)
        self.assertIsNotNone(mmu._prepared_host_session)
        self.assertFalse(requests.connected)
        record = mmu.host_recovery_record()
        self.assertEqual(record.pending_requests, (('PRELOAD', target, None),))

        mmu.capture_physical_state = lambda: recovered_capture(
            mmu, queued_events=1, head_event_seq=9)
        event = Event(INSTANCE, target, 9, 100, 14, 6, 0, 0)
        with patch('host.openams_managed_unit.peek_event', return_value=event), \
                patch('host.peek_event', return_value=event), \
                patch('host.event_delivery.send_pending_ack', return_value=True):
            self.assertTrue(unit.recover_retired_local_terminal())
        self.assertIsNone(mmu.host_recovery_record().pending_ack)
        self.assertIsNone(requests.pending_preload)
        self.assertIs(requests.preload_terminal, event)
        self.assertTrue(mmu._host_session_ambiguous)
        self.assertIsNone(mmu._host_lease)
        self.assertIsNotNone(mmu._prepared_host_session)

        # RECOVER_SESSION now has no leftover work to block it, even though
        # _prepared_host_session was never nulled by a connection invalidate.
        mmu.capture_physical_state = lambda: recovered_capture(
            mmu, host_connected=False)
        selected = mmu.reconcile_clean_host_recovery(2)
        self.assertEqual(selected.session, 14)
        self.assertFalse(mmu._host_session_ambiguous)

    def test_stats_command_labels_process_scope(self):
        mmu = observer()
        mmu._name = 'unit'
        replies = []
        command = type('Command', (), {'respond_info': lambda self, text: replies.append(text)})()
        mmu.cmd_KLIPPER_MMU_STATS(command)
        self.assertEqual(replies, [
            'klipper_mmu unit stats scope=process_observed_confirmed_ack empty=true'])

    def test_explicit_ack_retry_requires_exact_head_and_keeps_other_work(self):
        mmu = observer()
        requests = mmu._prepared_host_session.requests
        identity = requests.issue(1, 2, 'fps', 4)
        event = Event(INSTANCE, identity, 9, 100, 1, 0, 200, 0)
        mmu._prepared_host_session.delivery.pending = event
        mmu._prepared_host_session.delivery.ack_eligible = True
        mmu._disconnect()
        restore_attached(mmu)
        mmu._generation += 1
        mmu.capture_physical_state = lambda: recovered_capture(mmu)
        original = mmu.host_recovery_record()
        with patch('host.peek_event', return_value=dataclasses.replace(
                event, value=201)), patch('host.event_delivery.send_pending_ack') as ack:
            with self.assertRaisesRegex(ValueError, 'head changed'):
                mmu.retry_retained_host_ack()
            ack.assert_not_called()
        self.assertIs(mmu.host_recovery_record(), original)

        with patch('host.peek_event', return_value=event), patch(
                'host.event_delivery.send_pending_ack',
                side_effect=ValueError('ACK timed out')):
            with self.assertRaisesRegex(ValueError, 'timed out'):
                mmu.retry_retained_host_ack()
        self.assertIs(mmu.host_recovery_record(), original)

        def confirmed(mcu, reactor, delivery, generation, current, timeout):
            self.assertTrue(current())
            self.assertTrue(mmu._mcu_lock.locked())
            self.assertEqual(delivery.pending, event)
            self.assertTrue(delivery.ack_eligible)
            return True

        with patch('host.peek_event', return_value=event), patch(
                'host.event_delivery.send_pending_ack', side_effect=confirmed):
            self.assertTrue(mmu.retry_retained_host_ack())
        recovered = mmu.host_recovery_record()
        self.assertIsNone(recovered.pending_ack)
        self.assertFalse(recovered.pending_ack_eligible)
        self.assertEqual(recovered.pending_requests, original.pending_requests)
        self.assertTrue(mmu._host_session_ambiguous)
        self.assertEqual(mmu.operation_stats.snapshot(), {('load', 2, 'success'): 1})

    def test_explicit_ack_retry_rejects_ineligible_or_foreign_session(self):
        mmu = observer()
        event = Event(INSTANCE, Identity(1, 13, 1, 1, 0), 9, 100, 1, 0, 200, 0)
        mmu._prepared_host_session.delivery.pending = event
        mmu._disconnect()
        restore_attached(mmu)
        mmu._generation += 1
        mmu.capture_physical_state = lambda: recovered_capture(mmu)
        with patch('host.peek_event') as peek:
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                mmu.retry_retained_host_ack()
            peek.assert_not_called()
        mmu._host_recovery_record = dataclasses.replace(
            mmu.host_recovery_record(), pending_ack_eligible=True)
        original = mmu.host_recovery_record()
        for timeout in (float('nan'), float('inf')):
            with self.subTest(timeout=timeout):
                with self.assertRaisesRegex(ValueError, 'unavailable'):
                    mmu.retry_retained_host_ack(timeout=timeout)
        mmu.capture_physical_state = lambda: recovered_capture(
            mmu, client_sessions=(0, 14, 0, 0))
        with patch('host.peek_event') as peek:
            with self.assertRaisesRegex(ValueError, 'session changed'):
                mmu.retry_retained_host_ack()
            peek.assert_not_called()
        self.assertIs(mmu.host_recovery_record(), original)

    def test_clean_old_session_requires_explicit_fresh_reconciliation(self):
        mmu = observer()
        mmu._disconnect()
        self.assertTrue(mmu._host_session_ambiguous)
        restore_attached(mmu)
        mmu.capture_physical_state = lambda: recovered_capture(mmu)
        with patch('host.openams_session_bootstrap.send_heartbeat') as beat:
            selected = mmu.reconcile_clean_host_recovery(2)
            beat.assert_not_called()
        self.assertEqual(selected.session, 14)
        self.assertIsNone(mmu.host_recovery_record())
        self.assertFalse(mmu._host_session_ambiguous)

    def test_same_connection_fault_releases_retired_session_for_adopted_lane(self):
        mmu = observer()
        mmu._retain_host_recovery()  # A firmware fault revoked the lease.
        self.assertIsNotNone(mmu._prepared_host_session)
        identity, started, status, sensors = recovered_capture(
            mmu, state=2, path_occupied=True, occupant_lane=1, session=0,
            request=0, lane=255)
        present = dataclasses.replace(sensors.bays[1], present=True)
        mmu.capture_physical_state = lambda: (identity, started, status, dataclasses.replace(
            sensors, bays=(sensors.bays[0], present), hub=present))
        selected = mmu.reconcile_clean_host_recovery(2)
        self.assertEqual((selected.session_start.session, selected.lane), (14, 1))
        self.assertIsNone(mmu._prepared_host_session)
        self.assertFalse(mmu._host_session_ambiguous)

    def test_same_connection_host_comm_lost_recovers_without_a_live_heartbeat(self):
        # A HOST_COMM_LOST fault (src/operation_core.c km_core_host_comm_lost,
        # tripped by mcu/commands.c's command_km_heartbeat watchdog check)
        # leaves host_connected=False on the firmware side, and only a fresh
        # session's own first heartbeat -- sent later, once _activate() runs
        # again -- ever sets it back True. RECOVER_SESSION itself sends no
        # heartbeat (docs/path-reconciliation.md), so requiring
        # host_connected already True here made this exact retained fault
        # permanently unrecoverable: RECONCILE_PATH clears the fault flags,
        # but nothing was ever able to clear this one first.
        mmu = observer()
        mmu._retain_host_recovery()  # A firmware fault revoked the lease.
        mmu.capture_physical_state = lambda: recovered_capture(
            mmu, host_connected=False)
        selected = mmu.reconcile_clean_host_recovery(2)
        self.assertEqual(selected.session, 14)
        self.assertIsNone(mmu.host_recovery_record())
        self.assertFalse(mmu._host_session_ambiguous)

    def test_clean_preloaded_old_session_requires_physical_lane_proof(self):
        mmu = observer()
        mmu._disconnect()
        restore_attached(mmu)
        mmu.capture_physical_state = lambda: preloaded_recovery_capture(mmu)
        with patch('host.openams_session_bootstrap.send_heartbeat') as beat:
            selected = mmu.reconcile_clean_host_recovery(2)
            beat.assert_not_called()
        self.assertEqual(selected.session_start.session, 14)
        self.assertEqual(selected.lane, 1)
        self.assertIsNone(mmu.host_recovery_record())
        self.assertFalse(mmu._host_session_ambiguous)

    def test_clean_preloaded_recovery_rejects_unproven_state(self):
        for change in (dict(head_event_seq=9), dict(queued_events=1),
                       dict(client_sessions=(0, 14, 0, 0)),
                       dict(occupant_lane=0), dict(state=2)):
            with self.subTest(change=change):
                mmu = observer()
                mmu._disconnect()
                restore_attached(mmu)
                mmu.capture_physical_state = lambda: preloaded_recovery_capture(
                    mmu, **change)
                with self.assertRaises(ValueError):
                    mmu.reconcile_clean_host_recovery(2)
                self.assertIsNotNone(mmu.host_recovery_record())
        for sensor_change in (
                lambda s: dataclasses.replace(s, loads=(s.loads[0],
                    dataclasses.replace(s.loads[1], configured=False))),
                lambda s: dataclasses.replace(s, hub=dataclasses.replace(
                    s.hub, present=True)),
                lambda s: dataclasses.replace(s, bays=(s.bays[0],
                    dataclasses.replace(s.bays[1], valid=False))),
                lambda s: dataclasses.replace(s, query=s.query + 1)):
            mmu = observer()
            mmu._disconnect()
            restore_attached(mmu)
            capture = preloaded_recovery_capture(mmu)
            mmu.capture_physical_state = lambda: (
                *capture[:3], sensor_change(capture[3]))
            with self.assertRaises(ValueError):
                mmu.reconcile_clean_host_recovery(2)
            self.assertIsNotNone(mmu.host_recovery_record())

    def test_recovery_rejects_pending_work_and_changed_board(self):
        for pending in ('ack', 'waiter'):
            with self.subTest(pending=pending):
                mmu = observer()
                if pending == 'ack':
                    mmu._prepared_host_session.delivery.pending = Event(
                        INSTANCE, Identity(2, 13, 1, 1, 0), 9, 100, 1, 0,
                        200, 0)
                else:
                    mmu._prepared_host_session.requests.issue(
                        1, 2, 'fps', 4)
                mmu._disconnect()
                restore_attached(mmu)
                mmu.capture_physical_state = lambda: recovered_capture(mmu)
                with self.assertRaisesRegex(ValueError, 'unavailable'):
                    mmu.reconcile_clean_host_recovery(2)
                self.assertTrue(mmu._host_session_ambiguous)
                self.assertIsNotNone(mmu.host_recovery_record())

        for change in ({'queued_events': 1}, {'path_occupied': True},
                       {'motor_ready': False},
                       {'system_fault': 1}, {'motion_fault': 1},
                       {'follower_fault': 1}, {'position_uncertain': True},
                       {'pending_fault_event': True},
                       {'client_sessions': (0, 12, 0, 0)},
                       {'client_sessions': (0, 14, 0, 0)}):
            with self.subTest(change=change):
                mmu = observer()
                mmu._disconnect()
                restore_attached(mmu)
                mmu.capture_physical_state = lambda: recovered_capture(
                    mmu, **change)
                with self.assertRaises(ValueError):
                    mmu.reconcile_clean_host_recovery(2)
                self.assertTrue(mmu._host_session_ambiguous)
                self.assertIsNotNone(mmu.host_recovery_record())

    def test_recovery_rejects_stale_capture_or_new_instance(self):
        mmu = observer()
        mmu._fail(7, 'query failed')
        restore_attached(mmu)
        stale = recovered_capture(mmu)
        mmu.capture_physical_state = lambda: (
            stale[0], stale[1], dataclasses.replace(stale[2], query=77),
            dataclasses.replace(stale[3], query=77))
        with self.assertRaisesRegex(ValueError, 'connection changed'):
            mmu.reconcile_clean_host_recovery(2)
        mmu.capture_physical_state = lambda: (
            (mmu._generation, mmu._instance), 1.0,
            dataclasses.replace(recovered_capture(mmu)[2], query=79),
            dataclasses.replace(recovered_capture(mmu)[3], query=79))
        mmu._instance += 1
        with self.assertRaisesRegex(ValueError, 'connection changed'):
            mmu.reconcile_clean_host_recovery(2)
        self.assertTrue(mmu._host_session_ambiguous)

    def test_same_connection_recovery_after_real_observation(self):
        mmu = observer()
        mmu._firmware_composition = (1, 2)
        mmu._snapshot_query = 40
        # A transient query failure retains a recovery record but does not
        # disconnect Klipper's MCU object or advance its connection generation.
        mmu._fail(mmu._generation, 'transient query failure')
        record = mmu.host_recovery_record()
        self.assertEqual(record.generation, mmu._generation)
        self.assertEqual(record.query_highwater, 77)
        self.assertEqual(mmu._firmware_composition, (1, 2))

        class Query:
            def __init__(self, name):
                self.name = name

            def send(self, args):
                challenge = args[0]
                if self.name == 'km_transport_query':
                    return dict(challenge=challenge,
                                instance_hi=INSTANCE >> 32,
                                instance_lo=INSTANCE & 0xffffffff,
                                last_query=77, state=1)
                return dict(challenge=challenge, state=2, result=0)

        mmu._mcu = types.SimpleNamespace(
            lookup_query_command=lambda fmt, response: Query(fmt.split()[0]))
        mmu._printer.command_error = ValueError
        mmu._observe()
        self.assertEqual(mmu.physical_connection_identity(), (7, INSTANCE))
        unit = object.__new__(OpenAMSManagedUnit)
        unit._state = types.SimpleNamespace(bay_count=2, device_family='boxturtle')
        mmu._openams_unit = unit
        capture = recovered_capture(mmu)
        mmu.capture_physical_state = lambda: (
            capture[0], capture[1],
            dataclasses.replace(capture[2], query=78),
            dataclasses.replace(capture[3], query=78))
        command = types.SimpleNamespace(respond_info=Mock())
        mmu.cmd_KLIPPER_MMU_RECOVER_SESSION(command)
        self.assertIsNone(mmu.host_recovery_record())
        self.assertFalse(mmu._host_session_ambiguous)
        self.assertIn('motion remains revoked',
                      command.respond_info.call_args.args[0])
        # Recovery must also permit the next explicitly prepared session;
        # merely discarding its old ambiguity record is insufficient.
        mmu.capture_physical_state = repeated_capture(mmu)
        with patch('host.openams_session_bootstrap.send_heartbeat',
                   return_value=CommandResult(INSTANCE, 3, 14, 0)) as beat:
            prepared = mmu.prepare_cold_host_session(2)
        beat.assert_called_once()
        self.assertEqual(prepared.requests.session, 14)
        self.assertIsNone(mmu._host_lease)
        mmu._disconnect()
        self.assertIsNone(mmu._firmware_composition)

    def test_recovery_rejects_connection_change_during_capture(self):
        mmu = observer()
        mmu._disconnect()
        restore_attached(mmu)

        def changed():
            capture = recovered_capture(mmu)
            mmu._generation += 1
            return capture

        mmu.capture_physical_state = changed
        with self.assertRaisesRegex(ValueError, 'connection changed'):
            mmu.reconcile_clean_host_recovery(2)
        self.assertTrue(mmu._host_session_ambiguous)
        self.assertIsNotNone(mmu.host_recovery_record())

    def test_disconnect_preserves_exact_pending_ack_and_waiter(self):
        mmu = observer()
        requests = mmu._prepared_host_session.requests
        delivery = mmu._prepared_host_session.delivery
        identity = requests.issue(1, 2, 'fps', 4)
        pending = Event(INSTANCE, identity, 9, 100, 1, 0, 200, 0)
        delivery.pending = pending
        delivery.ack_eligible = True
        mmu._disconnect()

        record = mmu.host_recovery_record()
        self.assertEqual((record.instance, record.session, record.generation,
                          record.request_generation), (INSTANCE, 13, 7, 1))
        self.assertIs(record.pending_ack, pending)
        self.assertTrue(record.pending_ack_eligible)
        self.assertEqual(record.pending_requests, (('fps', identity, 4),))
        self.assertTrue(mmu._host_session_ambiguous)
        self.assertFalse(requests.connected)
        self.assertFalse(delivery.connected)
        self.assertIsNone(delivery.pending)
        self.assertIsNone(mmu._prepared_host_session)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            record.session = 14
        # Connect invalidation must not replace the original recovery proof.
        mmu._invalidate('unavailable')
        self.assertIs(mmu.host_recovery_record(), record)
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            mmu.prepare_cold_host_session(2)

    def test_query_failure_preserves_waiter_without_ack(self):
        mmu = observer()
        requests = mmu._prepared_host_session.requests
        identity = requests.issue(2, 1, 'fps', 5)
        mmu._fail(7, 'query failed')
        record = mmu.host_recovery_record()
        self.assertIsNone(record.pending_ack)
        self.assertEqual(record.pending_requests, (('fps', identity, 5),))
        self.assertTrue(mmu._host_session_ambiguous)
        self.assertFalse(requests.connected)
        self.assertIsNone(mmu._prepared_host_session)
        with self.assertRaises(ValueError):
            mmu.prepare_cold_host_session(2)

    def test_renewal_timeout_keeps_ack_even_after_lease_disconnects(self):
        mmu = observer()
        prepared = mmu._prepared_host_session
        identity = prepared.requests.issue(1, 2, 'fps', 4)
        pending = Event(INSTANCE, identity, 9, 100, 1, 0, 200, 0)
        prepared.delivery.pending = pending
        prepared.delivery.ack_eligible = False
        mmu._host_lease = HostLease(
            prepared, mmu._mcu, mmu._printer.reactor, 7,
            lambda instance, generation:
            mmu.physical_connection_identity() == (generation, instance),
            500, 200)
        status = Snapshot(INSTANCE, 78, dict(
            motor_ready=True, host_connected=True,
            client_sessions=(0, 13, 0, 0), system_fault=0,
            motion_fault=0, follower_fault=0,
            position_uncertain=False, pending_fault_event=False))
        mmu.capture_physical_state = lambda: ((7, INSTANCE), 1.0, status, None)
        with patch('host.openams_host_lease.send_heartbeat',
                   side_effect=ValueError('MMU command timed out')) as beat:
            with self.assertRaisesRegex(ValueError, 'timed out'):
                mmu.renew_host_lease()
            self.assertEqual(beat.call_count, 1)
        record = mmu.host_recovery_record()
        self.assertIs(record.pending_ack, pending)
        self.assertFalse(record.pending_ack_eligible)
        self.assertEqual(record.pending_requests, (('fps', identity, 4),))
        self.assertEqual((record.generation, record.request_generation), (7, 1))
        self.assertFalse(prepared.delivery.connected)
        self.assertIsNone(prepared.delivery.pending)
        self.assertTrue(mmu._host_session_ambiguous)
        with self.assertRaises(ValueError):
            mmu.renew_host_lease()

    def test_faulted_heartbeat_delivers_the_terminal_before_retaining_recovery(self):
        # A board fault refuses lease renewal, but only after one event cycle
        # delivered the interrupted operation's retained terminal and its fault
        # code. Otherwise that terminal is stranded behind the recovery record.
        for name, drain_first, load in (('renewal due', False, False),
                                        ('lease fresh at first cycle', True, False),
                                        ('LOAD waiter', False, True)):
            with self.subTest(name=name):
                reactor = ServiceReactor()
                mmu = observer()
                mmu._printer = types.SimpleNamespace(
                    get_reactor=lambda: reactor, command_error=ValueError)
                mmu._firmware_host_lease_ms = 500
                capture = [recovered_capture(mmu)]
                mmu.capture_physical_state = lambda: capture[0]
                unit = OpenAMSManagedUnit(
                    'box', mmu, 1, 'fps', 'boxturtle', 2, 800.,
                    host_watchdog_ms=500, lease_max_age_ms=200)
                mmu._openams_unit = unit
                unit._state.hardware_snapshot_ready = True
                requests = mmu._prepared_host_session.requests
                runtime = FaultedRuntime()
                unit.bind_runtime(runtime, 'fps')
                if load:
                    identity = requests.issue(OpType.LOAD, 0, waiter='fps', token=1)
                    event_type = EventType.LOAD_FAILED
                else:
                    identity = requests.issue_preload(0)
                    event_type = EventType.PRELOAD_FAILED
                unit.bind_runtime(runtime, 'fps', mmu._prepared_host_session,
                                  mmu._mcu, reactor)
                with patch('host.openams_host_lease.send_heartbeat',
                           return_value=CommandResult(INSTANCE, 3, 13, 0)):
                    lease = mmu.start_host_lease(
                        mmu._prepared_host_session, 500, 200)
                unit._lease = lease
                self.assertIs(mmu._host_lease, lease)
                # The unit's own cache is current; only the heartbeat's capture
                # carries the fault, exactly as a real renewal would see it.
                unit._captured_at = reactor.monotonic()
                unit._captured_status = capture[0][2]
                unit._bound = (mmu._generation, mmu._instance)
                capture[0] = recovered_capture(mmu, system_fault=9, motion_fault=9)
                acked = []

                def ack(mcu, reactor_, sends, replies, is_current, timeout, what):
                    acked.append(sends[0][1][-1])
                    return CommandResult(INSTANCE, Command.EVENT_ACK,
                                         sends[0][1][-1], CommandCode.OK)

                terminal = Event(INSTANCE, identity, 9, 100, event_type,
                                 OpCode.ERROR_UNSPECIFIED, 250, 0)
                lease.confirmed_at = (reactor.monotonic() if drain_first
                                      else reactor.monotonic() - lease.max_age_s)
                with patch('host.event_cycle.peek_event',
                           return_value=terminal), \
                        patch('host.event_delivery.exchange', side_effect=ack), \
                        patch('host.openams_host_lease.send_heartbeat',
                              return_value=CommandResult(INSTANCE, 3, 13, 0)):
                    if drain_first:
                        # No renewal here: the ordinary cycle still drains it.
                        unit._service(reactor.monotonic())
                        self.assertEqual(len(acked), 1)
                        lease.confirmed_at = (reactor.monotonic()
                                              - lease.max_age_s)
                    self.assertEqual(unit._service(reactor.monotonic()),
                                     reactor.NEVER)
                self.assertEqual(len(acked), 1)
                self.assertIsNone(mmu._host_lease)
                self.assertIsNotNone(mmu.host_recovery_record())
                if load:
                    self.assertEqual(runtime.terminals[0][:2],
                                     ('fps', OpCode.ERROR_UNSPECIFIED))
                    self.assertEqual(requests.pending, {})
                else:
                    self.assertIsNotNone(requests.preload_terminal)
                    self.assertEqual(requests.preload_terminal.event_type,
                                     event_type)
                with self.assertRaisesRegex(ValueError, 'unavailable'):
                    mmu.cmd_KLIPPER_MMU_RECOVER_LOCAL(
                        types.SimpleNamespace(respond_info=Mock(), error=Mock()))

    def test_service_exception_settles_waiter_logs_and_retains_recovery(self):
        # Any exception _service() cannot recover from (here: a plain bug in
        # event service, not a firmware fault) must not strand a caller
        # blocked in Runtime.request(...).wait() (OAMSM_LOAD_FILAMENT/UNLOAD,
        # KLIPPER_MMU_CALIBRATE). It logs once, fails that waiter through the
        # existing Completion/accept_terminal path, retains recovery and
        # never restores motion.
        reactor = ServiceReactor()
        mmu = observer()
        mmu._printer = types.SimpleNamespace(
            get_reactor=lambda: reactor, command_error=ValueError)
        mmu._firmware_host_lease_ms = 500
        capture = recovered_capture(mmu)
        mmu.capture_physical_state = lambda: capture
        unit = OpenAMSManagedUnit(
            'box', mmu, 1, 'fps', 'boxturtle', 2, 800.,
            host_watchdog_ms=500, lease_max_age_ms=200)
        mmu._openams_unit = unit
        unit._state.hardware_snapshot_ready = True
        requests = mmu._prepared_host_session.requests
        runtime = FaultedRuntime()
        unit.bind_runtime(runtime, 'fps')
        identity = requests.issue(OpType.LOAD, 0, waiter='fps', token=3)
        unit.bind_runtime(runtime, 'fps', mmu._prepared_host_session,
                          mmu._mcu, reactor)
        with patch('host.openams_host_lease.send_heartbeat',
                   return_value=CommandResult(INSTANCE, 3, 13, 0)):
            lease = mmu.start_host_lease(mmu._prepared_host_session, 500, 200)
        unit._lease = lease
        self.assertIs(mmu._host_lease, lease)
        unit._captured_at = reactor.monotonic()
        unit._captured_status = capture[2]
        unit._bound = (mmu._generation, mmu._instance)
        # Renewal is not due yet; the exception comes from event service.
        lease.confirmed_at = reactor.monotonic()
        mmu.service_openams_event_once = Mock(
            side_effect=RuntimeError('serial link dropped'))
        with self.assertLogs(level='ERROR') as logs:
            result = unit._service(reactor.monotonic())
        self.assertEqual(result, reactor.NEVER)
        self.assertIsNone(mmu._host_lease)
        self.assertIsNotNone(mmu.host_recovery_record())
        self.assertFalse(unit.motion_available)
        self.assertEqual(requests.pending, {})
        self.assertEqual(runtime.terminals,
                         [('fps', OpCode.ERROR_KLIPPER_CALL, 0, 3)])
        self.assertTrue(any('RECOVER_SESSION' in line for line in logs.output))
        self.assertTrue(any('serial link dropped' in line for line in logs.output))

    def test_no_prepared_session_keeps_previous_invalidation_behavior(self):
        mmu = observer()
        mmu._prepared_host_session = None
        mmu._disconnect()
        self.assertIsNone(mmu.host_recovery_record())
        self.assertFalse(mmu._host_session_ambiguous)
        self.assertEqual(mmu._state, 'disconnected')
        self.assertIsNone(mmu._instance)


if __name__ == '__main__':
    unittest.main()
