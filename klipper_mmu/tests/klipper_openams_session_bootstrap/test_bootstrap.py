# SPDX-License-Identifier: GPL-3.0-or-later
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from host.openams_session_bootstrap import (prepare_cold_host_session,
                                            prepare_occupied_host_session,
                                            prepare_preloaded_host_session)
from host.protocol import CommandResult, SensorSample, SensorSnapshot, Snapshot
from host.session_reconciliation import choose_preloaded_host_session

INSTANCE = 0x1020304050607080
GENERATION = 4


def status(**changes):
    fields = dict(state=0, stage=0, operation=0, client=0,
                  queued_events=0, head_event_seq=0, system_fault=0,
                  motion_fault=0, velocity=0, lane_active=False,
                  terminal_pending=False, path_occupied=False,
                  position_uncertain=False, pending_fault_event=False,
                  lane=255, session=0, request=0, owner_lane=255,
                  occupant_lane=255, acquired_ms=100,
                  client_sessions=(0, 12, 0, 0),
                  client_requests=(0, 0, 0, 0))
    fields.update(changes)
    return Snapshot(INSTANCE, 77, fields)


def sensors(**changes):
    clear = SensorSample(True, False, True, True, 100)
    fields = dict(instance=INSTANCE, query=77, lane_count=2,
                  observed_ms=100, bays=(clear, clear), hub=clear,
                  toolhead=clear)
    fields.update(changes)
    return SensorSnapshot(**fields)


class BootstrapTest(unittest.TestCase):
    def preload_observations(self):
        clear = sensors().hub
        present = replace(clear, present=True)
        return (status(state=8, path_occupied=True, occupant_lane=1,
                       owner_lane=255, session=0, request=0),
                sensors(bays=(clear, present), loads=(clear, present)))

    def test_preloaded_selection_requires_exact_quiescent_local_ownership(self):
        stopped, observed = self.preload_observations()
        selected = choose_preloaded_host_session(stopped, observed, INSTANCE, 2)
        self.assertEqual((selected.lane, selected.session_start.session,
                          selected.session_start.last_request), (1, 13, 0))
        # A device with no per-lane LOAD switch at all (every load reading
        # unconfigured -- QIDI Box's own shape, no `loads=` override here)
        # is proven the same way by BAY+HUB alone: `configured` reflects the
        # firmware's own build, never a transient/anomalous reading, so
        # "unconfigured everywhere" cannot mean "missing evidence for a
        # device that has some" the way a real switch reading False would.
        clear = sensors().hub
        present = replace(clear, present=True)
        no_load_switch = sensors(bays=(clear, present))
        selected = choose_preloaded_host_session(stopped, no_load_switch,
                                                  INSTANCE, 2)
        self.assertEqual((selected.lane, selected.session_start.session,
                          selected.session_start.last_request), (1, 13, 0))
        for changes in (dict(state=2), dict(stage=1), dict(operation=1),
                        dict(client=1), dict(lane_active=True),
                        dict(velocity=1), dict(queued_events=1),
                        dict(head_event_seq=1), dict(terminal_pending=True),
                        dict(system_fault=1), dict(motion_fault=1),
                        dict(position_uncertain=True),
                        dict(pending_fault_event=True),
                        dict(path_occupied=False), dict(occupant_lane=0),
                        dict(occupant_lane=255)):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                choose_preloaded_host_session(
                    status(**{**stopped.values, **changes}), observed,
                    INSTANCE, 2)
        for bad in (sensors(bays=(clear, present), loads=(present, present)),
                    sensors(bays=(clear, present), loads=(clear, clear)),
                    sensors(bays=(clear, present), loads=(clear, present),
                            hub=present),
                    sensors(bays=(clear, present), loads=(clear, present),
                            toolhead=present),
                    sensors(bays=(clear, present), loads=(clear, present),
                            query=78),
                    sensors(bays=(clear, present), loads=(clear, present),
                            observed_ms=99)):
            with self.subTest(sensors=bad), self.assertRaises(ValueError):
                choose_preloaded_host_session(stopped, bad, INSTANCE, 2)

    def test_emu_five_lane_virtual_hub_cold_and_local_preload_sessions(self):
        clear = sensors().hub
        five_clear = (clear,) * 5
        base_sensors = sensors(lane_count=5, bays=five_clear,
                               loads=(clear,) * 5, hub=clear)
        with patch('host.openams_session_bootstrap.send_heartbeat',
                   return_value=CommandResult(INSTANCE, 3, 13, 0)):
            cold = prepare_cold_host_session(
                object(), object(), status(), base_sensors, INSTANCE, 5,
                GENERATION, lambda i, g: (i, g) == (INSTANCE, GENERATION))
        self.assertEqual(cold.requests.session, 13)

        present = replace(clear, present=True)
        local_sensors = sensors(lane_count=5,
                                bays=five_clear[:4] + (present,),
                                loads=five_clear[:4] + (present,), hub=clear)
        local_status = status(state=8, path_occupied=True, occupant_lane=4)
        with patch('host.openams_session_bootstrap.send_heartbeat',
                   return_value=CommandResult(INSTANCE, 3, 13, 0)):
            local = prepare_preloaded_host_session(
                object(), object(), local_status, local_sensors, INSTANCE, 5,
                GENERATION, lambda i, g: (i, g) == (INSTANCE, GENERATION))
        self.assertEqual((local.lane, local.prepared.requests.session), (4, 13))

        # The local LOAD sensor does not prove shared-path occupancy for a
        # virtual hub. Do not resume an occupied route on that evidence alone.
        with patch('host.openams_session_bootstrap.send_heartbeat') as beat:
            with self.assertRaises(ValueError):
                prepare_occupied_host_session(
                    object(), object(), status(state=2, path_occupied=True,
                                              occupant_lane=4), local_sensors,
                    INSTANCE, 5, GENERATION,
                    lambda i, g: (i, g) == (INSTANCE, GENERATION))
            beat.assert_not_called()

    def test_emu_cold_boot_with_an_actually_invalid_hub_needs_virtual_hub(self):
        # src/operation_core.c's virtual_hub_lane reports the hub sample
        # itself invalid while idle (no lane owns the shared path -- a real
        # EMU cold boot's own shape), not merely absent. Without virtual_hub
        # this still blocks the cold session; with it, every LOAD switch
        # clear proves the path empty on its own.
        invalid_hub = SensorSample(True, False, False, False, 0)
        five_clear = (sensors().hub,) * 5
        base_sensors = sensors(lane_count=5, bays=five_clear,
                               loads=(sensors().hub,) * 5, hub=invalid_hub)
        with patch('host.openams_session_bootstrap.send_heartbeat',
                  return_value=CommandResult(INSTANCE, 3, 13, 0)):
            with self.assertRaises(ValueError):
                prepare_cold_host_session(
                    object(), object(), status(), base_sensors, INSTANCE, 5,
                    GENERATION, lambda i, g: (i, g) == (INSTANCE, GENERATION))
            cold = prepare_cold_host_session(
                object(), object(), status(), base_sensors, INSTANCE, 5,
                GENERATION, lambda i, g: (i, g) == (INSTANCE, GENERATION),
                virtual_hub=True)
        self.assertEqual(cold.requests.session, 13)

    def test_preloaded_bootstrap_confirms_heartbeat_and_guard(self):
        stopped, observed = self.preload_observations()
        args = (object(), object(), stopped, observed, INSTANCE, 2,
                GENERATION, lambda i, g: (i, g) == (INSTANCE, GENERATION))
        with patch('host.openams_session_bootstrap.send_heartbeat',
                   return_value=CommandResult(INSTANCE, 3, 13, 0)):
            result = prepare_preloaded_host_session(*args)
        self.assertEqual((result.lane, result.prepared.requests.session,
                          result.prepared.requests.last_request), (1, 13, 0))
        for reply in (CommandResult(INSTANCE, 3, 12, 0),
                      CommandResult(INSTANCE, 3, 13, 2),
                      CommandResult(INSTANCE + 1, 3, 13, 0)):
            with self.subTest(reply=reply), patch(
                    'host.openams_session_bootstrap.send_heartbeat',
                    return_value=reply):
                with self.assertRaisesRegex(ValueError, 'not confirmed OK'):
                    prepare_preloaded_host_session(*args)
        with patch('host.openams_session_bootstrap.send_heartbeat') as beat:
            with self.assertRaisesRegex(ValueError, 'changed'):
                prepare_preloaded_host_session(*args[:-1],
                                               lambda _i, _g: False)
            beat.assert_not_called()

    def prepare(self, observed=None, snapshot=None, guard=None):
        return prepare_cold_host_session(
            object(), object(), snapshot or status(), observed or sensors(),
            INSTANCE, 2, GENERATION, guard or (lambda i, g:
                (i, g) == (INSTANCE, GENERATION)))

    def test_confirmed_heartbeat_creates_fresh_unclaimed_helpers(self):
        with patch('host.openams_session_bootstrap.send_heartbeat',
                   return_value=CommandResult(INSTANCE, 3, 13, 0)) as beat:
            prepared = self.prepare()
        self.assertEqual((prepared.requests.instance, prepared.requests.session,
                          prepared.requests.last_request, prepared.snapshot_query),
                         (INSTANCE, 13, 0, 77))
        self.assertEqual((prepared.delivery.instance, prepared.delivery.generation),
                         (INSTANCE, GENERATION))
        self.assertEqual(prepared.requests.pending, {})
        self.assertIsNone(prepared.delivery.pending)
        self.assertEqual(beat.call_args.args[2:4], (INSTANCE, 13))

    def test_physical_uncertainty_blocks_heartbeat(self):
        clear = sensors().hub
        for observed, snapshot in (
                (sensors(hub=replace(clear, valid=False)), status()),
                (sensors(hub=replace(clear, present=True)), status()),
                (sensors(query=78), status()),
                (sensors(), status(path_occupied=True))):
            with self.subTest(observed=observed, snapshot=snapshot):
                with patch('host.openams_session_bootstrap.send_heartbeat') as beat:
                    with self.assertRaises(ValueError):
                        self.prepare(observed, snapshot)
                    beat.assert_not_called()

    def test_rejection_timeout_and_connection_change_create_nothing(self):
        for result in (CommandResult(INSTANCE, 3, 13, 2),
                       CommandResult(INSTANCE, 3, 12, 0),
                       CommandResult(INSTANCE + 1, 3, 13, 0)):
            with self.subTest(result=result), patch(
                    'host.openams_session_bootstrap.send_heartbeat',
                    return_value=result):
                with self.assertRaisesRegex(ValueError, 'not confirmed OK'):
                    self.prepare()
        with patch('host.openams_session_bootstrap.send_heartbeat',
                   side_effect=ValueError('MMU command timed out')):
            with self.assertRaisesRegex(ValueError, 'timed out'):
                self.prepare()
        checks = iter((True, True, False))
        with patch('host.openams_session_bootstrap.send_heartbeat',
                   return_value=CommandResult(INSTANCE, 3, 13, 0)):
            with self.assertRaisesRegex(ValueError, 'changed after heartbeat'):
                self.prepare(guard=lambda _i, _g: next(checks))

    def test_stale_guard_never_sends(self):
        with patch('host.openams_session_bootstrap.send_heartbeat') as beat:
            with self.assertRaisesRegex(ValueError, 'changed'):
                self.prepare(guard=lambda _i, _g: False)
            beat.assert_not_called()

    def test_occupied_reply_requires_exact_session_and_keeps_lane(self):
        clear = sensors().hub
        present = replace(clear, present=True)
        occupied = status(state=2, path_occupied=True, occupant_lane=1,
                          session=0, request=0, lane=255)
        observed = sensors(bays=(clear, present), hub=present)
        args = (object(), object(), occupied, observed, INSTANCE, 2,
                GENERATION, lambda i, g: (i, g) == (INSTANCE, GENERATION))
        with patch('host.openams_session_bootstrap.send_heartbeat',
                   return_value=CommandResult(INSTANCE, 3, 13, 0)):
            result = prepare_occupied_host_session(*args)
        self.assertEqual((result.lane, result.prepared.requests.session),
                         (1, 13))
        for reply in (CommandResult(INSTANCE, 3, 12, 0),
                      CommandResult(INSTANCE, 3, 13, 2),
                      CommandResult(INSTANCE + 1, 3, 13, 0)):
            with self.subTest(reply=reply), patch(
                    'host.openams_session_bootstrap.send_heartbeat',
                    return_value=reply):
                with self.assertRaisesRegex(ValueError, 'not confirmed OK'):
                    prepare_occupied_host_session(*args)


if __name__ == '__main__':
    unittest.main()
