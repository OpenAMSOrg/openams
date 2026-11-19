"""OAMSM_UPDATE_HOLD: a Cortex Bridge refuses its firmware update while an
AMS is busy, and the forward follower of a loaded lane counts. The hold stops
every follower through the store and keeps the manager from re-arming one
until it is released, Klipper restarts, a print starts or it times out."""

import types
import unittest

from test_follower_after_restart import lane_of, restarted_manager
from test_oams_manager import FakeError, FakeGcmd, S, manager_mod


def held_manager(print_state="standby"):
    """A loaded lane whose forward follower is armed, the printer idle."""
    mgr, unit = restarted_manager(following=True,
                                  direction=S.FOLLOWER_FORWARD)
    now = [100.0]
    mgr.reactor = types.SimpleNamespace(monotonic=lambda: now[0])
    stats = types.SimpleNamespace(
        get_status=lambda eventtime: {"state": state[0]})
    state = [print_state]
    lookup = mgr.printer.lookup_object

    def lookup_object(name, *default):
        if name == "print_stats":
            return stats
        return lookup(name)
    mgr.printer.lookup_object = lookup_object
    return mgr, unit, now, state


class Gcmd(FakeGcmd):
    """Klipper's get_int signature: a default before the bounds."""

    def get_int(self, key, default=None, minval=None, maxval=None):
        return int(self._p.get(key, default))


def hold(mgr, enable):
    gcmd = Gcmd({"ENABLE": enable})
    mgr.cmd_UPDATE_HOLD(gcmd)
    return gcmd.responses


class UpdateHoldTests(unittest.TestCase):
    def test_the_hold_stops_every_follower_through_the_store(self):
        mgr, unit, _, _ = held_manager()
        responses = hold(mgr, 1)
        self.assertIn((0, S.FOLLOWER_REVERSE), unit.sent)
        self.assertIs(lane_of(mgr).following, False, "LaneState says so")
        self.assertIn(S.Follow("fps1", 0, S.FOLLOWER_REVERSE),
                      mgr.runtime.actions)
        self.assertIn("held off for a bridge update (loaded: fps1)",
                      responses[0])

    def test_no_re_arm_while_held_by_any_path(self):
        mgr, unit, now, _ = held_manager()
        hold(mgr, 1)
        unit.sent.clear()
        # The restart loop, with a pending re-arm queued anyway.
        mgr._follower_rearm["fps1"] = {"tries": 0, "next": 0.0}
        mgr._reestablish_followers_after_restart()
        # A unit reachable again (around the bridge's detach).
        mgr._rearm_followers_of_unit(1)
        mgr._reestablish_followers_after_restart()
        now[0] += 60.0
        mgr._reestablish_followers_after_restart()
        self.assertEqual(unit.sent, [], "nothing re-armed")
        self.assertIs(lane_of(mgr).following, False)

    def test_the_release_re_arms_the_loaded_lane(self):
        mgr, unit, _, _ = held_manager()
        hold(mgr, 1)
        unit.sent.clear()
        responses = hold(mgr, 0)
        self.assertIn("re-arm", responses[0])
        mgr._reestablish_followers_after_restart()
        self.assertEqual(unit.sent, [(1, S.FOLLOWER_FORWARD)])
        self.assertIs(lane_of(mgr).following, True)

    def test_releasing_without_a_hold_says_so(self):
        mgr, _, _, _ = held_manager()
        self.assertIn("were not held", hold(mgr, 0)[0])

    def test_refused_while_a_print_runs_or_is_paused(self):
        for print_state in ("printing", "paused"):
            mgr, unit, _, _ = held_manager(print_state)
            with self.assertRaises(FakeError) as caught:
                hold(mgr, 1)
            self.assertIn("while a print is %s" % print_state,
                          str(caught.exception))
            self.assertEqual(unit.sent, [])
            self.assertIs(lane_of(mgr).following, True)

    def test_a_hold_nobody_releases_ends_by_itself(self):
        mgr, unit, now, _ = held_manager()
        hold(mgr, 1)
        unit.sent.clear()
        now[0] += manager_mod.UPDATE_HOLD_MAX_S + 1
        mgr._reestablish_followers_after_restart()
        self.assertEqual(unit.sent, [(1, S.FOLLOWER_FORWARD)])

    def test_a_print_start_releases_the_hold(self):
        mgr, unit, _, state = held_manager()
        hold(mgr, 1)
        unit.sent.clear()
        state[0] = "printing"
        mgr._print_was_active = False
        mgr._arm_followers_for_print(100.0)
        self.assertIsNone(mgr._update_hold_until)
        self.assertIn("fps1", mgr._follower_rearm)

    def test_a_klipper_start_begins_without_the_hold(self):
        # handle_ready resets it: nothing saves the hold across a restart.
        source = manager_mod.OAMSManager.handle_ready
        self.assertIn("self._update_hold_until = None",
                      __import__("inspect").getsource(source))


if __name__ == "__main__":
    unittest.main()
