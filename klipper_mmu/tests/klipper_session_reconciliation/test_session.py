# SPDX-License-Identifier: GPL-3.0-or-later
import sys
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from host.protocol import SensorSample, SensorSnapshot, Snapshot
from host.session_reconciliation import (choose_host_session,
                                         choose_host_session_with_physical,
                                         choose_occupied_host_session)

INSTANCE = 0x1020304050607080


def snapshot(**overrides):
    values = dict(state=0, stage=0, operation=0, client=0,
                  queued_events=0, head_event_seq=0,
                  system_fault=0, motion_fault=0, velocity=0,
                  lane_active=False, terminal_pending=False,
                  path_occupied=False, position_uncertain=False,
                  pending_fault_event=False,
                  session=0, request=0, lane=255,
                  owner_lane=255, occupant_lane=255, acquired_ms=100,
                  client_sessions=(0, 12, 3, 4),
                  client_requests=(0, 0xfffffffe, 5, 6))
    values.update(overrides)
    return Snapshot(INSTANCE, 77, values)


def sensors(**overrides):
    clear = SensorSample(True, False, True, True, 100)
    values = dict(instance=INSTANCE, query=77, lane_count=2,
                  observed_ms=100, bays=(clear, clear), hub=clear,
                  toolhead=clear)
    values.update(overrides)
    return SensorSnapshot(**values)


class SessionTest(unittest.TestCase):
    def test_occupied_quiescent_path_selects_new_session_and_lane(self):
        present = replace(sensors().hub, present=True)
        selected = choose_occupied_host_session(
            snapshot(state=2, path_occupied=True, occupant_lane=1),
            sensors(bays=(sensors().hub, present), hub=present), INSTANCE, 2)
        self.assertEqual(selected.lane, 1)
        self.assertEqual(selected.session_start,
                         choose_host_session(snapshot(), INSTANCE))

    def test_occupied_gate_rejects_busy_faulted_and_event_states(self):
        present = replace(sensors().hub, present=True)
        observed = sensors(bays=(sensors().hub, present), hub=present)
        for changes in (dict(state=8), dict(state=4), dict(state=0),
                        dict(stage=3), dict(operation=1), dict(client=1),
                        dict(lane_active=True), dict(velocity=1),
                        dict(terminal_pending=True), dict(queued_events=1),
                        dict(head_event_seq=1), dict(system_fault=1),
                        dict(motion_fault=1), dict(position_uncertain=True),
                        dict(pending_fault_event=True)):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                values = dict(state=2, path_occupied=True, occupant_lane=1)
                values.update(changes)
                choose_occupied_host_session(snapshot(**values), observed,
                                             INSTANCE, 2)

    def test_occupied_gate_rejects_unknown_wrong_or_stale_physical_data(self):
        clear = sensors().hub
        present = replace(clear, present=True)
        status = snapshot(state=2, path_occupied=True, occupant_lane=1)
        coherent = sensors(bays=(clear, present), hub=present)
        for observed in (sensors(),
                         sensors(bays=(present, clear), hub=present),
                         sensors(bays=(clear, replace(present, valid=False)),
                                 hub=present),
                         sensors(bays=(clear, present), hub=present, query=78),
                         sensors(bays=(clear, present), hub=present,
                                 observed_ms=99),
                         sensors(bays=(clear, present), hub=present,
                                 instance=INSTANCE + 1)):
            with self.subTest(observed=observed), self.assertRaises(ValueError):
                choose_occupied_host_session(status, observed, INSTANCE, 2)
        for changes in (dict(occupant_lane=0), dict(occupant_lane=255),
                        dict(occupant_lane=True), dict(acquired_ms=-1),
                        dict(client_sessions=(0, 0xffffffff, 0, 0)),
                        dict(client_requests=(0, -1, 0, 0)),
                        dict(state=True), dict(path_occupied=1),
                        dict(session=-1), dict(request=1 << 32),
                        dict(lane=True), dict(owner_lane=256)):
            with self.subTest(changes=changes), self.assertRaises(
                    (ValueError, OverflowError)):
                values = dict(state=2, path_occupied=True, occupant_lane=1)
                values.update(changes)
                choose_occupied_host_session(snapshot(**values), coherent,
                                             INSTANCE, 2)
        with self.assertRaises(ValueError):
            choose_occupied_host_session(status, coherent, INSTANCE + 1, 2)

    def test_clean_snapshot_advances_session_and_resets_request(self):
        selected = choose_host_session(snapshot(), INSTANCE)
        self.assertEqual((selected.instance, selected.snapshot_query,
                          selected.session, selected.last_request),
                         (INSTANCE, 77, 13, 0))

    def test_busy_or_physical_path_rejected(self):
        for changes in (dict(state=1), dict(stage=3), dict(operation=1),
                        dict(client=1), dict(velocity=10),
                        dict(lane_active=True), dict(path_occupied=True),
                        dict(position_uncertain=True)):
            with self.subTest(changes=changes), self.assertRaisesRegex(
                    ValueError, 'physical or event reconciliation'):
                choose_host_session(snapshot(**changes), INSTANCE)

    def test_fault_and_event_hazards_rejected(self):
        for changes in (dict(system_fault=1), dict(motion_fault=2),
                        dict(queued_events=1), dict(head_event_seq=5),
                        dict(terminal_pending=True), dict(pending_fault_event=True)):
            with self.subTest(changes=changes), self.assertRaisesRegex(
                    ValueError, 'physical or event reconciliation'):
                choose_host_session(snapshot(**changes), INSTANCE)

    def test_high_water_and_exhaustion(self):
        high = snapshot(client_sessions=(0, 0xfffffffe, 0, 0),
                        client_requests=(0, 0xffffffff, 0, 0))
        self.assertEqual(choose_host_session(high, INSTANCE).session, 0xffffffff)
        with self.assertRaisesRegex(OverflowError, 'exhausted'):
            choose_host_session(snapshot(client_sessions=(0, 0xffffffff, 0, 0)),
                                INSTANCE)

    def test_wrong_instance_and_malformed_fields(self):
        with self.assertRaisesRegex(ValueError, 'same-instance'):
            choose_host_session(snapshot(), INSTANCE + 1)
        for changes in (dict(client_sessions=(0, 1)),
                        dict(client_requests=(0, -1, 0, 0)),
                        dict(position_uncertain=0), dict(state=True),
                        dict(client_sessions=(0, True, 0, 0))):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                choose_host_session(snapshot(**changes), INSTANCE)

    def test_physical_gate_accepts_coherent_empty_path(self):
        selected = choose_host_session_with_physical(snapshot(), sensors(),
                                                     INSTANCE, 2)
        self.assertEqual(selected, choose_host_session(snapshot(), INSTANCE))

    def test_fps_profile_without_toolhead_switch_can_start_sessions(self):
        # FPS arrival configures no toolhead switch; HUB alone proves the path.
        absent = SensorSample(False, False, False, False, 0)
        self.assertEqual(choose_host_session_with_physical(
            snapshot(), sensors(toolhead=absent), INSTANCE, 2),
            choose_host_session(snapshot(), INSTANCE))
        present = replace(sensors().hub, present=True)
        selected = choose_occupied_host_session(
            snapshot(state=2, path_occupied=True, occupant_lane=1),
            sensors(bays=(sensors().hub, present), hub=present, toolhead=absent),
            INSTANCE, 2)
        self.assertEqual(selected.lane, 1)
        with self.assertRaisesRegex(ValueError, 'not proven empty'):
            choose_host_session_with_physical(
                snapshot(), sensors(hub=present, toolhead=absent), INSTANCE, 2)

    def test_staged_lane_load_switch_does_not_occupy_shared_path(self):
        clear = sensors().hub
        present = replace(clear, present=True)
        observed = sensors(bays=(present, present), loads=(present, present))
        self.assertEqual(choose_host_session_with_physical(
            snapshot(), observed, INSTANCE, 2),
            choose_host_session(snapshot(), INSTANCE))

    def test_virtual_hub_cold_boot_proceeds_on_an_invalid_hub_sample(self):
        # A virtual-hub image's own hub reading is invalid while idle (no
        # lane owns the shared path yet -- a fresh boot's own shape,
        # src/operation_core.c virtual_hub_lane); every LOAD switch is its
        # own directly-read sensor and proves the path empty instead.
        invalid_hub = SensorSample(True, False, False, False, 0)
        clear_loads = (sensors().hub, sensors().hub)  # configured, absent, valid, fresh
        selected = choose_host_session_with_physical(
            snapshot(), sensors(hub=invalid_hub, loads=clear_loads), INSTANCE, 2,
            virtual_hub=True)
        self.assertEqual(selected, choose_host_session(snapshot(), INSTANCE))
        # Without virtual_hub the same invalid hub sample still blocks it.
        with self.assertRaises(ValueError):
            choose_host_session_with_physical(
                snapshot(), sensors(hub=invalid_hub, loads=clear_loads), INSTANCE, 2)
        # A load present without an owner is not proven empty either way.
        present_load = replace(sensors().hub, present=True)
        with self.assertRaises(ValueError):
            choose_host_session_with_physical(
                snapshot(), sensors(hub=invalid_hub,
                                    loads=(sensors().hub, present_load)),
                INSTANCE, 2, virtual_hub=True)

    def test_physical_gate_rejects_presence_unknown_and_distinct_captures(self):
        clear = sensors().hub
        bad_samples = (replace(clear, configured=False),
                       replace(clear, valid=False),
                       replace(clear, fresh=False))
        cases = [
            (snapshot(), sensors(hub=replace(clear, present=True))),
            (snapshot(), sensors(toolhead=replace(clear, present=True))),
            (snapshot(), sensors(loads=(clear, replace(clear, present=True)))),
            (snapshot(), sensors(query=78)),
            (snapshot(acquired_ms=99), sensors()),
            (snapshot(), sensors(instance=INSTANCE + 1)),
            (snapshot(), sensors(lane_count=1)),
            (snapshot(path_occupied=True), sensors()),
            (snapshot(queued_events=1), sensors()),
            (snapshot(system_fault=1), sensors()),
        ]
        for bad in bad_samples:
            cases.append((snapshot(), sensors(hub=bad)))
            # An unconfigured BAY (like an unconfigured toolhead) is optional;
            # a configured one must still be good.
            if bad.configured:
                cases.append((snapshot(), sensors(bays=(bad, clear))))
                cases.append((snapshot(), sensors(toolhead=bad)))
        for status, observed in cases:
            with self.subTest(status=status, sensors=observed), self.assertRaises(ValueError):
                choose_host_session_with_physical(status, observed, INSTANCE, 2)

    def test_unconfigured_bay_does_not_block_a_session(self):
        # A device with no per-lane BAY switch at all (Angry Beaver, 3MS,
        # QuattroBox 1.0/1.1/2) still needs a usable session.
        clear = sensors().hub
        unconfigured_bay = replace(clear, configured=False, valid=False,
                                   fresh=False)
        observed = sensors(bays=(unconfigured_bay, unconfigured_bay))
        self.assertEqual(choose_host_session_with_physical(
            snapshot(), observed, INSTANCE, 2),
            choose_host_session(snapshot(), INSTANCE))


if __name__ == '__main__':
    unittest.main()
