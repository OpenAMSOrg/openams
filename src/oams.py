# OpenAMS unit driver
#
# Copyright (C) 2025-2026 JR Lomas <lomas.jr@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
# OAMS is a thin hardware driver for one OpenAMS unit: it sends firmware
# commands, mirrors telemetry, and reports operation completion. All system-level
# orchestration (what is loaded, runout, follower policy) lives in the store
# (oams_state.py) driven by the runtime (oams_runtime.py). When bound to a
# runtime, every firmware action-status reply is turned into an OpCompleted
# action; otherwise the blocking helpers below provide a standalone fallback for
# the low-level OAMS_* commands.

import logging
import struct
from collections import deque

import mcu

from . import oams_state as S
from . import ams_family
from . import oams_ext
from . import oams_tags
from .oams_state import (
    OAMS_OP_CODE_SUCCESS,
    OAMS_OP_CODE_ERROR_UNSPECIFIED,
    OAMS_OP_CODE_ERROR_BUSY,
    OAMS_OP_CODE_SPOOL_ALREADY_IN_BAY,
    OAMS_OP_CODE_NO_SPOOL_IN_BAY,
    OAMS_OP_CODE_ERROR_KLIPPER_CALL,
    OAMS_OP_CODE_CANCEL,
    OAMS_OP_CODE_TIMEOUT,
    FOLLOWER_REVERSE,
    FOLLOWER_FORWARD,
    POLL_INTERVAL,
    OAMS_ACTION_TIMEOUT,
)

# OAMS ptfe_length / encoder telemetry are in encoder clicks; this factor
# converts clicks to filament millimetres. It is a property of the OAMS
# hardware, so it lives here in the driver — the pure reducer reasons in mm
# only (LaneWorld.path_len).
FILAMENT_PATH_LENGTH_FACTOR = 1.14

# The PTFE length register is a uint32 on the wire (config_oams_ptfe length=%u)
# and 0 means "not calibrated". Those are the only bounds the host can state
# from the wire contract, so a load is refused unless the bay's length is in
# (0, PTFE_LENGTH_MAX_CLICKS] — H10's driver-layer mechanism (the reducer
# refuses first, with a user-facing message).
PTFE_LENGTH_MAX_CLICKS = 4294967295

# Firmware "action" identifiers reported by oams_action_status. These are the
# BUILT-IN FALLBACK values: when the firmware publishes the enum in the data
# dictionary (OAMS_PROTOCOL_VERSION >= 1) the resolved values are read at
# connect (see _resolve_protocol); on older firmware these defaults are used.
OAMS_STATUS_LOADING = 0
OAMS_STATUS_UNLOADING = 1
OAMS_STATUS_FORWARD_FOLLOWING = 2
OAMS_STATUS_REVERSE_FOLLOWING = 3
OAMS_STATUS_COASTING = 4
OAMS_STATUS_STOPPED = 5
OAMS_STATUS_CALIBRATING = 6
OAMS_STATUS_ERROR = 7

# Lowest firmware protocol version this plugin understands. The host also runs
# against firmware that publishes no version at all (legacy mode), so this is
# only a forward-compatibility floor, not a hard requirement.
MIN_PROTOCOL_VERSION = 1

# Highest protocol version this host speaks. A unit publishing a newer version
# may use result codes and statuses this plugin does not know, so H9 refuses it
# (no motion, a named reason) rather than misreading its responses.
MAX_PROTOCOL_VERSION = 3

# Oldest capability schema this host has code for. The schema is additive, so a
# higher number still describes this host's fields; a lower one may describe
# them differently, which H9 refuses.
MIN_CAPABILITY_SCHEMA = 4

# Smallest host-liveness window a unit may ask the host to meet. Klipper's
# ~1 s clock sync must fit at least twice inside the window.
MIN_HOST_WATCHDOG_MS = 2000

# F3: how often the environment (RH, temperature) query is sent. It changes on
# the scale of minutes; the per-cycle telemetry sequence stays untouched.
ENVIRONMENT_POLL_S = 5.0

# From this protocol version the firmware owns per-op liveness: it runs its own
# no-progress watchdog, stops the motors on a stall, and completes the op with
# code OAMS_OP_CODE_TIMEOUT. The host then drops its authoritative op deadline
# and its own stall detection (see Runtime.set_firmware_liveness).
LIVENESS_PROTOCOL_VERSION = 3
# Protocol v4 carries the reducer's complete 32-bit operation generation and
# names both the action and the generation when cancelling a load or unload.
# No released firmware speaks it: the family stays dormant until a firmware
# advertises every v4 shape (and MAX_PROTOCOL_VERSION admits it).
FULL_GEN_PROTOCOL_VERSION = 4

# Only the action enum is resolved dynamically from the dictionary (see
# _resolve_protocol): it is consumed solely here in the driver. The op-code and
# follower-direction enums are shared with the pure reducer (which cannot read
# the dictionary), so those are VALIDATED against the published values rather
# than substituted.

# (published firmware name, host module value) for the enums that flow into the
# pure reducer and must therefore match exactly. The firmware renamed CANCEL ->
# CANCEL_LOAD_SPOOL but kept the value (6).
_VALIDATED_ENUM_NAMES = (
    ("OAMS_OP_CODE_SUCCESS", OAMS_OP_CODE_SUCCESS),
    ("OAMS_OP_CODE_ERROR_UNSPECIFIED", OAMS_OP_CODE_ERROR_UNSPECIFIED),
    ("OAMS_OP_CODE_ERROR_KLIPPER_CALL", OAMS_OP_CODE_ERROR_KLIPPER_CALL),
    ("OAMS_OP_CODE_CANCEL_LOAD_SPOOL", OAMS_OP_CODE_CANCEL),
    ("OAMS_OP_CODE_TIMEOUT", OAMS_OP_CODE_TIMEOUT),
    ("OAMS_FOLLOWER_REVERSE", FOLLOWER_REVERSE),
    ("OAMS_FOLLOWER_FORWARD", FOLLOWER_FORWARD),
)

# PTFE calibration failure messages (below) report this firmware-side bound so
# the user knows the failed calibration ran to firmware's own timeout, not a
# host guess.
OAMS_FIRMWARE_OPERATION_TIMEOUT_SECONDS = 60


class FollowerResult(object):
    """What `set_oams_follower` returns for a request the firmware refused:
    the runtime records the follower as not running (never as confirmed) and
    callers raise `reason`."""

    follower_confirmed = True

    def __init__(self, refused=False, reason=None):
        self.follower_refused = refused
        self.reason = reason


class OAMS(oams_ext.UnitHooks):
    # Class-level defaults so a bare instance (and every pre-v4 code path)
    # behaves as it did before the full-width family existed.
    _use_wide_gen_protocol = False
    _active_action = None
    _active_gen = None
    oams_load_spool4_cmd = None
    oams_unload_spool4_cmd = None
    # Host-confirmed toolhead arrival (C1 AMS_CAP_HOST_ARRIVAL_CONFIRM).
    oams_load_arrived_cmd = None
    _arrival_sent_gen = None
    # The firmware's last follower refusal (C1 §4.2) as a readable reason.
    follower_refusal = None
    _follower_reply = None
    # Link status of a device a relaying firmware serves (C1
    # AMS_CAP_LINK_STATUS). None until the firmware has said; only an explicit
    # False means the device is unreachable.
    link_reachable = None
    ams_get_link_status_cmd = None
    # A unit that reads and decodes its spools' tags itself (C1 section 4.7.1,
    # AMS_CAP_TRAY_INFO / AMS_CAP_TRAY_READ): the per-bay summary it last
    # reported, and the two commands. None/empty without the capability.
    ams_tray_read_cmd = None
    ams_get_tray_info_cmd = None
    # A unit that stores a per-bay filament setting (C1 section 4.7.2,
    # AMS_CAP_TRAY_WRITE): the write command, and bay -> the write in flight.
    ams_set_tray_info_cmd = None
    _tray_writes = None
    _tray_write_token = 0
    oams_calibrate_ptfe_length4_cmd = None
    oams_calibrate_hub_hes4_cmd = None
    oams_cancel4_cmd = None

    def __init__(self, config):
        self.printer = config.get_printer()
        self.name = config.get_name()              # full section, e.g. "oams oams1"
        self.reactor = self.printer.get_reactor()
        self.mcu = mcu.get_printer_mcu(self.printer, config.get("mcu", "mcu"))
        # Family controls topology and UI shape. "auto" preserves old AMS1
        # configs before connect, then resolves from firmware-published facts.
        try:
            self.configured_family = ams_family.normalize_family(
                config.get("device_kind", "auto"))
        except ValueError as e:
            raise config.error(str(e))
        self.capabilities = ams_family.profile(self.configured_family)
        self.device_family = self.capabilities["family"]
        self.bay_count = self.capabilities["bays"]
        self.firmware_constants = {}
        self.capability_warnings = []
        # Host fallbacks for firmware without the identity and environment
        # queries (the STM32F072 mainboard): the unit MCU's CAN UUID is its
        # serial, and an optional [oams] environment_sensor names the host
        # [hdc1080] sensor object that supplies the ambience.
        self.environment_sensor_name = config.get("environment_sensor", None)
        self.canbus_uuid = self._read_canbus_uuid(config)


        # FPS lane this unit belongs to (short name). Optional: the manager
        # defaults it to the sole lane when only one FPS exists.
        self.fps_name = config.get("fps", None)

        self.fps_upper_threshold = config.getfloat("fps_upper_threshold")
        self.fps_lower_threshold = config.getfloat("fps_lower_threshold")
        self.fps_is_reversed = config.getboolean("fps_is_reversed")

        self.f1s_hes_on = self._thresholds(config, "f1s_hes_on")
        self.f1s_hes_is_above = config.getboolean("f1s_hes_is_above")
        self.hub_hes_on = self._thresholds(config, "hub_hes_on")
        self.hub_hes_is_above = config.getboolean("hub_hes_is_above")
        # 0 is allowed so a fresh machine can boot and run
        # OAMS_CALIBRATE_PTFE_LENGTH; the runout logic guards an uncalibrated
        # (<= 0) length rather than rejecting it here.
        self.filament_path_length = config.getfloat("ptfe_length")
        self._configure_ptfe_lengths(config)
        self._configure_spool_links(config)
        self.oams_idx = config.getint("oams_idx")

        self.kd = config.getfloat("kd", 0.0)
        self.ki = config.getfloat("ki", 0.0)
        self.kp = config.getfloat("kp", 6.0)
        self.current_kp = config.getfloat("current_kp", 0.375)
        self.current_ki = config.getfloat("current_ki", 0.0)
        self.current_kd = config.getfloat("current_kd", 0.0)
        self.fps_target = config.getfloat(
            "fps_target", 0.5, minval=0.0, maxval=1.0,
            above=self.fps_lower_threshold, below=self.fps_upper_threshold)
        self.current_target = config.getfloat(
            "current_target", 0.3, minval=0.1, maxval=0.4)

        # Index of the spool currently loaded through the hub (0..3), or None.
        # This is the driver's own firmware mirror, kept in step with the
        # commands this unit issues (see _apply_action_status).
        self.current_spool = None
        self._pending_bay = None

        # Protocol contract resolved at connect (see _resolve_protocol).
        self.protocol_version = None
        # H9: non-None when the connect-time guard refused this unit, and the
        # window the firmware asked the host to meet (None when unpublished).
        self.connect_refusal = None
        # H9c: non-None once the unit's host-communication watchdog fired;
        # the unit deconfigured itself and only FIRMWARE_RESTART recovers it.
        self.session_lost = None
        self.host_watchdog_ms = None
        # H9d: reactor time of the last unsolicited stats frame from this unit
        # (C1 §6.2). Silence longer than the unit's host watchdog marks it
        # unreachable; None until the first frame.
        self.last_stats_time = None
        # Action-enum values used to interpret oams_action_status; start at the
        # module fallbacks, overwritten from the dictionary when published.
        self.status_loading = OAMS_STATUS_LOADING
        self.status_unloading = OAMS_STATUS_UNLOADING
        self.status_calibrating = OAMS_STATUS_CALIBRATING
        self.status_error = OAMS_STATUS_ERROR

        # Generation-matched protocol (oams_cmd_*2 / oams_action_status2). When
        # the firmware advertises the *2 commands, the gen rides on the wire and
        # the firmware echoes it, so completions are matched authoritatively.
        self._use_gen_protocol = False
        # Legacy fallback: FIFO of op_gen values for ops whose completion has
        # not arrived yet. The firmware answers ops in order and (pre-*2) does
        # not echo a correlation id, so pairing replies with the OLDEST pending
        # gen keeps a late reply from a timed-out op off its retry. Unused once
        # the *2 protocol is active.
        self._gen_queue = deque()

        # Firmware command handles (resolved at klippy:connect; None until then).
        self.oams_load_spool_cmd = None
        self.oams_unload_spool_cmd = None
        self.oams_load_spool_cancel_cmd = None
        self.oams_follower_cmd = None
        self.oams_calibrate_ptfe_length_cmd = None
        self.oams_calibrate_hub_hes_cmd = None
        self.oams_pid_cmd = None
        self.oams_current_pid_cmd = None
        self.oams_set_led_error_cmd = None
        self.oams_spool_query_spool_cmd = None
        # Generation-matched (*2) command handles; None unless the firmware
        # advertises them (set in handle_connect).
        self.oams_load_spool2_cmd = None
        self.oams_unload_spool2_cmd = None
        self.oams_calibrate_ptfe_length2_cmd = None
        self.oams_calibrate_hub_hes2_cmd = None
        # F9: the atomic load (programs the PTFE length and requests the load
        # in one command); looked up on its own so a firmware that advertises
        # the *2 set but not this one keeps the separate length write.
        self.oams_load_spool3_cmd = None
        self.ams_ht_dryer_start_cmd = None
        self.ams_ht_dryer_stop_cmd = None
        self.ams_ht_clear_fault_cmd = None
        # The shared AMS HT/AMS2 status commands are fire-and-report, not queries.
        # They are feature-detected after identify and polled round-robin only
        # when available; motion/follower control remains entirely in firmware.
        self._telemetry_commands = {}
        self._telemetry_timer = None
        self._telemetry_index = 0
        self._telemetry = {}
        # The 24-digit unit serial from the identity query, or None when the
        # firmware does not answer it.
        self.unit_serial = None
        # Startup state must not be inferred from zero-initialized HES arrays.
        # The manager waits for this flag before its one-time hardware resync.
        self.hardware_snapshot_ready = False

        # S4b: the RFID tag reader service. Built for every unit (an OpenAMS 1
        # mainboard's [mfrc522] hands its reads and removals into it, C2
        # §4.6; a unit that decodes its own tags reports them, C1 §4.7.1).
        self.tags = {}
        self.tag_reader = (self._ext_tag_reader_class()
                           or oams_tags.TagReader)(self)
        self._tray_writes = {}


        # Live telemetry.
        self.fps_value = 0.0
        self.i_value = 0.0
        self.encoder_clicks = 0
        self.f1s_hes_value = [0, 0, 0, 0]
        self.hub_hes_value = [0, 0, 0, 0]

        # Completion signalling. Only written on the reactor thread: the serial
        # reader thread marshals every action-status message onto the reactor via
        # register_async_callback (see _action_status_received), and the sentinel
        # action_status is published last so a waiter sees a settled code.
        self.action_status = None
        self.action_status_code = None
        self.action_status_value = None

        # Set by the manager via bind_runtime(); turns firmware replies into store
        # actions. None when running the low-level commands standalone.
        self.runtime = None
        self.on_action_complete = None

        self.mcu.register_serial_response(
            self._action_status_received,
            "oams_action_status action=%c code=%c value=%u")
        self.mcu.register_serial_response(
            self._oams_cmd_stats,
            "oams_cmd_stats fps_value=%u hub_hes_value_0=%c hub_hes_value_1=%c"
            " hub_hes_value_2=%c hub_hes_value_3=%c f1s_hes_value_0=%c"
            " f1s_hes_value_1=%c f1s_hes_value_2=%c f1s_hes_value_3=%c"
            " encoder_clicks=%u")
        self.mcu.register_serial_response(
            self._oams_cmd_current_stats,
            "oams_cmd_current_status current_value=%u")
        self.mcu.register_config_callback(self._build_config)

        self.register_commands()
        self.printer.register_event_handler("klippy:connect", self.handle_connect)

        self.printer.register_event_handler("klippy:ready",
                                            self._handle_ready)
        self.printer.register_event_handler("klippy:disconnect",
                                            self._handle_disconnect)
        self._ext_init(config)
    # ------------------------------------------------------- runtime binding

    @staticmethod
    def _thresholds(config, option):
        values = [float(x.strip()) for x in config.get(option).split(",")]
        if len(values) == 1:
            values *= 4
        if len(values) != 4:
            raise config.error("%s requires one value (single-bay AMS HT) or"
                               " four comma-separated values" % option)
        return values

    def bind_runtime(self, runtime, fps_name):
        """Called by the manager so firmware replies feed the store."""
        self.runtime = runtime
        self.fps_name = fps_name
        self.on_action_complete = (
            lambda code, value, gen: runtime.dispatch(
                S.OpCompleted(fps_name, code, value, gen=gen,
                              fault=(None if code == OAMS_OP_CODE_SUCCESS
                                     else self._latched_fault_text()))))

    def _latched_fault_text(self):
        """The unit's latched fault with its recovery hint, from the domain
        telemetry (C2 section 5.7); None when nothing is latched or known."""
        domain = (getattr(self, "_telemetry", None) or {}).get("domain") or {}
        names = []
        for kind in ("motion", "system", "dryer"):
            name = domain.get("%s_fault_name" % kind)
            if domain.get("%s_fault" % kind) and name and name != "none":
                names.append(name)
        if not names:
            return None
        return ("the unit reports fault %s; clear it with OAMS_CLEAR_FAULT"
                " OAMS=%s, then OAMSM_CLEAR_ERRORS"
                % (", ".join(names), self.oams_idx))

    # ------------------------------------------------------------------ status

    def get_status(self, eventtime):
        return self.get_webhook_status()

    @property
    def supports_native_operations(self):
        """True when exact 32-bit starts and generation-scoped cancellation
        are available (the complete protocol-v4 family was selected)."""
        return (self._use_wide_gen_protocol
                and self.protocol_version is not None
                and self.protocol_version >= FULL_GEN_PROTOCOL_VERSION)

    @property
    def firmware_owns_liveness(self):
        """True when this unit's firmware runs its own per-op no-progress
        watchdog (protocol >= 3), so the host need not impose its own deadline."""
        return (self.protocol_version is not None
                and self.protocol_version >= LIVENESS_PROTOCOL_VERSION)

    @property
    def connected(self):
        """True once the firmware commands resolved at connect."""
        return self.oams_load_spool_cmd is not None

    @property
    def path_length_mm(self):
        """Filament path from the hub HES to the extruder in millimetres (the
        unit LaneWorld.path_len carries). ptfe_length is calibrated in encoder
        clicks; 0 stays 0 so the uncalibrated-runout guard still fires."""
        return getattr(self, "_selected_path_length", self.filament_path_length) / FILAMENT_PATH_LENGTH_FACTOR

    def _configure_ptfe_lengths(self, config):
        # Missing bay overrides retain the existing shared-length behavior.
        self.ptfe_lengths = [config.getfloat(
            "ptfe_length_%d" % bay, None, minval=0., maxval=4294967295.)
            for bay in range(self.bay_count)]
        self._selected_path_length = self.filament_path_length
        self._selected_ptfe_bay = None

    def _configure_spool_links(self, config):
        """H3: the bay↔spool links persisted in this [oams ...] section
        (spool_id_<bay>, link_source_<bay>; C3 OAMSM_SET_BAY_SPOOL)."""
        links = []
        for bay in range(self.bay_count):
            spool_id = config.getint("spool_id_%d" % bay, None, minval=1)
            source = config.get("link_source_%d" % bay, None)
            if spool_id is None:
                # Unlinked bays ignore any stale link_source.
                links.append((None, "none"))
                continue
            if source is None:
                source = "manual"
            elif source not in ("tag", "manual"):
                raise config.error(
                    "link_source_%d must be 'tag' or 'manual'" % bay)
            links.append((spool_id, source))
        self.bay_links = links

    def bay_link(self, bay):
        """(spool_id, link_source) for one bay; (None, "none") when unlinked."""
        links = getattr(self, "bay_links", None)
        if links is None or not 0 <= bay < len(links):
            return (None, "none")
        return links[bay]

    def set_bay_link(self, bay, spool_id, source):
        """Set (spool_id >= 1, source "tag"/"manual") or clear (spool_id None)."""
        if not hasattr(self, "bay_links"):
            self.bay_links = [(None, "none")] * self.bay_count
        if spool_id is None:
            self.bay_links[bay] = (None, "none")
            return
        if spool_id < 1:
            raise ValueError("spool_id must be >= 1 or None")
        if source not in ("tag", "manual"):
            raise ValueError("link source must be 'tag' or 'manual'")
        self.bay_links[bay] = (spool_id, source)

    def bay_filament(self, bay):
        """The working copy of one bay's filament, or None: ``{"material",
        "temp_c", "source"}`` (C2 §4.5). Memory only (C3 §4): a restart
        forgets it and the component pushes it again."""
        values = getattr(self, "bay_filaments", None)
        if values is None or not 0 <= bay < len(values) or values[bay] is None:
            return None
        return dict(values[bay])

    def set_bay_filament(self, bay, material, temp_c, source):
        """Set what a bay holds (material and/or temp_c), or clear it.

        Never persisted: this is the operating copy ``openams_spoolman``
        pushes, so the temperatures survive only as long as Klipper does.
        """
        if not hasattr(self, "bay_filaments"):
            self.bay_filaments = [None] * self.bay_count
        if material is None and temp_c is None:
            self.bay_filaments[bay] = None
            return
        if source not in ("spool", "table"):
            raise ValueError("filament source must be 'spool' or 'table'")
        self.bay_filaments[bay] = {"material": material,
                                   "temp_c": temp_c, "source": source}

    def _ptfe_length_for_bay(self, bay):
        """The effective PTFE length (encoder clicks) for a bay: its own
        ptfe_length_<bay> override when set, else the shared ptfe_length. None
        when the unit declares neither (a bare/legacy driver has no length to
        send)."""
        lengths = getattr(self, "ptfe_lengths", [])
        value = lengths[bay] if 0 <= bay < len(lengths) else None
        if value is not None:
            return value
        return getattr(self, "filament_path_length", None)

    def protocol_summary(self):
        """One-line protocol description for SELFTEST."""
        parts = ["protocol=%s" % (self.protocol_version
                                  if self.protocol_version is not None
                                  else "legacy")]
        if self._use_gen_protocol:
            parts.append("gen-matched")
        if self._use_wide_gen_protocol:
            parts.append("32-bit")
        if self.firmware_owns_liveness:
            parts.append("fw-liveness")
        return ", ".join(parts)

    def is_bay_ready(self, bay_index):
        return bool(self.f1s_hes_value[bay_index])

    def spool_label(self, bay):
        """This unit's spool at `bay` as a sentence names it: "oams2 Spool 3"
        (the unit's config name, the 1-based position)."""
        return S.spool_name(self.name.split()[-1], bay)

    def bay_loadable(self, bay_index):
        """Whether the manager may attempt a LOAD on this bay. An AMS unit
        has its own f1s (BAY) sensor per bay."""
        return bool(self.f1s_hes_value[bay_index])

    def is_bay_loaded(self, bay_index):
        return bool(self.hub_hes_value[bay_index])

    def get_spool_status(self, bay_index):
        return self.f1s_hes_value[bay_index]

    def get_current(self):
        return self.i_value

    def stats(self, eventtime):
        return (False,
                "OAMS[%s]: current_spool=%s fps_value=%s"
                " f1s_hes_value_0=%d f1s_hes_value_1=%d f1s_hes_value_2=%d"
                " f1s_hes_value_3=%d hub_hes_value_0=%d hub_hes_value_1=%d"
                " hub_hes_value_2=%d hub_hes_value_3=%d kp=%d ki=%d kd=%d"
                " encoder_clicks=%d i_value=%.2f"
                % (self.oams_idx, self.current_spool, self.fps_value,
                   self.f1s_hes_value[0], self.f1s_hes_value[1],
                   self.f1s_hes_value[2], self.f1s_hes_value[3],
                   self.hub_hes_value[0], self.hub_hes_value[1],
                   self.hub_hes_value[2], self.hub_hes_value[3],
                   self.kp, self.ki, self.kd, self.encoder_clicks,
                   self.i_value))

    def _supported_actions(self):
        def op_command(legacy_name, matched_name):
            wide = {"oams_load_spool2_cmd": "oams_load_spool4_cmd",
                    "oams_unload_spool2_cmd": "oams_unload_spool4_cmd",
                    "oams_calibrate_ptfe_length2_cmd":
                        "oams_calibrate_ptfe_length4_cmd",
                    "oams_calibrate_hub_hes2_cmd":
                        "oams_calibrate_hub_hes4_cmd"}
            name = (wide[matched_name] if self._use_wide_gen_protocol
                    else matched_name if getattr(self, "_use_gen_protocol", False)
                    else legacy_name)
            return getattr(self, name, None)

        actions = ["clear_errors"]
        motion = self.capabilities.get("normal_motion", True)
        if motion and op_command("oams_load_spool_cmd", "oams_load_spool2_cmd") is not None:
            actions.append("load")
        if motion and op_command("oams_unload_spool_cmd", "oams_unload_spool2_cmd") is not None:
            actions.append("unload")
        if motion and getattr(self, "oams_follower_cmd", None) is not None:
            actions.append("follower")
        if (motion and self.device_family == "ams1"
                and getattr(self, "oams_pid_cmd", None) is not None
                and getattr(self, "oams_current_pid_cmd", None) is not None):
            actions.append("configure_pid")
        if (motion and self.capabilities.get("ptfe_calibration")
                and op_command("oams_calibrate_ptfe_length_cmd",
                               "oams_calibrate_ptfe_length2_cmd") is not None):
            actions.append("calibrate_ptfe")
        if (motion and self.capabilities.get("hub_hes_calibration")
                and op_command("oams_calibrate_hub_hes_cmd",
                               "oams_calibrate_hub_hes2_cmd") is not None):
            actions.append("calibrate_hub_hes")
        # Dryer actions are offered only by a unit whose firmware reports a
        # dryer; a dictionary that still lists the commands does not make a
        # dryer-less unit able to run one.
        dryer = bool(self.capabilities.get("dryer", True))
        if (dryer and self.capabilities.get("heater_control", True)
                and self.ams_ht_dryer_start_cmd is not None
                and self._adapter_status() is not False
                and not self._dryer_cycle_running()
                and self._dryer_blocked_by_load() is None):
            actions.append("dryer_start")
        if dryer and self.ams_ht_dryer_stop_cmd is not None:
            actions.append("dryer_stop")
        if self.ams_ht_clear_fault_cmd is not None:
            actions.append("clear_fault")
        if (getattr(self, "ams_get_tray_info_cmd", None) is not None
                and getattr(self, "ams_tray_read_cmd", None) is not None):
            # A unit that decodes its own tags: a read only if it takes one.
            actions.append("rfid_scan")
        return self._ext_actions(actions)

    @staticmethod
    def _length_mm(length):
        """A PTFE length in encoder clicks as millimetres; 0 when no positive
        length is configured (the C2 schema's `path_length_mm` is always a
        number, and `calibration.state` says "uncalibrated" in that case)."""
        if length is None or length <= 0:
            return 0.0
        return length / FILAMENT_PATH_LENGTH_FACTOR

    @staticmethod
    def _length_state(length):
        return ("configured" if length is not None and length > 0
                else "uncalibrated")

    def _calibration_status(self):
        """The unit's shared PTFE length state and length in millimetres."""
        length = getattr(self, "filament_path_length", None)
        state = self._length_state(length)
        if (state == "configured"
                and self.action_status == getattr(self, "status_calibrating", None)):
            state = "calibrating"
        return {"state": state, "path_length_mm": self._length_mm(length)}

    @staticmethod
    def _read_canbus_uuid(config):
        """The CAN UUID of this unit's MCU from its [mcu NAME] section, or
        None (a USB or serial MCU has none)."""
        try:
            mcu_name = config.get("mcu", "mcu")
            section = "mcu" if mcu_name == "mcu" else "mcu " + mcu_name
            uuid = config.getsection(section).get("canbus_uuid", None)
        except Exception:
            return None
        uuid = None if uuid is None else str(uuid).strip()
        return uuid or None

    _ENV_SENSOR_PREFIXES = ("", "hdc1080 ", "aht3x ", "temperature_sensor ")

    def _find_environment_sensor(self):
        """The host sensor object for the environment fallback, or None.

        `environment_sensor` names the printer object (a bare name also
        matches its `hdc1080`, `aht3x` or `temperature_sensor` object). With
        no option, the sole HDC1080 object is used; none or several leave the
        unit without a reading (several log once, since the choice is the
        owner's)."""
        printer = getattr(self, "printer", None)
        if printer is None:
            return None
        name = getattr(self, "environment_sensor_name", None)
        try:
            if name:
                for prefix in self._ENV_SENSOR_PREFIXES:
                    obj = printer.lookup_object(prefix + name, None)
                    if obj is not None:
                        return obj
                return None
            found = [obj for key, obj in printer.lookup_objects()
                     if type(obj).__name__ == "HDC1080"
                     or str(key).startswith("hdc1080 ")]
        except Exception:
            return None
        if len(found) > 1 and not getattr(self, "_env_ambiguous_logged", False):
            self._env_ambiguous_logged = True
            logging.warning(
                "OAMS[%s]: %d [hdc1080] sensors and no environment_sensor"
                " option; set environment_sensor to choose one",
                self.oams_idx, len(found))
        return found[0] if len(found) == 1 else None

    def _host_environment_status(self):
        """The environment from a host HDC1080 sensor, for a unit whose
        firmware has no environment query; None when there is no usable
        reading (a sensor that has not sampled, or that reports its dead
        0.0/0.0, is not a reading)."""
        sensor = self._find_environment_sensor()
        if sensor is None:
            return None
        try:
            reading = sensor.get_status(self.reactor.monotonic())
            temp = float(reading["temperature"])
            humidity = float(reading["humidity"])
        except Exception:
            return None
        if temp == 0.0 and humidity == 0.0:
            return None
        return {"temp_c": temp, "rh_pct": max(0.0, min(100.0, humidity)),
                "source": "hdc1080"}

    def _environment_status(self):
        """F3 (C1 §5.4): the unit's ambience, or None when it has no reading.
        Firmware without the environment query falls back to the host
        sensor."""
        if getattr(self, "_environment_cmd", None) is None:
            if "environment" not in self._telemetry:
                return self._host_environment_status()
        data = self._telemetry.get("environment")
        if not data or not data.get("valid"):
            return None
        return {
            "temp_c": data["temperature_centic"] / 100.0,
            "rh_pct": data["rh_centipercent"] / 100.0,
            "source": "firmware",
        }

    def _adapter_status(self):
        """H6 (C2 §4.7): whether the dryer's power adapter is connected;
        ``None`` where the unit does not report it."""
        return self._ext_adapter_status()

    def _dryer_status(self):
        """The v2 dryer projection, or None when the unit has no dryer."""
        if not self.capabilities.get("dryer"):
            return None
        data = self._telemetry.get("dryer", {})
        heater = data.get("heater_permille")
        fan = data.get("fan_permille")
        return {
            "state": data.get("state_name", "unknown"),
            "target_c": data.get("target_c"),
            "remaining_s": data.get("remaining_s"),
            "heater_pct": None if heater is None else heater / 10,
            "fan_pct": None if fan is None else fan / 10,
            "fault": data.get("fault_name", "none"),
            "adapter": self._adapter_status(),
        }

    def _note_f1s(self, values, now):
        """Store an F1S snapshot and remember a fresh spool presence edge.

        The firmware positions a freshly inserted spool on its own.  This runs
        on the receive path before the snapshot is published, comparing against
        the previous values so a 0 -> non-zero transition is recorded as the
        moment positioning may have started (H16). Every rise or fall also
        tells the RFID reader service (C2 §4.6, S4b) about an insertion or a
        removal; that scheduling defers itself off this thread.
        """
        # Serial thread: never raise here, or the unit's stats stop.
        previous = getattr(self, "f1s_hes_value", None) or []
        self.f1s_hes_value = list(values)
        if not getattr(self, "hardware_snapshot_ready", False):
            return
        edge = None
        tag_reader = getattr(self, "tag_reader", None)
        for bay in range(min(len(values), len(previous))):
            if not previous[bay] and values[bay]:
                edge = bay
                if tag_reader is not None:
                    tag_reader.note_ready_rise(bay, now)
            elif previous[bay] and not values[bay] and tag_reader is not None:
                tag_reader.note_ready_fall(bay)
        if edge is not None:
            self._position_edge = (edge, now)

    def positioning_bays(self, now=None):
        """Bay indices the firmware is currently positioning (H16).

        A presence edge counts as positioning from the moment it is seen until
        a domain sample taken after the edge reports the unit is not
        positioning.  The domain telemetry is polled about once per cycle, so
        the edge time also caps the pending window when no sample arrives.
        """
        if now is None:
            now = self.reactor.monotonic()
        if (getattr(self, "capabilities", None) or {}).get(
                "normal_motion") is False:
            return frozenset()
        edge = getattr(self, "_position_edge", None)
        domain = (getattr(self, "_telemetry", None) or {}).get("domain") or {}
        if domain.get("motion_state_name") == "positioning":
            if edge is not None:
                return frozenset((edge[0],))
            return frozenset(
                bay for bay, present in enumerate(self.f1s_hes_value) if present)
        if edge is not None:
            timestamp = domain.get("timestamp")
            if (timestamp is None or timestamp < edge[1]) and \
                    now - edge[1] < 3.0:
                return frozenset((edge[0],))
        return frozenset()

    def _bay_status_entry(self, bay, ready, loaded):
        """One bay entry of the v2 status, with its own calibration fields."""
        length = self._ptfe_length_for_bay(bay)
        spool_id, link_source = self.bay_link(bay)
        entry = {"index": bay, "ready": ready, "loaded": loaded}
        entry.update({
            "calibrated": length is not None and length > 0,
            "calibration_state": self._length_state(length),
            "path_length_mm": self._length_mm(length),
            "positioning": bay in self.positioning_bays(),  # H16
            "spool_id": spool_id,
            "link_source": link_source,
            "tag": (getattr(self, "tags", None) or {}).get(bay),  # C2 §4.6
            "spool_hint": None,  # D6
            "filament": self.bay_filament(bay),  # C2 §4.5
        })
        return entry

    def get_webhook_status(self):

        f1s, hub = self.f1s_hes_value, self.hub_hes_value
        return self._ext_status({
            "schema_version": 1,
            "current_spool": self.current_spool,
            "oams_idx": self.oams_idx,
            "connected": self.connected,
            "fps_value": self.fps_value,
            "fps_upper_threshold": self.fps_upper_threshold,
            "fps_lower_threshold": self.fps_lower_threshold,
            "f1s_hes_value": list(f1s[:self.bay_count]),
            "hub_hes_value": list(hub[:self.bay_count]),
            "fps_target": self.fps_target,
            "current_kp": self.current_kp,
            "current_ki": self.current_ki,
            "current_kd": self.current_kd,
            "current_target": self.current_target,
            "f1s_hes_value_0": f1s[0], "f1s_hes_value_1": f1s[1],
            "f1s_hes_value_2": f1s[2], "f1s_hes_value_3": f1s[3],
            "hub_hes_value_0": hub[0], "hub_hes_value_1": hub[1],
            "hub_hes_value_2": hub[2], "hub_hes_value_3": hub[3],
            "kp": self.kp, "ki": self.ki, "kd": self.kd,
            "encoder_clicks": self.encoder_clicks, "i_value": self.i_value,
            "protocol_version": self.protocol_version,
            "gen_matched_protocol": self._use_gen_protocol,
            "family": self.device_family,
            "bay_count": self.bay_count,
            "capabilities": dict(self.capabilities),
            "capability_warnings": list(self.capability_warnings),
            "identity": {
                "serial": getattr(self, "unit_serial", None),
                "firmware": self.capabilities.get("firmware_version"),
                "board": self.capabilities.get("board_name"),
            },
            "calibration": self._calibration_status(),
            "environment": self._environment_status(),
            "dryer": self._dryer_status(),
            "faults": [],
            "follower_uncertain": False,
            "bays": [
                self._bay_status_entry(i, bool(f1s[i]), bool(hub[i]))
                for i in range(self.bay_count)
            ],
            "telemetry": dict(self._telemetry),
            "supported_actions": self._supported_actions(),
        })

    # -------------------------------------------------------------- connection

    def handle_connect(self):
        try:
            self._resolve_protocol()
            self.oams_ptfe_length_cmd = self.mcu.lookup_command(
                "config_oams_ptfe length=%u")
            self.oams_load_spool_cmd = self.mcu.lookup_command(
                "oams_cmd_load_spool spool=%c")
            self.oams_unload_spool_cmd = self.mcu.lookup_command(
                "oams_cmd_unload_spool")
            try:
                self.oams_load_spool_cancel_cmd = self.mcu.lookup_command(
                    "oams_cmd_load_spool_cancel")
            except Exception as e:
                logging.warning("OAMS: load-spool-cancel command unavailable"
                                " (update firmware): %s", e)
            self.oams_follower_cmd = self.mcu.lookup_command(
                "oams_cmd_follower enable=%c direction=%c")
            self.oams_calibrate_ptfe_length_cmd = self.mcu.lookup_command(
                "oams_cmd_calibrate_ptfe_length spool=%c")
            self.oams_calibrate_hub_hes_cmd = self.mcu.lookup_command(
                "oams_cmd_calibrate_hub_hes spool=%c")
            if self.capabilities.get("runtime_pid_tuning", True):
                self.oams_pid_cmd = self.mcu.lookup_command(
                    "oams_cmd_pid kp=%u ki=%u kd=%u target=%u")
                try:
                    self.oams_current_pid_cmd = self.mcu.lookup_command(
                        "oams_cmd_current_pid kp=%u ki=%u kd=%u target=%u")
                except Exception as e:
                    self.oams_current_pid_cmd = None
                    logging.info(
                        "OAMS: rewind-current PID command unavailable: %s", e)
            else:
                self.oams_pid_cmd = None
                self.oams_current_pid_cmd = None
                logging.info(
                    "OAMS[%s]: firmware owns its runtime control gains",
                    self.oams_idx)
            if self.capabilities.get("error_led_control", True):
                self.oams_set_led_error_cmd = self.mcu.lookup_command(
                    "oams_set_led_error idx=%c value=%c")
            else:
                self.oams_set_led_error_cmd = None
                logging.info(
                    "OAMS[%s]: firmware does not expose host error-LED control",
                    self.oams_idx)
            cmd_queue = self.mcu.alloc_command_queue()
            self.oams_spool_query_spool_cmd = self.mcu.lookup_query_command(
                "oams_cmd_query_spool", "oams_query_response_spool spool=%u",
                cq=cmd_queue)
            self._setup_ams_ht_telemetry()
            self._detect_gen_protocol()
            self._detect_atomic_load()
            self._detect_arrival_confirm()
            self._detect_link_status()
            self._detect_tray_info()
            self._detect_tray_write()
            self.clear_errors()
        except Exception as e:
            logging.exception("OAMS: failed to initialize commands: %s", e)

    def _resolve_protocol(self):
        """Read the firmware-published protocol contract from the data
        dictionary. Everything is optional: absent keys keep the built-in
        defaults so old firmware (which publishes nothing) still works."""
        consts = {}
        get_constants = getattr(self.mcu, "get_constants", None)
        if get_constants is None:
            # Should not happen on a real Klipper MCU, but never assume.
            logging.warning("OAMS[%s]: MCU has no get_constants(); assuming"
                            " legacy protocol", self.oams_idx)
        else:
            try:
                consts = get_constants() or {}
            except Exception:
                logging.exception("OAMS[%s]: reading firmware constants failed;"
                                  " assuming legacy protocol", self.oams_idx)
        # Distinguish "no OAMS_* published at all" (legacy firmware) from "some
        # published" so a misconfigured/partial dictionary is diagnosable.
        oams_keys = sorted(k for k in consts if k.startswith("OAMS_"))
        logging.info("OAMS[%s]: firmware published %d OAMS_* constant(s)%s",
                     self.oams_idx, len(oams_keys),
                     "" if not oams_keys else ": " + ", ".join(oams_keys))

        # A malformed value (e.g. a string) must degrade to the built-in
        # default, never raise: an exception here would abort handle_connect
        # and leave the whole unit without its firmware commands.
        def int_const(name, default):
            value = consts.get(name, default)
            try:
                return int(value)
            except (TypeError, ValueError):
                logging.warning("OAMS[%s]: ignoring non-integer firmware"
                                " constant %s=%r", self.oams_idx, name, value)
                return default
        self.firmware_constants = dict(consts)
        configured_family = getattr(self, "configured_family", "auto")
        self.capabilities, self.capability_warnings = (
            ams_family.resolve_capabilities(configured_family, consts))
        self.device_family = self.capabilities["family"]
        self.bay_count = self.capabilities["bays"]
        self._ext_capabilities_resolved()
        for warning in self.capability_warnings:
            logging.error("OAMS[%s]: capability mismatch: %s",
                          self.oams_idx, warning)


        self.protocol_version = (
            None if "OAMS_PROTOCOL_VERSION" not in consts
            else int_const("OAMS_PROTOCOL_VERSION", None))
        if self.protocol_version is None:
            logging.info("OAMS[%s]: firmware publishes no OAMS_PROTOCOL_VERSION;"
                         " using built-in protocol defaults (legacy mode)",
                         self.oams_idx)
        else:
            logging.info("OAMS[%s]: firmware protocol version %s",
                         self.oams_idx, self.protocol_version)

        # Action enum: resolved dynamically (driver-local interpretation).
        self.status_loading = int_const("OAMS_STATUS_LOADING",
                                        OAMS_STATUS_LOADING)
        self.status_unloading = int_const("OAMS_STATUS_UNLOADING",
                                          OAMS_STATUS_UNLOADING)
        self.status_calibrating = int_const("OAMS_STATUS_CALIBRATING",
                                            OAMS_STATUS_CALIBRATING)
        self.status_error = int_const("OAMS_STATUS_ERROR", OAMS_STATUS_ERROR)

        # H9: the guard's reasons become the unit's refusal. Refusing does not
        # raise — Klipper keeps running and the rest of the printer stays
        # usable; the unit is simply not operated (no motion, a named reason).
        self.host_watchdog_ms = (int_const("HOST_WATCHDOG_MS", None)
                                 if "HOST_WATCHDOG_MS" in consts else None)
        reasons = self._connect_guard(consts, int_const)
        self.connect_refusal = "; ".join(reasons) if reasons else None
        if self.connect_refusal is not None:
            logging.error("OAMS[%s]: refusing this unit: %s",
                          self.oams_idx, self.connect_refusal)
            self.capabilities["connect_refusal"] = (
                "OpenAMS refused this unit: " + self.connect_refusal)
            self.capabilities["normal_motion"] = False
            self.capabilities["autonomous_follower"] = False
            self.capabilities["ptfe_calibration"] = False
            self.capabilities["hub_hes_calibration"] = False

    def _connect_guard(self, consts, int_const):
        """H9: the connect-time contract checks. Only keys the firmware
        actually publishes are checked, so legacy firmware (which publishes
        none of them) is left exactly as it was. Returns the reasons this unit
        must not be operated; the caller turns them into a refusal."""
        reasons = []
        version = self.protocol_version
        if version is not None and not (
                MIN_PROTOCOL_VERSION <= version <= MAX_PROTOCOL_VERSION):
            reasons.append(
                "unsupported OAMS_PROTOCOL_VERSION %d (this host supports"
                " %d..%d)" % (version, MIN_PROTOCOL_VERSION,
                              MAX_PROTOCOL_VERSION))
        if "AMS_CAPABILITY_SCHEMA_VERSION" in consts:
            schema = int_const("AMS_CAPABILITY_SCHEMA_VERSION", None)
            if schema is not None and schema < MIN_CAPABILITY_SCHEMA:
                reasons.append("capability schema %d is below the supported"
                               " floor %d" % (schema, MIN_CAPABILITY_SCHEMA))
            elif schema is not None and schema > 5:
                # The schema is additive: a newer one still describes the
                # fields this host reads, so this is a warning, not a refusal.
                logging.warning(
                    "OAMS[%s]: capability schema %d is newer than this host"
                    " knows (5); unknown fields are ignored", self.oams_idx,
                    schema)
        # Op codes / follower directions: these flow into the pure reducer,
        # which cannot read the dictionary, so we cannot substitute them — we
        # validate that the firmware agrees with the host's compiled-in values
        # and refuse the unit on any divergence (a real contract break).
        mismatches = ["%s: firmware=%s host=%s" % (name, consts[name], default)
                      for name, default in _VALIDATED_ENUM_NAMES
                      if name in consts and int_const(name, default) != default]
        if mismatches:
            logging.error(
                "OAMS[%s]: firmware protocol enum mismatch (%s); the plugin's"
                " result-code handling may be wrong — update the plugin to a"
                " version matching this firmware", self.oams_idx,
                "; ".join(mismatches))
            reasons.append("firmware protocol enum mismatch (%s)"
                           % "; ".join(mismatches))
        watchdog = self.host_watchdog_ms
        if watchdog is not None and watchdog < MIN_HOST_WATCHDOG_MS:
            reasons.append("HOST_WATCHDOG_MS=%d is below %d"
                           % (watchdog, MIN_HOST_WATCHDOG_MS))
        return reasons

    def _detect_gen_protocol(self):
        """Select one complete generation-correlation family, strongest first.

        Protocol v4 (32-bit gen, exact cancel) is used only when every v4
        command is in the dictionary. A partial v4 set is never mixed with
        anything: the driver falls back to the complete *2 family (gen echoed
        as one byte), and from there to the legacy commands and FIFO matching.
        """
        self._use_gen_protocol = False
        self._use_wide_gen_protocol = False
        if (self.protocol_version is not None
                and self.protocol_version >= FULL_GEN_PROTOCOL_VERSION):
            try:
                load4 = self.mcu.lookup_command(
                    "oams_cmd_load_spool4 spool=%c gen=%u")
                unload4 = self.mcu.lookup_command(
                    "oams_cmd_unload_spool4 gen=%u")
                ptfe4 = self.mcu.lookup_command(
                    "oams_cmd_calibrate_ptfe_length4 spool=%c gen=%u")
                hub4 = self.mcu.lookup_command(
                    "oams_cmd_calibrate_hub_hes4 spool=%c gen=%u")
                cancel4 = self.mcu.lookup_command(
                    "oams_cmd_cancel4 action=%c gen=%u")
            except Exception:
                logging.info("OAMS[%s]: protocol v4 command set is incomplete;"
                             " falling back to the v2 family", self.oams_idx)
            else:
                self.oams_load_spool4_cmd = load4
                self.oams_unload_spool4_cmd = unload4
                self.oams_calibrate_ptfe_length4_cmd = ptfe4
                self.oams_calibrate_hub_hes4_cmd = hub4
                self.oams_cancel4_cmd = cancel4
                self.mcu.register_serial_response(
                    self._action_status4_received,
                    "oams_action_status4 action=%c code=%c value=%u gen=%u")
                self._use_gen_protocol = True
                self._use_wide_gen_protocol = True
                logging.info("OAMS[%s]: using 32-bit generation protocol v4",
                             self.oams_idx)
                return
        try:
            self.oams_load_spool2_cmd = self.mcu.lookup_command(
                "oams_cmd_load_spool2 spool=%c gen=%c")
            self.oams_unload_spool2_cmd = self.mcu.lookup_command(
                "oams_cmd_unload_spool2 gen=%c")
            self.oams_calibrate_ptfe_length2_cmd = self.mcu.lookup_command(
                "oams_cmd_calibrate_ptfe_length2 spool=%c gen=%c")
            self.oams_calibrate_hub_hes2_cmd = self.mcu.lookup_command(
                "oams_cmd_calibrate_hub_hes2 spool=%c gen=%c")
        except Exception:
            self._use_gen_protocol = False
            logging.info("OAMS[%s]: generation-matched commands unavailable;"
                         " using legacy FIFO completion matching", self.oams_idx)
            return
        # Only register the status2 handler once we know the firmware speaks it,
        # so old firmware never has a dangling response registration.
        self.mcu.register_serial_response(
            self._action_status2_received,
            "oams_action_status2 action=%c code=%c value=%u gen=%c")
        self._use_gen_protocol = True
        logging.info("OAMS[%s]: using generation-matched completion protocol",
                     self.oams_idx)

    def _detect_link_status(self):
        """For a firmware reporting AMS_CAP_LINK_STATUS (it relays a device
        over its own link): register the unsolicited `ams_link_status` and
        resolve the query. Absent or 0: nothing is registered or sent."""
        self.link_reachable = None
        self.ams_get_link_status_cmd = None
        if not self.capabilities.get("link_status"):
            return
        try:
            self._register_link_status_handler()
            # A plain command, not a query helper: Klipper's query helper
            # installs its own handler for the response name, replacing the
            # persistent one, and removes it when the query completes, which
            # would leave unsolicited changes unhandled. The reply arrives at
            # the persistent handler like any other `ams_link_status`.
            self.ams_get_link_status_cmd = self.mcu.lookup_command(
                "ams_get_link_status")
        except Exception:
            logging.exception("OAMS[%s]: link status unavailable",
                              self.oams_idx)

    LINK_STATUS_RESPONSE = "ams_link_status reachable=%c"

    def _response_in_dictionary(self, response):
        check = getattr(self.mcu, "check_valid_response", None)
        return bool(check(response)) if callable(check) else False

    #: The unsolicited completion messages and their handlers. Klipper logs
    #: a message with no handler ("got {...}") and drops it, so after a
    #: host-only restart a terminal that arrives between config finalize and
    #: klippy:connect would be lost and its operation would time out.
    def _register_status_handlers_early(self):
        """From _build_config, before the config is finalized: register every
        action-status handler the dictionary might use. `oams_action_status`
        is already registered at construction; the generation-matched
        variants are registered here instead of waiting for
        _detect_gen_protocol. A response the firmware lacks never arrives, so
        a response is registered only if the dictionary lists it."""
        for handler, response in (
                (self._action_status2_received,
                 "oams_action_status2 action=%c code=%c value=%u gen=%c"),
                (self._action_status4_received,
                 "oams_action_status4 action=%c code=%c value=%u gen=%u")):
            # Klipper raises "Unknown command" (later, when the config is
            # finalized) for a response the dictionary lacks, so only the
            # variants the firmware advertises are registered.
            if self._response_in_dictionary(response):
                self.mcu.register_serial_response(handler, response)

    # ------------------------------------------------- unit-decoded tray info
    # (C1 section 4.7.1: AMS_CAP_TRAY_INFO / AMS_CAP_TRAY_READ.)

    TRAY_INFO_RESPONSE = (
        "ams_tray_info bay=%c state=%c code=%.*s material=%.*s"
        " color_rgba=%u nozzle_min_c=%hu nozzle_max_c=%hu")
    TRAY_READ_COMMAND = "ams_tray_read bay=%c"
    TRAY_READ_RESPONSE = "ams_tray_read_result bay=%c result=%c"
    # ams_tray_read_result result (C1 section 4.7.1).
    _TRAY_READ_RESULTS = {0: "accepted", 1: "busy", 2: "invalid_bay",
                          3: "unavailable", 4: "faulted"}

    def _detect_tray_info(self):
        """For a firmware reporting AMS_CAP_TRAY_INFO: register the
        `ams_tray_info` handler and resolve `ams_get_tray_info`; with
        AMS_CAP_TRAY_READ also resolve `ams_tray_read`. Absent or 0: nothing
        is registered, looked up or sent."""
        self.ams_tray_read_cmd = None
        self.ams_get_tray_info_cmd = None
        if not self.capabilities.get("tray_info"):
            return
        try:
            self._register_tray_info_handler()
            # A plain command, like ams_get_link_status: the reply is the same
            # `ams_tray_info` the unit pushes on every change, so it must go to
            # the persistent handler (a query helper would replace it).
            self.ams_get_tray_info_cmd = self.mcu.lookup_command(
                "ams_get_tray_info bay=%c")
            if self.capabilities.get("tray_read"):
                # The reply names its own response, so a query helper is
                # safe here: it never touches the `ams_tray_info` handler.
                self.ams_tray_read_cmd = self.mcu.lookup_query_command(
                    self.TRAY_READ_COMMAND, self.TRAY_READ_RESPONSE,
                    cq=self.mcu.alloc_command_queue())
        except Exception:
            logging.exception("OAMS[%s]: tray info unavailable", self.oams_idx)
            self.ams_get_tray_info_cmd = None
            self.ams_tray_read_cmd = None

    def _register_tray_info_handler(self):
        self.mcu.register_serial_response(
            self._tray_info_received, self.TRAY_INFO_RESPONSE)

    def _register_tray_info_early(self):
        """From _build_config, like the link status: a unit may push its tray
        summaries right after finalize, before klippy:connect handlers run."""
        try:
            constants = self.mcu.get_constants() or {}
            if (int(constants.get("AMS_CAP_TRAY_INFO", 0))
                    and self._response_in_dictionary(
                        self.TRAY_INFO_RESPONSE)):
                self._register_tray_info_handler()
        except Exception:
            logging.info("OAMS[%s]: tray info handler not registered early",
                         self.oams_idx)

    @staticmethod
    def _tray_text(value, limit):
        """A ``%.*s`` field as text: Klipper delivers bytes; NULs and
        surrounding blanks are dropped, anything undecodable is replaced."""
        if isinstance(value, (bytes, bytearray)):
            value = bytes(value).decode("utf-8", "replace")
        return str(value or "").replace("\x00", "").strip()[:limit]

    def _tray_info_received(self, params):
        """Serial thread: decode one `ams_tray_info`, then hand it to the
        reactor thread, which owns the published tags."""
        try:
            bay = int(params["bay"])
            record = {
                "bay": bay,
                "state": int(params["state"]),
                "code": self._tray_text(params.get("code"), 16),
                "material": self._tray_text(params.get("material"), 32),
                "color_rgba": int(params.get("color_rgba", 0)) & 0xFFFFFFFF,
                "nozzle_min_c": int(params.get("nozzle_min_c", 0)),
                "nozzle_max_c": int(params.get("nozzle_max_c", 0)),
            }
        except (KeyError, TypeError, ValueError):
            logging.info("OAMS[%s]: malformed ams_tray_info ignored",
                         self.oams_idx)
            return
        if not 0 <= bay < self.bay_count:
            return
        self.reactor.register_async_callback(
            lambda eventtime, r=record: self.tag_reader.note_tray_info(
                r["bay"], r))

    def _query_tray_info(self, eventtime):
        """Ask for every bay's summary once after ready, from a reactor
        callback; the replies arrive at the persistent handler."""
        command = self.ams_get_tray_info_cmd
        if command is None:
            return
        for bay in range(self.bay_count):
            try:
                command.send([bay])
            except Exception:
                logging.exception("OAMS[%s]: tray info query for bay %d"
                                  " failed", self.oams_idx, bay)

    def tray_read_once(self, bay):
        """One `ams_tray_read`: the immediate result's name. The outcome
        arrives later as `ams_tray_info` (C1 section 4.7.1)."""
        command = self._require_cmd(self.ams_tray_read_cmd, "tray read")
        params = command.send([bay])
        if params is None:
            raise self.printer.command_error("tray read returned no result")
        return self._TRAY_READ_RESULTS.get(params.get("result", -1), "unknown")

    # --------------------------------------- stored tray setting (C1 4.7.2)
    # (AMS_CAP_TRAY_WRITE: the unit stores, per bay, the filament setting it
    # reports in `ams_tray_info` when it reads no tag.)

    TRAY_WRITE_COMMAND = (
        "ams_set_tray_info bay=%c code=%*s material=%*s color_rgba=%u"
        " nozzle_min_c=%hu nozzle_max_c=%hu")
    TRAY_WRITE_RESPONSE = "ams_tray_write_result bay=%c result=%c"
    # ams_tray_write_result result (C1 section 4.7.2): 0-4 and 7 the
    # immediate admission, 5-6 the completion that follows an accepted write.
    # 7 (a code or material the unit cannot store) is permanent: the same
    # write is never taken.
    _TRAY_WRITE_RESULTS = {0: "accepted", 1: "busy", 2: "invalid_bay",
                           3: "unavailable", 4: "faulted", 5: "written",
                           6: "failed", 7: "invalid_field"}
    _TRAY_WRITE_DONE = (5, 6)
    TRAY_CODE_MAX = 6
    TRAY_MATERIAL_MAX = 8
    #: How long a write waits for the unit's admission, then for completion.
    TRAY_WRITE_ADMIT_WAIT = 2.0
    TRAY_WRITE_COMPLETE_WAIT = 30.0

    def _detect_tray_write(self):
        """For a firmware reporting AMS_CAP_TRAY_WRITE: register the
        `ams_tray_write_result` handler and resolve `ams_set_tray_info`.
        Absent or 0: nothing is registered, looked up or sent. A firmware
        whose dictionary lacks either loses the capability, so nothing above
        the driver tries a write it cannot make."""
        self.ams_set_tray_info_cmd = None
        if self._tray_writes is None:
            self._tray_writes = {}
        if not self.capabilities.get("tray_write"):
            return
        try:
            # A plain command and one persistent handler: the response comes
            # twice per write (admission, then completion), which a query
            # helper (one reply, then its handler removed) cannot take.
            self.mcu.register_serial_response(
                self._tray_write_received, self.TRAY_WRITE_RESPONSE)
            self.ams_set_tray_info_cmd = self.mcu.lookup_command(
                self.TRAY_WRITE_COMMAND)
        except Exception:
            logging.exception("OAMS[%s]: tray write unavailable",
                              self.oams_idx)
            self.ams_set_tray_info_cmd = None
            self.capabilities["tray_write"] = False

    @classmethod
    def tray_write_text(cls, value, limit, name, required=False):
        """A `%*s` field of `ams_set_tray_info` as bytes: printable ASCII,
        trimmed, at most ``limit`` characters. Raises ValueError with a
        message a person can read."""
        text = str(value if value is not None else "").strip()
        if required and not text:
            raise ValueError("%s is required" % name)
        if len(text) > limit:
            raise ValueError("%s must be at most %d characters, got '%s'"
                             % (name, limit, text))
        if any(not 0x20 <= ord(char) <= 0x7E for char in text):
            raise ValueError("%s must be printable ASCII, got '%s'"
                             % (name, text))
        return text.encode("ascii")

    def _tray_write_args(self, bay, code, material, color_rgba,
                         nozzle_min_c, nozzle_max_c):
        """The command's arguments, checked; raises ValueError."""
        if not 0 <= int(bay) < self.bay_count:
            raise ValueError("BAY must be between 0 and %d"
                             % (self.bay_count - 1))
        code_bytes = self.tray_write_text(code, self.TRAY_CODE_MAX, "CODE")
        if b" " in code_bytes:
            raise ValueError("CODE must not contain spaces")
        material_bytes = self.tray_write_text(
            material, self.TRAY_MATERIAL_MAX, "MATERIAL", required=True)
        low, high = int(nozzle_min_c), int(nozzle_max_c)
        if not 0 <= low <= high <= 0xFFFF:
            raise ValueError("the nozzle range %d-%d is not a range" % (
                low, high))
        return [int(bay), code_bytes, material_bytes,
                int(color_rgba) & 0xFFFFFFFF, low, high]

    def tray_write_start(self, bay, code, material, color_rgba,
                         nozzle_min_c, nozzle_max_c, on_done=None):
        """Send one `ams_set_tray_info` and track it without blocking.

        The unit answers at once with an admission (accepted, busy, invalid
        bay, unavailable, faulted, invalid field) and, after an accepted
        write, with a completion (written, failed); each phase times out on
        its own. Only
        one write per bay is in flight. ``on_done(write)`` runs on the
        reactor once the write ends (its ``result``: a refusal, a
        completion, or ``timeout``). Returns the tracking dict, whose ``admission`` is set as
        soon as the unit admits or refuses. Raises the printer's command
        error for a write that cannot be sent at all.
        """
        command = self._require_cmd(self.ams_set_tray_info_cmd, "tray write")
        try:
            args = self._tray_write_args(bay, code, material, color_rgba,
                                         nozzle_min_c, nozzle_max_c)
        except (TypeError, ValueError) as error:
            raise self.printer.command_error(str(error))
        bay = args[0]
        if self._tray_writes is None:
            self._tray_writes = {}
        if bay in self._tray_writes:
            raise self.printer.command_error(
                "a filament setting is already being written to %s"
                % self.spool_label(bay))
        self._tray_write_token += 1
        write = {"token": self._tray_write_token, "bay": bay,
                 "phase": "admission", "admission": None, "result": None,
                 "on_done": on_done,
                 "setting": {"code": args[1].decode("ascii"),
                             "material": args[2].decode("ascii"),
                             "color_rgba": args[3], "nozzle_min_c": args[4],
                             "nozzle_max_c": args[5]}}
        self._tray_writes[bay] = write
        try:
            command.send(args)
        except Exception:
            self._tray_writes.pop(bay, None)
            raise
        # The admission may already have arrived (a unit that answers inside
        # send); the timer then finds the write past its admission phase.
        self._arm_tray_write_timeout(write, "admission",
                                     self.TRAY_WRITE_ADMIT_WAIT)
        return write

    def tray_write_now(self, bay, code, material, color_rgba, nozzle_min_c,
                       nozzle_max_c, on_done=None):
        """`tray_write_start`, then wait for the admission only, pausing the
        caller (a G-code command), never the reactor. Returns the
        admission's name, or ``timeout``; the completion goes to
        ``on_done``."""
        write = self.tray_write_start(bay, code, material, color_rgba,
                                      nozzle_min_c, nozzle_max_c, on_done)
        deadline = self.reactor.monotonic() + self.TRAY_WRITE_ADMIT_WAIT \
            + 0.1
        while write["admission"] is None:
            now = self.reactor.monotonic()
            if now >= deadline:
                break
            try:
                self.reactor.pause(min(deadline, now + 0.02))
            except Exception:
                break           # where pausing is not allowed: no wait
        return write["admission"] or "timeout"

    def _arm_tray_write_timeout(self, write, phase, seconds):
        reactor = self.reactor
        reactor.register_callback(
            lambda eventtime, w=write, p=phase: self._tray_write_timeout(w, p),
            reactor.monotonic() + seconds)

    def _tray_write_timeout(self, write, phase):
        if (self._tray_writes.get(write["bay"]) is write
                and write["phase"] == phase):
            logging.info("OAMS[%s]: tray write of bay %s: no %s reported",
                         self.oams_idx, write["bay"], phase)
            self._finish_tray_write(write, "timeout")

    def _tray_write_received(self, params):
        """Serial thread: hand one `ams_tray_write_result` to the reactor."""
        try:
            bay, result = int(params["bay"]), int(params["result"])
        except (KeyError, TypeError, ValueError):
            logging.info("OAMS[%s]: malformed ams_tray_write_result ignored",
                         self.oams_idx)
            return
        self.reactor.register_async_callback(
            lambda eventtime, b=bay, r=result: self._tray_write_result(b, r))

    def _tray_write_result(self, bay, result):
        """One admission or completion (reactor thread)."""
        write = (self._tray_writes or {}).get(bay)
        name = self._TRAY_WRITE_RESULTS.get(result, "unknown")
        if write is None:
            logging.info("OAMS[%s]: tray write result %s for bay %s, with no"
                         " write in flight", self.oams_idx, name, bay)
            return
        if write["phase"] == "admission":
            if result == 0:
                write["admission"] = name
                write["phase"] = "completion"
                self._publish_tray_write(bay, name)
                self._arm_tray_write_timeout(write, "completion",
                                             self.TRAY_WRITE_COMPLETE_WAIT)
                return
            # A refusal ends the write. A completion with no admission before
            # it (a lost frame) still ends it, and the write was admitted.
            write["admission"] = "accepted" if result in \
                self._TRAY_WRITE_DONE else name
            self._finish_tray_write(write, name)
            return
        if result in self._TRAY_WRITE_DONE:
            self._finish_tray_write(write, name)
        else:
            logging.info("OAMS[%s]: tray write of bay %s: unexpected %s after"
                         " admission", self.oams_idx, bay, name)

    def _finish_tray_write(self, write, name):
        if self._tray_writes.get(write["bay"]) is write:
            del self._tray_writes[write["bay"]]
        write["phase"] = "done"
        write["result"] = name
        if write["admission"] is None:
            write["admission"] = name
        self._publish_tray_write(write["bay"], name)
        logging.info("OAMS[%s]: tray write of bay %s (%s %s): %s",
                     self.oams_idx, write["bay"], write["setting"]["code"],
                     write["setting"]["material"], name)
        callback = write.get("on_done")
        if callback is not None:
            try:
                callback(write)
            except Exception:
                logging.exception("OAMS[%s]: tray write callback failed",
                                  self.oams_idx)

    def _publish_tray_write(self, bay, name):
        """The ``tray_write`` telemetry channel (C2 §4.4.2)."""
        self._publish_telemetry("tray_write", {
            "bay": bay, "result_name": name,
            "timestamp": self.reactor.monotonic()})

    def tray_write_in_flight(self, bay):
        """Whether a write to ``bay`` waits for its admission or completion."""
        return bay in (self._tray_writes or {})

    def _register_link_status_handler(self):
        self.mcu.register_serial_response(
            self._link_status_received, self.LINK_STATUS_RESPONSE)

    def _register_link_status_early(self):
        """From _build_config, which runs while the MCU sends its config and
        before it is finalized: a firmware pushes its link state right after
        finalize, earlier than klippy:connect handlers run, so the persistent
        handler must already exist. Harmless where the firmware has no such
        message."""
        try:
            constants = self.mcu.get_constants() or {}
            if (int(constants.get("AMS_CAP_LINK_STATUS", 0))
                    and self._response_in_dictionary(
                        self.LINK_STATUS_RESPONSE)):
                self._register_link_status_handler()
        except Exception:
            logging.info("OAMS[%s]: link status handler not registered early",
                         self.oams_idx)

    def _link_status_received(self, params):
        """Serial thread: record the link state, then let the reactor thread
        decide what it means (the manager's reachability check)."""
        reachable = bool(params["reachable"])
        changed = self.link_reachable is not reachable
        self.link_reachable = reachable
        if changed:
            self.reactor.register_async_callback(self._link_changed)

    def _link_changed(self, eventtime):
        manager = self.printer.lookup_object("oams_manager", None)
        check = getattr(manager, "check_unit_reachability", None)
        if callable(check):
            check(self.reactor.monotonic())

    def _query_link_status(self, eventtime):
        """Ask for the initial link state once, from a reactor callback after
        ready. The reply is delivered to the persistent handler."""
        command = self.ams_get_link_status_cmd
        if command is None:
            return
        try:
            command.send([])
        except Exception:
            logging.exception("OAMS[%s]: link status query failed",
                              self.oams_idx)

    def _require_link(self, gcmd):
        """A unit whose device is unreachable takes no command that would
        reach it."""
        if self.link_reachable is False:
            raise gcmd.error("The unit's device is not reachable; check its"
                             " connection")

    def _detect_arrival_confirm(self):
        """Resolve `oams_cmd_load_arrived` for a firmware that reports
        AMS_CAP_HOST_ARRIVAL_CONFIRM=1 (it has no toolhead input of its own
        and needs the host to confirm arrival). Absent or 0: nothing is
        looked up and nothing changes."""
        self.oams_load_arrived_cmd = None
        self._arrival_sent_gen = None
        if not self.capabilities.get("host_arrival_confirm"):
            return
        try:
            self.oams_load_arrived_cmd = self.mcu.lookup_command(
                "oams_cmd_load_arrived gen=%c")
        except Exception:
            logging.error("OAMS[%s]: the firmware needs host arrival"
                          " confirmation but has no oams_cmd_load_arrived",
                          self.oams_idx)

    @property
    def needs_host_arrival(self):
        """True when this unit's firmware needs the host to confirm toolhead
        arrival on a load (C1 AMS_CAP_HOST_ARRIVAL_CONFIRM)."""
        return bool(self.capabilities.get("host_arrival_confirm"))

    def confirm_load_arrived(self, gen):
        """Send the arrival acknowledgement for the load of generation `gen`.

        Only a unit reporting the capability, with a load actually in flight,
        and only once per generation; anything else sends nothing and returns
        False. The firmware ignores a generation that is not its active load,
        and the load's terminal status stays the outcome."""
        if not self.needs_host_arrival or self.oams_load_arrived_cmd is None:
            return False
        if self.action_status != self.status_loading:
            return False
        wire = self._wire_gen(gen)
        if self._arrival_sent_gen == wire:
            return False
        self.oams_load_arrived_cmd.send([wire])
        self._arrival_sent_gen = wire
        return True

    def _detect_atomic_load(self):
        """Feature-detect the atomic load (F9), the same try/except pattern
        used for the other optional commands. Looked up separately from the
        generation-matched set: the atomic command implies a gen, but a
        firmware that advertises only the *2 set must keep the separate
        PTFE-length write + load_spool2 path."""
        try:
            self.oams_load_spool3_cmd = self.mcu.lookup_command(
                "oams_cmd_load_spool3 spool=%c gen=%c clicks=%u")
        except Exception:
            self.oams_load_spool3_cmd = None
            logging.info("OAMS[%s]: atomic load command unavailable; the PTFE"
                         " length stays a separate write before the load",
                         self.oams_idx)

    # ------------------------------------------------------- AMS HT telemetry

    #: telemetry channel -> the capability whose explicit 0 means the unit
    #: does not send it (C1 §7.2). A channel not listed is always expected.
    _TELEMETRY_CAPABILITY = {"board": "board_telemetry", "dryer": "dryer"}

    def _telemetry_expected(self, name):
        capability = (self._TELEMETRY_CAPABILITY.get(name)
                      or self._EXT_TELEMETRY_CAPABILITY.get(name))
        if capability is None:
            return True
        # One capability or several: every one named must not be an explicit 0.
        names = (capability,) if isinstance(capability, str) else capability
        return all(self.capabilities.get(key) is not False for key in names)

    def _setup_ams_ht_telemetry(self):
        # Universal telemetry setup across all hardware families. Resolve each
        # command/response dynamically from firmware capabilities and dictionary.
        self.capabilities["telemetry"] = False
        self._telemetry_commands = {}
        for attribute in ("ams_ht_dryer_start_cmd", "ams_ht_dryer_stop_cmd",
                          "ams_ht_clear_fault_cmd"):
            setattr(self, attribute, None)
        self._ext_reset_commands()
        if (self.device_family not in ("ams_ht", "ams2")
                and not self.capabilities.get("telemetry", False)
                and self.capabilities.get("firmware_capability_schema", 0) < 4):
            return
        specs = [
            ("board", "ams_ht_get_board_sensor_status",
             "ams_ht_board_sensor_status f1s_counts=%hu filament_present=%c"
             " hub_counts=%hu hub_filament_present=%c door_counts=%hu"
             " door_closed=%c encoder_clicks=%i buffer_permille=%i"
             " f1s_overcurrent_level=%c f1s_overcurrent_active=%c"
             " f1s_overcurrent_trips=%u buffer_valid=%c",
             self._board_status_received),
            ("domain", "ams_ht_get_domain_status",
             "ams_ht_domain_status motion_state=%c motion_stage=%c"
             " motion_fault=%c dryer_state=%c dryer_fault=%c system_fault=%c",
             self._domain_status_received),
        ]
        if self.device_family in ("ams_ht", "ams2") or self.capabilities.get("dryer", False):
            specs.append(
                ("dryer", "ams_ht_get_dryer_status",
                 "ams_ht_dryer_status state=%c fault=%c target_centic=%hu"
                 " heater_centic=%hi chamber_centic=%hi humidity_mgm3=%u"
                 " remaining_s=%u heater_permille=%hu fan_permille=%hu"
                 " fan_rpm=%u fan_pwm_permille=%hu vent_position=%c"
                 " purging=%c door_closed=%c",
                 self._dryer_status_received)
            )
        specs = self._ext_telemetry_specs(specs)
        # Register a channel only where the firmware does not report it
        # unavailable: an absent constant keeps the family behaviour, an
        # explicit 0 means the unit never sends it, so none is expected (and
        # no error is logged for a lookup that cannot succeed).
        specs = [spec for spec in specs if self._telemetry_expected(spec[0])]
        for name, command, response, callback in specs:
            try:
                handle = self.mcu.lookup_command(command)
                self.mcu.register_serial_response(callback, response)
                self._telemetry_commands[name] = handle
            except Exception:
                logging.exception("OAMS[%s]: AMS HT %s telemetry unavailable",
                                  self.oams_idx, name)
        self._ext_after_channels()
        self.capabilities["telemetry"] = bool(self._telemetry_commands)
        self.capabilities["telemetry_channels"] = sorted(self._telemetry_commands)
        try:
            control_queue = self.mcu.alloc_command_queue()
        except Exception:
            logging.exception("OAMS[%s]: AMS HT control queue unavailable",
                              self.oams_idx)
        else:
            dryer_response = "ams_ht_dryer_result result=%c"
            control_cmds = [
                ("ams_ht_clear_fault_cmd", "ams_ht_clear_fault",
                 "fault clear"),
            ]
            if ((self.device_family in ("ams_ht", "ams2")
                    or self.capabilities.get("dryer", False))
                    and self._telemetry_expected("dryer")):
                control_cmds.extend([
                    ("ams_ht_dryer_start_cmd",
                     "ams_ht_dryer_start target_centic=%hu duration_s=%u"
                     " hold_after=%c", "dryer start"),
                    ("ams_ht_dryer_stop_cmd", "ams_ht_dryer_stop",
                     "dryer stop"),
                ])
            control_cmds = self._ext_control_commands(control_cmds)
            for attribute, command, operation in control_cmds:
                try:
                    value = self.mcu.lookup_query_command(
                        command, dryer_response, cq=control_queue)
                except Exception:
                    logging.exception("OAMS[%s]: AMS HT %s unavailable",
                                      self.oams_idx, operation)
                else:
                    setattr(self, attribute, value)
        self._ext_after_controls()

        # F3 (C1 §5.4): the environment changes slowly, so its query rides its
        # own slow poll instead of a slot of the per-cycle sequence above.
        self._environment_cmd = None
        try:
            self._environment_cmd = self.mcu.lookup_command(
                "ams_get_environment")
            self.mcu.register_serial_response(
                self._environment_status_received,
                "ams_environment rh_centipercent=%hu temperature_centic=%hi"
                " valid=%c")
        except Exception:
            self._environment_cmd = None   # older firmware: no F3 query
        logging.info("OAMS[%s]: %s telemetry channels: %s", self.oams_idx,
                     self._family_label(),
                     ", ".join(sorted(self._telemetry_commands)))

    def _handle_ready(self):
        # Klipper runs klippy:ready handlers with pausing forbidden
        # (reactor.assert_no_pause), and the identity query blocks on the
        # MCU's reply, so it runs from a reactor callback instead.
        if getattr(self, "unit_serial", None) is None:
            self.reactor.register_callback(self._identity_callback)
        if self.ams_get_link_status_cmd is not None:
            self.reactor.register_callback(self._query_link_status)
        if self.ams_get_tray_info_cmd is not None:
            self.reactor.register_callback(self._query_tray_info)
        if "dryer" in self._telemetry_commands:
            # A dryer cycle may be running from an earlier session: ask for
            # its state at once, so it is published from the first status
            # instead of after the telemetry round-robin reaches the dryer.
            self.reactor.register_callback(self._query_dryer_status)
        if self._telemetry_commands and self._telemetry_timer is None:
            self._telemetry_timer = self.reactor.register_timer(
                self._poll_ams_ht_telemetry, self.reactor.NOW)

    def _query_dryer_status(self, eventtime):
        command = self._telemetry_commands.get("dryer")
        if command is None:
            return
        try:
            command.send([])
        except Exception:
            logging.exception("OAMS[%s]: initial dryer status failed",
                              self.oams_idx)

    def _family_label(self):
        """The family's display name for log lines (profile `display_name`)."""
        try:
            return ams_family.profile(self.device_family)["display_name"]
        except Exception:
            return "OAMS"

    def _unit_label(self):
        """The unit named for console replies, e.g. ``ams2 (AMS 2 Pro)``."""
        name = getattr(self, "name", None)
        short = name.split()[-1] if name else "OAMS[%s]" % self.oams_idx
        return "%s (%s)" % (short, self._family_label())

    def _identity_callback(self, eventtime):
        if getattr(self, "unit_serial", None) is None:
            self._query_unit_identity()

    def _query_unit_identity(self):
        # C1 §2: the unit's stable identity as three little-endian 32-bit
        # words, rendered as 24 uppercase hex digits. Older firmware does not
        # answer the query, so any failure leaves the serial None.
        try:
            try:
                query = self.mcu.lookup_query_command(
                    "ams_get_identity",
                    "ams_identity uid0=%u uid1=%u uid2=%u")
            except Exception as e:
                # The STM32F072 mainboard firmware has no identity query:
                # its CAN UUID is the serial, else there is none. One info
                # line, not a traceback.
                self.unit_serial = getattr(self, "canbus_uuid", None)
                logging.info(
                    "OAMS[%s]: no unit identity query (%s); serial %s",
                    self.oams_idx, e,
                    "from the CAN UUID" if self.unit_serial else "unknown")
                return
            params = query.send()
            raw = (int(params["uid0"]).to_bytes(4, "little")
                   + int(params["uid1"]).to_bytes(4, "little")
                   + int(params["uid2"]).to_bytes(4, "little"))
            self.unit_serial = raw.hex().upper()
        except Exception:
            logging.exception("OAMS[%s]: unit identity query unavailable",
                              self.oams_idx)
            self.unit_serial = None

    def _handle_disconnect(self):
        self.hardware_snapshot_ready = False
        self._ext_disconnect()
        if self._telemetry_timer is not None:
            self.reactor.unregister_timer(self._telemetry_timer)
            self._telemetry_timer = None

    def _poll_ams_ht_telemetry(self, eventtime):
        sequence = self._POLL_SEQUENCE
        name = sequence[self._telemetry_index % len(sequence)]
        self._telemetry_index += 1
        command = self._telemetry_commands.get(name)
        if command is not None:
            try:
                command.send([])
            except Exception:
                logging.exception("OAMS[%s]: polling AMS HT %s failed",
                                  self.oams_idx, name)
        env_cmd = getattr(self, "_environment_cmd", None)
        last_env = getattr(self, "_last_environment_poll", None)
        if (name == "domain" and env_cmd is not None
                and (last_env is None
                     or eventtime - last_env >= ENVIRONMENT_POLL_S)):
            try:
                env_cmd.send([])
                self._last_environment_poll = eventtime
            except Exception:
                logging.exception("OAMS[%s]: polling the environment failed",
                                  self.oams_idx)
        self._ext_poll(eventtime, name)
        return eventtime + self._POLL_INTERVAL_S


    def _telemetry_data(self, params):
        data = {key: value for key, value in params.items()
                if not str(key).startswith("#")}
        data["timestamp"] = params.get(
            "#receive_time", self.reactor.monotonic())
        return data

    def _publish_telemetry(self, name, data):
        snapshot = dict(self._telemetry)
        snapshot[name] = data
        self._telemetry = snapshot

    #: Wire fault numbers (capability schema 4 and 5) and their names.
    _FAULT_NAMES = {
        0: "none", 1: "invalid_heater_thermistor",
        2: "invalid_motor_thermistor", 3: "heater_overtemperature",
        4: "motor_overtemperature", 5: "chamber_overtemperature",
        6: "environment_sensor_invalid", 7: "fan_stalled",
        8: "ac_sync_lost", 9: "invalid_request",
        10: "motion_timeout", 11: "motion_stalled",
        12: "motor_drive_fault",
        13: "supply_undervoltage", 14: "supply_overvoltage",
        15: "supply_monitor_invalid", 16: "heater_drive_fault",
        17: "host_communication_lost", 18: "first_stage_overcurrent",
        19: "follower_buffer_invalid", 20: "follower_filament_lost",
        21: "follower_starved", 22: "follower_stalled",
        23: "follower_timeout", 24: "vent_actuator_fault",
        # The unit sees filament at the hub but not at the feeder inlet.
        25: "lane_obstructed",
        # The unit acknowledged a dryer start and never ran it.
        26: "dryer_start_refused",
        # The unit raised an error for a spool tag it rejected.
        27: "rfid_tag_error",
    }

    def _controller_fault_name(self, value):
        name = self._ext_fault_name(value)
        if name is not None:
            return name
        schema = self.capabilities.get("firmware_capability_schema", 0)
        if value >= 13 and schema > 5:
            return "unknown"  # A later schema's values are not named here.
        return self._FAULT_NAMES.get(value, "unknown")

    def _environment_status_received(self, params):
        self._publish_telemetry("environment", self._telemetry_data(params))

    #: ams_ht_domain_status motion_stage values and their names.
    _MOTION_STAGE_NAMES = {
        0: "stopped", 1: "load_feed_to_hub", 3: "load_to_buffer",
        4: "follow", 5: "unload_to_hub_clear", 6: "position_feed_to_hub",
        11: "unload_stop_settle", 13: "unload_to_f1s_clear",
    }

    def _domain_status_received(self, params):
        data = self._telemetry_data(params)
        data["motion_state_name"] = {
            0: "idle", 1: "load", 2: "follow", 3: "unload",
            4: "fault", 5: "positioning",
        }.get(data["motion_state"], "unknown")
        stage = data["motion_stage"]
        data["motion_stage_name"] = (
            self._MOTION_STAGE_NAMES.get(stage)
            or self._EXT_MOTION_STAGE_NAMES.get(stage, "unknown"))
        data["dryer_state_name"] = {
            0: "off", 1: "preheat", 2: "hold", 3: "timed_dry",
            4: "cooldown", 5: "fault",
        }.get(data["dryer_state"], "unknown")
        for key in ("motion_fault", "dryer_fault", "system_fault"):
            data[key + "_name"] = self._controller_fault_name(data[key])
        self._publish_telemetry("domain", data)
        # H9c: the unit's host-communication watchdog fired and it
        # deconfigured itself; hand off to the reactor thread once.
        if (data.get("system_fault_name") == "host_communication_lost"
                and getattr(self, "session_lost", None) is None):
            self.reactor.register_async_callback(
                lambda eventtime: self._on_session_lost())

    def _on_session_lost(self):
        self.session_lost = S.SESSION_LOST_TEXT
        logging.error("OAMS unit %s: %s", self.oams_idx, self.session_lost)
        self.capabilities["session_lost"] = self.session_lost
        self.capabilities["normal_motion"] = False
        mgr = self.printer.lookup_object("oams_manager", None)
        if mgr is not None and hasattr(mgr, "unit_session_lost"):
            mgr.unit_session_lost(self.oams_idx, self.session_lost)

    def _board_status_received(self, params):
        data = self._telemetry_data(params)
        # This legacy response describes one feed path, not all AMS2 bays.
        # Only the full oams_cmd_stats snapshot may update the four-bay state.
        if self.device_family == "ams_ht":
            self._note_f1s(
                [int(bool(data["filament_present"])), 0, 0, 0],
                params.get("#receive_time", self.reactor.monotonic()))
            self.hub_hes_value = [
                int(bool(data["hub_filament_present"])), 0, 0, 0]
            was_ready = getattr(self, "hardware_snapshot_ready", False)
            self.hardware_snapshot_ready = True
            tag_reader = getattr(self, "tag_reader", None)
            if not was_ready and tag_reader is not None:
                tag_reader.note_hardware_ready()
            self.last_stats_time = params.get("#receive_time",
                                              self.reactor.monotonic())
        self._publish_telemetry("board", data)

    #: Dryer states that mean a cycle is in progress (C1 §4.4).
    _DRYER_RUNNING_STATES = ("preheat", "hold", "timed_dry", "cooldown")

    def _dryer_blocked_by_load(self):
        """For a unit reporting AMS_CAP_DRYER_REQUIRES_UNLOADED: the bay
        whose filament is out to the toolhead (the loaded bay, else a bay
        whose hub sensor still sees filament), which keeps it from drying.
        None when nothing is out, or for any other unit."""
        if not self.capabilities.get("dryer_requires_unloaded"):
            return None
        if getattr(self, "current_spool", None) is not None:
            return self.current_spool
        for bay, hub in enumerate(getattr(self, "hub_hes_value", None) or ()):
            if hub:
                return bay
        return None

    def _dryer_cycle_running(self):
        """True while the firmware reports a dryer cycle in progress, whether
        this session started it or adopted it from an earlier one."""
        data = (getattr(self, "_telemetry", None) or {}).get("dryer") or {}
        return data.get("state_name") in self._DRYER_RUNNING_STATES

    def _dryer_status_received(self, params):
        data = self._telemetry_data(params)
        for raw, friendly in (("target_centic", "target_c"),
                              ("heater_centic", "heater_c"),
                              ("chamber_centic", "chamber_c")):
            value = data[raw]
            data[friendly] = None if value == -32768 else value / 100.0
        data["state_name"] = {
            0: "off", 1: "preheat", 2: "hold", 3: "timed_dry",
            4: "cooldown", 5: "fault",
        }.get(data["state"], "unknown")
        data["fault_name"] = self._controller_fault_name(data["fault"])
        data["humidity_gm3"] = data["humidity_mgm3"] / 1000.0
        data["vent_position_name"] = {
            0: "unknown", 1: "closed", 2: "open",
        }.get(data["vent_position"], "unknown")
        if (not getattr(self, "_dryer_seen", False)
                and data["state_name"] in self._DRYER_RUNNING_STATES):
            # A firmware may keep a cycle running when the host session ends
            # (C1 §4.4 / §6.2): the first status of this session shows it.
            # It is adopted as it is, never stopped or restarted here.
            logging.info(
                "OAMS[%s]: adopting a dryer cycle already running (%s,"
                " target %s C, %s s left)", self.oams_idx,
                data["state_name"], data.get("target_c"),
                data.get("remaining_s"))
        self._dryer_seen = True
        self._publish_telemetry("dryer", data)

    def clear_errors(self):
        for i in range(self.bay_count):
            self.set_led_error(i, 0)
        # CLEAR_ERRORS is the resync point: any replies still owed for
        # abandoned ops are no longer wanted.
        self._gen_queue.clear()
        self._active_action = None
        self._active_gen = None
        self.current_spool = self.determine_current_spool()

    def set_led_error(self, idx, value):
        if self.oams_set_led_error_cmd is None:
            return
        logging.info("OAMS: setting LED %d to %d", idx, value)
        self.oams_set_led_error_cmd.send([idx, value])

    def determine_current_spool(self):
        if self.oams_spool_query_spool_cmd is None:
            return None
        params = self.oams_spool_query_spool_cmd.send()
        if params is not None and 0 <= params.get("spool", -1) < self.bay_count:
            return params["spool"]
        return None

    # ---------------------------------------------------------------- commands

    def register_commands(self):
        oams_id = str(self.oams_idx)
        gcode = self.printer.lookup_object("gcode")
        for name, handler, desc in (
            ("OAMS_LOAD_SPOOL", self.cmd_OAMS_LOAD_SPOOL,
             self.cmd_OAMS_LOAD_SPOOL_help),
            ("OAMS_UNLOAD_SPOOL", self.cmd_OAMS_UNLOAD_SPOOL,
             self.cmd_OAMS_UNLOAD_SPOOL_help),
            ("OAMS_DRYER_START", self.cmd_OAMS_DRYER_START,
             self.cmd_OAMS_DRYER_START_help),
            ("OAMS_DRYER_STOP", self.cmd_OAMS_DRYER_STOP,
             self.cmd_OAMS_DRYER_STOP_help),
            ("OAMS_CLEAR_FAULT", self.cmd_OAMS_CLEAR_FAULT,
             self.cmd_OAMS_CLEAR_FAULT_help),
            ("OAMS_RFID_SCAN", self.cmd_OAMS_RFID_SCAN,
             self.cmd_OAMS_RFID_SCAN_help),
            ("OAMS_FOLLOWER", self.cmd_OAMS_FOLLOWER, self.cmd_OAMS_FOLLOWER_help),
            ("OAMS_CALIBRATE_PTFE_LENGTH", self.cmd_OAMS_CALIBRATE_PTFE_LENGTH,
             self.cmd_OAMS_CALIBRATE_PTFE_LENGTH_help),
            ("OAMS_CALIBRATE_HUB_HES", self.cmd_OAMS_CALIBRATE_HUB_HES,
             self.cmd_OAMS_CALIBRATE_HUB_HES_help),
            ("OAMS_PID_AUTOTUNE", self.cmd_OAMS_PID_AUTOTUNE,
             self.cmd_OAMS_PID_AUTOTUNE_help),
            ("OAMS_PID_SET", self.cmd_OAMS_PID_SET, self.cmd_OAMS_PID_SET_help),
            ("OAMS_CURRENT_PID_SET", self.cmd_OAMS_CURRENT_PID_SET,
             self.cmd_OAMS_CURRENT_PID_SET_help),
        ):
            gcode.register_mux_command(name, "OAMS", oams_id, handler, desc=desc)
        self._ext_register_commands(gcode, oams_id)

    # ------------------------------------------------ standalone blocking wait

    def _wait_for_action(self, timeout=OAMS_ACTION_TIMEOUT):
        """Bounded wait used by the low-level OAMS_* commands when no runtime is
        driving completion. Times out instead of hanging on a lost reply.
        Returns True if the wait timed out (the firmware op may still be
        running and should be cancelled where possible)."""
        endtime = self.reactor.monotonic() + timeout
        while self.action_status is not None:
            if self.reactor.monotonic() >= endtime:
                logging.warning("OAMS[%s]: timed out waiting for firmware status",
                                self.oams_idx)
                self.action_status_code = OAMS_OP_CODE_ERROR_UNSPECIFIED
                self.action_status = None
                return True
            self.reactor.pause(self.reactor.monotonic() + POLL_INTERVAL)
        return False

    # --------------------------------------------------------------- PID setup

    cmd_OAMS_CURRENT_PID_SET_help = "Set the PID values for the rewind current sensor"

    def cmd_OAMS_CURRENT_PID_SET(self, gcmd):
        p, i, d = gcmd.get_float("P"), gcmd.get_float("I"), gcmd.get_float("D")
        t = gcmd.get_float("TARGET", self.current_target)
        self._require_cmd(
            self.oams_current_pid_cmd, "rewind current PID command")
        self.oams_current_pid_cmd.send(
            [self.float_to_u32(p), self.float_to_u32(i),
             self.float_to_u32(d), self.float_to_u32(t)])
        self.current_kp, self.current_ki, self.current_kd = p, i, d
        self.current_target = t
        gcmd.respond_info("Current PID values set to P=%f I=%f D=%f TARGET=%f"
                          % (p, i, d, t))

    cmd_OAMS_PID_SET_help = "Set the PID values for the OAMS hub motor"

    def cmd_OAMS_PID_SET(self, gcmd):
        p, i, d = gcmd.get_float("P"), gcmd.get_float("I"), gcmd.get_float("D")
        t = gcmd.get_float("TARGET", self.fps_target)
        self._require_cmd(self.oams_pid_cmd, "PID command")
        self.oams_pid_cmd.send([self.float_to_u32(p), self.float_to_u32(i),
                                self.float_to_u32(d), self.float_to_u32(t)])
        self.kp, self.ki, self.kd, self.fps_target = p, i, d, t
        gcmd.respond_info("PID values set to P=%f I=%f D=%f TARGET=%f"
                          % (p, i, d, t))

    cmd_OAMS_PID_AUTOTUNE_help = "Report that automatic PID tuning is unsupported"

    def cmd_OAMS_PID_AUTOTUNE(self, gcmd):
        raise gcmd.error(
            "OAMS PID autotune is unsupported; use OAMS_PID_SET and "
            "OAMS_CURRENT_PID_SET with qualified values")

    # ----------------------------------------------------- firmware op senders

    def _require_dryer(self, gcmd):
        """Refuse at once, with a reason, a dryer command on a unit whose
        firmware reports no dryer (C3: a refusal is never silently ignored)."""
        if not self.capabilities.get("dryer", True):
            raise gcmd.error("This unit has no dryer")

    def _require_rfid(self, gcmd):
        """Refuse at once a tag command on a unit whose firmware reports no
        RFID reader."""
        if not self.capabilities.get("rfid", True):
            raise gcmd.error("This unit has no RFID reader")

    def _require_cmd(self, cmd, name):
        if cmd is None:
            raise self.printer.command_error(
                "OAMS[%s]: %s unavailable (MCU not connected or firmware"
                " initialization failed; see klippy.log)" % (self.oams_idx, name))
        return cmd

    @staticmethod
    def _wire_gen(gen):
        # The gen rides as a single byte (%c). The reducer already keeps op_gen
        # in 0..255; the standalone fallback path passes gen=None -> 0 (unused).
        return 0 if gen is None else (gen & 0xFF)

    @staticmethod
    def _wire_gen32(gen):
        # Protocol v4: the complete logical generation; gen=None -> 0 (unused).
        return 0 if gen is None else (gen & 0xFFFFFFFF)

    def _note_active(self, action, gen):
        # Protocol v4 only: remember the exact operation in flight so a stale
        # completion cannot move the driver mirrors (see _apply_action_status4).
        if self._use_wide_gen_protocol:
            self._active_action = action
            self._active_gen = self._wire_gen32(gen)

    def _track_gen(self, gen):
        # Legacy protocol only: remember the gen so the next completion can be
        # tagged with it. Under the *2 and v4 protocols the firmware echoes
        # the gen.
        if not self._use_gen_protocol:
            self._gen_queue.append(gen)

    def _require_motion(self):
        reason = ams_family.motion_block_reason(
            getattr(self, "capabilities", {}) or {})
        if reason is not None:
            raise self.printer.command_error(reason)

    def start_load_spool(self, spool_idx, gen=None):
        self._require_motion()
        # H10 (driver mechanism): a bay whose PTFE length is unset (None), 0,
        # or outside the uint32 wire range cannot be loaded — the unit would
        # either reject the 0 or feed against a wrong path. Validate before
        # resolving or sending anything, so a refusal leaves no partial state
        # and no phantom FIFO entry. The reducer refuses this earlier with the
        # same user-facing message; this is the per-unit backstop. A driver
        # that declares no length at all (a bare/legacy unit) has nothing to
        # validate or send and keeps its previous behavior.
        length = None
        if (getattr(self, "ptfe_lengths", None) is not None
                or getattr(self, "filament_path_length", None) is not None):
            length = self._ptfe_length_for_bay(spool_idx)
            if (length is None or length <= 0
                    or length > PTFE_LENGTH_MAX_CLICKS):
                raise self.printer.command_error(
                    "%s is not calibrated; run OAMS_CALIBRATE_PTFE_LENGTH"
                    " SPOOL=%d" % (self.spool_label(spool_idx), spool_idx))
        # Resolve the command and SEND before mutating any state: a lookup or
        # send failure must leave no phantom FIFO entry and no stuck sentinel
        # (a stale FIFO entry would desync completion matching for every
        # subsequent op on legacy firmware). The reply cannot race the
        # bookkeeping below — replies are marshalled back onto this same
        # reactor thread and only run after we return.
        #
        # F9: when the firmware advertises oams_cmd_load_spool3, send ONLY it,
        # with the bay's length. Programming the register and requesting the
        # load atomically closes the window in which the separate length write
        # could be lost or land on another bay. The path is taken only when
        # there is a length to program (a driver declaring no PTFE length has
        # nothing to write and keeps its legacy send).
        # The v4 family has no atomic variant: it sends the length write and
        # then load4, like the plain *2 family.
        atomic = (getattr(self, "oams_load_spool3_cmd", None) is not None
                  and length is not None
                  and not self._use_wide_gen_protocol)
        if self._use_wide_gen_protocol:
            cmd = self._require_cmd(
                self.oams_load_spool4_cmd, "protocol-v4 load command")
            args = [spool_idx, self._wire_gen32(gen)]
        elif atomic:
            cmd = self._require_cmd(self.oams_load_spool3_cmd, "load command")
            args = [spool_idx, self._wire_gen(gen), int(length)]
        elif self._use_gen_protocol:
            cmd = self._require_cmd(self.oams_load_spool2_cmd, "load command")
            args = [spool_idx, self._wire_gen(gen)]
        else:
            cmd = self._require_cmd(self.oams_load_spool_cmd, "load command")
            args = [spool_idx]
        # These handles use the same default MCU queue, so the length reaches
        # firmware before its load command. Re-send it on every load (never a
        # stale or 0 value) whenever the unit exposes the length register.
        if length is not None and not atomic:
            length_cmd = getattr(self, "oams_ptfe_length_cmd", None)
            if length_cmd is not None:
                length_cmd.send([int(length)])
        cmd.send(args)
        if hasattr(self, "filament_path_length"):
            self._selected_path_length = self._ptfe_length_for_bay(spool_idx)
            self._selected_ptfe_bay = spool_idx
        self._pending_bay = spool_idx
        self._track_gen(gen)
        self.action_status_code = None
        self.action_status = OAMS_STATUS_LOADING
        self._note_active(self.status_loading, gen)

    def start_unload_spool(self, gen=None):
        self._require_motion()
        if self._use_wide_gen_protocol:
            cmd = self._require_cmd(
                self.oams_unload_spool4_cmd, "protocol-v4 unload command")
            cmd.send([self._wire_gen32(gen)])
        elif self._use_gen_protocol:
            cmd = self._require_cmd(self.oams_unload_spool2_cmd, "unload command")
            cmd.send([self._wire_gen(gen)])
        else:
            cmd = self._require_cmd(self.oams_unload_spool_cmd, "unload command")
            cmd.send([])
        self._track_gen(gen)
        self.action_status_code = None
        self.action_status = OAMS_STATUS_UNLOADING
        self._note_active(self.status_unloading, gen)

    def start_calibrate(self, kind, bay, gen=None):
        self._require_motion()
        if self._use_wide_gen_protocol:
            cmd = self._require_cmd(
                self.oams_calibrate_hub_hes4_cmd if kind == "hub_hes"
                else self.oams_calibrate_ptfe_length4_cmd,
                "protocol-v4 calibrate command")
            cmd.send([bay, self._wire_gen32(gen)])
        elif self._use_gen_protocol:
            cmd = self._require_cmd(
                self.oams_calibrate_hub_hes2_cmd if kind == "hub_hes"
                else self.oams_calibrate_ptfe_length2_cmd,
                "calibrate command")
            cmd.send([bay, self._wire_gen(gen)])
        else:
            cmd = self._require_cmd(
                self.oams_calibrate_hub_hes_cmd if kind == "hub_hes"
                else self.oams_calibrate_ptfe_length_cmd,
                "calibrate command")
            cmd.send([bay])
        self._track_gen(gen)
        self.action_status_code = None
        self.action_status_value = None
        self.action_status = OAMS_STATUS_CALIBRATING
        self._note_active(self.status_calibrating, gen)

    def load_spool_cancel(self):
        if self._use_wide_gen_protocol:
            return self.cancel_operation(self.status_loading, 0)
        if self.oams_load_spool_cancel_cmd is None:
            return "OAMS load spool cancel command not available on this firmware"
        self.oams_load_spool_cancel_cmd.send()
        return "OAMS load spool operation cancelled"

    def cancel_operation(self, action, gen=None):
        """Cancel one named operation.

        Protocol v4 names both the action and the full generation, so a
        delayed or cross-action cancel cannot affect a newer operation. Older
        firmware can cancel a load only, with its original command.
        """
        if self._use_wide_gen_protocol:
            cmd = self._require_cmd(
                self.oams_cancel4_cmd, "protocol-v4 cancel command")
            cmd.send([action, self._wire_gen32(gen)])
            return "OAMS operation cancellation requested"
        if action != self.status_loading:
            return "OAMS unload cancel requires protocol v4 firmware"
        return self.load_spool_cancel()

    REVERSE_FOLLOWER_REFUSAL = (
        "this unit has no reverse follower; its unload retracts the filament"
        " on its own")

    @property
    def has_reverse_follower(self):
        """False only for a firmware that reports AMS_CAP_REVERSE_FOLLOWER=0
        (absent or 1: the reverse follower exists, as always)."""
        return self.capabilities.get("reverse_follower") is not False

    #: How long an enable request waits for the firmware's immediate reply.
    FOLLOWER_REPLY_WAIT = 0.3

    def set_oams_follower(self, enable, direction):
        """Send a follower request. The firmware replies at once with an
        `oams_action_status` for the follower action (C1 §4.2); a refusal
        (any code but success, e.g. a reverse follow it cannot do) is
        returned as a :class:`FollowerResult` and recorded in
        `follower_refusal`. An accepted request, a disable (always accepted)
        and a firmware that sends no reply return None, as before."""
        if enable:
            self._require_motion()
        if self.oams_follower_cmd is None:
            return None
        if enable and direction == FOLLOWER_REVERSE \
                and not self.has_reverse_follower:
            # AMS_CAP_REVERSE_FOLLOWER=0: refused here, never sent. The
            # unit's own unload retracts after the host's cut.
            self.follower_refusal = self.REVERSE_FOLLOWER_REFUSAL
            return FollowerResult(refused=True,
                                  reason=self.REVERSE_FOLLOWER_REFUSAL)
        self.follower_refusal = None
        self._follower_reply = None
        self.oams_follower_cmd.send([enable, direction])
        if not enable:
            return None
        reply = self._await_follower_reply()
        if reply is None or reply["code"] == OAMS_OP_CODE_SUCCESS:
            return None
        reason = "the unit refused the %s follower (%s)" % (
            "forward" if direction else "reverse",
            S.describe_code(reply["code"]))
        self.follower_refusal = reason
        logging.warning("OAMS[%s]: %s", self.oams_idx, reason)
        return FollowerResult(refused=True, reason=reason)

    def _await_follower_reply(self):
        deadline = self.reactor.monotonic() + self.FOLLOWER_REPLY_WAIT
        while self._follower_reply is None:
            now = self.reactor.monotonic()
            if now >= deadline:
                return None
            try:
                self.reactor.pause(min(deadline, now + 0.02))
            except Exception:
                return None      # where pausing is not allowed: no wait
        return self._follower_reply

    # --------------------------------------------- low-level OAMS_* commands

    def _result_message(self, code, verb):
        if code == OAMS_OP_CODE_SUCCESS:
            return "Spool %sed successfully" % verb
        if code == OAMS_OP_CODE_CANCEL:
            return "Spool %sing cancelled" % verb
        return "Spool %sing failed (%s)" % (verb, S.describe_code(code))

    cmd_OAMS_LOAD_SPOOL_help = "Load a specific bay on this OAMS"

    def cmd_OAMS_LOAD_SPOOL(self, gcmd):
        bay = gcmd.get_int("SPOOL", minval=0, maxval=self.bay_count - 1)
        if self.runtime is not None:
            # Route through the store so it is the single source of truth (and
            # the bay gets runout protection if it belongs to a group).
            result = self.runtime.request(
                self.fps_name, S.LoadBay(self.fps_name, (self.oams_idx, bay))).wait()
            if result.ok or result.code == OAMS_OP_CODE_CANCEL:
                gcmd.respond_info(result.message)
            else:
                raise gcmd.error(result.message)
            return
        # Standalone fallback (no manager bound): bounded blocking.
        self.start_load_spool(bay)
        if self._wait_for_action():
            # The firmware op is still running; tell it to stop feeding.
            self.load_spool_cancel()
        code = self.action_status_code
        if code in (OAMS_OP_CODE_SUCCESS, OAMS_OP_CODE_CANCEL):
            gcmd.respond_info(self._result_message(code, "load"))
        else:
            raise gcmd.error(self._result_message(code, "load"))

    cmd_OAMS_UNLOAD_SPOOL_help = "Unload the spool loaded on this OAMS"

    def cmd_OAMS_UNLOAD_SPOOL(self, gcmd):
        # H15b: a unit that must not move refuses the unload here, with its
        # own reason, rather than failing inside the driver later.
        reason = ams_family.motion_block_reason(
            getattr(self, "capabilities", None) or {})
        if reason is not None:
            raise gcmd.error(reason)
        requested = gcmd.get_int("SPOOL", None)
        if self.runtime is not None:
            lane = self.runtime.get_state().lanes.get(self.fps_name)
            unit = lane.unit if lane is not None else None
            if unit is None or unit[0] != self.oams_idx:
                self.set_oams_follower(0, FOLLOWER_REVERSE)
                gcmd.respond_info("No spool loaded on this OAMS; nothing to"
                                  " unload (follower stopped)")
                return
            if requested is not None and requested != unit[1]:
                raise gcmd.error("Refusing to unload: %s is loaded on this"
                                 " OAMS, not %s" % (self.spool_label(unit[1]),
                                                    self.spool_label(requested)))
            result = self.runtime.request(self.fps_name, S.Unload(self.fps_name)).wait()
            if result.ok:
                gcmd.respond_info(result.message)
            else:
                raise gcmd.error(result.message)  # store already stopped follower
            return
        # Standalone fallback (no manager bound).
        if self.current_spool is None:
            self.set_oams_follower(0, FOLLOWER_REVERSE)
            gcmd.respond_info("No spool is currently loaded on this OAMS; nothing"
                              " to unload (follower stopped)")
            return
        if requested is not None and requested != self.current_spool:
            raise gcmd.error("Refusing to unload: %s is loaded on this"
                             " OAMS, not %s" % (self.spool_label(self.current_spool),
                                                self.spool_label(requested)))
        self.start_unload_spool()
        self._wait_for_action()
        code = self.action_status_code
        if code == OAMS_OP_CODE_SUCCESS:
            gcmd.respond_info(self._result_message(code, "unload"))
        else:
            self.set_oams_follower(0, FOLLOWER_REVERSE)
            raise gcmd.error(self._result_message(code, "unload"))

    cmd_OAMS_FOLLOWER_help = "Enable or disable the follower and set its direction"

    def cmd_OAMS_FOLLOWER(self, gcmd):
        enable = gcmd.get_int("ENABLE", minval=0, maxval=1)
        direction = gcmd.get_int("DIRECTION", minval=0, maxval=1)
        # When this unit is the lane's loaded unit, go through the store so
        # LaneState.following/direction stay truthful; otherwise drive the
        # hardware directly (the store does not track idle units).
        lane = None
        if self.runtime is not None:
            lane = self.runtime.get_state().lanes.get(self.fps_name)
        if lane is not None and lane.unit is not None \
                and lane.unit[0] == self.oams_idx:
            self.runtime.dispatch(S.Follow(self.fps_name, enable, direction))
        else:
            self.set_oams_follower(enable, direction)
        if enable and self.follower_refusal:
            raise gcmd.error(self.follower_refusal)
        if enable == 0:
            gcmd.respond_info("Follower disabled")
        elif direction == FOLLOWER_FORWARD:
            gcmd.respond_info("Follower enabled in forward direction")
        else:
            gcmd.respond_info("Follower enabled in reverse direction")

    # -------------------------------------------------- AMS HT safe controls

    _AMS_HT_RESULTS = {
        0: "accepted", 1: "busy", 2: "invalid bay",
        3: "invalid parameter", 4: "faulted", 5: "unavailable",
    }

    def _send_ams_ht_control(self, command, args, operation, gcmd):
        self._require_cmd(command, operation)
        params = command.send(args)
        result = -1 if params is None else params.get("result", -1)
        if result != 0:
            raise gcmd.error("%s rejected: %s (%d)" % (
                operation, self._AMS_HT_RESULTS.get(result, "unknown"), result))

    cmd_OAMS_DRYER_START_help = "Start the dryer within firmware limits"

    def cmd_OAMS_DRYER_START(self, gcmd):
        self._require_link(gcmd)
        self._require_dryer(gcmd)
        if self.capabilities.get("heater_control") is False:
            raise gcmd.error("This firmware does not support heater control")
        blocked = self._dryer_blocked_by_load()
        if blocked is not None:
            raise gcmd.error(
                "%s does not dry while filament is loaded: unload %s first,"
                " then start the dryer"
                % (self.name.split()[-1], self.spool_label(blocked)))
        if self._adapter_status() is False:
            raise gcmd.error(
                "the dryer needs the power adapter; connect it and retry")
        # H6: ROTATE is a host-side gate only; no rotation parameter exists on
        # the wire (C1), so the value never reaches the firmware.
        rotate = gcmd.get_int("ROTATE", 0, minval=0, maxval=1)
        if rotate and not self.capabilities.get(
                "spool_rotation_during_drying"):
            raise gcmd.error("this unit cannot rotate spools while drying")
        low = self.capabilities.get("dryer_target_min_c") or 30.0
        high = self.capabilities.get("dryer_target_max_c") or 80.0
        target = gcmd.get_float("TARGET", minval=low, maxval=high)
        duration = gcmd.get_int(
            "DURATION", 3600, minval=1, maxval=7 * 24 * 60 * 60)
        hold_after = gcmd.get_int("HOLD_AFTER", 0, minval=0, maxval=1)
        running = self._dryer_cycle_running()
        try:
            self._send_ams_ht_control(
                self.ams_ht_dryer_start_cmd,
                [int(round(target * 100.0)), duration, hold_after],
                "%s dryer start" % self._unit_label(), gcmd)
        except Exception as exc:
            if running and "busy" in str(exc):
                # The firmware's answer (C1 §4.4: Busy) is authoritative; say
                # what the host knows about the cycle in the way.
                data = self._telemetry.get("dryer") or {}
                left = data.get("remaining_s")
                raise gcmd.error(
                    "a dryer cycle is already running (%s%s); OAMS_DRYER_STOP"
                    " first (%s)" % (
                        data.get("state_name"),
                        "" if left is None else ", %d s left" % left, exc))
            raise
        gcmd.respond_info(
            "%s: dryer started at %.1fC for %ds (hold_after=%d)"
            % (self._unit_label(), target, duration, hold_after))

    cmd_OAMS_DRYER_STOP_help = "Stop the unit's dryer safely"

    def cmd_OAMS_DRYER_STOP(self, gcmd):
        self._require_link(gcmd)
        self._require_dryer(gcmd)
        label = self._unit_label()
        self._send_ams_ht_control(
            self.ams_ht_dryer_stop_cmd, [], "%s dryer stop" % label, gcmd)
        gcmd.respond_info("%s: dryer stopped" % label)

    cmd_OAMS_CLEAR_FAULT_help = "Clear a recoverable unit firmware fault"

    # The firmware answers a clear when it accepts it, not when its recovery
    # ends, and reports no result of its own (C1 has no terminal status for
    # clear). Read the unit's latched faults once recovery has had time.
    CLEAR_FAULT_CHECK_S = 15.0

    def cmd_OAMS_CLEAR_FAULT(self, gcmd):
        self._require_link(gcmd)
        label = self._unit_label()
        self._send_ams_ht_control(
            self.ams_ht_clear_fault_cmd, [], "%s fault clear" % label, gcmd)
        gcmd.respond_info(
            "%s: fault clear accepted; checking the result in %.0fs"
            % (label, self.CLEAR_FAULT_CHECK_S))
        self.reactor.register_timer(
            self._clear_fault_check,
            self.reactor.monotonic() + self.CLEAR_FAULT_CHECK_S)

    def _clear_fault_check(self, eventtime):
        domain = self._telemetry.get("domain")
        label = self._unit_label()
        if domain is None:
            message = "%s: no fault status to confirm the clear" % label
        else:
            latched = [domain.get("%s_fault_name" % kind) or "unknown"
                       for kind in ("motion", "dryer", "system")
                       if domain.get("%s_fault" % kind)]
            message = (
                "%s: fault still latched after the clear: %s"
                % (label, ", ".join(latched)) if latched
                else "%s: fault cleared" % label)
        self.printer.lookup_object("gcode").respond_info(message)
        return self.reactor.NEVER

    cmd_OAMS_RFID_SCAN_help = "Read the spool tag of a bay when idle"

    def cmd_OAMS_RFID_SCAN(self, gcmd):
        self._require_link(gcmd)
        if self.ams_get_tray_info_cmd is not None:
            self._cmd_tray_read(gcmd)       # unit-decoded tags (C1 4.7.1)
            return
        self._require_rfid(gcmd)
        self._ext_rfid_scan(gcmd)

    def _cmd_tray_read(self, gcmd):
        """``OAMS_RFID_SCAN BAY=`` on a unit that decodes its own tags.

        With AMS_CAP_TRAY_READ it asks the unit to read the bay (the unit
        may turn the spool, so the positioning refusal and busy retries
        apply); the outcome arrives later as ``ams_tray_info``
        and is published as the bay's tag. Without it the unit reads on its
        own, and this reports what it last said."""
        raw_bay = gcmd.get("BAY", None)
        if raw_bay is None:
            raise gcmd.error("BAY is required: this unit reads its spools'"
                             " tags per bay")
        bay = int(raw_bay)
        if not 0 <= bay < self.bay_count:
            raise gcmd.error(
                "BAY must be between 0 and %d" % (self.bay_count - 1))
        if self.ams_tray_read_cmd is None:
            record = self.tag_reader.tray_record(bay)
            gcmd.respond_info(
                "This unit reads its tags itself and cannot be told to read;"
                " %s: %s" % (self.spool_label(bay),
                             "no report yet" if record is None
                             else "state %d" % record["state"]))
            return
        if bay in self.positioning_bays():
            raise gcmd.error(
                "%s is positioning; wait for the firmware to"
                " complete insertion" % self.spool_label(bay))
        result_name = self.tag_reader.tray_read_now(bay)
        if result_name != "accepted":
            gcmd.respond_info("Tray read: %s (%s)"
                              % (result_name, self.spool_label(bay)))
            return
        gcmd.respond_info("Tray read started (%s); the result follows as the"
                          " bay's tag" % self.spool_label(bay))


    # ------------------------------------------------- calibration (async/store)

    def _calibrate(self, kind, bay):
        if self.runtime is not None:
            result = self.runtime.request(
                self.fps_name, S.Calibrate(self.fps_name, self.oams_idx, bay, kind)
            ).wait()
            return result.ok, result.value
        # Standalone fallback (no manager): bounded blocking.
        self.start_calibrate(kind, bay)
        self._wait_for_action()
        ok = self.action_status_code == OAMS_OP_CODE_SUCCESS
        return ok, self.action_status_value

    def _persist_option(self, option, value, gcmd):
        """Persist one option on this [oams ...] section. Routes through the
        manager's in-place file writeback (SAVE_CONFIG can't reach an included
        subfile like oams.cfg); falls back to configfile.set only if no manager
        is present (unsupported, but never silently lose the value)."""
        mgr = self.printer.lookup_object("oams_manager", None)
        if mgr is not None and hasattr(mgr, "persist_config_option"):
            mgr.persist_config_option(self.name, option, value)
            return "saved to %s" % mgr.openams_config_path
        configfile = self.printer.lookup_object("configfile")
        configfile.set(self.name, option, value)
        return "run SAVE_CONFIG to persist"

    cmd_OAMS_CALIBRATE_HUB_HES_help = "Calibrate the range of a single hub HES"

    def cmd_OAMS_CALIBRATE_HUB_HES(self, gcmd):
        bay = gcmd.get_int("SPOOL", minval=0, maxval=self.bay_count - 1)
        if not self.capabilities.get("hub_hes_calibration"):
            raise gcmd.error("Hub HES calibration is unsupported by this unit")

        ok, value = self._calibrate("hub_hes", bay)
        if not ok:
            raise gcmd.error("Calibration of HES %d failed" % bay)
        threshold = self.u32_to_float(value)
        self.hub_hes_on[bay] = threshold
        how = self._persist_option(
            "hub_hes_on", ",".join(map(str, self.hub_hes_on)), gcmd)
        gcmd.respond_info("Calibrated HES %d to %f threshold (%s)."
                          % (bay, threshold, how))

    def _ptfe_calibration_error_message(self, spool):
        """A specific, actionable message for a failed PTFE calibration.
        self.action_status_code/self.action_status_value are the firmware's
        reply to this calibration, set by _apply_action_status just before the
        request this method is called from returns."""
        code = self.action_status_code
        if code == OAMS_OP_CODE_ERROR_BUSY:
            return "Cannot calibrate PTFE length: OAMS is busy with another operation."
        if code == OAMS_OP_CODE_SPOOL_ALREADY_IN_BAY:
            return (
                "Cannot calibrate PTFE length: filament is already loaded at "
                "the hub or follower mode is active. Run "
                "OAMS_UNLOAD_SPOOL OAMS=%d before retrying." % self.oams_idx
            )
        if code == OAMS_OP_CODE_NO_SPOOL_IN_BAY:
            return (
                "Cannot calibrate PTFE length: no filament was detected in "
                "%s. Insert filament in that spool position and retry."
                % self.spool_label(spool)
            )
        if code == OAMS_OP_CODE_ERROR_KLIPPER_CALL:
            return "PTFE calibration was stopped by Klipper."
        if code == OAMS_OP_CODE_CANCEL:
            return "PTFE calibration was cancelled."
        if code == OAMS_OP_CODE_TIMEOUT:
            clicks = self.action_status_value or 0
            if clicks > 0:
                distance_mm = clicks / FILAMENT_PATH_LENGTH_FACTOR
                travel = " after feeding approximately %.1f mm (%d encoder clicks)" % (
                    distance_mm,
                    clicks,
                )
            else:
                travel = " with no forward travel reported"
            return (
                "PTFE calibration timed out in firmware after %d s%s. The "
                "filament did not reach the expected sensor state. Check the "
                "filament path and PTFE tube, then run OAMS_UNLOAD_SPOOL "
                "OAMS=%d before retrying because the filament position is "
                "unknown."
                % (
                    OAMS_FIRMWARE_OPERATION_TIMEOUT_SECONDS,
                    travel,
                    self.oams_idx,
                )
            )
        return "PTFE calibration failed with firmware error code %s." % code

    cmd_OAMS_CALIBRATE_PTFE_LENGTH_help = "Calibrate the length of the PTFE tube"

    def cmd_OAMS_CALIBRATE_PTFE_LENGTH(self, gcmd):
        # H15b: checked before the capability test, whose "unsupported" text
        # would be misleading for a unit that is merely not allowed to move.
        reason = ams_family.motion_block_reason(
            getattr(self, "capabilities", None) or {})
        if reason is not None:
            raise gcmd.error(reason)
        bay = gcmd.get_int("SPOOL", minval=0, maxval=self.bay_count - 1)
        if not self.capabilities.get("ptfe_calibration"):
            raise gcmd.error("PTFE calibration is unsupported by this unit")

        ok, value = self._calibrate("ptfe", bay)
        if not ok:
            raise gcmd.error(self._ptfe_calibration_error_message(bay))
        option = "ptfe_length" if self.bay_count == 1 else "ptfe_length_%d" % bay
        how = self._persist_option(option, "%d" % (value,), gcmd)
        if self.bay_count == 1:
            self.filament_path_length = value
        else:
            self.ptfe_lengths[bay] = value
        if getattr(self, "_selected_ptfe_bay", None) in (None, bay):
            self._selected_path_length = value
        gcmd.respond_info("Calibrated PTFE length to %d (%s)." % (value, how))

    # ----------------------------------------------------- firmware callbacks

    def _oams_cmd_stats(self, params):
        # Serial reader thread. Publish each array as a fresh list (atomic ref
        # swap) so reactor-thread readers always see a consistent snapshot.
        self.fps_value = self.u32_to_float(params["fps_value"])
        self._note_f1s(
            [params["f1s_hes_value_0"], params["f1s_hes_value_1"],
             params["f1s_hes_value_2"], params["f1s_hes_value_3"]],
            params.get("#receive_time", self.reactor.monotonic()))
        self.hub_hes_value = [params["hub_hes_value_0"], params["hub_hes_value_1"],
                              params["hub_hes_value_2"], params["hub_hes_value_3"]]
        self.encoder_clicks = params["encoder_clicks"]
        was_ready = getattr(self, "hardware_snapshot_ready", False)
        self.hardware_snapshot_ready = True
        tag_reader = getattr(self, "tag_reader", None)
        if not was_ready and tag_reader is not None:
            tag_reader.note_hardware_ready()
        self.last_stats_time = params.get("#receive_time",
                                          self.reactor.monotonic())

    def _oams_cmd_current_stats(self, params):
        self.i_value = self.u32_to_float(params["current_value"])

    def _action_status_received(self, params):
        # Serial reader thread -> marshal onto the reactor thread so the
        # completion bookkeeping (and any waiter) stays single-threaded.
        self.reactor.register_async_callback(
            lambda et, params=params: self._apply_action_status(params))

    def _action_status2_received(self, params):
        # Generation-matched variant: the firmware echoes the op gen, so it is
        # authoritative (no FIFO inference).
        self.reactor.register_async_callback(
            lambda et, params=params: self._apply_action_status(
                params, wire_gen=params["gen"]))

    def _action_status4_received(self, params):
        # Protocol v4 carries the complete logical generation.
        self.reactor.register_async_callback(
            lambda et, params=params: self._apply_action_status(
                params, wire_gen=params["gen"]))

    def _clear_hub_after_unload(self):
        """Mirror the hub HES the firmware just read as clear.

        An unload ends when the unloaded bay's hub HES clears, and the unit
        reports success at once, but the hub readings reach the host only in
        the periodic oams_cmd_stats (every 450 ms on legacy firmware). Until
        the next one arrives, the cached bit would still say "filament in the
        hub", and a load issued right after the unload (every toolchange)
        would be refused by the reducer's hub check. The bay is the one the
        driver loaded, or, when that is unknown (after a restart), the only
        hub that reads filament, which is the bay the firmware unloads. Stats
        sent before the completion are handled before it, so a stale reading
        cannot overwrite this afterwards."""
        hub = list(getattr(self, "hub_hes_value", None) or ())
        bay = self.current_spool
        if bay is None:
            count = getattr(self, "bay_count", len(hub))
            on = [b for b, v in enumerate(hub[:count]) if v]
            bay = on[0] if len(on) == 1 else None
        if bay is not None and 0 <= bay < len(hub) and hub[bay]:
            hub[bay] = 0
            self.hub_hes_value = hub  # a fresh list, as _oams_cmd_stats does

    def _apply_action_status(self, params, wire_gen=None):
        action = params["action"]
        code = params["code"]
        if self._use_wide_gen_protocol and wire_gen is not None:
            self._apply_action_status4(params, wire_gen)
            return
        if action == self.status_calibrating:
            self.action_status_value = params["value"]
            self.action_status_code = code
        # Verified against firmware 2.0.25: code 5 (KLIPPER_CALL) only ever
        # co-occurs with action=CALIBRATING, so the `or` clause is redundant
        # there — kept for robustness against other firmware versions. The
        # follower paths (actions 2-5) only use codes BUSY/NO_SPOOL_IN_BAY.
        elif action in (self.status_loading, self.status_unloading,
                        self.status_error) or code == OAMS_OP_CODE_ERROR_KLIPPER_CALL:
            self.action_status_code = code
            # Keep the firmware mirror of current_spool in step with our commands.
            if action == self.status_loading and code == OAMS_OP_CODE_SUCCESS:
                self.current_spool = self._pending_bay
            elif action == self.status_unloading and code == OAMS_OP_CODE_SUCCESS:
                self._clear_hub_after_unload()
                self.current_spool = None
        else:
            # Follower/coast/stop notifications and anything unrecognized are
            # NOT op completions: dispatching them would falsely complete (or
            # fail) an in-flight load/unload on this lane. Log and drop.
            if action in (OAMS_STATUS_FORWARD_FOLLOWING,
                          OAMS_STATUS_REVERSE_FOLLOWING,
                          OAMS_STATUS_COASTING, OAMS_STATUS_STOPPED):
                # The firmware's immediate reply to a follower request
                # (C1 §4.2): kept for set_oams_follower, which reports a
                # refusal. It is not an op completion.
                self._follower_reply = {"action": action, "code": code}
                return
            logging.info("OAMS[%s]: ignoring non-completion action status"
                         " action=%d code=%d", self.oams_idx, action, code)
            return
        # F8: the unit ended the op because it lost its host session. Take
        # the session-loss path first (it settles the in-flight op with the
        # FIRMWARE_RESTART reason and blocks motion); the completion below
        # then finds nothing in flight.
        if (code == S.OAMS_OP_CODE_SESSION_LOST
                and getattr(self, "session_lost", None) is None):
            self._on_session_lost()
        # Publish the completion sentinel last, then notify the store (if
        # bound). The gen is the firmware echo (wire_gen) under the *2 protocol,
        # or the oldest pending FIFO entry on legacy firmware. Either way the
        # reducer rejects a gen that does not match the in-flight op; an
        # unsolicited completion-class status (gen=None / empty queue) is
        # never accepted.
        self.action_status = None
        if wire_gen is not None:
            gen = wire_gen
        elif self._gen_queue:
            gen = self._gen_queue.popleft()
        else:
            gen = None
        if self.on_action_complete is not None:
            self.on_action_complete(self.action_status_code,
                                    self.action_status_value, gen)

    def _apply_action_status4(self, params, gen):
        """Protocol-v4 completion: the echoed generation is exact. Every
        status still reaches the reducer, which keeps the audit trail and
        rejects a stale one; the driver mirrors (current_spool, the waiter
        sentinel) move only when action and generation match the operation in
        flight, so a stale completion is observable but physically inert."""
        action = params["action"]
        code = params["code"]
        calibrating = action == self.status_calibrating
        if not (calibrating or code == OAMS_OP_CODE_ERROR_KLIPPER_CALL
                or action in (self.status_loading, self.status_unloading,
                              self.status_error)):
            logging.info("OAMS[%s]: ignoring non-completion action status"
                         " action=%d code=%d", self.oams_idx, action, code)
            return
        value = params["value"] if calibrating else None
        if (code == S.OAMS_OP_CODE_SESSION_LOST
                and getattr(self, "session_lost", None) is None):
            self._on_session_lost()
        if (self._active_action is not None and gen == self._active_gen
                and action in (self._active_action, self.status_error)):
            if calibrating:
                self.action_status_value = value
            self.action_status_code = code
            if action == self.status_loading and code == OAMS_OP_CODE_SUCCESS:
                self.current_spool = self._pending_bay
            elif (action == self.status_unloading
                  and code == OAMS_OP_CODE_SUCCESS):
                self._clear_hub_after_unload()
                self.current_spool = None
            self._active_action = None
            self._active_gen = None
            # Publish the completion sentinel last.
            self.action_status = None
        elif self._active_action is not None:
            logging.info(
                "OAMS[%s]: stale completion ignored by the driver mirror"
                " action=%s gen=%s (active action=%s gen=%s)",
                self.oams_idx, action, gen,
                self._active_action, self._active_gen)
        if self.on_action_complete is not None:
            self.on_action_complete(code, value, gen)

    # -------------------------------------------------------------- utilities

    def float_to_u32(self, f):
        return struct.unpack("I", struct.pack("f", f))[0]

    def u32_to_float(self, i):
        return struct.unpack("f", struct.pack("I", i))[0]

    def _build_config(self):
        self._register_link_status_early()
        self._register_tray_info_early()
        self._register_status_handlers_early()
        self.mcu.add_config_cmd(
            "config_oams_buffer upper=%u lower=%u is_reversed=%u"
            % (self.float_to_u32(self.fps_upper_threshold),
               self.float_to_u32(self.fps_lower_threshold), self.fps_is_reversed))
        self.mcu.add_config_cmd(
            "config_oams_f1s_hes on1=%u on2=%u on3=%u on4=%u is_above=%u"
            % (self.float_to_u32(self.f1s_hes_on[0]),
               self.float_to_u32(self.f1s_hes_on[1]),
               self.float_to_u32(self.f1s_hes_on[2]),
               self.float_to_u32(self.f1s_hes_on[3]), self.f1s_hes_is_above))
        self.mcu.add_config_cmd(
            "config_oams_hub_hes on1=%u on2=%u on3=%u on4=%u is_above=%u"
            % (self.float_to_u32(self.hub_hes_on[0]),
               self.float_to_u32(self.hub_hes_on[1]),
               self.float_to_u32(self.hub_hes_on[2]),
               self.float_to_u32(self.hub_hes_on[3]), self.hub_hes_is_above))
        self.mcu.add_config_cmd(
            "config_oams_pid kp=%u ki=%u kd=%u target=%u"
            % (self.float_to_u32(self.kp), self.float_to_u32(self.ki),
               self.float_to_u32(self.kd), self.float_to_u32(self.fps_target)))
        self.mcu.add_config_cmd(
            "config_oams_ptfe length=%u" % (self.filament_path_length,))
        self.mcu.add_config_cmd(
            "config_oams_current_pid kp=%u ki=%u kd=%u target=%u"
            % (self.float_to_u32(self.current_kp),
               self.float_to_u32(self.current_ki),
               self.float_to_u32(self.current_kd),
               self.float_to_u32(self.current_target)))
        self.mcu.add_config_cmd("config_oams_logger idx=%u" % (self.oams_idx,))


def load_config_prefix(config):
    return OAMS(config)
