# SPDX-License-Identifier: GPL-3.0-or-later
import threading
import unittest
from dataclasses import replace
from unittest.mock import patch

from host import KlipperMMU
from host.openams_device import OpenAMSDevice
from host.protocol import Identity, SensorSample, SensorSnapshot, Snapshot


INSTANCE = 0x123456789


class FakeMCU:
    def is_fileoutput(self):
        return False


class FakeReactor:
    def __init__(self):
        self.now = 10.0

    def monotonic(self):
        return self.now


class FakePrinter:
    command_error = ValueError

    def __init__(self):
        self.reactor = FakeReactor()

    def get_reactor(self):
        return self.reactor


class FakeObserver(KlipperMMU):
    def __init__(self):
        self._active = True
        self._connected = True
        self._state = 'attached'
        self._instance = INSTANCE
        self._generation = 3
        self._last_query = 0
        self._snapshot_query = 0
        self._last_error = None
        self._mcu_lock = threading.Lock()
        self._prepared_host_session = None
        self._host_lease = None
        self._host_session_ambiguous = False
        self._host_recovery_record = None
        self._mcu = FakeMCU()
        self._printer = FakePrinter()

    def _observe(self):
        pass



def capture(query, occupied=True, occupant=2, valid=True, **overrides):
    values = dict(path_occupied=occupied, lane_active=occupied,
                  terminal_pending=False, position_uncertain=False,
                  pending_fault_event=False, owner_lane=occupant,
                  occupant_lane=occupant, system_fault=0, motion_fault=0,
                  stage=0, state=0,
                  follower_state=0, follower_direction=0, follower_fault=0,
                  acquired_ms=100)
    values.update(overrides)
    sample = lambda present: SensorSample(True, present, True, valid, 100)
    status = Snapshot(INSTANCE, query, values)
    sensors = SensorSnapshot(INSTANCE, query, 4, 100,
                             tuple(map(sample, (True, False, True, False))),
                             sample(occupied), sample(False))
    return status, sensors


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.observer = FakeObserver()
        self.device = OpenAMSDevice('oams 7', self.observer, 7, 'fps',
                                    'boxturtle', 4, 842.5)

    def test_identity_and_no_readiness_before_reconciled_capture(self):
        self.assertEqual(self.device.oams_idx, 7)
        self.assertEqual(self.device.filament_path_length, 842.5)
        self.assertEqual(self.device.path_length_mm, 842.5)
        self.assertTrue(self.device.connected)
        self.assertFalse(self.device.firmware_owns_liveness)
        self.assertIsNone(self.device.protocol_version)
        self.assertIn('physical observation', self.device.protocol_summary())
        self.assertFalse(self.device.hardware_snapshot_ready)
        self.assertIsNone(self.device.f1s_hes_value)
        with self.assertRaisesRegex(ValueError, 'unknown'):
            self.device.is_bay_ready(0)
        webhook = self.device.get_webhook_status()
        self.assertTrue(webhook['connected'])
        self.assertFalse(webhook['hardware_snapshot_ready'])
        self.assertIsNone(webhook['hub_hes_value'])
        self.assertIsNone(webhook['load_present'])
        self.assertIsNone(self.device.local_reserved_lane)
        with patch('host.read_physical_state',
                   side_effect=lambda *args, **kwargs: capture(args[3])):
            self.assertTrue(self.device.refresh())
        self.assertEqual(self.device.f1s_hes_value,
                         (True, False, True, False))
        self.assertEqual(self.device.hub_hes_value,
                         (False, False, True, False))
        self.assertEqual(self.device.current_spool, 2)
        self.assertTrue(self.device.is_bay_loaded(2))
        self.assertEqual(self.device.get_webhook_status()['hub_hes_value'],
                         (False, False, True, False))
        self.assertEqual(self.device.get_status(None)['load_present'],
                         (None,) * 4)

    def test_optional_load_is_published_without_changing_prep_or_hub(self):
        def with_load(*args, **kwargs):
            status, sensors = capture(args[3])
            loads = (SensorSample(True, False, True, True, 100),
                     SensorSample(True, True, True, True, 100)) + sensors.loads[2:]
            return status, replace(sensors, loads=loads)
        with patch('host.read_physical_state', side_effect=with_load):
            self.assertTrue(self.device.refresh())
        self.assertEqual(self.device.get_status(None)['load_present'],
                         (False, True, None, None))
        self.assertEqual(self.device.get_webhook_status()['load_present'],
                         (False, True, None, None))
        self.assertEqual(self.device.f1s_hes_value, (True, False, True, False))
        self.assertEqual(self.device.hub_hes_value, (False, False, True, False))

    def test_fps_mirror_uses_only_fresh_valid_buffer_position(self):
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            _ = self.device.fps_value
        with patch('host.read_physical_state', side_effect=lambda *args, **kwargs: capture(
                args[3], buffer_valid=True, buffer_position=375)):
            self.assertTrue(self.device.refresh())
        self.assertEqual(self.device.fps_value, 0.375)
        self.observer._printer.reactor.now += 2.0
        with self.assertRaisesRegex(ValueError, 'unavailable'):
            _ = self.device.fps_value
        with patch('host.read_physical_state', side_effect=lambda *args, **kwargs: capture(
                args[3], buffer_valid=False, buffer_position=375)):
            self.assertTrue(self.device.refresh())
        with self.assertRaisesRegex(ValueError, 'invalid'):
            _ = self.device.fps_value
        with patch('host.read_physical_state', side_effect=lambda *args, **kwargs: capture(
                args[3], buffer_valid=True, buffer_position=1001)):
            self.assertTrue(self.device.refresh())
        with self.assertRaisesRegex(ValueError, 'invalid'):
            _ = self.device.fps_value

    def test_local_preload_reservation_expires_with_capture(self):
        def local(*args, **kwargs):
            status, sensors = capture(args[3], occupied=True,
                                      lane_active=False, state=8, stage=0,
                                      operation=0)
            sensors = replace(sensors, hub=SensorSample(
                True, False, True, True, 100))
            loads = tuple(SensorSample(True, bay == 2, True, True, 100)
                          for bay in range(4))
            return status, replace(sensors, loads=loads)
        with patch('host.read_physical_state', side_effect=local):
            self.assertTrue(self.device.refresh())
        self.assertEqual(self.device.local_reserved_lane, 2)
        self.assertEqual(self.device.hub_hes_value, (False,) * 4)
        self.observer._printer.reactor.now += 2.0
        self.assertIsNone(self.device.local_reserved_lane)

    def test_unknown_and_disconnection_never_masquerade_as_empty(self):
        with patch('host.read_physical_state',
                   side_effect=lambda *args, **kwargs: capture(args[3], valid=False)):
            self.assertFalse(self.device.refresh())
        self.assertIsNone(self.device.hub_hes_value)
        with patch('host.read_physical_state',
                   side_effect=lambda *args, **kwargs: capture(args[3])):
            self.assertTrue(self.device.refresh())
        self.observer._generation += 1
        self.assertFalse(self.device.hardware_snapshot_ready)
        self.assertIsNone(self.device.hub_hes_value)
        with self.assertRaisesRegex(ValueError, 'unknown'):
            self.device.is_bay_loaded(2)
        self.assertTrue(self.device.connected)
        self.observer._connected = False
        self.assertFalse(self.device.connected)

    def test_generation_change_during_capture_drops_result(self):
        def disconnect(*args, **kwargs):
            self.observer._generation += 1
            self.observer._connected = False
            return capture(args[3])

        with patch('host.read_physical_state',
                   side_effect=disconnect):
            self.assertFalse(self.device.refresh())
        self.assertFalse(self.device.hardware_snapshot_ready)

    def test_failure_releases_guard_and_clears_previous_capture(self):
        with patch('host.read_physical_state',
                   side_effect=lambda *args, **kwargs: capture(args[3])):
            self.assertTrue(self.device.refresh())
        with patch('host.read_physical_state',
                   side_effect=ValueError('timeout')):
            with self.assertRaisesRegex(ValueError, 'timeout'):
                self.device.refresh()
        self.assertFalse(self.observer._mcu_lock.locked())
        self.assertFalse(self.device.hardware_snapshot_ready)
        self.assertIsNone(self.device.hub_hes_value)

    def test_refresh_waits_for_an_inflight_exchange_and_keeps_the_cache(self):
        with patch('host.read_physical_state',
                   side_effect=lambda *args, **kwargs: capture(args[3])):
            self.assertTrue(self.device.refresh())
        previous = self.device._captured_status
        acquired = self.device._captured_at
        self.observer._mcu_lock.acquire()  # For example, START awaiting a reply.
        result = []
        with patch('host.read_physical_state',
                   side_effect=lambda *args, **kwargs: capture(args[3], occupant=0)):
            waiting = threading.Thread(
                target=lambda: result.append(self.device.refresh()))
            waiting.start()
            waiting.join(0.05)
            # Waiting is not a failure: the prior capture keeps its identity/age.
            self.assertTrue(self.device.hardware_snapshot_ready)
            self.assertIs(self.device._captured_status, previous)
            self.assertEqual(self.device._captured_at, acquired)
            self.assertEqual(self.device.current_spool, 2)
            self.observer._mcu_lock.release()
            waiting.join(5)
        self.assertEqual(result, [True])
        self.assertGreater(self.device._captured_status.query, previous.query)
        self.assertEqual(self.device.current_spool, 0)

    def test_identity_change_is_never_hidden_by_the_cache(self):
        with patch('host.read_physical_state',
                   side_effect=lambda *args, **kwargs: capture(args[3])):
            self.assertTrue(self.device.refresh())
        self.observer._generation += 1
        self.assertFalse(self.device.hardware_snapshot_ready)
        self.assertIsNone(self.device.current_spool)

    def test_capture_expires_without_polling_and_refresh_recovers(self):
        with patch('host.read_physical_state',
                   side_effect=lambda *args, **kwargs: capture(args[3])):
            self.assertTrue(self.device.refresh())
            clock = self.observer._printer.reactor
            clock.now += 1.99
            self.assertTrue(self.device.hardware_snapshot_ready)
            clock.now += 0.01
            self.assertFalse(self.device.hardware_snapshot_ready)
            self.assertIsNone(self.device.f1s_hes_value)
            self.assertIsNone(self.device.current_spool)
            with self.assertRaisesRegex(ValueError, 'unknown'):
                self.device.is_bay_loaded(2)
            self.assertTrue(self.device.refresh())
            self.assertTrue(self.device.hardware_snapshot_ready)
            self.assertTrue(self.device.is_bay_loaded(2))

    def test_owner_identity_expires_and_disappears_on_disconnect_or_fault(self):
        fields = dict(client=1, session=7, request=12, operation=1, lane=2,
                      client_sessions=(0, 7, 0, 0),
                      client_requests=(0, 12, 0, 0))
        with patch('host.read_physical_state',
                   side_effect=lambda *args, **kwargs: capture(args[3], **fields)):
            self.assertTrue(self.device.refresh())
        self.assertEqual(self.device.owner_identity(), Identity(1, 7, 12, 1, 2))
        self.observer._printer.reactor.now += 2.0
        self.assertIsNone(self.device.owner_identity())
        with patch('host.read_physical_state',
                   side_effect=lambda *args, **kwargs: capture(args[3], **fields)):
            self.assertTrue(self.device.refresh())
        self.observer._generation += 1
        self.assertIsNone(self.device.owner_identity())
        with patch('host.read_physical_state',
                   side_effect=lambda *args, **kwargs: capture(args[3], **fields)):
            self.assertTrue(self.device.refresh())
        with patch('host.read_physical_state',
                   side_effect=lambda *args, **kwargs: capture(args[3], system_fault=1,
                                                     **fields)):
            self.assertFalse(self.device.refresh())
        self.assertIsNone(self.device.owner_identity())
        self.device.disconnect()
        self.assertIsNone(self.device.owner_identity())

    def test_follower_observation_expires_and_needs_new_capture(self):
        fields = dict(client=1, session=7, request=12, operation=3, lane=2,
                      stage=3, state=4,
                      client_sessions=(0, 7, 0, 0),
                      client_requests=(0, 12, 0, 0))
        with patch('host.read_physical_state',
                   side_effect=lambda *args, **kwargs: capture(args[3],
                       follower_state=1, follower_direction=1, **fields)):
            self.assertTrue(self.device.refresh())
        seen = self.device.follower_observation()
        self.assertEqual((seen.enabled, seen.direction, seen.bay), (True, 1, 2))
        self.observer._printer.reactor.now += 2.0
        self.assertIsNone(self.device.follower_observation())
        with patch('host.read_physical_state',
                   side_effect=lambda *args, **kwargs: capture(args[3], occupied=False,
                       owner_lane=255, occupant_lane=255)):
            self.assertTrue(self.device.refresh())
        stopped = self.device.follower_observation()
        self.assertEqual((stopped.enabled, stopped.direction, stopped.bay),
                         (False, None, None))
        self.assertGreater(stopped.query, seen.query)
        self.observer._generation += 1
        self.assertIsNone(self.device.follower_observation())

    def test_cache_ttl_requires_finite_positive_bound(self):
        for ttl in (0, -1, float('inf'), float('nan'), 61, True):
            with self.subTest(ttl=ttl), self.assertRaises(ValueError):
                OpenAMSDevice('oams 7', self.observer, 7, 'fps',
                              'boxturtle', 4, 842.5, cache_ttl=ttl)

    def test_delayed_read_cannot_publish_fresh_readiness(self):
        clock = self.observer._printer.reactor

        def delayed(*args, **kwargs):
            clock.now += 5.0
            return capture(args[3])

        with patch('host.read_physical_state',
                   side_effect=delayed):
            self.assertFalse(self.device.refresh())
        self.assertTrue(self.device.connected)
        self.assertFalse(self.device.hardware_snapshot_ready)
        self.assertIsNone(self.device.hub_hes_value)
        with self.assertRaisesRegex(ValueError, 'unknown'):
            self.device.is_bay_loaded(2)


class FakeGateMetadata:
    """A minimal stand-in for gate_metadata.GateMetadata: only the read
    accessor bay_loadable() actually calls."""

    def __init__(self, statuses=None):
        self._statuses = statuses or {}

    def gate_status(self, gate):
        return self._statuses.get(gate)


class BayLoadableTests(unittest.TestCase):
    """OpenAMS asks the unit whether a bay is loadable: a real BAY switch
    answers directly; a device with none defers to this MMU's own gate
    status, never fabricating a refusal."""

    def setUp(self):
        self.observer = FakeObserver()
        self.device = OpenAMSDevice('oams 7', self.observer, 7, 'fps',
                                    'boxturtle', 4, 842.5)

    def refresh_with_bay_configured(self, configured_mask):
        def with_bays(*args, **kwargs):
            status, sensors = capture(args[3])
            bays = tuple(replace(sample, configured=cfg)
                        for sample, cfg in zip(sensors.bays, configured_mask))
            return status, replace(sensors, bays=bays)
        with patch('host.read_physical_state', side_effect=with_bays):
            self.assertTrue(self.device.refresh())

    def test_real_switch_answers_directly(self):
        self.refresh_with_bay_configured((True, True, True, True))
        self.assertEqual(self.device.f1s_hes_value, (True, False, True, False))
        self.assertTrue(self.device.bay_loadable(0))
        self.assertFalse(self.device.bay_loadable(1))

    def test_no_switch_and_no_gate_metadata_is_left_attemptable(self):
        self.refresh_with_bay_configured((False, False, False, False))
        self.assertEqual(self.device.f1s_hes_value, (None, None, None, None))
        self.assertTrue(self.device.bay_loadable(1))  # never fabricated refused

    def test_no_switch_defers_to_gate_status(self):
        self.refresh_with_bay_configured((False, False, False, False))
        self.observer._gate_metadata = FakeGateMetadata({0: True, 1: False})
        self.assertTrue(self.device.bay_loadable(0))   # GATE_AVAILABLE
        self.assertFalse(self.device.bay_loadable(1))  # GATE_EMPTY
        self.assertTrue(self.device.bay_loadable(2))   # GATE_UNKNOWN: attemptable

    def test_a_lane_with_its_own_real_switch_ignores_gate_status(self):
        # Only bay 1 has no real switch. Bay 0's real reading is present
        # (capture()'s own fixture); a contradicting gate status note must
        # never override it.
        self.refresh_with_bay_configured((True, False, True, True))
        self.observer._gate_metadata = FakeGateMetadata({0: False, 1: False})
        self.assertTrue(self.device.bay_loadable(0))   # real switch: present
        self.assertFalse(self.device.bay_loadable(1))  # no switch: gate status

    def test_bounds_and_unready_device(self):
        with self.assertRaises(ValueError):
            self.device.bay_loadable(0)  # never refreshed: unknown state
        self.refresh_with_bay_configured((True, True, True, True))
        for bad in (-1, 4, 'x'):
            with self.assertRaises(IndexError):
                self.device.bay_loadable(bad)


if __name__ == '__main__':
    unittest.main()
