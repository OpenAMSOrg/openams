# SPDX-License-Identifier: GPL-3.0-or-later
import threading
import unittest
from unittest.mock import patch

from host.openams_device import OpenAMSDevice
from host.openams_follow_bridge import OpenAMSFollowCommandBridge
from host.protocol import Admission, Identity, SensorSample, SensorSnapshot, Snapshot
from host.requests import FOLLOW, HOST, LOAD, Requests


INSTANCE = 0x123456789
OWNER = Identity(HOST, 17, 41, LOAD, 2)


class Observer:
    def __init__(self):
        self._mcu_lock = threading.Lock()
        self.now = 10.0
        self.identity = (3, INSTANCE)
        self.values = {}

    def physical_connection_identity(self):
        return self.identity

    def physical_monotonic(self):
        return self.now

    def capture_physical_state(self):
        fields = dict(path_occupied=True, lane_active=True,
                      terminal_pending=False, position_uncertain=False,
                      pending_fault_event=False, owner_lane=2,
                      occupant_lane=2, system_fault=0, motion_fault=0,
                      acquired_ms=100, client=HOST, session=17, request=41,
                      operation=LOAD, lane=2,
                      client_sessions=(0, 17, 0, 0),
                      client_requests=(0, 41, 0, 0))
        fields.update(self.values)
        sample = lambda present: SensorSample(True, present, True, True, 100)
        sensors = SensorSnapshot(INSTANCE, 1, 4, 100,
                                 tuple(map(sample, (True, False, True, False))),
                                 sample(True), sample(False))
        return self.identity, self.now, Snapshot(INSTANCE, 1, fields), sensors


class FollowCompositionTest(unittest.TestCase):
    def setUp(self):
        self.observer = Observer()
        self.device = OpenAMSDevice('oams 7', self.observer, 7, 'fps',
                                    'boxturtle', 4, 842.5)
        self.requests = Requests(INSTANCE, 17, 41)
        self.bridge = OpenAMSFollowCommandBridge(
            object(), object(), self.requests, self.device.owner_identity,
            lambda: self.device.current_spool,
            lambda: self.observer.identity == (3, INSTANCE),
            lambda: True, bay_count=4)

    def test_coherent_load_owner_permits_follow_but_lost_proof_never_sends(self):
        self.assertTrue(self.device.refresh())
        self.assertEqual(self.device.owner_identity(), OWNER)
        self.assertEqual(self.device.current_spool, 2)

        def send(mcu, reactor, instance, identity, current, **kwargs):
            self.assertEqual(identity, Identity(HOST, 17, 42, FOLLOW, 2))
            self.assertTrue(current())
            return Admission(instance, identity, 0, 0)

        with patch('host.openams_follow_bridge.send_operation', side_effect=send) as transport:
            self.assertEqual(self.bridge.set_oams_follower(1, 0).identity.request, 42)
            self.assertEqual(transport.call_count, 1)

        for change in ('expired', 'unknown', 'conflicting'):
            with self.subTest(change=change):
                self.observer.now = 10.0
                self.observer.values = {}
                if change == 'unknown':
                    self.observer.values['occupant_lane'] = 255
                elif change == 'conflicting':
                    self.observer.values['owner_lane'] = 1
                self.assertEqual(self.device.refresh(), change == 'expired')
                if change == 'expired':
                    self.observer.now += 2.0
                self.assertIsNone(self.device.owner_identity())
                with patch('host.openams_follow_bridge.send_operation') as transport:
                    with self.assertRaises(ValueError):
                        self.bridge.set_oams_follower(1, 0)
                    transport.assert_not_called()


if __name__ == '__main__':
    unittest.main()
