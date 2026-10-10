# C1 — Unit ↔ host

**Version: v1.** The wire-level command and telemetry boundary between an
OpenAMS unit and the `openams` host driver (`src/oams.py` and its neighbors):
the commands the host sends, the responses and constants it reads, and the
rules both sides keep. See `README.md` in this directory for the legend
(EXISTS / TO ADD / OPEN / DISCREPANCY) and the versioning rules.

---

## 1. Scope and transport

A **unit** is a Klipper MCU that speaks this protocol. Two kinds exist:

- **A stock AMS behind a Cortex Bridge.** The bridge presents each AMS 1,
  AMS 2 Pro or AMS HT on its bus to Klipper as one MCU of its own (the relay
  gives each a serial port, `/dev/oams/<uid>`). The AMS keeps its Bambu Lab
  firmware; the bridge answers this protocol for it.
- **The OpenAMS STM32F072 mainboard** (OpenAMS 1), which speaks the legacy
  form of this protocol (§1.4).

A klipper-mmu device speaks its own wire, C5, not this one.

This document describes the wire and the host's behavior.

### 1.1 Two unrelated wire protocols share the name "follower" — scope note

**This document covers exactly one of two independent MCU protocols in the
`openams` host tree.**

- **The OAMS unit protocol** — commands `oams_cmd_*`, `ams_*`, `ams_ht_*` and
  `config_oams_*`, with the dictionary constant `OAMS_PROTOCOL_VERSION`.
  `src/oams.py` is its host driver. **This is C1's scope.**
- **The `[follower]` inline-stepper protocol** — commands `follower_cmd_*`,
  fully specified in `FOLLOWER_PROTOCOL.md`, dictionary constant
  `FOLLOWER_PROTOCOL_VERSION`, on a different MCU. Its host driver is
  `src/follower.py`. **Out of scope for C1.**

The two share op-code and follower-direction *values*
(`FOLLOWER_OP_CODE_SUCCESS=0` mirrors `OAMS_OP_CODE_SUCCESS=0`), but are
otherwise separate command namespaces on separate MCUs. Do not confuse
`src/follower.py` with the unit's own follower, commanded by
`oams_cmd_follower` (§4.2).

### 1.2 Transport

Units speak the Klipper protocol. The transport below it (USB serial for a
Cortex Bridge port, CAN for the STM32F072 mainboard) is not part of this
contract. Do not conflate the protocol version (§1.3) with any version of the
link that carries it.

### 1.3 Versioning: two independent numbers, plus how the host negotiates

| Name | Constant | Current value |
|---|---|---|
| Capability schema | `AMS_CAPABILITY_SCHEMA_VERSION` | **5** |
| Unit protocol version | `OAMS_PROTOCOL_VERSION` | **3** |

The STM32F072 mainboard publishes neither (§1.4).

**How the host negotiates (EXISTS).** At connect, `oams.py` reads the whole
dictionary with `mcu.get_constants()`, logs every published `OAMS_*` key, and
resolves the unit's capabilities with
`ams_family.resolve_capabilities(configured_family, consts)` (§7). It then
reads `OAMS_PROTOCOL_VERSION`: absent means legacy mode
(`protocol_version = None`, built-in enum values). The connect guard
(`_connect_guard`) checks only the keys the unit publishes, so a legacy
dictionary passes untouched. It **refuses the unit** (no motion, a named
reason in `capabilities.connect_refusal`; Klipper keeps running) on any of:

- `OAMS_PROTOCOL_VERSION` outside 1..3;
- `AMS_CAPABILITY_SCHEMA_VERSION` below 4 (a schema above 5 is only a
  warning, since the schema is additive);
- an op-code or follower-direction value that differs from the host's
  compiled-in one (`_VALIDATED_ENUM_NAMES`);
- `HOST_WATCHDOG_MS` below 2000.

A refused unit also loses `normal_motion`, `autonomous_follower`,
`ptfe_calibration` and `hub_hes_calibration`.

### 1.4 The STM32F072 replacement mainboard (legacy protocol)

**EXISTS:** `src/ams_family.py` (`is_stm32f072`, `detect_family`,
`resolve_capabilities`) and `src/oams.py` (`_query_unit_identity`,
`_host_environment_status`); tested by `test/test_stm32f072.py`.

The original OpenAMS **STM32F072 replacement mainboard** (AMS 1 mechanics,
run with the OpenAMS mainboard firmware 2.0.x) is supported through the
legacy protocol. Its dictionary publishes `MCU="oams-stm32f072rbt6"`,
`MCU_VERSION="1.0"`, `CLOCK_FREQ`, `STATS_SUMSQ_BASE` and `BUS_PINS_i2c0`, and
nothing else this contract names: no `OAMS_PROTOCOL_VERSION` (so the host is in
legacy mode: in-order completion matching, no generation echo), no
`BOARD_NAME`, no `AMS_*` capability constants, no `HOST_WATCHDOG_MS`. The
connect guard therefore has nothing to check and the unit is not refused. The
board connects over CAN (`canbus_uuid` in its `[mcu NAME]` section).

| Matter | Behavior |
|---|---|
| Family | `ams1`. `detect_family` recognizes the board from `MCU == "oams-stm32f072rbt6"` (after a published `AMS_FAMILY`, before the `BOARD_NAME` heuristics). |
| Board identity | `devices.<unit>.identity.board` is `"oams-stm32f072"` (C2 §4); the dictionary has no `BOARD_NAME` to report. |
| Capabilities | The `ams1` profile, plus `hub_hes_calibration` true: the board answers `oams_cmd_calibrate_hub_hes spool=%c` with a float threshold. No dryer or heater (`dryer`, `heater_control` false). Motion, follower and PTFE calibration are as for any AMS 1. |
| Serial | The firmware has no `ams_get_identity` query. `identity.serial` is the `canbus_uuid` of the unit MCU's `[mcu ...]` section, as written there. Without a `canbus_uuid` it stays `null`. The missing query is logged once at info level. |
| Environment | The firmware has no `ams_get_environment` query. `environment` comes from a host `[hdc1080]` sensor on the unit's I2C bus, with `source: "hdc1080"`. An `[oams NAME]` option `environment_sensor: <object name>` chooses the sensor; absent, the sole HDC1080 sensor is used, and with none or several the environment is `null`. |
| RFID | Host-driven `[mfrc522]` readers (C2 §4.6), not a unit command. |

The fallbacks apply only to a unit that lacks the query; a unit that answers
`ams_get_identity` or `ams_get_environment` is unchanged. Migration from
`klipper_openams`: [`docs/stm32f072.md`](../stm32f072.md).

---

## 2. Identity and constants

| Constant | Host read | Notes |
|---|---|---|
| `BOARD_NAME` | `ams_family.py` (`detect_family`, `resolve_capabilities`) | `"ams1"`, `"ams2"` or `"ams-ht"` (hyphen). The host lowercases it and replaces `-` with `_` before matching. |
| `AMS_BAYS` | `ams_family.py` | AMS 1: 4 bays; AMS 2 Pro: 4; AMS HT: 1. A mismatch with the configured family is a `capability_warnings` entry; it does not remap the configured bays. |
| `AMS_FAMILY` | `detect_family` reads it first | `"ams1"`, `"ams2"` or `"ams_ht"` (no hyphen, unlike `BOARD_NAME`). |
| `MCU_VERSION`, `BUILD_VERSION` | `ams_family.py` (`firmware_version`, `build_version`) | the unit's version strings, published as `identity.firmware`. |

**The unit serial is a query, not a constant** (`src/oams.py`
`_query_unit_identity`): `ams_get_identity` (no arguments) →
`ams_identity uid0=%u uid1=%u uid2=%u`, the unit's stable identity in three
32-bit words (a Cortex Bridge derives one per AMS from that unit's serial
number), `uid0` holding
bytes 0-3 little-endian, `uid1` bytes 4-7, `uid2` bytes 8-11. It is answered
before configuration and never gated on actuation admission. The host
renders it as 24 uppercase hex digits, bytes 0 to 11 in order (C2
`identity.serial`). The STM32F072 board has no such query (§1.4).

---

## 3. Enumerations

Every unit-domain enum travels as a bare integer in response fields. The host
names them with the tables below (`src/oams.py`).

| Enum | Wire values | Wire field(s) it appears in |
|---|---|---|
| Motion state | idle=0, load=1, follow=2, unload=3, fault=4, positioning=5 | `ams_ht_domain_status.motion_state` |
| Motion stage | stopped=0, load_feed_to_hub=1, load_to_buffer=3, follow=4, unload_to_hub_clear=5, position_feed_to_hub=6, unload_stop_settle=11, unload_to_f1s_clear=13; any other value is named `unknown` | `ams_ht_domain_status.motion_stage` |
| Dryer state | off=0, preheat=1, hold=2, timed_dry=3, cooldown=4, fault=5 | `ams_ht_domain_status.dryer_state`, `ams_ht_dryer_status.state` |
| Vent position | unknown=0, closed=1, open=2 | `ams_ht_dryer_status.vent_position` |
| Fault (capability schema 4 and 5) | none=0, invalid_heater_thermistor=1, invalid_motor_thermistor=2, heater_overtemperature=3, motor_overtemperature=4, chamber_overtemperature=5, environment_sensor_invalid=6, fan_stalled=7, ac_sync_lost=8, invalid_request=9, motion_timeout=10, motion_stalled=11, motor_drive_fault=12, supply_undervoltage=13, supply_overvoltage=14, supply_monitor_invalid=15, heater_drive_fault=16, host_communication_lost=17, first_stage_overcurrent=18, follower_buffer_invalid=19, follower_filament_lost=20, follower_starved=21, follower_stalled=22, follower_timeout=23, vent_actuator_fault=24, lane_obstructed=25, dryer_start_refused=26, rfid_tag_error=27. Above 27 under a schema newer than 5, the host says `unknown` rather than guess. | `ams_ht_domain_status.{motion,dryer,system}_fault`, `ams_ht_dryer_status.fault` |
| Request result | accepted=0, busy=1, invalid_bay=2, invalid_parameter=3, faulted=4, unavailable=5 | `ams_ht_dryer_result.result` (§5.1) |

**Op codes and follower directions.** The host validates these against its
compiled constants (`_VALIDATED_ENUM_NAMES`) when the dictionary publishes
them: `OAMS_OP_CODE_SUCCESS=0`, `OAMS_OP_CODE_ERROR_UNSPECIFIED=1`,
`OAMS_OP_CODE_ERROR_KLIPPER_CALL=5`, `OAMS_OP_CODE_CANCEL_LOAD_SPOOL=6`,
`OAMS_OP_CODE_TIMEOUT=7`; `OAMS_FOLLOWER_REVERSE=0`,
`OAMS_FOLLOWER_FORWARD=1`. Also published and used by value:
`OAMS_OP_CODE_ERROR_BUSY=2`, `OAMS_OP_CODE_SPOOL_ALREADY_IN_BAY=3`,
`OAMS_OP_CODE_NO_SPOOL_IN_BAY=4`, `OAMS_OP_CODE_UNCALIBRATED=8`,
`OAMS_OP_CODE_SESSION_LOST=9`.

**Action values.** `OAMS_STATUS_LOADING`, `OAMS_STATUS_UNLOADING`,
`OAMS_STATUS_CALIBRATING` and `OAMS_STATUS_ERROR` are read from the
dictionary when published and otherwise default to 0, 1, 6 and 7. Values 2
to 5 are the follower actions (forward, reverse, coasting, stopped).

---

## 4. Commands, host → unit

All formats below are the exact Klipper lookup strings the host uses
(`src/oams.py`).

### 4.1 Motion — load / unload / cancel

| Host string | Status |
|---|---|
| `oams_cmd_load_spool spool=%c` | EXISTS (v1, no generation) |
| `oams_cmd_load_spool2 spool=%c gen=%c` | EXISTS (v2, generation) |
| `oams_cmd_load_spool3 spool=%c gen=%c clicks=%u` | EXISTS: programs the bay's PTFE length (`clicks`) and requests the load in one command; replies like `load_spool2`. `clicks=0` is refused with op code 8 (`OAMS_OP_CODE_UNCALIBRATED`). |
| `oams_cmd_unload_spool` | EXISTS (v1) |
| `oams_cmd_unload_spool2 gen=%c` | EXISTS (v2) |
| `oams_cmd_load_spool_cancel` | EXISTS. Stops filament motion and returns the terminal reply. |
| `oams_cmd_load_arrived gen=%c` | Only for a unit reporting `AMS_CAP_HOST_ARRIVAL_CONFIRM=1` (§7.2). No direct response. The unit treats it as the toolhead-arrival acknowledgement of the load with that generation (its low 8 bits) and ignores it when no such load is in flight. The load's terminal status stays the outcome. |

`spool` is the bay index (0-based). On accept, the v2 variants store the
host's `gen` byte and defer the reply to the terminal motion event (§6.1); on
reject, they reply at once with the caller's `gen` echoed. The v1 variants
have the same accept/reject split without the generation.

**Admission.** A load is refused when no PTFE length is configured
(`OAMS_OP_CODE_UNCALIBRATED`). Every motion command also requires a live
session (§6.7) and no autonomous positioning in progress; otherwise the reply
is the error or busy code.

**Autonomous insertion positioning.** On a fresh spool insertion the unit may
position the filament on its own: it reports the `positioning` motion state
(§3) and replies busy to load, unload and calibration until it ends. There is
no host command and no terminal `oams_action_status` for it. **EXISTS (H16):**
the host refuses a load during positioning and publishes
`bays[i].positioning` (C2 §4.5).

### 4.2 Follower

| Host string | Status |
|---|---|
| `oams_cmd_follower enable=%c direction=%c` | EXISTS. No generation variant. |

`direction`: 0 = reverse, 1 = forward (`OAMS_FOLLOWER_REVERSE` /
`OAMS_FOLLOWER_FORWARD`, §3). It always replies **synchronously and at once**
with the ungenerationed `oams_action_status`. A disable is accepted even when
other actuating commands would be refused (§6.7).

**No reverse follower (`AMS_CAP_REVERSE_FOLLOWER=0`, §7.2).** A unit that
cannot run a reverse follower says so with this capability, and the host does
not ask.

**Refusals are consumed (`set_oams_follower`).** The reply's `action` is the
follower action and its `code` is success unless the request is refused.
After an enable request the host waits briefly (0.3 s) for that status. A
refusal is not an operation completion: the host records the follower as
known not running, keeps the readable reason (`"the unit refused the reverse
follower (<code text>)"`) and raises it from `OAMS_FOLLOWER`, `OAMSM_FOLLOWER`
and `UNLOAD_FROM_TOOLHEAD`. A disable is always accepted and never waited on;
a unit that sends no reply is treated as accepting.

The follower regulates the buffer on its own. Its faults, latched, are the
follower members of the fault enum (§3) in `ams_ht_domain_status`.

### 4.3 Calibration

| Host string | Response |
|---|---|
| `oams_cmd_calibrate_ptfe_length spool=%c` | EXISTS (v1); the measured length in encoder clicks arrives as the terminal status `value` |
| `oams_cmd_calibrate_ptfe_length2 spool=%c gen=%c` | EXISTS (v2) |
| `oams_cmd_calibrate_hub_hes spool=%c` | EXISTS; a unit without the capability (`AMS_HT_CAP_HUB_HES_CALIBRATION=0`) replies at once with an error code |
| `oams_cmd_calibrate_hub_hes2 spool=%c gen=%c` | the same, generation-echoing |

**PTFE length is one active value on the wire, not one per bay.** The
`spool` argument of a PTFE calibration only selects which bay is measured.
The host persists per-bay lengths (`ptfe_length_<bay>`) and sends
`config_oams_ptfe length=%u` with the right bay's value immediately before
every load, or uses `oams_cmd_load_spool3`, which carries the length in the
load request itself.

### 4.4 Dryer

| Host string | Status |
|---|---|
| `ams_ht_dryer_start target_centic=%hu duration_s=%u hold_after=%c` | EXISTS, on a unit with a dryer |
| `ams_ht_dryer_stop` | EXISTS, on a unit with a dryer |

Both reply with `ams_ht_dryer_result result=%c` (§5.1). `target_centic` is in
hundredths of a degree. The unit clamps the target to the range it publishes
as `AMS_HT_MIN_TARGET_CENTIC` / `AMS_HT_MAX_TARGET_CENTIC` (the names carry an
`AMS_HT_` prefix but apply to every unit with a dryer). The valid range of
`duration_s` is not published; a violation comes back as
`invalid_parameter`.

**A dryer cycle may outlive the host session.** A unit may keep a dryer cycle
running when the host session ends (Klipper restarts, or the watchdog of §6.2
fires): the cycle is bounded by its own duration and the unit's local safety
limits, and ends by itself. The unit reports the running cycle to the next
session in `ams_ht_dryer_status`, exactly as it would a cycle that session
started.

The host **adopts** a running cycle and never stops it implicitly
(`_query_dryer_status`, `_dryer_status_received`): no connect, ready,
reconnect or restart path sends `ams_ht_dryer_stop`. It asks for the dryer
status once right after ready, logs that it adopted a running cycle, and
offers `dryer_stop`. `dryer_start` is not offered while a cycle is in
progress, and an `OAMS_DRYER_START` sent anyway reaches the unit, whose busy
reply is reported as `a dryer cycle is already running (<state>, <n> s left);
OAMS_DRYER_STOP first`.

### 4.5 Vent

The host sends no vent command. A unit with a dryer reports its vent position
in the dryer status (§5.4).

### 4.6 Fault clear

| Host string | Status |
|---|---|
| `ams_ht_clear_fault` | EXISTS; replies with `ams_ht_dryer_result` |

See §6.3. This is **not** a force-clear.

### 4.7 Tags

The host sends no raw tag command. It learns what each bay holds in one of
two ways: a unit that reads and decodes its spools' tags itself reports a
summary per bay (§4.7.1), and the STM32F072 mainboard's host-driven
`[mfrc522]` readers hand their reads to the host (§1.4, C2 §4.6).

### 4.7.1 Unit-decoded tags (`AMS_CAP_TRAY_INFO`, `AMS_CAP_TRAY_READ`)

A unit that reads and decodes its spools' tags itself reports a **decoded
summary per bay**. Both capabilities are additive (§7.2): absent or `0`
leaves every message below unregistered, unqueried and unsent.

| Capability | Meaning |
|---|---|
| `AMS_CAP_TRAY_INFO` = 1 | the unit reports `ams_tray_info`. The host registers the response and queries every bay once after connect. |
| `AMS_CAP_TRAY_READ` = 1 | the unit accepts `ams_tray_read`. **A read may move filament**: the unit turns the spool past its reader. |

| Host string | Reply | Meaning |
|---|---|---|
| `ams_tray_read bay=%c` | `ams_tray_read_result bay=%c result=%c` | Ask the unit to read the bay's spool. The reply is immediate and only says whether the read was taken; the outcome arrives later as `ams_tray_info`. `result`: **0** accepted, **1** busy (the unit is moving, positioning or already reading), **2** invalid bay, **3** unavailable (for example no spool in the bay, or no reader), **4** faulted. |
| `ams_get_tray_info bay=%c` | `ams_tray_info` for that bay | Query. A plain command, like `ams_get_link_status`: the reply is the same message the unit pushes on a change, so it reaches the one persistent handler. |

`ams_tray_info bay=%c state=%c code=%.*s material=%.*s color_rgba=%u
nozzle_min_c=%hu nozzle_max_c=%hu` is sent in reply to `ams_get_tray_info`
and **unsolicited on any change** of a bay's report (including each state
change of a read).

| `state` | Meaning |
|---|---|
| 0 | no spool |
| 1 | spool present, not read |
| 2 | reading |
| 3 | read, no tag |
| 4 | tag decoded |
| 5 | read failed |

`code` is the manufacturer's filament code (for example a Bambu code such as
`GFB60`) and `material` the material name (`ABS`); both are empty text unless
`state` is 4. `color_rgba` is `0xRRGGBBAA`; `nozzle_min_c` and `nozzle_max_c`
are the nozzle range in degrees C, `0` when unreported. **These summaries carry
no tag UID and no tray UID.**

**What the host does** (`src/oams.py` `_detect_tray_info`,
`_tray_info_received`; `src/oams_tags.py` `note_tray_info`): publishes the
bay's decoded state as its `tag` with `format: "unit_summary"` (C2 §4.6),
with no UID; links it to Spoolman by the policy of C4 §4.1. With
`AMS_CAP_TRAY_READ` the insertion and startup triggers (C2 §4.6) send
`ams_tray_read bay=%c`, after any positioning ends, with busy retried up to
five times one second apart, and one read at a time: a read holds the slot
until its outcome (state 0, 3, 4 or 5) arrives or 120 s pass. At startup only
a bay the unit reports as present and not read (state 1) is read, since a
read may turn the spool. `OAMS_RFID_SCAN OAMS=<unit> BAY=<i>` (C3) sends the
same command. The handler is registered while the MCU config is built (like
`ams_link_status`), since a unit may push summaries right after
`finalize_config`.

### 4.7.2 Stored tray setting (`AMS_CAP_TRAY_WRITE`)

Some units store, per bay, a **filament setting**: the summary they report in
`ams_tray_info` (§4.7.1) for a spool whose tag they do not read, such as a
cardboard spool. With this capability the host can write that setting. It is
additive (§7.2): absent or `0` leaves the command and response unregistered
and unsent. Meaningful with `AMS_CAP_TRAY_INFO`, through which the host sees
the result.

| Capability | Meaning |
|---|---|
| `AMS_CAP_TRAY_WRITE` = 1 | the unit accepts `ams_set_tray_info`. A write moves nothing; it changes only what the unit stores and reports for the bay. |

| Host string | Reply | Meaning |
|---|---|---|
| `ams_set_tray_info bay=%c code=%*s material=%*s color_rgba=%u nozzle_min_c=%hu nozzle_max_c=%hu` | `ams_tray_write_result bay=%c result=%c`, twice | Store the bay's filament setting. `code` is the manufacturer's filament code, **at most 6** printable ASCII characters, possibly empty; `material` at most **8** printable ASCII characters. `color_rgba` is `0xRRGGBBAA`, `AA` = `0xFF` for an opaque colour, `0` for none. `nozzle_min_c` and `nozzle_max_c` are the nozzle range in °C. |

`ams_tray_write_result` comes first as the **immediate admission**, then, for
an accepted write only, as the **completion**:

| `result` | Phase | Meaning |
|---|---|---|
| 0 | admission | accepted; the completion follows |
| 1 | admission | busy (the unit is moving, reading or writing) |
| 2 | admission | invalid bay |
| 3 | admission | unavailable |
| 4 | admission | faulted |
| 7 | admission | invalid field: the code or the material is too long or not printable ASCII, and nothing was stored. **Permanent** for that request: the host does not retry it and reports it to the user |
| 5 | completion | written |
| 6 | completion | failed |

The completion is sent only to the session that asked. After a successful
write the unit pushes the usual `ams_tray_info` for the bay (§4.7.1), now
carrying the stored setting whenever it reads no tag there.

**What the host does** (`src/oams.py` `_detect_tray_write`,
`tray_write_start`, `tray_write_now`, `_tray_write_result`): registers one
persistent handler for `ams_tray_write_result` when `klippy:connect` resolves
the command (the reply comes twice, so a query helper, which takes one reply
and removes its handler, cannot be used), and sends `ams_set_tray_info` as a
plain command. A dictionary that lacks either loses the capability
(`capabilities.tray_write` false). One write per bay is in flight. The host
waits **2 s** for the admission and, after `accepted`, **30 s** for the
completion; a missing reply ends the write as `timeout`. Each admission,
completion or timeout is published as `telemetry.tray_write` (`bay`,
`result_name`, `timestamp`; C2 §4.4.2). The command checks the field limits
above before sending, so `invalid field` is the unit's own, stricter limit.
`OAMSM_SET_TRAY_INFO` (C3 §4) sends it on request, and `openams_spoolman`
keeps the setting in step with the bay's linked Spoolman spool (C4 §7.5).

### 4.8 `spool_hint` (D6, reserved)

**FUTURE (not in v1, D6).** No `spool_hint` identifier exists on the wire or
in `oams.py`. Per D6, this is reserved (mass g, outer radius mm, valid flag);
Spoolman does not store spool geometry, so `valid` stays `false` in v1. See
C4 §7.3.

### 4.9 LEDs

| Host string | Status |
|---|---|
| `oams_set_led_error idx=%c value=%c` | EXISTS on the STM32F072 mainboard; looked up only when `AMS_CAP_ERROR_LED_CONTROL` is absent or 1 |

### 4.10 `config_*` (config-time)

The host sends these during configuration (`_build_config`):

| Host string | Meaning |
|---|---|
| `config_oams_buffer upper=%u lower=%u is_reversed=%u` | FPS thresholds (float bits) |
| `config_oams_f1s_hes on1=%u on2=%u on3=%u on4=%u is_above=%u` | bay-sensor thresholds |
| `config_oams_hub_hes on1=%u on2=%u on3=%u on4=%u is_above=%u` | hub-sensor thresholds |
| `config_oams_pid kp=%u ki=%u kd=%u target=%u` | follower gains and target |
| `config_oams_ptfe length=%u` | the active PTFE length (§4.3); also re-sent before every load |
| `config_oams_current_pid kp=%u ki=%u kd=%u target=%u` | rewind-current gains |
| `config_oams_logger idx=%u` | the unit's index |

A unit reporting `AMS_CAP_HOST_SENSOR_CONFIGURATION=0` accepts the threshold
commands and ignores them, so its thresholds are its own. At run time,
`oams_cmd_pid kp=%u ki=%u kd=%u target=%u` and
`oams_cmd_current_pid kp=%u ki=%u kd=%u target=%u` (`OAMS_PID_SET`,
`OAMS_CURRENT_PID_SET`) are looked up only when `AMS_CAP_RUNTIME_PID_TUNING`
is absent or 1.

### 4.11 Telemetry query commands

| Host string | Response |
|---|---|
| `oams_cmd_query_spool` | `oams_query_response_spool spool=%u`: the loaded bay, or 255 for none |
| `ams_ht_get_board_sensor_status` | `ams_ht_board_sensor_status f1s_counts=%hu filament_present=%c hub_counts=%hu hub_filament_present=%c door_counts=%hu door_closed=%c encoder_clicks=%i buffer_permille=%i f1s_overcurrent_level=%c f1s_overcurrent_active=%c f1s_overcurrent_trips=%u buffer_valid=%c`; a one-bay unit's bay and hub presence |
| `ams_ht_get_domain_status` | `ams_ht_domain_status` (§5.3) |
| `ams_ht_get_dryer_status` | `ams_ht_dryer_status` (§5.4), on a unit with a dryer |
| `ams_get_environment` | `ams_environment` (§5.4) |
| `ams_get_identity` | `ams_identity` (§2) |
| `ams_get_link_status` | `ams_link_status` (§7.2) |
| `ams_get_tray_info bay=%c` | `ams_tray_info` (§4.7.1) |

The host polls the status channels it registered (board, domain, dryer) in a
round-robin, each about once per 1.3 s, and the environment on its own slow
poll (every 5 s). A channel is registered only where the dictionary has it
and the capability it depends on is not explicitly 0 (§7.2).

---

## 5. Responses and telemetry, unit → host

### 5.1 Completion

| Response | Fields | Host parser |
|---|---|---|
| `oams_action_status action=%c code=%c value=%u` | see §6.1 | `_action_status_received` → `_apply_action_status` |
| `oams_action_status2 action=%c code=%c value=%u gen=%c` | adds `gen` | `_action_status2_received` |
| `ams_ht_dryer_result result=%c` | a request result (§3) | `_send_ams_ht_control` |

`action` is one of the action values (§3); `code` an op code (§3); `value`
is context-dependent: for a PTFE calibration, the measured length in encoder
clicks. For load, unload and PTFE calibration the host sees only an op code:
a busy unit replies with code 2, every other refusal with the error code.
Dryer start/stop and fault clear carry the request result itself in
`ams_ht_dryer_result`, and the host turns any value but `accepted` into a
G-code error naming it.

### 5.2 Periodic telemetry

| Response | Fields | Cadence | Host parser |
|---|---|---|---|
| `oams_cmd_stats fps_value=%u hub_hes_value_0..3=%c f1s_hes_value_0..3=%c encoder_clicks=%u` | buffer value (float bits), hub and bay presence per bay, encoder | several times per second | `_oams_cmd_stats` |
| `oams_cmd_current_status current_value=%u` | current reading (float bits) | about once per second | `_oams_cmd_current_stats` |

### 5.3 Domain / fault status

`ams_ht_get_domain_status` → `ams_ht_domain_status motion_state=%c
motion_stage=%c motion_fault=%c dryer_state=%c dryer_fault=%c
system_fault=%c`. Host: `_domain_status_received`, naming all six enums per
§3. The three fault fields carry the latched fault per category; the host
treats `system_fault = host_communication_lost` as session loss (§6.1, §6.2).

### 5.4 Dryer status and environment

`ams_ht_get_dryer_status` → `ams_ht_dryer_status state=%c fault=%c
target_centic=%hu heater_centic=%hi chamber_centic=%hi humidity_mgm3=%u
remaining_s=%u heater_permille=%hu fan_permille=%hu fan_rpm=%u
fan_pwm_permille=%hu vent_position=%c purging=%c door_closed=%c`. Host:
`_dryer_status_received`, converting the centi-degree fields to °C (the
`-32768` sentinel becomes `None`), `humidity_mgm3` to `humidity_gm3`
(absolute humidity), and naming `state`, `fault` and `vent_position`.

**Relative humidity.** `ams_get_environment` (no arguments) →
`ams_environment rh_centipercent=%hu temperature_centic=%hi valid=%c`:
relative humidity in 0.01 %RH, temperature in 0.01 °C (signed), and
`valid=1` when the reading is current. With no reading, `valid=0` and the
other two fields are 0. Host: published as C2 `environment`.

### 5.5 Tags

A unit that decodes its own tags reports `ams_tray_info`,
`ams_tray_read_result` and `ams_tray_write_result` (§4.7.1, §4.7.2).

---

## 6. Invariants

### 6.1 Op generation and "exactly one terminal status per generation"

There is **one generation at a time** (not per bay, not a queue). It is set
only when a v2 command (`oams_cmd_load_spool2` / `unload_spool2` /
`calibrate_ptfe_length2`, or `load_spool3`) is accepted, and cleared when the
terminal status is sent: `oams_action_status2` if a generation is pending,
otherwise `oams_action_status`.

**Invariant.** An accepted generation ends with exactly one
`oams_action_status2` carrying that generation, and the unit has stopped all
motion before sending it. Session loss (`config_reset`, `emergency_stop`, the
host watchdog of §6.2) is not an exception: the failure is replied with the
pending generation. For the host watchdog and `config_reset`, that terminal's
code is `OAMS_OP_CODE_SESSION_LOST = 9`, so the host tells a lost session from
a refusal and settles the op with the FIRMWARE_RESTART reason; the host's own
`emergency_stop` keeps `OAMS_OP_CODE_ERROR_UNSPECIFIED`. Independently, the
host (a) settles every in-flight generation as failed on `klippy:shutdown`
and `klippy:disconnect`, without sending cancel or stop commands; (b) treats
`system_fault = host_communication_lost` as session loss requiring
FIRMWARE_RESTART; and (c) never acts on an ungenerationed
`oams_action_status` under protocol 3 or above. A generation is never
resurrected.

`oams_cmd_follower` (§4.2) has **no generation at all**.

### 6.2 Unit-owned liveness (host-communication watchdog, protocol ≥ 3)

- The unit arms a host-communication watchdog **only on `finalize_config`**;
  it is disarmed by `config_reset`, `emergency_stop`, MCU re-initialization,
  and by firing.
- The timeout is published as `HOST_WATCHDOG_MS`. Any non-empty inbound frame
  resets it. On fire, the session is no longer configured, a later
  `get_config` reports `is_shutdown=1` (§6.7), the pending generation's
  failure is replied with that generation, and `host_communication_lost` is
  latched (§6.1).
- From protocol 3 the unit runs its own no-progress watchdog per operation,
  stops motion on a stall, and completes the operation with
  `OAMS_OP_CODE_TIMEOUT`; the host then drops its own per-operation deadline.

**The host side (EXISTS, H9).** The host reads `HOST_WATCHDOG_MS` at connect
and:

- **keepalive:** sends a frame to every unit at least every
  `HOST_WATCHDOG_MS/2`, and refuses a unit whose `HOST_WATCHDOG_MS` is under
  2000 ms;
- **self-stall detection:** if consecutive monitor ticks are more than
  `HOST_WATCHDOG_MS/2` apart, it assumes the watchdog may have tripped;
- **unit silence:** no frame for `HOST_WATCHDOG_MS + 1000` ms means the unit
  is unreachable: its in-flight ops are failed and motion is withheld.

Recovery after a watchdog trip requires `finalize_config`, i.e.
FIRMWARE_RESTART; the host says so in `OAMSM_SELFTEST` and in status.

**A dryer cycle does not depend on the session.** The watchdog (and
`config_reset`, `emergency_stop`) ends the *session*, not necessarily a dryer
cycle: see §4.4.

### 6.3 Fault latching and clearing

Faults latch in three independent categories: dryer, motion and system.
`ams_ht_clear_fault` is not a force-clear: the unit replies `faulted` while
the latched condition is still present. A `host_communication_lost` fault
clears on reconnection alone; any dryer or motion fault underneath stays
latched. The clear is answered when it is accepted, not when the unit's
recovery ends, so the host reads the latched faults again 15 s later and
reports whether they cleared (`OAMS_CLEAR_FAULT`, C3).

### 6.4 The host never assumes motion has stopped without a terminal code or a fault

The host advances lane state only on a terminal `oams_action_status(2)` or a
latched fault.

### 6.5 Dryer limits come from the dictionary

The target range a unit accepts is the one it publishes
(`AMS_HT_MIN_TARGET_CENTIC`, `AMS_HT_MAX_TARGET_CENTIC`, §4.4); the host's
pre-connect profile values are only a default until connect.

### 6.6 Tag writes (D5)

The host never writes a tag. It writes only a unit's stored per-bay setting
(§4.7.2), and never a Bambu tag (D5).

### 6.7 Admission gate: every actuating command gated on session state

Every actuating command (load, unload, fault clear, dryer start/stop,
follower enable, calibration) is refused unless all of the following hold:

- the session is configured — set by `finalize_config`, cleared by
  `config_reset`, `emergency_stop` and the watchdog;
- the host watchdog is armed — armed by `finalize_config`, cleared by the
  same events (§6.2);
- the unit is not shut down — shut down when the watchdog fires or
  `emergency_stop` is received.

**Recovery after a watchdog trip.** After the watchdog fires, the unit
answers `get_config` with `is_shutdown=1`, and Klipper refuses to configure a
shut-down MCU on a host-only restart. **The only recovery is
FIRMWARE_RESTART** (the MCU `reset`, a fresh start, then `finalize_config`).

---

## 7. Capabilities

A unit's capabilities are its family profile (`src/ams_family.py`), refined by
the constants it publishes. **Absent keeps the profile default; an explicit 0
disables.** The host never infers a capability from the family alone when the
unit publishes it.

| Constant | Sets | Meaning of 0 |
|---|---|---|
| `AMS_CAP_F1S_NORMAL_MOTION` | `normal_motion` | the unit may not move filament: no load, unload, follower or PTFE/hub calibration, and the reason `"This firmware does not support normal filament motion"` |
| `AMS_HT_CAP_PTFE_CALIBRATION` | `ptfe_calibration` | no PTFE calibration |
| `AMS_HT_CAP_HUB_HES_CALIBRATION` | `hub_hes_calibration` | no hub-sensor calibration |
| `AMS_DRYER`, `AMS_HT_DRYER` | `dryer` | no dryer: no dryer commands or telemetry |
| `AMS_CAP_HEATER_CONTROL` | `heater_control` | the dryer cannot be started |
| `AMS_CAP_DRYER_SPOOL_ROTATION` | `spool_rotation_during_drying` | `OAMS_DRYER_START ROTATE=1` is refused |
| `AMS_CAP_DRYER_REQUIRES_UNLOADED` | `dryer_requires_unloaded` | (1: see §7.2) |
| `AMS_DRYER_ZONES`, `AMS_DRYER_INDEPENDENT_ZONES` | `dryer_zone_count`, `dryer_independent_zones` | published for UIs |
| `AMS_HT_MIN_TARGET_CENTIC`, `AMS_HT_MAX_TARGET_CENTIC` | `dryer_target_min_c`, `dryer_target_max_c` | the published dryer range (§4.4) |
| `AMS_CAP_RFID_TRANSPORT` | `rfid` | the published `rfid` capability is false; tags then come only from a unit that decodes its own (§4.7.1) |
| `AMS_RFID_READERS`, `AMS_VENT_ACTUATORS` | `rfid_reader_count`, `vent_actuator_count` | published for UIs |
| `AMS_CAP_HOST_SENSOR_CONFIGURATION` | `host_sensor_configuration` | the `config_oams_*` thresholds are ignored (§4.10) |
| `AMS_CAP_RUNTIME_PID_TUNING` | `runtime_pid_tuning` | no runtime PID commands (§4.10) |
| `AMS_CAP_LOGGER_CONTROL` | `logger_control` | published only |
| `AMS_CAP_ERROR_LED_CONTROL` | `error_led_control` | no `oams_set_led_error` (§4.9) |
| `AMS_CAP_HOST_ARRIVAL_CONFIRM`, `AMS_CAP_LINK_STATUS`, `AMS_CAP_TRAY_INFO`, `AMS_CAP_TRAY_READ`, `AMS_CAP_TRAY_WRITE`, `AMS_CAP_FPS_TOOLHEAD`, `AMS_CAP_REVERSE_FOLLOWER`, `AMS_CAP_BOARD_TELEMETRY` | see §7.2 | see §7.2 |

A unit may publish further constants; the host ignores what it does not read.

### 7.0 What a capability-limited unit must emit

A unit may leave capabilities unavailable. The host needs no change for that;
it needs the facts below, from existing telemetry only.

| Fact the UIs show | Source the host reads | Rule for a unit |
|---|---|---|
| bay holds a spool (`r`; else `e`) | `oams_cmd_stats f1s_hes_value_0..3`; a one-bay unit with board telemetry: `ams_ht_board_sensor_status filament_present` | nonzero = a spool is in that bay. Unused bays of a unit with fewer than 4 report 0. |
| bay is loaded to the hub (`l`) | `hub_hes_value_0..3`, or `hub_filament_present` | a unit with no hub sensor reports 0, and its bays are never `l`. |
| bay is being positioned (`p`) | `ams_ht_domain_status motion_state=positioning`, or a bay-presence 0 to nonzero edge (`positioning_bays`) | not published for a unit with `AMS_CAP_F1S_NORMAL_MOTION=0`. |
| unit online | the unit's `oams_cmd_stats` frames keep arriving within `HOST_WATCHDOG_MS` + 1 s; a one-bay unit: its board status | a unit that keeps sending frames is online whatever it can do. |
| first snapshot, so the manager is ready | the first `oams_cmd_stats` (or `ams_ht_board_sensor_status`) | required, or `ready` stays false. |
| serial (`identity.serial`) | `ams_get_identity` (§2); without that query, the unit's CAN UUID, else null | answer the query; the serial is only a label. |
| family (`auto`) | `AMS_FAMILY` if published, else the MCU string, else `BOARD_NAME` containing `ams2`/`ams_2`, else `AMS_HT_DRYER`/`AMS_BAYS=1`/`ams_ht`, else `ams1` (`detect_family`) | publish `AMS_FAMILY` (`ams1`, `ams2`, `ams_ht`) and `AMS_BAYS`. |
| capability absent from the dictionary | the family profile default | publish `0` explicitly for every capability the unit lacks; absence keeps the family default. |

### 7.1 Other dictionary constants the host reads

| Constant | What the host does with it |
|---|---|
| `AMS_CAPABILITY_SCHEMA_VERSION`, `OAMS_PROTOCOL_VERSION`, `HOST_WATCHDOG_MS` | negotiation and the connect guard (§1.3, §6.2) |
| `OAMS_STATUS_*`, `OAMS_OP_CODE_*`, `OAMS_FOLLOWER_*` | §3 |
| `BOARD_NAME`, `AMS_FAMILY`, `AMS_BAYS`, `MCU`, `MCU_VERSION`, `BUILD_VERSION` | identity and family (§2, §1.4) |
| `CLOCK_FREQ` | a Klipper core constant; read with the rest of the dictionary |

### 7.2 Optional capability constants

All of these are additive: **absent keeps today's behaviour**.

| Constant | Meaning |
|---|---|
| `AMS_CAP_HOST_ARRIVAL_CONFIRM` | `1`: the unit has no input of its own that senses toolhead arrival, so a load finishes only when the host confirms arrival with `oams_cmd_load_arrived gen=%c` (§4.1). Absent or `0`: the unit senses arrival itself; the host sends nothing and does not look the command up. |
| `AMS_CAP_LINK_STATUS` | `1`: the MCU relays a device over its own link and reports that link's state (see below). Absent or `0`: nothing is registered, queried or changed; the unit is as reachable as its stats frames say (§6.2). |
| `AMS_CAP_TRAY_INFO` | `1`: the unit decodes its spools' tags itself and reports `ams_tray_info` per bay (§4.7.1). |
| `AMS_CAP_TRAY_READ` | `1`: the unit accepts `ams_tray_read` (§4.7.1); a read may move filament. Meaningful with `AMS_CAP_TRAY_INFO`. |
| `AMS_CAP_TRAY_WRITE` | `1`: the unit stores a per-bay filament setting and accepts `ams_set_tray_info` (§4.7.2); a write moves nothing. Meaningful with `AMS_CAP_TRAY_INFO`. |
| `AMS_CAP_DRYER_REQUIRES_UNLOADED` | `1`: the unit does not dry while filament is out to the toolhead (an AMS 2 Pro). The unit refuses `ams_ht_dryer_start` with busy while a bay is loaded; the host refuses `OAMS_DRYER_START` first with a message to unload, and withholds `dryer_start` from the unit's actions. |
| `AMS_CAP_FPS_TOOLHEAD` | `1`: the unit's `oams_cmd_stats` `fps_value` (0..1) is the position of a toolhead-side buffer sensor that the unit relays, not an internal measurement. A lane whose `[fps]` reads `source_oams: <unit>` then counts as a toolhead-side arrival signal for host-confirmed arrival (below). |
| `AMS_CAP_REVERSE_FOLLOWER` | `0`: the unit has no reverse follower, and its own unload retracts the filament after the host's cut. For such a unit the host never sends `oams_cmd_follower` with `enable=1 direction=0`: `UNLOAD_FROM_TOOLHEAD` and every other unload path skip that request and its confirmation wait, run the toolhead cut macro, then send the unit's unload as usual; `OAMS_FOLLOWER` / `OAMSM_FOLLOWER` with reverse are refused at once (`"this unit has no reverse follower; its unload retracts the filament on its own"`). |
| `AMS_CAP_BOARD_TELEMETRY` | `0`: the unit does not answer `ams_ht_get_board_sensor_status`; the host does not poll it. Bay presence must then arrive in `oams_cmd_stats` (§7.0). |

**Completion handlers before connect.** After a host-only restart a unit can
send an operation's terminal status between `finalize_config` and the
`klippy:connect` handlers, and Klipper drops a message with no handler. The
host therefore registers every unsolicited response handler while the MCU
config is built (`_build_config`): `oams_action_status` (at construction),
`oams_action_status2` (where the dictionary has it), `oams_cmd_stats`,
`oams_cmd_current_status` and, for a relaying unit, `ams_link_status`, and
for a unit with `AMS_CAP_TRAY_INFO`, `ams_tray_info`.

**Relaying unit: link status** (`src/oams.py` `_detect_link_status`,
`_link_status_received`, `_query_link_status`; `src/oams_manager.py`
`_check_unit_silence`). A unit that relays a device over its own link stays
alive while that device goes silent, so its stats frames keep arriving and
the host's silence rule (§6.2) cannot see it. Such a unit reports the link
itself:

| Message | Direction | Meaning |
|---|---|---|
| `ams_link_status reachable=%c` | unit to host | `1` the relayed device is reachable, `0` it is not. Sent unsolicited on every change, and as the reply to `ams_get_link_status`. |
| `ams_get_link_status` | host to unit | Query; the reply is `ams_link_status`. Sent once after connect from a reactor callback, as a plain command, so the reply arrives at the same persistent handler as the pushes. |

For a unit reporting `AMS_CAP_LINK_STATUS=1` the host treats `reachable=0` as
the unit being unreachable at once: `connected` (so `openams_ui` `online`)
becomes false; motion, dryer, tag and fault-clear commands are refused with a
readable reason (`"The unit's device is not reachable; check its
connection"`); and the unit raises the `unit_unreachable` `stop` fault only
by the rule that already applies to a silent unit (the lane is loading,
unloading, calibrating, holding a spool of it, or reloading onto it). The MCU
session is kept: no reconnect and no Klipper error. `reachable=1` makes the
unit online again. An unknown state (no answer yet) is treated as reachable.

**Telemetry registration by capability** (`_setup_ams_ht_telemetry`): a
telemetry channel is registered unless the capability it depends on is
explicitly `0`, and a channel that is not registered is never looked up or
polled:

| Channel (query) | Registered unless this is 0 |
|---|---|
| board sensor | `AMS_CAP_BOARD_TELEMETRY` |
| dryer (and the dryer start/stop commands) | `AMS_DRYER` / `AMS_HT_DRYER` |

**Host-confirmed arrival** (`src/oams_manager.py` `_check_host_arrival`,
`src/oams.py` `confirm_load_arrived`). Only for a unit reporting the
capability, while its lane is loading or reloading after a runout:

1. The arrival signal is the lane's own `[fps]` `pin:` reading (adjusted by
   `reversed:`). It must come from the toolhead side, never from the unit's
   own buffer telemetry; a lane with `source_oams:` therefore has none, unless
   the source unit reports `AMS_CAP_FPS_TOOLHEAD=1`: then its relayed
   `fps_value` is used exactly like a pin reading.
2. The host arms when it has seen the reading below `arrival_threshold` during
   this load, then, when the reading is at or above the threshold
   continuously for `arrival_debounce` seconds, sends `oams_cmd_load_arrived`
   once with the load's generation.
3. It is sent once per generation, never while unloading or with no load in
   flight, and never to a unit without the capability. The unit's terminal
   status remains the outcome.
4. A capability-1 unit whose lane has no arrival signal cannot be loaded or
   used as a reload target. It shows at configuration (a startup warning, and
   an `info` fault `arrival_signal_missing`), on a load, which is refused at
   once, and at a runout whose spare is such a unit, where the lane pauses
   with message `no spare spool`. The refusal text is `"This unit needs the
   host to confirm toolhead arrival, but lane <fps> has no arrival signal;
   give [fps <fps>] a 'pin' that reads the toolhead side"`.

Options of `[fps]` (defaults): `arrival_threshold: 0.65` (0 to 1, after
`reversed:`), `arrival_debounce: 0.3` (seconds).
