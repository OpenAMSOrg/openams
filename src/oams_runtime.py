# OpenAMS — runtime (store + effect executor + completion/timeout plumbing)
#
# Copyright (C) 2025-2026 JR Lomas <lomas.jr@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
# The runtime is the ONLY place that performs side effects and touches the
# reactor/gcode. It holds the immutable SystemState, runs the pure reducer from
# oams_state.py on every action, and executes the returned effects. All dispatch
# happens on the reactor thread (the OAMS driver marshals firmware replies there
# via register_async_callback), so the store needs no locks.

import logging
from collections import deque

from . import oams_state as S

# Stall detection (encoder-didn't-move) while a load/unload op is in flight.
STALL_SAMPLES = 2
MIN_ENCODER_DIFF = 1
STALL_AFTER = 2.0   # seconds after op start before sampling begins


class _AsyncWaiter:
    """A completion that delivers its result to a callback instead of a
    blocked greenlet. It answers the same test/complete/wait calls as a
    reactor completion, so every waiter check in the runtime treats it alike.
    The callback runs from a fresh reactor callback, never inside the
    reducer's dispatch."""

    def __init__(self, reactor, callback):
        self._reactor = reactor
        self._callback = callback
        self._result = None
        self._done = False

    def test(self):
        return self._done

    def complete(self, result):
        if self._done:
            return
        self._done = True
        self._result = result
        callback = self._callback
        self._reactor.register_async_callback(
            lambda eventtime: callback(result))

    def wait(self):
        return self._result


class Runtime:
    _narrowed = None

    def __init__(self, printer, fps_names, build_world, resolve_oam):
        self.printer = printer
        self.reactor = printer.get_reactor()
        self.build_world = build_world      # callable(now) -> S.World
        self.resolve_oam = resolve_oam      # callable(oams_idx) -> oam | None
        self.system = S.initial_system(fps_names)
        self._pending = {}                  # fps -> ReactorCompletion
        self._deadlines = {}                # fps -> reactor timer handle
        self._stall = {}                    # fps -> deque(maxlen=STALL_SAMPLES)
        # At most one accepted retained event per physical lane.
        self._accepted_firmware_faults = {}
        self._firmware_fault_waiters = {}
        # fps -> (logical gen, wire byte) for the operation most recently
        # started on a driver whose wire carries one byte (see _driver_gen).
        self._narrowed = {}

    # -------------------------------------------------------------- dispatch

    def get_state(self):
        return self.system

    def set_firmware_liveness(self, owns):
        """Record whether the firmware owns per-op liveness (protocol >= 3).
        When it does, the host stops running its own encoder-stall watchdog and
        downgrades its op deadline to a coarse disconnect backstop."""
        self.system = S.set_liveness(self.system, owns)

    def dispatch(self, action):
        if isinstance(action, S.OpCompleted):
            gen = self._widen(action.fps, action.gen)
            if gen != action.gen:
                action = S.replace(action, gen=gen)
        prev = self.system
        try:
            now = self.reactor.monotonic()
            # Only build the hardware snapshot for actions that read it: a
            # transient world-build failure must not be able to kill the
            # reduction of an unrelated completion, and completions/timeouts
            # fire far more often than world-consuming actions.
            world = (self.build_world(now)
                     if isinstance(action, S.WORLD_ACTIONS) else S.World())
            self.system, effects = S.reduce(prev, action, world, now)
        except Exception:
            logging.exception("OAMS: reducer failed on %s", type(action).__name__)
            # Never leave a gcode waiter blocked on the op-starting dispatch it
            # is waiting for. Crashes on OTHER actions (Cancel, a completion,
            # a tick) must NOT fail an unrelated op in flight — its own
            # completion or deadline will settle it.
            if isinstance(action, (S.Load, S.LoadBay, S.Unload, S.Calibrate)):
                self._settle(action.fps, S.OpResult(False, None,
                             "internal error (see klippy.log)"))
            return self.system
        self._log_transitions(prev, self.system, action)
        for e in effects:
            try:
                self._apply(e)
            except Exception:
                logging.exception("OAMS: effect %s failed", type(e).__name__)
                self._effect_failed(e)
        return self.system

    def end_session(self, reason):
        """Release every in-flight op because Klipper stopped or disconnected.

        The firmware has already stopped all motion, so settle every G-code
        waiter now and cancel every deadline; nothing is sent and nothing is
        dispatched (a shut-down or gone MCU must not receive commands). The
        recovery path is FIRMWARE_RESTART, which rebuilds the host state, so
        this is idempotent. Any request after this is refused by ``request``.
        """
        self._session_ended = reason
        for fps in list(self._deadlines):
            self._cancel_deadline(fps)
        for fps in list(self._pending):
            self._settle(fps, self._session_ended_result())

    def _session_ended_result(self):
        return S.OpResult(False, None, "OpenAMS: %s; the operation was"
                          " abandoned (run FIRMWARE_RESTART)"
                          % getattr(self, "_session_ended", None))

    def request(self, fps, action):
        """Start an op and return a ReactorCompletion the caller can wait() on.
        The completion is settled by a later OpCompleted/Timeout (or immediately
        if the reducer rejects the request)."""
        # A waiter normally holds the gcode mutex, so two concurrent requests on
        # one lane should be impossible — but never leak a previous waiter.
        self._settle(fps, S.OpResult(False, None, "superseded by a new request"))
        completion = self.reactor.completion()
        if getattr(self, "_session_ended", None) is not None:
            # Refuse without dispatching: a new op would enter a state that no
            # firmware reply can ever complete.
            completion.complete(self._session_ended_result())
            return completion
        self._pending[fps] = completion
        getattr(self, "_terminal_stop_pending", {}).pop(fps, None)
        getattr(self, "_terminal_stop_proof", {}).pop(fps, None)
        self.dispatch(action)
        if isinstance(action, S.Calibrate):
            lane = self.system.lanes.get(fps)
            if (lane is not None and lane.op == S.OP_CALIBRATING
                    and self._pending.get(fps) is completion
                    and not completion.test()):
                self._retired_calibrate_units = getattr(
                    self, "_retired_calibrate_units", {})
                self._retired_calibrate_units[fps] = (
                    (action.oams_idx, action.bay), lane.op_gen, completion)
        return completion

    def request_async(self, fps, action, callback):
        """Start an op without blocking the Klippy reactor greenlet.

        ``callback(result)`` is invoked from a fresh reactor callback after
        the reducer has settled the operation. This is the HELIX semantic
        operation boundary; ordinary G-code callers keep using request().
        """
        if not callable(callback):
            raise TypeError("OpenAMS async completion must be callable")
        self._settle(fps, S.OpResult(False, None, "superseded by a new request"))
        if getattr(self, "_session_ended", None) is not None:
            result = self._session_ended_result()
            self.reactor.register_async_callback(
                lambda eventtime: callback(result))
            return

        self._pending[fps] = _AsyncWaiter(self.reactor, callback)
        getattr(self, "_terminal_stop_pending", {}).pop(fps, None)
        getattr(self, "_terminal_stop_proof", {}).pop(fps, None)
        self.dispatch(action)

    def _driver_gen(self, oam, fps, gen):
        """The generation value a driver should put on its wire.

        The reducer's generation is a nonzero 32 bit value. Only a driver that
        declares ``supports_native_operations`` (protocol v4) carries all of
        it. Every other driver (v2, legacy, the follower, the MMU bridge) sends
        or stores one byte, exactly as before the widening: it receives the low
        byte here, and ``_widen`` maps its echo back to the logical value.
        """
        if gen is None:
            return None
        if self._narrowed is None:
            self._narrowed = {}
        if getattr(oam, "supports_native_operations", False) is True:
            self._narrowed.pop(fps, None)
            return gen
        wire = gen & 0xFF
        if wire == gen:
            self._narrowed.pop(fps, None)
        else:
            self._narrowed[fps] = (gen, wire)
        return wire

    def _widen(self, fps, gen):
        """Map a one-byte echo back to the logical generation of the operation
        that was started with it. Anything else passes through unchanged, so
        the reducer still rejects a stale or unsolicited completion."""
        narrowed = (self._narrowed or {}).get(fps)
        if narrowed is not None and type(gen) is int and gen == narrowed[1]:
            return narrowed[0]
        return gen

    def accept_terminal(self, fps, code, value, gen):
        """Accept an exact MMU terminal only after its G-code waiter settles.

        Call on the reactor thread. Auto-reload has no request waiter and must
        be handled separately; this method returns false for it. The caller
        retains the firmware event and withholds its ACK on a false result.
        """
        gen = self._widen(fps, gen)
        lane = self.system.lanes.get(fps)
        waiter = self._pending.get(fps)
        if (lane is None or waiter is None or waiter.test()
                or gen != lane.op_gen or lane.op not in (
                    S.OP_LOADING, S.OP_UNLOADING, S.OP_CALIBRATING)):
            return False
        calibrating = lane.op == S.OP_CALIBRATING
        action = S.OpCompleted(fps, code, value, gen)
        stop = None
        proof = getattr(self, "_terminal_stop_proof", {}).get(fps)
        if code != 0 and not calibrating:
            try:
                _, effects = S.reduce(self.system, action, S.World(),
                                      self.reactor.monotonic())
                stop = next((e for e in effects if isinstance(e, S.SetFollower)
                             and e.fps == fps and e.enable == 0), None)
                if stop is not None and proof != (gen, stop.unit):
                    pending = getattr(self, "_terminal_stop_pending", {}).get(fps)
                    if pending == (gen, stop.unit):
                        return False
                    if pending is not None:
                        self._terminal_stop_pending.pop(fps, None)
                    oam = self.resolve_oam(stop.unit[0])
                    if oam is None:
                        return False
                    response = oam.set_oams_follower(0, stop.direction)
                    if getattr(response, "follower_confirmed", False) is not True:
                        self._terminal_stop_pending = getattr(
                            self, "_terminal_stop_pending", {})
                        self._terminal_stop_pending[fps] = (gen, stop.unit)
                        return False
            except Exception:
                logging.exception("OAMS: terminal follower STOP failed")
                return False
            # The STOP call may yield to the reactor; a timeout or replacement
            # request must not be completed by this old firmware terminal.
            if (self.system.lanes.get(fps) is not lane
                    or self._pending.get(fps) is not waiter or waiter.test()
                    or self.system.lanes[fps].op_gen != gen):
                return False
        self._terminal_stop_confirmed = stop
        try:
            self.dispatch(action)
        finally:
            self._terminal_stop_confirmed = None
        if stop is not None:
            getattr(self, "_terminal_stop_proof", {}).pop(fps, None)
            getattr(self, "_terminal_stop_pending", {}).pop(fps, None)
        if not waiter.test():
            return False
        result = waiter.wait()
        return (result.code == code and result.ok == (code == 0)
                and (not calibrating or result.value == value))

    def accept_reload_terminal(self, fps, code, value, gen, bay):
        """Accept the retained terminal for an active runout auto-reload.

        The caller must first match the full firmware request identity. This
        operation has no G-code waiter, so require its exact current lane and
        generation, then verify that dispatch applied the expected transition.
        """
        gen = self._widen(fps, gen)
        lane = self.system.lanes.get(fps)
        if (lane is None or fps in self._pending or gen != lane.op_gen
                or lane.op != S.OP_LOADED or lane.runout != S.RUNOUT_LOADING
                or lane.reload_target is None
                or type(bay) is not int or bay != lane.reload_target[1]):
            return False
        action = S.OpCompleted(fps, code, value, gen)
        expected, _ = S.reduce(self.system, action, S.World(),
                               self.reactor.monotonic())
        expected_lane = expected.lanes[fps]
        if expected_lane == lane:
            return False
        self.dispatch(action)
        return self.system.lanes.get(fps) == expected_lane

    def accept_retired_terminal(self, fps, unit, gen, code, value,
                                disarmed_proof):
        """Settle one old operation after a fresh same-controller STOP proof.

        The caller validates the retired session and retained queue head before
        calling on the reactor thread. ``physical_disarmed`` must be the fresh
        physical observation for this exact controller, unit and generation;
        command admission alone is not proof. This method never ACKs an event.
        """
        if (disarmed_proof != (unit, gen, True) or type(unit) is not tuple
                or len(unit) != 2 or type(gen) is not int
                or getattr(self, "_retired_terminal_busy", False)):
            return False
        gen = self._widen(fps, gen)
        lane = self.system.lanes.get(fps)
        waiter = self._pending.get(fps)
        calibration_unit = getattr(self, "_retired_calibrate_units", {}).get(fps)
        owns_unit = (lane is not None and (lane.unit == unit or
            (lane.op == S.OP_CALIBRATING and
             calibration_unit == (unit, gen, waiter))))
        if (lane is None or not owns_unit or lane.op_gen != gen
                or lane.op not in (S.OP_LOADING, S.OP_UNLOADING,
                                   S.OP_CALIBRATING)
                or waiter is None or waiter.test()):
            return False
        before = self.system
        action = S.OpCompleted(fps, code, value, gen)
        try:
            after, effects = S.reduce(before, action, S.World(),
                                      self.reactor.monotonic())
        except Exception:
            logging.exception("OAMS: retired terminal reducer failed")
            return False
        if after == before:
            return False
        # The fresh physical proof supersedes any logical follower flag that
        # the terminal reducer retained (notably successful LOAD/CALIBRATE).
        after = S.replace(after, lanes=dict(after.lanes, **{
            fps: S.replace(after.lanes[fps], following=False)}))
        settles = [e for e in effects if isinstance(e, S.Settle)]
        if (len(settles) != 1 or settles[0].fps != fps
                or settles[0].result.code != code
                or settles[0].result.ok != (code == 0)
                or (lane.op == S.OP_CALIBRATING
                    and settles[0].result.value != value)):
            return False
        for effect in effects:
            if isinstance(effect, S.SetFollower):
                if (effect.unit != unit or effect.enable != 0
                        or effect.fps != fps):
                    return False
            elif not isinstance(effect, (S.CancelDeadline, S.Settle)):
                return False
        self._retired_terminal_busy = True
        try:
            # Cancel the old deadline before committing. A callback that runs
            # during cancellation may replace this operation; recheck after it.
            for effect in effects:
                if isinstance(effect, S.CancelDeadline):
                    self._apply(effect)
            if (self.system is not before or self.system.lanes.get(fps) is not lane
                    or self._pending.get(fps) is not waiter or waiter.test()):
                return False
            self.system = after
            self._log_transitions(before, after, action)
            # The physical STOP has already been observed. Never issue the
            # reducer's redundant SetFollower effect through the command path.
            self._apply(settles[0])
            getattr(self, "_retired_calibrate_units", {}).pop(fps, None)
            if not waiter.test():
                return False
            result = waiter.wait()
            return (result.code == code and result.ok == (code == 0)
                    and (lane.op != S.OP_CALIBRATING or result.value == value))
        except Exception:
            logging.exception("OAMS: retired terminal effect failed")
            return False
        finally:
            self._retired_terminal_busy = False

    def accept_verified_follower(self, fps, unit, following):
        """Apply a fresh physical follower observation on the reactor thread.

        The caller must have verified the device identity and motor state;
        command admission and hub sensors do not provide that proof. An orphan
        unit may only be cleared by a verified stop while it remains recorded
        as uncertain on this lane.
        """
        lane = self.system.lanes.get(fps)
        if (type(following) is not bool or not isinstance(unit, tuple)
                or len(unit) != 2 or lane is None
                or (unit != lane.unit and
                    (following or unit not in lane.uncertain_follower_units))):
            return False
        action = S.FollowerApplied(fps, unit, following)
        try:
            expected, _ = S.reduce(self.system, action, S.World(),
                                   self.reactor.monotonic())
        except Exception:
            return False
        self.dispatch(action)
        # dispatch logs and absorbs reducer failures. A verified observation is
        # accepted only after the exact resulting state has been committed.
        accepted = self.system == expected
        if accepted:
            if following:
                getattr(self, "_terminal_stop_proof", {}).pop(fps, None)
                getattr(self, "_terminal_stop_pending", {}).pop(fps, None)
            elif getattr(self, "_terminal_stop_pending", {}).get(fps) == (lane.op_gen, unit):
                self._terminal_stop_proof = getattr(self, "_terminal_stop_proof", {})
                self._terminal_stop_proof[fps] = (lane.op_gen, unit)
                self._terminal_stop_pending.pop(fps, None)
        return accepted

    def accept_firmware_fault(self, fps, physical_lane, code, identity,
                              from_command=False):
        """Accept a retained FAULT_ENTERED on the reactor thread.

        The caller must first verify MCU, session, and event identity. False
        means the retained event must not be acknowledged. The same exact
        event can retry a failed effect; another event cannot replace it.
        A caller inside a G-code command handler passes from_command=True:
        it already holds the G-code mutex, so PAUSE must not take it again.
        """
        action = S.FirmwareFault(fps, physical_lane, code, identity)
        record = (fps, code, identity)
        existing = self.system.firmware_faults.get(physical_lane)
        if existing is not None and existing != record:
            return False
        lane = self.system.lanes.get(fps) if fps is not None else None
        if fps is not None and lane is not None and (
                lane.following is not False or lane.uncertain_follower_units):
            return False
        if fps is None and any(
                lane.following is not False or lane.uncertain_follower_units
                for lane in self.system.lanes.values()):
            return False
        if existing == record and self._accepted_firmware_faults.get(physical_lane) == record:
            return True
        needs_waiter = (lane is not None and lane.op in
                        (S.OP_LOADING, S.OP_UNLOADING, S.OP_CALIBRATING))
        waiter = (self._pending.get(fps) if existing is None else
                  self._firmware_fault_waiters.get(physical_lane)) if needs_waiter else None
        if needs_waiter and (waiter is None or existing is None and waiter.test()):
            return False
        if existing is None:
            try:
                expected, effects = S.reduce(self.system, action, S.World(),
                                             self.reactor.monotonic())
            except Exception:
                logging.exception("OAMS: firmware fault reducer failed")
                return False
            if expected == self.system or expected.firmware_faults.get(physical_lane) != record:
                return False
            self.system = expected
            if needs_waiter:
                self._firmware_fault_waiters[physical_lane] = waiter
        else:
            # Retry only the unfinished effects of this same retained event.
            effects = []
            if fps is not None:
                if lane.op_deadline is not None:
                    effects.append(S.CancelDeadline(fps))
                if needs_waiter:
                    effects.append(S.Settle(fps, S.OpResult(
                        False, code, "firmware fault (%s)" % code)))
            effects.append(S.Pause(fps or "global", "firmware fault (%s)" % code))
        try:
            for effect in effects:
                if isinstance(effect, S.Pause):
                    # _pause logs and swallows failures, so use the strict path.
                    gcode = self.printer.lookup_object("gcode")
                    (gcode.run_script_from_command if from_command
                     else gcode.run_script)("PAUSE")
                else:
                    self._apply(effect)
            if needs_waiter and not (waiter.test() and
                    waiter.wait().code == code and not waiter.wait().ok):
                return False
        except Exception:
            logging.exception("OAMS: firmware fault effect failed")
            return False
        self._accepted_firmware_faults[physical_lane] = record
        return True

    def accept_firmware_fault_cleared(self, physical_lane, identity):
        """Remove a latched firmware fault after a verified clear (reactor thread).

        The caller must have independently verified the firmware left FAULT;
        this only removes the latch when it still matches exactly and resyncs
        the affected lane(s) from hardware. False means nothing changed, so a
        stale or mismatched call is always safe to retry or drop. No
        pause/resume side effects.
        """
        action = S.FirmwareFaultCleared(physical_lane, identity)
        try:
            now = self.reactor.monotonic()
            expected, effects = S.reduce(self.system, action,
                                         self.build_world(now), now)
        except Exception:
            logging.exception("OAMS: firmware fault clear reducer failed")
            return False
        if expected == self.system or effects:
            return False
        self.system = expected
        self._accepted_firmware_faults.pop(physical_lane, None)
        self._firmware_fault_waiters.pop(physical_lane, None)
        return True

    def tick(self):
        """Called once per monitor interval from the manager's reactor timer."""
        self.dispatch(S.Tick())
        self._check_stalls()

    def _log_transitions(self, prev, cur, action):
        for fps, lane in cur.lanes.items():
            pl = prev.lanes.get(fps)
            if pl is None or pl.op != lane.op or pl.runout != lane.runout:
                logging.info("OAMS[%s] %s/%s --%s--> %s/%s%s", fps,
                             None if pl is None else pl.op,
                             None if pl is None else pl.runout,
                             type(action).__name__, lane.op, lane.runout,
                             "" if not lane.message else " (%s)" % lane.message)

    # --------------------------------------------------------------- effects

    def _apply(self, e):
        ended = getattr(self, "_session_ended", None)
        if ended is not None and not isinstance(e, (S.Settle, S.CancelDeadline)):
            # The session is over: send nothing and run no G-code. Only the
            # effects that release local state are still safe and required.
            logging.info("OAMS: session ended (%s); dropping %s effect",
                         ended, type(e).__name__)
            return
        if isinstance(e, S.SetFollower) and e.enable and e.fps:
            getattr(self, "_terminal_stop_proof", {}).pop(e.fps, None)
            getattr(self, "_terminal_stop_pending", {}).pop(e.fps, None)
        if (isinstance(e, S.SetFollower) and
                e == getattr(self, "_terminal_stop_confirmed", None)):
            self._terminal_stop_confirmed = None
            return
        if isinstance(e, S.StartLoad):
            oam = self._start_oam(e)
            oam.start_load_spool(
                e.unit[1], gen=self._driver_gen(oam, e.fps, e.gen))
        elif isinstance(e, S.StartUnload):
            oam = self._start_oam(e)
            oam.start_unload_spool(
                gen=self._driver_gen(oam, e.fps, e.gen))
        elif isinstance(e, S.StartCalibrate):
            oam = self._start_oam(e)
            oam.start_calibrate(
                e.kind, e.unit[1],
                gen=self._driver_gen(oam, e.fps, e.gen))
        elif isinstance(e, S.CancelLoad):
            oam = self.resolve_oam(e.unit[0])
            if oam is None:
                logging.warning("OAMS: cannot cancel load, unit %s unknown",
                                e.unit[0])
            elif getattr(oam, "supports_native_operations", False) is True:
                oam.cancel_operation(oam.status_loading, e.gen)
            else:
                oam.load_spool_cancel()
        elif isinstance(e, S.CancelUnload):
            oam = self.resolve_oam(e.unit[0])
            if oam is None:
                logging.warning("OAMS: cannot cancel unload, unit %s unknown",
                                e.unit[0])
            elif getattr(oam, "supports_native_operations", False) is True:
                oam.cancel_operation(oam.status_unloading, e.gen)
            # Older families cannot cancel an unload on the wire; the
            # follower stop the reducer also emits still applies.
        elif isinstance(e, S.SetFollower):
            oam = self.resolve_oam(e.unit[0])
            if oam is None:
                raise RuntimeError("OAMS: cannot set follower, unit %s unknown"
                                   % (e.unit[0],))
            else:
                response = oam.set_oams_follower(e.enable, e.direction)
                if e.fps:
                    # Legacy drivers return None after their local send. New
                    # devices may return admission without proving motor state.
                    if getattr(response, "follower_confirmed", True) is False:
                        self.dispatch(S.FollowerFailed(e.fps, e.unit))
                    elif getattr(response, "follower_refused", False):
                        # The firmware said no: it is known not to be
                        # following, which is not the same as unknown.
                        self.dispatch(S.FollowerApplied(
                            e.fps, e.unit, False))
                    else:
                        self.dispatch(S.FollowerApplied(
                            e.fps, e.unit, bool(e.enable)))
        elif isinstance(e, S.Pause):
            self._pause(e.reason)
        elif isinstance(e, S.ArmDeadline):
            self._arm_deadline(e.fps, e.seconds)
        elif isinstance(e, S.CancelDeadline):
            self._cancel_deadline(e.fps)
        elif isinstance(e, S.Settle):
            self._settle(e.fps, e.result)

    def _start_oam(self, e):
        """Resolve the OAMS unit a Start* effect targets, or raise so the
        failure is surfaced now instead of as a generic timeout 120 s later."""
        oam = self.resolve_oam(e.unit[0])
        if oam is None:
            raise RuntimeError("OAMS unit %s is not configured" % (e.unit[0],))
        return oam

    def _effect_failed(self, e):
        if isinstance(e, S.SetFollower) and e.fps:
            self.dispatch(S.FollowerFailed(e.fps, e.unit))
            return
        # A Start* effect that raised (unit missing, MCU not connected, send
        # failure) leaves the lane in an in-flight op state that nothing will
        # ever complete before the deadline. Fail it now — deferred one reactor
        # iteration so the current dispatch (including a trailing ArmDeadline)
        # finishes first.
        if not isinstance(e, (S.StartLoad, S.StartUnload, S.StartCalibrate)):
            return
        fps, gen = e.fps, e.gen

        def fail(eventtime):
            self.dispatch(S.OpCompleted(
                fps, S.OAMS_OP_CODE_ERROR_KLIPPER_CALL, gen=gen))

        self.reactor.register_async_callback(fail)

    def _pause(self, reason):
        try:
            gcode = self.printer.lookup_object("gcode")
            gcode.run_script("M118 OAMS: %s" % reason)
            gcode.run_script("M117 OAMS paused")
            gcode.run_script("PAUSE")
        except Exception:
            logging.exception("OAMS: failed to issue PAUSE")

    def _arm_deadline(self, fps, seconds):
        self._cancel_deadline(fps)

        def fire(eventtime, fps=fps):
            self.dispatch(S.Timeout(fps))
            return self.reactor.NEVER

        self._deadlines[fps] = self.reactor.register_timer(
            fire, self.reactor.monotonic() + seconds)

    def _cancel_deadline(self, fps):
        timer = self._deadlines.pop(fps, None)
        if timer is not None:
            self.reactor.unregister_timer(timer)

    def _settle(self, fps, result):
        completion = self._pending.pop(fps, None)
        if completion is not None and not completion.test():
            completion.complete(result)

    # ---------------------------------------------------------- stall detect

    def _check_stalls(self):
        # Protocol >= 3 firmware runs its own no-progress watchdog (and stops
        # the motors, completing the op with code TIMEOUT), so the host's
        # encoder-stall detection is redundant and disabled.
        if self.system.fw_owns_liveness:
            return
        now = self.reactor.monotonic()
        for fps, lane in list(self.system.lanes.items()):
            # Watched ops: explicit load/unload, and the runout auto-reload
            # (op stays LOADED while the reload firmware op feeds from
            # reload_target — a jam there must pause promptly, not print the
            # dwindling tail for the whole deadline).
            if lane.op in (S.OP_LOADING, S.OP_UNLOADING) and lane.unit is not None:
                unit, label = lane.unit, lane.op
            elif (lane.op == S.OP_LOADED and lane.runout == S.RUNOUT_LOADING
                    and lane.reload_target is not None):
                unit, label = lane.reload_target, "RELOADING"
            else:
                self._stall.pop(fps, None)
                continue
            if now - lane.since <= STALL_AFTER:
                self._stall.pop(fps, None)   # reset window at op start
                continue
            oam = self.resolve_oam(unit[0])
            if oam is None:
                continue
            samples = self._stall.setdefault(fps, deque(maxlen=STALL_SAMPLES))
            samples.append(oam.encoder_clicks)
            if len(samples) < STALL_SAMPLES:
                continue
            if abs(samples[-1] - samples[0]) < MIN_ENCODER_DIFF:
                logging.info("OAMS[%s]: %s stall detected", fps, label)
                self._stall.pop(fps, None)
                # Fail the op FIRST (settles any waiter via the unified timeout
                # path; for a reload it cancels the firmware op and pauses).
                # The waiter may hold the gcode mutex that _pause's
                # run_script("PAUSE") needs, so pausing before settling would
                # block this tick until the op deadline rescued it.
                self.dispatch(S.Timeout(fps))
                oam.set_led_error(unit[1], 1)
                if label != "RELOADING":
                    # (the reducer's reload-timeout branch pauses by itself)
                    self._pause("%s speed too low" % label.lower())
