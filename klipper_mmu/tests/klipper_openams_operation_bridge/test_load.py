# SPDX-License-Identifier: GPL-3.0-or-later
import unittest
from unittest.mock import patch

from host.openams_operation_bridge import OpenAMSLoadCommandBridge
from host.protocol import Admission, CommandResult
from host.requests import Requests


class LoadBridgeTest(unittest.TestCase):
    def setUp(self):
        self.requests = Requests(0x1020304050607080, 8, 9)
        self.permission = [True]
        self.connection = [True]
        class Reactor:
            def __init__(self):
                self.callbacks = []

            def register_async_callback(self, callback):
                self.callbacks.append(callback)

        self.reactor = Reactor()
        self.bridge = OpenAMSLoadCommandBridge(
            object(), self.reactor, self.requests, 'fps0', 4,
            lambda: self.connection[0], lambda: self.permission[0],
            lambda fps, code, value, gen: True)

    def test_start_preserves_identity_and_waiter_until_terminal(self):
        def send(mcu, reactor, instance, identity, current, direction):
            self.assertTrue(current())
            self.assertEqual((instance, identity.client, identity.session,
                              identity.request, identity.operation, identity.lane,
                              direction),
                             (self.requests.instance, 1, 8, 10, 1, 2, 1))
            return Admission(instance, identity, 0, 0)

        with patch('host.openams_operation_bridge.send_operation', side_effect=send):
            started = self.bridge.start_load_spool(2, gen=7)
        self.assertEqual(started.response.admission, 0)
        self.assertEqual(self.requests.pending['fps0'], (started.identity, 7))
        self.assertFalse(self.bridge.hardware_snapshot_ready)

    def test_permission_defaults_to_refusal_before_request_allocation(self):
        bridge = OpenAMSLoadCommandBridge(
            object(), object(), self.requests, 'fps0', 4)
        with patch('host.openams_operation_bridge.send_operation') as send:
            with self.assertRaises(ValueError):
                bridge.start_load_spool(1, gen=0)
            send.assert_not_called()
        self.assertEqual(self.requests.last_request, 9)
        self.assertFalse(self.requests.pending)

    def test_lost_connection_retains_identity_for_reconciliation(self):
        def send(mcu, reactor, instance, identity, current, direction):
            self.connection[0] = False
            return Admission(instance, identity, 0, 0)

        with patch('host.openams_operation_bridge.send_operation', side_effect=send):
            with self.assertRaisesRegex(ValueError, 'changed'):
                self.bridge.start_load_spool(1, gen=3)
        self.assertEqual(self.requests.pending['fps0'][1], 3)
        self.assertEqual(self.requests.last_request, 10)

    def test_administrative_rejection_raises_and_defers_exact_retirement(self):
        def send(mcu, reactor, instance, identity, current, direction):
            return CommandResult(instance, 1, identity.request, 7)

        with patch('host.openams_operation_bridge.send_operation', side_effect=send):
            with self.assertRaisesRegex(ValueError, 'command result 7'):
                self.bridge.start_load_spool(0, gen=255)
            with self.assertRaisesRegex(ValueError, 'Existing OpenAMS waiter'):
                self.bridge.start_load_spool(1, gen=0)
        self.assertEqual(self.requests.pending['fps0'][1], 255)
        self.assertEqual(self.requests.last_request, 10)
        self.reactor.callbacks.pop(0)(0)
        self.assertFalse(self.requests.pending)

    def test_rejected_admission_defers_settlement_and_retains_failed_match(self):
        self.bridge.accept_terminal = lambda *args: False

        def send(mcu, reactor, instance, identity, current, direction):
            return Admission(instance, identity, 2, 0)

        with patch('host.openams_operation_bridge.send_operation', side_effect=send):
            started = self.bridge.start_load_spool(0, gen=7)
        self.assertEqual(started.response.admission, 2)
        self.assertEqual(self.requests.pending['fps0'][1], 7)
        self.reactor.callbacks.pop(0)(0)
        self.assertEqual(self.requests.pending['fps0'][1], 7)

    def test_ambiguous_send_failure_retains_request_for_reconciliation(self):
        with patch('host.openams_operation_bridge.send_operation',
                   side_effect=ValueError('MMU command timed out')):
            with self.assertRaisesRegex(ValueError, 'timed out'):
                self.bridge.start_load_spool(2, gen=4)
        self.assertEqual(self.requests.pending['fps0'][1], 4)
        self.assertFalse(self.reactor.callbacks)


if __name__ == '__main__':
    unittest.main()
