import importlib
import os
import sys
import types
import unittest
from dataclasses import dataclass

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if "oamspkg" not in sys.modules:
    _pkg = types.ModuleType("oamspkg")
    _pkg.__path__ = [_SRC]
    sys.modules["oamspkg"] = _pkg

S = importlib.import_module("oamspkg.oams_state")
HelixOpenAMSBackend = importlib.import_module(
    "oamspkg.helix_backend"
).HelixOpenAMSBackend


@dataclass(frozen=True)
class Outcome:
    ok: bool
    value: object = None
    error: object = None
    evidence: object = None

    @classmethod
    def success(cls, value=None, evidence=None):
        return cls(True, value=value, evidence=evidence)

    @classmethod
    def failure(cls, error, evidence=None):
        return cls(False, error=error, evidence=evidence)


class Id:
    def __init__(self, generation, sequence):
        self.program_generation = generation
        self.sequence = sequence


class Request:
    def __init__(self, generation, sequence):
        self.id = Id(generation, sequence)


class Runtime:
    def __init__(self, lanes):
        self.state = S.SystemState(lanes=lanes)
        self.requests = []
        self.cancels = []

    def get_state(self):
        return self.state

    def request_async(self, fps, action, callback):
        self.requests.append((fps, action, callback))

    def dispatch(self, action):
        self.cancels.append(action)


class Manager:
    def __init__(self, lanes):
        self.ready = True
        self.runtime = Runtime(lanes)
        self.group_lane = {"T0": "F0", "T1": "F1"}
        self.topo = types.SimpleNamespace(groups={"T0": 1, "T1": 1})


class MachineBackend:
    def __init__(self):
        self.calls = []

    def acquire_resources(self, resources):
        return True

    def release_resources(self, resources):
        self.calls.append(("release", resources))

    def safe_pause(self, *, finish):
        self.calls.append(("pause",))
        return Outcome.success()


class HelixBackendTests(unittest.TestCase):
    def make_backend(self, lanes):
        deferred = object()
        manager = Manager(lanes)
        backend = HelixOpenAMSBackend(
            manager,
            MachineBackend(),
            outcome_type=Outcome,
            deferred=deferred,
        )
        return backend, manager, deferred

    def test_real_reducer_load_is_nonblocking_and_generation_scoped(self):
        lanes = {
            "F0": S.LaneState(
                op=S.OP_LOADED, group="T0", unit=(1, 0)
            ),
            "F1": S.LaneState(),
        }
        backend, manager, deferred = self.make_backend(lanes)
        request = Request(7, 1)
        preflight = backend.preflight(
            "T1", finish=lambda result: None, request=request
        )
        self.assertTrue(preflight.ok)
        self.assertEqual(preflight.value, "T0")
        finished = []
        result = backend.load_material(
            "T1", finish=finished.append, request=request
        )
        self.assertIs(result, deferred)
        fps, action, callback = manager.runtime.requests[-1]
        self.assertEqual(fps, "F1")
        self.assertEqual(action, S.Load("F1", "T1"))
        callback(S.OpResult(True, 0, "loaded"))
        self.assertTrue(finished[0].ok)
        self.assertEqual(finished[0].value, "T1")

    def test_cancel_recalls_both_load_and_unload(self):
        lanes = {
            "F0": S.LaneState(
                op=S.OP_LOADED, group="T0", unit=(1, 0)
            ),
            "F1": S.LaneState(),
        }
        backend, manager, deferred = self.make_backend(lanes)
        request = Request(8, 1)
        backend.preflight(
            "T1", finish=lambda result: None, request=request
        )
        backend.unload_material(
            finish=lambda result: None, request=request
        )
        backend.cancel_material(request=request)
        self.assertEqual(
            manager.runtime.cancels, [S.Cancel("F0")]
        )
        backend.load_material(
            "T1", finish=lambda result: None, request=request
        )
        backend.cancel_material(request=request)
        self.assertEqual(
            manager.runtime.cancels,
            [S.Cancel("F0"), S.Cancel("F1")],
        )

    def test_busy_or_unmanaged_state_fails_preflight(self):
        busy = {
            "F0": S.LaneState(op=S.OP_LOADING),
            "F1": S.LaneState(),
        }
        backend, manager, deferred = self.make_backend(busy)
        self.assertFalse(
            backend.preflight(
                "T1",
                finish=lambda result: None,
                request=Request(9, 1),
            ).ok
        )
        unmanaged = {
            "F0": S.LaneState(
                op=S.OP_LOADED, group=None, unit=(1, 0)
            ),
            "F1": S.LaneState(),
        }
        backend, manager, deferred = self.make_backend(unmanaged)
        self.assertFalse(
            backend.preflight(
                "T1",
                finish=lambda result: None,
                request=Request(10, 1),
            ).ok
        )


class LazyBindingTests(unittest.TestCase):
    def test_importing_openams_modules_never_imports_helix(self):
        import subprocess
        code = (
            "import importlib, os, sys, types\n"
            "src = %r\n"
            "pkg = types.ModuleType('oamspkg'); pkg.__path__ = [src]\n"
            "sys.modules['oamspkg'] = pkg\n"
            "sys.modules['mcu'] = types.ModuleType('mcu')\n"
            "for name in ('oams_state', 'oams_runtime', 'oams', "
            "'oams_manager'):\n"
            "    importlib.import_module('oamspkg.' + name)\n"
            "bad = [m for m in sys.modules if m == 'helix' "
            "or m.startswith('helix.') or m.endswith('helix_backend')]\n"
            "sys.exit(1 if bad else 0)\n" % os.path.abspath(_SRC)
        )
        result = subprocess.run([sys.executable, "-c", code])
        self.assertEqual(result.returncode, 0)

    def test_manager_builds_the_backend_lazily(self):
        manager_mod = importlib.import_module("oamspkg.oams_manager")
        fake_machine = types.ModuleType("helix.machine")
        fake_machine.OperationOutcome = Outcome
        fake_host = types.ModuleType("helix.openams.host")
        fake_host.DEFERRED = object()
        injected = {
            "helix": types.ModuleType("helix"),
            "helix.machine": fake_machine,
            "helix.openams": types.ModuleType("helix.openams"),
            "helix.openams.host": fake_host,
        }
        saved = {name: sys.modules.get(name) for name in injected}
        sys.modules.update(injected)
        try:
            backend = manager_mod.OAMSManager.make_helix_backend(
                Manager({"F0": S.LaneState()}), MachineBackend())
        finally:
            for name, module in saved.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module
        self.assertIsInstance(backend, HelixOpenAMSBackend)
        self.assertIs(backend.deferred, fake_host.DEFERRED)
        self.assertIs(backend.outcome_type, Outcome)


class RealRuntimeBindingTests(unittest.TestCase):
    """The backend on the real runtime and reducer, with a driver that
    selected protocol v4."""

    def setUp(self):
        import test_oams_runtime as rt
        self.rt = rt
        self.runtime, self.reactor, self.oam, _ = rt.make_runtime(
            oam=rt.FakeNativeOam())
        self.runtime.system = S.SystemState(lanes={rt.FPS: S.LaneState()})
        manager = types.SimpleNamespace(
            ready=True, runtime=self.runtime,
            group_lane={"T0": rt.FPS},
            topo=types.SimpleNamespace(groups={"T0": 1}))
        self.backend = HelixOpenAMSBackend(
            manager, MachineBackend(), outcome_type=Outcome,
            deferred=object())

    def _world(self):
        self.runtime.build_world = lambda now: S.World(lanes={
            self.rt.FPS: S.LaneWorld(
                ready={self.rt.UNIT: True},
                group_bays={"T0": (self.rt.UNIT,)})})

    def test_load_completes_without_blocking_and_delivers_asynchronously(self):
        self._world()
        request = Request(3, 1)
        self.assertTrue(self.backend.preflight(
            "T0", finish=lambda r: None, request=request).ok)
        finished = []
        self.backend.load_material(
            "T0", finish=finished.append, request=request)
        gen = self.oam.calls[-1][2]
        self.assertEqual(gen, 1)
        self.runtime.dispatch(S.OpCompleted(
            self.rt.FPS, S.OAMS_OP_CODE_SUCCESS, gen=gen))
        self.assertEqual(finished, [])           # not inside the reducer
        self.reactor.run_async_callbacks()
        self.assertTrue(finished[0].ok)
        self.assertEqual(finished[0].value, "T0")

    def test_cancel_reaches_the_driver_with_action_and_generation(self):
        self._world()
        request = Request(4, 1)
        self.backend.preflight("T0", finish=lambda r: None, request=request)
        self.backend.load_material(
            "T0", finish=lambda r: None, request=request)
        gen = self.oam.calls[-1][2]
        self.backend.cancel_material(request=request)
        self.assertIn(("cancel", self.oam.status_loading, gen),
                      self.oam.calls)


if __name__ == "__main__":
    unittest.main()
