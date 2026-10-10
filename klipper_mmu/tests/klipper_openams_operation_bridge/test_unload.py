# SPDX-License-Identifier: GPL-3.0-or-later
import unittest
from unittest.mock import patch

from host.openams_operation_bridge import OpenAMSUnloadCommandBridge
from host.protocol import Admission, CommandResult
from host.requests import Requests


class Reactor:
    def __init__(self):
        self.callbacks = []

    def register_async_callback(self, callback):
        self.callbacks.append(callback)


class UnloadBridgeTest(unittest.TestCase):
    def setUp(self):
        self.requests = Requests(0x1020304050607080, 8, 9)
        self.reactor = Reactor()
        self.occupant = [2]
        self.permission = [True]
        self.current = [True]
        self.accepted = []
        self.bridge = OpenAMSUnloadCommandBridge(
            object(), self.reactor, self.requests, 'fps0', 4,
            lambda: self.occupant[0], lambda: self.current[0],
            lambda: self.permission[0],
            lambda *args: self.accepted.append(args) or True)

    def test_unload_sends_current_occupant_and_waits_for_terminal(self):
        def send(mcu, reactor, instance, identity, current, direction):
            self.assertTrue(current())
            self.assertEqual((identity.client, identity.session, identity.request,
                              identity.operation, identity.lane, direction),
                             (1, 8, 10, 2, 2, 0))
            return Admission(instance, identity, 0, 0)

        with patch('host.openams_operation_bridge.send_operation', side_effect=send):
            result = self.bridge.start_unload_spool(gen=7)
        self.assertEqual(self.requests.pending['fps0'], (result.identity, 7))
        self.assertEqual(self.accepted, [])
        self.assertEqual(self.reactor.callbacks, [])

    def test_unknown_occupant_and_default_permissions_refuse_before_issue(self):
        with patch('host.openams_operation_bridge.send_operation') as send:
            for occupant in (None, -1, 4, True):
                self.occupant[0] = occupant
                with self.assertRaisesRegex(ValueError, 'occupant'):
                    self.bridge.start_unload_spool(gen=1)
            bridge = OpenAMSUnloadCommandBridge(
                object(), self.reactor, self.requests, 'fps0', 4,
                current_occupant=lambda: 1)
            with self.assertRaisesRegex(ValueError, 'permission'):
                bridge.start_unload_spool(gen=1)
            send.assert_not_called()
        self.assertEqual(self.requests.last_request, 9)

    def test_occupant_change_retains_ambiguous_request(self):
        def send(mcu, reactor, instance, identity, current, direction):
            self.occupant[0] = 1
            return Admission(instance, identity, 0, 0)

        with patch('host.openams_operation_bridge.send_operation', side_effect=send):
            with self.assertRaisesRegex(ValueError, 'changed'):
                self.bridge.start_unload_spool(gen=3)
        self.assertEqual(self.requests.pending['fps0'][0].lane, 2)

    def test_rejected_admission_defers_exact_waiter_settlement(self):
        def send(mcu, reactor, instance, identity, current, direction):
            return Admission(instance, identity, 2, 0)

        with patch('host.openams_operation_bridge.send_operation', side_effect=send):
            result = self.bridge.start_unload_spool(gen=7)
        self.assertEqual(self.requests.pending['fps0'][0], result.identity)
        self.assertEqual(self.accepted, [])
        self.reactor.callbacks.pop()(0)
        self.assertEqual(self.accepted, [('fps0', 2, 0, 7)])
        self.assertFalse(self.requests.pending)

    def test_stale_rejection_does_not_settle_new_session(self):
        def send(mcu, reactor, instance, identity, current, direction):
            return Admission(instance, identity, 2, 0)

        with patch('host.openams_operation_bridge.send_operation', side_effect=send):
            self.bridge.start_unload_spool(gen=7)
        self.requests.disconnect()
        self.reactor.callbacks.pop()(0)
        self.assertEqual(self.accepted, [])
        self.assertIn('fps0', self.requests.pending)

    def test_command_rejection_retired_only_after_deferred_callback(self):
        def send(mcu, reactor, instance, identity, current, direction):
            return CommandResult(instance, 1, identity.request, 7)

        with patch('host.openams_operation_bridge.send_operation', side_effect=send):
            with self.assertRaisesRegex(ValueError, 'command result 7'):
                self.bridge.start_unload_spool(gen=4)
        self.assertIn('fps0', self.requests.pending)
        self.reactor.callbacks.pop()(0)
        self.assertEqual(self.accepted, [('fps0', 5, 0, 4)])
        self.assertFalse(self.requests.pending)


if __name__ == '__main__':
    unittest.main()
