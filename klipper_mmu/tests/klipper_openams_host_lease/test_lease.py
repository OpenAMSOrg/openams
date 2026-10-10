# SPDX-License-Identifier: GPL-3.0-or-later
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from host.event_delivery import EventDelivery
from host.openams_host_lease import HostLease
from host.openams_session_bootstrap import PreparedHostSession
from host.protocol import CommandResult
from host.requests import Requests

INSTANCE = 0x1020304050607080


class Reactor:
    now = 1.0

    def monotonic(self):
        return self.now


class LeaseTest(unittest.TestCase):
    def setUp(self):
        self.reactor = Reactor()
        self.current = True
        self.prepared = PreparedHostSession(Requests(INSTANCE, 12),
                                            EventDelivery(INSTANCE, 4), 9)
        self.lease = HostLease(self.prepared, object(), self.reactor, 4,
                               lambda i, g: self.current and
                               (i, g) == (INSTANCE, 4), 500, 200)

    def test_requires_confirmed_renewal_and_live_board(self):
        self.assertFalse(self.lease.motion_permitted(lambda: True))
        with patch('host.openams_host_lease.send_heartbeat',
                   return_value=CommandResult(INSTANCE, 3, 12, 0)) as beat:
            self.assertTrue(self.lease.renew())
        self.assertEqual(beat.call_args.args[2:4], (INSTANCE, 12))
        self.assertTrue(self.lease.motion_permitted(lambda: True))
        self.assertFalse(self.lease.motion_permitted(lambda: False))
        self.reactor.now = 1.199
        self.assertTrue(self.lease.motion_permitted(lambda: True))
        self.reactor.now = 1.2
        self.assertFalse(self.lease.motion_permitted(lambda: True))
        self.assertFalse(self.lease.available)

    def test_expired_renewal_never_sends_or_revives(self):
        with patch('host.openams_host_lease.send_heartbeat',
                   return_value=CommandResult(INSTANCE, 3, 12, 0)) as beat:
            self.lease.renew()
            self.reactor.now = 1.2
            with self.assertRaisesRegex(ConnectionError, 'expired'):
                self.lease.renew()
            self.assertEqual(beat.call_count, 1)
        self.assertFalse(self.lease.available)
        self.assertFalse(self.prepared.requests.connected)
        self.assertFalse(self.prepared.delivery.connected)

    def test_delayed_confirmed_reply_cannot_extend_lease(self):
        def delayed(*args, **kwargs):
            self.reactor.now = 1.2
            return CommandResult(INSTANCE, 3, 12, 0)
        with patch('host.openams_host_lease.send_heartbeat', side_effect=delayed):
            with self.assertRaisesRegex(ConnectionError, 'reply arrived'):
                self.lease.renew()
        self.assertFalse(self.lease.available)
        self.assertFalse(self.prepared.requests.connected)
        self.assertFalse(self.prepared.delivery.connected)
        self.assertFalse(self.lease.motion_permitted(lambda: True))

    def test_early_reply_uses_pre_send_time(self):
        def delayed(*args, **kwargs):
            self.reactor.now = 1.1
            return CommandResult(INSTANCE, 3, 12, 0)
        with patch('host.openams_host_lease.send_heartbeat', side_effect=delayed):
            self.lease.renew()
        self.assertEqual(self.lease.confirmed_at, 1.0)
        self.reactor.now = 1.2
        self.assertFalse(self.lease.motion_permitted(lambda: True))

    def test_bad_identity_rejection_and_timeout_latch_unavailable(self):
        for outcome in (CommandResult(INSTANCE, 3, 13, 0),
                        CommandResult(INSTANCE, 3, 12, 4),
                        CommandResult(INSTANCE + 1, 3, 12, 0),
                        ValueError('MMU command timed out')):
            with self.subTest(outcome=outcome):
                self.setUp()
                kwargs = ({'side_effect': outcome} if isinstance(outcome, Exception)
                          else {'return_value': outcome})
                with patch('host.openams_host_lease.send_heartbeat', **kwargs):
                    with self.assertRaises(ValueError):
                        self.lease.renew()
                self.assertFalse(self.lease.available)
                self.assertFalse(self.prepared.requests.connected)
                self.assertFalse(self.prepared.delivery.connected)
                self.assertFalse(self.lease.motion_permitted(lambda: True))

    def test_generation_change_and_disconnect_close_helpers(self):
        self.current = False
        self.assertFalse(self.lease.motion_permitted(lambda: True))
        self.assertFalse(self.prepared.requests.connected)
        self.assertFalse(self.prepared.delivery.connected)
        with patch('host.openams_host_lease.send_heartbeat') as beat:
            with self.assertRaises(ConnectionError):
                self.lease.renew()
            beat.assert_not_called()

    def test_changed_during_heartbeat_cannot_renew(self):
        def respond(*args, **kwargs):
            self.current = False
            return CommandResult(INSTANCE, 3, 12, 0)
        with patch('host.openams_host_lease.send_heartbeat', side_effect=respond):
            with self.assertRaises(ConnectionError):
                self.lease.renew()
        self.assertFalse(self.lease.available)

    def test_invalid_watchdog_bound_rejected(self):
        for age in (0, 500, 501):
            with self.subTest(age=age), self.assertRaises(ValueError):
                HostLease(self.prepared, object(), self.reactor, 4,
                          lambda i, g: True, 500, age)


if __name__ == '__main__':
    unittest.main()
