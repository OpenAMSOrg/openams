"""The runtime hook surface klipper-mmu binds an OpenAMS unit to.

klipper_mmu/host/openams_managed_unit.py refuses to bind a runtime that lacks
any of these, and calls each with the signature checked here. The contract is
owned by openams because the add-on lives in this repository: a rename or a
reordered parameter here breaks every Klipper-MMU unit at connect.
"""

import importlib
import inspect
import os
import sys
import types
import unittest

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if "oamspkg" not in sys.modules:
    _pkg = types.ModuleType("oamspkg")
    _pkg.__path__ = [_SRC]
    sys.modules["oamspkg"] = _pkg
sys.modules.setdefault("mcu", types.ModuleType("mcu"))

runtime_mod = importlib.import_module("oamspkg.oams_runtime")
Runtime = runtime_mod.Runtime
S = importlib.import_module("oamspkg.oams_state")

# name -> positional parameter names, exactly as klipper-mmu calls the hook.
HOOKS = {
    "dispatch": ["action"],
    "accept_terminal": ["fps", "code", "value", "gen"],
    "accept_reload_terminal": ["fps", "code", "value", "gen", "bay"],
    "accept_retired_terminal": ["fps", "unit", "gen", "code", "value",
                                "disarmed_proof"],
    "accept_verified_follower": ["fps", "unit", "following"],
    "accept_firmware_fault": ["fps", "physical_lane", "code", "identity",
                              "from_command"],
    "accept_firmware_fault_cleared": ["physical_lane", "identity"],
}


def parameters(hook):
    """Positional parameter names of a bound method, without `self`."""
    return list(inspect.signature(hook).parameters)[1:]


class RuntimeHookContractTests(unittest.TestCase):
    def test_every_hook_klipper_mmu_binds_is_present(self):
        missing = [name for name in HOOKS
                   if not callable(getattr(Runtime, name, None))]
        self.assertEqual(missing, [])

    def test_hooks_take_the_parameters_klipper_mmu_passes(self):
        for name, expected in HOOKS.items():
            with self.subTest(hook=name):
                self.assertEqual(parameters(getattr(Runtime, name)), expected)

    def test_only_accept_firmware_fault_takes_from_command(self):
        for name, expected in HOOKS.items():
            hook = getattr(Runtime, name)
            with self.subTest(hook=name):
                keyword = inspect.signature(hook).parameters["from_command"] \
                    if "from_command" in expected else None
                self.assertEqual(
                    bool(keyword is not None and keyword.default is False),
                    name == "accept_firmware_fault")

    def test_accept_hooks_answer_with_a_bool_the_caller_can_retry_on(self):
        # klipper-mmu reads a False as "retain the event and do not ACK it", so
        # every accept hook must answer a bool and never raise here.
        runtime = Runtime.__new__(Runtime)
        runtime.reactor = types.SimpleNamespace(monotonic=lambda: 0.0)
        runtime.system = S.SystemState(lanes={"fps0": S.LaneState()})
        runtime._pending = {}
        runtime._accepted_firmware_faults = {}
        answers = [
            runtime.accept_verified_follower("fps0", (1, 0), True),
            runtime.accept_terminal("fps0", 0, None, 0),
            runtime.accept_reload_terminal("fps0", 0, None, 0, 0),
            runtime.accept_retired_terminal("fps0", (1, 0), 0, 0, None,
                                            ((1, 0), 0, True)),
        ]
        for answer in answers:
            self.assertIs(answer, False)


if __name__ == "__main__":
    unittest.main()
