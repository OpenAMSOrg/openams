# SPDX-License-Identifier: GPL-3.0-or-later
import unittest
from dataclasses import replace

from host.openams_device_state import OpenAMSDeviceState
from host.protocol import Identity, SensorSample, SensorSnapshot, Snapshot


INSTANCE = 0x123456789


def captured(query=1, occupied=True, occupant=2, hub=True, ready=True,
             acquired_ms=100, lane_count=4, **overrides):
    state = dict(path_occupied=occupied, lane_active=occupied,
                 terminal_pending=False, position_uncertain=False,
                 pending_fault_event=False, owner_lane=occupant,
                 occupant_lane=occupant, system_fault=0, motion_fault=0,
                 stage=0, state=0,
                 follower_state=0, follower_direction=0, follower_fault=0,
                 acquired_ms=acquired_ms)
    state.update(overrides)
    status = Snapshot(INSTANCE, query, state)
    bays = tuple(SensorSample(True, i % 2 == 0, True, ready, acquired_ms)
                 for i in range(lane_count))
    shared = SensorSample(True, hub, True, ready, acquired_ms)
    absent = SensorSample(True, False, True, ready, acquired_ms)
    sensors = SensorSnapshot(INSTANCE, query, lane_count, acquired_ms,
                             bays, shared, absent)
    return status, sensors


class DeviceStateTests(unittest.TestCase):
    def setUp(self):
        self.state = OpenAMSDeviceState(7, 'fps0', 'boxturtle', 4, 842.5)
        self.state.begin_connection(INSTANCE, 3)

    def test_known_and_proven_empty_publish_truthful_arrays(self):
        self.assertTrue(self.state.observe(*captured(), 3, True))
        self.assertTrue(self.state.hardware_snapshot_ready)
        self.assertEqual(self.state.f1s_hes_value, (True, False, True, False))
        self.assertEqual(self.state.load_present, (None,) * 4)
        self.assertEqual(self.state.hub_hes_value, (False, False, True, False))
        self.assertTrue(self.state.observe(*captured(
            query=2, occupied=False, hub=False), 3, True))
        self.assertEqual(self.state.hub_hes_value, (False,) * 4)

    def test_staged_load_switch_is_not_shared_path_occupancy(self):
        status, sensors = captured(occupied=False, hub=False)
        staged = SensorSample(True, True, True, True, 100)
        loads = (staged, sensors.hub, staged, sensors.hub)
        self.assertTrue(self.state.observe(status, replace(sensors, loads=loads),
                                           3, True))
        self.assertTrue(self.state.hardware_snapshot_ready)
        self.assertEqual(self.state.load_present, (True, False, True, False))
        self.assertEqual(self.state.hub_hes_value, (False,) * 4)
        next_status, next_sensors = captured(query=2, occupied=False,
                                             hub=False, acquired_ms=101)
        bad_loads = (next_sensors.hub, replace(staged, sample_ms=101),
                     next_sensors.hub, next_sensors.hub)
        self.assertFalse(self.state.observe(next_status,
                                            replace(next_sensors, loads=bad_loads),
                                            3, True))

    def test_active_load_before_hub_stays_ready_without_claiming_hub(self):
        fields = dict(occupied=False, hub=False, lane_active=True,
                      terminal_pending=True, owner_lane=2,
                      occupant_lane=255, client=1, session=7, request=12,
                      lane=2, operation=1, state=1, stage=1,
                      client_sessions=(0, 7, 0, 0),
                      client_requests=(0, 12, 0, 0))
        status, sensors = captured(**fields)
        loads = tuple(SensorSample(True, bay == 2, True, True, 100)
                      for bay in range(4))
        self.assertTrue(self.state.observe(status, replace(sensors, loads=loads),
                                           3, True))
        self.assertEqual(self.state.hub_hes_value, (False,) * 4)
        self.assertIsNone(self.state.local_reserved_lane)
        self.assertEqual(self.state.owner_identity, Identity(1, 7, 12, 1, 2))

    def test_load_sensor_remains_distinct_and_clears_on_disconnect(self):
        status, sensors = captured()
        loads = (SensorSample(True, False, True, True, 100),
                 SensorSample(True, True, True, True, 100),
                 SensorSample(True, False, True, True, 100)) + sensors.loads[3:]
        self.assertTrue(self.state.observe(status, replace(sensors, loads=loads),
                                           3, True))
        self.assertEqual(self.state.load_present, (False, True, False, None))
        self.assertEqual(self.state.f1s_hes_value, (True, False, True, False))
        self.assertEqual(self.state.hub_hes_value, (False, False, True, False))
        stale = list(loads)
        stale[2] = SensorSample(True, True, False, False, 100)
        next_status, next_sensors = captured(query=2)
        self.assertFalse(self.state.observe(
            next_status, replace(next_sensors, loads=tuple(stale)), 3, True))
        self.assertFalse(self.state.hardware_snapshot_ready)
        self.assertIsNone(self.state.load_present)
        self.state.disconnect()
        self.assertIsNone(self.state.load_present)

    def test_local_preload_keeps_hub_clear_and_reserves_its_lane(self):
        status, sensors = captured(hub=False, lane_active=False,
                                   state=8, stage=0, operation=0)
        loads = tuple(SensorSample(True, bay == 2, True, True, 100)
                      for bay in range(4))
        self.assertTrue(self.state.observe(status, replace(sensors, loads=loads),
                                           3, True))
        self.assertEqual(self.state.local_reserved_lane, 2)
        self.assertEqual(self.state.hub_hes_value, (False,) * 4)
        self.assertEqual(self.state.load_present, (False, False, True, False))

    def test_active_load_owner_requires_full_reconciled_host_identity(self):
        fields = dict(client=1, session=7, request=12, operation=1, lane=2,
                      client_sessions=(0, 7, 0, 0),
                      client_requests=(0, 12, 0, 0))
        self.assertTrue(self.state.observe(*captured(**fields), 3, True))
        self.assertEqual(self.state.owner_identity, Identity(1, 7, 12, 1, 2))
        self.assertTrue(self.state.observe(*captured(query=2, **dict(
            fields, operation=3)), 3, True))
        self.assertEqual(self.state.owner_identity, Identity(1, 7, 12, 3, 2))
        changes = (dict(lane_active=False), dict(owner_lane=1),
                   dict(client=2), dict(client=True),
                   dict(session=0), dict(request=0),
                   dict(operation=2), dict(lane=1),
                   dict(client_sessions=(0, 8, 0, 0)),
                   dict(client_requests=(0, 11, 0, 0)))
        for query, change in enumerate(changes, start=3):
            with self.subTest(change=change):
                self.state.observe(*captured(query=query, **dict(
                    fields, **change)), 3, True)
                self.assertIsNone(self.state.owner_identity)
        self.assertTrue(self.state.observe(*captured(
            query=13, **fields), 3, True))
        self.assertIsNotNone(self.state.owner_identity)
        self.assertFalse(self.state.observe(*captured(
            query=14, occupant=255, **fields), 3, True))
        self.assertIsNone(self.state.owner_identity)

    def test_follower_observation_needs_exact_owner_and_safe_capture(self):
        fields = dict(client=1, session=7, request=12, operation=3, lane=2,
                      stage=3, state=4,
                      client_sessions=(0, 7, 0, 0),
                      client_requests=(0, 12, 0, 0))
        self.assertTrue(self.state.observe(*captured(
            follower_state=1, follower_direction=1, **fields), 3, True))
        seen = self.state.follower_observation
        self.assertEqual((seen.enabled, seen.direction, seen.bay), (True, 1, 2))
        self.assertEqual((seen.instance, seen.generation, seen.query),
                         (INSTANCE, 3, 1))
        self.assertTrue(self.state.observe(*captured(
            query=2, follower_state=2, **fields), 3, True))
        self.assertTrue(self.state.follower_observation.enabled)
        cases = (dict(client=2), dict(owner_lane=1),
                 dict(lane=1), dict(follower_fault=1),
                 dict(follower_state=3), dict(follower_direction=2),
                 dict(stage=2), dict(state=2))
        for query, change in enumerate(cases, start=3):
            with self.subTest(change=change):
                self.state.observe(*captured(query=query,
                                             **dict(dict(fields, follower_state=1),
                                                    **change)), 3, True)
                self.assertIsNone(self.state.follower_observation)
        self.assertTrue(self.state.observe(*captured(
            query=11, follower_state=1,
            **dict(fields, operation=1)), 3, True))
        self.assertTrue(self.state.follower_observation.enabled)
        self.assertFalse(self.state.observe(*captured(
            query=12, follower_state=1, system_fault=1, **fields), 3, True))
        self.assertIsNone(self.state.follower_observation)

    def test_load_admission_without_follow_stage_cannot_confirm_following(self):
        fields = dict(client=1, session=7, request=12, operation=1, lane=2,
                      client_sessions=(0, 7, 0, 0),
                      client_requests=(0, 12, 0, 0))
        self.assertTrue(self.state.observe(*captured(
            follower_state=1, **fields), 3, True))
        self.assertIsNone(self.state.follower_observation)

    def test_disarm_can_be_observed_without_a_bay_but_cannot_invent_one(self):
        self.assertTrue(self.state.observe(*captured(
            occupied=False, hub=False), 3, True))
        seen = self.state.follower_observation
        self.assertEqual((seen.enabled, seen.direction, seen.bay),
                         (False, None, None))
        self.assertTrue(self.state.observe(*captured(
            query=2, follower_state=0), 3, True))
        self.assertEqual(self.state.follower_observation.bay, 2)
        self.assertFalse(self.state.observe(*captured(
            query=3, occupant=255), 3, True))
        self.assertIsNone(self.state.follower_observation)

    def test_unknown_or_stale_capture_clears_readiness_without_false_absence(self):
        self.assertTrue(self.state.observe(*captured(), 3, True))
        self.assertFalse(self.state.observe(*captured(
            query=2, occupant=255), 3, True))
        self.assertFalse(self.state.hardware_snapshot_ready)
        self.assertIsNone(self.state.f1s_hes_value)
        self.assertIsNone(self.state.hub_hes_value)
        self.assertFalse(self.state.observe(*captured(
            query=3, ready=False), 3, True))
        status, sensors = captured(query=4)
        self.assertFalse(self.state.observe(status, replace(
            sensors, query=5), 3, True))
        self.assertFalse(self.state.hardware_snapshot_ready)

    def test_connection_generation_and_replay_guard(self):
        self.assertTrue(self.state.observe(*captured(), 3, True))
        self.state.begin_connection(INSTANCE + 1, 4)
        self.assertFalse(self.state.observe(*captured(query=2), 3, True))
        self.assertFalse(self.state.hardware_snapshot_ready)
        self.state.begin_connection(INSTANCE, 5)
        self.assertTrue(self.state.observe(*captured(), 5, True))
        self.assertFalse(self.state.observe(*captured(query=2), 4, True))
        self.assertTrue(self.state.hardware_snapshot_ready)
        self.assertFalse(self.state.observe(*captured(), 5, True))
        self.assertFalse(self.state.hardware_snapshot_ready)
        self.assertFalse(self.state.observe(*captured(query=2), 5, False))
        self.assertIsNone(self.state._instance)

    def test_emu_five_lane_virtual_hub_keeps_local_load_lane_specific(self):
        emu = OpenAMSDeviceState(7, 'fps0', 'emu', 5, 842.5)
        emu.begin_connection(INSTANCE, 9)
        status, sensors = captured(query=1, occupied=True, occupant=4,
                                   hub=False, lane_count=5,
                                   lane_active=False, state=8, stage=0,
                                   operation=0)
        loads = tuple(SensorSample(True, lane == 4, True, True, 100)
                      for lane in range(5))
        self.assertTrue(emu.observe(status, replace(sensors, loads=loads),
                                    9, True))
        self.assertEqual(emu.local_reserved_lane, 4)
        self.assertEqual(emu.load_present, (False, False, False, False, True))
        self.assertEqual(emu.hub_hes_value, (False,) * 5)

        # A virtual hub with no asserted LOAD and no occupied path is empty.
        empty, empty_sensors = captured(query=2, occupied=False, occupant=255,
                                        hub=False, lane_count=5,
                                        acquired_ms=101)
        empty_loads = tuple(SensorSample(True, False, True, True, 101)
                            for _ in range(5))
        self.assertTrue(emu.observe(
            empty, replace(empty_sensors, loads=empty_loads), 9, True))
        self.assertEqual(emu.hub_hes_value, (False,) * 5)

        # Missing/stale per-lane evidence cannot be inferred empty.
        stale_loads = list(empty_loads)
        stale_loads[3] = SensorSample(True, False, True, False, 101)
        stale, stale_sensors = captured(query=3, occupied=False, occupant=255,
                                        hub=False, lane_count=5,
                                        acquired_ms=102)
        self.assertFalse(emu.observe(
            stale, replace(stale_sensors, loads=tuple(stale_loads)), 9, True))
        self.assertFalse(emu.hardware_snapshot_ready)

    def test_requires_configured_identity_topology_and_path_length(self):
        for args in ((-1, 'fps0', 'boxturtle', 4, 800),
                     (1, '', 'boxturtle', 4, 800),
                     (1, 'fps0', '', 4, 800),
                     (1, 'fps0', 'boxturtle', 0, 800),
                     (1, 'fps0', 'boxturtle', 4, 0),
                     (1, 'fps0', 'boxturtle', 4, float('nan'))):
            with self.subTest(args=args), self.assertRaises(ValueError):
                OpenAMSDeviceState(*args)


if __name__ == '__main__':
    unittest.main()
