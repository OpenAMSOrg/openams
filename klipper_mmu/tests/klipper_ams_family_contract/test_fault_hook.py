# SPDX-License-Identifier: GPL-3.0-or-later
"""Exercise the firmware-fault hook of the OpenAMS runtime in OPENAMS_SOURCE."""
from dataclasses import replace
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from openams_source import load

S, runtime_module = load("oams_state", "oams_runtime")


class Completion:
    def __init__(self): self.result = None
    def test(self): return self.result is not None
    def complete(self, value): self.result = value
    def wait(self): return self.result


class Reactor:
    NEVER = 1e99
    def monotonic(self): return 0.0
    def completion(self): return Completion()
    def register_timer(self, callback, when): return callback
    def unregister_timer(self, timer): pass


class Gcode:
    def __init__(self): self.calls = []; self.fail = False
    def run_script(self, script):
        self.calls.append(script)
        if self.fail: raise RuntimeError("pause failed")
    # ACK_FAULT runs inside a G-code command, so the host asks for this path.
    run_script_from_command = run_script


class Printer:
    def __init__(self, reactor, gcode): self.reactor, self.gcode = reactor, gcode
    def get_reactor(self): return self.reactor
    def lookup_object(self, name):
        assert name == "gcode"
        return self.gcode


class Oam:
    def start_load_spool(self, bay, gen=None): self.gen = gen


class FaultHook(unittest.TestCase):
    S, Runtime = S, runtime_module.Runtime

    def setUp(self):
        self.gcode, self.oam = Gcode(), Oam()
        self.runtime = self.Runtime(Printer(Reactor(), self.gcode), ["fps"],
                                    lambda now: self.S.World(), lambda idx: self.oam)
        self.waiter = self.runtime.request("fps", self.S.LoadBay("fps", (1, 2)))

    def test_exact_fault_settles_waiter_pauses_and_is_idempotent(self):
        identity = (11, 42)
        self.assertTrue(self.runtime.accept_firmware_fault("fps", 2, 7, identity))
        self.assertEqual(self.waiter.wait().code, 7)
        self.assertEqual(self.gcode.calls, ["PAUSE"])
        self.assertTrue(self.runtime.accept_firmware_fault("fps", 2, 7, identity))
        self.assertEqual(self.gcode.calls, ["PAUSE"])
        self.assertFalse(self.runtime.accept_firmware_fault("fps", 2, 8, (11, 43)))

    def test_wrong_lane_and_invalid_code_leave_waiter_active(self):
        self.assertFalse(self.runtime.accept_firmware_fault("fps", 3, 7, (11, 42)))
        self.assertFalse(self.runtime.accept_firmware_fault("fps", 2, 0, (11, 42)))
        self.assertFalse(self.waiter.test())

    def test_failed_pause_refuses_ack_then_exact_retry_completes(self):
        self.gcode.fail = True
        self.assertFalse(self.runtime.accept_firmware_fault("fps", 2, 7, (11, 42)))
        self.assertEqual(self.waiter.wait().code, 7)
        self.gcode.fail = False
        self.assertTrue(self.runtime.accept_firmware_fault("fps", 2, 7, (11, 42)))
        self.assertEqual(self.gcode.calls, ["PAUSE", "PAUSE"])

    def test_global_fault_requires_idle_and_no_lane_assignment(self):
        self.assertFalse(self.runtime.accept_firmware_fault(None, 255, 7, (11, 42)))
        self.runtime = self.Runtime(Printer(Reactor(), self.gcode), ["fps"],
                                    lambda now: self.S.World(), lambda idx: self.oam)
        self.assertTrue(self.runtime.accept_firmware_fault(None, 255, 7, (11, 42)))
        self.assertFalse(self.runtime.accept_firmware_fault("fps", 255, 7, (11, 42)))

    def test_generic_clear_errors_preserves_firmware_fault_latch(self):
        identity = (11, 42)
        self.assertTrue(self.runtime.accept_firmware_fault("fps", 2, 7, identity))
        self.runtime.dispatch(self.S.ClearErrors())
        self.assertEqual(self.runtime.get_state().firmware_faults[2],
                         ("fps", 7, identity))
        self.assertFalse(self.runtime.accept_firmware_fault("fps", 2, 8, (11, 43)))
        self.assertTrue(self.runtime.accept_firmware_fault("fps", 2, 7, identity))

    def test_acceptance_cache_is_bounded_by_physical_lane(self):
        self.assertTrue(self.runtime.accept_firmware_fault("fps", 2, 7, (11, 42)))
        self.assertEqual(self.runtime._accepted_firmware_faults,
                         {2: ("fps", 7, (11, 42))})
        self.assertFalse(self.runtime.accept_firmware_fault("fps", 2, 7, (11, 43)))
        self.assertEqual(len(self.runtime._accepted_firmware_faults), 1)

    def test_active_or_uncertain_follower_refuses_fault_ack(self):
        for following, uncertain in ((True, ()), (None, ()),
                                     (False, ((1, 2),))):
            with self.subTest(following=following, uncertain=uncertain):
                lane = replace(self.runtime.get_state().lanes["fps"],
                               following=following,
                               uncertain_follower_units=uncertain)
                self.runtime.system = replace(self.runtime.get_state(),
                                              lanes={"fps": lane})
                self.assertFalse(self.runtime.accept_firmware_fault(
                    "fps", 2, 7, (11, 42)))
                self.assertFalse(self.waiter.test())
                self.assertEqual(self.gcode.calls, [])
                self.assertEqual(self.runtime.get_state().firmware_faults, {})

    @unittest.skipUnless(
        callable(getattr(runtime_module.Runtime, 'accept_firmware_fault_cleared', None)),
        'OPENAMS_SOURCE has no accept_firmware_fault_cleared (it predates the fault clear)')
    def test_verified_clear_removes_latch_and_resyncs_lane(self):
        identity = (11, 42)
        self.assertTrue(self.runtime.accept_firmware_fault("fps", 2, 7, identity))
        self.assertFalse(self.runtime.accept_firmware_fault_cleared(2, ("wrong",)))
        self.assertEqual(self.runtime.get_state().firmware_faults[2],
                         ("fps", 7, identity))
        self.assertTrue(self.runtime.accept_firmware_fault_cleared(2, identity))
        self.assertEqual(self.runtime.get_state().firmware_faults, {})
        lane = self.runtime.get_state().lanes["fps"]
        self.assertEqual(lane.op, self.S.OP_UNLOADED)
        self.assertIsNone(lane.unit)
        self.assertNotIn(2, self.runtime._accepted_firmware_faults)
        # Consumed: the same identity no longer matches anything.
        self.assertFalse(self.runtime.accept_firmware_fault_cleared(2, identity))

    @unittest.skipUnless(
        callable(getattr(runtime_module.Runtime, 'accept_firmware_fault_cleared', None)),
        'OPENAMS_SOURCE has no accept_firmware_fault_cleared (it predates the fault clear)')
    def test_verified_clear_with_no_latch_is_refused(self):
        self.assertFalse(self.runtime.accept_firmware_fault_cleared(2, (11, 42)))

    def test_global_fault_refuses_any_uncertain_follower(self):
        self.runtime = self.Runtime(Printer(Reactor(), self.gcode), ["fps"],
                                    lambda now: self.S.World(), lambda idx: self.oam)
        for following, uncertain in ((True, ()), (None, ()),
                                     (False, ((1, 2),))):
            with self.subTest(following=following, uncertain=uncertain):
                lane = replace(self.runtime.get_state().lanes["fps"],
                               following=following,
                               uncertain_follower_units=uncertain)
                self.runtime.system = replace(self.runtime.get_state(),
                                              lanes={"fps": lane})
                self.assertFalse(self.runtime.accept_firmware_fault(
                    None, 255, 7, (11, 42)))
                self.assertEqual(self.gcode.calls, [])
                self.assertEqual(self.runtime.get_state().firmware_faults, {})


if __name__ == "__main__": unittest.main()
