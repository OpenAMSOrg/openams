# SPDX-License-Identifier: GPL-3.0-or-later
import unittest
from unittest.mock import patch

from host.openams_cancel_bridge import OpenAMSLoadCancelBridge
from host.protocol import Admission, CommandResult, Identity
from host.requests import LOAD, Requests


INSTANCE = 0x123456789


class CancelBridgeTest(unittest.TestCase):
    def setUp(self):
        self.requests = Requests(INSTANCE, 17)
        self.identity = self.requests.issue(LOAD, 2, 'fps', 9)
        self.live = True
        self.bridge = OpenAMSLoadCancelBridge(
            object(), object(), self.requests, 'fps', lambda: self.live,
            timeout=0.25)

    def test_accepted_uses_exact_identity_and_keeps_waiter(self):
        response = Admission(INSTANCE, self.identity, 0, 0)
        with patch('host.openams_cancel_bridge.send_operation', return_value=response) as send:
            self.assertEqual(self.bridge.load_spool_cancel(), response)
        args, kwargs = send.call_args
        self.assertEqual(args[2:4], (INSTANCE, self.identity))
        self.assertTrue(args[4]())
        self.assertEqual(kwargs, {'cancel': True, 'timeout': 0.25})
        self.assertEqual(self.requests.cancel_target('fps'), self.identity)
        self.assertEqual(self.requests.last_request, self.identity.request)

    def test_rejected_and_malformed_responses_keep_waiter(self):
        replies = [Admission(INSTANCE, self.identity, 4, 0),
                   CommandResult(INSTANCE, 2, self.identity.request, 3),
                   Admission(INSTANCE, Identity(1, 17, 99, LOAD, 2), 0, 0),
                   CommandResult(INSTANCE, 2, self.identity.request, 0), object()]
        for response in replies:
            with self.subTest(response=response):
                with patch('host.openams_cancel_bridge.send_operation', return_value=response):
                    with self.assertRaises(ValueError):
                        self.bridge.load_spool_cancel()
                self.assertEqual(self.requests.cancel_target('fps'), self.identity)

    def test_stale_generation_instance_and_missing_pending_do_not_send(self):
        with patch('host.openams_cancel_bridge.send_operation') as send:
            self.live = False
            with self.assertRaises(ValueError):
                self.bridge.load_spool_cancel()
            self.live = True
            self.requests.instance += 1
            with self.assertRaises(ValueError):
                self.bridge.load_spool_cancel()
            self.requests.instance = INSTANCE
            self.requests.disconnect()
            with self.assertRaises(ValueError):
                self.bridge.load_spool_cancel()
            send.assert_not_called()

    def test_missing_or_non_load_waiter_does_not_send(self):
        with patch('host.openams_cancel_bridge.send_operation') as send:
            del self.requests.pending['fps']
            with self.assertRaises(KeyError):
                self.bridge.load_spool_cancel()
            self.requests.issue(2, 2, 'fps', 9)
            with self.assertRaises(ValueError):
                self.bridge.load_spool_cancel()
            send.assert_not_called()

    def test_stale_during_exchange_and_timeout_leave_pending(self):
        def stale(*args, **kwargs):
            self.requests.disconnect()
            return Admission(INSTANCE, self.identity, 0, 0)

        with patch('host.openams_cancel_bridge.send_operation', side_effect=stale):
            with self.assertRaises(ValueError):
                self.bridge.load_spool_cancel()
        self.assertEqual(self.requests.pending['fps'][0], self.identity)
        self.requests.connected = True
        self.bridge = OpenAMSLoadCancelBridge(
            object(), object(), self.requests, 'fps', lambda: True)
        with patch('host.openams_cancel_bridge.send_operation', side_effect=ValueError('timeout')):
            with self.assertRaisesRegex(ValueError, 'timeout'):
                self.bridge.load_spool_cancel()
        self.assertEqual(self.requests.pending['fps'][0], self.identity)


if __name__ == '__main__':
    unittest.main()
