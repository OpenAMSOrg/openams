# SPDX-License-Identifier: GPL-3.0-or-later
import threading
import unittest
from unittest.mock import patch

from host import KlipperMMU
from host.protocol import SensorSample, SensorSnapshot


class CommandError(Exception):
    pass


class Printer:
    command_error = CommandError

    def get_reactor(self):
        return object()


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
    device._active = True
    device._connected = True
    device._state = 'attached'
    device._generation = 7
    device._instance = 0x100000001
    device._last_query = 15
    device._snapshot_query = 14
    device._last_error = None
    device._mcu_lock = threading.Lock()
    device._snapshot = None
    device._observe = lambda: None
    return device


class SensorCommandTest(unittest.TestCase):
    def test_query_is_serialized_and_reports_unknown_as_unknown(self):
        device = observer()
        cmd = Gcmd()
        samples = (
            SensorSample(True, True, True, True, 10),
            SensorSample(True, False, True, False, 9),
            SensorSample(False, False, False, False, 0),
        )
        observed = SensorSnapshot(device._instance, 16, 3, 11, samples,
                                  samples[1], samples[2])

        def read(mcu, reactor, instance, query, current):
            self.assertTrue(device._mcu_lock.locked())
            self.assertTrue(current())
            self.assertEqual((instance, query), (device._instance, 16))
            return observed

        with patch('host.read_sensors', side_effect=read):
            device.cmd_KLIPPER_MMU_SENSORS(cmd)
        self.assertFalse(device._mcu_lock.locked())
        self.assertEqual(device._last_query, 16)
        self.assertIn('bay0=present', cmd.replies[0])
        self.assertIn('bay1=unknown', cmd.replies[0])
        self.assertIn('bay2=unconfigured', cmd.replies[0])
        self.assertNotIn('bay1=absent', cmd.replies[0])

    def test_changed_generation_rejects_result_without_overwriting_new_state(self):
        device = observer()
        cmd = Gcmd()

        def read(mcu, reactor, instance, query, current):
            device._generation += 1
            device._last_error = 'new generation'
            return SensorSnapshot(instance, query, 1, 0,
                                  (SensorSample(False, False, False, False, 0),),
                                  SensorSample(False, False, False, False, 0),
                                  SensorSample(False, False, False, False, 0))

        with patch('host.read_sensors', side_effect=read):
            with self.assertRaises(CommandError):
                device.cmd_KLIPPER_MMU_SENSORS(cmd)
        self.assertEqual(device._last_error, 'new generation')
        self.assertEqual(device._last_query, 15)
        self.assertFalse(cmd.replies)
        self.assertFalse(device._mcu_lock.locked())

    def test_inactive_call_sends_nothing(self):
        device = observer()
        cmd = Gcmd()
        with patch('host.read_sensors') as read:
            device._active = False
            with self.assertRaises(CommandError):
                device.cmd_KLIPPER_MMU_SENSORS(cmd)
            read.assert_not_called()


if __name__ == '__main__':
    unittest.main()
