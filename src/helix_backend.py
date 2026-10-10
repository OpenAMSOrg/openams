# OpenAMS — HELIX semantic-operation binding
#
# Copyright (C) 2026 JR Lomas <lomas.jr@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
# Binds HELIX material operations to the OpenAMS reducer and runtime. This is
# the only module that knows HELIX, and it imports HELIX lazily (only when no
# outcome type is injected), so importing any OpenAMS module under stock Klipper
# never imports it. OAMSManager.make_helix_backend() imports this module on
# demand.

from dataclasses import dataclass

from . import oams_state as S


@dataclass
class _Selection:
    requested_material: str
    target_fps: str
    current_fps: str = None
    active_fps: str = None
    active_kind: str = None


class HelixOpenAMSBackend:
    """Bind HELIX material operations to the real OpenAMS reducer/runtime.

    The OpenAMS operation adapter opts into request context, so all transient
    selections are keyed by immutable HELIX program generation. No G-code
    command is invoked and no reactor greenlet blocks.
    """

    helix_request_context = True

    def __init__(
        self,
        manager,
        machine_backend,
        outcome_type=None,
        deferred=None,
    ):
        if manager is None or manager.runtime is None:
            raise ValueError("OpenAMS manager/runtime is required")
        if machine_backend is None:
            raise ValueError("HELIX machine backend is required")
        if outcome_type is None or deferred is None:
            from helix.machine import OperationOutcome
            from helix.openams.host import DEFERRED

            outcome_type = outcome_type or OperationOutcome
            deferred = DEFERRED if deferred is None else deferred
        self.manager = manager
        self.machine_backend = machine_backend
        self.outcome_type = outcome_type
        self.deferred = deferred
        self._selections = {}

    def _success(self, value=None, evidence=None):
        return self.outcome_type.success(value, evidence=evidence)

    def _failure(self, error, evidence=None):
        return self.outcome_type.failure(error, evidence=evidence)

    def _delegate(
        self, name, request, finish, *arguments, cleanup=False
    ):
        method = getattr(self.machine_backend, name)

        def complete(outcome):
            if cleanup:
                self.release_program(request.id.program_generation)
            finish(outcome)

        callback = complete if cleanup else finish
        keywords = {"finish": callback}
        if getattr(
            self.machine_backend, "helix_request_context", False
        ):
            keywords["request"] = request
        result = method(*arguments, **keywords)
        if cleanup and result is not self.deferred:
            self.release_program(request.id.program_generation)
        return result

    def _selection(self, request):
        return self._selections.get(request.id.program_generation)

    def preflight(self, material, *, finish, request):
        material = str(material)
        manager = self.manager
        if not manager.ready:
            return self._failure("OpenAMS is not ready")
        target_fps = manager.group_lane.get(material)
        if target_fps is None:
            if material in manager.topo.groups:
                return self._failure(
                    "OpenAMS group '%s' has no bays" % material
                )
            return self._failure(
                "OpenAMS group '%s' does not exist" % material
            )
        lanes = manager.runtime.get_state().lanes
        for fps, lane in lanes.items():
            if (
                lane.op
                in (S.OP_LOADING, S.OP_UNLOADING, S.OP_CALIBRATING)
                or lane.runout != S.RUNOUT_IDLE
            ):
                return self._failure(
                    "OpenAMS lane '%s' is busy" % fps
                )
        loaded = [
            (fps, lane)
            for fps, lane in lanes.items()
            if lane.op == S.OP_LOADED
        ]
        if len(loaded) > 1:
            return self._failure(
                "multiple OpenAMS lanes report loaded material"
            )
        current_fps = None
        current_material = None
        if loaded:
            current_fps, lane = loaded[0]
            current_material = lane.group
            if not current_material:
                return self._failure(
                    "loaded OpenAMS bay is not assigned to a group"
                )
        evidence = {
            "target_fps": target_fps,
            "current_fps": current_fps,
            "current_material": current_material,
        }
        if current_material != material:
            self._selections[request.id.program_generation] = _Selection(
                material, target_fps, current_fps
            )
        return self._success(current_material, evidence=evidence)

    def _settle_material(
        self, request, finish, result, success_value
    ):
        selection = self._selection(request)
        if selection is not None:
            selection.active_fps = None
            selection.active_kind = None
        evidence = {
            "code": result.code,
            "message": result.message,
        }
        if result.ok:
            finish(self._success(success_value, evidence=evidence))
        else:
            finish(self._failure(result.message, evidence=evidence))

    def unload_material(self, *, finish, request):
        selection = self._selection(request)
        if selection is None:
            return self._failure("missing OpenAMS preflight context")
        if selection.current_fps is None:
            return self._success()
        selection.active_fps = selection.current_fps
        selection.active_kind = "unload"
        self.manager.runtime.request_async(
            selection.current_fps,
            S.Unload(selection.current_fps),
            lambda result: self._settle_material(
                request, finish, result, None
            ),
        )
        return self.deferred

    def load_material(self, material, *, finish, request):
        selection = self._selection(request)
        if (
            selection is None
            or str(material) != selection.requested_material
        ):
            return self._failure("stale OpenAMS material selection")
        selection.active_fps = selection.target_fps
        selection.active_kind = "load"
        self.manager.runtime.request_async(
            selection.target_fps,
            S.Load(
                selection.target_fps,
                selection.requested_material,
            ),
            lambda result: self._settle_material(
                request,
                finish,
                result,
                selection.requested_material,
            ),
        )
        return self.deferred

    def cancel_material(self, *, request):
        selection = self._selection(request)
        if (
            selection is not None
            and selection.active_fps is not None
            and selection.active_kind in ("load", "unload")
        ):
            self.manager.runtime.dispatch(
                S.Cancel(selection.active_fps)
            )

    def release_program(self, generation):
        self._selections.pop(generation, None)

    def acquire_resources(self, resources):
        return self.machine_backend.acquire_resources(resources)

    def release_resources(self, resources):
        return self.machine_backend.release_resources(resources)

    def checkpoint(self, value, policy, *, finish, request):
        return self._delegate(
            "checkpoint", request, finish, value, policy
        )

    def run_track(self, track, *, finish, request):
        return self._delegate(
            "run_track", request, finish, track
        )

    def cut_and_verify(self, *, finish, request):
        return self._delegate(
            "cut_and_verify", request, finish
        )

    def rebase_next_chapter(self, *, finish, request):
        return self._delegate(
            "rebase_next_chapter", request, finish
        )

    def safe_pause(self, *, finish, request):
        return self._delegate(
            "safe_pause",
            request,
            finish,
            cleanup=True,
        )

    def resume_job(self, *, finish, request):
        return self._delegate("resume_job", request, finish)


__all__ = ["HelixOpenAMSBackend"]
