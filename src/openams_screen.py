#
# Copyright (C) 2025-2026 JR Lomas <lomas.jr@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
# openams_screen: what the printer's LCD "AMS status" screen reads
# (docs/contracts/C2_status.md section 6a). One Klipper printer object,
# "openams_screen", registered by oams_manager. It carries what the screen
# shows and Klipper's other objects do not: each bay's material and the
# spool's remaining percent (the operating copy openams_spoolman pushes with
# OAMSM_SET_BAY_FILAMENT), the units the screen pages through, and the unit
# it shows now. It exists for the LCD only: spool data stays off the
# openams_ui projections (C2 §6.2 `sp`, review 4/7).
#
# A display redraw reads it twice a second, so get_status() is cheap: it
# rebuilds at most every `interval` seconds, from the units' in-memory
# values and the reducer state (never from oams_manager.get_status()), and
# hands back the same object while nothing it carries changed.

import logging
import os
import sys

# install.sh links each module into klippy/extras when it runs, and an update
# is only a git pull: a module a later release added has no link of its own, so
# the package also looks for siblings in this file's checkout (install.md).
if __package__:
    _CHECKOUT = os.path.dirname(os.path.realpath(__file__))
    if _CHECKOUT not in sys.modules[__package__].__path__:
        sys.modules[__package__].__path__.append(_CHECKOUT)

try:                                  # deployed as a Klipper extras package
    from . import oams_state as S
except ImportError:                   # imported standalone (src/ on sys.path)
    import oams_state as S

# The screen shows each unit for this long in turn when it pages.
PAGE_S = 5.0
# get_status() rebuilds at most this often (a display redraws every 0.5 s).
INTERVAL = 0.25
# print_stats states that count as a print running: the screen stays on the
# loaded unit and does not page.
PRINT_STATES = ("printing", "paused")
# Fault severities that hold the screen on a unit; an `info` fault (a
# missing arrival signal, a cloned tag) never does.
HOLDING_SEVERITIES = ("stop", "pause")
# The longest material label published, as OAMSM_SET_BAY_FILAMENT keeps it.
MATERIAL_MAX = 16


def pick_active(units, pin, printing, loaded, faulted, busy, last, page):
    """The unit the screen shows (the owner's rules, 2026-11-11).

    `units` is the ordered unit list; `loaded`, `faulted` and `busy` are sets
    of unit names; `last` is the unit shown before; `page` the paging step
    (seconds since start // PAGE_S).

    - A pinned unit (OAMSM_SCREEN UNIT=) always wins.
    - While a print runs: the unit holding the loaded bay; with none loaded
      (mid toolchange), a unit with a fault, then one loading or unloading;
      else the unit shown before. Never pages during a print.
    - Otherwise: a unit with a fault, then one loading or unloading; else
      each unit for PAGE_S seconds in turn.
    """
    if not units:
        return None
    if pin in units:
        return pin
    order = (loaded, faulted, busy) if printing else (faulted, busy)
    for wanted in order:
        for name in units:
            if name in wanted:
                return name
    if printing:
        return last if last in units else units[0]
    return units[page % len(units)]


def tag_material(tag):
    """A material from a bay's tag record (C2 §4.6), or None: a unit's own
    decoded summary, a decoded Bambu tag, an OpenSpool record."""
    if not isinstance(tag, dict):
        return None
    for source, key in (("summary", "material"), ("bambu", "type"),
                        ("openspool", "type")):
        value = (tag.get(source) or {}).get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:MATERIAL_MAX]
    return None


class OpenAMSScreen:
    """The "openams_screen" printer object (C2 §6a)."""

    def __init__(self, manager, interval=INTERVAL, page_s=PAGE_S):
        self.manager = manager
        self.interval = interval
        self.page_s = page_s
        self.pin = None
        self._key = None
        self._status = {"v": 1, "units": [], "active": None, "pin": None,
                        "bays": {}}
        self._built_at = None
        self._last_active = None
        self._logged = False

    # ------------------------------------------------------------- the pin

    def units(self):
        """The units the screen pages through: every unit with spool bays,
        in the manager's order (an inline follower has none to show)."""
        mgr = self.manager
        names = []
        for name in mgr._ordered_unit_names():
            try:
                if mgr.topo.kind_of(name) == "follower":
                    continue
            except KeyError:
                continue             # not in the topology (a rebuild runs)
            names.append(name)
        return names

    def set_pin(self, name):
        """Show `name` whatever the rules say, or follow them (None)."""
        self.pin = name
        self._built_at = None        # the next read reflects it at once

    # ------------------------------------------------------------- status

    def get_status(self, eventtime=None):
        mgr = self.manager
        now = mgr.reactor.monotonic() if eventtime is None else eventtime
        if (self._built_at is not None
                and now - self._built_at < self.interval):
            return self._status
        self._built_at = now
        try:
            key = self._inputs(now)
        except Exception:
            if not self._logged:
                self._logged = True
                logging.exception("OAMS: openams_screen not rebuilt")
            return self._status
        if key != self._key:
            self._key = key
            units, bays, active, pin, tool = key
            self._status = {
                "v": 1, "units": list(units), "active": active, "pin": pin,
                "tool": tool,
                "bays": {name: [{"m": m, "r": r} for m, r in entries]
                         for name, entries in zip(units, bays)}}
        return self._status

    def _inputs(self, now):
        """Everything the object carries, as a comparable tuple."""
        units = tuple(self.units())
        bays = tuple(self._bays(name) for name in units)
        active = self._active(now, units)
        pin = self.pin if self.pin in units else None
        return units, bays, active, pin, self._tool(active)

    @staticmethod
    def _plan_runs(lane):
        return bool(lane.stage_plan and lane.stage_index is not None
                    and lane.stage_failed is None)

    def _tool(self, active):
        """The tool a running plan works on in the shown unit's lane: the
        loaded group, else the group the plan brings in. None otherwise."""
        mgr = self.manager
        if active is None:
            return None
        lane = mgr.runtime.get_state().lanes.get(
            mgr.topo.lane_of_oams(active))
        if lane is None or not self._plan_runs(lane):
            return None
        return lane.group or lane.stage_group

    def _bays(self, name):
        """((material, remaining percent), ...) for one unit's bays."""
        mgr = self.manager
        unit = mgr.oams[name]
        filament_of = getattr(unit, "bay_filament", None)
        remaining_of = getattr(unit, "bay_remaining_pct", None)
        tags = getattr(unit, "tags", None) or {}
        out = []
        for bay in range(mgr.topo.bays_of(name)):
            filament = filament_of(bay) if callable(filament_of) else None
            material = (filament or {}).get("material") or \
                tag_material(tags.get(bay))
            remaining = remaining_of(bay) if callable(remaining_of) else None
            out.append((material, remaining))
        return tuple(out)

    def _faulted(self, units, state):
        """The units a `stop` or `pause` fault holds the screen on.

        Every unit of a lane carries that lane's failed load, but only the
        one whose bay failed names the bay (C2 §4.8): that one holds the
        screen. A lane fault that names no bay holds every unit of its lane
        unless another fault on that lane names one."""
        mgr = self.manager
        owned, shared = set(), {}
        for name in units:
            fps = mgr.topo.lane_of_oams(name)
            for fault in mgr._unit_faults(mgr.oams[name], state, fps):
                if fault.get("severity") not in HOLDING_SEVERITIES:
                    continue
                if fault.get("source") == "lane" and fault.get("bay") is None:
                    shared.setdefault(fps, set()).add(name)
                else:
                    owned.add(name)
        faulted = set(owned)
        for fps, names in shared.items():
            if not any(mgr.topo.lane_of_oams(n) == fps for n in owned):
                faulted |= names
        return faulted

    def _printing(self, now):
        try:
            stats = self.manager.printer.lookup_object("print_stats", None)
            if stats is None:
                return False
            return stats.get_status(now).get("state") in PRINT_STATES
        except Exception:
            return False

    def _active(self, now, units):
        mgr = self.manager
        name_of = {mgr.oams[name].oams_idx: name for name in units}
        state = mgr.runtime.get_state()
        loaded, busy = set(), set()
        for lane in state.lanes.values():
            unit = lane.unit[0] if lane.unit else None
            if lane.op == S.OP_LOADED and unit in name_of:
                loaded.add(name_of[unit])
            elif (lane.op in (S.OP_LOADING, S.OP_UNLOADING)
                    and unit in name_of):
                busy.add(name_of[unit])
            # A running step plan is a load or unload from its first step:
            # home, heat and the cut come before the AMS moves. A load into
            # an empty lane has no bay yet: the group it brings in names the
            # unit.
            if self._plan_runs(lane):
                if unit in name_of:
                    busy.add(name_of[unit])
                elif lane.stage_group in mgr.topo.groups:
                    busy.update(n for n, _bay in
                                mgr.topo.groups[lane.stage_group]
                                if n in units)
            target = lane.reload_target
            if (lane.runout == S.RUNOUT_LOADING and target
                    and target[0] in name_of):
                busy.add(name_of[target[0]])
        faulted = self._faulted(units, state)
        active = pick_active(list(units), self.pin, self._printing(now),
                             loaded, faulted, busy, self._last_active,
                             int(now // self.page_s))
        self._last_active = active
        return active
