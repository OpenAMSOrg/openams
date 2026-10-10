# SPDX-License-Identifier: GPL-3.0-or-later
import threading
import unittest
from unittest.mock import patch

from host import KlipperMMU
from host.protocol import SensorSample, SensorSnapshot, Snapshot


class CommandError(Exception):
    pass


class Printer:
    command_error = CommandError

    def get_reactor(self):
        return self

    def monotonic(self):
        return 10.0


class Mcu:
    def is_fileoutput(self):
        return False


class Gcmd:
    def __init__(self):
        self.replies = []

    def respond_info(self, value):
        self.replies.append(value)


def observer():
    device = object.__new__(KlipperMMU)
    device._printer = Printer()
    device._mcu = Mcu()
    device._name = 'unit'
    device._active = device._connected = True
    device._state = 'attached'
    device._generation = 7
    device._instance = 0x100000001
    device._last_query = 15
    device._snapshot_query = 14
    device._last_error = None
    device._prepared_host_session = None
    device._mcu_lock = threading.Lock()
    device._observe = lambda: None
    return device


def capture(instance, query, sample=None):
    good = SensorSample(True, True, True, True, 100)
    sample = sample or good
    status = Snapshot(instance, query, dict(
        acquired_ms=100, path_occupied=True, lane_active=True,
        terminal_pending=False, position_uncertain=False,
        pending_fault_event=False, owner_lane=0, occupant_lane=0,
        system_fault=0, motion_fault=0))
    sensors = SensorSnapshot(instance, query, 1, 100, (sample,), good, good)
    return status, sensors


class PhysicalCommandTest(unittest.TestCase):
    def test_public_capture_proves_identity_and_coherence(self):
        device = observer()
        with patch('host.read_physical_state', side_effect=lambda m, r, i, q, c:
                   capture(i, q)):
            identity, started, status, sensors = device.capture_physical_state()
        self.assertEqual(identity, (7, device._instance))
        self.assertEqual(started, 10.0)
        self.assertEqual(status.query, sensors.query)
        self.assertEqual(device._last_query, status.query)

    def test_public_capture_rejects_mismatched_same_query_pair(self):
        device = observer()

        def mismatched(mcu, reactor, instance, query, current):
            status, sensors = capture(instance, query)
            return status, SensorSnapshot(instance, query, 1, 101,
                                          sensors.bays, sensors.hub,
                                          sensors.toolhead)

        with patch('host.read_physical_state', side_effect=mismatched):
            with self.assertRaisesRegex(ValueError, 'capture changed'):
                device.capture_physical_state()
        self.assertFalse(device._mcu_lock.locked())
        self.assertIsNone(device._last_query)

    def test_known_lane_uses_one_guarded_capture(self):
        device, cmd = observer(), Gcmd()

        def read(mcu, reactor, instance, query, current):
            self.assertTrue(device._mcu_lock.locked())
            self.assertTrue(current())
            self.assertEqual((instance, query), (device._instance, 16))
            return capture(instance, query)

        with patch('host.read_physical_state', side_effect=read):
            device.cmd_KLIPPER_MMU_PHYSICAL(cmd)
        self.assertIn('ownership=known lane 0', cmd.replies[0])
        self.assertIn('bay0=present configured=True valid=True fresh=True', cmd.replies[0])
        self.assertEqual(device._last_query, 16)
        self.assertFalse(device._mcu_lock.locked())

    def test_stale_sensor_is_unknown_and_not_absence(self):
        device, cmd = observer(), Gcmd()
        stale = SensorSample(True, False, True, False, 99)
        with patch('host.read_physical_state', return_value=capture(
                device._instance, 16, stale)):
            device.cmd_KLIPPER_MMU_PHYSICAL(cmd)
        self.assertIn('ownership=unknown', cmd.replies[0])
        self.assertIn('bay0=unknown configured=True valid=True fresh=False', cmd.replies[0])
        self.assertNotIn('bay0=absent', cmd.replies[0])

    def test_transport_failure_and_invalidation_release_guard(self):
        device, cmd = observer(), Gcmd()
        with patch('host.read_physical_state', side_effect=ValueError('timed out')):
            with self.assertRaises(CommandError):
                device.cmd_KLIPPER_MMU_PHYSICAL(cmd)
        self.assertFalse(device._mcu_lock.locked())
        self.assertIsNone(device._last_query)
        self.assertEqual(device._snapshot_query, 16)
        self.assertFalse(cmd.replies)
        device = observer()

        def invalidated(mcu, reactor, instance, query, current):
            device._generation += 1
            device._last_error = 'new generation'
            return capture(instance, query)

        with patch('host.read_physical_state', side_effect=invalidated):
            with self.assertRaises(CommandError):
                device.cmd_KLIPPER_MMU_PHYSICAL(cmd)
        self.assertEqual(device._last_error, 'new generation')
        self.assertFalse(device._mcu_lock.locked())

    def test_guard_rejects_inactive_without_send(self):
        device, cmd = observer(), Gcmd()
        with patch('host.read_physical_state') as read:
            device._active = False
            with self.assertRaises(CommandError):
                device.cmd_KLIPPER_MMU_PHYSICAL(cmd)
            read.assert_not_called()


if __name__ == '__main__':
    unittest.main()
