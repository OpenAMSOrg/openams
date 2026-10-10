# SPDX-License-Identifier: GPL-3.0-or-later
import threading
import unittest

from host.openams_device import OpenAMSDevice
from host.openams_follow_bridge import FollowCommand
from host.openams_verified_follower import OpenAMSVerifiedFollowerBridge
from host.openams_device_state import FollowerObservation
from host.protocol import Admission, Identity, SensorSample, SensorSnapshot, Snapshot
from host.requests import FOLLOW, HOST, LOAD


INSTANCE = 0x123456789
UNIT = (7, 2)
LOAD_OWNER = Identity(HOST, 17, 41, LOAD, 2)
FOLLOW_OWNER = Identity(HOST, 17, 42, FOLLOW, 2)


class Observer:
    def __init__(self):
        self._mcu_lock = threading.Lock()
        self.identity = (3, INSTANCE)
        self.now = 1.0
        self.query = 1
        self.armed = False
        self.owner = LOAD_OWNER
        self.occupied = True
        self.fault = 0

    def physical_connection_identity(self):
        return self.identity

    def physical_monotonic(self):
        return self.now

    def capture_physical_state(self):
        owner = self.owner
        lane = 2 if self.occupied else 255
        values = dict(path_occupied=self.occupied, lane_active=owner is not None,
                      terminal_pending=False, position_uncertain=False,
                      pending_fault_event=False, owner_lane=lane,
                      occupant_lane=lane, system_fault=self.fault, motion_fault=0,
                      stage=3 if self.armed else 0,
                      state=4 if self.armed else 0,
                      follower_state=1 if self.armed else 0,
                      follower_direction=1, follower_fault=0,
                      acquired_ms=100, client=owner.client if owner else 0,
                      session=owner.session if owner else 0,
                      request=owner.request if owner else 0,
                      operation=owner.operation if owner else 0,
                      lane=owner.lane if owner else 255,
                      client_sessions=(0, 17, 0, 0),
                      client_requests=(0, 42, 0, 0))
        sample = lambda present: SensorSample(True, present, True, True, 100)
        bays = tuple(sample(p) for p in (False, False, self.occupied, False))
        sensors = SensorSnapshot(INSTANCE, self.query, 4, 100, bays,
                                 sample(self.occupied), sample(False))
        return (self.identity, self.now, Snapshot(INSTANCE, self.query, values),
                sensors)


class Runtime:
    """Strict receiver: an already tracked unit is required for acceptance."""
    def __init__(self):
        self.tracked = UNIT
        self.calls = []

    def accept_verified_follower(self, fps, unit, enabled):
        if fps != 'fps' or unit != self.tracked:
            return False
        self.calls.append((fps, unit, enabled))
        return True


class VerifiedFollowerTests(unittest.TestCase):
    def setUp(self):
        self.observer = Observer()
        self.device = OpenAMSDevice('oams 7', self.observer, 7, 'fps',
                                    'boxturtle', 4, 842.5)
        self.runtime = Runtime()
        self.bridge = OpenAMSVerifiedFollowerBridge(self.device, self.runtime)
        self.assertTrue(self.device.refresh())
        self.before = self.device.follower_observation()
        self.command = FollowCommand(FOLLOW_OWNER,
                                     Admission(INSTANCE, FOLLOW_OWNER, 0, 0),
                                     direction=1)

    def armed(self):
        self.observer.query += 1
        self.observer.armed = True
        self.observer.owner = FOLLOW_OWNER
        self.assertTrue(self.device.refresh())

    def test_enabled_requires_later_exact_host_owner(self):
        self.assertFalse(self.bridge.accept(self.command, self.before, UNIT, True))
        self.armed()
        self.assertTrue(self.bridge.accept(self.command, self.before, UNIT, True))
        self.assertEqual(self.runtime.calls, [('fps', UNIT, True)])
        self.runtime.calls.clear()
        self.observer.query += 1
        self.observer.owner = LOAD_OWNER
        self.assertTrue(self.device.refresh())
        self.assertFalse(self.bridge.accept(self.command, self.before, UNIT, True))
        self.assertEqual(self.runtime.calls, [])

    def test_stop_clears_only_previously_tracked_unit(self):
        self.armed()
        before_stop = self.device.follower_observation()
        self.observer.query += 1
        self.observer.armed = False
        self.observer.owner = None
        self.observer.occupied = False
        self.assertTrue(self.device.refresh())
        self.assertIsNone(self.device.follower_observation().bay)
        self.assertTrue(self.bridge.accept(self.command, before_stop, UNIT, False))
        self.assertEqual(self.runtime.calls, [('fps', UNIT, False)])
        self.runtime.calls.clear()
        self.runtime.tracked = None
        self.assertFalse(self.bridge.accept(self.command, before_stop, UNIT, False))
        self.assertEqual(self.runtime.calls, [])
        self.assertFalse(self.bridge.accept(self.command, before_stop,
                                            (8, 2), False))
        # An automatic post-load follower is stopped by cancelling LOAD.
        self.runtime.tracked = UNIT
        load_cancel = FollowCommand(LOAD_OWNER,
                                    Admission(INSTANCE, LOAD_OWNER, 0, 0))
        self.assertTrue(self.bridge.accept(load_cancel, before_stop, UNIT, False))

    def test_stale_unknown_and_new_generation_do_not_mutate_runtime(self):
        self.armed()
        self.observer.now += 2.1
        self.assertFalse(self.bridge.accept(self.command, self.before, UNIT, True))
        self.observer.now += 0.1
        self.observer.query += 1
        self.observer.fault = 1
        self.assertFalse(self.device.refresh())
        self.assertFalse(self.bridge.accept(self.command, self.before, UNIT, True))
        self.observer.fault = 0
        self.observer.identity = (4, INSTANCE)
        self.observer.query = 1
        self.assertTrue(self.device.refresh())
        self.assertFalse(self.bridge.accept(self.command, self.before, UNIT, True))
        self.assertEqual(self.runtime.calls, [])

    def test_direction_mismatch_and_legacy_result_do_not_confirm(self):
        self.armed()
        reverse = FollowCommand(FOLLOW_OWNER,
                                Admission(INSTANCE, FOLLOW_OWNER, 0, 0),
                                direction=0)
        self.assertFalse(self.bridge.accept(reverse, self.before, UNIT, True))
        legacy = FollowCommand(FOLLOW_OWNER,
                               Admission(INSTANCE, FOLLOW_OWNER, 0, 0))
        self.assertFalse(self.bridge.accept(legacy, self.before, UNIT, True))
        self.assertEqual(self.runtime.calls, [])

    def test_wrong_identity_and_admission_cannot_confirm(self):
        self.armed()
        wrong = FollowCommand(Identity(HOST, 17, 43, FOLLOW, 2),
                              Admission(INSTANCE, Identity(HOST, 17, 43,
                                                           FOLLOW, 2), 0, 0))
        self.assertFalse(self.bridge.accept(wrong, self.before, UNIT, True))
        rejected = FollowCommand(FOLLOW_OWNER,
                                 Admission(INSTANCE, FOLLOW_OWNER, 2, 0))
        self.assertFalse(self.bridge.accept(rejected, self.before, UNIT, True))
        self.assertFalse(self.bridge.accept(self.command, self.before,
                                            (7, 1), True))
        self.assertEqual(self.runtime.calls, [])

    def test_before_capture_must_identify_the_same_unit_bay(self):
        self.armed()
        wrong_bay = FollowerObservation(False, None, 1, INSTANCE, 3, 1)
        unknown_bay = FollowerObservation(False, None, None, INSTANCE, 3, 1)
        for before in (wrong_bay, unknown_bay):
            with self.subTest(before=before):
                self.assertFalse(self.bridge.accept(self.command, before,
                                                    UNIT, True))
        self.assertEqual(self.runtime.calls, [])


if __name__ == '__main__':
    unittest.main()
