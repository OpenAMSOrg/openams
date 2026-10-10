# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise the OpenAMS terminal hook and follower STOP gate in OPENAMS_SOURCE."""
from pathlib import Path
import sys
import types
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from openams_source import load

S, runtime_module = load("oams_state", "oams_runtime")


class Completion:
    def __init__(self):
        self.result = None

    def test(self):
        return self.result is not None

    def complete(self, result):
        self.result = result

    def wait(self):
        return self.result


class Reactor:
    def monotonic(self):
        return 0.0

    def completion(self):
        return Completion()

    def register_timer(self, callback, when):
        return callback

    def unregister_timer(self, timer):
        pass


class Printer:
    def get_reactor(self):
        return Reactor()


class Oam:
    def __init__(self):
        self.stops = []
        self.stop_result = None
        self.stop_error = None

    def start_load_spool(self, bay, gen=None):
        self.gen = gen

    def load_spool_cancel(self):
        pass

    def set_oams_follower(self, enable, direction):
        self.stops.append((enable, direction))
        if self.stop_error:
            raise self.stop_error
        return self.stop_result


class RuntimeHook(unittest.TestCase):
    S, Runtime = S, runtime_module.Runtime

    def setUp(self):
        self.oam = Oam()
        self.runtime = self.Runtime(Printer(), ["fps"],
                                    lambda now: self.S.World(),
                                    lambda idx: self.oam if idx == 1 else None)
        self.waiter = self.runtime.request("fps", self.S.LoadBay("fps", (1, 0)))
        self.gen = self.oam.gen
        self.assertFalse(self.waiter.test())

    def test_success_settles_exact_waiter(self):
        self.assertTrue(self.runtime.accept_terminal("fps", 0, 0, self.gen))
        self.assertTrue(self.waiter.wait().ok)
        self.assertFalse(self.runtime.accept_terminal("fps", 0, 0, self.gen))

    def test_timeout_cannot_accept_later_success(self):
        self.runtime.dispatch(self.S.Timeout("fps"))
        self.assertFalse(self.runtime.accept_terminal("fps", 0, 0, self.gen))
        self.assertFalse(self.waiter.wait().ok)

    def test_swallowed_dispatch_failure_does_not_accept(self):
        original = self.runtime.dispatch
        self.runtime.dispatch = lambda action: self.runtime.get_state()
        self.assertFalse(self.runtime.accept_terminal("fps", 0, 0, self.gen))
        self.assertFalse(self.waiter.test())
        self.runtime.dispatch = original
        self.assertTrue(self.runtime.accept_terminal("fps", 0, 0, self.gen))

    def test_wrong_outcome_does_not_accept(self):
        original = self.runtime.dispatch
        self.runtime.dispatch = lambda action: original(
            self.S.OpCompleted(action.fps, 7, action.value, action.gen))
        self.assertFalse(self.runtime.accept_terminal("fps", 0, 0, self.gen))
        self.assertEqual(self.waiter.wait().code, 7)

    def test_wrong_generation_does_not_dispatch(self):
        self.assertFalse(self.runtime.accept_terminal("fps", 0, 0, self.gen + 1))
        self.assertFalse(self.waiter.test())

    def test_failed_load_stop_exception_keeps_terminal_retryable(self):
        self.runtime.system = self.S.replace(
            self.runtime.system,
            lanes={"fps": self.S.replace(self.runtime.system.lanes["fps"],
                                         following=True)})
        before = self.runtime.get_state()
        self.oam.stop_error = RuntimeError("send failed")
        self.assertFalse(self.runtime.accept_terminal("fps", 7, 0, self.gen))
        self.assertIs(self.runtime.get_state(), before)
        self.assertFalse(self.waiter.test())
        self.oam.stop_error = None
        self.oam.stop_result = types.SimpleNamespace(follower_confirmed=True)
        self.assertTrue(self.runtime.accept_terminal("fps", 7, 0, self.gen))
        self.assertEqual(len(self.oam.stops), 2)
        self.assertEqual(self.waiter.wait().code, 7)

    def test_failed_load_unconfirmed_stop_waits_without_resending(self):
        self.runtime.system = self.S.replace(
            self.runtime.system,
            lanes={"fps": self.S.replace(self.runtime.system.lanes["fps"],
                                         following=True)})
        before = self.runtime.get_state()
        self.oam.stop_result = types.SimpleNamespace(follower_confirmed=False)
        self.assertFalse(self.runtime.accept_terminal("fps", 7, 0, self.gen))
        self.assertIs(self.runtime.get_state(), before)
        self.assertFalse(self.waiter.test())
        self.oam.stop_result = types.SimpleNamespace(follower_confirmed=True)
        for _ in range(3):
            self.assertFalse(self.runtime.accept_terminal("fps", 7, 0, self.gen))
            self.assertIs(self.runtime.get_state(), before)
            self.assertFalse(self.waiter.test())
        self.assertEqual(len(self.oam.stops), 1)
        self.assertTrue(self.runtime.accept_verified_follower("fps", (1, 0), False))
        self.assertTrue(self.runtime.accept_terminal("fps", 7, 0, self.gen))
        self.assertEqual(len(self.oam.stops), 1)
        self.assertFalse(self.runtime.accept_terminal("fps", 7, 0, self.gen))
        self.assertEqual(len(self.oam.stops), 1)

    def test_verified_stop_allows_retained_terminal(self):
        self.runtime.system = self.S.replace(
            self.runtime.system,
            lanes={"fps": self.S.replace(self.runtime.system.lanes["fps"],
                                         following=True)})
        self.oam.stop_result = types.SimpleNamespace(follower_confirmed=False)
        self.assertFalse(self.runtime.accept_terminal("fps", 7, 0, self.gen))
        self.assertTrue(self.runtime.accept_verified_follower("fps", (1, 0), False))
        self.assertTrue(self.runtime.accept_terminal("fps", 7, 0, self.gen))
        self.assertEqual(len(self.oam.stops), 1)
        self.assertEqual(self.waiter.wait().code, 7)

    def test_failed_unload_waits_for_verified_stop(self):
        self.assertTrue(self.runtime.accept_terminal("fps", 0, 0, self.gen))
        self.oam.start_unload_spool = lambda gen=None: setattr(self.oam, "gen", gen)
        waiter = self.runtime.request("fps", self.S.Unload("fps"))
        gen = self.oam.gen
        self.oam.stop_result = types.SimpleNamespace(follower_confirmed=False)
        before = self.runtime.get_state()
        self.assertFalse(self.runtime.accept_terminal("fps", 7, 0, gen))
        self.assertIs(self.runtime.get_state(), before)
        self.assertFalse(waiter.test())
        self.assertTrue(self.runtime.accept_verified_follower("fps", (1, 0), False))
        self.assertTrue(self.runtime.accept_terminal("fps", 7, 0, gen))
        self.assertEqual(len(self.oam.stops), 1)

    def test_follow_on_invalidates_old_stop_proof(self):
        self.runtime.system = self.S.replace(
            self.runtime.system,
            lanes={"fps": self.S.replace(self.runtime.system.lanes["fps"],
                                         following=True)})
        self.oam.stop_result = types.SimpleNamespace(follower_confirmed=False)
        self.assertFalse(self.runtime.accept_terminal("fps", 7, 0, self.gen))
        self.assertTrue(self.runtime.accept_verified_follower("fps", (1, 0), False))
        self.assertTrue(self.runtime.accept_verified_follower("fps", (1, 0), True))
        self.assertFalse(self.runtime.accept_terminal("fps", 7, 0, self.gen))
        self.assertFalse(self.waiter.test())

    def test_prior_generation_stop_proof_cannot_accept_terminal(self):
        self.runtime.system = self.S.replace(
            self.runtime.system,
            lanes={"fps": self.S.replace(self.runtime.system.lanes["fps"],
                                         following=True)})
        self.runtime._terminal_stop_proof = {"fps": (self.gen - 1, (1, 0))}
        self.oam.stop_result = types.SimpleNamespace(follower_confirmed=False)
        self.assertFalse(self.runtime.accept_terminal("fps", 7, 0, self.gen))
        self.assertEqual(len(self.oam.stops), 1)
        self.assertFalse(self.waiter.test())

    def test_follow_command_invalidates_stop_proof(self):
        self.runtime.system = self.S.replace(
            self.runtime.system,
            lanes={"fps": self.S.replace(self.runtime.system.lanes["fps"],
                                         following=True)})
        self.oam.stop_result = types.SimpleNamespace(follower_confirmed=False)
        self.assertFalse(self.runtime.accept_terminal("fps", 7, 0, self.gen))
        self.assertTrue(self.runtime.accept_verified_follower("fps", (1, 0), False))
        self.runtime.dispatch(self.S.Follow("fps", 1))
        self.assertFalse(self.runtime.accept_terminal("fps", 7, 0, self.gen))
        self.assertFalse(self.waiter.test())

    def test_calibration_terminal_does_not_send_stop(self):
        self.runtime.dispatch(self.S.Timeout("fps"))
        self.oam.start_calibrate = lambda kind, bay, gen=None: setattr(self.oam, "gen", gen)
        waiter = self.runtime.request("fps", self.S.Calibrate("fps", 1, 0, "ptfe"))
        gen = self.oam.gen
        self.assertTrue(self.runtime.accept_terminal("fps", 7, 42, gen))
        self.assertEqual(waiter.wait().value, 42)
        self.assertEqual(self.oam.stops, [])


if __name__ == "__main__":
    unittest.main()
