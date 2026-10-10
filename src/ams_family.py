# OpenAMS hardware-family capability model (pure; no Klipper imports).
#
# One registry, two sources of units: the AMS families (ams1, ams_ht, ams2)
# and every klipper-mmu device (klipper_mmu/host/__init__.py
# _OPENAMS_DEVICE_IDS). The klipper-mmu names are accepted exactly as that set
# spells them, so `[klipper_mmu <name>] openams_family: boxturtle` is the same
# string the C2 schemas publish.

if __package__:
    from . import oams_ext
else:               # src/ on sys.path (the contract tools, some tests)
    import oams_ext

SCHEMA_VERSION = 1

_ALIASES = {
    "ams1": "ams1",
    "ams": "ams1",
    "ams_1": "ams1",
    "ams2": "ams2",
    "ams_2": "ams2",
    "ams_2_pro": "ams2",
    "ams2_pro": "ams2",
    "ams_ht": "ams_ht",
    "ht": "ams_ht",
    "auto": "auto",
}

_PROFILES = {
    "ams1": {
        "family": "ams1", "display_name": "AMS 1", "bays": 4,
        "spool_drive_count": 4,
        "feed_motor_control": "device_owned_speed_current",
        "device_owned_motion_control": True,
        "dryer": False, "dryer_zone_count": 0, "dryer_module_count": 0,
        "dryer_independent_zones": False,
        "heater_count": 0, "fan_count": 0,
        "dryer_temperature_sensor_count": 0,
        "spool_rotation_during_drying": False,
        "dryer_target_min_c": None, "dryer_target_max_c": None,
        "vent_count": 0, "autonomous_follower": True,
        "ptfe_calibration": True,
        "hub_hes_calibration": True, "rfid": True,
        "host_sensor_configuration": True, "runtime_pid_tuning": True,
        "logger_control": True, "error_led_control": True,
        "rfid_reader_scope": "per_bay", "rfid_uid_inventory": True,
        "rfid_material_metadata": "host_owned",
    },
    "ams_ht": {
        "family": "ams_ht", "display_name": "AMS HT", "bays": 1,
        "spool_drive_count": 1,
        "feed_motor_control": "device_owned_speed_current",
        "device_owned_motion_control": True,
        "dryer": True, "dryer_zone_count": 1, "dryer_module_count": 1,
        "dryer_independent_zones": False,
        "heater_count": 1, "fan_count": 1,
        "dryer_temperature_sensor_count": 2,
        # The hardware can rotate its spool, but no unit exposes dryer-driven
        # rotation as a supported operation yet.
        "spool_rotation_during_drying": False,
        "dryer_target_min_c": 30.0, "dryer_target_max_c": 80.0,
        "vent_count": 2, "autonomous_follower": True,
        "ptfe_calibration": True,
        "hub_hes_calibration": False, "rfid": False,
        "host_sensor_configuration": False, "runtime_pid_tuning": True,
        "logger_control": False, "error_led_control": False,
        "rfid_reader_scope": "single_bay", "rfid_uid_inventory": False,
        "rfid_material_metadata": "host_owned",
    },
    "ams2": {
        "family": "ams2", "display_name": "AMS 2 Pro", "bays": 4,
        "spool_drive_count": 4,
        "feed_motor_control": "device_owned_speed_current",
        "device_owned_motion_control": True,
        # Two heater/fan/sensor modules; whether a unit exposes them as one or
        # two independently targetable zones is unknown until it publishes
        # AMS_DRYER_ZONES.
        "dryer": True, "dryer_zone_count": None, "dryer_module_count": 2,
        "dryer_independent_zones": None,
        "heater_count": 2, "fan_count": 2,
        "dryer_temperature_sensor_count": 2,
        "spool_rotation_during_drying": True,
        "dryer_target_min_c": 30.0, "dryer_target_max_c": 65.0,
        "vent_count": 2, "autonomous_follower": True,
        "ptfe_calibration": True,
        "hub_hes_calibration": True, "rfid": False,
        "host_sensor_configuration": False, "runtime_pid_tuning": True,
        "logger_control": False, "error_led_control": False,
        "rfid_reader_scope": "per_bay", "rfid_uid_inventory": False,
        "rfid_material_metadata": "device_synchronized",
    },
}

# The klipper-mmu selector kinds, in klipper-mmu's own spelling
# (mcu/Kconfig.selector: KLIPPER_MMU_SELECTOR_KIND_*). NONE means independent
# per-lane feeders, which C2 names a feeder per lane.
SELECTOR_KINDS = ("feeder_per_lane", "linear", "rotary", "servo", "indexed")
_SELECTOR_BY_FIRMWARE = {
    "feeder_per_lane": "KLIPPER_MMU_SELECTOR_KIND_NONE",
    "linear": "KLIPPER_MMU_SELECTOR_KIND_LINEAR",
    "rotary": "KLIPPER_MMU_SELECTOR_KIND_ROTARY",
    "servo": "KLIPPER_MMU_SELECTOR_KIND_SERVO",
    "indexed": "KLIPPER_MMU_SELECTOR_KIND_INDEXED",
}

# One entry per _OPENAMS_DEVICE_IDS key: display name, default lane count,
# selector kind, and (lanes_min, lanes_max) when the device ships more than
# one lane count. Sources, per column: display name and lane counts from
# klipper_mmu/README.md "What it drives" (site/devices.html is the same
# table), the dossier gate counts in klipper_mmu/docs/devices/*.json, and the
# selector kinds from mcu/Kconfig.selector's choice block.
_MMU_DEVICES = (
    ("boxturtle", "BoxTurtle", 4, "feeder_per_lane", 4, 8),
    ("nightowl", "NightOwl", 2, "feeder_per_lane", 2, 4),
    ("emu", "EMU", 5, "feeder_per_lane", 5, 5),
    ("kms", "KMS", 4, "feeder_per_lane", 4, 4),
    ("qidi_box", "QIDI Box", 4, "feeder_per_lane", 4, 4),
    ("angry_beaver", "Angry Beaver", 4, "feeder_per_lane", 4, 4),
    ("3ms", "3MS", 4, "feeder_per_lane", 4, 4),
    ("quattrobox_1_0", "QuattroBox 1.0", 4, "feeder_per_lane", 4, 4),
    ("quattrobox_1_1", "QuattroBox 1.1", 4, "feeder_per_lane", 4, 4),
    ("quattrobox_2", "QuattroBox 2", 4, "feeder_per_lane", 4, 4),
    # The generic profile: its selector kind is a build choice that defaults
    # to independent feeders, and its lane count is the firmware's own 1..12
    # override (mcu/Kconfig KLIPPER_MMU_LANE_COUNT).
    ("custom", "Custom", 4, "feeder_per_lane", 1, 12),
    ("ercf_1_1", "ERCF 1.1", 9, "linear", 9, 9),
    ("ercf_2_0", "ERCF 2.0", 8, "linear", 8, 8),
    ("ercf_3_0", "ERCF 3.0", 8, "linear", 8, 8),
    ("tradrack", "Tradrack", 10, "linear", 10, 10),
    ("3d_chameleon", "3D Chameleon", 4, "rotary", 4, 4),
    ("htlf", "HTLF", 4, "rotary", 4, 4),
    ("mmx6", "MMX6", 6, "rotary", 6, 6),
    ("mmx", "MMX", 4, "servo", 4, 4),
    ("low_rider", "Low Rider", 6, "servo", 6, 6),
    ("pico_mmu", "Pico MMU", 4, "servo", 4, 4),
    ("btt_vivid", "BTT ViViD", 4, "indexed", 4, 4),
    ("claymore", "Claymore", 4, "indexed", 4, 4),
)


def _mmu_profile(family, display_name, lanes, selector_kind, low, high):
    """A klipper-mmu device profile.

    Capability defaults: no dryer and no RFID (neither device has a heater or
    a tag reader), and motion as klipper-mmu reports it -- the MCU owns the
    load, unload and follow loop and publishes no motor current or temperature
    channel, so motor telemetry is off and the only encoder is the one the
    linear machines wire in.
    """
    return {
        "family": family, "display_name": display_name,
        "bays": lanes, "bays_min": low, "bays_max": high,
        "selector_kind": selector_kind,
        "selector_kind_firmware": _SELECTOR_BY_FIRMWARE[selector_kind],
        # The four ops C5 has and an AMS unit does not, reached through
        # the OAMS_PRELOAD/OAMS_UNPRELOAD/OAMS_EJECT/OAMS_HOME per-unit
        # commands (C3 §2). Every device can stage, release and eject a bay;
        # homing needs a selector to move, so a feeder-per-lane unit has
        # nothing to home.
        "preload": True, "unpreload": True, "eject": True,
        "home": selector_kind != "feeder_per_lane",
        "feeder_drive_type": "klipper_mmu_shared_core_stepper_feeder",
        "spool_drive_count": lanes,
        "feed_motor_control": "device_owned_speed_current",
        "device_owned_motion_control": True,
        "normal_motion": True,
        "encoder": selector_kind == "linear",
        "dryer": False, "dryer_zone_count": 0, "dryer_module_count": 0,
        "dryer_independent_zones": False,
        "heater_count": 0, "fan_count": 0,
        "dryer_temperature_sensor_count": 0,
        "spool_rotation_during_drying": False,
        "dryer_target_min_c": None, "dryer_target_max_c": None,
        "vent_count": 0, "autonomous_follower": True,
        "ptfe_calibration": False,
        "hub_hes_calibration": False, "rfid": False,
        "host_sensor_configuration": False, "runtime_pid_tuning": False,
        "logger_control": False, "error_led_control": False,
        "rfid_reader_scope": None, "rfid_uid_inventory": False,
        "rfid_material_metadata": None,
    }


for _name, _display, _lanes, _kind, _low, _high in _MMU_DEVICES:
    _PROFILES[_name] = _mmu_profile(
        _name, _display, _lanes, _kind, _low, _high)
    _ALIASES[_name] = _name
del _name, _display, _lanes, _kind, _low, _high

# Every family the registry knows, in a stable order: the AMS families
# first, then the klipper-mmu devices in their _OPENAMS_DEVICE_IDS order. The
# C2 schemas' family enums are checked against this tuple.
FAMILIES = tuple(_PROFILES)
KLIPPER_MMU_FAMILIES = tuple(name for name, *_ in _MMU_DEVICES)


def normalize_family(value):
    key = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    if key not in _ALIASES:
        raise ValueError(
            "unknown device_kind %r (expected auto, one of the AMS families, "
            "or one of the klipper-mmu devices: %s)"
            % (value, ", ".join(sorted(_PROFILES))))
    return _ALIASES[key]


def profile(family):
    family = normalize_family(family)
    if family == "auto":
        family = "ams1"       # safe, backward-compatible pre-connect shape
    result = dict(_PROFILES[family])
    result.update(oams_ext.FAMILY.profile_extras(family))
    result["schema_version"] = SCHEMA_VERSION
    return result


# The original OpenAMS STM32F072 replacement mainboard (AMS 1 mechanics). Its
# firmware publishes only MCU, MCU_VERSION and CLOCK_FREQ (plus the stock
# Klipper build constants): no BOARD_NAME, no AMS_* capability constants, no
# OAMS_PROTOCOL_VERSION. The MCU string is how the host knows the board.
STM32F072_MCU = "oams-stm32f072rbt6"
STM32F072_BOARD = "oams-stm32f072"


def is_stm32f072(constants):
    """True when the dictionary is the STM32F072 OpenAMS mainboard's."""
    return str(constants.get("MCU", "")).strip().lower() == STM32F072_MCU


def _int_constant(constants, name, default):
    try:
        return int(constants.get(name, default))
    except (TypeError, ValueError):
        return default


def detect_family(configured, constants):
    """Resolve an explicit/automatic family from a firmware dictionary."""
    configured = normalize_family(configured)
    if configured != "auto":
        return configured
    # A firmware that publishes its family is authoritative; the BOARD_NAME
    # heuristics below only cover firmware that does not.
    published = str(constants.get("AMS_FAMILY", "")).strip().lower().replace(
        "-", "_")
    if published in _PROFILES:
        return published
    if is_stm32f072(constants):
        return "ams1"     # AMS 1 mechanics, by the MCU string
    board = str(constants.get("BOARD_NAME", "")).lower().replace("-", "_")
    if "ams2" in board or "ams_2" in board:
        return "ams2"
    if (_int_constant(constants, "AMS_HT_DRYER", 0) != 0
            or _int_constant(constants, "AMS_BAYS", 4) == 1
            or "ams_ht" in board):
        return "ams_ht"
    return "ams1"


def resolve_capabilities(configured, constants):
    """Return (capabilities, warnings), refining family defaults with facts
    explicitly published by firmware. The explicit family controls topology;
    a contradictory firmware dictionary is surfaced instead of silently
    changing the configured bay map."""
    family = detect_family(configured, constants)
    result = profile(family)
    warnings = []
    firmware_bays = _int_constant(constants, "AMS_BAYS", result["bays"])
    if firmware_bays != result["bays"]:
        warnings.append(
            "configured %s has %d bay(s), firmware publishes AMS_BAYS=%d"
            % (family, result["bays"], firmware_bays))
    else:
        result["bays"] = firmware_bays
    if "AMS_HT_DRYER" in constants:
        result["dryer"] = bool(_int_constant(constants, "AMS_HT_DRYER", 0))
    if "AMS_HT_CAP_PTFE_CALIBRATION" in constants:
        result["ptfe_calibration"] = bool(_int_constant(
            constants, "AMS_HT_CAP_PTFE_CALIBRATION", 0))
    if "AMS_HT_MIN_TARGET_CENTIC" in constants:
        result["dryer_target_min_c"] = _int_constant(
            constants, "AMS_HT_MIN_TARGET_CENTIC", 3000) / 100.0
    if "AMS_HT_MAX_TARGET_CENTIC" in constants:
        result["dryer_target_max_c"] = _int_constant(
            constants, "AMS_HT_MAX_TARGET_CENTIC", 8000) / 100.0
    if "AMS_HT_CAP_HUB_HES_CALIBRATION" in constants:
        result["hub_hes_calibration"] = bool(_int_constant(
            constants, "AMS_HT_CAP_HUB_HES_CALIBRATION", 0))
    result["board_name"] = constants.get("BOARD_NAME")
    if is_stm32f072(constants) and family == "ams1":
        # No BOARD_NAME on this firmware: name the board from the MCU string
        # (C2 identity.board). Its capabilities are the AMS 1 profile with the
        # hub HES calibration command, and no dryer or heater, stated here so
        # a later profile change cannot grant them.
        if not result["board_name"]:
            result["board_name"] = STM32F072_BOARD
        result["hub_hes_calibration"] = True
        result.update({"dryer": False, "heater_control": False})
    result["firmware_version"] = constants.get("MCU_VERSION")
    result["build_version"] = constants.get("BUILD_VERSION")
    result["firmware_capability_schema"] = _int_constant(
        constants, "AMS_CAPABILITY_SCHEMA_VERSION", 0)
    # Candidate families; the driver refines this after dictionary lookup.
    result["telemetry"] = (
        family in ("ams_ht", "ams2")
        or result["firmware_capability_schema"] >= 4)
    if "AMS_CAP_RFID_TRANSPORT" in constants:
        result["rfid"] = bool(_int_constant(
            constants, "AMS_CAP_RFID_TRANSPORT", 0))
        result["rfid_uid_inventory"] = result["rfid"]
    # Published implementation capabilities override historical family defaults.
    # Absence retains compatibility with older firmware; explicit zero disables.
    for field, constant in (
            ("normal_motion", "AMS_CAP_F1S_NORMAL_MOTION"),
            ("heater_control", "AMS_CAP_HEATER_CONTROL"),
            ("spool_rotation_during_drying", "AMS_CAP_DRYER_SPOOL_ROTATION"),
            # A unit that refuses to dry while filament is out to the
            # toolhead (an AMS 2 Pro); absent means not known to.
            ("dryer_requires_unloaded", "AMS_CAP_DRYER_REQUIRES_UNLOADED"),
            ("dryer_independent_zones", "AMS_DRYER_INDEPENDENT_ZONES"),
            ("host_sensor_configuration",
             "AMS_CAP_HOST_SENSOR_CONFIGURATION"),
            ("runtime_pid_tuning", "AMS_CAP_RUNTIME_PID_TUNING"),
            ("logger_control", "AMS_CAP_LOGGER_CONTROL"),
            ("error_led_control", "AMS_CAP_ERROR_LED_CONTROL"),
            ("host_arrival_confirm", "AMS_CAP_HOST_ARRIVAL_CONFIRM"),
            ("board_telemetry", "AMS_CAP_BOARD_TELEMETRY"),
            ("link_status", "AMS_CAP_LINK_STATUS"),
            # A unit that decodes its spools' tags itself (C1 section 4.7.1);
            # absent means 0, and the key is then not published at all.
            ("tray_info", "AMS_CAP_TRAY_INFO"),
            ("tray_read", "AMS_CAP_TRAY_READ"),
            # A unit that stores a per-bay filament setting the host writes
            # (C1 section 4.7.2); absent means 0, and the key is not published.
            ("tray_write", "AMS_CAP_TRAY_WRITE"),
            ("fps_toolhead", "AMS_CAP_FPS_TOOLHEAD"),
            ("reverse_follower", "AMS_CAP_REVERSE_FOLLOWER")):
        if constant in constants:
            result[field] = bool(_int_constant(constants, constant, 0))
    for field, constant in (
            ("dryer_zone_count", "AMS_DRYER_ZONES"),
            ("rfid_reader_count", "AMS_RFID_READERS"),
            ("vent_actuator_count", "AMS_VENT_ACTUATORS")):
        if constant in constants:
            result[field] = max(0, _int_constant(constants, constant, 0))
    if "AMS_DRYER" in constants:
        result["dryer"] = bool(_int_constant(constants, "AMS_DRYER", 0))
    oams_ext.FAMILY.refine(result, constants)
    if result.get("normal_motion") is False:
        result["autonomous_follower"] = False
        result["ptfe_calibration"] = False
        result["hub_hes_calibration"] = False
    # Schema 4 has a single dryer target and no drying-rotation command.
    if result["firmware_capability_schema"] == 4:
        if "AMS_CAP_DRYER_SPOOL_ROTATION" not in constants:
            result["spool_rotation_during_drying"] = False
        if "AMS_DRYER_INDEPENDENT_ZONES" not in constants:
            result["dryer_independent_zones"] = False
    return result, warnings


def motion_block_reason(capabilities):
    """H15: why this unit cannot move filament, or None when it can. Driven by
    `normal_motion`, so an explicit AMS_CAP_F1S_NORMAL_MOTION=0 is reported
    too."""
    # H9: a connect-time refusal supersedes every other reason — the unit is
    # not operated at all, and its text names the contract break.
    if capabilities.get("connect_refusal"):
        return capabilities["connect_refusal"]
    # H9c: a unit that lost its host communication stopped and needs a
    # FIRMWARE_RESTART; it must not be operated until then.
    if capabilities.get("session_lost"):
        return capabilities["session_lost"]
    # H9d: the unit went silent past its host watchdog, so it is unreachable
    # (C1 §6.2) and must not be operated until its frames return.
    if capabilities.get("unreachable"):
        return capabilities["unreachable"]
    if capabilities.get("normal_motion") is not False:
        return None
    return (oams_ext.FAMILY.motion_block_reason(capabilities)
            or "This firmware does not support normal filament motion")
