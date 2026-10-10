"""Per-unit read-only gating: a Klipper-MMU unit beside a normal AMS.

A `[klipper_mmu NAME]` unit is a physical observation: it never takes motion.
It must refuse its own commands, naming itself, and leave every other unit
free to load, unload and follow.
"""

import types
import unittest

from test_oams_manager import (FakeError, FakeFps, FakeGroup, FakeOam, S,
                               build_manager)
from test_oams_manager_mmu_discovery import MMUObserver, ReadOnlyUnit


class ReadOnlyWithFollower(ReadOnlyUnit):
    """The read-only unit, plus the follower stop the cleanup path calls."""

    def __init__(self, idx, fps="fps2"):
        super().__init__(idx, fps)
        self.follower_calls = []

    def set_oams_follower(self, enable, direction):
        self.follower_calls.append((enable, direction))


class Unit(FakeOam):
    """A normal unit that records the follower stops the manager sends."""

    def __init__(self, idx, fps=None):
        super().__init__(idx, fps)
        self.follower_calls = []

    def set_oams_follower(self, enable, direction):
        self.follower_calls.append((enable, direction))


class Gcmd:
    """A G-code command with the real get_int signature (optional default)."""

    def __init__(self, params):
        self._p = dict(params)
        self.responses = []

    def get(self, key, default="__required__"):
        if key in self._p:
            return self._p[key]
        if default == "__required__":
            raise FakeError("missing %s" % key)
        return default

    def get_int(self, key, default="__required__", minval=None, maxval=None):
        if key not in self._p:
            if default == "__required__":
                raise FakeError("missing %s" % key)
            return default
        return int(self._p[key])

    def error(self, message):
        return FakeError(message)

    def respond_info(self, message):
        self.responses.append(message)


class RecordingRuntime:
    def __init__(self, lanes):
        self._sys = S.SystemState(lanes=lanes)
        self.requests = []
        self.dispatched = []
        self.result = S.OpResult(True, S.OAMS_OP_CODE_SUCCESS, "ok")

    def get_state(self):
        return self._sys

    def dispatch(self, action):
        self.dispatched.append(action)

    def tick(self):
        pass

    def request(self, fps, action):
        self.requests.append((fps, action))
        return types.SimpleNamespace(wait=lambda: self.result)


def build_system():
    """Two lanes: fps1 feeds a normal AMS (idx 1), fps2 the read-only MMU
    (idx 7). Group T0 is the AMS bay, T1 the MMU bay."""
    read_only = ReadOnlyWithFollower(7, "fps2")
    objects = {
        "fps": [("fps a", FakeFps("fps1")), ("fps b", FakeFps("fps2"))],
        "oams": [("oams oams1", Unit(1, "fps1"))],
        "klipper_mmu": [("klipper_mmu box", MMUObserver(read_only))],
        "filament_group": [("filament_group T0", FakeGroup([("oams1", 0)])),
                           ("filament_group T1", FakeGroup([("box", 0)]))],
    }
    manager = build_manager(objects, ["fps a", "fps b", "oams oams1",
                                      "klipper_mmu box",
                                      "filament_group T0",
                                      "filament_group T1"])
    manager.ready = True
    manager.runtime = RecordingRuntime({
        "fps1": S.LaneState(op=S.OP_LOADED, unit=(1, 0), group="T0"),
        "fps2": S.LaneState(op=S.OP_LOADED, unit=(7, 0), group="T1")})
    return manager


class ReadOnlyUnitGatingTests(unittest.TestCase):
    def test_group_load_refuses_only_the_read_only_unit(self):
        manager = build_system()
        manager.cmd_LOAD_FILAMENT(Gcmd({"GROUP": "T0"}))
        fps, action = manager.runtime.requests[-1]
        self.assertEqual(fps, "fps1")
        self.assertIsInstance(action, S.Load)
        self.assertEqual(action.group, "T0")
        with self.assertRaisesRegex(FakeError, "motion unavailable for box"):
            manager.cmd_LOAD_FILAMENT(Gcmd({"GROUP": "T1"}))
        self.assertEqual(len(manager.runtime.requests), 1)

    def test_slot_load_refuses_only_the_unit_holding_the_slot(self):
        manager = build_system()
        slots = manager._slot_id_map()
        manager.cmd_LOAD_FILAMENT(Gcmd({"GROUP": "T0",
                                        "SLOT": slots[("oams1", 0)]}))
        fps, action = manager.runtime.requests[-1]
        self.assertEqual(fps, "fps1")
        self.assertIsInstance(action, S.LoadBay)
        self.assertEqual(action.unit, (1, 0))
        with self.assertRaisesRegex(FakeError, "motion unavailable for box"):
            manager.cmd_LOAD_FILAMENT(Gcmd({"GROUP": "T1",
                                            "SLOT": slots[("box", 0)]}))

    def test_unload_refuses_only_the_read_only_lane(self):
        manager = build_system()
        manager.cmd_UNLOAD_FILAMENT(Gcmd({"FPS": "fps1"}))
        self.assertEqual(manager.runtime.requests[-1],
                         ("fps1", S.Unload("fps1")))
        with self.assertRaisesRegex(FakeError, "motion unavailable for box"):
            manager.cmd_UNLOAD_FILAMENT(Gcmd({"FPS": "fps2"}))
        # A refused unload still stops the followers it would have left armed.
        self.assertEqual(manager.oams["oams1"].follower_calls,
                         [(0, S.FOLLOWER_REVERSE)])

    def test_follower_arms_on_the_normal_lane_only(self):
        manager = build_system()
        manager.cmd_FOLLOWER(Gcmd({"FPS": "fps1", "ENABLE": "1",
                                   "DIRECTION": "0"}))
        self.assertIn(S.Follow("fps1", 1, 0), manager.runtime.dispatched)
        with self.assertRaisesRegex(FakeError, "motion unavailable for box"):
            manager.cmd_FOLLOWER(Gcmd({"FPS": "fps2", "ENABLE": "1",
                                       "DIRECTION": "0"}))

    def test_read_only_unit_does_not_hold_the_startup_resync(self):
        manager = build_system()
        manager.ready = False
        manager._startup_resync_pending = True
        self.assertTrue(manager._finish_startup_resync_if_ready())
        self.assertTrue(manager.ready)
        self.assertIn(S.ClearErrors(keep_in_flight=True),
                      manager.runtime.dispatched)

    def test_broadcast_command_skips_the_read_only_unit_and_says_so(self):
        manager = build_system()
        cleared = []
        for name, oam in manager.oams.items():
            oam.clear_errors = (
                (lambda unit: lambda: cleared.append(unit))(oam)
                if name != "box" else
                (lambda: self.fail("a read-only unit was cleared")))
        manager.monitor_timer = None
        manager.reactor.NOW = 0.0
        manager.reactor.register_timer = lambda *args: None
        manager.reactor.unregister_timer = lambda *args: None
        gcmd = Gcmd({})
        manager.cmd_CLEAR_ERRORS(gcmd)
        self.assertEqual(len(cleared), 1)
        self.assertEqual(len(manager.runtime.dispatched), 1)
        self.assertIn("box", gcmd.responses[0])
        self.assertIn("skipped read-only", gcmd.responses[0])


if __name__ == "__main__":
    unittest.main()
