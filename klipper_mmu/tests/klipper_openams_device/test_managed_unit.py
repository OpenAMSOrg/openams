# SPDX-License-Identifier: GPL-3.0-or-later
import threading
import sys
import types
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from host.openams_managed_unit import OpenAMSManagedUnit
from host import HostRecoveryRecord, KlipperMMU
from host.event_delivery import EventDelivery
from host.openams_operation_bridge import (OpenAMSLoadCommandBridge,
                                           OpenAMSUnloadCommandBridge)
from host.openams_follow_bridge import OpenAMSFollowCommandBridge
from host.openams_session_bootstrap import (PreparedHostSession,
                                            PreparedOccupiedHostSession)
from host.protocol import (Admission, Event, Identity, SensorSample,
                           SensorSnapshot, Snapshot)
from host.requests import FOLLOW, HOST, LOAD, Requests
from host.wire import OpCode, OpState


INSTANCE = 0x1020304050607080


class Reactor:
    NEVER = 1e30

    def __init__(self):
        self.now = 10.0
        self.timers = []

    def monotonic(self):
        return self.now

    def register_timer(self, callback, when):
        self.timers.append((callback, when))
        return callback


class Observer:
    def __init__(self):
        self._printer = types.SimpleNamespace(get_reactor=lambda: self.reactor)
        self.reactor = Reactor()
        self._mcu = object()
        self._mcu_lock = threading.Lock()
        self._firmware_actuation = 1
        self._diagnostic_startup = True
        self._startup_state = 'ready'
        self._startup_result = 0
        self._host_session_ambiguous = False
        self._host_lease = None
        self._prepared_host_session = None
        self._host_recovery_record = None
        self.recoveries = 0
        self.query = 12
        self.owner = Identity(HOST, 13, 41, LOAD, 2)
        self.armed = False
        self.direction = 1
        self.follower_state = 0

    def physical_connection_identity(self):
        return (3, INSTANCE)

    def physical_monotonic(self):
        return self.reactor.now

    def get_mcu(self):
        return self._mcu

    def _retain_host_recovery(self):
        self.recoveries += 1

    def host_recovery_record(self):
        return self._host_recovery_record

    def capture_physical_state(self):
        owner = self.owner
        lane = owner.lane if owner is not None else 255
        occupied = owner is not None
        values = dict(path_occupied=occupied, lane_active=occupied,
                      terminal_pending=False, position_uncertain=False,
                      pending_fault_event=False, owner_lane=lane,
                      occupant_lane=lane, system_fault=0, motion_fault=0,
                      stage=3 if self.armed else 0,
                      state=4 if self.armed else 0,
                      follower_state=self.follower_state,
                      follower_direction=self.direction, follower_fault=0,
                      acquired_ms=100, client=owner.client if owner else 0,
                      session=owner.session if owner else 0,
                      request=owner.request if owner else 0,
                      operation=owner.operation if owner else 0,
                      lane=lane, motor_ready=True, host_connected=True,
                      client_sessions=(0, 13, 0, 0),
                      client_requests=(0, 50, 0, 0))
        sample = lambda present: SensorSample(True, present, True, True, 100)
        bays = tuple(sample(occupied and bay == lane) for bay in range(4))
        sensors = SensorSnapshot(INSTANCE, self.query, 4, 100, bays,
                                 sample(occupied), sample(False))
        return ((3, INSTANCE), self.reactor.now,
                Snapshot(INSTANCE, self.query, values), sensors)


class Lease:
    def __init__(self, prepared, reactor):
        self.prepared = prepared
        self.reactor = reactor
        self.available = True
        self.max_age_s = .4
        self.confirmed_at = 10.

    def motion_permitted(self, board_ready):
        return (self.available and self.confirmed_at is not None
                and 0 <= self.reactor.monotonic() - self.confirmed_at
                < self.max_age_s and board_ready())


def fake_runtime():
    """Every hook the managed unit requires from OpenAMS's runtime."""
    return types.SimpleNamespace(
        dispatch=lambda action: None, accept_terminal=lambda *args, **kwargs: True,
        accept_reload_terminal=lambda *args, **kwargs: False,
        accept_verified_follower=Mock(return_value=True),
        accept_retired_terminal=Mock(return_value=False),
        accept_firmware_fault=Mock(return_value=False))


class ManagedUnitTests(unittest.TestCase):
    def test_motion_composition_requires_exact_id_and_lanes(self):
        extra = KlipperMMU.__new__(KlipperMMU)
        
        devices = [
            (7, 'emu', 5),
            (1, 'boxturtle', 4),
            (2, 'nightowl', 4),
            (5, '3ms', 4),
            (6, 'angry_beaver', 4),
            (12, 'kms', 4),
            (18, 'quattrobox_1_0', 4),
            (19, 'quattrobox_1_1', 4),
            (20, 'quattrobox_2', 4),
        ]
        
        for expected_id, family, expected_bays in devices:
            with self.subTest(family=family, expected_bays=expected_bays):
                extra._firmware_composition = (expected_id, expected_bays)
                extra._openams_unit = types.SimpleNamespace(
                    device_family=family, bay_count=expected_bays)
                self.assertTrue(extra._motion_composition_matches(expected_bays))
                
                # Mismatch tests
                for firmware, test_family, bays, match_bays in (
                        ((expected_id, expected_bays - 1), family, expected_bays, expected_bays),
                        ((expected_id == 1 and 2 or 1, expected_bays), family, expected_bays, expected_bays),
                        ((expected_id, expected_bays), 'unknown', expected_bays, expected_bays),
                        ((expected_id, expected_bays), family, expected_bays - 1, expected_bays),
                        ((expected_id, expected_bays), family, expected_bays, expected_bays + 1)):
                    with self.subTest(firmware=firmware, test_family=test_family, bays=bays, match_bays=match_bays):
                        extra._firmware_composition = firmware
                        extra._openams_unit = types.SimpleNamespace(
                            device_family=test_family, bay_count=bays)
                        self.assertFalse(extra._motion_composition_matches(match_bays))

    def test_motion_install_registers_unpreload_gcode(self):
        class Config:
            options = dict(openams_idx='1', openams_fps='fps',
                           openams_family='boxturtle', openams_bays='4',
                           openams_path_length_mm='800',
                           openams_host_watchdog_ms='1000',
                           openams_lease_max_age_ms='400')

            def __init__(self, printer):
                self.printer = printer

            def get_printer(self):
                return self.printer

            def get_name(self):
                return 'klipper_mmu box'

            def get(self, key, default=None):
                return self.options.get(key, default)

            def getboolean(self, key, default=False):
                return True if key in ('openams_motion', 'diagnostic_startup') else default

        class Printer:
            config_error = ValueError

            def __init__(self):
                self.mcu = types.SimpleNamespace(register_config_callback=lambda cb: None)
                self.commands = []

            def lookup_objects(self, name):
                return []

            def lookup_object(self, name):
                return types.SimpleNamespace(register_mux_command=lambda *a, **k:
                                             self.commands.append(a[0]))

            get_reactor = lambda self: types.SimpleNamespace(mutex=threading.Lock)
            def register_event_handler(self, *args):
                pass

        printer = Printer()
        with patch.dict(sys.modules, {'mcu': types.SimpleNamespace(
                get_printer_mcu=lambda p, n: p.mcu)}):
            KlipperMMU(Config(printer))
        self.assertIn('KLIPPER_MMU_UNPRELOAD', printer.commands)
        self.assertIn('KLIPPER_MMU_RECOVER_LOCAL', printer.commands)

    def test_installed_unit_requires_explicit_motion_and_lease_config(self):
        class Config:
            options = dict(openams_idx='1', openams_fps='fps',
                           openams_family='boxturtle', openams_bays='4',
                           openams_path_length_mm='800',
                           openams_host_watchdog_ms='1000',
                           openams_lease_max_age_ms='400')

            def __init__(self, printer, motion):
                self.printer, self.motion = printer, motion

            def get_printer(self):
                return self.printer

            def get_name(self):
                return 'klipper_mmu box'

            def get(self, key, default=None):
                return self.options.get(key, default)

            def getboolean(self, key, default=False):
                return self.motion if key in ('openams_motion',
                                              'diagnostic_startup') else default

        class Printer:
            config_error = ValueError

            def __init__(self):
                self.mcu = object()
                self.commands = []

            def lookup_objects(self, name):
                return []

            def lookup_object(self, name):
                return types.SimpleNamespace(register_mux_command=lambda *a, **k:
                                             self.commands.append(a[0]))

            get_reactor = lambda self: types.SimpleNamespace(mutex=threading.Lock)
            def register_event_handler(self, *args):
                pass

        printer = Printer()
        printer.mcu = types.SimpleNamespace(register_config_callback=lambda cb: None)
        with patch.dict(sys.modules, {'mcu': types.SimpleNamespace(
                get_printer_mcu=lambda p, n: p.mcu)}):
            old = KlipperMMU(Config(printer, False)).get_openams_unit()
            self.assertFalse(old.motion_available)
            self.assertNotIn('KLIPPER_MMU_RECOVER_LOCAL', printer.commands)
            unit = KlipperMMU(Config(printer, True)).get_openams_unit()
            self.assertIsInstance(unit, OpenAMSManagedUnit)
            self.assertFalse(unit.motion_available)
            Config.options['openams_lease_max_age_ms'] = '1000'
            try:
                with self.assertRaisesRegex(ValueError, 'lease bounds'):
                    KlipperMMU(Config(printer, True))
            finally:
                Config.options['openams_lease_max_age_ms'] = '400'

    def make_bound(self):
        observer = Observer()
        unit = OpenAMSManagedUnit('box', observer, 1, 'fps', 'boxturtle', 4,
                                  800., host_watchdog_ms=1000,
                                  lease_max_age_ms=400)
        requests = Requests(INSTANCE, 13, 41)
        prepared = PreparedHostSession(requests, EventDelivery(INSTANCE, 3), 12)
        runtime = fake_runtime()
        unit.bind_runtime(runtime, 'fps')
        observer._prepared_host_session = prepared
        lease = Lease(prepared, observer.reactor)
        observer._host_lease = unit._lease = lease
        unit._bound = (3, INSTANCE)
        unit._state.hardware_snapshot_ready = True
        unit._captured_at = 10.
        unit._captured_status = Snapshot(INSTANCE, 12, dict(
            motor_ready=True, host_connected=True,
            client_sessions=(0, 13, 0, 0), system_fault=0,
            motion_fault=0, follower_fault=0,
            position_uncertain=False, pending_fault_event=False))
        unit._load = OpenAMSLoadCommandBridge(
            observer._mcu, observer.reactor, requests, 'fps', 4,
            is_current=lambda: unit._event_current(),
            motion_permitted=lambda: unit.motion_available,
            accept_terminal=runtime.accept_terminal)
        unit._follow = OpenAMSFollowCommandBridge(
            observer._mcu, observer.reactor, requests,
            owner_identity=unit.owner_identity,
            current_occupant=lambda: unit.current_spool,
            is_current=lambda: unit._event_current(),
            motion_permitted=lambda: unit.motion_available,
            bay_count=4)
        unit.bind_runtime(runtime, 'fps', prepared, observer._mcu,
                          observer.reactor)
        return observer, unit

    def test_stale_snapshot_refreshes_before_next_world_build(self):
        observer, unit = self.make_bound()
        unit.refresh = Mock(return_value=True)
        unit._snapshot_stale = True  # Set when the event cycle consumes an event.
        self.assertTrue(unit.hardware_snapshot_ready)
        self.assertTrue(unit.hardware_snapshot_ready)
        unit.refresh.assert_called_once_with()

    def test_start_requires_fresh_fault_free_capture_and_lease(self):
        observer, unit = self.make_bound()
        self.assertTrue(unit.motion_available)
        with patch('host.openams_operation_bridge.send_operation',
                   return_value=Admission(INSTANCE, None, 0, 0)) as send:
            unit._captured_status.values['motion_fault'] = 5
            self.assertFalse(unit.motion_available)
            with self.assertRaisesRegex(ValueError, 'permission'):
                unit.start_load_spool(0, 1)
            send.assert_not_called()
            unit._captured_status.values['motion_fault'] = 0
            observer.reactor.now = 10.5
            self.assertFalse(unit.motion_available)
            with self.assertRaisesRegex(ValueError, 'permission'):
                unit.start_load_spool(0, 1)
            send.assert_not_called()

    def test_admitted_load_stays_pending_for_retained_terminal(self):
        observer, unit = self.make_bound()

        def admit(mcu, reactor, instance, identity, current, **kwargs):
            self.assertTrue(observer._mcu_lock.locked())
            self.assertTrue(current())
            return Admission(instance, identity, 0, 0)

        with patch('host.openams_operation_bridge.send_operation',
                   side_effect=admit) as send:
            admission = unit.start_load_spool(2, 7)
        self.assertEqual(send.call_count, 1)
        self.assertFalse(observer._mcu_lock.locked())
        self.assertEqual(admission.identity.lane, 2)
        self.assertIn('fps', unit._event_binding[1].requests.pending)

    def test_refresh_while_start_yields_preserves_permission_and_waiter(self):
        observer, unit = self.make_bound()
        previous = unit._captured_status
        prepared = unit._event_binding[1]
        in_send, release, results = threading.Event(), threading.Event(), {}

        def admit(mcu, reactor, instance, identity, current, **kwargs):
            in_send.set()
            release.wait(5)  # START holds the MCU mutex until its reply.
            self.assertTrue(current())
            return Admission(instance, identity, 0, 0)

        def run(name, call):
            return threading.Thread(target=lambda: results.setdefault(name, call()))

        with patch('host.openams_operation_bridge.send_operation',
                   side_effect=admit):
            start = run('start', lambda: unit.start_load_spool(2, 7))
            start.start()
            self.assertTrue(in_send.wait(5))
            # The manager's periodic refresh waits; that is not a disconnect.
            refresh = run('refresh', unit.refresh)
            refresh.start()
            refresh.join(0.05)
            self.assertNotIn('refresh', results)
            self.assertTrue(unit.hardware_snapshot_ready)
            self.assertTrue(unit.motion_available)
            self.assertIs(unit._captured_status, previous)
            identity = prepared.requests.pending['fps'][0]
            observer.query += 1
            release.set()
            start.join(5)
            refresh.join(5)
        self.assertIs(results['refresh'], True)
        self.assertEqual(results['start'].identity, identity)
        self.assertFalse(observer._mcu_lock.locked())
        self.assertEqual(observer.recoveries, 0)
        self.assertIs(unit._event_binding[1], prepared)
        self.assertEqual(prepared.requests.pending['fps'][0], identity)

    def test_local_preload_allows_same_lane_and_blocks_cross_lane_load(self):
        observer, unit = self.make_bound()
        unit._state.local_reserved_lane = 2
        unit._state.hub_hes_value = (False,) * 4
        self.assertIsNone(unit.current_spool)
        self.assertEqual(unit._command_occupant(), 2)
        with patch('host.openams_operation_bridge.send_operation') as send:
            with self.assertRaisesRegex(ValueError, 'reserves another'):
                unit.start_load_spool(1, 7)
            send.assert_not_called()
        with patch('host.openams_operation_bridge.send_operation',
                   side_effect=lambda m, r, i, identity, current, **kw:
                   Admission(i, identity, 0, 0)):
            self.assertEqual(unit.start_load_spool(2, 7).identity.lane, 2)

    def test_local_preload_unload_uses_reserved_lane(self):
        observer, unit = self.make_bound()
        unit._state.local_reserved_lane = 2
        unit._state.hub_hes_value = (False,) * 4
        unit._unload = OpenAMSUnloadCommandBridge(
            observer._mcu, observer.reactor,
            unit._event_binding[1].requests, 'fps', 4,
            current_occupant=unit._command_occupant,
            is_current=unit._event_current,
            motion_permitted=lambda: unit.motion_available,
            accept_terminal=unit._event_binding[0].accept_terminal)
        with patch('host.openams_operation_bridge.send_operation',
                   side_effect=lambda m, r, i, identity, current, **kw:
                   Admission(i, identity, 0, 0)):
            self.assertEqual(unit.start_unload_spool(7).identity.lane, 2)

    def test_preload_has_own_guarded_identity_and_no_openams_waiter(self):
        observer, unit = self.make_bound()
        requests = unit._event_binding[1].requests

        def admit(mcu, reactor, instance, identity, current, **kwargs):
            self.assertTrue(observer._mcu_lock.locked())
            self.assertTrue(current())
            self.assertEqual((identity.operation, identity.lane, kwargs),
                             (6, 2, {'direction': 1}))
            return Admission(instance, identity, 0, 0)

        with patch('host.openams_managed_unit.send_operation', side_effect=admit):
            identity, reply = unit.start_preload(2)
        self.assertEqual(reply.admission, 0)
        self.assertEqual(requests.pending_preload, identity)
        self.assertEqual(requests.pending, {})
        with self.assertRaisesRegex(ValueError, 'Existing PRELOAD'):
            unit.start_preload(1)
        unit._lease.available = False
        with self.assertRaisesRegex(ValueError, 'permission'):
            unit.start_preload(1)

    def test_eject_on_a_preloaded_lane_says_to_unpreload_first(self):
        observer, unit = self.make_bound()
        unit.refresh = Mock(return_value=False)
        status = types.SimpleNamespace(values={'state': OpState.PRELOADED})
        observer.capture_physical_state = Mock(return_value=((1, 2), 0.0, status, None))
        with patch('host.openams_managed_unit.send_operation') as send:
            with self.assertRaisesRegex(ValueError, 'run KLIPPER_MMU_UNPRELOAD first'):
                unit.start_eject(2)
            status.values['state'] = OpState.IDLE
            with self.assertRaisesRegex(ValueError, 'requires a current physical snapshot'):
                unit.start_eject(2)
            send.assert_not_called()

    def test_eject_requires_clear_path_and_exact_terminal(self):
        observer, unit = self.make_bound()
        requests = unit._event_binding[1].requests
        unit.refresh = Mock(return_value=True)
        unit._state.hub_hes_value = (False, False, True, False)
        with patch('host.openams_managed_unit.send_operation') as send:
            with self.assertRaisesRegex(ValueError, 'clear shared path'):
                unit.start_eject(2)
            send.assert_not_called()
        unit._state.hub_hes_value = (False,) * 4

        def admit(mcu, reactor, instance, identity, current, **kwargs):
            self.assertTrue(observer._mcu_lock.locked())
            self.assertTrue(current())
            self.assertEqual((identity.operation, identity.lane, kwargs),
                             (7, 2, {'direction': 0}))
            return Admission(instance, identity, 0, 0)

        with patch('host.openams_managed_unit.send_operation', side_effect=admit):
            identity, reply = unit.start_eject(2)
        self.assertEqual(reply.identity, identity)
        self.assertEqual(requests.pending_eject, identity)
        self.assertEqual(requests.pending, {})
        with patch('host.openams_managed_unit.send_operation', side_effect=
                   lambda m, r, i, target, current, **kw:
                   Admission(i, target, 0, 0)):
            self.assertEqual(unit.cancel_local_request('EJECT')[0], identity)
        self.assertEqual(requests.pending_eject, identity)
        self.assertFalse(requests.accept_eject(
            Event(INSTANCE, replace(identity, operation=2), 1, 100, 4, 0, 0, 0),
            requests.generation))
        terminal = Event(INSTANCE, identity, 2, 101, 17, 6, 0, 0)
        self.assertTrue(requests.accept_eject(terminal, requests.generation))
        self.assertIs(requests.eject_terminal, terminal)
        self.assertIsNone(requests.pending_eject)

    def test_standalone_cancel_targets_exact_request_and_waits_for_terminal(self):
        for operation, field in (('PRELOAD', 'pending_preload'),
                                 ('UNPRELOAD', 'pending_unpreload')):
            with self.subTest(operation=operation):
                observer, unit = self.make_bound()
                requests = unit._event_binding[1].requests
                identity = (requests.issue_preload(2) if operation == 'PRELOAD'
                            else requests.issue_unpreload(2))
                requests.pending['fps'] = (Identity(HOST, 13, 40, LOAD, 2), 7)

                def admit(mcu, reactor, instance, target, current, **kwargs):
                    self.assertTrue(observer._mcu_lock.locked())
                    self.assertTrue(current())
                    self.assertEqual((instance, target, kwargs),
                                     (INSTANCE, identity, {'cancel': True}))
                    return Admission(instance, target, 0, 0)

                with patch('host.openams_managed_unit.send_operation', side_effect=admit):
                    result = unit.cancel_local_request(operation)
                self.assertEqual(result[0], identity)
                self.assertEqual(getattr(requests, field), identity)
                self.assertIsNone(requests.preload_terminal if operation == 'PRELOAD'
                                  else requests.unpreload_terminal)
                self.assertIn('fps', requests.pending)
                accept = (requests.accept_preload if operation == 'PRELOAD'
                          else requests.accept_unpreload)
                event_type = 14 if operation == 'PRELOAD' else 5
                foreign = Event(INSTANCE, replace(identity, request=identity.request + 1),
                                1, 100, event_type, 6, 0, 0)
                self.assertFalse(accept(foreign, requests.generation))
                self.assertEqual(getattr(requests, field), identity)
                terminal = replace(foreign, identity=identity)
                self.assertTrue(accept(terminal, requests.generation))
                self.assertIsNone(getattr(requests, field))
                self.assertIs(requests.preload_terminal if operation == 'PRELOAD'
                              else requests.unpreload_terminal, terminal)

    def test_standalone_cancel_fails_closed(self):
        observer, unit = self.make_bound()
        requests = unit._event_binding[1].requests
        with patch('host.openams_managed_unit.send_operation') as send:
            with self.assertRaisesRegex(ValueError, 'No pending PRELOAD'):
                unit.cancel_local_request('PRELOAD')
            send.assert_not_called()
        identity = requests.issue_preload(2)
        with patch('host.openams_managed_unit.send_operation',
                   return_value=Admission(INSTANCE, identity, 2, 0)):
            with self.assertRaisesRegex(ValueError, 'rejected'):
                unit.cancel_local_request('PRELOAD')
        with patch('host.openams_managed_unit.send_operation',
                   return_value=Admission(INSTANCE, identity, 10, 0)):
            with self.assertRaisesRegex(ValueError, 'rejected'):
                unit.cancel_local_request('PRELOAD')
        self.assertEqual(requests.pending_preload, identity)
        with patch('host.openams_managed_unit.send_operation',
                   side_effect=ValueError('MMU command timed out')):
            with self.assertRaisesRegex(ValueError, 'timed out'):
                unit.cancel_local_request('PRELOAD')
        self.assertEqual(requests.pending_preload, identity)
        requests.generation += 1
        with patch('host.openams_managed_unit.send_operation') as send:
            with self.assertRaisesRegex(ValueError, 'connection'):
                unit.cancel_local_request('PRELOAD')
            send.assert_not_called()

    def test_unpreload_requires_local_reservation_and_retains_own_result(self):
        observer, unit = self.make_bound()
        unit.refresh = Mock(return_value=True)
        requests = unit._event_binding[1].requests
        with patch('host.openams_managed_unit.send_operation') as send:
            with self.assertRaisesRegex(ValueError, 'local reservation'):
                unit.start_unpreload(2)
            send.assert_not_called()
        unit._state.local_reserved_lane = 2
        unit._state.hub_hes_value = (False,) * 4

        def admit(mcu, reactor, instance, identity, current, **kwargs):
            self.assertTrue(current())
            self.assertTrue(observer._mcu_lock.locked())
            self.assertEqual((identity.operation, identity.lane, kwargs),
                             (2, 2, {'direction': 0}))
            return Admission(instance, identity, 0, 0)

        with patch('host.openams_managed_unit.send_operation', side_effect=admit):
            identity, reply = unit.start_unpreload(2)
        self.assertEqual(reply.admission, 0)
        self.assertEqual(requests.pending_unpreload, identity)
        self.assertEqual(requests.pending, {})
        with patch('host.openams_managed_unit.send_operation') as send:
            with self.assertRaisesRegex(ValueError, 'Existing local request'):
                unit.start_unpreload(2)
            with self.assertRaisesRegex(ValueError, 'Existing UNPRELOAD'):
                unit.start_preload(2)
            send.assert_not_called()

    def test_unpreload_definite_rejection_and_changed_capture(self):
        observer, unit = self.make_bound()
        unit.refresh = Mock(return_value=True)
        unit._state.local_reserved_lane = 2
        unit._state.hub_hes_value = (False,) * 4
        requests = unit._event_binding[1].requests
        with patch('host.openams_managed_unit.send_operation',
                   side_effect=lambda m, r, i, identity, current, **kw:
                   Admission(i, identity, 2, 0)):
            identity, reply = unit.start_unpreload(2)
        self.assertEqual(reply.admission, 2)
        self.assertIsNone(requests.pending_unpreload)
        self.assertIs(requests.unpreload_terminal, reply)
        with patch('host.openams_managed_unit.send_operation') as send:
            unit._state.local_reserved_lane = 1
            with self.assertRaisesRegex(ValueError, 'local reservation'):
                unit.start_unpreload(2)
            send.assert_not_called()

    def test_unpreload_refresh_rejects_disappeared_local_reservation(self):
        observer, unit = self.make_bound()
        requests = unit._event_binding[1].requests
        unit._state.local_reserved_lane = 2  # Previous usable capture.
        observer.owner = None  # A later physical capture sees an empty path.
        observer.query += 1
        with patch('host.openams_managed_unit.send_operation') as send:
            with self.assertRaisesRegex(ValueError, 'local reservation'):
                unit.start_unpreload(2)
            send.assert_not_called()
        self.assertIsNone(requests.pending_unpreload)
        self.assertEqual(requests.last_request, 41)

    def test_unpreload_gcode_reports_pending_and_rejects_invalid_bay(self):
        observer, unit = self.make_bound()
        unit.refresh = Mock(return_value=True)
        extra = KlipperMMU.__new__(KlipperMMU)
        extra._openams_unit = unit
        extra._printer = types.SimpleNamespace(command_error=ValueError)

        class Gcmd:
            def __init__(self, bay=None):
                self.bay = bay
                self.messages = []

            def get(self, key, default=None):
                return self.bay if key == 'BAY' and self.bay is not None else default

            def get_int(self, key):
                return self.bay

            def respond_info(self, message):
                self.messages.append(message)

        query = Gcmd()
        extra.cmd_KLIPPER_MMU_UNPRELOAD(query)
        self.assertEqual(query.messages, ['UNPRELOAD has no recorded request'])
        unit._state.local_reserved_lane = 2
        unit._state.hub_hes_value = (False,) * 4
        with patch('host.openams_managed_unit.send_operation',
                   side_effect=lambda m, r, i, identity, current, **kw:
                   Admission(i, identity, 0, 0)) as send:
            with self.assertRaisesRegex(ValueError, 'Invalid MMU bay'):
                extra.cmd_KLIPPER_MMU_UNPRELOAD(Gcmd(8))
            send.assert_not_called()
            start = Gcmd(2)
            extra.cmd_KLIPPER_MMU_UNPRELOAD(start)
            self.assertIn('admission=0', start.messages[0])
        query = Gcmd()
        extra.cmd_KLIPPER_MMU_UNPRELOAD(query)
        self.assertIn('UNPRELOAD pending', query.messages[0])

    def test_gcode_standalone_cancel_reports_admission_only(self):
        observer, unit = self.make_bound()
        requests = unit._event_binding[1].requests
        extra = KlipperMMU.__new__(KlipperMMU)
        extra._openams_unit = unit
        extra._printer = types.SimpleNamespace(command_error=ValueError)

        class Gcmd:
            def __init__(self, cancel=1, bay=None):
                self.cancel, self.bay, self.messages = cancel, bay, []

            def get(self, key, default=None):
                return {'CANCEL': self.cancel, 'BAY': self.bay}.get(key) or default

            def get_int(self, key):
                return self.cancel if key == 'CANCEL' else self.bay

            def respond_info(self, message):
                self.messages.append(message)

        for operation, command, issue, field in (
                ('PRELOAD', extra.cmd_KLIPPER_MMU_PRELOAD,
                 requests.issue_preload, 'pending_preload'),
                ('UNPRELOAD', extra.cmd_KLIPPER_MMU_UNPRELOAD,
                 requests.issue_unpreload, 'pending_unpreload')):
            identity = issue(2)
            with patch('host.openams_managed_unit.send_operation',
                       return_value=Admission(INSTANCE, identity, 0, 0)):
                gcmd = Gcmd()
                command(gcmd)
            self.assertIn('terminal pending', gcmd.messages[0])
            self.assertEqual(getattr(requests, field), identity)
            with self.assertRaisesRegex(ValueError, 'does not take BAY'):
                command(Gcmd(bay=2))
            setattr(requests, field, None)

    def test_recover_local_gcode_reconciles_only_retired_local_ack(self):
        observer, unit = self.make_bound()
        requests = unit._event_binding[1].requests
        extra = KlipperMMU.__new__(KlipperMMU)
        extra._openams_unit = unit
        extra._printer = types.SimpleNamespace(command_error=ValueError)
        extra._host_recovery_record = None
        replies = []
        gcmd = types.SimpleNamespace(respond_info=replies.append)

        with patch.object(unit, 'recover_retired_local_terminal', return_value=True) as recover:
            extra.cmd_KLIPPER_MMU_RECOVER_LOCAL(gcmd)
        recover.assert_called_once_with()
        self.assertIn('terminal and ACK reconciled', replies.pop())

        identity = requests.issue_preload(2)
        event = Event(INSTANCE, identity, 9, 100, 14, 6, 0, 0)
        requests.pending_preload = None
        requests.preload_terminal = event
        requests.disconnect()
        unit._retired_requests = requests
        unit._retired_pending_ack = event
        unit._retired_pending_ack_eligible = True
        extra._host_recovery_record = HostRecoveryRecord(
            INSTANCE, 13, 3, requests.generation, event, True, ())
        with patch.object(unit, 'retry_retired_ack', return_value=True) as retry:
            extra.cmd_KLIPPER_MMU_RECOVER_LOCAL(gcmd)
        retry.assert_called_once_with()
        self.assertIn('terminal ACK reconciled', replies.pop())

        foreign = replace(event, identity=replace(identity, request=identity.request + 1))
        unit._retired_pending_ack = foreign
        extra._host_recovery_record = replace(extra._host_recovery_record,
                                              pending_ack=foreign)
        with patch.object(unit, 'retry_retired_ack') as retry:
            with self.assertRaisesRegex(ValueError, 'No exact retired local ACK'):
                extra.cmd_KLIPPER_MMU_RECOVER_LOCAL(gcmd)
            retry.assert_not_called()

    def test_service_once_serializes_renewal_and_event(self):
        observer, unit = self.make_bound()
        observer.reactor.now = 10.21
        observer.renew_host_lease = Mock(side_effect=lambda **kwargs: setattr(
            unit._lease, 'confirmed_at', observer.reactor.now))
        observer.service_openams_event_once = Mock(return_value='empty')
        next_at = unit._service(10.21)
        self.assertGreater(next_at, observer.reactor.now)
        observer.renew_host_lease.assert_called_once()
        observer.service_openams_event_once.assert_called_once()
        observer.reactor.now = 10.32
        unit._service(10.32)
        self.assertEqual(observer.renew_host_lease.call_count, 1)
        self.assertEqual(observer.service_openams_event_once.call_count, 2)
        observer.service_openams_event_once.side_effect = TimeoutError()
        self.assertEqual(unit._service(10.33), observer.reactor.NEVER)
        self.assertEqual(observer.recoveries, 1)

    def test_follower_admission_waits_for_later_exact_physical_capture(self):
        observer, unit = self.make_bound()
        self.assertTrue(unit.refresh())
        runtime = unit._runtime
        observer.service_openams_event_once = Mock(return_value='empty')
        with patch('host.openams_follow_bridge.send_operation',
                   side_effect=lambda m, r, instance, identity, current, **kw:
                   Admission(instance, identity, 0, 0)):
            command = unit.set_oams_follower(1, 1)
        self.assertEqual(command.identity.operation, FOLLOW)
        runtime.accept_verified_follower.assert_not_called()
        # A fresh query that has not yet armed the controller is insufficient.
        observer.query += 1
        observer.reactor.now = 10.1
        unit._service(observer.reactor.now)
        runtime.accept_verified_follower.assert_not_called()
        observer.query += 1
        observer.owner = command.identity
        observer.armed = True
        observer.follower_state = 1
        observer.reactor.now = 10.2
        unit._service(observer.reactor.now)
        runtime.accept_verified_follower.assert_called_once_with(
            'fps', (1, 2), True)
        self.assertIsNone(unit._pending_follower)

    def test_newer_stop_supersedes_pending_start_and_needs_disarmed_capture(self):
        observer, unit = self.make_bound()
        self.assertTrue(unit.refresh())
        runtime = unit._runtime
        observer.service_openams_event_once = Mock(return_value='empty')
        with patch('host.openams_follow_bridge.send_operation',
                   side_effect=lambda m, r, instance, identity, current, **kw:
                   Admission(instance, identity, 0, 0)):
            start = unit.set_oams_follower(1, 1)
            stop = unit.set_oams_follower(0, 1)
        self.assertEqual(stop.identity, observer.owner)
        self.assertNotEqual(start.identity, stop.identity)
        observer.query += 1
        observer.owner = None
        observer.armed = False
        observer.follower_state = 0
        observer.reactor.now = 10.1
        unit._service(observer.reactor.now)
        runtime.accept_verified_follower.assert_called_once_with(
            'fps', (1, 2), False)

    def test_missing_observation_and_rebound_session_never_confirm(self):
        observer, unit = self.make_bound()
        self.assertTrue(unit.refresh())
        runtime = unit._runtime
        observer.service_openams_event_once = Mock(return_value='empty')
        with patch('host.openams_follow_bridge.send_operation',
                   side_effect=lambda m, r, instance, identity, current, **kw:
                   Admission(instance, identity, 0, 0)):
            unit.set_oams_follower(1, 1)
        observer.query += 1
        observer.follower_state = 9  # Usable capture, unknown follower state.
        observer.reactor.now = 10.1
        unit._service(observer.reactor.now)
        runtime.accept_verified_follower.assert_not_called()
        self.assertIsNotNone(unit._pending_follower)
        observer._prepared_host_session = None
        observer._host_lease = None
        observer.reactor.now = 10.2
        unit._service(observer.reactor.now)
        self.assertIsNone(unit._pending_follower)
        runtime.accept_verified_follower.assert_not_called()

    def test_service_defers_nested_terminal_consumer_until_event_finishes(self):
        observer, unit = self.make_bound()
        observer._event_cycle_in_progress = True
        observer.renew_host_lease = Mock()
        observer.service_openams_event_once = Mock()
        observer.reactor.now = 10.21
        next_at = unit._service(observer.reactor.now)
        self.assertGreater(next_at, observer.reactor.now)
        self.assertLess(next_at, 10.4)
        self.assertEqual(observer.recoveries, 0)
        observer.renew_host_lease.assert_not_called()
        observer.service_openams_event_once.assert_not_called()
        observer.reactor.now = 10.39
        self.assertAlmostEqual(unit._service(observer.reactor.now), 10.4)

        observer._event_cycle_in_progress = False
        observer.renew_host_lease.side_effect = lambda **kwargs: setattr(
            unit._lease, 'confirmed_at', observer.reactor.now)
        observer.reactor.now = next_at
        self.assertGreater(unit._service(next_at), next_at)
        observer.renew_host_lease.assert_called_once()
        observer.service_openams_event_once.assert_called_once()
        self.assertEqual(observer.recoveries, 0)

    def test_service_deferred_at_lease_expiry_fails_closed(self):
        observer, unit = self.make_bound()
        observer._event_cycle_in_progress = True
        observer.reactor.now = 10.4
        observer.renew_host_lease = Mock()
        observer.service_openams_event_once = Mock()
        self.assertEqual(unit._service(observer.reactor.now), observer.reactor.NEVER)
        self.assertEqual(observer.recoveries, 1)
        self.assertFalse(unit.motion_available)
        observer.renew_host_lease.assert_not_called()
        observer.service_openams_event_once.assert_not_called()

    def test_service_refreshes_capture_ahead_of_ttl(self):
        observer, unit = self.make_bound()
        unit._cache_ttl = .3
        observer.service_openams_event_once = Mock(return_value='empty')
        observer.renew_host_lease = Mock(side_effect=lambda **kwargs: setattr(
            unit._lease, 'confirmed_at', observer.reactor.now))

        def refresh():
            unit._captured_at = observer.reactor.now
            return True

        with patch.object(unit, 'refresh', side_effect=refresh) as capture:
            observer.reactor.now = 10.14
            unit._service(observer.reactor.now)
            capture.assert_not_called()
            observer.reactor.now = 10.16
            unit._service(observer.reactor.now)
            capture.assert_called_once()
            self.assertTrue(unit.motion_available)
            observer.reactor.now = 10.29
            unit._service(observer.reactor.now)
            capture.assert_called_once()
        self.assertEqual(observer.recoveries, 0)

    def test_failed_physical_refresh_closes_service_before_event(self):
        observer, unit = self.make_bound()
        unit._cache_ttl = .3
        observer.reactor.now = 10.16
        observer.service_openams_event_once = Mock()

        def refresh():
            unit.disconnect()
            return False

        with patch.object(unit, 'refresh', side_effect=refresh) as capture:
            self.assertEqual(unit._service(observer.reactor.now), observer.reactor.NEVER)
            capture.assert_called_once()
        self.assertEqual(observer.recoveries, 1)
        self.assertFalse(unit.motion_available)
        observer.service_openams_event_once.assert_not_called()

    def test_transient_unknown_ownership_retries_instead_of_giving_up(self):
        # host/physical_ownership.py:64-66 reads a one-tick status/sensor
        # query mismatch as 'unknown', not a fault -- its own comment at
        # line 130-131 documents this for UNPRELOAD/EJECT travel: "every
        # refresh during their travel read 'unknown' and OpenAMS dropped
        # the session mid-operation". Unlike a real disconnect (previous
        # test), refresh() never clears _bound for this outcome, so
        # _service() must retry next tick, not tear the session down.
        observer, unit = self.make_bound()
        unit._cache_ttl = .3
        observer.reactor.now = 10.16
        real_capture = observer.capture_physical_state

        def mismatched_capture():
            identity, started, status, sensors = real_capture()
            return identity, started, status, replace(
                sensors, query=sensors.query + 1)

        observer.capture_physical_state = mismatched_capture
        observer.service_openams_event_once = Mock(return_value='acked')
        result = unit._service(observer.reactor.now)
        self.assertNotEqual(result, observer.reactor.NEVER)
        self.assertEqual(observer.recoveries, 0)
        self.assertFalse(unit._ready())  # this tick's capture stayed 'unknown'
        self.assertIsNotNone(unit._bound)
        observer.service_openams_event_once.assert_called_once()

    def test_unowned_queue_head_stops_motion_service(self):
        observer, unit = self.make_bound()
        observer.service_openams_event_once = Mock(return_value='blocked')
        observer.peek_retained_fault_once = Mock(return_value='fault')
        self.assertEqual(unit._service(10.), observer.reactor.NEVER)
        self.assertEqual(observer.recoveries, 1)
        observer.peek_retained_fault_once.assert_called_once_with(unit, timeout=.1)

    def test_unowned_queue_head_settles_pending_waiter_instead_of_hanging(self):
        # Reproduces the EMU/KMS intermittent UNLOAD hang: a live connection
        # that cannot deliver its retained event (event_cycle's 'blocked',
        # with no legitimate pending follower-stop confirmation to wait on)
        # must fail any open OpenAMS waiter the same way the except branch
        # below does, instead of leaving its G-code command (an UNLOAD, say)
        # blocked forever with no reply while the service timer stops.
        observer, unit = self.make_bound()
        requests = unit._event_binding[1].requests
        identity = Identity(HOST, requests.session, 40, LOAD, 2)
        requests.pending['fps'] = (identity, 7)
        unit._runtime.accept_terminal = Mock(return_value=True)
        observer.service_openams_event_once = Mock(return_value='blocked')
        observer.peek_retained_fault_once = Mock(return_value='fault')
        self.assertEqual(unit._service(10.), observer.reactor.NEVER)
        self.assertEqual(observer.recoveries, 1)
        self.assertNotIn('fps', requests.pending)
        unit._runtime.accept_terminal.assert_called_once_with(
            'fps', OpCode.ERROR_KLIPPER_CALL, 0, 7)

    def test_terminal_waits_for_new_follower_stop_physical_confirmation(self):
        observer, unit = self.make_bound()
        observer.armed = True
        observer.follower_state = 1
        self.assertTrue(unit.refresh())
        runtime = unit._runtime
        observer.reactor.now = 10.05
        observer.renew_host_lease = Mock(side_effect=lambda **kwargs: setattr(
            unit._lease, 'confirmed_at', observer.reactor.now))

        def consume(*args, **kwargs):
            if unit._pending_follower is None and not runtime.accept_verified_follower.called:
                unit.set_oams_follower(0, 1)
            return 'blocked' if not runtime.accept_verified_follower.called else 'acked'

        observer.service_openams_event_once = Mock(side_effect=consume)
        with patch('host.openams_follow_bridge.send_operation',
                   side_effect=lambda m, r, instance, identity, current, **kw:
                   Admission(instance, identity, 0, 0)):
            next_at = unit._service(observer.reactor.now)
        self.assertGreater(next_at, observer.reactor.now)
        self.assertEqual(observer.recoveries, 0)
        runtime.accept_verified_follower.assert_not_called()

        # Another capture still showing the old motor state retains the head.
        observer.query += 1
        observer.reactor.now = next_at
        next_at = unit._service(observer.reactor.now)
        self.assertGreater(next_at, observer.reactor.now)
        self.assertEqual(observer.recoveries, 0)
        runtime.accept_verified_follower.assert_not_called()

        observer.query += 1
        observer.owner = None
        observer.armed = False
        observer.follower_state = 0
        observer.reactor.now = next_at
        self.assertGreater(unit._service(observer.reactor.now), observer.reactor.now)
        runtime.accept_verified_follower.assert_called_once_with(
            'fps', (1, 2), False)
        self.assertEqual(observer.service_openams_event_once.call_count, 3)
        self.assertEqual(observer.recoveries, 0)

    def test_terminal_stop_confirmation_deadline_fails_closed(self):
        observer, unit = self.make_bound()
        observer.armed = True
        observer.follower_state = 1
        self.assertTrue(unit.refresh())
        observer.reactor.now = 10.05
        observer.renew_host_lease = Mock(side_effect=lambda **kwargs: setattr(
            unit._lease, 'confirmed_at', observer.reactor.now))

        def consume(*args, **kwargs):
            if unit._pending_follower is None:
                unit.set_oams_follower(0, 1)
            return 'blocked'

        observer.service_openams_event_once = Mock(side_effect=consume)
        with patch('host.openams_follow_bridge.send_operation',
                   side_effect=lambda m, r, instance, identity, current, **kw:
                   Admission(instance, identity, 0, 0)):
            next_at = unit._service(observer.reactor.now)
        self.assertGreater(next_at, observer.reactor.now)
        self.assertEqual(observer.recoveries, 0)

        # The follower never disarms; the retained terminal cannot wait past
        # the physical confirmation deadline.
        observer.reactor.now = unit._pending_follower[5] + .001
        self.assertEqual(unit._service(observer.reactor.now), observer.reactor.NEVER)
        self.assertEqual(observer.recoveries, 1)
        self.assertIsNone(unit._pending_terminal_stop)

    def test_failed_renewal_peeks_fault_once_without_event_delivery(self):
        observer, unit = self.make_bound()
        observer.reactor.now = 10.21
        observer.renew_host_lease = Mock(side_effect=ConnectionError('board fault'))
        observer.peek_retained_fault_once = Mock(return_value='fault')
        observer.service_openams_event_once = Mock()
        self.assertEqual(unit._service(observer.reactor.now), observer.reactor.NEVER)
        observer.peek_retained_fault_once.assert_called_once_with(unit, timeout=.1)
        observer.service_openams_event_once.assert_not_called()
        self.assertEqual(observer.recoveries, 1)

    def test_failed_fault_peek_cannot_restore_motion_service(self):
        observer, unit = self.make_bound()
        observer.reactor.now = 10.21
        observer.renew_host_lease = Mock(side_effect=ConnectionError('board fault'))
        observer.peek_retained_fault_once = Mock(side_effect=TimeoutError())
        observer.service_openams_event_once = Mock()
        self.assertEqual(unit._service(observer.reactor.now), observer.reactor.NEVER)
        self.assertEqual(observer.recoveries, 1)
        observer.service_openams_event_once.assert_not_called()

    def test_activation_binds_same_unit_and_registers_one_service_timer(self):
        observer = Observer()
        unit = OpenAMSManagedUnit('box', observer, 1, 'fps', 'boxturtle', 4,
                                  800., host_watchdog_ms=1000,
                                  lease_max_age_ms=400)
        runtime = fake_runtime()
        unit.bind_runtime(runtime, 'fps')
        prepared = PreparedHostSession(Requests(INSTANCE, 13),
                                       EventDelivery(INSTANCE, 3), 12)
        lease = Lease(prepared, observer.reactor)
        observer.prepare_cold_host_session = Mock(return_value=prepared)

        def start_lease(*args):
            observer._prepared_host_session = prepared
            observer._host_lease = lease
            return lease

        observer.start_host_lease = Mock(side_effect=start_lease)
        with patch('host.openams_command_device.OpenAMSDevice.refresh',
                   return_value=True) as physical:
            self.assertTrue(unit.refresh())
            self.assertEqual(physical.call_count, 2)
        self.assertIs(unit._event_binding[0], runtime)
        self.assertIs(unit._event_binding[1], prepared)
        self.assertEqual(len(observer.reactor.timers), 1)
        observer.prepare_cold_host_session.assert_called_once_with(4)

    def test_occupied_activation_rebinds_proven_lane_without_cold_retry(self):
        observer = Observer()
        unit = OpenAMSManagedUnit('box', observer, 1, 'fps', 'boxturtle', 4,
                                  800., host_watchdog_ms=1000,
                                  lease_max_age_ms=400)
        runtime = fake_runtime()
        unit.bind_runtime(runtime, 'fps')
        unit._bound = (3, INSTANCE)
        unit._state.hardware_snapshot_ready = True
        unit._state.hub_hes_value = (False, False, True, False)
        unit._captured_at = observer.reactor.now
        prepared = PreparedHostSession(Requests(INSTANCE, 14),
                                       EventDelivery(INSTANCE, 3), 12)
        observer.prepare_cold_host_session = Mock()
        observer.prepare_occupied_host_session = Mock(return_value=
            PreparedOccupiedHostSession(prepared, 2))
        lease = Lease(prepared, observer.reactor)

        def start_lease(*args):
            observer._prepared_host_session = prepared
            observer._host_lease = lease
            return lease

        observer.start_host_lease = Mock(side_effect=start_lease)
        with patch('host.openams_command_device.OpenAMSDevice.refresh',
                   return_value=True):
            self.assertTrue(unit.refresh())
        observer.prepare_occupied_host_session.assert_called_once_with(4)
        observer.prepare_cold_host_session.assert_not_called()
        observer.start_host_lease.assert_called_once_with(prepared, 1000, 400)
        self.assertIs(unit._event_binding[1], prepared)
        self.assertEqual(unit.current_spool, 2)
        self.assertEqual(len(observer.reactor.timers), 1)

    def test_occupied_lane_disagreement_retains_recovery_without_binding(self):
        observer = Observer()
        unit = OpenAMSManagedUnit('box', observer, 1, 'fps', 'boxturtle', 4,
                                  800., host_watchdog_ms=1000,
                                  lease_max_age_ms=400)
        unit._bound = (3, INSTANCE)
        unit._state.hardware_snapshot_ready = True
        unit._state.hub_hes_value = (False, False, True, False)
        unit._captured_at = observer.reactor.now
        prepared = PreparedHostSession(Requests(INSTANCE, 14),
                                       EventDelivery(INSTANCE, 3), 12)
        observer.prepare_occupied_host_session = Mock(return_value=
            PreparedOccupiedHostSession(prepared, 1))
        observer.prepare_cold_host_session = Mock()
        observer.start_host_lease = Mock()
        with self.assertRaisesRegex(ValueError, 'lane changed'):
            unit._activate()
        observer.prepare_cold_host_session.assert_not_called()
        observer.start_host_lease.assert_not_called()
        self.assertIsNone(unit._event_binding)
        self.assertEqual(observer.recoveries, 1)

    def test_local_preload_activates_matching_host_session(self):
        observer = Observer()
        unit = OpenAMSManagedUnit('box', observer, 1, 'fps', 'boxturtle', 4,
                                  800., host_watchdog_ms=1000,
                                  lease_max_age_ms=400)
        runtime = fake_runtime()
        unit.bind_runtime(runtime, 'fps')
        unit._state.local_reserved_lane = 2
        prepared = PreparedHostSession(Requests(INSTANCE, 14),
                                       EventDelivery(INSTANCE, 3), 12)
        observer.prepare_preloaded_host_session = Mock(return_value=
            PreparedOccupiedHostSession(prepared, 2))
        observer.prepare_cold_host_session = Mock()
        observer.prepare_occupied_host_session = Mock()
        lease = Lease(prepared, observer.reactor)
        def start_lease(*args):
            observer._prepared_host_session = prepared
            observer._host_lease = lease
            return lease
        observer.start_host_lease = Mock(side_effect=start_lease)
        unit._activate()
        observer.prepare_preloaded_host_session.assert_called_once_with(4)
        observer.prepare_cold_host_session.assert_not_called()
        observer.prepare_occupied_host_session.assert_not_called()
        self.assertIs(unit._event_binding[1], prepared)
        self.assertEqual(observer.recoveries, 0)

    def test_local_preload_lane_disagreement_blocks_lease(self):
        observer = Observer()
        unit = OpenAMSManagedUnit('box', observer, 1, 'fps', 'boxturtle', 4,
                                  800., host_watchdog_ms=1000,
                                  lease_max_age_ms=400)
        unit._state.local_reserved_lane = 2
        prepared = PreparedHostSession(Requests(INSTANCE, 14),
                                       EventDelivery(INSTANCE, 3), 12)
        observer.prepare_preloaded_host_session = Mock(return_value=
            PreparedOccupiedHostSession(prepared, 1))
        observer.start_host_lease = Mock()
        with self.assertRaisesRegex(ValueError, 'lane changed'):
            unit._activate()
        observer.start_host_lease.assert_not_called()
        self.assertEqual(observer.recoveries, 1)

    def test_refresh_keeps_binding_but_disconnect_retains_ack(self):
        observer, unit = self.make_bound()
        binding = unit._event_binding
        with patch('host.openams_command_device.OpenAMSDevice.refresh',
                   return_value=True):
            self.assertTrue(unit.refresh())
            self.assertIs(unit._event_binding, binding)
            pending_head = object()
            binding[1].delivery.pending = pending_head
            observer._prepared_host_session = None
            observer._host_lease = None
            self.assertTrue(unit.refresh())
        self.assertIsNone(unit._event_binding)
        self.assertIs(unit._retired_pending_ack, pending_head)
        self.assertFalse(binding[1].requests.connected)
        self.assertFalse(unit.motion_available)

    def test_retirement_uses_matching_observer_recovery_head(self):
        for eligible in (False, True):
            with self.subTest(eligible=eligible):
                observer, unit = self.make_bound()
                binding = unit._event_binding
                requests, delivery = binding[1].requests, binding[1].delivery
                head = object()
                observer._host_recovery_record = HostRecoveryRecord(
                    requests.instance, requests.session, binding[4][0],
                    binding[5], head, eligible, ())
                delivery.disconnect()
                observer._prepared_host_session = None
                unit.retire_event_binding()
                self.assertIs(unit._retired_pending_ack, head)
                self.assertIs(unit._retired_pending_ack_eligible, eligible)

    def test_retirement_ignores_other_session_recovery_head(self):
        observer, unit = self.make_bound()
        binding = unit._event_binding
        requests, delivery = binding[1].requests, binding[1].delivery
        observer._host_recovery_record = HostRecoveryRecord(
            requests.instance, requests.session + 1, binding[4][0],
            binding[5], object(), True, ())
        delivery.disconnect()
        unit.retire_event_binding()
        self.assertIsNone(unit._retired_pending_ack)
        self.assertFalse(unit._retired_pending_ack_eligible)

    def test_explicit_retired_ack_clears_only_after_exact_observer_success(self):
        observer, unit = self.make_bound()
        binding = unit._event_binding
        requests = binding[1].requests
        head = Event(INSTANCE, Identity(HOST, 13, 42, LOAD, 2),
                     9, 100, 1, 0, 200, 0)
        record = HostRecoveryRecord(INSTANCE, 13, binding[4][0],
                                    binding[5], head, True, ())
        observer._host_recovery_record = record
        observer._prepared_host_session = None
        unit.retire_event_binding()
        self.assertEqual(unit._retired_pending_ack, head)

        observer.retry_retained_host_ack = Mock(side_effect=ValueError('timeout'))
        with self.assertRaisesRegex(ValueError, 'timeout'):
            unit.retry_retired_ack(timeout=.2)
        self.assertEqual(unit._retired_pending_ack, head)
        self.assertTrue(unit._retired_pending_ack_eligible)

        def confirmed(timeout):
            observer._host_recovery_record = replace(
                record, pending_ack=None, pending_ack_eligible=False)
            return True

        observer.retry_retained_host_ack = Mock(side_effect=confirmed)
        self.assertTrue(unit.retry_retired_ack(timeout=.2))
        observer.retry_retained_host_ack.assert_called_once_with(timeout=.2)
        self.assertIsNone(unit._retired_pending_ack)
        self.assertFalse(unit._retired_pending_ack_eligible)
        self.assertFalse(requests.connected)


if __name__ == '__main__':
    unittest.main()

    def test_gcode_reads_retained_terminal_after_disconnect(self):
        observer, unit = self.make_bound()
        requests = unit._event_binding[1].requests
        extra = KlipperMMU.__new__(KlipperMMU)
        extra._openams_unit = unit
        extra._printer = types.SimpleNamespace(command_error=ValueError)

        class Gcmd:
            def __init__(self, bay=None):
                self.bay, self.messages = bay, []
            def get(self, key, default=None):
                return self.bay if key == 'BAY' else default
            def get_int(self, key):
                return self.bay
            def respond_info(self, message):
                self.messages.append(message)

        # 1. Provide a terminal result on the connected binding
        identity = requests.issue_preload(2)
        requests.pending_preload = None
        requests.preload_terminal = types.SimpleNamespace(
            identity=identity, event_type=15, result_code=7)
        
        # 2. Make the binding go stale without side-effects of full refresh
        unit.retire_event_binding()
        
        # 3. Poll via gcode
        gcmd = Gcmd()
        extra.cmd_KLIPPER_MMU_PRELOAD(gcmd)
        self.assertIn('event=15 code=7', gcmd.messages[0])
