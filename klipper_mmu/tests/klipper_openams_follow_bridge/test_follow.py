# SPDX-License-Identifier: GPL-3.0-or-later
import unittest
from unittest.mock import patch

from host.openams_follow_bridge import OpenAMSFollowCommandBridge
from host.protocol import Admission, CommandResult, Identity
from host.requests import FOLLOW, HOST, LOAD, Requests


INSTANCE = 0x123456789


class FollowBridgeTest(unittest.TestCase):
    def setUp(self):
        self.requests = Requests(INSTANCE, 17, 41)
        self.owner = Identity(HOST, 17, 41, LOAD, 2)
        self.lane = 2
        self.live = True
        self.allowed = True
        self.bridge = OpenAMSFollowCommandBridge(
            object(), object(), self.requests, lambda: self.owner,
            lambda: self.lane, lambda: self.live, lambda: self.allowed,
            bay_count=4, timeout=0.25)

    def test_start_allocates_follow_identity_without_waiter(self):
        def send(mcu, reactor, instance, identity, current, **kwargs):
            self.assertEqual((identity.client, identity.session, identity.request,
                              identity.operation, identity.lane),
                             (HOST, 17, 42, FOLLOW, 2))
            self.assertEqual(kwargs, {'direction': 0, 'cancel': False,
                                      'timeout': 0.25})
            self.assertTrue(current())
            return Admission(instance, identity, 0, 0)

        with patch('host.openams_follow_bridge.send_operation', side_effect=send):
            result = self.bridge.set_oams_follower(1, 0)
        self.assertIs(result.follower_confirmed, False)
        self.assertEqual(result.direction, 0)
        self.assertEqual(result.identity.request, 42)
        self.assertEqual(self.requests.last_request, 42)
        self.assertEqual(self.requests.pending, {})

    def test_disable_targets_completed_load_owner_without_new_request(self):
        def send(mcu, reactor, instance, identity, current, **kwargs):
            self.assertEqual(identity, self.owner)
            self.assertEqual(kwargs, {'direction': None, 'cancel': True,
                                      'timeout': 0.25})
            return Admission(instance, identity, 0, 0)

        self.allowed = False  # A stop must remain available after motion denial.
        with patch('host.openams_follow_bridge.send_operation', side_effect=send):
            result = self.bridge.set_oams_follower(0, 0)
        self.assertIs(result.follower_confirmed, False)
        self.assertIsNone(result.direction)
        self.assertEqual(result.identity, self.owner)
        self.assertEqual(self.requests.last_request, 41)
        self.assertEqual(self.requests.pending, {})

    def test_disable_targets_explicit_follow_identity(self):
        self.requests.last_request = 42
        self.owner = Identity(HOST, 17, 42, FOLLOW, 2)
        with patch('host.openams_follow_bridge.send_operation',
                   return_value=Admission(INSTANCE, self.owner, 1, 0)) as send:
            result = self.bridge.set_oams_follower(0, 1)
        self.assertEqual(result.identity, self.owner)
        self.assertTrue(send.call_args.kwargs['cancel'])
        self.assertEqual(self.requests.last_request, 42)

    def test_reject_unknown_or_stale_owner_and_default_motion_denial(self):
        with patch('host.openams_follow_bridge.send_operation') as send:
            self.allowed = False
            with self.assertRaises(ValueError):
                self.bridge.set_oams_follower(1, 1)
            self.allowed = True
            for owner, lane in ((None, 2), (self.owner, None),
                                (Identity(HOST, 16, 41, LOAD, 2), 2),
                                (Identity(HOST, 17, 41, LOAD, 1), 2),
                                (Identity(2, 17, 41, LOAD, 2), 2)):
                self.owner, self.lane = owner, lane
                with self.subTest(owner=owner, lane=lane):
                    with self.assertRaises(ValueError):
                        self.bridge.set_oams_follower(1, 1)
            send.assert_not_called()

        default_denied = OpenAMSFollowCommandBridge(
            object(), object(), self.requests, lambda: self.owner,
            lambda: self.lane, lambda: True, bay_count=4)
        self.owner, self.lane = Identity(HOST, 17, 41, LOAD, 2), 2
        with patch('host.openams_follow_bridge.send_operation') as send:
            with self.assertRaises(ValueError):
                default_denied.set_oams_follower(1, 1)
            send.assert_not_called()

    def test_reconnect_or_guard_loss_never_reports_success(self):
        def stale(*args, **kwargs):
            self.requests.disconnect()
            identity = args[3]
            return Admission(INSTANCE, identity, 0, 0)

        with patch('host.openams_follow_bridge.send_operation', side_effect=stale):
            with self.assertRaises(ValueError):
                self.bridge.set_oams_follower(1, 1)
        self.assertEqual(self.requests.last_request, 42)
        self.assertFalse(self.requests.pending)

    def test_start_accepts_its_own_takeover_and_rejects_other_owner_or_lane(self):
        def takeover(mcu, reactor, instance, identity, current, **kwargs):
            self.assertTrue(current())
            self.owner = identity
            self.assertTrue(current())
            return Admission(instance, identity, 0, 0)

        with patch('host.openams_follow_bridge.send_operation', side_effect=takeover):
            self.assertEqual(self.bridge.set_oams_follower(1, 1).identity,
                             Identity(HOST, 17, 42, FOLLOW, 2))

        for changed_owner, changed_lane in (
                (Identity(HOST, 17, 99, FOLLOW, 2), 2),
                (self.owner, 3), (None, 2)):
            self.requests.last_request = 41
            self.owner, self.lane = Identity(HOST, 17, 41, LOAD, 2), 2

            def unrelated(mcu, reactor, instance, identity, current, **kwargs):
                self.assertTrue(current())
                self.owner, self.lane = changed_owner, changed_lane
                self.assertFalse(current())
                return Admission(instance, identity, 0, 0)

            with self.subTest(owner=changed_owner, lane=changed_lane):
                with patch('host.openams_follow_bridge.send_operation', side_effect=unrelated):
                    with self.assertRaises(ValueError):
                        self.bridge.set_oams_follower(1, 1)

    def test_cancel_accepts_cleared_owner_and_rejects_other_owner_or_lane(self):
        def cleared(mcu, reactor, instance, identity, current, **kwargs):
            self.assertTrue(current())
            self.owner, self.lane = None, None
            self.assertTrue(current())
            return Admission(instance, identity, 0, 0)

        with patch('host.openams_follow_bridge.send_operation', side_effect=cleared):
            self.assertEqual(self.bridge.set_oams_follower(0, 0).identity,
                             Identity(HOST, 17, 41, LOAD, 2))

        for changed_owner, changed_lane in (
                (Identity(HOST, 17, 99, FOLLOW, 2), 2),
                (None, 3), (Identity(HOST, 17, 41, LOAD, 2), 3)):
            self.owner, self.lane = Identity(HOST, 17, 41, LOAD, 2), 2

            def unrelated(mcu, reactor, instance, identity, current, **kwargs):
                self.assertTrue(current())
                self.owner, self.lane = changed_owner, changed_lane
                self.assertFalse(current())
                return Admission(instance, identity, 0, 0)

            with self.subTest(owner=changed_owner, lane=changed_lane):
                with patch('host.openams_follow_bridge.send_operation', side_effect=unrelated):
                    with self.assertRaises(ValueError):
                        self.bridge.set_oams_follower(0, 0)

    def test_rejections_and_malformed_reply_do_not_claim_following(self):
        def responses(*args, **kwargs):
            identity = args[3]
            return self.reply(INSTANCE, identity)

        for reply in (lambda inst, ident: Admission(inst, ident, 10, 0),
                      lambda inst, ident: Admission(inst, ident, 2, 0),
                      lambda inst, ident: Admission(inst, ident, 0, 1),
                      lambda inst, ident: CommandResult(inst, 1, ident.request, 3),
                      lambda inst, ident: object()):
            self.reply = reply
            with self.subTest(reply=reply):
                with patch('host.openams_follow_bridge.send_operation', side_effect=responses):
                    with self.assertRaises(ValueError):
                        self.bridge.set_oams_follower(1, 1)
        self.assertEqual(self.requests.pending, {})


if __name__ == '__main__':
    unittest.main()
