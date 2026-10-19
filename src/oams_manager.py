# OpenAMS Manager — the system adapter
#
# Copyright (C) 2025-2026 JR Lomas <lomas.jr@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
# The manager is the thin Klipper-facing layer of the system. It builds the FPS
# lane topology from config, owns the runtime (the store + effect executor),
# exposes the OAMSM_* gcode commands and webhooks, and drives the monitor tick.
# All decision logic lives in the pure reducer (oams_state.py); all side effects
# run in the runtime (oams_runtime.py).

import inspect
import logging
import os

from . import oams_state as S
from . import oams_topology as T
from . import ams_family
from . import oams_config_io
from . import oams_ext
from .oams_runtime import Runtime
from .openams_ui import OpenAMSUIProjection

# A managed follower may acknowledge command admission before its service has
# physically applied the direction. Keep toolhead motion behind a finite gate.
FOLLOW_CONFIRM_TIMEOUT = 2.0
FOLLOW_CONFIRM_POLL = 0.05
# OAMSM_HEAT_WAIT polls the heater this often (seconds of reactor time).
HEAT_WAIT_POLL = 0.5
# Delays before each further forward-follower retry (a try follows each);
# the first try is immediate. Bounded: len + 1 tries in all.
FOLLOWER_RETRY_BACKOFF = (5.0, 15.0, 45.0)
STOPPED_TEXT = S.STOPPED_TEXT

# FPS change (as a fraction of full compression) that republishes a
# lane's pressure reading in the UI API v1 status, so ADC noise around one
# value does not resend the lanes on every poll.
PRESSURE_DEADBAND = 0.02

# A lane's host FPS reading and the loaded unit's own reading of the same
# buffer are sampled once a monitor tick while the lane sits loaded and idle.
# Consecutive samples where the two sum to about 1 while disagreeing well
# beyond noise mean the [fps] `reversed:` option is set the wrong way round for
# the sensor wired to that pin: the firmware loads correctly (it uses its own
# value) but every UI draws the pressure backwards.
FPS_ORIENTATION_SAMPLES = 10
FPS_ORIENTATION_SUM_TOLERANCE = 0.15
FPS_ORIENTATION_MIN_DELTA = 0.3

# Bambu's generic filament code by material, for a filament setting written
# without CODE= (OAMSM_SET_TRAY_INFO). It mirrors the code column of
# MATERIAL_DEFAULTS in moonraker/openams_spoolman/tray.py (Moonraker's process
# cannot share the module); keep the two equal.
GENERIC_FILAMENT_CODES = {
    "PLA": "GFL99", "PLA-CF": "GFL98", "PETG": "GFG99", "ABS": "GFB99",
    "ASA": "GFB98", "TPU": "GFU99", "PA": "GFN99", "PC": "GFC99",
    "PVA": "GFS99"}


def generic_filament_code(material):
    """The table's code for a material: the exact name, then the part before
    the first "-", "+" or space; else the generic PLA code."""
    name = str(material or "").strip().upper()
    if name in GENERIC_FILAMENT_CODES:
        return GENERIC_FILAMENT_CODES[name]
    for index, char in enumerate(name):
        if char in "-+ ":
            return GENERIC_FILAMENT_CODES.get(name[:index], "GFL99")
    return "GFL99"


# Latched firmware fault names the C2 §5.7 table classifies `pause`; every
# other latched fault (the table's `stop` row plus the unclassified ones,
# which withhold motion) is `stop`.
PAUSE_FIRMWARE_FAULTS = frozenset((
    "motion_timeout", "motion_stalled", "follower_buffer_invalid",
    "follower_filament_lost", "follower_starved", "follower_stalled",
    "follower_timeout", "lane_obstructed", "rfid_tag_error"))

# `lane_obstructed`: an idle lane whose hub sensor reads filament while its
# feeder inlet sensor does not. A continuous filament cannot read that way;
# the usual cause is a broken piece left in the lane, which the unit would
# otherwise try to retract without end.
LANE_OBSTRUCTED_TEXT = (
    "Bay %s: the hub sensor sees filament but the feeder inlet does not. "
    "Most likely a broken piece of filament is stuck in this lane; remove "
    "it, then clear the fault.")

# `rfid_tag_error`: the unit raised an error for a spool tag it rejected (a
# tag written by hand, without the maker's signature). The unit keeps
# reporting it after a clear; removing the tag and power-cycling the unit
# ended it.
RFID_TAG_ERROR_TEXT = (
    "RFID Tag Error: the unit rejected a spool's RFID tag. Remove the tag, "
    "power-cycle the unit, then clear the fault.")

# Latched firmware fault names the C2 §5.7 table classifies `info`: reported,
# cleared with clear_fault, and never a reason to withhold motion.
INFO_FIRMWARE_FAULTS = frozenset(("dryer_start_refused",))

# `dryer_start_refused`: the unit acknowledged a dryer start and kept
# reporting "off". Its heater never ran. An AMS 2 Pro does this while
# filament is loaded; a start is normally refused up front instead.
DRYER_START_REFUSED_TEXT = (
    "The dryer did not start. This unit does not dry while filament is "
    "loaded: unload it, clear the fault, then start the dryer again.")

# Lane messages that leave a lane fault entry (C2 §5.7, the lane `pause` row).
LANE_FAILURE_MESSAGES = frozenset((
    "load failed", "unload failed", "reload failed", "calibration failed",
    "no spare spool", "ptfe_length uncalibrated",
    "spare spool is not calibrated", "auto reload off"))

# C2 §4.9's D3 defaults, the policy switches every v2 status carries. H7
# reads each from the manager's own config section at startup and persists
# changes made by OAMSM_SET_OPTION (C3).
SETTING_DEFAULTS = {
    "read_tag_on_insertion": True, "read_tags_at_startup": True,
    "auto_reload_on_runout": True, "auto_create_spoolman_spools": True,
    "apply_pa_on_load": False, "sync_unit_tray_info": True}

# The openams_spoolman remote method OAMSM_SET_TRAY_INFO calls to write a
# Spoolman spool's filament into a unit (C3 §4, C4 §7.5): the host never
# talks to Spoolman itself.
TRAY_WRITE_REMOTE_METHOD = "openams_spoolman_tray_write"
# The nozzle range a written filament setting may name (C3 §4), the same
# range OAMSM_SET_BAY_FILAMENT accepts.
TRAY_TEMP_MIN_C = 150
TRAY_TEMP_MAX_C = 400

# HelixScreen UI API v1 (docs/UI_API.md): a small, versioned status/command
# contract for native UIs, independent of AFC and of any particular OAMS
# family. api_version is bumped on any meaning change of an existing field.
UI_API_VERSION = 1
UI_API_SCHEMA = "openams.manager"

# The four klipper-mmu ops an AMS unit has no counterpart for (C5 §7.1),
# each published as a per-unit OAMS_* command the UIs already address a unit
# by, and as the action token a UI offers it under (C2 §5.6). Only a
# klipper-mmu family has them, and `home` only where the family has a
# selector to move.
MMU_UNIT_OPS = ("preload", "unpreload", "eject", "home")

# The AMS families, whose bay motion actions follow what the unit reports it
# offers (a capability-limited unit offers fewer).
AMS_FAMILIES = ("ams1", "ams2", "ams_ht")
MMU_UNIT_COMMANDS = (("OAMS_PRELOAD", "preload"),
                     ("OAMS_UNPRELOAD", "unpreload"),
                     ("OAMS_EJECT", "eject"),
                     ("OAMS_HOME", "home"))
MMU_UNIT_COMMANDS_HELP = {
    "OAMS_PRELOAD": "Stage a bay's filament to the hub (klipper-mmu)",
    "OAMS_UNPRELOAD": "Release a bay's staged filament (klipper-mmu)",
    "OAMS_EJECT": "Eject a bay's filament out of the unit (klipper-mmu)",
    "OAMS_HOME": "Home the selector (klipper-mmu, selector builds only)"}


class OAMSManager(oams_ext.ManagerHooks):
    def __init__(self, config):
        self.config = config
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        # C2 §4.9's policy switches, read once from this section and changed
        # at runtime by OAMSM_SET_OPTION (C3), which persists them back here.
        self.settings = {
            name: config.getboolean(name, default)
            for name, default in SETTING_DEFAULTS.items()}
        self.reload_before = config.getfloat("reload_before_toolhead_distance", 0.0)
        # How often the runout/health monitor runs. Defaulted so it need not
        # appear in the config; lower = snappier runout reaction, more CPU.
        self.monitor_interval = config.getfloat(
            "monitor_interval", S.MONITOR_INTERVAL, above=0.0)
        self._ext_init(config)
        # H16: how long a load/unload waits for a unit to finish positioning a
        # freshly inserted spool before the reducer refuses it. Zero disables
        # the wait and leaves only the refusal.
        self.positioning_wait = config.getfloat("positioning_wait", 10.0,
                                                minval=0.)

        # UI-facing load/unload commands coordinate these printer-specific
        # extruder-only macros with the MCU-owned AMS motion. Keep them as bare
        # command names so arguments cannot accidentally inject another phase.
        self.toolhead_load_macro = self._macro_name(
            config, "toolhead_load_macro", "LOAD_FILAMENT")
        self.toolhead_unload_macro = self._macro_name(
            config, "toolhead_unload_macro", "UNLOAD_FILAMENT")
        # File that runtime writeback (group edits, calibration results) is
        # written to — the OpenAMS config holding the [oams ...] and
        # [filament_group ...] sections. Defaults to oams.cfg next to the main
        # printer config; SAVE_CONFIG can't reach an included subfile, so the
        # plugin edits this file in place.
        self.openams_config_path = os.path.expanduser(config.get(
            "openams_config_path", self._default_config_path()))

        self._build_topology()
        # openams_ui projections (H5), one printer object per unit. Built here
        # and re-checked at ready, once family discovery has settled bays.
        self._ui_projections = {}
        self._register_ui_projections()

        self.idle_timeout = None
        self.monitor_timer = None
        self._monitor_busy = False
        self._snapshot_refresh_due = {}
        self._snapshot_refresh_ok = {}
        self.ready = False
        self._startup_resync_pending = False
        # Lanes whose unknown follower the manager already tried to resolve
        # since this Klipper start (once per restart per lane).
        self._follower_rearm = {}
        self._follower_gave_up = set()
        self._failed_flows = set()
        self._print_was_active = False
        # UI API v1 (docs/UI_API.md): last published FPS pressure per lane,
        # so a reading is republished only once it moves by PRESSURE_DEADBAND.
        self._published_pressure = {}
        # Lanes whose running sequence the user asked to stop (openams/stop),
        # and the lanes that are inside an OAMSM_HEAT_WAIT right now.
        self._stop_requested = set()
        self._heat_waiting = set()
        # Consecutive inverse readings per lane, and the lanes already warned
        # about this Klipper session (_check_fps_orientation).
        self._fps_orientation_samples = {}
        self._fps_orientation_warned = set()

        # The store + effect executor. Each OAMS is bound so its firmware replies
        # become OpCompleted actions on its lane.
        self.runtime = Runtime(self.printer, list(self.fpss.keys()),
                               self.build_world, self.oam_by_idx.get)
        for oam in self.oams.values():
            oam.bind_runtime(self.runtime, oam.fps_name)

        webhooks = self.printer.lookup_object("webhooks")
        webhooks.register_endpoint("openams/status", self._webhook_status)
        webhooks.register_endpoint("openams/cancel_load", self._webhook_cancel_load)
        webhooks.register_endpoint("openams/stop", self._webhook_stop)
        webhooks.register_endpoint("openams/topology", self._webhook_topology)

        self.register_commands()
        self.printer.register_event_handler("klippy:ready", self.handle_ready)
        self.printer.register_event_handler("klippy:shutdown",
                                            self._handle_shutdown)
        self.printer.register_event_handler("klippy:disconnect",
                                            self._handle_disconnect)
        self.printer.register_event_handler("gcode:command_error",
                                            self._handle_command_error)

    def _handle_command_error(self):
        """C2 §5.3 rule 6: a G-code command raised, so the macro running a plan
        died without sending FAILED=1 and the progress bar would otherwise sit
        on the step until the stale-plan rule drops it. The error is
        attributed to the current step of the running macro plan, but only when
        exactly one lane has such a plan: with none or several, guessing would
        fail the wrong one. A lane whose user asked to stop takes the stop
        effect first (_apply_stop), which fails its own step whatever the other
        lanes are doing. Never raises -- an event handler must not."""
        try:
            for fps in sorted(self._stop_requested):
                self._apply_stop(fps)
            lanes = self.runtime.get_state().lanes
            running = [fps for fps, lane in lanes.items()
                       if lane.stage_index is not None
                       and S._stage_macro_active(lane)]
            if len(running) != 1:
                return
            self.runtime.dispatch(S.StageFail(running[0]))
            self._note_flow_failure(running[0])
        except Exception:
            pass

    # ------------------------------------------------- failed-flow follower

    def _note_flow_failure(self, fps):
        """An OpenAMS flow on `fps` ended without DONE (a G-code error inside
        the plan, a stage reported failed, or the user's Stop). Klipper macros
        have no try/finally, so nothing else stops the follower the flow armed
        (a failed unload leaves the reverse follower running). Record it and
        stop it as soon as that is safe. Never raises."""
        try:
            self.__dict__.setdefault("_failed_flows", set()).add(fps)
            self._resolve_failed_flows()
        except Exception:
            logging.exception("OAMS: could not stop the follower of the"
                              " failed flow on %s", fps)

    def _resolve_failed_flows(self):
        """Stop the follower of every lane whose flow failed, once the lane is
        quiet. Deferred (kept pending) while a print job runs, so a follower is
        never stopped under a print that is still going, and while a firmware
        operation is still in flight (the unit manages its own follower then).
        A new plan on the lane makes the record stale and drops it."""
        pending = self.__dict__.get("_failed_flows")
        if not pending:
            return
        get_state = getattr(self.runtime, "get_state", None)
        if get_state is None:
            return
        printing = self._print_job_active(self.reactor.monotonic())
        for fps in sorted(pending):
            lane = get_state().lanes.get(fps)
            if lane is None or lane.following is False:
                pending.discard(fps)
                continue
            if S._stage_macro_active(lane) and lane.stage_index is not None:
                pending.discard(fps)         # a newer flow owns the follower
                continue
            if printing or lane.op in (S.OP_LOADING, S.OP_UNLOADING):
                continue
            pending.discard(fps)
            logging.warning("OAMS: lane %s: a flow failed; stopping its"
                            " follower", fps)
            self._stop_followers([fps])

    # --------------------------------------------------------------- topology

    @staticmethod
    def _macro_name(config, option, default):
        name = config.get(option, default).strip().upper()
        if (not name or name[0] not in "_ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                or not name.replace("_", "").isalnum()
                or not name.isascii() or name.startswith("OAMSM_")):
            raise config.error(
                "%s must name one non-OAMSM G-code macro (letters, digits,"
                " underscores only)" % option)
        return name

    def _default_config_path(self):
        """oams.cfg next to the main printer config, derived from Klipper's
        start args so it works regardless of the user's config directory."""
        try:
            main = self.printer.get_start_args().get("config_file")
        except Exception:
            main = None
        if main:
            return os.path.join(os.path.dirname(main), "oams.cfg")
        return "~/printer_data/config/oams.cfg"

    def _force_load_sections(self):
        """Instantiate every fps / oams / filament_group section up front so
        topology validation does not depend on Klipper's config load order.
        load_object is idempotent (returns the already-loaded object), and
        filament_group no longer cross-references OAMS at load time, so the
        order we load these in does not matter."""
        for section in self.config.get_prefix_sections(''):
            name = section.get_name()
            if (name == "fps" or name.startswith("fps ")
                    or name.startswith("oams ")
                    or name.startswith("klipper_mmu ")
                    or name.startswith("follower ")
                    or name == "filament_group"
                    or name.startswith("filament_group ")):
                self.printer.load_object(self.config, name)

    def _build_topology(self):
        self._force_load_sections()

        # Klipper object maps (objects can't live in the pure model). self.oams
        # is really "units by short name": OAMS units AND inline followers
        # present the same driver interface, so everything downstream (idx
        # maps, world builder, effects) treats them uniformly.
        self.fpss = {fps.fps_name: fps
                     for _name, fps in self.printer.lookup_objects(module="fps")}
        self.oams = {full.split()[-1]: oam
                     for full, oam in self.printer.lookup_objects(module="oams")}
        for full, observer in self.printer.lookup_objects(module="klipper_mmu"):
            unit = observer.get_openams_unit()
            if unit is not None:
                # The observer owns transport; only its explicitly configured
                # OpenAMS unit belongs in the shared topology/runtime.
                short = full.split()[-1]
                if short in self.oams:
                    raise self.config.error(
                        "OpenAMS configuration error: Duplicate unit name '%s'"
                        % short)
                self.oams[short] = unit
        followers = {full.split()[-1]: f
                     for full, f in self.printer.lookup_objects(
                         module="follower")}
        self._groups = {full.split()[-1]: g
                        for full, g in self.printer.lookup_objects(
                            module="filament_group")}

        # Validate all relationships in the pure model (raises with a clear,
        # user-facing message that we surface as a Klipper config error).
        oams_specs = [
            T.OamsSpec(name=short, idx=oam.oams_idx, fps=oam.fps_name,
                       family=getattr(oam, "device_family", "ams1"),
                       bays=getattr(oam, "bay_count", 4))
            for short, oam in self.oams.items()]
        oams_specs += [T.OamsSpec(name=short, idx=f.oams_idx, fps=f.fps_name,
                                  kind="follower")
                       for short, f in followers.items()]
        self.oams.update(followers)
        group_specs = [(short, list(g.bay_specs))
                       for short, g in self._groups.items()]
        try:
            self.topo = T.build_topology(list(self.fpss.keys()),
                                         oams_specs, group_specs)
        except T.TopologyError as e:
            raise self.config.error("OpenAMS configuration error: %s" % (e,))

        # Tell each OAMS its resolved lane (used by bind_runtime and requests).
        for short, oam in self.oams.items():
            oam.fps_name = self.topo.lane_of_oams(short)
        self._rebuild_derived()

    def _rebuild_derived(self):
        """(Re)compute the object-keyed maps the runtime/world/handlers use from
        the current topology. Called at build and after every runtime edit."""
        self.unit_name_by_idx = {
            oam.oams_idx: name for name, oam in self.oams.items()}
        by_idx = {oam.oams_idx: oam for oam in self.oams.values()}
        if hasattr(self, "oam_by_idx"):
            # Runtime holds the bound `dict.get` created during initial
            # construction. Keep that dictionary alive when firmware family
            # discovery rebuilds topology at ready.
            self.oam_by_idx.clear()
            self.oam_by_idx.update(by_idx)
        else:
            self.oam_by_idx = by_idx
        self.lane_oams = {lane: [self.oams[n] for n in self.topo.oams_on_lane(lane)]
                          for lane in self.topo.fps_names}
        self.group_lane = {}
        self.groups_by_lane = {lane: {} for lane in self.topo.fps_names}
        for gname in self.topo.groups:
            lane = self.topo.lane_of_group(gname)
            if lane is None:
                continue  # empty group (created at runtime, not yet populated)
            self.group_lane[gname] = lane
            self.groups_by_lane[lane][gname] = self.topo.group_bays_idx(gname)

    def _register_ui_projections(self):
        """Publish one "openams_ui <unit>" object per unit (H5). Idempotent:
        called at build and again at ready, when auto-detected bays may have
        added units. A printer without add_object (unit tests) simply gets no
        projections."""
        add = getattr(self.printer, "add_object", None)
        if add is None:
            return
        for name in self._ordered_unit_names():
            if name in self._ui_projections:
                continue
            try:
                add("openams_ui %s" % name, OpenAMSUIProjection(self, name))
            except Exception as e:
                # A duplicate object name means another instance already
                # published it; anything else is worth a line in the log.
                logging.debug("OAMS: openams_ui %s not registered: %s",
                              name, e)
                continue
            self._ui_projections[name] = True

    # ------------------------------------------------------------- world build

    def _is_printing(self, now):
        if self.idle_timeout is None:
            return False
        return self.idle_timeout.get_status(now)["state"] == "Printing"

    def _print_job_active(self, now):
        """True only while a print job runs (``print_stats.state`` is
        "printing"). Unlike `_is_printing`, which follows idle_timeout and is
        also "Printing" for the duration of any G-code (an unload's homing),
        this is false for a flow the user or a UI started outside a print.
        Runout detection and the print-time follower arming use it."""
        try:
            stats = self.printer.lookup_object("print_stats", None)
            if stats is None:
                return False
            return stats.get_status(now).get("state") == "printing"
        except Exception:
            return False

    def _settings(self):
        """The C2 §4.9 settings dict. A manager built without __init__ (status
        fixtures) reports the D3 defaults."""
        return getattr(self, "settings", SETTING_DEFAULTS)

    def build_world(self, now):
        unavailable = [name for name, oam in self.oams.items()
                       if not getattr(oam, "hardware_snapshot_ready", True)]
        if unavailable:
            raise RuntimeError("OpenAMS hardware snapshot unavailable for %s"
                               % ", ".join(sorted(unavailable)))
        # LaneWorld.printing drives runout detection only, so it is a real
        # print job, not "any G-code is running" (idle_timeout "Printing").
        printing = self._print_job_active(now)
        lanes = {}
        for fps_name, fps in self.fpss.items():
            loaded, ready, path_len, reserved, calibrated = {}, {}, {}, {}, {}
            motion_blocked = {}
            positioning = {}
            for oam in self.lane_oams[fps_name]:
                short = self.unit_name_by_idx[oam.oams_idx]
                bay_count = self.topo.bays_of(short)
                local_lane = getattr(oam, "local_reserved_lane", None)
                if (local_lane is not None
                        and (type(local_lane) is not int
                             or not 0 <= local_lane < bay_count)):
                    raise RuntimeError("OpenAMS invalid local reservation for %s"
                                       % short)
                # H10: the reducer must refuse a load on a bay whose PTFE
                # length is unusable before any command reaches the unit, so
                # the world snapshot marks those bays uncalibrated. The
                # effective length is what the driver would send (a per-bay
                # override, else the shared ptfe_length); a unit that has no
                # such notion (e.g. a follower) is left out of the map, which
                # the reducer treats as usable.
                ptfe_for_bay = getattr(oam, "_ptfe_length_for_bay", None)
                for bay in range(bay_count):
                    key = (oam.oams_idx, bay)
                    loaded[key] = bool(oam.hub_hes_value[bay])
                    ready[key] = oam.bay_loadable(bay)
                    if ptfe_for_bay is not None:
                        value = ptfe_for_bay(bay)
                        calibrated[key] = value is not None and value > 0
                path_len[oam.oams_idx] = oam.path_length_mm
                # H15: a unit that may not move filament is recorded so the
                # reducer refuses loads on its bays.
                reason = ams_family.motion_block_reason(
                    getattr(oam, "capabilities", None) or {})
                if reason is None:
                    reason = self._arrival_block_reason(oam, fps_name)
                if reason is not None:
                    motion_blocked[oam.oams_idx] = reason
                # H16: while a unit positions a newly inserted spool it
                # answers BUSY to every motion command, so record it here.
                bays_fn = getattr(oam, "positioning_bays", None)
                if callable(bays_fn):
                    positioning_bays = bays_fn()
                    if positioning_bays:
                        positioning[oam.oams_idx] = frozenset(positioning_bays)
                if local_lane is not None:
                    reserved[oam.oams_idx] = local_lane
            epos = fps.extruder.last_position if fps.extruder is not None else 0.0
            lanes[fps_name] = S.LaneWorld(
                extruder_pos=epos, printing=printing, loaded=loaded, ready=ready,
                calibrated=calibrated, reserved=reserved,
                motion_blocked=motion_blocked, positioning=positioning,
                group_bays=self.groups_by_lane.get(fps_name, {}),
                path_len=path_len, reload_before=self.reload_before,
                auto_reload=self._settings()["auto_reload_on_runout"],
                unit_names=dict(self.unit_name_by_idx))
        return S.World(lanes=lanes)

    def unit_runout_active(self, oams_idx):
        """C2 §4.6: is a lane holding this unit mid-runout right now?

        The RFID reader service (``src/oams_tags.py``) keeps a bay's tag on a
        ``ready`` fall while its lane is reloading (a runout is not a
        removal, C4 §3); it clears the tag once the fall happens with no
        runout in progress.
        """
        for lane in self.runtime.get_state().lanes.values():
            if lane.unit is not None and lane.unit[0] == oams_idx:
                if lane.runout != S.RUNOUT_IDLE:
                    return True
        return False

    def bay_in_use(self, oams_idx, bay):
        """Is this exact bay the one a lane has loaded, loading or unloading?

        At the instant an F1S fall is a runout, the
        reducer has not yet recorded ``runout`` on the same frame the driver
        sees the fall on (``unit_runout_active`` is briefly, wrongly, False).
        The filament being threaded through this bay's hub *right now* is
        the deterministic fact that does not race: while it holds, the spool
        cannot have been pulled, whatever the runout sub-state says.
        """
        target = (oams_idx, bay)
        for lane in self.runtime.get_state().lanes.values():
            if lane.unit == target and lane.op in (
                    S.OP_LOADED, S.OP_LOADING, S.OP_UNLOADING):
                return True
        return False

    def unit_session_lost(self, oams_idx, reason):
        # Called on the reactor thread when a unit's host-communication
        # watchdog fires. Settle every lane that was waiting on that unit, with
        # the reason; the reducer ignores the lanes that don't involve it.
        printing = self._is_printing(self.reactor.monotonic())
        for fps in self.runtime.get_state().lanes:
            self.runtime.dispatch(
                S.UnitSessionLost(fps, oams_idx, reason, printing))

    def _check_unit_silence(self, now):
        """C1 §6.2: a unit that stops sending its stats frames is unreachable.

        Silence longer than that unit's host watchdog plus a slack second
        settles its in-flight work and stops the host operating it, until its
        frames return. A relaying firmware that reports its device's link
        (C1 §7.2 AMS_CAP_LINK_STATUS) as down is unreachable the same way,
        at once and without waiting for any silence.
        """
        for name, oam in self.oams.items():
            watchdog = getattr(oam, "host_watchdog_ms", None)
            last = getattr(oam, "last_stats_time", None)
            silent = (watchdog is not None and last is not None
                      and now - last > (watchdog + 1000) / 1000.0)
            link_down = getattr(oam, "link_reachable", None) is False
            if (silent or link_down) and getattr(
                    oam, "unreachable", None) is None:
                if silent:
                    oam.unreachable = (
                        "no data from %s for %.1f s; check its connection"
                        % (name, now - last))
                else:
                    oam.unreachable = (
                        "the device behind %s is not reachable; check its"
                        " connection" % name)
                oam.capabilities["unreachable"] = oam.unreachable
                (logging.error if silent else logging.warning)(
                    "OAMS %s: %s", name, oam.unreachable)
                self.unit_session_lost(oam.oams_idx, oam.unreachable)
            elif (not silent and not link_down
                    and getattr(oam, "unreachable", None) is not None):
                oam.unreachable = None
                oam.capabilities.pop("unreachable", None)
                logging.info("OAMS %s: reachable again", name)

    def check_unit_reachability(self, now):
        """Run the reachability rule now, e.g. when a unit reports its link
        state changed, instead of at the next monitor tick."""
        self._check_unit_silence(now)

    def _check_fps_orientation(self, now):
        """Warn when a lane's host FPS reading looks like the inverse of the
        loaded unit's own reading of the same buffer.

        Read-only: it changes no reading, no lane state and no behavior. It
        only counts samples and warns once per Klipper session per lane, so a
        user who wired a sensor the wrong way round is told instead of getting
        a backwards pressure bar in every UI. `now` keeps the call uniform with
        the other monitor checks; nothing here is time dependent.
        """
        samples = getattr(self, "_fps_orientation_samples", None)
        if samples is None:
            samples = self._fps_orientation_samples = {}
        warned = getattr(self, "_fps_orientation_warned", None)
        if warned is None:
            warned = self._fps_orientation_warned = set()
        get_state = getattr(self.runtime, "get_state", None)
        if get_state is None:
            return  # a manager built without a runtime (status fixtures)
        lanes = get_state().lanes
        for fps_name, fps in self.fpss.items():
            # A lane reading its own pin is the only one that can disagree
            # with a unit; a lane with source_oams already reports the unit's
            # value, so there is nothing to compare.
            if getattr(fps, "source_oams_name", None) is not None:
                continue
            lane = lanes.get(fps_name)
            if lane is None or lane.op != S.OP_LOADED or lane.unit is None:
                samples.pop(fps_name, None)
                continue
            oam = self.oam_by_idx.get(lane.unit[0])
            b = getattr(oam, "fps_value", None) if oam is not None else None
            a = self._fps_status_value(fps)
            if not isinstance(a, (int, float)) or not isinstance(
                    b, (int, float)):
                samples.pop(fps_name, None)
                continue
            inverted = (abs(a + b - 1) <= FPS_ORIENTATION_SUM_TOLERANCE
                        and abs(a - b) >= FPS_ORIENTATION_MIN_DELTA)
            if not inverted:
                samples.pop(fps_name, None)
                continue
            count = samples.get(fps_name, 0) + 1
            samples[fps_name] = count
            if count >= FPS_ORIENTATION_SAMPLES and fps_name not in warned:
                warned.add(fps_name)
                self._warn_fps_orientation(fps_name, oam, a, b)

    def _warn_fps_orientation(self, fps_name, oam, host_value, unit_value):
        """One log line and one console message for a lane whose host FPS
        reading looks reversed, naming the lane and unit and what to change."""
        unit_name = self.unit_name_by_idx.get(oam.oams_idx, oam.oams_idx)
        message = ("[fps] '%s' reads %.2f while %s reads its own pressure as"
                   " %.2f: the host reading looks reversed. Set 'reversed:"
                   " true' in [fps] (or remove it if it is set)."
                   % (fps_name, host_value, unit_name, unit_value))
        logging.warning("OAMS: %s", message)
        self.printer.lookup_object("gcode").respond_info(message)

    # ----------------------------------------------------------------- ready

    def _handle_shutdown(self):
        runtime = getattr(self, "runtime", None)
        if runtime is not None:
            runtime.end_session("Klipper shut down")

    def _handle_disconnect(self):
        runtime = getattr(self, "runtime", None)
        if runtime is not None:
            runtime.end_session("Klipper disconnected")

    def handle_ready(self):
        # Auto family detection happens at MCU connect; revalidate groups and
        # rebuild the derived maps now that each firmware bay count is known.
        self._build_topology()
        self.idle_timeout = self.printer.lookup_object("idle_timeout")
        # The firmware owns per-op liveness only if EVERY bound unit guarantees
        # it (protocol >= 3); otherwise the host keeps its own op deadline as
        # the conservative choice. Versions are resolved at connect, so this is
        # known by ready.
        owns = bool(self.oams) and all(
            oam.firmware_owns_liveness for oam in self.oams.values())
        self.runtime.set_firmware_liveness(owns)
        logging.info("OAMS: firmware owns per-op liveness: %s", owns)
        # Do not infer an empty lane from zero-initialized sensor mirrors. The
        # monitor performs one resync after each unit publishes a real hardware
        # snapshot; until then the manager reports not-ready and takes no lane
        # decisions.
        self._startup_resync_pending = True
        self.ready = False
        self._follower_rearm = {}
        self._follower_gave_up = set()
        # Family detection may have added units; publish a projection for any
        # unit that appeared since construction.
        self._register_ui_projections()
        self.start_monitor()

    def _finish_startup_resync_if_ready(self):
        if not self._startup_resync_pending:
            return True
        # A read-only unit publishes a real physical snapshot but never takes
        # motion, so only the units whose motion can still return hold the
        # world back.
        if self._motion_pending_units():
            return False
        if not all(getattr(oam, "hardware_snapshot_ready", True)
                   for oam in self.oams.values()):
            return False
        self.runtime.dispatch(S.ClearErrors(keep_in_flight=True))
        self._startup_resync_pending = False
        self.ready = True
        logging.info("OAMS: initial hardware snapshot resync complete")
        return True

    def start_monitor(self):
        self.stop_monitor()
        self.monitor_timer = self.reactor.register_timer(
            self._monitor, self.reactor.NOW)
        for name, oam in self.oams.items():
            reason = self._arrival_block_reason(
                oam, self.topo.lane_of_oams(name))
            if reason is not None:
                logging.warning("OAMS %s: %s; it cannot load or auto-reload",
                                name, reason)
        # Host-confirmed arrival needs a faster look than the monitor's tick,
        # but only when some unit asks for it.
        if any(getattr(oam, "needs_host_arrival", False)
               for oam in self.oams.values()):
            self._arrival_timer = self.reactor.register_timer(
                self._arrival_tick, self.reactor.NOW)
        logging.info("OAMS: monitor started")

    # ------------------------------------------- host-confirmed arrival (C1)

    ARRIVAL_POLL = 0.1
    ARRIVAL_NEAR_MARGIN = 0.05

    def _arrival_signal(self, fps_name):
        """Whether the lane has a toolhead-side arrival signal: its own
        `pin`, or a `source_oams` unit that advertises AMS_CAP_FPS_TOOLHEAD
        (its relayed `fps_value` is a toolhead-side FPS/buffer). A
        `source_oams` value is otherwise the unit's own buffer telemetry and
        is no arrival signal."""
        fps = self.fpss.get(fps_name)
        if fps is None:
            return False
        source_name = getattr(fps, "source_oams_name", None)
        if source_name is None:
            return getattr(fps, "_pin", None) is not None
        source = getattr(fps, "source_oams", None)
        if source is None:
            short = source_name.split()[-1]
            source = self.oams.get(short)
        capabilities = getattr(source, "capabilities", None) or {}
        return bool(capabilities.get("fps_toolhead"))

    def _arrival_block_reason(self, oam, fps_name):
        """Why a unit that needs host arrival confirmation cannot be loaded
        on this lane, or None. Asked before a load, so it is refused at once
        rather than timing out."""
        if not getattr(oam, "needs_host_arrival", False):
            return None
        if getattr(oam, "oams_load_arrived_cmd", True) is None:
            return ("The unit needs host arrival confirmation but its firmware"
                    " has no oams_cmd_load_arrived")
        if not self._arrival_signal(fps_name):
            return ("This unit needs the host to confirm toolhead arrival,"
                    " but lane %s has no arrival signal; give [fps %s] a"
                    " 'pin' that reads the toolhead side" % (fps_name,
                                                             fps_name))
        return None

    def _arrival_tick(self, eventtime):
        try:
            self._check_host_arrival(eventtime)
        except Exception:
            logging.exception("OpenAMS: host arrival check failed")
        return eventtime + self.ARRIVAL_POLL

    def _check_host_arrival(self, now):
        """For a lane that is loading a unit needing host confirmation (a load,
        or a runout reload onto it): on the
        lane's arrival signal (its own pin, or a `source_oams` value from a
        unit reporting AMS_CAP_FPS_TOOLHEAD) rising through `arrival_threshold` and holding
        for `arrival_debounce`, send the arrival acknowledgement once for the
        load's generation. A generation change, any other lane state, or a
        reading already above the threshold when the load began (never seen
        below it) sends nothing."""
        track = getattr(self, "_arrival_track", None)
        if track is None:
            track = self._arrival_track = {}
        lanes = self.runtime.get_state().lanes
        for fps_name, fps in self.fpss.items():
            lane = lanes.get(fps_name)
            # The unit being loaded: the lane's own for a load, the reload
            # target for an automatic runout reload (the lane stays LOADED
            # while its next spool loads). Anything else, an unload included,
            # sends nothing.
            if lane is not None and lane.op == S.OP_LOADING:
                target = lane.unit
            elif (lane is not None and lane.op == S.OP_LOADED
                    and lane.runout == S.RUNOUT_LOADING):
                target = lane.reload_target
            else:
                target = None
            if target is None:
                self._arrival_drop(track, fps_name, now)
                continue
            oam = self.oam_by_idx.get(target[0])
            if (not getattr(oam, "needs_host_arrival", False)
                    or not self._arrival_signal(fps_name)):
                self._arrival_drop(track, fps_name, now)
                continue
            value = self._fps_status_value(fps)
            if not isinstance(value, (int, float)):
                continue
            entry = track.get(fps_name)
            if entry is None or entry["gen"] != lane.op_gen:
                self._arrival_drop(track, fps_name, now)
                entry = track[fps_name] = {
                    "gen": lane.op_gen, "armed": False, "since": None,
                    "sent": False, "start": now, "last": now, "near": 0.0,
                    "peak": value}
            if entry["sent"]:
                continue
            threshold = getattr(fps, "arrival_threshold", 0.65)
            if threshold - self.ARRIVAL_NEAR_MARGIN <= value < threshold:
                entry["near"] += now - entry["last"]
            entry["last"] = now
            entry["peak"] = max(entry["peak"], value)
            if value < threshold:
                entry["armed"] = True
                entry["since"] = None
                continue
            if not entry["armed"]:
                continue
            if entry["since"] is None:
                entry["since"] = now
            if now - entry["since"] >= getattr(fps, "arrival_debounce", 0.3):
                entry["sent"] = True
                oam.confirm_load_arrived(lane.op_gen)

    def _arrival_drop(self, track, fps_name, now):
        """Forget a lane's arrival tracking. A load that ended unconfirmed
        while the reading sat just under the threshold for most of the wait
        names the setting to lower."""
        entry = track.pop(fps_name, None)
        if entry is None or entry["sent"]:
            return
        elapsed = now - entry["start"]
        if elapsed < 2.0 or entry["near"] < 0.5 * elapsed:
            return
        fps = self.fpss.get(fps_name)
        logging.warning(
            "OpenAMS: load on %s was not confirmed as arrived: the FPS "
            "reading stayed within %.2f below arrival_threshold %.2f for "
            "%.0f of %.0f s (peak %.2f). The sensor likely tops out below "
            "the threshold; lower [fps %s] arrival_threshold under %.2f.",
            fps_name, self.ARRIVAL_NEAR_MARGIN,
            getattr(fps, "arrival_threshold", 0.65), entry["near"], elapsed,
            entry["peak"], fps_name, entry["peak"])

    def stop_monitor(self):
        timer = getattr(self, "_arrival_timer", None)
        if timer is not None:
            self.reactor.unregister_timer(timer)
            self._arrival_timer = None
        if self.monitor_timer is not None:
            self.reactor.unregister_timer(self.monitor_timer)
            self.monitor_timer = None

    def _monitor(self, eventtime):
        # An opt-in capture may wait on Klipper's reactor. Nested timer
        # dispatch must not start another capture or run a stale world tick.
        if getattr(self, "_monitor_busy", False):
            return eventtime + self.monitor_interval
        self._monitor_busy = True
        try:
            # H9d: a monitor tick late by more than half a unit's host
            # watchdog means the host itself stalled (C1 §6.2), so that
            # unit's watchdog may have fired while nothing was watching.
            previous = getattr(self, "_last_monitor_time", None)
            if previous is not None:
                gap = eventtime - previous
                watchdogs = [getattr(oam, "host_watchdog_ms", None)
                             for oam in self.oams.values()]
                watchdogs = [ms for ms in watchdogs if ms is not None]
                if watchdogs and gap > min(watchdogs) / 2000.0:
                    logging.warning(
                        "OpenAMS: the host stalled for %.1f s; a unit's host"
                        " watchdog may have fired", gap)
            self._last_monitor_time = eventtime
            self._check_unit_silence(eventtime)
            refresh_ok = True
            has_refresh = False
            due = self._snapshot_refresh_due = getattr(
                self, "_snapshot_refresh_due", {})
            results = self._snapshot_refresh_ok = getattr(
                self, "_snapshot_refresh_ok", {})
            for name, oam in self.oams.items():
                refresh = getattr(oam, "refresh", None)
                if not callable(refresh):
                    continue
                has_refresh = True
                if eventtime >= due.get(name, float("-inf")):
                    try:
                        results[name] = bool(refresh())
                    except Exception:
                        results[name] = False
                        logging.exception("OAMS: physical refresh failed for %s",
                                          name)
                    # Time from completion, so a slow/failed query cannot
                    # immediately launch another transport transaction.
                    due[name] = self.reactor.monotonic() + max(
                        1.0, self.monitor_interval)
                refresh_ok = refresh_ok and results.get(name, False)
            # A capture may have waited on the reactor. Schedule from its
            # completion and pass current time to later decisions; legacy
            # units retain the callback's original event time.
            now = self.reactor.monotonic() if has_refresh else eventtime
            if self._motion_pending_units():
                self._startup_resync_pending = True
                self.ready = False
                return now + self.monitor_interval
            # A connected MMU can lose its snapshot after startup (for example
            # on MCU disconnect). Do not build a world from invalid mirrors.
            # Reuse the startup resync when every unit has a fresh snapshot.
            if not refresh_ok or not all(
                    getattr(oam, "hardware_snapshot_ready", True)
                    for oam in self.oams.values()):
                self._startup_resync_pending = True
                self.ready = False
                return now + self.monitor_interval
            if not self._finish_startup_resync_if_ready():
                return now + self.monitor_interval
            self.runtime.tick()
            self._resolve_failed_flows()
            self._arm_followers_for_print(now)
            self._reestablish_followers_after_restart()
            self._check_fps_orientation(now)
            self._ext_monitor(now)
        except Exception:
            logging.exception("OAMS: monitor tick failed")
        finally:
            self._monitor_busy = False
        return ((self.reactor.monotonic() if has_refresh else eventtime)
                + self.monitor_interval)

    # --------------------------------------------------------------- status

    def _ordered_unit_names(self):
        return sorted(self.oams, key=lambda n: (self.oams[n].oams_idx, n))

    def _slot_entries(self):
        """[(slot id, unit short name, bay)] in stable global order: units
        ordered by (oams_idx, name), bays 0..bay_count-1. UI API v1
        (docs/UI_API.md) commands address a bay by this manager-assigned,
        snapshot-stable integer id."""
        entries = []
        slot_id = 0
        for name in self._ordered_unit_names():
            for bay in range(self.topo.bays_of(name)):
                entries.append((slot_id, name, bay))
                slot_id += 1
        return entries

    def _slot_id_map(self):
        return {(name, bay): slot_id for slot_id, name, bay in self._slot_entries()}

    def _slot_id_by_unit(self):
        """Like _slot_id_map, but keyed by (oams_idx, bay) -- the shape
        LaneState.unit uses -- rather than (unit short name, bay)."""
        return {(self.oams[name].oams_idx, bay): slot_id
                for slot_id, name, bay in self._slot_entries()}

    def _slot_lookup(self, slot_id):
        """(unit short name, bay) for a UI API v1 slot id, or None."""
        for sid, name, bay in self._slot_entries():
            if sid == slot_id:
                return name, bay
        return None

    def _ui_topology_kind(self, short_name):
        """The hardware-neutral rendering/operation shape UI API v1 calls
        'topology' (hub/linear/parallel/mixed; docs/UI_API.md 'Model'). An OAMS
        unit feeds several bays into one shared buffer: a hub. An inline
        [follower] is a single bay in series with the toolhead, not a hub of
        bays, so it is reported as linear. A client that meets a topology
        value it does not know may still draw the unit as a hub (slots are
        addressed by id), so this is a safe, additive choice either way."""
        return "linear" if self.topo.kind_of(short_name) == "follower" else "hub"

    def _ui_commands(self):
        """{action: command name} for only the UI API v1 actions whose G-code
        command is actually registered (docs/UI_API.md 'commands'). Load and
        unload are oams_macros.cfg's OPENAMS_LOAD and OPENAMS_UNLOAD, so a
        screen's buttons run the toolchange T<n> runs (cut, retract, feed,
        CLEAN_NOZZLE, the printer's hooks and OAMS_CUT). Without that file
        they fall back to the manager's toolhead-coordinating commands, which
        run only toolhead_load_macro/toolhead_unload_macro around the AMS."""
        available = self.printer.lookup_object("gcode").get_status(
            self.reactor.monotonic())["commands"]
        commands = {}
        for key, candidates in (
                ("load", ("OPENAMS_LOAD", "OAMSM_LOAD_TO_TOOLHEAD")),
                ("unload", ("OPENAMS_UNLOAD", "OAMSM_UNLOAD_FROM_TOOLHEAD")),
                ("cancel", ("OAMSM_LOAD_FILAMENT_CANCEL",)),
                ("reset", ("OAMSM_CLEAR_ERRORS",))):
            for command in candidates:
                if command in available:
                    commands[key] = command
                    break
        return commands

    def _ui_units(self):
        slot_ids = self._slot_id_map()
        units = []
        lanes_now = self.runtime.get_state().lanes
        for name in self._ordered_unit_names():
            lane_state = lanes_now.get(self.topo.lane_of_oams(name))
            oam = self.oams[name]
            slots = [{"id": slot_ids[(name, bay)], "bay": bay,
                     "ready": bool(oam.bay_loadable(bay)),
                     "loaded": bool(oam.hub_hes_value[bay])
                     or self._flow_holds_bay(
                         lane_state, oam.oams_idx, bay)}
                    for bay in range(self.topo.bays_of(name))]
            units.append({
                "id": str(oam.oams_idx), "name": name,
                "kind": self.topo.kind_of(name),
                "topology": self._ui_topology_kind(name),
                "lane": self.topo.lane_of_oams(name),
                "connected": bool(getattr(oam, "connected", True)),
                "slots": slots})
        return units

    def _ui_groups(self):
        slot_ids = self._slot_id_map()
        world = self._status_world()
        groups = []
        for gname in sorted(self.topo.groups):
            slots = [slot_ids[entry] for entry in self.topo.groups[gname]
                     if entry in slot_ids]
            lane = self.group_lane.get(gname)
            next_bay = self._group_next_bay(world, gname, lane)
            groups.append({"name": gname, "lane": lane, "slots": slots,
                           # The bay a load of this group would take now, and
                           # the temperature it holds, so a load can heat to
                           # it before the filament moves (C2 §5.6).
                           "next_bay": (list(next_bay)
                                        if next_bay is not None else None),
                           "next_filament_temp_c":
                               self._bay_filament_temp_c(next_bay)})
        return groups

    def _status_world(self):
        """The lane world a load is reduced against, or None when it cannot
        be built (a snapshot not ready, a status fixture). Never raises: the
        status is published on every poll."""
        try:
            return self.build_world(self.reactor.monotonic())
        except Exception:
            return None

    def _group_next_bay(self, world, group, lane):
        """The bay a load of `group` would take now, or None.

        The reducer's own selection (`oams_state._group_candidates` over the
        same world a load gets), so a UI and the command it mirrors can never
        disagree about which bay is next.
        """
        if world is None:
            return None
        lw = world.lanes.get(lane)
        if lw is None:
            return None
        for unit in S._group_candidates(lw, group):
            if lw.ready.get(unit):
                return unit
        return None

    def _lane_pressure(self, fps_name):
        """FPS compression for lane `fps_name`, 0.0 (no pressure) to 1.0
        (fully compressed). Republished only once it moves by
        PRESSURE_DEADBAND, so ADC noise around one value does not resend the
        lanes on every status poll (docs/UI_API.md 'Model')."""
        fps = self.fpss.get(fps_name)
        value = self._fps_status_value(fps) if fps is not None else None
        if value is None:
            return None
        published = self._published_pressure.get(fps_name)
        if published is None or abs(value - published) >= PRESSURE_DEADBAND:
            published = value
            self._published_pressure[fps_name] = published
        return round(min(max(published, 0.0), 1.0), 2)

    def _lane_regulators(self, fps_name, lane):
        """The units whose hub motor regulates the lane's pressure: the loaded
        unit if one is loaded, else every unit on the lane, in order."""
        if lane.unit is not None and lane.unit[0] in self.oam_by_idx:
            return [self.oam_by_idx[lane.unit[0]]]
        return self.lane_oams.get(fps_name, ())

    def _lane_set_point(self, fps_name, lane):
        """The compression the feeding unit's hub motor regulates to: the
        loaded unit's if one is loaded, else the first unit on the lane."""
        for oam in self._lane_regulators(fps_name, lane):
            target = getattr(oam, "fps_target", None)
            if target is not None:
                return round(float(target), 2)
        return None

    def _lane_pressure_band(self, fps_name, lane):
        """[low, high]: the band the same unit's regulator keeps the pressure
        in (its fps_lower_threshold and fps_upper_threshold), or None when
        no unit reports a valid one. Informational: a reading outside it is
        not a fault by itself."""
        for oam in self._lane_regulators(fps_name, lane):
            low = getattr(oam, "fps_lower_threshold", None)
            high = getattr(oam, "fps_upper_threshold", None)
            if low is None or high is None:
                continue
            low, high = round(float(low), 2), round(float(high), 2)
            if 0.0 <= low < high <= 1.0:
                return [low, high]
        return None

    def _lane_status(self, fps_name, lane):
        status = self._lane_status_base(fps_name, lane)
        # Additive and present only while `following` is null, so every other
        # lane's published shape is unchanged.
        reason = self._follower_unknown_reason(lane)
        if reason is not None:
            status["motion_blocked_reason"] = reason
        return status

    def _lane_status_base(self, fps_name, lane):
        current_slot = (self._slot_id_by_unit().get(lane.unit)
                        if lane.unit is not None else None)
        return {"op": lane.op, "group": lane.group, "unit": lane.unit,
                "runout": lane.runout, "following": lane.following,
                "since": lane.since, "message": lane.message,
                # UI API v1 additive fields (docs/UI_API.md); every field
                # above is unchanged.
                "state": lane.op.lower(), "current_slot": current_slot,
                "direction": lane.direction,
                "pressure": self._lane_pressure(fps_name),
                "set_point": self._lane_set_point(fps_name, lane),
                "pressure_band": self._lane_pressure_band(fps_name, lane),
                # C2 v2 fields (H1).
                "reload_target": (list(lane.reload_target)
                                  if lane.reload_target is not None else None),
                "runout_from": (list(lane.runout_from)
                                if lane.runout_from is not None else None),
                "stage": (None if not lane.stage_plan else
                          {"plan": list(lane.stage_plan),
                           "index": lane.stage_index,
                           "failed": lane.stage_failed,
                           "labels": dict(lane.stage_labels),
                           "detail": lane.stage_detail}),
                "motion_stage": self._lane_motion_stage(lane),
                "extruder": self.fpss[fps_name].extruder_name,
                "motion_settled": self._motion_settled(fps_name, lane),
                # The loaded bay's working temperature, so an unload can heat
                # to it (C2 §4.2). null when the lane holds nothing, or when
                # the component has not pushed one for that bay.
                "filament_temp_c": self._bay_filament_temp_c(lane.unit)}

    def _bay_filament_temp_c(self, unit):
        """The temp_c pushed for one [unit_idx, bay] pair, or None (C2 §4.5)."""
        if unit is None:
            return None
        oam = self.oam_by_idx.get(unit[0])
        getter = getattr(oam, "bay_filament", None)
        if not callable(getter):
            return None      # a follower has one bay and no spool link
        return (getter(unit[1]) or {}).get("temp_c")

    def _lane_motion_stage(self, lane):
        """The firmware motion-stage name of the lane's loaded or busy unit
        (C2 §5.3, diagnostic only), or None when there is no such unit or it
        has published no domain sample yet. While a runout reloads, the unit
        being loaded is the busy one."""
        unit = (lane.reload_target if lane.runout == S.RUNOUT_LOADING
                else lane.unit)
        if unit is None:
            return None
        oam = self.oam_by_idx.get(unit[0])
        if oam is None:
            return None
        domain = (getattr(oam, "_telemetry", None) or {}).get("domain") or {}
        return domain.get("motion_stage_name")

    def _follower_unit_ok(self, fps, lane):
        """The lane's units can take a follower command right now."""
        unit = self.oam_by_idx.get(lane.unit[0]) if lane.unit else None
        if unit is None or ams_family.motion_block_reason(
                getattr(unit, "capabilities", None) or {}) is not None:
            return False
        return not any(not getattr(oam, "hardware_snapshot_ready", True)
                       or getattr(oam, "motion_available", True) is False
                       or not oam.connected
                       for oam in self.lane_oams.get(fps, []))

    def _reestablish_followers_after_restart(self):
        """Resolve an unknown follower on a LOADED lane, and finish a pending
        forward-follower re-arm. Bounded, never a permanent block.

        `_resync_lane` (startup, ClearErrors, fault clear) is effect-free and
        publishes `following=None`, which keeps `motion_settled` false. A
        completed load leaves a LOADED lane at rest with the forward follower
        armed, so once the lane is provably idle and healthy the manager
        commands exactly that, through the same path as `OAMSM_FOLLOWER
        ENABLE=1 DIRECTION=1`. When the unit refuses it, or the command is not
        confirmed (the unit may still be finishing a reverse unload), the
        lane is first put into a known stopped state (`following=False`, which
        makes it `motion_settled` so a UI can act), then the forward follower
        is retried with a backoff (FOLLOWER_RETRY_BACKOFF) while the lane
        stays idle. After the last try the lane stays stopped (or unknown if
        even the stop was not confirmed) until ClearErrors or a restart
        starts a new resolution. A lane that has nothing pending and a known
        follower is never touched. Any unmet precondition does nothing; the
        lane is looked at again on the next tick."""
        if not self.ready or self._startup_resync_pending:
            return
        get_state = getattr(self.runtime, "get_state", None)
        if get_state is None:
            return  # a manager built without a runtime (status fixtures)
        pending = self.__dict__.setdefault("_follower_rearm", {})
        gave_up = self.__dict__.setdefault("_follower_gave_up", set())
        now = self.reactor.monotonic()
        for fps, lane in list(get_state().lanes.items()):
            rec = pending.get(fps)
            if lane.op != S.OP_LOADED or lane.unit is None or lane.following is True:
                pending.pop(fps, None)
                gave_up.discard(fps)
                continue
            if rec is None:
                if lane.following is not None:
                    gave_up.discard(fps)
                    continue
                if fps in gave_up:
                    continue
                rec = pending[fps] = {"tries": 0, "next": 0.0}
            printing = rec.get("print", False)
            if printing:
                eligible = self._print_arm_eligible(fps, lane)
            else:
                eligible = self._motion_settled(
                    fps, lane, allow_unknown_follower=True)
            if (not eligible or not self._follower_unit_ok(fps, lane)
                    or now < rec["next"]):
                continue
            rec["tries"] += 1
            try:
                self.runtime.dispatch(S.Follow(fps, 1, S.FOLLOWER_FORWARD))
            except Exception:
                logging.exception("OAMS: could not re-establish the follower"
                                  " on lane %s", fps)
            refusal = self._follower_refusal(lane.unit)
            after = get_state().lanes.get(fps)
            if (not refusal and after is not None and after.following is True):
                logging.info("OAMS: lane %s: re-established the forward"
                             " follower", fps)
                pending.pop(fps, None)
                gave_up.discard(fps)
                continue
            logging.warning(
                "OAMS: lane %s: the forward follower could not be"
                " re-established (%s), try %d of %d", fps,
                refusal or "not confirmed", rec["tries"],
                len(FOLLOWER_RETRY_BACKOFF) + 1)
            if not printing and (after is None or after.following is not False):
                # Establish a known stopped state so the lane is not blocked.
                self._stop_followers([fps])
                after = get_state().lanes.get(fps)
            if rec["tries"] > len(FOLLOWER_RETRY_BACKOFF):
                pending.pop(fps, None)
                if after is not None and after.following is None:
                    gave_up.add(fps)
                logging.warning("OAMS: lane %s: giving up re-arming the"
                                " forward follower; the lane stays stopped"
                                " (OAMSM_FOLLOWER ENABLE=1 DIRECTION=1 arms it)",
                                fps)
                continue
            pending[fps] = rec
            rec["next"] = now + FOLLOWER_RETRY_BACKOFF[rec["tries"] - 1]

    def _print_arm_eligible(self, fps, lane):
        """A LOADED lane with nothing running on it, so a print may arm the
        forward follower that feeds the extruder."""
        return (lane.op == S.OP_LOADED and lane.unit is not None
                and lane.runout == S.RUNOUT_IDLE
                and not S._stage_macro_active(lane)
                and not lane.uncertain_follower_units
                and fps not in self._heat_waiting)

    def _arm_followers_for_print(self, now):
        """Printing needs the forward follower, and nothing else arms it when
        a lane was left stopped (a failed flow, a refused re-arm) or unknown.
        On each start or resume of a print job, queue the forward follower for
        every idle LOADED lane that does not have it; the re-arm loop sends it
        with the same bounded retry. Never stops anything."""
        active = self._print_job_active(now)
        was = self.__dict__.get("_print_was_active", False)
        self._print_was_active = active
        if not active or was:
            return
        get_state = getattr(self.runtime, "get_state", None)
        if get_state is None:
            return
        pending = self.__dict__.setdefault("_follower_rearm", {})
        self.__dict__.setdefault("_follower_gave_up", set()).clear()
        for fps, lane in get_state().lanes.items():
            if (lane.following is not True
                    and self._print_arm_eligible(fps, lane)):
                pending[fps] = {"tries": 0, "next": 0.0, "print": True}

    def _follower_unknown_reason(self, lane):
        """Why a lane with `following` null withholds motion, or None."""
        if lane.following is not None:
            return None
        if lane.uncertain_follower_units:
            return ("follower state unconfirmed: a follower command was not"
                    " acknowledged by the unit")
        return ("follower state unknown (restart or failed follower command);"
                " the manager re-establishes the forward follower once the"
                " lane is idle and its units are ready, or send OAMSM_FOLLOWER"
                " ENABLE=1 DIRECTION=1 FPS=<lane>")

    def _motion_settled(self, fps_name, lane, allow_unknown_follower=False):
        """Whether nothing is moving on the lane and no motion action is in
        progress: the published form of the motion-safety predicate a UI
        offers load/unload against (C2 §5.6). `following` may be True: an
        armed follower on an idle LOADED lane is the normal state."""
        if lane.op not in (S.OP_UNLOADED, S.OP_LOADED):
            return False
        if lane.runout != S.RUNOUT_IDLE:
            return False
        # A manager built without __init__ (status fixtures) has no
        # idle_timeout object; _is_printing treats that as not printing.
        now = self.reactor.monotonic()
        printing = (self._is_printing(now)
                    if getattr(self, "idle_timeout", None) is not None
                    else False)
        if printing:
            return False
        if lane.following is None and not allow_unknown_follower:
            return False
        if lane.uncertain_follower_units:
            return False
        # A lane failure is a `pause` fault: it withholds motion (C2 §5.6/§5.7).
        if lane.message in LANE_FAILURE_MESSAGES:
            return False
        state = self.runtime.get_state()
        for physical_lane, record in state.firmware_faults.items():
            if physical_lane == 255 or record[0] == fps_name:
                return False
        for oam in self.lane_oams.get(fps_name, []):
            if not oam.connected or getattr(oam, "unreachable", None):
                return False
        # A stop or pause fault on any unit of the lane withholds motion
        # (C2 §5.6/§5.7); an `info` fault does not.
        for oam in self.lane_oams.get(fps_name, []):
            for fault in self._unit_faults(oam, state, fps_name):
                if fault["severity"] in ("stop", "pause"):
                    return False
        if self._positioning_units(fps_name):
            return False
        return True

    def _positioning_units(self, fps_name):
        """The units on the lane the driver reports as positioning a freshly
        inserted spool (H16). A unit without the notion — a follower, or a
        driver that never learned the method — is never positioning."""
        units = []
        for oam in self.lane_oams.get(fps_name, []):
            bays_fn = getattr(oam, "positioning_bays", None)
            if callable(bays_fn) and bays_fn():
                units.append(oam)
        return units

    def _flow_holds_bay(self, lane, oams_idx, bay):
        """True when the lane is LOADED from this bay and an OpenAMS flow is
        running on it (a recent active macro plan, or a heat wait). The unit's
        own tip pull-back flickers the hub sensor then; the lane, not the
        sensor, says the bay still holds the filament. A real removal ends the
        flow and the lane resyncs, so it is never hidden for longer."""
        if (lane is None or lane.op != S.OP_LOADED
                or lane.unit != (oams_idx, bay)):
            return False
        if S._stage_macro_active(lane):
            return (self.reactor.monotonic() - lane.stage_since
                    <= S.STAGE_FLOW_FRESH_S)
        return False

    def _debounce_bays(self, unit, device, lane):
        """Publish a flow-held bay as loaded even while the hub flickers."""
        for entry in device.get("bays") or ():
            if (not entry.get("loaded") and self._flow_holds_bay(
                    lane, unit.oams_idx, entry.get("index"))):
                entry["loaded"] = True

    def _enrich_device(self, name, unit, device, state, lanes_by_fps):
        """The C2 §4.4/§4.8/§5.6 v2 additions for one `devices[name]`:
        `faults`, `follower_uncertain` and each bay's `supported_actions`.
        `device` is the unit driver's own status, enriched in place."""
        fps = self.topo.lane_of_oams(name)
        lane = lanes_by_fps.get(fps)
        faults = self._unit_faults(unit, state, fps)
        self._debounce_bays(unit, device, state.lanes.get(fps))
        device["faults"] = faults
        device["follower_uncertain"] = any(
            unit.oams_idx == entry[0]
            for ln in state.lanes.values()
            for entry in (ln.uncertain_follower_units or ()))
        mmu_ops = self._enrich_mmu_ops(name, device)
        # A `stop` fault on the unit withholds every motion action (C2 §5.6).
        # H15: a unit that may not move filament withholds them too.
        stop = any(fault["severity"] == "stop" for fault in faults)
        if ams_family.motion_block_reason(
                getattr(unit, "capabilities", None) or {}) is not None:
            stop = True
        if self._arrival_block_reason(unit, fps) is not None:
            stop = True
        # H16: while the unit positions a freshly inserted spool every motion
        # command answers BUSY, so no bay of it offers a motion action.
        if self._positioning_units(fps):
            stop = True
        # A unit that stopped sending frames is offline: `connected` is the
        # reachability of C2 §4.4, so a UI shows it offline instead of
        # inferring that from a fault.
        if getattr(unit, "unreachable", None):
            device["connected"] = False
        # An AMS unit offers a motion or calibration action on a bay only
        # when it reports it (a capability-limited unit reports some of them
        # unavailable). A klipper-mmu unit's bay actions
        # are its own (C5 §7.1) and are left as they are.
        unit_actions = (
            tuple(device["supported_actions"])
            if (self.topo.family_of(name) in AMS_FAMILIES
                and device.get("supported_actions") is not None) else None)
        for bay in device.get("bays") or ():
            bay["supported_actions"] = self._bay_actions(
                lane, unit.oams_idx, bay, stop, mmu_ops, unit_actions)

    def _mmu_op_actions(self, name):
        """The klipper-mmu op tokens this unit really offers (C5 §7.1).

        Only a klipper-mmu family has them, and `home` needs a selector to
        move, so a feeder-per-lane unit offers preload, unpreload and eject
        only. The family profile is the single source (src/ams_family.py)."""
        family = self.topo.family_of(name)
        if family not in ams_family.KLIPPER_MMU_FAMILIES:
            return ()
        profile = ams_family.profile(family)
        return tuple(op for op in MMU_UNIT_OPS if profile.get(op))

    def _enrich_mmu_ops(self, name, device):
        """Publish a klipper-mmu unit's own ops as capability flags and
        action tokens (C2 §4.4.1/§5.6), and return them for the bay table.
        An AMS unit's profile carries none of them, so it publishes none
        either."""
        ops = self._mmu_op_actions(name)
        if not ops:
            return ()
        device["supported_actions"] = [
            action for action in (device.get("supported_actions") or ())
            if action not in MMU_UNIT_OPS] + list(ops)
        capabilities = device.setdefault("capabilities", {})
        for op in MMU_UNIT_OPS:
            capabilities[op] = op in ops
        return ops

    @staticmethod
    def _unit_has_work(unit, state, fps):
        """Whether the lane is using this unit: loading, unloading,
        calibrating or holding a spool of it, a reload aimed at it, or a
        failed operation of it still on display. A silent unit with none of
        that is merely offline; it only becomes a fault when it stops work."""
        lane = state.lanes.get(fps)
        if lane is None:
            return False
        idx = unit.oams_idx
        for ref in (lane.unit, lane.reload_target, lane.fault_unit):
            if ref is not None and ref[0] == idx:
                return True
        return False

    def _unit_faults(self, unit, state, fps):
        """The §4.8 fault entries for one unit: each latched firmware fault on
        the unit's lane or system-wide (key 255), plus a failed lane."""
        faults = []
        # H9: a unit the connect guard refused reports that first, as a stop
        # fault, so the UI withholds its motion actions and names the reason.
        refusal = getattr(unit, "connect_refusal", None)
        if refusal:
            faults.append({
                "severity": "stop", "code": "connect_refused",
                "text": (getattr(unit, "capabilities", None) or {}).get(
                    "connect_refusal", refusal),
                "bay": None, "actions": [], "source": "firmware"})
        # H9d: a unit that went silent past its host watchdog is unreachable;
        # nothing clears it but its frames coming back.
        unreachable = getattr(unit, "unreachable", None)
        if unreachable and self._unit_has_work(unit, state, fps):
            faults.append({
                "severity": "stop", "code": "unit_unreachable",
                "text": unreachable, "bay": None, "actions": [],
                "source": "firmware"})
        # A unit that needs host arrival confirmation on a lane with no
        # toolhead-side arrival signal can neither load nor auto-reload; say
        # so on the unit for as long as the configuration stays that way.
        arrival = self._arrival_block_reason(unit, fps)
        if arrival is not None:
            faults.append({
                "severity": "info", "code": "arrival_signal_missing",
                "text": arrival, "bay": None, "actions": [],
                "source": "config"})
        resolve = getattr(unit, "_controller_fault_name", None)
        # A klipper-mmu unit latches a raw km_fault value and names it itself:
        # the C5 §7.6 severities, codes and action tokens are its own, and a
        # UI must never see the wire number. An AMS unit has no such hook.
        describe = getattr(unit, "firmware_fault_info", None)
        for physical_lane, record in state.firmware_faults.items():
            if physical_lane != 255 and record[0] != fps:
                continue
            entry = (describe(record[1], physical_lane)
                     if callable(describe) else None)
            if entry is None:
                code = resolve(record[1]) if resolve is not None else None
                if not code:
                    code = str(record[1])
                entry = {
                    "severity": ("pause" if code in PAUSE_FIRMWARE_FAULTS
                                 else "info" if code in INFO_FIRMWARE_FAULTS
                                 else "stop"),
                    "code": code, "text": code, "bay": None,
                    "actions": ["clear_fault"], "source": "firmware"}
            faults.append(entry)
        # The unit's own decoded fault names, straight from its domain
        # telemetry (C2 §5.7). A name the runtime latch already carries is
        # not repeated.
        domain = (getattr(unit, "_telemetry", None) or {}).get("domain") or {}
        for key in ("motion_fault_name", "system_fault_name",
                    "dryer_fault_name"):
            name = domain.get(key)
            if not name or name == "none":
                continue
            if any(fault["code"] == name for fault in faults):
                continue
            if name in PAUSE_FIRMWARE_FAULTS:
                severity = "pause"
            elif name in INFO_FIRMWARE_FAULTS:
                severity = "info"
            else:
                # C2 §5.7: every latched firmware fault not in the pause or
                # info list is `stop`, the dryer's sensor faults included: a
                # unit raises them as dryer faults that stop the dryer, not
                # as informational monitoring.
                severity = "stop"
            entry = {
                "severity": severity, "code": name, "text": name,
                "bay": None, "actions": ["clear_fault"],
                "source": "firmware"}
            if name == "lane_obstructed":
                bay = self._obstructed_bay(unit)
                entry["bay"] = bay
                entry["text"] = LANE_OBSTRUCTED_TEXT % (
                    bay if bay is not None else "?")
            if name == "dryer_start_refused":
                entry["text"] = DRYER_START_REFUSED_TEXT
            if name == "rfid_tag_error":
                entry["text"] = RFID_TAG_ERROR_TEXT
            if name == "host_communication_lost":
                # The unit deconfigured itself; only FIRMWARE_RESTART brings
                # it back, so nothing here clears it.
                entry["severity"] = "stop"
                entry["actions"] = []
                if getattr(unit, "session_lost", None):
                    entry["text"] = unit.session_lost
            faults.append(entry)
        lane_state = state.lanes.get(fps)
        if (lane_state is not None
                and lane_state.message in LANE_FAILURE_MESSAGES):
            # The bay is named only on the unit that owns it: every unit of
            # a shared lane carries the fault, but only one holds that bay.
            ref = lane_state.fault_unit
            owns = ref is not None and ref[0] == unit.oams_idx
            faults.append({
                "severity": "pause", "code": lane_state.message,
                "text": lane_state.fault_text or lane_state.message,
                "bay": ref[1] if owns else None,
                "actions": (["retry_load", "clear_errors"]
                            if lane_state.message in ("load failed",
                                                      "reload failed")
                            else ["clear_errors"]),
                "source": "lane"})
        return faults

    @staticmethod
    def _obstructed_bay(unit):
        """The first bay whose hub sensor reads filament without its feeder
        inlet sensor, or None."""
        hub = getattr(unit, "hub_hes_value", None) or []
        f1s = getattr(unit, "f1s_hes_value", None) or []
        for bay in range(min(len(hub), len(f1s))):
            if hub[bay] and not f1s[bay]:
                return bay
        return None

    def _bay_actions(self, lane, oams_idx, bay, stop_fault, mmu_ops=(),
                     unit_actions=None):
        """The §5.6 per-bay action table for one bay: what this bay allows
        now, so a UI can gray out correctly.

        `unit_actions` is the unit's own `supported_actions` for an AMS unit
        (None for any other device): load, unload and calibrate_ptfe
        are offered only where the unit offers them. set_spool and clear_spool
        are host-side spool links, so no capability withholds them."""
        def offered(action):
            return unit_actions is None or action in unit_actions

        actions = []
        settled = bool(lane and lane.get("motion_settled"))
        if settled and not stop_fault:
            if (lane["op"] == S.OP_UNLOADED and lane["following"] is False
                    and bay.get("ready") and not bay.get("loaded")
                    and bay.get("calibration_state") == "configured"
                    and not bay.get("positioning") and offered("load")):
                actions.append("load")
            if (lane["op"] == S.OP_LOADED and lane.get("unit") is not None
                    and tuple(lane["unit"]) == (oams_idx, bay["index"])
                    and offered("unload")):
                actions.append("unload")
            if (lane["op"] == S.OP_UNLOADED and bay.get("ready")
                    and offered("calibrate_ptfe")):
                actions.append("calibrate_ptfe")
        if bay.get("ready"):
            actions.append("set_spool")
        if bay.get("spool_id") is not None:
            actions.append("clear_spool")
        # A klipper-mmu unit's own bay ops are offered per bay like its
        # load/unload, and the unit's admission answers whether this bay can
        # do it right now (C5 §7.1); `home` is the unit's, not a bay's.
        actions.extend(op for op in mmu_ops if op != "home")
        return actions

    def _ui_lanes(self):
        """The UI API v1 "lanes" array (docs/UI_API.md): one entry per FPS
        lane, in topology order. Shared by get_status()'s "lanes" and
        _build_ui_api_status()'s "lanes", so the array is built in one place."""
        state = self.runtime.get_state()
        lanes = []
        for fps_name in self.topo.fps_names:
            lane = state.lanes.get(fps_name)
            if lane is None:
                continue
            ls = self._lane_status(fps_name, lane)
            lanes.append({
                "id": fps_name, "state": ls["state"],
                "current_group": lane.group, "current_slot": ls["current_slot"],
                "following": bool(lane.following), "direction": int(lane.direction),
                "message": lane.message, "pressure": ls["pressure"],
                "set_point": ls["set_point"]})
        return lanes

    def _build_ui_api_status(self):
        """The complete HelixScreen UI API v1 payload (docs/UI_API.md):
        api_version, schema, ready, commands, lanes (the versioned array),
        units, groups. Embedded under the openams/status webhook's "api" key,
        and folded into get_status() (the oams_manager status object read
        through printer.objects.subscribe/query, which is what HelixScreen
        actually reads) via the same _ui_lanes() builder."""
        return {"api_version": UI_API_VERSION, "schema": UI_API_SCHEMA,
                "ready": self.ready, "commands": self._ui_commands(),
                "lanes": self._ui_lanes(), "units": self._ui_units(),
                "groups": self._ui_groups()}

    def _topology_status(self):
        t = self.topo
        return {
            "schema_version": 1,
            "fps": list(t.fps_names),
            "oams": {n: {"idx": t.idx_of(n), "lane": t.lane_of_oams(n),
                         "kind": t.kind_of(n), "family": t.family_of(n),
                         "bays": t.bays_of(n),
                         "capabilities": getattr(self.oams[n],
                                                 "capabilities", {})}
                     for n in t.oams},
            "groups": {g: {"lane": t.lane_of_group(g),
                           "bays": ["%s-%d" % e for e in t.groups[g]]}
                       for g in t.groups}}

    def get_status(self, eventtime):
        state = self.runtime.get_state()
        lanes_by_fps = {fps: self._lane_status(fps, lane)
                        for fps, lane in state.lanes.items()}
        # Back-compat: current_group is the group on the first loaded lane.
        current_group = None
        for lane in state.lanes.values():
            if lane.op == S.OP_LOADED and lane.group:
                current_group = lane.group
                break
        devices = {name: unit.get_webhook_status()
                   for name, unit in self.oams.items()}
        for name, unit in self.oams.items():
            self._enrich_device(name, unit, devices[name], state, lanes_by_fps)
        status = {"schema_version": 2, "ready": self.ready,
                  "current_group": current_group,
                  "topology": self._topology_status(),
                  "devices": devices,
                  "settings": dict(self._settings())}
        # HelixScreen UI API v1 (docs/UI_API.md): "lanes" is the versioned
        # array, addressed by released HelixScreen builds through
        # printer.objects.subscribe/query. "lanes_by_fps" is additive,
        # outside the v1 contract: the same per-FPS dict this field has
        # always published (each entry already carries the v1
        # current_slot/pressure/set_point/state/direction fields added in
        # _lane_status), kept under this new name so it does not collide
        # with the v1 array.
        status["lanes"] = self._ui_lanes()
        status["lanes_by_fps"] = lanes_by_fps
        status["api_version"] = UI_API_VERSION
        status["schema"] = UI_API_SCHEMA
        status["commands"] = self._ui_commands()
        status["units"] = self._ui_units()
        status["groups"] = self._ui_groups()
        return status

    def _fps_status_value(self, fps):
        """Return the status-safe FPS value for read-only reporting."""
        return fps.get_status(self.reactor.monotonic()).get("fps_value")

    def make_helix_backend(self, machine_backend):
        """Bind this live reducer/runtime to HELIX semantic operations.

        The import is lazy so stock Klipper, where HELIX is not installed,
        never loads it.
        """
        from .helix_backend import HelixOpenAMSBackend

        return HelixOpenAMSBackend(self, machine_backend)

    def _webhook_status(self, request):
        state = self.runtime.get_state()
        status = {"ready": self.ready, "units": len(self.oams),
                  "fps": {}, "lanes": {}, "filament_groups": {},
                  "devices": {}}
        for fps_name, fps in self.fpss.items():
            status["fps"][fps_name] = {"value": self._fps_status_value(fps),
                                       "extruder": fps.extruder_name}
        for fps_name, lane in state.lanes.items():
            status["lanes"][fps_name] = self._lane_status(fps_name, lane)
        for gname in self.topo.groups:
            status["filament_groups"][gname] = {
                "lane": self.group_lane.get(gname),
                "bays": ["%s-%d" % (oams_name, bay)
                         for (oams_name, bay) in self.topo.groups[gname]]}
        for name, unit in self.oams.items():
            status["devices"][name] = unit.get_webhook_status()
        # Preserve the original webhook fields (above) while additionally
        # publishing the full UI API v1 snapshot (docs/UI_API.md) below its
        # "api" key, which is a separate namespace from this webhook's own
        # top-level "lanes" dict, so its versioned "lanes" array can be
        # complete here with no collision.
        status["api"] = self._build_ui_api_status()
        request.send({"status": {"openams": status}})

    def _webhook_topology(self, request):
        # Read-only model for a UI to render lanes/OAMS/groups and build edits.
        request.send({"topology": self._topology_status()})

    def _webhook_cancel_load(self, request):
        state = self.runtime.get_state()
        loading = [fps for fps, lane in state.lanes.items()
                   if lane.op == S.OP_LOADING]
        if loading:
            for fps in loading:
                self.runtime.dispatch(S.Cancel(fps))
            request.send({"result": "cancel requested"})
        else:
            request.send({"result": "no load in progress"})

    # ------------------------------------------------------------------- stop

    def _lane_running_sequence(self, fps):
        """A lane is running a sequence when it has an active macro plan, a
        firmware load or unload in flight, or a heat wait in progress."""
        lane = self.runtime.get_state().lanes.get(fps)
        if lane is None:
            return False
        return (S._stage_macro_active(lane)
                or lane.op in (S.OP_LOADING, S.OP_UNLOADING)
                or fps in self._heat_waiting)

    def _webhook_stop(self, request):
        """openams/stop [lane]: stop the whole running sequence (C3). Sets the
        lane's stop flag, which OAMSM_HEAT_WAIT and OAMSM_CHECK_STOP turn into
        a raise at the next boundary, and cancels the firmware load or unload
        in flight right now, the way openams/cancel_load does."""
        lane = request.get("lane", None)
        if lane is not None and lane not in self.fpss:
            request.send({"result": "nothing running"})
            return
        targets = [lane] if lane is not None else list(self.fpss)
        stopped = False
        for fps in targets:
            if not self._lane_running_sequence(fps):
                continue
            stopped = True
            self._stop_requested.add(fps)
            self.runtime.dispatch(S.Cancel(fps))
        request.send({"result": "stopping" if stopped else "nothing running"})

    def _stop_lane(self, gcmd, heater=None):
        """The lane a stop check applies to: FPS=, else the lane that owns
        `heater`, else the sole lane, else the one lane with a plan or an
        operation in flight. None when it cannot be told (a check then does
        nothing rather than guess)."""
        name = gcmd.get("FPS", None)
        if name is not None:
            return name if name in self.fpss else None
        if heater is not None:
            owned = [fps for fps, unit in self.fpss.items()
                     if getattr(unit, "extruder_name", None) == heater]
            if len(owned) == 1:
                return owned[0]
        fps, problem = self._stage_lane(gcmd)
        return fps if problem is None else None

    def _apply_stop(self, fps):
        """A stop has taken effect on a lane (a check raised, or a cancelled
        operation ended the macro): fail the current step, say so in the lane's
        message, put back any heat the sequence raised, and clear the flag."""
        if fps not in self._stop_requested:
            return
        self._stop_requested.discard(fps)
        self.runtime.dispatch(S.Stopped(fps))
        self._note_flow_failure(fps)
        self._restore_raised_heat(fps)

    def _restore_raised_heat(self, fps):
        """Set the lane's extruder back to the target _OAMS_PLAN recorded when
        the sequence raised it, never during a print."""
        plan = self.printer.lookup_object("gcode_macro _OAMS_PLAN", None)
        variables = getattr(plan, "variables", None) or {}
        if int(variables.get("raised", 0) or 0) != 1:
            return
        stats = self.printer.lookup_object("print_stats", None)
        if stats is not None:
            state = stats.get_status(self.reactor.monotonic()).get("state")
            if state == "printing":
                return
        heater = getattr(self.fpss[fps], "extruder_name", None)
        if not heater:
            return
        target = float(variables.get("pre_target", 0.0) or 0.0)
        self.printer.lookup_object("gcode").run_script_from_command(
            "SET_HEATER_TEMPERATURE HEATER=%s TARGET=%s\n"
            "SET_GCODE_VARIABLE MACRO=_OAMS_PLAN VARIABLE=raised VALUE=0"
            % (heater, target))

    cmd_CHECK_STOP_help = ("Raise 'Stopped by the user' when the user asked to"
                           " stop the running sequence")

    def cmd_CHECK_STOP(self, gcmd):
        """C3: a macro calls this right before each step and hook. Does
        nothing unless openams/stop flagged the lane."""
        fps = self._stop_lane(gcmd)
        if fps is not None and fps in self._stop_requested:
            self._apply_stop(fps)
            raise gcmd.error(STOPPED_TEXT)

    cmd_HEAT_WAIT_help = ("Wait for a heater to reach a minimum, like"
                          " TEMPERATURE_WAIT, but stoppable by the user")

    def cmd_HEAT_WAIT(self, gcmd):
        """C3: TEMPERATURE_WAIT that polls with the reactor and raises
        'Stopped by the user' as soon as the lane's stop flag is set."""
        heater = gcmd.get("HEATER")
        minimum = gcmd.get_float("MINIMUM")
        fps = self._stop_lane(gcmd, heater)
        obj = self.printer.lookup_object(heater, None)
        if obj is None or not hasattr(obj, "get_status"):
            raise gcmd.error("OAMSM_HEAT_WAIT: unknown heater '%s'" % heater)
        if fps is not None:
            self._heat_waiting.add(fps)
        try:
            while True:
                if fps is not None and fps in self._stop_requested:
                    self._apply_stop(fps)
                    raise gcmd.error(STOPPED_TEXT)
                now = self.reactor.monotonic()
                temperature = obj.get_status(now).get("temperature")
                if temperature is not None and temperature >= minimum:
                    return
                self.reactor.pause(now + HEAT_WAIT_POLL)
        finally:
            if fps is not None:
                self._heat_waiting.discard(fps)

    # --------------------------------------------------------------- commands

    def register_commands(self):
        gcode = self.printer.lookup_object("gcode")
        for name, handler, desc in (
            ("OAMSM_UNLOAD_FROM_TOOLHEAD", self.cmd_UNLOAD_FROM_TOOLHEAD,
             self.cmd_UNLOAD_FROM_TOOLHEAD_help),
            ("OAMSM_LOAD_TO_TOOLHEAD", self.cmd_LOAD_TO_TOOLHEAD,
             self.cmd_LOAD_TO_TOOLHEAD_help),
            ("OAMSM_UNLOAD_FILAMENT", self.cmd_UNLOAD_FILAMENT,
             self.cmd_UNLOAD_FILAMENT_help),
            ("OAMSM_LOAD_FILAMENT", self.cmd_LOAD_FILAMENT,
             self.cmd_LOAD_FILAMENT_help),
            ("OAMSM_VALIDATE_LOAD", self.cmd_VALIDATE_LOAD,
             self.cmd_VALIDATE_LOAD_help),
            ("OAMSM_FOLLOWER", self.cmd_FOLLOWER, self.cmd_FOLLOWER_help),
            ("OAMSM_CURRENT_LOADED_GROUP", self.cmd_CURRENT_LOADED_GROUP,
             self.cmd_CURRENT_LOADED_GROUP_help),
            ("OAMSM_CLEAR_ERRORS", self.cmd_CLEAR_ERRORS,
             self.cmd_CLEAR_ERRORS_help),
            ("OAMSM_STAGE", self.cmd_STAGE, self.cmd_STAGE_help),
            ("OAMSM_CHECK_STOP", self.cmd_CHECK_STOP,
             self.cmd_CHECK_STOP_help),
            ("OAMSM_HEAT_WAIT", self.cmd_HEAT_WAIT, self.cmd_HEAT_WAIT_help),
            ("OAMSM_LOAD_FILAMENT_CANCEL", self.cmd_LOAD_FILAMENT_CANCEL,
             self.cmd_LOAD_FILAMENT_CANCEL_help),
            ("OAMSM_CREATE_GROUP", self.cmd_CREATE_GROUP,
             self.cmd_CREATE_GROUP_help),
            ("OAMSM_DELETE_GROUP", self.cmd_DELETE_GROUP,
             self.cmd_DELETE_GROUP_help),
            ("OAMSM_ASSIGN_BAY", self.cmd_ASSIGN_BAY, self.cmd_ASSIGN_BAY_help),
            ("OAMSM_SET_BAY_SPOOL", self.cmd_SET_BAY_SPOOL,
             self.cmd_SET_BAY_SPOOL_help),
            ("OAMSM_CLEAR_BAY_SPOOL", self.cmd_CLEAR_BAY_SPOOL,
             self.cmd_CLEAR_BAY_SPOOL_help),
            ("OAMSM_SET_BAY_FILAMENT", self.cmd_SET_BAY_FILAMENT,
             self.cmd_SET_BAY_FILAMENT_help),
            ("OAMSM_SET_TRAY_INFO", self.cmd_SET_TRAY_INFO,
             self.cmd_SET_TRAY_INFO_help),
            ("OAMSM_UNASSIGN_BAY", self.cmd_UNASSIGN_BAY,
             self.cmd_UNASSIGN_BAY_help),
            ("OAMSM_SET_OPTION", self.cmd_SET_OPTION,
             self.cmd_SET_OPTION_help),
            ("OAMSM_SELFTEST", self.cmd_SELFTEST, self.cmd_SELFTEST_help),
        ):
            gcode.register_command(name, handler, desc=desc)
        self._register_fault_clear_routes(gcode)
        self._register_mmu_op_routes(gcode)

    def _register_mmu_op_routes(self, gcode):
        """Route OAMS_PRELOAD/UNPRELOAD/EJECT/HOME OAMS=<idx> to the unit.

        klipper-mmu reaches these four operations only through its own
        KLIPPER_MMU_* commands, so OpenAMS publishes them under the OAMS_*
        names and the OAMS= key the UIs already address a unit by (C3 §2).
        Every unit gets a route, including one that cannot do the operation:
        a refusal that names the reason is what a UI needs, where an
        unregistered command would only say the name is unknown."""
        for name in self._ordered_unit_names():
            index = str(self.oams[name].oams_idx)
            for command, op in MMU_UNIT_COMMANDS:
                def route(gcmd, command=command, op=op):
                    return self._cmd_unit_mmu_op(command, op, gcmd)
                gcode.register_mux_command(
                    command, "OAMS", index, route,
                    desc=MMU_UNIT_COMMANDS_HELP[command])

    def _cmd_unit_mmu_op(self, command, op, gcmd):
        """One klipper-mmu op on the unit OAMS= names, through the unit object.

        The unit owns the wire, so it also owns the refusal: whatever it
        raises (a busy path, a latched fault, a read-only unit) is what the
        caller is told, under this command's name (C3 §2)."""
        try:
            index = int(gcmd.get("OAMS", None))
        except (TypeError, ValueError):
            raise gcmd.error("%s: OAMS=<idx> is required" % command)
        name = self.unit_name_by_idx.get(index)
        if name is None:
            raise gcmd.error("%s: no OpenAMS unit with index %s"
                             % (command, index))
        family = self.topo.family_of(name)
        if family not in ams_family.KLIPPER_MMU_FAMILIES:
            raise gcmd.error("%s: unit %s is not a klipper-mmu unit"
                             % (command, name))
        if not ams_family.profile(family).get(op):
            raise gcmd.error("%s: unit %s feeds each lane independently, so "
                             "it has no selector to home" % (command, name))
        unit = self.oams[name]
        method = getattr(unit, op, None)
        if not callable(method):
            raise gcmd.error("%s: unit %s does not support %s"
                             % (command, name, op))
        bay = None if op == "home" else self._mmu_op_bay(gcmd, command, name)
        try:
            if bay is None:
                method()
            else:
                method(bay)
        except Exception as exc:
            raise gcmd.error("%s: %s" % (command, exc))
        gcmd.respond_info("%s: %s" % (
            command, "unit %s" % name if bay is None
            else "unit %s bay %d" % (name, bay)))

    def _mmu_op_bay(self, gcmd, command, name):
        """The bay one klipper-mmu op addresses, from `SPOOL=<bay>`."""
        raw = gcmd.get("SPOOL", None)
        if raw is None:
            raise gcmd.error("%s: SPOOL=<bay> is required" % command)
        try:
            bay = int(raw)
        except (TypeError, ValueError):
            raise gcmd.error("%s: SPOOL must be a bay number" % command)
        if not 0 <= bay < self.topo.bays_of(name):
            raise gcmd.error("%s: bay %s is outside unit %s" % (command, raw,
                                                              name))
        return bay

    def _register_fault_clear_routes(self, gcode):
        """Route OAMS_CLEAR_FAULT OAMS=<idx> to the unit that owns the index.

        C2 §5.7's `clear_fault` is that one command for every family, and the
        UIs send it verbatim. An AMS unit registers the mux command itself
        (src/oams.py); a unit that recovers differently — a klipper-mmu unit
        acknowledges the fault instead — registers it here, under its own
        index, so the name and the token stay what the UIs already speak."""
        for name in self._ordered_unit_names():
            unit = self.oams.get(name)
            route = getattr(unit, "register_fault_clear_command", None)
            if callable(route):
                route(gcode)

    cmd_SELFTEST_help = "Report OpenAMS wiring/health (read-only diagnostics)"

    def cmd_SELFTEST(self, gcmd):
        """Non-destructive bring-up check: confirms every OAMS connected, the
        negotiated protocol per unit, sensor readings, the validated topology
        and live lane state, and flags anything suspicious. Moves no filament."""
        lines = ["OpenAMS self-test:"]
        warns = []
        for name, fps in self.fpss.items():
            value = self._fps_status_value(fps)
            value_text = "unavailable" if value is None else "%.3f" % value
            lines.append("  FPS %s: value=%s extruder=%s"
                         % (name, value_text,
                            getattr(fps, "extruder_name", "?")))
            if value is None:
                warns.append("FPS %s feedback unavailable" % name)
            if getattr(fps, "extruder", None) is None:
                warns.append("FPS %s extruder unresolved" % name)
        oams_versions = set()
        for short, oam in sorted(self.oams.items()):
            kind = self.topo.kind_of(short)
            bays = self.topo.bays_of(short)
            connected = oam.connected
            sensors_available = (getattr(oam, "hardware_snapshot_ready", True)
                                 and oam.f1s_hes_value is not None
                                 and oam.hub_hes_value is not None)
            if sensors_available:
                ready = [b for b in range(bays) if oam.f1s_hes_value[b]]
                loaded = [b for b in range(bays) if oam.hub_hes_value[b]]
            else:
                warns.append("%s %s sensor snapshot unavailable" % (kind, short))
            lines.append(
                "  %s %s (idx %s) lane %s: %s, %s"
                % (kind.upper(), short, oam.oams_idx, oam.fps_name,
                   "connected" if connected else "NOT CONNECTED",
                   oam.protocol_summary()))
            if kind == "follower":
                lines.append("    pre(ready)=%s post(loaded)=%s"
                             " path_length=%.1fmm fps_stale=%s ff_underrun=%s"
                             % (oam.f1s_hes_value[0] if sensors_available
                                else "unavailable",
                                oam.hub_hes_value[0] if sensors_available
                                else "unavailable",
                                oam.path_length_mm, oam.fps_stale,
                                oam.ff_underrun))
                if oam.path_length_mm <= 0:
                    warns.append("follower %s path_length is 0 (runout will"
                                 " pause instead of auto-reloading)" % short)
            else:
                oams_versions.add(oam.protocol_version)
                lines.append("    bays ready=%s loaded=%s"
                             % ((ready or "-") if sensors_available
                                else "unavailable",
                                (loaded or "-") if sensors_available
                                else "unavailable"))
            if not connected:
                warns.append("%s %s firmware commands not resolved"
                             % (kind, short))
            # H9: a refused unit is named here, with the guard's reason, so
            # bring-up says why the unit will not be operated.
            refusal = getattr(oam, "connect_refusal", None)
            if refusal:
                lines.append("    REFUSED: %s" % refusal)
                warns.append("%s %s refused: %s" % (kind, short, refusal))
        if len(oams_versions) > 1:
            warns.append("mixed OAMS firmware protocol versions %s"
                         % sorted(str(v) for v in oams_versions))
        for g in self.topo.groups:
            bays = ",".join("%s-%d" % e for e in self.topo.groups[g])
            lines.append("  group %s: lane=%s bays=%s"
                         % (g, self.topo.lane_of_group(g), bays or "(empty)"))
        for fps, ls in self.runtime.get_state().lanes.items():
            lines.append("  lane %s: op=%s group=%s unit=%s following=%s"
                         % (fps, ls.op, ls.group, ls.unit, ls.following))
        lines.append("  RESULT: %s"
                     % ("PASS" if not warns else "WARN -> " + "; ".join(warns)))
        gcmd.respond_info("\n".join(lines))

    def _resolve_fps(self, gcmd, prefer_op=None, required=True):
        """Resolve which FPS lane a command targets. Optional FPS= parameter;
        defaults to the sole lane, or (when ambiguous) the single lane currently
        in prefer_op. Raises a gcode error when ambiguous (so macros do not sail
        past it), unless required=False, in which case None is returned."""
        name = gcmd.get("FPS", None)
        if name is not None:
            if name not in self.fpss:
                raise gcmd.error("Unknown FPS '%s'" % name)
            return name
        if len(self.fpss) == 1:
            return next(iter(self.fpss))
        if prefer_op is not None:
            state = self.runtime.get_state()
            match = [fps for fps, lane in state.lanes.items()
                     if lane.op == prefer_op]
            if len(match) == 1:
                return match[0]
        if required:
            raise gcmd.error("Multiple FPS present; specify FPS=<name>")
        return None

    def _motion_pending_units(self):
        """Unit short names whose motion is unavailable but can still return.

        A Klipper-MMU physical observation unit declares `motion_available` as
        a constant: it is read-only by design and never takes motion, so it
        must not hold the other units not-ready forever. A managed unit
        computes the flag, so its False is temporary and its sensor mirrors
        stay untrustworthy until its session comes back.
        """
        pending = []
        for name, oam in self.oams.items():
            if getattr(oam, "motion_available", True) is not False:
                continue
            if isinstance(inspect.getattr_static(type(oam),
                                                 "motion_available", None),
                          bool):
                continue
            pending.append(name)
        return pending

    def _lane_unit_names(self, fps):
        """Short names of the units feeding `fps` (a lane-addressed command
        moves every one of them, so every one of them gates it)."""
        lane_oams = getattr(self, "lane_oams", {})
        if fps not in lane_oams:
            return list(getattr(self, "oams", {}))
        names = getattr(self, "unit_name_by_idx", {})
        return [names.get(oam.oams_idx, str(oam.oams_idx))
                for oam in lane_oams[fps]]

    def _group_unit_names(self, group):
        """Short names of the units a group draws its bays from; none for a
        group that does not exist, so its own error is what the user sees."""
        names = getattr(self, "unit_name_by_idx", {})
        if group not in getattr(getattr(self, "topo", None), "groups", {}):
            return []
        return [names.get(index, str(index))
                for index in sorted({bay[0] for bay
                                     in self.topo.group_bays_idx(group)})]

    def _command_unit_names(self, gcmd):
        """The units a lane-addressed command targets: FPS=<lane>, else the
        sole lane, else every unit (it cannot be narrowed to one)."""
        lanes = getattr(self, "fpss", {})
        name = gcmd.get("FPS", None)
        if name is not None and name in lanes:
            return self._lane_unit_names(name)
        if len(lanes) == 1:
            return self._lane_unit_names(next(iter(lanes)))
        return list(getattr(self, "oams", {}))

    def _require_motion_available(self, gcmd, unit_names=None,
                                  stop_followers=False):
        """Refuse a motion command that targets a unit which cannot move.

        Only the units the command addresses are gated, so a read-only unit
        (a Klipper-MMU physical observation) refuses its own commands and
        names itself, while every other unit keeps working.
        """
        oams = getattr(self, "oams", {})
        if unit_names is None:
            unit_names = list(oams)
        unavailable = [name for name in unit_names
                       if getattr(oams.get(name), "motion_available",
                                  True) is False]
        if unavailable:
            if stop_followers:
                self._stop_followers(self.fpss)
            raise gcmd.error("OpenAMS motion unavailable for %s"
                             % ", ".join(sorted(unavailable)))

    def _require_hardware_snapshot(self, gcmd, unit_names=None):
        self._require_motion_available(gcmd, unit_names)
        unavailable = [name for name, oam in getattr(self, "oams", {}).items()
                       if not getattr(oam, "hardware_snapshot_ready", True)]
        if unavailable:
            raise gcmd.error("OpenAMS hardware snapshot unavailable for %s"
                             % ", ".join(sorted(unavailable)))

    def _stop_followers(self, fps_names):
        """Stop followers on the given lanes, both in the store (so
        LaneState.following stays truthful) and on every unit of the lane (to
        cover followers enabled directly on idle units)."""
        for fps in fps_names:
            # An explicit stop ends any pending forward re-arm of that lane.
            self.__dict__.get("_follower_rearm", {}).pop(fps, None)
            try:
                self.runtime.dispatch(S.Follow(fps, 0, S.FOLLOWER_REVERSE))
            except Exception:
                logging.exception("OpenAMS: failed to clear follower state on %s",
                                  fps)
            for oam in self.lane_oams[fps]:
                try:
                    response = oam.set_oams_follower(0, S.FOLLOWER_REVERSE)
                    # The direct stop covers orphan units absent from the
                    # lane's current selection. An MMU command admission is
                    # not proof that the follower stopped; only a confirmed
                    # response may clear its uncertain unit record.
                    if getattr(response, "follower_confirmed", True) is False:
                        continue
                    lane = self.runtime.get_state().lanes.get(fps)
                    if lane is not None:
                        for unit in lane.uncertain_follower_units:
                            if unit[0] == oam.oams_idx:
                                self.runtime.dispatch(
                                    S.FollowerApplied(fps, unit, False))
                except Exception:
                    logging.exception(
                        "OpenAMS: failed to stop follower unit %s on %s",
                        getattr(oam, "oams_idx", "?"), fps)

    cmd_CLEAR_ERRORS_help = "Clear the error state of the OAMS"

    def cmd_CLEAR_ERRORS(self, gcmd):
        """Clear the error state of every unit that has any.

        A read-only unit (a Klipper-MMU physical observation) never takes
        motion, so it holds no error state and its `clear_errors()` raises.
        It is skipped, not obeyed, so one such unit cannot block error
        recovery on the rest of the system; the reply names what it left
        alone."""
        skipped = [name for name in self._ordered_unit_names()
                   if getattr(self.oams[name], "motion_available",
                              True) is False]
        self._require_hardware_snapshot(
            gcmd, [name for name in self._ordered_unit_names()
                   if name not in skipped])
        self.stop_monitor()
        for name, oam in self.oams.items():
            if name not in skipped:
                oam.clear_errors()
        # A new resolution of any unknown follower starts from a clean slate.
        self.__dict__.setdefault("_follower_gave_up", set()).clear()
        self.runtime.dispatch(S.ClearErrors())
        self.start_monitor()
        if skipped:
            gcmd.respond_info("OAMSM_CLEAR_ERRORS: skipped read-only %s; "
                              "it holds no error state to clear"
                              % ", ".join(skipped))

    cmd_STAGE_help = "Report a toolchange/unload plan and its progress (never fails)"

    def cmd_STAGE(self, gcmd):
        """C3 §4: a macro publishes its plan (PLAN=), the step it entered
        (NAME=, with an optional DETAIL=), a failed step (FAILED=1) or the end
        of the plan (DONE=1). A PLAN= also carries LABEL_<STEP>="text" display
        labels. Never raises: every mistake is a warning and a no-op, so a
        stage report can never abort a toolchange."""
        fps, problem = self._stage_lane(gcmd)
        lane = self.runtime.get_state().lanes[fps] if fps is not None else None
        plan = gcmd.get("PLAN", None)
        name = gcmd.get("NAME", None)
        failed = gcmd.get("FAILED", None)
        done = gcmd.get("DONE", None)
        labels = self._stage_labels(gcmd)
        detail = gcmd.get("DETAIL", None)
        given = [v for v in (plan, name, failed, done) if v is not None]
        if problem is None and len(given) != 1:
            problem = ("specify exactly one of PLAN=, NAME=, FAILED=1 or"
                       " DONE=1")
        # C3 §4: the flags are FAILED=1 and DONE=1; any other value is a
        # mistake to report, not a request.
        for key, value in (("FAILED", failed), ("DONE", done)):
            if problem is None and value is not None and value.strip() != "1":
                problem = "%s must be 1" % key
        if problem is None and labels and plan is None:
            problem = "LABEL_<STEP> goes with PLAN="
        if problem is None and detail is not None and name is None:
            problem = "DETAIL goes with NAME="
        action = None
        stray = ()
        if problem is None:
            action, problem, stray = self._stage_action(
                fps, lane, plan, name, failed, done, labels, detail)
        if problem is not None:
            gcmd.respond_info("OAMSM_STAGE: %s; ignored" % problem)
            return
        self.runtime.dispatch(action)
        if failed is not None:
            self._note_flow_failure(fps)
        # A new plan starts clean, and a plan that ended normally has nothing
        # left to stop (openams/stop's flag).
        if plan is not None or done is not None:
            self._stop_requested.discard(fps)
        # A label for a step the plan does not carry is dropped, but the plan
        # itself is still accepted.
        for step in stray:
            gcmd.respond_info("OAMSM_STAGE: no step '%s' in the plan;"
                             " its label was dropped" % step)

    def _stage_labels(self, gcmd):
        """The ``LABEL_<STEP>="text"`` display labels of one OAMSM_STAGE
        command, as ``{step: label}`` keyed case-insensitively (Klipper
        upper-cases every parameter name, so ``LABEL_BRUSH`` is the label of
        ``brush``). A blank label is no label. The parameters come from
        Klipper's ``GCodeCommand.get_command_parameters()``."""
        params = gcmd.get_command_parameters()
        labels = {}
        for key in sorted(params or ()):
            if not key.upper().startswith("LABEL_"):
                continue
            label = S.stage_text(params[key], S.STAGE_LABEL_MAX_BYTES)
            if label is not None:
                labels[key[len("LABEL_"):].lower()] = label
        return labels

    def _stage_lane(self, gcmd):
        """The lane an OAMSM_STAGE report targets (C3 §4), or ``(None, why)``
        when it cannot be resolved: FPS=, else GROUP=, else the sole lane,
        else the single lane with a plan or an operation in flight."""
        name = gcmd.get("FPS", None)
        if name is not None:
            if name not in self.fpss:
                return None, "unknown FPS '%s'" % name
            return name, None
        group = gcmd.get("GROUP", None)
        if group is not None:
            lane = self.group_lane.get(group)
            if lane is None:
                return None, "unknown GROUP '%s'" % group
            return lane, None
        if len(self.fpss) == 1:
            return next(iter(self.fpss)), None
        state = self.runtime.get_state()
        candidates = [fps for fps, lane in state.lanes.items()
                      if lane.stage_plan
                      or lane.op not in (S.OP_UNLOADED, S.OP_LOADED)]
        if len(candidates) == 1:
            return candidates[0], None
        return None, "no single lane with a stage or operation; specify FPS="

    def _stage_action(self, fps, lane, plan, name, failed, done, labels,
                      detail):
        """The reducer action for one valid-looking request, or ``(None, why)``
        when the lane's current state forbids it (the reducer would ignore it
        silently, so the manager warns instead). `stray` names the labels the
        plan does not carry, which are dropped rather than refused."""
        stray = ()
        if plan is not None:
            names = tuple(part.strip() for part in plan.split(",")
                          if part.strip())
            if not names:
                return None, "PLAN= needs at least one step", stray
            for step in names:
                if not S.stage_name_ok(step):
                    return None, "invalid step name '%s'" % step, stray
            if len(names) > S.STAGE_MAX_STEPS:
                return (None, "PLAN= takes at most %d steps"
                        % S.STAGE_MAX_STEPS, stray)
            if len(set(names)) != len(names):
                return None, "PLAN= repeats a step", stray
            stray = tuple(step for step in sorted(labels) if step not in names)
            return (S.StagePlan(fps, names,
                                tuple((step, labels[step])
                                      for step in names if step in labels)),
                    None, stray)
        if name is not None:
            if not S.stage_name_ok(name):
                return None, "invalid step name '%s'" % name, stray
            if not lane.stage_plan:
                return None, "no active plan", stray
            if name not in lane.stage_plan:
                return None, "step '%s' is not in the plan" % name, stray
            if (lane.stage_index is not None
                    and lane.stage_plan.index(name) < lane.stage_index):
                return (None, "step '%s' is before the current step" % name,
                        stray)
            return S.StageEnter(
                fps, name, S.stage_text(detail, S.STAGE_DETAIL_MAX_BYTES)
            ), None, stray
        if failed is not None:
            if not lane.stage_plan:
                return None, "no active plan", stray
            if lane.stage_index is None:
                return None, "no step has started", stray
            return S.StageFail(fps), None, stray
        if done is not None:
            if not lane.stage_plan:
                return None, "no active plan", stray
            return S.StageDone(fps), None, stray
        return (None, "specify exactly one of PLAN=, NAME=, FAILED=1 or"
                " DONE=1", stray)

    cmd_CURRENT_LOADED_GROUP_help = "Report the currently loaded group(s)"

    def cmd_CURRENT_LOADED_GROUP(self, gcmd):
        state = self.runtime.get_state()
        loaded = {fps: lane.group for fps, lane in state.lanes.items()
                  if lane.op == S.OP_LOADED and lane.group}
        if not loaded:
            gcmd.respond_info("No group is currently loaded")
        elif len(loaded) == 1:
            gcmd.respond_info(next(iter(loaded.values())))
        else:
            gcmd.respond_info(", ".join("%s=%s" % (f, g)
                                        for f, g in loaded.items()))

    cmd_FOLLOWER_help = "Enable the follower on whichever OAMS is loaded on a lane"

    def cmd_FOLLOWER(self, gcmd):
        enable = gcmd.get_int("ENABLE", minval=0, maxval=1)
        direction = gcmd.get_int("DIRECTION", minval=0, maxval=1)
        # Starting a follower needs an unambiguous lane (raise rather than
        # guess); stopping is always safe to broadcast, and stop commands run
        # in cleanup/error macros that must not abort.
        fps = self._resolve_fps(gcmd, prefer_op=S.OP_LOADED,
                                required=bool(enable))
        if fps is None:
            self._stop_followers(self.fpss)
            gcmd.respond_info("Followers stopped on all lanes")
            return
        if enable:
            # A follower drives one lane, so a unit of that lane which cannot
            # move refuses it; the other lanes keep theirs.
            self._require_hardware_snapshot(gcmd, self._lane_unit_names(fps))
        lane = self.runtime.get_state().lanes[fps]
        if lane.unit is None:
            if not enable:
                self._stop_followers([fps])
            gcmd.respond_info("No spool is currently loaded on lane %s" % fps)
            return
        if enable and direction == S.FOLLOWER_REVERSE:
            unit = getattr(self, "oam_by_idx", {}).get(lane.unit[0])
            if not getattr(unit, "has_reverse_follower", True):
                # Refused at once, without sending (AMS_CAP_REVERSE_FOLLOWER=0).
                raise gcmd.error(unit.REVERSE_FOLLOWER_REFUSAL)
        # Through the store so LaneState.following/direction stay truthful.
        self.runtime.dispatch(S.Follow(fps, enable, direction))
        refusal = self._follower_refusal(lane.unit)
        if enable and refusal:
            raise gcmd.error(refusal)

    cmd_LOAD_FILAMENT_CANCEL_help = "Cancel the in-flight load on a lane"

    def cmd_LOAD_FILAMENT_CANCEL(self, gcmd):
        # Cancel runs from cleanup/error macros; never abort them. With no
        # FPS= and no unambiguous loading lane there is nothing to cancel.
        fps = self._resolve_fps(gcmd, prefer_op=S.OP_LOADING, required=False)
        if fps is not None and self.runtime.get_state().lanes[fps].op == S.OP_LOADING:
            self.runtime.dispatch(S.Cancel(fps))
            gcmd.respond_info("Load cancel requested on lane %s" % fps)
        else:
            gcmd.respond_info("No load filament operation is in progress")

    cmd_UNLOAD_FILAMENT_help = "Unload the spool loaded on a lane"

    def cmd_UNLOAD_FILAMENT(self, gcmd):
        # SAFE_UNLOAD_FILAMENT enables the follower (rewind) before calling us,
        # so every path below that does not unload MUST stop followers — this
        # is the runaway-rewind case this command exists to prevent.
        self._require_motion_available(gcmd, self._command_unit_names(gcmd),
                                       stop_followers=True)
        if any(not getattr(oam, "hardware_snapshot_ready", True)
               for oam in getattr(self, "oams", {}).values()):
            self._stop_followers(self.fpss)
            self._require_hardware_snapshot(gcmd)
        fps = self._resolve_fps(gcmd, prefer_op=S.OP_LOADED, required=False)
        if fps is None:
            state = self.runtime.get_state()
            if any(lane.op == S.OP_LOADED for lane in state.lanes.values()):
                # Can't guess which loaded lane to unload — but before
                # aborting, stop any lane known to be rewinding or whose
                # follower command failed. Unknown may still be moving in
                # either direction; a known forward follower is left alone.
                rewinding = [f for f, lane in state.lanes.items()
                             if lane.following is None
                             or (lane.following
                                 and lane.direction == S.FOLLOWER_REVERSE)]
                self._stop_followers(rewinding)
                raise gcmd.error(
                    "Multiple FPS have a spool loaded; specify FPS=<name>")
            # Multiple FPS, no FPS= given and nothing loaded anywhere: there is
            # no lane to unload, but a follower may still be rewinding. Stop
            # them all rather than leaving one running.
            self._stop_followers(self.fpss)
            gcmd.respond_info("No spool is loaded on any lane (followers"
                              " stopped); specify FPS=<name> to target a lane")
            return
        lane = self.runtime.get_state().lanes[fps]
        if lane.op != S.OP_LOADED:
            self._stop_followers([fps])
            gcmd.respond_info("No spool is loaded on lane %s (follower stopped)"
                              % fps)
            return
        # H15b: the loaded unit must not move either, so refuse the unload up
        # front with its own reason instead of dispatching it and failing
        # inside the driver with a generic "unload failed".
        unit = self.oam_by_idx.get(lane.unit[0]) if lane.unit is not None else None
        reason = ams_family.motion_block_reason(
            getattr(unit, "capabilities", None) or {})
        if reason is not None:
            raise gcmd.error(reason)
        self._wait_for_positioning(fps, gcmd)
        result = self.runtime.request(fps, S.Unload(fps)).wait()
        if not result.ok:
            # Raise so SAFE_UNLOAD-style macros stop instead of carrying on
            # with a spool still (partially) loaded.
            raise gcmd.error(result.message)
        gcmd.respond_info(result.message)

    def _validate_load_target(self, gcmd, group_name, requested_slot):
        """OAMSM_VALIDATE_LOAD: reject a malformed, unknown, unavailable, or
        incorrectly grouped slot before any homing, cutting, or unloading
        (docs/UI_API.md 'Load a selected slot'). Returns the resolved
        (unit short name, bay)."""
        if not self.ready:
            raise gcmd.error("OpenAMS is not ready")
        if group_name not in self.topo.groups:
            raise gcmd.error("Group %s does not exist" % group_name)
        selected = self._slot_lookup(requested_slot)
        if selected is None:
            raise gcmd.error("OpenAMS slot %d does not exist" % requested_slot)
        if selected not in self.topo.groups[group_name]:
            raise gcmd.error("%s is not assigned to group %s"
                             % (S.spool_name(*selected), group_name))
        short_name, bay = selected
        oam = self.oams[short_name]
        # Before any heating, homing or cutting: a unit that cannot move
        # filament refuses with its own reason (H15b, C3).
        reason = ams_family.motion_block_reason(
            getattr(oam, "capabilities", None) or {})
        if reason is None:
            reason = self._arrival_block_reason(
                oam, self.topo.lane_of_oams(short_name))
        if reason is not None:
            raise gcmd.error(reason)
        lane = self.runtime.get_state().lanes.get(self.topo.lane_of_oams(short_name))
        is_current = lane is not None and lane.unit == (oam.oams_idx, bay)
        if not oam.bay_loadable(bay) and not is_current:
            raise gcmd.error("%s is not ready" % S.spool_name(short_name, bay))
        return selected

    cmd_VALIDATE_LOAD_help = "Validate a UI load target without moving filament"

    def cmd_VALIDATE_LOAD(self, gcmd):
        self._validate_load_target(gcmd, gcmd.get("GROUP"),
                                   gcmd.get_int("SLOT", minval=0))

    cmd_LOAD_FILAMENT_help = (
        "Load a spool from a group (lane inferred from group), optionally a"
        " specific UI API v1 SLOT within it")

    def cmd_LOAD_FILAMENT(self, gcmd):
        group = gcmd.get("GROUP")
        # A group load may pick any of its bays, so every unit it draws from
        # gates it (a SLOT load is a load from that same group). An unknown
        # group gates nothing and is reported by the resolution below.
        units = self._group_unit_names(group)
        self._require_hardware_snapshot(gcmd, units)
        # A drying unit does not feed (a stock AMS HT ignored a load during its
        # timed cycle until the firmware's motion check failed). Refuse when
        # every unit the group could load from is drying.
        drying = [name for name in units
                  if getattr(self.oams.get(name), "_dryer_cycle_running",
                             lambda: False)()]
        if units and len(drying) == len(units):
            raise gcmd.error(
                "%s is drying and does not feed; stop the dryer"
                " (OAMS_DRYER_STOP) before loading %s"
                % (", ".join(drying), group))
        fps = self.group_lane.get(group)
        if fps is None:
            if group in self.topo.groups:
                raise gcmd.error("Group %s has no bays assigned" % group)
            raise gcmd.error("Group %s does not exist" % group)
        requested_slot = gcmd.get_int("SLOT", None, minval=0)
        if requested_slot is not None:
            # Bypasses group-wide bay selection for this exact bay; the group
            # is still resolved above so runout protection applies unchanged.
            short_name, bay = self._validate_load_target(
                gcmd, group, requested_slot)
            action = S.LoadBay(fps, unit=(self.oams[short_name].oams_idx, bay))
        else:
            action = S.Load(fps, group)
        self._wait_for_positioning(fps, gcmd)
        result = self.runtime.request(fps, action).wait()
        if result.ok or result.code == S.OAMS_OP_CODE_CANCEL:
            gcmd.respond_info(result.message)
        else:
            # Raise so toolchange macros stop instead of printing without
            # filament loaded.
            raise gcmd.error(result.message)

    def _run_toolhead_macro(self, name):
        self.printer.lookup_object("gcode").run_script_from_command(name)
        # A macro may only queue its extrusion moves. Do not begin autonomous
        # rewind (or report a completed load) until the toolhead queue is empty.
        self.printer.lookup_object("gcode").run_script_from_command("M400")

    def _wait_for_positioning(self, fps, gcmd):
        """Give a unit a few seconds to finish positioning a freshly inserted
        spool before a motion command reaches it (H16). The firmware answers
        BUSY meanwhile, so a toolchange running right after an insertion waits
        here instead of failing; when the wait is used up the reducer refuses
        with its own message."""
        units = self._positioning_units(fps)
        if not units:
            return
        deadline = self.reactor.monotonic() + getattr(
            self, "positioning_wait", 0.0)
        gcmd.respond_info("Waiting for OAMS %d to finish positioning a new"
                          " spool" % units[0].oams_idx)
        while self._positioning_units(fps):
            if self.reactor.monotonic() >= deadline:
                return
            self.reactor.pause(self.reactor.monotonic() + 0.2)

    def _follower_refusal(self, unit):
        """The firmware's readable refusal of the last follower request on a
        unit ((idx, bay) pair), or None."""
        oam = getattr(self, "oam_by_idx", {}).get(unit[0])
        return getattr(oam, "follower_refusal", None)

    def _wait_for_follower(self, fps, unit, direction, previous_lane, gcmd):
        """Wait for a fresh reducer confirmation of this exact follower."""
        reactor = self.reactor
        deadline = reactor.monotonic() + FOLLOW_CONFIRM_TIMEOUT
        while True:
            lane = self.runtime.get_state().lanes.get(fps)
            if lane is None or lane.unit != unit:
                raise gcmd.error("Lane %s changed while waiting for follower" % fps)
            refusal = self._follower_refusal(unit)
            if refusal:
                raise gcmd.error("Reverse follower on lane %s was refused:"
                                 " %s; toolhead unload was stopped"
                                 % (fps, refusal))
            if (lane is not previous_lane and lane.following is True
                    and lane.direction == direction
                    and unit not in lane.uncertain_follower_units):
                return
            if reactor.monotonic() >= deadline:
                raise gcmd.error("Reverse follower on lane %s was not confirmed;"
                                 " toolhead unload was stopped" % fps)
            reactor.pause(min(deadline, reactor.monotonic()
                              + FOLLOW_CONFIRM_POLL))

    cmd_LOAD_TO_TOOLHEAD_help = (
        "Load an AMS group, then run the configured extruder load macro")

    def cmd_LOAD_TO_TOOLHEAD(self, gcmd):
        # The firmware load owns every real-time edge and enters forward
        # following on success. Only after it has settled at the toolhead do we
        # ask the printer-specific macro to capture/prime it in the extruder.
        self.cmd_LOAD_FILAMENT(gcmd)
        try:
            self._run_toolhead_macro(self.toolhead_load_macro)
        except Exception as exc:
            # Do not unwind the successful AMS load: leaving the filament
            # loaded with its forward follower armed is safer than a blind
            # rewind against an unknown toolhead state.
            raise gcmd.error(
                "AMS load reached the toolhead, but %s failed: %s. Filament"
                " remains loaded and forward following remains armed."
                % (self.toolhead_load_macro, exc))

    cmd_UNLOAD_FROM_TOOLHEAD_help = (
        "Reverse-follow the toolhead retract, then autonomously rewind the AMS")

    def cmd_UNLOAD_FROM_TOOLHEAD(self, gcmd):
        self._require_motion_available(gcmd, self._command_unit_names(gcmd),
                                       stop_followers=True)
        if any(not getattr(oam, "hardware_snapshot_ready", True)
               for oam in getattr(self, "oams", {}).values()):
            self._stop_followers(self.fpss)
            self._require_hardware_snapshot(gcmd)
        fps = self._resolve_fps(gcmd, prefer_op=S.OP_LOADED, required=False)
        if fps is None:
            # Let the existing low-level handler produce the precise
            # no-loaded/ambiguous-lane response without moving the extruder.
            self.cmd_UNLOAD_FILAMENT(gcmd)
            return
        lane = self.runtime.get_state().lanes[fps]
        if lane.op != S.OP_LOADED or lane.unit is None:
            self.cmd_UNLOAD_FILAMENT(gcmd)
            return

        # Firmware owns the pressure edge response. The host only sequences the
        # coarse phases: reverse-follow while the extruder retracts, then issue
        # the autonomous rewind operation. Any callback failure must disarm the
        # reverse follower immediately so it cannot run away.
        unit = self.oam_by_idx.get(lane.unit[0]) if hasattr(
            self, "oam_by_idx") else None
        if not getattr(unit, "has_reverse_follower", True):
            # AMS_CAP_REVERSE_FOLLOWER=0: nothing pushes back while the
            # extruder retracts; the unit's own unload retracts after the
            # host's cut. No follower request and no confirmation wait.
            self._run_toolhead_macro(self.toolhead_unload_macro)
            self.cmd_UNLOAD_FILAMENT(gcmd)
            return
        previous_lane = self.runtime.get_state().lanes.get(fps)
        self.runtime.dispatch(S.Follow(fps, 1, S.FOLLOWER_REVERSE))
        try:
            self._wait_for_follower(fps, lane.unit, S.FOLLOWER_REVERSE,
                                    previous_lane, gcmd)
            self._run_toolhead_macro(self.toolhead_unload_macro)
            self.cmd_UNLOAD_FILAMENT(gcmd)
        except Exception:
            self._stop_followers([fps])
            raise

    # ------------------------------------------------ runtime group editing
    # Create groups and reassign bays at runtime (for the UI). Each change
    # validates through the pure model, writes the [filament_group] sections of
    # the OpenAMS config file in place, and then swaps the live model — so the
    # change takes effect immediately AND survives a restart, with no
    # SAVE_CONFIG (which can only rewrite printer.cfg, not an included subfile).
    # Edits are refused on a lane that is mid-op or printing from the affected
    # group, so the model cannot change underneath an in-flight operation.

    def _assert_editable(self, lanes, groups=()):
        """Refuse edits on a busy/runout lane, and refuse changing the
        membership of any group in `groups` that is currently loaded — the
        runout auto-reload picks its spare from the loaded group's bays, so
        mutating it mid-print would silently change what gets loaded next."""
        state = self.runtime.get_state()
        for lane in lanes:
            ls = state.lanes.get(lane)
            if ls is None:
                continue
            if ls.op in (S.OP_LOADING, S.OP_UNLOADING, S.OP_CALIBRATING):
                raise self.printer.command_error(
                    "Cannot edit filament groups while lane %s is busy (%s)."
                    % (lane, ls.op))
            if ls.op == S.OP_LOADED and ls.runout != S.RUNOUT_IDLE:
                raise self.printer.command_error(
                    "Cannot edit filament groups while lane %s is handling a"
                    " runout." % lane)
            if ls.op == S.OP_LOADED and ls.group and ls.group in groups:
                raise self.printer.command_error(
                    "Cannot edit group '%s' while it is loaded on lane %s;"
                    " unload it first." % (ls.group, lane))

    def _persist_and_apply(self, new_topo):
        """Persist the difference between the current and new topology to the
        config file, then swap in the new model. File first: if the write
        fails the running model is left untouched, so saved and live state can
        never diverge."""
        old = self.topo
        edits = []
        must_exist = []
        for g in new_topo.groups:
            if old.groups.get(g) != new_topo.groups[g]:
                edits.append((g, new_topo.group_config_value(g)))
                if g in old.groups:
                    must_exist.append(g)   # pre-existing group: update in place
        for g in old.groups:
            if g not in new_topo.groups:
                edits.append((g, None))           # delete the section
                must_exist.append(g)

        def transform(text):
            # A pre-existing group whose section is NOT in this file lives in
            # another config file (e.g. printer.cfg). Appending a copy here
            # would create a duplicate section; Klipper merges duplicates with
            # later-wins semantics, so the edit would silently apply-or-not
            # depending on include order — refuse with a pointer at the knob.
            for g in must_exist:
                if not oams_config_io.has_group(text, g):
                    raise self.printer.command_error(
                        "[filament_group %s] is not defined in '%s'; move the"
                        " section into that file or point [oams_manager]"
                        " openams_config_path at the file that holds it."
                        % (g, self.openams_config_path))
            return oams_config_io.apply_group_edits(text, edits)

        self._rewrite_config(transform)
        self.topo = new_topo
        self._rebuild_derived()

    def persist_config_option(self, section, option, value):
        """Set one option in one section of the OpenAMS config file in place
        (used by calibration writeback). Like group edits, this targets the
        included subfile SAVE_CONFIG cannot reach. Refuses (rather than
        appending a duplicate section) when the section lives in a different
        file. Raises command_error on any failure."""
        def transform(text):
            if not oams_config_io.has_section(text, section):
                raise self.printer.command_error(
                    "[%s] is not defined in '%s'; move the section into that"
                    " file or point [oams_manager] openams_config_path at the"
                    " file that holds it." % (section, self.openams_config_path))
            return oams_config_io.set_option(text, section, option, value)

        self._rewrite_config(transform)

    def remove_config_option(self, section, option):
        """Remove one option from one section of the OpenAMS config file in
        place (the inverse of persist_config_option, used when a bay's spool
        link is cleared). Refuses, exactly like persist_config_option, when the
        section lives in a different file rather than appending a duplicate."""
        def transform(text):
            if not oams_config_io.has_section(text, section):
                raise self.printer.command_error(
                    "[%s] is not defined in '%s'; move the section into that"
                    " file or point [oams_manager] openams_config_path at the"
                    " file that holds it." % (section, self.openams_config_path))
            return oams_config_io.remove_option(text, section, option)

        self._rewrite_config(transform)

    def _rewrite_config(self, transform):
        """Atomically read -> transform -> write the OpenAMS config file."""
        path = self.openams_config_path
        try:
            with open(path) as f:
                text = f.read()
        except OSError as e:
            raise self.printer.command_error(
                "Cannot read OpenAMS config '%s' to persist the change"
                " (set [oams_manager] openams_config_path): %s" % (path, e))
        new_text = transform(text)
        tmp = path + ".tmp"
        try:
            with open(tmp, "w") as f:
                f.write(new_text)
            os.replace(tmp, path)                 # atomic
        except OSError as e:
            raise self.printer.command_error(
                "Cannot write OpenAMS config '%s': %s" % (path, e))

    cmd_CREATE_GROUP_help = "Create a new (empty) filament group at runtime"

    def cmd_CREATE_GROUP(self, gcmd):
        name = gcmd.get("GROUP")
        try:
            topo = T.with_group(self.topo, name)
        except T.TopologyError as e:
            raise gcmd.error(str(e))
        self._persist_and_apply(topo)
        gcmd.respond_info("Created group '%s' (saved to %s)."
                          % (name, self.openams_config_path))

    cmd_DELETE_GROUP_help = "Delete a filament group at runtime"

    def cmd_DELETE_GROUP(self, gcmd):
        name = gcmd.get("GROUP")
        if name not in self.topo.groups:
            raise gcmd.error("filament_group '%s' does not exist." % name)
        self._assert_editable([self.topo.lane_of_group(name)], groups=(name,))
        self._persist_and_apply(T.without_group(self.topo, name))
        gcmd.respond_info("Deleted group '%s' (saved to %s)."
                          % (name, self.openams_config_path))

    cmd_ASSIGN_BAY_help = "Assign an OAMS bay to a filament group at runtime"

    def cmd_ASSIGN_BAY(self, gcmd):
        group = gcmd.get("GROUP")
        oams_name = gcmd.get("OAMS")
        if oams_name not in self.oams:
            raise gcmd.error("Unknown OAMS '%s'." % oams_name)
        bay = gcmd.get_int(
            "BAY", minval=0, maxval=self.topo.bays_of(oams_name) - 1)
        affected = {self.topo.lane_of_oams(oams_name)}
        # Both groups whose membership changes are protected while loaded: the
        # destination AND the donor (the group the bay is being moved out of).
        touched = {group}
        donor = next((g for g, bays in self.topo.groups.items()
                      if (oams_name, bay) in bays), None)
        if donor is not None:
            touched.add(donor)
        for g in touched:
            if g in self.topo.groups:
                gl = self.topo.lane_of_group(g)
                if gl is not None:
                    affected.add(gl)
        self._assert_editable(affected, groups=touched)
        try:
            topo = T.with_bay(self.topo, group, oams_name, bay)
        except T.TopologyError as e:
            raise gcmd.error(str(e))
        self._persist_and_apply(topo)
        gcmd.respond_info("Assigned %s-%d to '%s' (saved to %s)."
                          % (oams_name, bay, group, self.openams_config_path))

    cmd_UNASSIGN_BAY_help = "Remove an OAMS bay from a filament group at runtime"

    def cmd_UNASSIGN_BAY(self, gcmd):
        group = gcmd.get("GROUP")
        oams_name = gcmd.get("OAMS")
        if oams_name not in self.oams:
            raise gcmd.error("Unknown OAMS '%s'." % oams_name)
        bay = gcmd.get_int(
            "BAY", minval=0, maxval=self.topo.bays_of(oams_name) - 1)
        if group not in self.topo.groups:
            raise gcmd.error("filament_group '%s' does not exist." % group)
        self._assert_editable([self.topo.lane_of_group(group)], groups=(group,))
        try:
            topo = T.without_bay(self.topo, group, oams_name, bay)
        except T.TopologyError as e:
            raise gcmd.error(str(e))
        self._persist_and_apply(topo)
        gcmd.respond_info("Removed %s-%d from '%s' (saved to %s)."
                          % (oams_name, bay, group, self.openams_config_path))

    cmd_SET_BAY_SPOOL_help = "Link a Spoolman spool to an OAMS bay"

    def cmd_SET_BAY_SPOOL(self, gcmd):
        # A spool link is metadata, not motion, so it is allowed at any time
        # (including on a lane's loaded bay). A spool may be linked to only one
        # bay: any other bay already claiming SPOOL is cleared first.
        name = gcmd.get("OAMS")
        if name not in self.oams:
            raise gcmd.error("Unknown OAMS '%s'." % name)
        bay = gcmd.get_int(
            "BAY", minval=0, maxval=self.topo.bays_of(name) - 1)
        spool = gcmd.get_int("SPOOL", minval=1)
        source = gcmd.get("SOURCE", "manual").lower()
        if source not in ("tag", "manual"):
            raise gcmd.error("SOURCE must be 'tag' or 'manual'.")
        moved = [(other_name, b)
                 for other_name, other in self.oams.items()
                 for b in range(self.topo.bays_of(other_name))
                 if (other_name, b) != (name, bay)
                 and other.bay_link(b)[0] == spool]
        previous = self.oams[name].bay_link(bay)[0]
        # Persist first, then apply (like group edits): a refused write
        # leaves the in-memory links matching the file.
        self.persist_config_option(
            "oams %s" % name, "spool_id_%d" % bay, "%d" % spool)
        self.persist_config_option(
            "oams %s" % name, "link_source_%d" % bay, source)
        for other_name, b in moved:
            self.remove_config_option(
                "oams %s" % other_name, "spool_id_%d" % b)
            self.remove_config_option(
                "oams %s" % other_name, "link_source_%d" % b)
        for other_name, b in moved:
            self.oams[other_name].set_bay_link(b, None, None)
            # The pushed filament described the spool that left, so it goes
            # with it; the component pushes the new bay's values (C3 §4).
            self.oams[other_name].set_bay_filament(b, None, None, None)
        self.oams[name].set_bay_link(bay, spool, source)
        if previous != spool:
            self.oams[name].set_bay_filament(bay, None, None, None)
        message = "Linked spool %d to %s (%s)." % (
            spool, S.spool_name(name, bay), source)
        for other_name, b in moved:
            message += " Moved from %s." % S.spool_name(other_name, b)
        gcmd.respond_info(message)

    cmd_CLEAR_BAY_SPOOL_help = "Clear the spool link of an OAMS bay"

    def cmd_CLEAR_BAY_SPOOL(self, gcmd):
        name = gcmd.get("OAMS")
        if name not in self.oams:
            raise gcmd.error("Unknown OAMS '%s'." % name)
        bay = gcmd.get_int(
            "BAY", minval=0, maxval=self.topo.bays_of(name) - 1)
        self.remove_config_option("oams %s" % name, "spool_id_%d" % bay)
        self.remove_config_option("oams %s" % name, "link_source_%d" % bay)
        self.oams[name].set_bay_link(bay, None, None)
        self.oams[name].set_bay_filament(bay, None, None, None)
        gcmd.respond_info("Cleared the spool link of %s."
                          % S.spool_name(name, bay))

    cmd_SET_BAY_FILAMENT_help = (
        "Set what an OAMS bay holds of filament, in memory only "
        "(MATERIAL=<text> TEMP=<150-400> SOURCE=spool|table)")

    def cmd_SET_BAY_FILAMENT(self, gcmd):
        # The working copy openams_spoolman pushes (C3 §4). Metadata, not
        # motion, so it is allowed at any time; nothing is persisted, so a
        # restart leaves the bay unknown until the component pushes again.
        name = gcmd.get("OAMS")
        if name not in self.oams:
            raise gcmd.error("Unknown OAMS '%s'." % name)
        bay = gcmd.get_int(
            "BAY", minval=0, maxval=self.topo.bays_of(name) - 1)
        temp = self._bay_filament_temp(gcmd)
        material = gcmd.get("MATERIAL", None)
        if material is not None:
            material = material.strip() or None
        source = gcmd.get("SOURCE", "spool").lower()
        if source not in ("spool", "table"):
            raise gcmd.error("SOURCE must be 'spool' or 'table'.")
        if temp is None and material is None:
            self.oams[name].set_bay_filament(bay, None, None, None)
            gcmd.respond_info("Cleared the filament of %s."
                              % S.spool_name(name, bay))
            return
        self.oams[name].set_bay_filament(bay, material, temp, source)
        gcmd.respond_info("Set %s to %s at %s C (%s)." % (
            S.spool_name(name, bay), material or "unknown material",
            "?" if temp is None else temp, source))

    @staticmethod
    def _bay_filament_temp(gcmd):
        """TEMP= as an int, None when absent or 0 (which means "none").

        gcmd.get_int has no way to say "absent", and 0 is outside the range
        the host publishes, so it is read and checked here (C3 §4).
        """
        raw = gcmd.get("TEMP", None)
        if raw is None:
            return None
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise gcmd.error("TEMP must be an integer, got '%s'." % raw)
        if value == 0:
            return None
        if not 150 <= value <= 400:
            raise gcmd.error("TEMP must be between 150 and 400, got %d."
                             % value)
        return value

    cmd_SET_TRAY_INFO_help = (
        "Write a bay's filament setting into a unit that stores one"
        " (OAMS=<unit> BAY=<n> [SPOOL=<id>] or CODE= MATERIAL= COLOR= MIN="
        " MAX=)")

    _TRAY_FIELDS = ("CODE", "MATERIAL", "COLOR", "MIN", "MAX")

    def cmd_SET_TRAY_INFO(self, gcmd):
        # C3 §4: metadata, not motion, so allowed at any time. The unit keeps
        # what is written (it is the setting it reports for a spool whose tag
        # it cannot read); nothing is persisted on the host.
        name = gcmd.get("OAMS")
        if name not in self.oams:
            raise gcmd.error("Unknown OAMS '%s'." % name)
        bay = gcmd.get_int(
            "BAY", minval=0, maxval=self.topo.bays_of(name) - 1)
        unit = self.oams[name]
        label = S.spool_name(name, bay)
        if getattr(unit, "ams_set_tray_info_cmd", None) is None:
            raise gcmd.error("%s cannot store a filament setting." % name)
        explicit = [key for key in self._TRAY_FIELDS
                    if gcmd.get(key, None) is not None]
        spool = gcmd.get("SPOOL", None)
        if explicit and spool is not None:
            raise gcmd.error("Give SPOOL= or %s, not both."
                             % ", ".join(k + "=" for k in self._TRAY_FIELDS))
        if not explicit:
            self._tray_info_from_spool(gcmd, name, bay, label, spool)
            return
        code, material, rgba, low, high = self._tray_info_fields(gcmd, unit)
        gcode = self.printer.lookup_object("gcode")

        def done(write, label=label):
            # Only the completion of an admitted write is news here; a
            # refusal was already the command's own error.
            if write["admission"] != "accepted":
                return
            respond = getattr(gcode, "respond_info", None)
            if respond is None:
                return
            if write["result"] == "written":
                respond("Stored the filament setting of %s." % label)
            else:
                respond("%s did not store the filament setting (%s)."
                        % (label, write["result"]))

        try:
            result = unit.tray_write_now(bay, code, material, rgba, low, high,
                                         on_done=done)
        except self.printer.command_error as error:
            raise gcmd.error(str(error))
        if result == "invalid_field":
            # Permanent (C1 §4.7.2): the unit cannot store this code or
            # material, so sending it again would be refused again.
            raise gcmd.error("%s refused the filament setting: invalid_field"
                             " (it cannot store this code or material; nothing"
                             " was written)." % label)
        if result == "timeout":
            raise gcmd.error("%s did not answer the filament setting write."
                             % label)
        if result != "accepted":
            raise gcmd.error("%s refused the filament setting: %s."
                             % (label, result))
        gcmd.respond_info(
            "Writing %s %s #%08X %d-%d C to %s." % (
                code or "(no code)", material, rgba, low, high, label))

    def _tray_info_fields(self, gcmd, unit):
        """The explicit form's fields, checked (C3 §4)."""
        missing = [key for key in ("MATERIAL", "MIN", "MAX")
                   if gcmd.get(key, None) is None]
        if missing:
            raise gcmd.error("Missing %s: the explicit form needs MATERIAL=,"
                             " MIN= and MAX= (CODE= and COLOR= optional)."
                             % ", ".join(k + "=" for k in missing))
        code = gcmd.get("CODE", "").strip()
        material = gcmd.get("MATERIAL").strip()
        for value, field, limit in ((code, "CODE", 6),
                                    (material, "MATERIAL", 8)):
            try:
                unit.tray_write_text(value, limit, field,
                                     required=field == "MATERIAL")
            except ValueError as error:
                raise gcmd.error("%s." % error)
        if " " in code:
            raise gcmd.error("CODE must not contain spaces.")
        if not code:
            # A unit's preset needs a code: without CODE= take the generic
            # one for the material, as the Spoolman sync does.
            code = generic_filament_code(material)
        rgba = self._tray_color(gcmd.get("COLOR", None), gcmd)
        low = gcmd.get_int("MIN", minval=TRAY_TEMP_MIN_C,
                           maxval=TRAY_TEMP_MAX_C)
        high = gcmd.get_int("MAX", minval=TRAY_TEMP_MIN_C,
                            maxval=TRAY_TEMP_MAX_C)
        if low > high:
            raise gcmd.error("MIN (%d) must not be above MAX (%d)."
                             % (low, high))
        return code, material, rgba, low, high

    @staticmethod
    def _tray_color(raw, gcmd):
        """COLOR=RRGGBB (opaque) or RRGGBBAA as 0xRRGGBBAA; absent is 0, no
        colour."""
        if raw is None:
            return 0
        text = raw.strip().lstrip("#")
        if len(text) not in (6, 8) or any(
                c not in "0123456789abcdefABCDEF" for c in text):
            raise gcmd.error("COLOR must be RRGGBB or RRGGBBAA in hex, got"
                             " '%s'." % raw)
        if len(text) == 6:
            text += "FF"
        return int(text, 16)

    def _tray_info_from_spool(self, gcmd, name, bay, label, spool):
        """SPOOL=<id>, or no field at all for the bay's linked spool: the
        filament comes from Spoolman, which only openams_spoolman reads, so
        the write is handed to it (C4 §7.5) and reported there."""
        if spool is None:
            spool_id = self.oams[name].bay_link(bay)[0]
            if spool_id is None:
                raise gcmd.error(
                    "%s has no linked spool: give SPOOL=<id>, or CODE=,"
                    " MATERIAL=, COLOR=, MIN= and MAX=." % label)
        else:
            try:
                spool_id = int(spool)
            except ValueError:
                raise gcmd.error("SPOOL must be a Spoolman spool id, got '%s'."
                                 % spool)
            if spool_id < 1:
                raise gcmd.error("SPOOL must be a Spoolman spool id, got '%s'."
                                 % spool)
        webhooks = self.printer.lookup_object("webhooks")
        try:
            webhooks.call_remote_method(TRAY_WRITE_REMOTE_METHOD, unit=name,
                                        bay=bay, spool_id=spool_id)
        except Exception as error:
            raise gcmd.error(
                "openams_spoolman is not available to read spool %d (%s);"
                " give CODE=, MATERIAL=, COLOR=, MIN= and MAX= instead."
                % (spool_id, error))
        gcmd.respond_info("Asked openams_spoolman to write spool %d's"
                          " filament to %s." % (spool_id, label))

    cmd_SET_OPTION_help = ("Set an OpenAMS policy option and persist it"
                           " (NAME=<setting> VALUE=<bool>)")

    def cmd_SET_OPTION(self, gcmd):
        # C3 §4: one of C2 §4.9's six switches, persisted to the
        # [oams_manager] section. Allowed at any time (it moves no filament).
        name = gcmd.get("NAME")
        if name not in SETTING_DEFAULTS:
            raise gcmd.error("Unknown OpenAMS setting '%s'" % name)
        raw = gcmd.get("VALUE").strip().lower()
        if raw in ("1", "true", "on"):
            value = True
        elif raw in ("0", "false", "off"):
            value = False
        else:
            raise gcmd.error(
                "VALUE must be one of 1/0/true/false/on/off, got '%s'"
                % gcmd.get("VALUE"))
        text = "True" if value else "False"
        self.persist_config_option("oams_manager", name, text)
        self.settings[name] = value
        gcmd.respond_info("Set %s to %s (saved to %s)."
                          % (name, text, self.openams_config_path))


def load_config(config):
    return OAMSManager(config)
