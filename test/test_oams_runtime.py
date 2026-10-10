# Unit tests for the OpenAMS runtime (oams_runtime.py) using a fake reactor.
#
# No Klipper, no hardware, no pytest required:
#   cd openams && python3 -m unittest discover -s test -v

import importlib
import os
import sys
import types
import unittest
from unittest import mock

# oams_runtime uses package-relative imports (it is installed into
# klippy/extras as part of a package), so expose src/ as a synthetic package.
_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if "oamspkg" not in sys.modules:
    _pkg = types.ModuleType("oamspkg")
    _pkg.__path__ = [_SRC]
    sys.modules["oamspkg"] = _pkg

S = importlib.import_module("oamspkg.oams_state")
oams_runtime = importlib.import_module("oamspkg.oams_runtime")
Runtime = oams_runtime.Runtime


FPS = "fps1"
UNIT = (1, 0)


class FakeCompletion:
    def __init__(self, events):
        self._events = events
        self._done = False
        self._result = None

    def test(self):
        return self._done

    def complete(self, result):
        self._done = True
        self._result = result
        self._events.append(("settle", result))

    def wait(self, *args):
        return self._result


class FakeReactor:
    NEVER = 9e99

    def __init__(self, events):
        self._events = events
        self.now = 0.0
        self.timers = []          # mutable [callback, waketime] pairs
        self.async_cbs = []

    def monotonic(self):
        return self.now

    def completion(self):
        return FakeCompletion(self._events)

    def register_timer(self, callback, waketime):
        timer = [callback, waketime]
        self.timers.append(timer)
        return timer

    def unregister_timer(self, timer):
        if timer in self.timers:
            self.timers.remove(timer)

    def register_async_callback(self, callback):
        self.async_cbs.append(callback)

    def run_async_callbacks(self):
        callbacks, self.async_cbs = self.async_cbs, []
        for callback in callbacks:
            callback(self.now)

    def fire_due_timers(self):
        for timer in list(self.timers):
            if timer[1] <= self.now:
                timer[1] = timer[0](self.now)


class FakeGcode:
    def __init__(self, events):
        self._events = events

    def run_script(self, script):
        self._events.append(("script", script))

    def run_script_from_command(self, script):
        self._events.append(("command_script", script))


class FakePrinter:
    def __init__(self, reactor, gcode):
        self._reactor = reactor
        self._gcode = gcode

    def get_reactor(self):
        return self._reactor

    def lookup_object(self, name):
        if name == "gcode":
            return self._gcode
        raise KeyError(name)


class FakeOam:
    def __init__(self):
        self.encoder_clicks = 0
        self.calls = []

    def start_load_spool(self, bay, gen=None):
        self.calls.append(("load", bay, gen))

    def start_unload_spool(self, gen=None):
        self.calls.append(("unload", gen))

    def start_calibrate(self, kind, bay, gen=None):
        self.calls.append(("calibrate", kind, bay, gen))

    def load_spool_cancel(self):
        self.calls.append(("cancel_load",))

    def set_oams_follower(self, enable, direction):
        self.calls.append(("follower", enable, direction))

    def set_led_error(self, idx, value):
        self.calls.append(("led", idx, value))


def make_runtime(build_world=None, oam=None):
    events = []
    reactor = FakeReactor(events)
    printer = FakePrinter(reactor, FakeGcode(events))
    if build_world is None:
        build_world = lambda now: S.World(lanes={FPS: S.LaneWorld()})
    if oam is None:
        oam = FakeOam()
    runtime = Runtime(printer, [FPS],
                      build_world, lambda idx: oam if idx == UNIT[0] else None)
    return runtime, reactor, oam, events


class DispatchSafetyTests(unittest.TestCase):
    def test_verified_follower_state_settles_admission_only_after_observation(self):
        runtime, reactor, oam, events = make_runtime()
        runtime.system = S.SystemState(lanes={
            FPS: S.LaneState(op=S.OP_LOADED, unit=UNIT)})
        oam.set_oams_follower = lambda enable, direction: types.SimpleNamespace(
            follower_confirmed=False)
        runtime.dispatch(S.Follow(FPS, 1, S.FOLLOWER_FORWARD))
        self.assertIsNone(runtime.get_state().lanes[FPS].following)
        self.assertTrue(runtime.accept_verified_follower(FPS, UNIT, True))
        lane = runtime.get_state().lanes[FPS]
        self.assertIs(lane.following, True)
        self.assertEqual(lane.uncertain_follower_units, ())

    def test_verified_orphan_stop_clears_only_matching_uncertain_unit(self):
        runtime, reactor, oam, events = make_runtime()
        other = (2, 1)
        runtime.system = S.SystemState(lanes={FPS: S.LaneState(
            unit=None, following=None, uncertain_follower_units=(UNIT, other))})
        self.assertTrue(runtime.accept_verified_follower(FPS, UNIT, False))
        lane = runtime.get_state().lanes[FPS]
        self.assertIsNone(lane.following)
        self.assertEqual(lane.uncertain_follower_units, (other,))
        self.assertTrue(runtime.accept_verified_follower(FPS, other, False))
        self.assertIs(runtime.get_state().lanes[FPS].following, False)

    def test_verified_follower_rejects_swallowed_dispatch_failure(self):
        runtime, reactor, oam, events = make_runtime()
        runtime.system = S.SystemState(lanes={FPS: S.LaneState(
            op=S.OP_LOADED, unit=UNIT, following=None,
            uncertain_follower_units=(UNIT,))})
        before = runtime.get_state()
        original_reduce = S.reduce
        calls = 0

        def fail_on_dispatch(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("reducer failed during dispatch")
            return original_reduce(*args)

        with mock.patch.object(S, "reduce", side_effect=fail_on_dispatch):
            self.assertFalse(runtime.accept_verified_follower(FPS, UNIT, True))
        self.assertEqual(calls, 2)
        self.assertEqual(runtime.get_state(), before)
        self.assertEqual(runtime.get_state().lanes[FPS].uncertain_follower_units,
                         (UNIT,))
        self.assertTrue(runtime.accept_verified_follower(FPS, UNIT, True))
        self.assertIs(runtime.get_state().lanes[FPS].following, True)

    def test_verified_follower_accepts_idempotent_confirmed_state(self):
        runtime, reactor, oam, events = make_runtime()
        runtime.system = S.SystemState(lanes={FPS: S.LaneState(
            op=S.OP_LOADED, unit=UNIT, following=True)})
        self.assertTrue(runtime.accept_verified_follower(FPS, UNIT, True))
        self.assertIs(runtime.get_state().lanes[FPS].following, True)

    def test_verified_follower_rejects_mismatched_and_unassigned_enable(self):
        runtime, reactor, oam, events = make_runtime()
        runtime.system = S.SystemState(lanes={FPS: S.LaneState(
            op=S.OP_LOADED, unit=UNIT, following=None,
            uncertain_follower_units=(UNIT,))})
        original = runtime.get_state()
        self.assertFalse(runtime.accept_verified_follower(FPS, (2, 0), False))
        self.assertFalse(runtime.accept_verified_follower("other", UNIT, False))
        self.assertFalse(runtime.accept_verified_follower(FPS, UNIT, 1))
        self.assertFalse(runtime.accept_verified_follower(FPS, None, False))
        self.assertEqual(runtime.get_state(), original)
        runtime.system = S.SystemState(lanes={FPS: S.LaneState(
            unit=None, following=None, uncertain_follower_units=(UNIT,))})
        self.assertFalse(runtime.accept_verified_follower(FPS, UNIT, True))
        self.assertEqual(runtime.get_state().lanes[FPS].uncertain_follower_units,
                         (UNIT,))

    def test_admitted_follower_enable_remains_unknown_until_verified(self):
        runtime, reactor, oam, events = make_runtime()
        runtime.system = S.SystemState(lanes={
            FPS: S.LaneState(op=S.OP_LOADED, unit=UNIT)})
        admission = types.SimpleNamespace(follower_confirmed=False)
        oam.set_oams_follower = lambda enable, direction: admission
        runtime.dispatch(S.Follow(FPS, 1, S.FOLLOWER_FORWARD))
        lane = runtime.get_state().lanes[FPS]
        self.assertIsNone(lane.following)
        self.assertEqual(lane.uncertain_follower_units, (UNIT,))

    def test_admitted_follower_stop_does_not_clear_uncertainty(self):
        runtime, reactor, oam, events = make_runtime()
        runtime.system = S.SystemState(lanes={
            FPS: S.LaneState(op=S.OP_LOADED, unit=UNIT, following=True,
                             uncertain_follower_units=(UNIT,))})
        admission = types.SimpleNamespace(follower_confirmed=False)
        oam.set_oams_follower = lambda enable, direction: admission
        runtime.dispatch(S.Follow(FPS, 0, S.FOLLOWER_REVERSE))
        lane = runtime.get_state().lanes[FPS]
        self.assertIsNone(lane.following)
        self.assertEqual(lane.uncertain_follower_units, (UNIT,))
        # Only a later verified observation may clear the pending unit.
        runtime.dispatch(S.FollowerApplied(FPS, UNIT, False))
        self.assertEqual(runtime.get_state().lanes[FPS].uncertain_follower_units,
                         ())
        self.assertIs(runtime.get_state().lanes[FPS].following, False)

    def test_failed_follower_enable_is_unknown(self):
        runtime, reactor, oam, events = make_runtime()
        runtime.system = S.SystemState(lanes={
            FPS: S.LaneState(op=S.OP_LOADED, unit=UNIT)})

        def fail(enable, direction):
            raise RuntimeError("device rejected follower command")

        oam.set_oams_follower = fail
        runtime.dispatch(S.Follow(FPS, 1, S.FOLLOWER_REVERSE))
        lane = runtime.get_state().lanes[FPS]
        self.assertIsNone(lane.following)
        self.assertEqual(lane.direction, S.FOLLOWER_REVERSE)

    def test_failed_follower_disable_is_unknown_and_retry_recovers(self):
        runtime, reactor, oam, events = make_runtime()
        runtime.system = S.SystemState(lanes={
            FPS: S.LaneState(op=S.OP_LOADED, unit=UNIT, following=True,
                             direction=S.FOLLOWER_REVERSE)})
        original = oam.set_oams_follower

        def fail(enable, direction):
            raise RuntimeError("device rejected stop")

        oam.set_oams_follower = fail
        runtime.dispatch(S.Follow(FPS, 0, S.FOLLOWER_REVERSE))
        self.assertIsNone(runtime.get_state().lanes[FPS].following)
        oam.set_oams_follower = original
        runtime.dispatch(S.Follow(FPS, 0, S.FOLLOWER_REVERSE))
        self.assertIs(runtime.get_state().lanes[FPS].following, False)

    def test_missing_follower_unit_is_unknown(self):
        runtime, reactor, oam, events = make_runtime()
        missing = (99, 0)
        runtime.system = S.SystemState(lanes={
            FPS: S.LaneState(op=S.OP_LOADED, unit=missing)})
        runtime.dispatch(S.Follow(FPS, 1, S.FOLLOWER_FORWARD))
        self.assertIsNone(runtime.get_state().lanes[FPS].following)

    def test_failed_cleanup_remembers_unit_after_load_clears_it(self):
        runtime, reactor, oam, events = make_runtime()
        runtime.request(FPS, S.LoadBay(FPS, UNIT))
        gen = oam.calls[-1][2]
        runtime.dispatch(S.Follow(FPS, 1, S.FOLLOWER_FORWARD))

        def fail(enable, direction):
            raise RuntimeError("stop was not confirmed")

        oam.set_oams_follower = fail
        runtime.dispatch(S.OpCompleted(
            FPS, S.OAMS_OP_CODE_ERROR_UNSPECIFIED, gen=gen))
        lane = runtime.get_state().lanes[FPS]
        self.assertIsNone(lane.unit)
        self.assertIsNone(lane.following)
        self.assertEqual(lane.uncertain_follower_units, (UNIT,))

        runtime.dispatch(S.ClearErrors())
        lane = runtime.get_state().lanes[FPS]
        self.assertIsNone(lane.following)
        self.assertEqual(lane.uncertain_follower_units, (UNIT,))
        next_load = runtime.request(FPS, S.LoadBay(FPS, UNIT))
        self.assertTrue(next_load.test())
        self.assertFalse(next_load.wait().ok)
        self.assertIn("follower state unknown", next_load.wait().message)

    def test_successful_cleanup_clears_prior_uncertainty(self):
        runtime, reactor, oam, events = make_runtime()
        runtime.system = S.SystemState(lanes={
            FPS: S.LaneState(uncertain_follower_units=(UNIT,), following=None)})
        runtime._apply(S.SetFollower(UNIT, 0, S.FOLLOWER_FORWARD, FPS))
        lane = runtime.get_state().lanes[FPS]
        self.assertIs(lane.following, False)
        self.assertEqual(lane.uncertain_follower_units, ())

    def test_failed_runout_stop_is_unknown(self):
        world = lambda now: S.World(lanes={
            FPS: S.LaneWorld(extruder_pos=S.PAUSE_DISTANCE + 1)})
        runtime, reactor, oam, events = make_runtime(build_world=world)
        runtime.system = S.SystemState(lanes={
            FPS: S.LaneState(op=S.OP_LOADED, unit=UNIT, following=True,
                             runout=S.RUNOUT_PAUSING, pause_origin=0.0)})

        def fail(enable, direction):
            raise RuntimeError("stop was not confirmed")

        oam.set_oams_follower = fail
        runtime.tick()
        lane = runtime.get_state().lanes[FPS]
        self.assertEqual(lane.runout, S.RUNOUT_COASTING)
        self.assertIsNone(lane.following)
        self.assertEqual(lane.uncertain_follower_units, (UNIT,))
        self.assertIn(("script", "PAUSE"), events)

    def test_failed_unload_cleanup_is_unknown(self):
        runtime, reactor, oam, events = make_runtime()
        runtime.system = S.SystemState(lanes={
            FPS: S.LaneState(op=S.OP_UNLOADING, unit=UNIT, following=True,
                             op_gen=7)})

        def fail(enable, direction):
            raise RuntimeError("stop was not confirmed")

        oam.set_oams_follower = fail
        runtime.dispatch(S.OpCompleted(
            FPS, S.OAMS_OP_CODE_ERROR_UNSPECIFIED, gen=7))
        lane = runtime.get_state().lanes[FPS]
        self.assertEqual(lane.op, S.OP_LOADED)
        self.assertIsNone(lane.following)
        self.assertEqual(lane.uncertain_follower_units, (UNIT,))

    def test_load_failure_retries_stop_after_uncertain_enable(self):
        runtime, reactor, oam, events = make_runtime()
        runtime.request(FPS, S.LoadBay(FPS, UNIT))
        gen = oam.calls[-1][2]

        def fail(enable, direction):
            raise RuntimeError("follower command not confirmed")

        oam.set_oams_follower = fail
        runtime.dispatch(S.Follow(FPS, 1, S.FOLLOWER_FORWARD))
        self.assertIsNone(runtime.get_state().lanes[FPS].following)
        runtime.dispatch(S.OpCompleted(
            FPS, S.OAMS_OP_CODE_ERROR_UNSPECIFIED, gen=gen))
        lane = runtime.get_state().lanes[FPS]
        self.assertIsNone(lane.following)
        self.assertEqual(lane.uncertain_follower_units, (UNIT,))

    def test_reducer_exception_settles_pending_waiter(self):
        # A gcode handler blocked in request(...).wait() holds the gcode mutex;
        # a dispatch that dies must fail the op, never leave it hanging.
        def broken_world(now):
            raise RuntimeError("boom")

        runtime, reactor, oam, events = make_runtime(build_world=broken_world)
        completion = runtime.request(FPS, S.LoadBay(FPS, UNIT))
        self.assertTrue(completion.test())
        self.assertFalse(completion.wait().ok)

    def test_start_effect_failure_fails_op(self):
        # resolve_oam returning None (bad oams_idx / not configured) must fail
        # the op promptly instead of stranding the lane until the deadline.
        runtime, reactor, oam, events = make_runtime()
        completion = runtime.request(FPS, S.LoadBay(FPS, (99, 0)))
        self.assertFalse(completion.test())     # failure is deferred one cycle
        reactor.run_async_callbacks()
        self.assertTrue(completion.test())
        self.assertFalse(completion.wait().ok)
        self.assertEqual(runtime.get_state().lanes[FPS].op, S.OP_UNLOADED)

    def test_world_only_built_for_world_actions(self):
        # A transient world-build failure must not be able to kill the
        # reduction of a completion, so completions must not build the world.
        calls = []

        def counting_world(now):
            calls.append(now)
            return S.World(lanes={FPS: S.LaneWorld()})

        runtime, reactor, oam, events = make_runtime(build_world=counting_world)
        runtime.request(FPS, S.LoadBay(FPS, UNIT))
        self.assertEqual(len(calls), 1)                   # LoadBay reads it
        gen = oam.calls[-1][2]
        runtime.dispatch(S.OpCompleted(FPS, S.OAMS_OP_CODE_SUCCESS, gen=gen))
        self.assertEqual(len(calls), 1)                   # OpCompleted does not
        self.assertEqual(runtime.get_state().lanes[FPS].op, S.OP_LOADED)

    def test_superseded_request_settles_previous_waiter(self):
        runtime, reactor, oam, events = make_runtime()
        first = runtime.request(FPS, S.LoadBay(FPS, UNIT))
        self.assertFalse(first.test())          # op in flight
        second = runtime.request(FPS, S.LoadBay(FPS, UNIT))
        self.assertTrue(first.test())           # old waiter not leaked
        self.assertFalse(first.wait().ok)


class CompletionTests(unittest.TestCase):
    def test_completion_with_matching_gen_settles_ok(self):
        runtime, reactor, oam, events = make_runtime()
        completion = runtime.request(FPS, S.LoadBay(FPS, UNIT))
        gen = oam.calls[-1][2]
        runtime.dispatch(S.OpCompleted(FPS, S.OAMS_OP_CODE_SUCCESS, gen=gen))
        self.assertTrue(completion.wait().ok)
        self.assertEqual(runtime.get_state().lanes[FPS].op, S.OP_LOADED)
        self.assertEqual(runtime._deadlines, {})       # timer cleaned up
        self.assertEqual(reactor.timers, [])

    def test_completion_with_stale_gen_is_ignored(self):
        runtime, reactor, oam, events = make_runtime()
        completion = runtime.request(FPS, S.LoadBay(FPS, UNIT))
        gen = oam.calls[-1][2]
        runtime.dispatch(S.OpCompleted(FPS, S.OAMS_OP_CODE_SUCCESS, gen=gen - 1))
        self.assertFalse(completion.test())
        self.assertEqual(runtime.get_state().lanes[FPS].op, S.OP_LOADING)

    def test_deadline_times_out_and_cancels_firmware_load(self):
        runtime, reactor, oam, events = make_runtime()
        completion = runtime.request(FPS, S.LoadBay(FPS, UNIT))
        reactor.now = S.OAMS_ACTION_TIMEOUT + 1.0
        reactor.fire_due_timers()
        self.assertTrue(completion.test())
        self.assertFalse(completion.wait().ok)
        self.assertEqual(runtime.get_state().lanes[FPS].op, S.OP_UNLOADED)
        self.assertIn(("cancel_load",), oam.calls)


class TerminalAcceptanceTests(unittest.TestCase):
    def test_matching_success_settles_waiter(self):
        runtime, reactor, oam, events = make_runtime()
        completion = runtime.request(FPS, S.LoadBay(FPS, UNIT))
        gen = oam.calls[-1][2]
        self.assertTrue(runtime.accept_terminal(
            FPS, S.OAMS_OP_CODE_SUCCESS, None, gen))
        self.assertTrue(completion.wait().ok)
        self.assertEqual(runtime.get_state().lanes[FPS].op, S.OP_LOADED)

    def test_matching_failure_settles_waiter(self):
        runtime, reactor, oam, events = make_runtime()
        completion = runtime.request(FPS, S.LoadBay(FPS, UNIT))
        gen = oam.calls[-1][2]
        code = S.OAMS_OP_CODE_ERROR_UNSPECIFIED
        self.assertTrue(runtime.accept_terminal(FPS, code, None, gen))
        self.assertFalse(completion.wait().ok)
        self.assertEqual(completion.wait().code, code)
        self.assertEqual(runtime.get_state().lanes[FPS].op, S.OP_UNLOADED)

    def test_stale_generation_and_prior_timeout_are_not_accepted(self):
        runtime, reactor, oam, events = make_runtime()
        completion = runtime.request(FPS, S.LoadBay(FPS, UNIT))
        gen = oam.calls[-1][2]
        self.assertFalse(runtime.accept_terminal(
            FPS, S.OAMS_OP_CODE_SUCCESS, None, gen - 1))
        self.assertFalse(completion.test())
        reactor.now = S.OAMS_ACTION_TIMEOUT + 1.0
        reactor.fire_due_timers()
        self.assertFalse(runtime.accept_terminal(
            FPS, S.OAMS_OP_CODE_SUCCESS, None, gen))
        self.assertFalse(completion.wait().ok)

    def test_swallowed_dispatch_cannot_acknowledge(self):
        runtime, reactor, oam, events = make_runtime()
        completion = runtime.request(FPS, S.LoadBay(FPS, UNIT))
        gen = oam.calls[-1][2]
        runtime.dispatch = lambda action: runtime.system
        self.assertFalse(runtime.accept_terminal(
            FPS, S.OAMS_OP_CODE_SUCCESS, None, gen))
        self.assertFalse(completion.test())

    def test_auto_reload_without_waiter_is_not_accepted(self):
        runtime, reactor, oam, events = make_runtime()
        lane = S.LaneState(op=S.OP_LOADED, group="G", unit=UNIT,
                           runout=S.RUNOUT_LOADING, reload_target=(1, 3),
                           op_gen=4)
        runtime.system = S.SystemState(lanes={FPS: lane})
        self.assertFalse(runtime.accept_terminal(
            FPS, S.OAMS_OP_CODE_SUCCESS, None, 4))
        self.assertEqual(runtime.get_state().lanes[FPS], lane)

    def test_exact_auto_reload_success_applies_once(self):
        runtime, reactor, oam, events = make_runtime()
        target = (1, 3)
        lane = S.LaneState(op=S.OP_LOADED, group="G", unit=UNIT,
                           runout=S.RUNOUT_LOADING, reload_target=target,
                           op_gen=4)
        runtime.system = S.SystemState(lanes={FPS: lane})
        self.assertFalse(runtime.accept_reload_terminal(FPS, 0, None, 3, 3))
        self.assertFalse(runtime.accept_reload_terminal(FPS, 0, None, 4, 0))
        self.assertEqual(runtime.get_state().lanes[FPS], lane)
        self.assertTrue(runtime.accept_reload_terminal(FPS, 0, None, 4, 3))
        applied = runtime.get_state().lanes[FPS]
        self.assertEqual(applied.unit, target)
        self.assertEqual(applied.runout, S.RUNOUT_IDLE)
        self.assertFalse(runtime.accept_reload_terminal(FPS, 0, None, 4, 3))
        self.assertEqual(runtime.get_state().lanes[FPS], applied)

    def test_auto_reload_failure_and_swallowed_dispatch(self):
        runtime, reactor, oam, events = make_runtime()
        lane = S.LaneState(op=S.OP_LOADED, group="G", unit=UNIT,
                           runout=S.RUNOUT_LOADING, reload_target=(1, 3),
                           op_gen=4)
        runtime.system = S.SystemState(lanes={FPS: lane})
        original = runtime.dispatch
        runtime.dispatch = lambda action: runtime.system
        self.assertFalse(runtime.accept_reload_terminal(FPS, 7, None, 4, 3))
        runtime.dispatch = original
        self.assertEqual(runtime.get_state().lanes[FPS], lane)
        self.assertTrue(runtime.accept_reload_terminal(FPS, 7, None, 4, 3))
        self.assertEqual(runtime.get_state().lanes[FPS].op, S.OP_UNLOADED)
        self.assertFalse(runtime.accept_reload_terminal(FPS, 7, None, 4, 3))


class StallTests(unittest.TestCase):
    def _start_stalled_load(self):
        runtime, reactor, oam, events = make_runtime()
        completion = runtime.request(FPS, S.LoadBay(FPS, UNIT))
        reactor.now = oams_runtime.STALL_AFTER + 3.0   # past the grace window
        return runtime, reactor, oam, events, completion

    def test_stall_fails_op_and_pauses(self):
        runtime, reactor, oam, events, completion = self._start_stalled_load()
        runtime.tick()                                 # first encoder sample
        self.assertFalse(completion.test())
        runtime.tick()                                 # second identical sample
        self.assertTrue(completion.test())
        self.assertFalse(completion.wait().ok)
        scripts = [e[1] for e in events if e[0] == "script"]
        self.assertIn("PAUSE", scripts)

    def test_stall_settles_waiter_before_pausing(self):
        # The waiter may hold the gcode mutex PAUSE needs; the settle must come
        # first or the monitor wedges until the op deadline.
        runtime, reactor, oam, events, completion = self._start_stalled_load()
        runtime.tick()
        runtime.tick()
        kinds = [e[0] for e in events]
        self.assertIn("settle", kinds)
        self.assertLess(kinds.index("settle"),
                        kinds.index("script"))

    def test_moving_encoder_is_not_a_stall(self):
        runtime, reactor, oam, events, completion = self._start_stalled_load()
        runtime.tick()
        oam.encoder_clicks += 100
        runtime.tick()
        self.assertFalse(completion.test())

    def test_runout_reload_stall_cancels_and_pauses(self):
        # Legacy firmware: a jammed runout auto-reload (op stays LOADED,
        # runout==RUNOUT_LOADING) must be caught by the host stall detector,
        # not left to feed the print a dwindling tail until the deadline.
        events = []
        reactor = FakeReactor(events)
        printer = FakePrinter(reactor, FakeGcode(events))
        oam = FakeOam()
        lw = S.LaneWorld(extruder_pos=1000.0, printing=True,
                         loaded={UNIT: False, (1, 3): True},
                         ready={(1, 3): True},
                         group_bays={"G": (UNIT, (1, 3))},
                         path_len={1: 600.0})
        runtime = Runtime(printer, [FPS],
                          lambda now: S.World(lanes={FPS: lw}),
                          lambda idx: oam if idx == UNIT[0] else None)
        reloading = S.LaneState(op=S.OP_LOADED, group="G", unit=UNIT,
                                runout=S.RUNOUT_LOADING, reload_target=(1, 3),
                                op_gen=4, since=0.0)
        runtime.system = S.SystemState(lanes={FPS: reloading})
        reactor.now = oams_runtime.STALL_AFTER + 3.0
        runtime.tick()                                 # first encoder sample
        runtime.tick()                                 # identical -> stall
        lane = runtime.get_state().lanes[FPS]
        self.assertEqual(lane.op, S.OP_UNLOADED)       # reload failed over
        self.assertIn(("cancel_load",), oam.calls)     # firmware op cancelled
        scripts = [e[1] for e in events if e[0] == "script"]
        self.assertEqual(scripts.count("PAUSE"), 1)    # paused exactly once


class LivenessTests(unittest.TestCase):
    def test_coarse_deadline_armed_when_firmware_owns_liveness(self):
        runtime, reactor, oam, events = make_runtime()
        runtime.set_firmware_liveness(True)
        runtime.request(FPS, S.LoadBay(FPS, UNIT))
        self.assertEqual(len(reactor.timers), 1)
        waketime = reactor.timers[0][1]
        self.assertAlmostEqual(waketime, reactor.now + S.OAMS_DISCONNECT_BACKSTOP)

    def test_authoritative_deadline_armed_for_legacy(self):
        runtime, reactor, oam, events = make_runtime()
        runtime.request(FPS, S.LoadBay(FPS, UNIT))
        waketime = reactor.timers[0][1]
        self.assertAlmostEqual(waketime, reactor.now + S.OAMS_ACTION_TIMEOUT)

    def test_stall_check_skipped_when_firmware_owns_liveness(self):
        runtime, reactor, oam, events = make_runtime()
        runtime.set_firmware_liveness(True)
        completion = runtime.request(FPS, S.LoadBay(FPS, UNIT))
        reactor.now = oams_runtime.STALL_AFTER + 3.0     # past the grace window
        runtime.tick()
        runtime.tick()                                   # would stall if checked
        self.assertFalse(completion.test())
        self.assertEqual(runtime.get_state().lanes[FPS].op, S.OP_LOADING)


class FirmwareFaultClearedRuntimeTests(unittest.TestCase):
    IDENTITY = (1, 2, 3, 0, 4, 5, 6, 0, 7, 0, 8, 0)

    def test_exact_identity_removes_latch_and_resyncs_loaded(self):
        runtime, reactor, oam, events = make_runtime(
            build_world=lambda now: S.World(lanes={FPS: S.LaneWorld(
                group_bays={"T0": (UNIT,)}, path_len={UNIT[0]: 600.0},
                loaded={UNIT: True})}))
        runtime.system = S.SystemState(
            lanes={FPS: S.LaneState(op=S.OP_UNLOADED)},
            firmware_faults={0: (FPS, 5, self.IDENTITY)})
        runtime._accepted_firmware_faults[0] = (FPS, 5, self.IDENTITY)
        self.assertTrue(runtime.accept_firmware_fault_cleared(0, self.IDENTITY))
        state = runtime.get_state()
        self.assertEqual(state.firmware_faults, {})
        self.assertEqual(state.lanes[FPS].op, S.OP_LOADED)
        self.assertEqual(state.lanes[FPS].unit, UNIT)
        self.assertNotIn(0, runtime._accepted_firmware_faults)

    def test_wrong_identity_returns_false_and_leaves_latch(self):
        runtime, reactor, oam, events = make_runtime()
        runtime.system = S.SystemState(
            lanes={FPS: S.LaneState()},
            firmware_faults={0: (FPS, 5, self.IDENTITY)})
        before = runtime.get_state()
        self.assertFalse(runtime.accept_firmware_fault_cleared(0, ("different",)))
        self.assertEqual(runtime.get_state(), before)

    def test_fault_from_command_pauses_without_retaking_gcode_mutex(self):
        runtime, reactor, oam, events = make_runtime()
        runtime.system = S.SystemState(lanes={FPS: S.LaneState(
            op=S.OP_LOADED, unit=UNIT)})
        self.assertTrue(runtime.accept_firmware_fault(
            FPS, UNIT[1], 5, self.IDENTITY, from_command=True))
        self.assertIn(("command_script", "PAUSE"), events)
        self.assertNotIn(("script", "PAUSE"), events)

    def test_no_latch_returns_false(self):
        runtime, reactor, oam, events = make_runtime()
        before = runtime.get_state()
        self.assertFalse(runtime.accept_firmware_fault_cleared(0, self.IDENTITY))
        self.assertEqual(runtime.get_state(), before)

    def test_global_255_resyncs_every_lane(self):
        fps2 = "fps2"
        unit2 = (2, 0)
        runtime, reactor, oam, events = make_runtime(
            build_world=lambda now: S.World(lanes={
                FPS: S.LaneWorld(group_bays={"T0": (UNIT,)},
                                 path_len={UNIT[0]: 600.0}, loaded={UNIT: True}),
                fps2: S.LaneWorld(group_bays={"T1": (unit2,)},
                                  path_len={unit2[0]: 600.0},
                                  loaded={unit2: False}),
            }))
        runtime.system = S.SystemState(
            lanes={FPS: S.LaneState(op=S.OP_LOADED, unit=UNIT),
                   fps2: S.LaneState(op=S.OP_LOADED, unit=unit2)},
            firmware_faults={255: (None, 9, self.IDENTITY)})
        runtime._firmware_fault_waiters[255] = object()
        self.assertTrue(runtime.accept_firmware_fault_cleared(255, self.IDENTITY))
        state = runtime.get_state()
        self.assertEqual(state.firmware_faults, {})
        self.assertEqual(state.lanes[FPS].op, S.OP_LOADED)
        self.assertEqual(state.lanes[FPS].unit, UNIT)
        self.assertEqual(state.lanes[fps2].op, S.OP_UNLOADED)
        self.assertIsNone(state.lanes[fps2].unit)
        self.assertNotIn(255, runtime._firmware_fault_waiters)

    def test_reducer_failure_returns_false(self):
        runtime, reactor, oam, events = make_runtime()
        runtime.system = S.SystemState(
            lanes={FPS: S.LaneState()},
            firmware_faults={0: (FPS, 5, self.IDENTITY)})
        before = runtime.get_state()
        with mock.patch.object(S, "reduce", side_effect=RuntimeError("boom")):
            self.assertFalse(runtime.accept_firmware_fault_cleared(0, self.IDENTITY))
        self.assertEqual(runtime.get_state(), before)


class FaultLatchRequestTests(unittest.TestCase):
    """Runtime.request() must return an already-settled completion when a
    firmware fault is latched, never block indefinitely."""

    IDENTITY = (3, 2, 1)

    def _runtime_with_fault(self, lane_op=S.OP_UNLOADED, global_fault=False):
        runtime, reactor, oam, events = make_runtime()
        lane = S.LaneState(op=lane_op, unit=UNIT if lane_op != S.OP_UNLOADED else None,
                           group="G" if lane_op != S.OP_UNLOADED else None)
        if global_fault:
            faults = {255: (None, 9, self.IDENTITY)}
        else:
            faults = {UNIT[1]: (FPS, 5, self.IDENTITY)}
        runtime.system = S.SystemState(lanes={FPS: lane}, firmware_faults=faults)
        return runtime, reactor, oam, events

    def test_load_under_lane_fault_settled_immediately(self):
        runtime, reactor, oam, events = self._runtime_with_fault()
        completion = runtime.request(FPS, S.Load(FPS, "G"))
        self.assertTrue(completion.test(),
                        "completion must be settled synchronously")
        result = completion.wait()
        self.assertFalse(result.ok)
        self.assertIn("firmware fault", result.message)
        self.assertIn("OAMS_CLEAR_FAULT", result.message)

    def test_load_bay_under_lane_fault_settled_immediately(self):
        runtime, reactor, oam, events = self._runtime_with_fault()
        completion = runtime.request(FPS, S.LoadBay(FPS, UNIT))
        self.assertTrue(completion.test())
        self.assertFalse(completion.wait().ok)
        self.assertIn("firmware fault", completion.wait().message)

    def test_unload_under_lane_fault_settled_immediately(self):
        runtime, reactor, oam, events = self._runtime_with_fault(
            lane_op=S.OP_LOADED)
        completion = runtime.request(FPS, S.Unload(FPS))
        self.assertTrue(completion.test())
        self.assertFalse(completion.wait().ok)

    def test_calibrate_under_lane_fault_settled_immediately(self):
        runtime, reactor, oam, events = self._runtime_with_fault()
        completion = runtime.request(FPS, S.Calibrate(FPS, UNIT[0], UNIT[1], "ptfe"))
        self.assertTrue(completion.test())
        self.assertFalse(completion.wait().ok)

    def test_load_under_global_fault_settled_immediately(self):
        runtime, reactor, oam, events = self._runtime_with_fault(global_fault=True)
        completion = runtime.request(FPS, S.Load(FPS, "G"))
        self.assertTrue(completion.test())
        result = completion.wait()
        self.assertFalse(result.ok)
        self.assertIn("firmware fault", result.message)

    def test_no_firmware_op_started_under_fault(self):
        runtime, reactor, oam, events = self._runtime_with_fault()
        runtime.request(FPS, S.LoadBay(FPS, UNIT))
        load_calls = [c for c in oam.calls if c[0] == "load"]
        self.assertEqual(load_calls, [],
                         "no firmware op must be started under a latched fault")


class FakeNativeOam(FakeOam):
    """A driver that selected the complete protocol-v4 family."""
    supports_native_operations = True
    status_loading = 0
    status_unloading = 1

    def cancel_operation(self, action, gen=None):
        self.calls.append(("cancel", action, gen))


def _lane(op, gen, **fields):
    return S.SystemState(lanes={FPS: S.LaneState(
        op=op, op_gen=gen, **fields)})


class AsyncRequestTests(unittest.TestCase):
    def test_async_request_delivers_after_reducer_settles(self):
        runtime, reactor, oam, events = make_runtime()
        results = []
        runtime.request_async(FPS, S.LoadBay(FPS, UNIT), results.append)
        gen = oam.calls[-1][2]
        runtime.dispatch(S.OpCompleted(FPS, S.OAMS_OP_CODE_SUCCESS, gen=gen))
        self.assertEqual(results, [])            # never inside dispatch
        reactor.run_async_callbacks()
        self.assertTrue(results[0].ok)
        self.assertEqual(runtime.get_state().lanes[FPS].op, S.OP_LOADED)

    def test_async_request_rejects_a_non_callable(self):
        runtime, reactor, oam, events = make_runtime()
        with self.assertRaises(TypeError):
            runtime.request_async(FPS, S.LoadBay(FPS, UNIT), None)

    def test_async_request_after_session_end_is_refused_not_dispatched(self):
        runtime, reactor, oam, events = make_runtime()
        runtime.end_session("shutdown")
        results = []
        runtime.request_async(FPS, S.LoadBay(FPS, UNIT), results.append)
        reactor.run_async_callbacks()
        self.assertFalse(results[0].ok)
        self.assertEqual(oam.calls, [])

    def test_async_waiter_is_settled_by_end_session(self):
        runtime, reactor, oam, events = make_runtime()
        results = []
        runtime.request_async(FPS, S.LoadBay(FPS, UNIT), results.append)
        runtime.end_session("shutdown")
        reactor.run_async_callbacks()
        self.assertFalse(results[0].ok)


class NativeCancelTests(unittest.TestCase):
    def test_timeout_cancels_load_with_full_generation(self):
        runtime, reactor, oam, events = make_runtime(oam=FakeNativeOam())
        runtime.system = _lane(S.OP_UNLOADED, 0x12345)
        completion = runtime.request(FPS, S.LoadBay(FPS, UNIT))
        gen = oam.calls[-1][2]
        self.assertEqual(gen, 0x12346)
        reactor.now = S.OAMS_ACTION_TIMEOUT + 1.0
        reactor.fire_due_timers()
        self.assertFalse(completion.wait().ok)
        self.assertIn(("cancel", oam.status_loading, 0x12346), oam.calls)

    def test_explicit_unload_cancel_reaches_firmware_with_generation(self):
        runtime, reactor, oam, events = make_runtime(oam=FakeNativeOam())
        runtime.system = _lane(S.OP_LOADED, 40, group="G", unit=UNIT)
        runtime.request_async(FPS, S.Unload(FPS), lambda result: None)
        gen = oam.calls[-1][1]
        self.assertEqual(gen, 41)
        runtime.dispatch(S.Cancel(FPS))
        self.assertIn(("cancel", oam.status_unloading, 41), oam.calls)

    def test_pre_v4_driver_keeps_its_original_cancel(self):
        runtime, reactor, oam, events = make_runtime()
        runtime.system = _lane(S.OP_UNLOADED, 3)
        runtime.request(FPS, S.LoadBay(FPS, UNIT))
        runtime.dispatch(S.Cancel(FPS))
        self.assertIn(("cancel_load",), oam.calls)

    def test_pre_v4_driver_gets_no_unload_cancel(self):
        runtime, reactor, oam, events = make_runtime()
        runtime.system = _lane(S.OP_LOADED, 40, group="G", unit=UNIT)
        runtime.request_async(FPS, S.Unload(FPS), lambda result: None)
        runtime.dispatch(S.Cancel(FPS))
        self.assertFalse([c for c in oam.calls if c[0].startswith("cancel")])


class WireWidthTests(unittest.TestCase):
    """The reducer's generation is 32 bits wide; a driver that cannot carry
    it keeps receiving and echoing one byte, exactly as before."""

    def test_native_driver_receives_the_full_generation(self):
        runtime, reactor, oam, events = make_runtime(oam=FakeNativeOam())
        runtime.system = _lane(S.OP_UNLOADED, 0xFFFFFFFE)
        runtime.request(FPS, S.LoadBay(FPS, UNIT))
        self.assertEqual(oam.calls[-1], ("load", 0, 0xFFFFFFFF))

    def test_one_byte_driver_receives_the_low_byte(self):
        runtime, reactor, oam, events = make_runtime()
        runtime.system = _lane(S.OP_UNLOADED, 0x2FF)
        runtime.request(FPS, S.LoadBay(FPS, UNIT))
        self.assertEqual(oam.calls[-1], ("load", 0, 0x00))   # op 0x300

    def test_one_byte_echo_completes_the_wide_operation(self):
        runtime, reactor, oam, events = make_runtime()
        runtime.system = _lane(S.OP_UNLOADED, 0x2FF)
        completion = runtime.request(FPS, S.LoadBay(FPS, UNIT))
        runtime.dispatch(S.OpCompleted(FPS, S.OAMS_OP_CODE_SUCCESS, gen=0))
        self.assertTrue(completion.wait().ok)
        self.assertEqual(runtime.get_state().lanes[FPS].op, S.OP_LOADED)

    def test_one_byte_stale_echo_is_still_rejected(self):
        runtime, reactor, oam, events = make_runtime()
        runtime.system = _lane(S.OP_UNLOADED, 0x2FF)
        completion = runtime.request(FPS, S.LoadBay(FPS, UNIT))
        runtime.dispatch(S.OpCompleted(FPS, S.OAMS_OP_CODE_SUCCESS, gen=0xFF))
        self.assertFalse(completion.test())
        self.assertEqual(runtime.get_state().lanes[FPS].op, S.OP_LOADING)

    def test_small_generations_are_unchanged(self):
        runtime, reactor, oam, events = make_runtime()
        runtime.system = _lane(S.OP_UNLOADED, 6)
        completion = runtime.request(FPS, S.LoadBay(FPS, UNIT))
        self.assertEqual(oam.calls[-1], ("load", 0, 7))
        runtime.dispatch(S.OpCompleted(FPS, S.OAMS_OP_CODE_SUCCESS, gen=7))
        self.assertTrue(completion.wait().ok)

    def test_terminal_acceptance_maps_the_one_byte_identity(self):
        runtime, reactor, oam, events = make_runtime()
        runtime.system = _lane(S.OP_UNLOADED, 0x2FF)
        completion = runtime.request(FPS, S.LoadBay(FPS, UNIT))
        self.assertTrue(runtime.accept_terminal(
            FPS, S.OAMS_OP_CODE_SUCCESS, None, 0))
        self.assertTrue(completion.wait().ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)
