# SPDX-License-Identifier: GPL-3.0-or-later
import threading
import unittest
from unittest.mock import patch
from dataclasses import replace
from types import SimpleNamespace

from host import KlipperMMU
from host.protocol import CommandResult, SensorSample, SensorSnapshot, Snapshot


INSTANCE = 0x1020304050607080
IDENTITY = (7, INSTANCE)


class Printer:
    def get_reactor(self):
        return self


def empty_capture(**changes):
    fields = dict(state=0, stage=0, operation=0, client=0,
                  queued_events=0, head_event_seq=0, system_fault=0,
                  motion_fault=0, velocity=0, lane_active=False,
                  terminal_pending=False, path_occupied=False,
                  position_uncertain=False, pending_fault_event=False,
                  owner_lane=255, occupant_lane=255, acquired_ms=100,
                  client_sessions=(0, 12, 0, 0),
                  client_requests=(0, 0, 0, 0))
    fields.update(changes)
    clear = SensorSample(True, False, True, True, 100)
    return (IDENTITY, 10.0, Snapshot(INSTANCE, 77, fields),
            SensorSnapshot(INSTANCE, 77, 2, 100,
                           (clear, clear), clear, clear))


def occupied_capture(**changes):
    fields = dict(state=2, path_occupied=True, occupant_lane=1, session=0,
                  request=0, lane=255)
    fields.update(changes)
    identity, started, status, sensors = empty_capture(**fields)
    present = replace(sensors.hub, present=True)
    sensors = replace(sensors, bays=(sensors.bays[0], present), hub=present)
    return identity, started, status, sensors


def preloaded_capture(**changes):
    fields = dict(state=8, path_occupied=True, occupant_lane=1,
                  session=0, request=0, lane=255)
    fields.update(changes)
    identity, started, status, sensors = empty_capture(**fields)
    present = replace(sensors.bays[1], present=True)
    loads = (sensors.bays[0], present)
    sensors = replace(sensors, bays=(sensors.bays[0], present), loads=loads)
    return identity, started, status, sensors


def observer():
    device = object.__new__(KlipperMMU)
    device._printer = Printer()
    device._mcu = object()
    device._active = device._connected = True
    device._state = 'attached'
    device._generation, device._instance = IDENTITY
    device._mcu_lock = threading.Lock()
    device._firmware_actuation = 1
    device._firmware_composition = (1, 2)
    device._openams_unit = SimpleNamespace(device_family='boxturtle', bay_count=2)
    device._diagnostic_startup = True
    device._startup_state = 'ready'
    device._startup_result = 0
    device._prepared_host_session = None
    device._host_session_ambiguous = False
    device.capture_physical_state = lambda: empty_capture()
    return device


class ColdSessionTest(unittest.TestCase):
    def test_custom_composition_can_prepare_matching_session(self):
        device = observer()
        device._firmware_composition = (3, 2)
        device._openams_unit = SimpleNamespace(device_family='custom', bay_count=2)
        with patch('host.openams_session_bootstrap.send_heartbeat',
                   return_value=CommandResult(INSTANCE, 3, 13, 0)) as beat:
            prepared = device.prepare_cold_host_session(2)
        self.assertEqual(prepared.requests.session, 13)
        beat.assert_called_once()

    def test_composition_mismatch_never_sends_heartbeat(self):
        for composition, family, bays in (((2, 2), 'boxturtle', 2),
                                          ((1, 4), 'boxturtle', 2),
                                          (None, 'boxturtle', 2),
                                          ((3, 4), 'custom', 2),
                                          ((3, 2), 'planned-family', 2)):
            with self.subTest(composition=composition, family=family):
                device = observer()
                device._firmware_composition = composition
                device._openams_unit = SimpleNamespace(
                    device_family=family, bay_count=bays)
                with patch('host.openams_session_bootstrap.send_heartbeat') as beat:
                    with self.assertRaisesRegex(ValueError, 'unavailable'):
                        device.prepare_cold_host_session(2)
                    beat.assert_not_called()
                self.assertFalse(device._host_session_ambiguous)

    def test_gates_and_physical_uncertainty_send_no_heartbeat(self):
        for change in ({'_firmware_actuation': 0},
                       {'_diagnostic_startup': False},
                       {'_startup_state': 'idle'},
                       {'_connected': False}):
            with self.subTest(change=change):
                device = observer()
                device.__dict__.update(change)
                with patch('host.openams_session_bootstrap.send_heartbeat') as beat:
                    with self.assertRaises(ValueError):
                        device.prepare_cold_host_session(2)
                    beat.assert_not_called()
        for fields in ({'queued_events': 1}, {'operation': 1},
                       {'path_occupied': True}):
            with self.subTest(fields=fields):
                device = observer()
                device.capture_physical_state = lambda: empty_capture(**fields)
                with patch('host.openams_session_bootstrap.send_heartbeat') as beat:
                    with self.assertRaises(ValueError):
                        device.prepare_cold_host_session(2)
                    beat.assert_not_called()
                self.assertFalse(device._host_session_ambiguous)

    def test_confirmed_heartbeat_returns_fresh_session_once(self):
        device = observer()
        with patch('host.openams_session_bootstrap.send_heartbeat',
                   return_value=CommandResult(INSTANCE, 3, 13, 0)) as beat:
            prepared = device.prepare_cold_host_session(2)
            self.assertEqual((prepared.requests.session, prepared.snapshot_query),
                             (13, 77))
            self.assertIs(device._prepared_host_session, prepared)
            self.assertFalse(device._mcu_lock.locked())
            with self.assertRaises(ValueError):
                device.prepare_cold_host_session(2)
            self.assertEqual(beat.call_count, 1)

    def test_timeout_and_reconnect_do_not_publish_session_or_retry(self):
        device = observer()
        with patch('host.openams_session_bootstrap.send_heartbeat',
                   side_effect=ValueError('MMU command timed out')) as beat:
            with self.assertRaisesRegex(ValueError, 'timed out'):
                device.prepare_cold_host_session(2)
            self.assertIsNone(device._prepared_host_session)
            self.assertTrue(device._host_session_ambiguous)
            device._generation += 1
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                device.prepare_cold_host_session(2)
            self.assertEqual(beat.call_count, 1)

    def test_disconnect_while_heartbeat_waits_discards_reply(self):
        device = observer()

        def disconnected(*args, **kwargs):
            device._connected = False
            device._generation += 1
            return CommandResult(INSTANCE, 3, 13, 0)

        with patch('host.openams_session_bootstrap.send_heartbeat',
                   side_effect=disconnected):
            with self.assertRaisesRegex(ValueError, 'changed after heartbeat'):
                device.prepare_cold_host_session(2)
        self.assertIsNone(device._prepared_host_session)
        self.assertTrue(device._host_session_ambiguous)


class OccupiedSessionTest(unittest.TestCase):
    def test_composition_mismatch_never_sends_heartbeat(self):
        device = observer()
        device.capture_physical_state = occupied_capture
        device._firmware_composition = (1, 4)
        with patch('host.openams_session_bootstrap.send_heartbeat') as beat:
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                device.prepare_occupied_host_session(2)
            beat.assert_not_called()

    def test_confirmed_heartbeat_publishes_proven_lane_and_session(self):
        device = observer()
        device.capture_physical_state = occupied_capture
        with patch('host.openams_session_bootstrap.send_heartbeat',
                   return_value=CommandResult(INSTANCE, 3, 13, 0)) as beat:
            result = device.prepare_occupied_host_session(2)
        self.assertEqual((result.lane, result.prepared.requests.session,
                          result.prepared.snapshot_query), (1, 13, 77))
        self.assertIs(device._prepared_host_session, result.prepared)
        self.assertFalse(device._host_session_ambiguous)
        beat.assert_called_once()

    def test_ineligible_capture_never_sends_or_latches(self):
        for fields in ({'queued_events': 1}, {'operation': 1},
                       {'position_uncertain': True}, {'state': 0}):
            with self.subTest(fields=fields):
                device = observer()
                device.capture_physical_state = lambda: occupied_capture(**fields)
                with patch('host.openams_session_bootstrap.send_heartbeat') as beat:
                    with self.assertRaises(ValueError):
                        device.prepare_occupied_host_session(2)
                beat.assert_not_called()
                self.assertFalse(device._host_session_ambiguous)

    def test_timeout_latches_without_retry(self):
        device = observer()
        device.capture_physical_state = occupied_capture
        with patch('host.openams_session_bootstrap.send_heartbeat',
                   side_effect=ValueError('timed out')) as beat:
            with self.assertRaisesRegex(ValueError, 'timed out'):
                device.prepare_occupied_host_session(2)
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                device.prepare_occupied_host_session(2)
        self.assertEqual(beat.call_count, 1)
        self.assertIsNone(device._prepared_host_session)
        self.assertTrue(device._host_session_ambiguous)


class PreloadedSessionTest(unittest.TestCase):
    def test_fresh_local_capture_confirms_one_session(self):
        device = observer()
        device.capture_physical_state = preloaded_capture
        with patch('host.openams_session_bootstrap.send_heartbeat',
                   return_value=CommandResult(INSTANCE, 3, 13, 0)) as beat:
            result = device.prepare_preloaded_host_session(2)
            self.assertEqual((result.lane, result.prepared.requests.session,
                              result.prepared.snapshot_query), (1, 13, 77))
            self.assertIs(device._prepared_host_session, result.prepared)
            self.assertFalse(device._host_session_ambiguous)
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                device.prepare_preloaded_host_session(2)
        beat.assert_called_once()

    def test_wrong_generation_or_changed_lane_rejects_before_heartbeat(self):
        for capture in (lambda: ((IDENTITY[0] - 1, INSTANCE),
                                  *preloaded_capture()[1:]),
                        lambda: preloaded_capture(occupant_lane=0),
                        lambda: preloaded_capture(state=2)):
            with self.subTest(capture=capture):
                device = observer()
                device.capture_physical_state = capture
                with patch('host.openams_session_bootstrap.send_heartbeat') as beat:
                    with self.assertRaises(ValueError):
                        device.prepare_preloaded_host_session(2)
                beat.assert_not_called()
                self.assertIsNone(device._prepared_host_session)
                self.assertFalse(device._host_session_ambiguous)

    def test_uncertain_heartbeat_latches_and_never_repeats(self):
        device = observer()
        device.capture_physical_state = preloaded_capture
        with patch('host.openams_session_bootstrap.send_heartbeat',
                   side_effect=ValueError('timed out')) as beat:
            with self.assertRaisesRegex(ValueError, 'timed out'):
                device.prepare_preloaded_host_session(2)
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                device.prepare_preloaded_host_session(2)
        beat.assert_called_once()
        self.assertIsNone(device._prepared_host_session)
        self.assertTrue(device._host_session_ambiguous)

    def test_generation_change_during_heartbeat_discards_reply(self):
        device = observer()
        device.capture_physical_state = preloaded_capture

        def reconnect(*args, **kwargs):
            device._generation += 1
            return CommandResult(INSTANCE, 3, 13, 0)

        with patch('host.openams_session_bootstrap.send_heartbeat',
                   side_effect=reconnect) as beat:
            with self.assertRaisesRegex(ValueError, 'changed after heartbeat'):
                device.prepare_preloaded_host_session(2)
        beat.assert_called_once()
        self.assertIsNone(device._prepared_host_session)
        self.assertTrue(device._host_session_ambiguous)


if __name__ == '__main__':
    unittest.main()
