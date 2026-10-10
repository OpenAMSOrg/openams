# The active spool and the load-time pressure advance (C4 §7.2).
#
# Moonraker attributes usage to exactly one active spool; this class watches the
# host's lane status (C2) and keeps Moonraker's active spool on the bay the
# followed lane is loaded from, by calling ``set_active_spool`` in process. With
# ``settings.apply_pa_on_load`` it also runs ``SET_PRESSURE_ADVANCE`` from the
# linked spool's ``flow_k`` extra when a lane loads, or runout-reloads onto
# another bay. It never stores the link (the host owns it, C4 §1) and never
# raises out of ``on_status``.

import logging

from . import ops
from .api import SpoolmanError, SpoolmanUnavailable

_LOGGER = logging.getLogger(__name__)

# A sentinel that marks ``_active`` as "never set by this component".  Any
# comparison with a real spool id (or ``None``) is always unequal, so the
# first status call always runs — the same guarantee the old ``_sent`` flag
# provided — without needing a separate boolean.
_UNSET = object()

LOADED = "LOADED"


class ActiveSpool(object):
    def __init__(self, api, run_gcode, set_active_spool, seed=None):
        self._api = api
        self._run_gcode = run_gcode
        self._set_active_spool = set_active_spool
        # Without a seed, ``_UNSET`` guarantees the first status always calls
        # (``None != _UNSET`` is True).  With a seed, we start at the value
        # Moonraker already holds, so no redundant call is needed, and
        # ``_seeded`` prevents clearing it to None before we have a lane.
        self._active = _UNSET if seed is None else seed
        self._seeded = seed is not None
        self._lanes = {}        # lane name -> {"op", "unit"} of the last status

    @property
    def active(self):
        """The spool id last set on Moonraker; ``None`` means cleared."""
        return None if self._active is _UNSET else self._active

    async def on_status(self, status, extruder=None):
        """One pass over an ``oams_manager`` v2 status dict (C2, C4 §7.2)."""
        try:
            await self._on_status(status, extruder)
        except Exception as error:  # a status pass must never take the host down
            _LOGGER.info("active spool: ignored error %s", error)

    async def _on_status(self, status, extruder):
        if not status or not status.get("ready"):
            return
        lanes = status.get("lanes_by_fps") or {}
        previous = self._lanes
        self._lanes = dict(
            (lane, {"op": entry.get("op"), "unit": entry.get("unit")})
            for lane, entry in lanes.items())
        name, entry = self._follow(lanes, extruder)
        spool_id = self._spool_id(status, entry)
        if self._seeded and entry is None:
            # While seeded and no lane is loaded, do not interfere with
            # Moonraker's active spool (it may have been set elsewhere). A
            # loaded lane, even on an unlinked bay, takes over (C4 §7.2).
            pass
        elif spool_id != self._active:
            # Record it only once Moonraker took it, so a failed call retries
            # on the next status instead of being forgotten.
            await self._set_active_spool(spool_id)
            self._active = spool_id
            self._seeded = False
        else:
            # Value matches the seeded state; we now own this position.
            self._seeded = False
        settings = status.get("settings") or {}
        if bool(settings.get("apply_pa_on_load")) and \
                self._is_event(name, entry, previous):
            await self._apply_pa(entry, spool_id)

    @staticmethod
    def _follow(lanes, extruder):
        """The lane to follow (C4 §7.2): the loaded one on ``extruder``.

        With no ``extruder``, a single loaded lane; several and no match is no
        lane.
        """
        loaded = [(lane, entry) for lane, entry in lanes.items()
                  if entry.get("op") == LOADED]
        if extruder is not None:
            for lane, entry in loaded:
                if entry.get("extruder") == extruder:
                    return lane, entry
            return None, None
        if len(loaded) == 1:
            return loaded[0]
        return None, None

    @staticmethod
    def _spool_id(status, entry):
        """The followed lane's bay's host link (C4 §7.2): ``[oams_idx, bay]``."""
        unit = entry.get("unit") if entry else None
        if not isinstance(unit, (list, tuple)) or len(unit) < 2:
            return None
        oams_idx, bay = unit[0], unit[1]
        oams = (status.get("topology") or {}).get("oams") or {}
        for name, topology in oams.items():
            if topology.get("idx") != oams_idx:
                continue
            for candidate in (status.get("devices") or {}).get(name, {}).get(
                    "bays") or []:
                if candidate.get("index") == bay:
                    return candidate.get("spool_id")
        return None

    @staticmethod
    def _is_event(name, entry, previous):
        """Whether this status is a load or a runout reload (C4 §7.2, first two)."""
        if entry is None or entry.get("op") != LOADED:
            return False
        before = previous.get(name)
        if before is None or before.get("op") != LOADED:
            return True
        return before.get("unit") != entry.get("unit")

    async def _apply_pa(self, entry, spool_id):
        lane_extruder = entry.get("extruder")
        if spool_id is None or lane_extruder is None:
            return
        try:
            spool = await self._api.request("GET", "/v1/spool/%s" % spool_id)
        except SpoolmanUnavailable as error:
            _LOGGER.info("active spool: PA skipped, Spoolman offline: %s", error)
            return
        except SpoolmanError as error:
            _LOGGER.info("active spool: PA skipped, Spoolman error: %s", error)
            return
        value = ops.decode_extra((spool or {}).get("extra", {}).get("flow_k"))
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return
        await self._run_gcode(
            "SET_PRESSURE_ADVANCE EXTRUDER=%s ADVANCE=%.4f"
            % (lane_extruder, value))
