# SPDX-License-Identifier: GPL-3.0-or-later
import threading
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from host import KlipperMMU


class CommandError(Exception):
    pass


class Printer:
    command_error = CommandError

    def config_error(self, message):
        return CommandError(message)


class Query:
    def __init__(self, response):
        self.response = response
        self.sent = []

    def send(self, args):
        self.sent.append(list(args))
        if callable(self.response):
            return self.response(args)
        return dict(self.response)


class Mcu:
    def __init__(self, query=None, constants=None):
        self.query = query
        self.constants = constants or {}
        self.commands = []

    def lookup_query_command(self, request, response):
        self.formats = request, response
        return self.query

    def is_fileoutput(self):
        return False

    def get_constants(self):
        return self.constants

    def add_config_cmd(self, command, is_init=False):
        self.commands.append((command, is_init))


class Gcmd:
    def __init__(self):
        self.replies = []

    def respond_info(self, message):
        self.replies.append(message)


def observer(response):
    query = Query(response)
    device = object.__new__(KlipperMMU)
    device._printer = Printer()
    device._mcu = Mcu(query)
    device._name = 'unit'
    device._diff_probe = True
    device._diff_probe_query = None
    device._active = device._connected = True
    device._generation = 4
    device._challenge = 100
    device._mcu_lock = threading.Lock()
    return device, query


def pair(challenge, **overrides):
    result = dict(challenge=challenge, state=2, result=0, a_raw=1532,
                  b_raw=2490, a_attempt_us=11, b_ready_us=17,
                  observed_us=18)
    result.update(overrides)
    return result


class DiffProbeHostTest(unittest.TestCase):
    def test_queries_and_reports_only_the_fresh_raw_pair(self):
        device, query = observer(lambda args: pair(args[0]))
        command = Gcmd()
        device.cmd_KLIPPER_MMU_DIFF_PROBE(command)
        self.assertEqual(query.sent, [[101]])
        self.assertEqual(device._mcu.formats, (
            'km_diff_probe_query challenge=%u',
            'km_diff_probe_state challenge=%u state=%c result=%i a_raw=%hu '
            'b_raw=%hu a_attempt_us=%u b_ready_us=%u observed_us=%u'))
        self.assertIn('a_raw=1532 b_raw=2490', command.replies[0])
        self.assertFalse(device._mcu_lock.locked())

    def test_nonfresh_state_and_bad_echo_never_report_raw_values(self):
        for response in (lambda args: pair(args[0], state=1, a_raw=2000),
                         lambda args: pair(args[0] + 1)):
            device, _ = observer(response)
            command = Gcmd()
            with self.assertRaises(CommandError):
                device.cmd_KLIPPER_MMU_DIFF_PROBE(command)
            self.assertFalse(command.replies)
            self.assertFalse(device._mcu_lock.locked())

    def test_generation_change_rejects_reply(self):
        device, _ = observer(None)

        def changed(args):
            device._generation += 1
            return pair(args[0])

        device._mcu.query.response = changed
        command = Gcmd()
        with self.assertRaisesRegex(CommandError, 'connection changed'):
            device.cmd_KLIPPER_MMU_DIFF_PROBE(command)
        self.assertFalse(command.replies)

    def test_init_gate_requires_receive_only_probe_firmware(self):
        device = object.__new__(KlipperMMU)
        device._printer = Printer()
        device._mcu = Mcu(constants={
            'KLIPPER_MMU_PROTOCOL_VERSION': 1,
            'KLIPPER_MMU_INTEGRATION_VERSION': 1,
            'KLIPPER_MMU_ACTUATION': 0,
            'KLIPPER_MMU_DIFF_PROBE': 1,
        })
        device._diff_probe = True
        device._diagnostic_startup = False
        device._calibration_data = None
        device._config_calibration_and_startup()
        self.assertEqual(device._mcu.commands,
                         [('km_diff_probe_start', True)])

        for key, value in (('KLIPPER_MMU_ACTUATION', 1),
                           ('KLIPPER_MMU_DIFF_PROBE', True)):
            device._mcu.commands.clear()
            device._mcu.constants[key] = value
            with self.assertRaises(CommandError):
                device._config_calibration_and_startup()
            self.assertFalse(device._mcu.commands)


if __name__ == '__main__':
    unittest.main()
