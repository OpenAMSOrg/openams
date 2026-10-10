# SPDX-License-Identifier: GPL-3.0-or-later
import unittest
from unittest.mock import patch

from host.openams_operation_bridge import OpenAMSPTFECalibrationCommandBridge
from host.protocol import Admission, CommandResult, Event
from host.requests import Requests


class Reactor:
    def __init__(self):
        self.callbacks = []

    def register_async_callback(self, callback):
        self.callbacks.append(callback)


class CalibrationBridgeTest(unittest.TestCase):
    def setUp(self):
        self.requests = Requests(0x1020304050607080, 8, 9)
        self.reactor = Reactor()
        self.current = [True]
        self.permission = [True]
        self.accepted = []
        self.bridge = OpenAMSPTFECalibrationCommandBridge(
            object(), self.reactor, self.requests, 'fps0', 4,
            lambda: self.current[0], lambda: self.permission[0],
            lambda *args: self.accepted.append(args) or True)

    def test_ptfe_admission_waits_for_retained_terminal(self):
        def send(mcu, reactor, instance, identity, current, direction):
            self.assertTrue(current())
            self.assertEqual((identity.operation, identity.lane, direction), (4, 2, 1))
            return Admission(instance, identity, 0, 0)

        with patch('host.openams_operation_bridge.send_operation', side_effect=send):
            result = self.bridge.start_calibrate('ptfe', 2, gen=7)
        self.assertEqual(self.requests.pending['fps0'], (result.identity, 7))
        self.assertEqual(self.accepted, [])
        self.assertEqual(self.reactor.callbacks, [])

    def test_unsupported_kind_and_default_permission_refuse_before_issue(self):
        with patch('host.openams_operation_bridge.send_operation') as send:
            with self.assertRaisesRegex(ValueError, 'Unsupported OpenAMS calibration kind'):
                self.bridge.start_calibrate('hub_hes', 0, gen=1)
            with self.assertRaisesRegex(ValueError, 'Invalid OpenAMS bay'):
                self.bridge.start_calibrate('ptfe', True, gen=1)
            default = OpenAMSPTFECalibrationCommandBridge(
                object(), self.reactor, self.requests, 'fps0', 4)
            with self.assertRaisesRegex(ValueError, 'permission'):
                default.start_calibrate('ptfe', 0, gen=1)
            send.assert_not_called()
        self.assertEqual(self.requests.last_request, 9)

    def test_rejected_admission_settles_exact_waiter_after_dispatch(self):
        def send(mcu, reactor, instance, identity, current, direction):
            return Admission(instance, identity, 2, 0)

        with patch('host.openams_operation_bridge.send_operation', side_effect=send):
            started = self.bridge.start_calibrate('ptfe', 0, gen=3)
        self.assertEqual(self.requests.pending['fps0'], (started.identity, 3))
        self.assertEqual(self.accepted, [])
        self.reactor.callbacks.pop()(0)
        self.assertEqual(self.accepted, [('fps0', 2, 0, 3)])
        self.assertFalse(self.requests.pending)

    def test_command_rejection_defers_settlement(self):
        def send(mcu, reactor, instance, identity, current, direction):
            return CommandResult(instance, 1, identity.request, 7)

        with patch('host.openams_operation_bridge.send_operation', side_effect=send):
            with self.assertRaisesRegex(ValueError, 'command result 7'):
                self.bridge.start_calibrate('ptfe', 1, gen=4)
        self.assertIn('fps0', self.requests.pending)
        self.reactor.callbacks.pop()(0)
        self.assertEqual(self.accepted, [('fps0', 5, 0, 4)])
        self.assertFalse(self.requests.pending)

    def start(self, gen=7):
        def send(mcu, reactor, instance, identity, current, direction):
            return Admission(instance, identity, 0, 0)

        with patch('host.openams_operation_bridge.send_operation', side_effect=send):
            return self.bridge.start_calibrate('ptfe', 2, gen=gen).identity

    def terminal(self, identity, event_type=8, code=0, value=0, flags=0):
        return Event(self.requests.instance, identity, 1, 100, event_type, code,
                     value, flags)

    def test_encoder_measurement_value_reaches_openams_unchanged(self):
        identity = self.start()
        match = self.requests.event(self.terminal(identity, value=2000), 1)
        self.assertEqual((match.waiter, match.code, match.value, match.token),
                         ('fps0', 0, 2000, 7))

    def test_step_estimate_is_never_an_openams_encoder_length(self):
        identity = self.start()
        match = self.requests.event(self.terminal(identity, value=12345, flags=2), 1)
        # OpenAMS's PTFE value is an encoder length; 0 means uncalibrated.
        self.assertEqual((match.code, match.value), (0, 0))

    def test_step_estimate_flag_rejected_outside_calibration_success(self):
        identity = self.start()
        for event_type, code, flags in ((9, 6, 2), (10, 7, 2), (8, 0, 4)):
            with self.subTest(event_type=event_type, flags=flags):
                with self.assertRaisesRegex(ValueError, 'Unsupported terminal'):
                    self.requests.event(self.terminal(identity, event_type, code,
                                                      flags=flags), 1)
        self.assertIn('fps0', self.requests.pending)


if __name__ == '__main__':
    unittest.main()
