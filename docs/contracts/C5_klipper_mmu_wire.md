# C5 — Klipper-MMU MCU wire

**Status: v1 (2026-09-29).** Derived from `klipper_mmu/docs/protocol-contract.md`
and `klipper_mmu/host/wire.py`. `test/test_contract_c5_sync.py` parses the
enumeration tables in §3 and fails if any value differs from `wire.py`, so this
document cannot drift from the host's copy of the wire silently.

Klipper-MMU is a separate GPL-3.0 MCU add-on that ships its own firmware and
its own `km_*` protocol. It is **not** an OpenAMS unit: C1's
`oams_*` messages, `OAMS_STATUS_*` action enum and `OAMS_OP_CODE_*` codes do
not appear on this wire at all. What it shares with C1 is the *role* — a
firmware that owns filament motion and answers a host — and one compatibility
token, the OpenAMS op code (§7.3).

| | C1 | C5 |
|---|---|---|
| Firmware | an OpenAMS unit | Klipper-MMU (`KLIPPER_MMU_PROTOCOL_VERSION` = 1, `KLIPPER_MMU_INTEGRATION_VERSION` = 1) |
| Message prefix | `oams_*` | `km_*` |
| Unit addressing | `oams_idx`, one op at a time, 8-bit `gen` | 32-bit `instance`, `client` + 32-bit `session` + 32-bit `request` + `operation` + `lane` |
| Motion owner | firmware (protocol ≥ 3) | firmware, always |
| Host C2 projection | `devices.<unit>` | `devices.<unit>` with a klipper-mmu `family` (see §7) |

## 1. Scope and sources

- `klipper_mmu/host/wire.py` — the host's single copy of the message formats
  and of every enumerated value. Its module docstring is authoritative for
  the export rule; §3 restates it.
- `klipper_mmu/docs/protocol-contract.md` — the portable core's contract:
  motion and timing, identity/admission/events, the wire enumerations, the
  reconnection observations, and the lifecycle and recovery rules.
- `klipper_mmu/mcu/enumerations.c` — the firmware's `DECL_ENUMERATION` export.

Where this document states a number, `wire.py` is the source and §3's tables
are checked against it by test. Where it states a rule, the protocol contract
is the source.

## 2. Transport and the enumeration export

**Transport.** Klipper's own MCU serial/msgpack transport, one `km_*`
message per direction, exactly as C1's `oams_*` messages travel. The
`km_*` dictionary names match no message parameter or `_name` suffix, so
Klipper parses and encodes every field as a plain integer.

**The export rule (EXISTS).** Firmware exports every value a host reads as a
Klipper `DECL_ENUMERATION` in `mcu/enumerations.c`. Each entry name is its C
enumerator *suffix*, so `km_op_state LOADED` is declared as
`KM_OP_STATE_LOADED`: an entry cannot name one constant and carry another's
value. At connect the host requires every name it knows to be declared with
the same value; firmware may declare more, and unknown values stay rejected
where they appear.

- All enumerations declared → the host compares value by value
  (`check_enumerations`) and raises on any missing or different entry.
- **None** declared → an image built before the export. The version check
  already limits those to protocol 1 / integration 1, whose values have only
  been *appended* since the first release, so the host reads them from its own
  table.
- Any other mix → the MMU is unavailable while the printer continues
  (`optional-connect.md`).

**Never renumber a value.** Add a new name in firmware and the host table
together. `klipper_mmu/tests/test_wire_enumerations.py` requires the exported
set to equal the host table; `test/test_contract_c5_sync.py` requires §3 below
to equal the same table. `KLIPPER_MMU_PROTOCOL_VERSION` stays 1 because the
export is additive.

## 3. Enumerations

Every value below is a host-compiled constant in `wire.py` and a
`DECL_ENUMERATION` entry in the firmware, compared at connect (§2). Values are
only ever appended, never renumbered. `test/test_contract_c5_sync.py` parses
these tables and fails on any difference from `wire.py`, including a missing
or extra member.

### 3.1 `km_op_state` (`wire.OpState`)

10 members — the unit's motion state, status `state`.

| Name | Value |
|---|---|
| `IDLE` | 0 |
| `LOADING` | 1 |
| `LOADED` | 2 |
| `UNLOADING` | 3 |
| `FOLLOWING` | 4 |
| `CALIBRATING` | 5 |
| `FAULT` | 6 |
| `PRELOADING` | 7 |
| `PRELOADED` | 8 |
| `EJECTING` | 9 |

### 3.2 `km_stage` (`wire.Stage`)

16 members — the motion stage, status `stage`.

| Name | Value |
|---|---|
| `STOPPED` | 0 |
| `LOAD_FEED_TO_HUB` | 1 |
| `LOAD_FEED_TO_BUFFER` | 2 |
| `FOLLOW` | 3 |
| `UNLOAD_RETRACT_TO_HUB` | 4 |
| `UNLOAD_REWIND_TO_SPOOL` | 5 |
| `UNLOAD_STOP_SETTLE` | 6 |
| `CALIBRATE_PROBE` | 7 |
| `CALIBRATE_RETRACT` | 8 |
| `BRAKING` | 9 |
| `UNLOAD_RETRACT_TO_LOAD` | 10 |
| `EJECT_TO_BAY` | 11 |
| `LOAD_RETRY_RETRACT` | 12 |
| `SELECT_HOME` | 13 |
| `SELECT_MOVE` | 14 |
| `SERVO_DWELL` | 15 |

### 3.3 `km_op_type` (`wire.OpType`)

9 members — the requested operation, `operation`.

| Name | Value |
|---|---|
| `NONE` | 0 |
| `LOAD` | 1 |
| `UNLOAD` | 2 |
| `FOLLOW` | 3 |
| `CALIBRATE` | 4 |
| `CANCEL` | 5 |
| `PRELOAD` | 6 |
| `EJECT` | 7 |
| `HOME` | 8 |

### 3.4 `km_client` (`wire.Client`)

4 members — the requesting client, `client`.

| Name | Value |
|---|---|
| `NONE` | 0 |
| `HOST` | 1 |
| `LOCAL_UI` | 2 |
| `INTERNAL` | 3 |

### 3.5 `km_admission` (`wire.AdmissionResult`)

11 members — the admission reply, `admission` (C `KM_REQ_*`).

| Name | Value |
|---|---|
| `ACCEPTED` | 0 |
| `ALREADY_IN_PROGRESS` | 1 |
| `BUSY` | 2 |
| `INVALID_LANE` | 3 |
| `NO_SPOOL` | 4 |
| `PATH_OCCUPIED` | 5 |
| `FAULTED` | 6 |
| `UNAVAILABLE` | 7 |
| `REJECTED_STALE` | 8 |
| `REJECTED_CONFLICT` | 9 |
| `ALREADY_COMPLETED` | 10 |

### 3.6 `km_fault` (`wire.Fault`)

14 members — the fault, `fault` and the status `*_fault` fields.

| Name | Value |
|---|---|
| `NONE` | 0 |
| `HOST_COMM_LOST` | 1 |
| `DRIVER_OVERCURRENT` | 2 |
| `DRIVER_FAULT` | 3 |
| `NO_PROGRESS` | 4 |
| `JAM` | 5 |
| `BUFFER_SIGNAL` | 6 |
| `FILAMENT_LOST` | 7 |
| `STARVED` | 8 |
| `STALLED` | 9 |
| `EMERGENCY_STOP` | 10 |
| `SENSOR_FAILURE` | 11 |
| `CONTROL_TIMING` | 12 |
| `SELECTOR` | 13 |

### 3.7 `km_event_type` (`wire.EventType`)

21 members — the retained event kind, `event_type`.

| Name | Value |
|---|---|
| `NONE` | 0 |
| `LOAD_COMPLETE` | 1 |
| `LOAD_CANCELLED` | 2 |
| `LOAD_FAILED` | 3 |
| `UNLOAD_COMPLETE` | 4 |
| `UNLOAD_CANCELLED` | 5 |
| `UNLOAD_FAILED` | 6 |
| `FOLLOW_FAULT` | 7 |
| `PTFE_CAL_COMPLETE` | 8 |
| `PTFE_CAL_CANCELLED` | 9 |
| `PTFE_CAL_FAILED` | 10 |
| `STOPPED` | 11 |
| `FAULT_ENTERED` | 12 |
| `PRELOAD_COMPLETE` | 13 |
| `PRELOAD_CANCELLED` | 14 |
| `PRELOAD_FAILED` | 15 |
| `EJECT_COMPLETE` | 16 |
| `EJECT_CANCELLED` | 17 |
| `EJECT_FAILED` | 18 |
| `HOME_COMPLETE` | 19 |
| `HOME_FAILED` | 20 |

### 3.8 `km_event_flag` (`wire.EventFlag`)

2 members — event flag bits, `flags`.

| Name | Value |
|---|---|
| `OVERFLOW` | 1 |
| `STEP_ESTIMATE` | 2 |

### 3.9 `km_op_code` (`wire.OpCode`)

8 members — the OpenAMS compatibility token, event `result_code`.

| Name | Value |
|---|---|
| `SUCCESS` | 0 |
| `ERROR_UNSPECIFIED` | 1 |
| `ERROR_BUSY` | 2 |
| `SPOOL_ALREADY_IN_BAY` | 3 |
| `NO_SPOOL_IN_BAY` | 4 |
| `ERROR_KLIPPER_CALL` | 5 |
| `CANCEL` | 6 |
| `TIMEOUT` | 7 |

### 3.10 `km_command` (`wire.Command`)

8 members — the administrative command kind.

| Name | Value |
|---|---|
| `START` | 1 |
| `CANCEL` | 2 |
| `HEARTBEAT` | 3 |
| `STOP` | 4 |
| `EVENT_PEEK` | 5 |
| `EVENT_ACK` | 6 |
| `TERMINAL` | 7 |
| `SNAPSHOT` | 8 |

### 3.11 `km_command_result` (`wire.CommandCode`)

8 members — a command reply `result`.

| Name | Value |
|---|---|
| `OK` | 0 |
| `UNBOUND` | 1 |
| `INSTANCE_MISMATCH` | 2 |
| `INVALID` | 3 |
| `STALE` | 4 |
| `EMPTY` | 5 |
| `SHUTDOWN` | 6 |
| `UNAVAILABLE` | 7 |

### 3.12 `km_binding` (`wire.Binding`)

4 members — the transport binding state.

| Name | Value |
|---|---|
| `UNBOUND` | 0 |
| `ATTACHED` | 1 |
| `SHUTDOWN` | 2 |
| `CLOCK_UNAVAILABLE` | 3 |

### 3.13 `km_follower_state` (`wire.FollowerState`)

4 members — the follower state, `follower_state`.

| Name | Value |
|---|---|
| `DISARMED` | 0 |
| `IDLE` | 1 |
| `DRIVING` | 2 |
| `FAULT` | 3 |

### 3.14 `km_direction` (`wire.Direction`)

2 members — the follower direction, `direction` / `follower_direction`.

| Name | Value |
|---|---|
| `REVERSE` | 0 |
| `FORWARD` | 1 |

### 3.15 `km_startup_state` (`wire.StartupState`)

4 members — the diagnostic startup state.

| Name | Value |
|---|---|
| `IDLE` | 0 |
| `WAITING` | 1 |
| `READY` | 2 |
| `FAULTED` | 3 |

### 3.16 `km_result` (`wire.Result`)

11 members — a negative result, startup `result` and negated reconcile `status`.

| Name | Value |
|---|---|
| `OK` | 0 |
| `ERR_INVALID_PARAM` | -1 |
| `ERR_BUSY` | -2 |
| `ERR_UNAVAILABLE` | -3 |
| `ERR_FAULTED` | -4 |
| `ERR_CONFLICT` | -5 |
| `ERR_STALE` | -6 |
| `ERR_TIMEOUT` | -7 |
| `ERR_NOT_SUPPORTED` | -8 |
| `ERR_QUEUE_FULL` | -9 |
| `ERR_QUEUE_EMPTY` | -10 |

### 3.17 `km_startup_step` (`wire.StartupStep`)

19 members — a startup step, `km_diagnostic_detail` `step`.

| Name | Value |
|---|---|
| `NONE` | 0 |
| `CLOCK` | 1 |
| `IDENTITY` | 2 |
| `CALIBRATION` | 3 |
| `DEADLINE` | 4 |
| `ALREADY_STARTED` | 5 |
| `PROFILE` | 6 |
| `PINS` | 7 |
| `RESOURCES` | 8 |
| `SAVED_CONFIG` | 9 |
| `RESERVE` | 10 |
| `SWITCHES` | 11 |
| `ADC` | 12 |
| `DRIVER_SETUP` | 13 |
| `REWIND_SETUP` | 14 |
| `DRIVER_CONFIRM` | 15 |
| `BIND` | 16 |
| `ATTACH` | 17 |
| `SELECTOR_SETUP` | 18 |

### 3.18 `km_pin_role` (`wire.PinRole`)

22 members — a pin role, `km_diagnostic_detail` `role0` / `role1`.

| Name | Value |
|---|---|
| `STEP` | 1 |
| `DIR` | 2 |
| `ENABLE` | 3 |
| `UART` | 4 |
| `BAY` | 5 |
| `HUB` | 6 |
| `TOOLHEAD` | 7 |
| `BUFFER_A` | 8 |
| `BUFFER_B` | 9 |
| `REWIND_FORWARD` | 10 |
| `REWIND_REVERSE` | 11 |
| `REWIND_ENABLE` | 12 |
| `ENCODER_A` | 13 |
| `ENCODER_B` | 14 |
| `CURRENT` | 15 |
| `LOAD` | 16 |
| `SELECTOR_STEP` | 17 |
| `SELECTOR_DIR` | 18 |
| `SELECTOR_ENABLE` | 19 |
| `SELECTOR_UART` | 20 |
| `SELECTOR_ENDSTOP` | 21 |
| `SERVO` | 22 |

### 3.19 `km_resource_diag` (`wire.ResourceDiag`)

7 members — a resource diagnostic reason, `reason`.

| Name | Value |
|---|---|
| `NONE` | 0 |
| `BAD_ARGUMENT` | 1 |
| `BAD_CLAIM` | 2 |
| `DUPLICATE_RESOURCE` | 3 |
| `TIMER_CONFIGURATION` | 4 |
| `MISSING_TIMER_BASE` | 5 |
| `MISSING_ADC_UNIT` | 6 |

### 3.20 Sentinel values

Two fields use sentinels rather than an enumeration member.

| Constant | Value | Meaning |
|---|---|---|
| `wire.NO_LANE` | 255 | a `km_diagnostic_detail` step is not per-lane |
| `wire.NO_PIN` | 65535 | a `km_diagnostic_detail` pin is unknown |

## 4. Messages

`wire.py` holds every format string as a module constant; this table names
them and says which direction and phase they belong to. Fields are the ones
that carry contract meaning — the header (`instance_hi`, `instance_lo`, and
`query` where present) is omitted.

### 4.1 Host → firmware

| Constant | Message | Purpose |
|---|---|---|
| `TRANSPORT_QUERY` | `km_transport_query challenge` | Challenge/response handshake; `km_transport_state` reports the high-water mark, which cannot establish controller identity by itself. |
| `DIAGNOSTIC_QUERY` | `km_diagnostic_query challenge` | Start the bounded diagnostic sequence. |
| `DIAGNOSTIC_START_HOST` | `km_diagnostic_start_host epoch_hi epoch_lo` | Start the host-side diagnostic with a one-shot MCU boot identity. |
| `DIAGNOSTIC_DETAIL_QUERY` | `km_diagnostic_detail_query challenge` | Fetch the per-step diagnostic detail (startup step, pin roles, reason). |
| `DIFF_PROBE_QUERY` | `km_diff_probe_query challenge` | Sample the DiffA/DiffB buffer channels. |
| `SWITCH_DELAY_QUERY` | `km_switch_delay_query …` | Measure switch bounce and idle edges. |
| `START` | `km_start instance client session request operation lane direction` | **Admit** an operation (see §5). |
| `CANCEL` | `km_cancel instance client session request operation lane` | Cancel a specific owned operation. |
| `HEARTBEAT` | `km_heartbeat instance session` | Refresh the host lease (§6). |
| `STOP` | `km_stop instance` | Release the session. |
| `PATH_RECONCILE` | `km_path_reconcile instance query occupied lane` | Declare the physical path occupied/empty and by which lane. |
| `DRIVER_RESTART` | `km_driver_restart instance query lane` | Clear a per-lane driver fault after it is resolved. |
| `CALIBRATED_ARRIVAL_SET` | `km_calibrated_arrival_set instance steps` | Publish a measured arrival distance. |
| `SELECTOR_GATE_SET` | `km_selector_gate_set instance gate value` | Write a calibrated selector gate position. |
| `SELECTOR_SERVO_TEST` | `km_selector_servo_test instance degrees` | Exercise the grip servo. |
| `SELECTOR_MOTORS_OFF` | `km_selector_motors_off instance` | De-energize the selector. |
| `EVENT_PEEK` | `km_event_peek instance` | Peek the event ring head (see §5.3). |
| `EVENT_ACK` | `km_event_ack instance client session request operation lane seq` | Acknowledge **that exact** head event. |
| `TERMINAL_QUERY` | `km_terminal_query instance client session request operation lane` | Read a request's retained terminal after it left the ring. |
| `LOAD_ATTEMPTS_QUERY` | `km_load_attempts_query instance client session request operation lane` | Read the load retry history. |
| `SNAPSHOT_PART` | `km_snapshot_part instance query part` | Read snapshot part *n* (one retained capture per query). |
| `SENSOR_PART` | `km_sensor_part instance query part` | Read the sensor snapshot parts. |
| `DIAG_PART` | `km_diag_part instance query part` | Read switch-bounce, idle-edge and TMC gstat/drv_status counters. |
| `CONFIG_SELECTOR_GATE` | `config_km_selector_gate oid gate steps` | Config-time: seed the firmware's per-gate selector table. |
| `CONFIG_FEEDER_ROTATION` | `config_km_feeder_rotation oid lane um` | Config-time: seed a lane's rotation distance. |

### 4.2 Firmware → host

| Constant | Message | Purpose |
|---|---|---|
| `TRANSPORT_STATE` | `km_transport_state …` | Binding state, the instance high-water mark and a host-epoch answer. |
| `DIAGNOSTIC_STATE` | `km_diagnostic_state … state result` | Diagnostic progress (startup state) and its result. |
| `DIAGNOSTIC_DETAIL` | `km_diagnostic_detail … step lane cause …` | Name the switch, lane or driver at fault in plain words. |
| `DIFF_PROBE_STATE` | `km_diff_probe_state … state result …` | A DiffA/DiffB calibration sample. |
| `ADMISSION` | `km_admission … admission fault` | The admission reply to `START`/`CANCEL` (§5.2). |
| `COMMAND_RESULT` | `km_command_result … command result` | An administrative command's own result. |
| `PATH_RECONCILE_RESULT` | `km_path_reconcile_result … result status` | The declared occupancy, accepted or refused. |
| `DRIVER_RESTART_RESULT` | `km_driver_restart_result … result status` | Per-lane driver restart outcome. |
| `CALIBRATED_ARRIVAL_SET_RESULT` | `km_calibrated_arrival_set_result … result status` | Arrival-distance write outcome. |
| `SELECTOR_GATE_SET_RESULT` | `km_selector_gate_set_result … result status` | Selector gate write outcome. |
| `SELECTOR_SERVO_TEST_RESULT` | `km_selector_servo_test_result … result status` | Servo test outcome. |
| `SELECTOR_MOTORS_OFF_RESULT` | `km_selector_motors_off_result … result status` | Selector de-energize outcome. |
| `EVENT` | `km_event … seq timestamp_ms client session request operation lane event_type result_code value flags` | One retained event (§5.3). |
| `LOAD_ATTEMPTS` | `km_load_attempts … count stage0 fault0 travel0 …` | The load retry history (up to three attempts). |
| `STATUS_REPLIES` | `km_status_identity`, `km_status_motion`, `km_status_time`, `km_status_sessions`, `km_status_requests`, `km_status_selector`, and the remaining parts | The snapshot parts, in part order; one retained capture per query (§5.4). |
| `SENSOR_REPLIES` | `km_sensor_flags`, `km_sensor_bays0/1/2`, `km_sensor_loads0/1/2` | Per-lane sensor samples. Parts 3-4 and 6 need `KLIPPER_MMU_SENSOR_LOADS=1`; part 5 and the second bay block need `lane_count > 8`. |
| `DIAG_REPLIES` | `km_diag_bay_bounce`, `km_diag_bay_idle`, `km_diag_load_bounce`, `km_diag_load_idle`, `km_diag_tmc0/1` | Switch and TMC counters, gated by `KLIPPER_MMU_DIAG_COUNTERS` and `KLIPPER_MMU_TMC_DIAG`. |
| `TERMINAL_EVENTS` | (not a message) | The retained terminal triple per operation: success, cancel, failure. |

## 5. Identity, admission, retained events, acknowledgement

### 5.1 Identity

An operation's identity is the **complete** tuple

> `(instance, client, session, request, operation, lane)`

- `instance` is the 32-bit controller instance (`instance_hi` + `instance_lo`).
  A reconnect that raises the high-water mark is a *different* instance.
- `client` is `km_client` (§3.4); the host is `HOST` (1). `LOCAL_UI` and
  `INTERNAL` are firmware-side actors.
- `session` and `request` are **nonzero 32-bit** values that increase without
  wrapping. Use a newer session before request exhaustion. Counters at
  `UINT32_MAX` must not wrap.
- `operation` is `km_op_type` and `lane` is the lane number, so a request
  number alone never identifies work.

**Ownership.** Active ownership prevents another client/session taking over.
The host may use the admitted counters to avoid reusing an old identity, but
must first reconcile an outstanding request and path ownership. **Sessions
cannot increase while another session still owns the path.**

`instance` is *not* a boot identity. RAM counters and event sequences do not
identify a firmware boot. The optional H723 diagnostic path attaches with a
one-shot MCU boot identity (`km_diagnostic_start_host`); installed OpenAMS
session and physical-path reconciliation remain pending. Reconnect reconciles
the real physical state; reconnecting never quietly restores motion.

### 5.2 Admission

`km_start` does not begin motion. The firmware answers `km_admission` with an
`km_admission` value and a `fault` value; only an admitting answer starts an
operation.

| `km_admission` | The request… | Admitting? |
|---|---|---|
| `ACCEPTED` (0) | …was admitted and will run. | yes |
| `ALREADY_IN_PROGRESS` (1) | …is identical and already running. Neither restarts motion. | yes |
| `ALREADY_COMPLETED` (10) | …is a completed recent identity; query its terminal with `km_terminal_query`. Neither restarts motion. | yes |
| `BUSY` (2) | …arrived while another operation held the unit. | no |
| `INVALID_LANE` (3) | names a lane the build does not have. | no |
| `NO_SPOOL` (4) | found no filament in the requested lane. | no |
| `PATH_OCCUPIED` (5) | found the feed path already in use. | no |
| `FAULTED` (6) | was refused because a fault is latched. | no |
| `UNAVAILABLE` (7) | was refused because the unit is not operational. | no |
| `REJECTED_STALE` (8) | reused an identity older than the bounded history. | no |
| `REJECTED_CONFLICT` (9) | conflicted with an active owner. | no |

`wire.ADMITTED` is `{ACCEPTED, ALREADY_IN_PROGRESS, ALREADY_COMPLETED}` —
exactly the answers that leave the request a retained terminal rather than a
rejection. A host must not treat a non-admitting answer as a completed
operation.

**Cancellation** uses the preferred `km_core_request_cancel_target`, which
requires the complete owning identity *including operation and lane*; wire
commands use that entry point. Legacy entry points retain their
client/session/request behavior. **Zero and sessionless cancels are
rejected.** Ownership and event reservation remain until velocity is zero and
all physical pulse endings are complete. A cancellation during an unload or
calibration always produces *that* operation's canceled result, including
when success was being settled. Occupancy remains conservative after an
interruption.

### 5.3 Retained events and acknowledgement

An admitted operation reserves event capacity in the **16-entry ring**. A load
reserves both its completion *and* a subsequent autonomous-follow stop or
fault. Queue pressure rejects new work before output. **No push operation
evicts an unacknowledged event.** Sequence zero is excluded.

`km_event` carries the full identity plus `seq`, `timestamp_ms`,
`event_type`, `result_code`, `value` and `flags` (`OVERFLOW`, `STEP_ESTIMATE`).

Delivery is **peek/retransmit, then acknowledge the exact head sequence**:

1. `km_event_peek` — the host may retransmit freely; peeking is not acking.
2. process it;
3. `km_event_ack` with the **exact** `seq`.

`pop` exists only as a synchronous convenience. An unsolicited fault at a full
queue is **separately latched**; new work and recovery are blocked until it
is queued, and repeated observation of a latched fault does not create
repeated terminal events.

**The retained terminal triple.** Every host operation terminates in exactly
one of three events (`wire.TERMINAL_EVENTS`):

| `km_op_type` | Success | Cancelled | Failed |
|---|---|---|---|
| `LOAD` (1) | `LOAD_COMPLETE` | `LOAD_CANCELLED` | `LOAD_FAILED` |
| `UNLOAD` (2) | `UNLOAD_COMPLETE` | `UNLOAD_CANCELLED` | `UNLOAD_FAILED` |
| `CALIBRATE` (4) | `PTFE_CAL_COMPLETE` | `PTFE_CAL_CANCELLED` | `PTFE_CAL_FAILED` |
| `PRELOAD` (6) | `PRELOAD_COMPLETE` | `PRELOAD_CANCELLED` | `PRELOAD_FAILED` |
| `EJECT` (7) | `EJECT_COMPLETE` | `EJECT_CANCELLED` | `EJECT_FAILED` |
| `HOME` (8) | `HOME_COMPLETE` | — (none) | `HOME_FAILED` |

Plus three op types with no triple: `FOLLOW` (3) reports `FOLLOW_FAULT`,
and `STOPPED` (11) and `FAULT_ENTERED` (12) are unsolicited.

**After an acknowledgement has already removed an event from the ring**, query
retained terminal history by full identity with `km_terminal_query`. The
history is bounded at eight terminals; older reused IDs remain stale.
**History eviction is an unknown/stale result, never inferred success.**

### 5.4 The snapshot

`km_core_get_snapshot` returns a read-only copy exposed as the
`km_snapshot_part` / `STATUS_REPLIES` parts. It exposes the full active
operation identity, whether its terminal is still pending, **path ownership
and occupancy separately**, pulse-position uncertainty, per-client admitted
session and request counters, and the queued-event head/count plus any pending
unsolicited fault.

It **neither** acknowledges events **nor** clears faults, refreshes the host
lease, changes request counters or resumes motion. Snapshot time is the last
accepted controller time; the transport must separately timestamp
acquisition. The adapter must serialize snapshot capture with updates and
physical servicing.

The host **must not** read more into it than it says:

- Load completion can coexist with continuing ownership and automatic
  following, so `terminal_pending=false` is **not** a released path, and an
  occupied-but-unowned path after stop/recovery is **not** an empty machine.
- Owner and occupant lane fields are meaningful only when their corresponding
  flags are true. An occupied path can report `occupant_lane=255`
  (`NO_LANE`) when local presence is observed without an established source:
  bay switches alone cannot resolve that source, and admission stays blocked
  until explicit path reconciliation. A later absence or invalid reading does
  **not** clear retained occupancy.
- Active identity fields are **not** a terminal record. After ordinary
  settlement some retained identity numbers remain while `active_client=NONE`
  and `lane_active=false`. The retained terminal API or an actual terminal
  event establishes a request's result.
- Absence of position uncertainty is not physical homing and not an
  independent filament-position measurement. `motor_ready` exposes the core's
  global admission flag, not proof that every configured lane or optional
  backend is initialized.
- Snapshot query numbers increase for the entire controller instance,
  including reconnects; replaying an existing query returns its original
  capture. Replies queued from an earlier connection must not settle a new
  connection's waiter.

## 6. The host lease

A host that owns motion holds a **lease**: the local watchdog requires
heartbeat/ready updates within the configured lease.

- `km_heartbeat instance session` refreshes it. The lease is host-owned; the
  firmware never refreshes it for the host, and a snapshot read does not
  refresh it either (§5.4).
- **Periodic service remains required while idle or faulted**, not only while
  moving.
- If the lease expires, the firmware faults with `HOST_COMM_LOST` (1) and
  disables outputs. Reconnection only refreshes communication.
- **Explicit recovery** reconciles known position and path after the
  underlying fault is resolved, and **keeps outputs off**. It never enables a
  driver, and runtime state never automatically authorizes resuming a print.
- Sensor acquisition has **separate** freshness limits from the lease.
  Configured invalid or stale sensors fault active operations; absent optional
  sensors are not fabricated.
- The optional local-UI client (`km_client.LOCAL_UI`) holds its own
  per-client lease; per-client admitted session and request counters are in
  `km_status_sessions` and `km_status_requests`.

## 7. Mapping to C1 and C2

C5 and C1 are **separate wires**, not two views of one. The host does not
decode a C5 wire value into a C1 enum: the `[klipper_mmu …]` observer
publishes a unit into the C2 topology and, with `openams_motion`, into the C2
`devices` object, and the four operations that had no C1 counterpart at all
are reached through that unit object as the `OAMS_*` commands of C3 §2. The
tables record the correspondence a client needs so a UI written against C2
can read a klipper-mmu unit without a second vocabulary — and record,
explicitly, where there is nothing to map to.

### 7.1 Operations → C1 command and C2 action

C1 has no op *type* enum: an operation is implied by the command that carries
it (`oams_cmd_load_spool3`, `oams_cmd_unload_spool`, `oams_cmd_calibrate_*`,
`oams_cmd_cancel`). C2's unit vocabulary is `supported_actions`.

| `km_op_type` | C1 command / op | C2 `supported_actions` token | Event that terminates it |
|---|---|---|---|
| `LOAD` (1) | `oams_cmd_load_spool3` | `load` (per bay) | `LOAD_COMPLETE` / `_CANCELLED` / `_FAILED` |
| `UNLOAD` (2) | `oams_cmd_unload_spool` | `unload` (per bay) | `UNLOAD_COMPLETE` / `_CANCELLED` / `_FAILED` |
| `FOLLOW` (3) | `oams_cmd_set_follower` | `follower` (unit) | `FOLLOW_FAULT` only — no completion |
| `CALIBRATE` (4) | `oams_cmd_calibrate_ptfe_length` | `calibrate_ptfe` (per bay) | `PTFE_CAL_COMPLETE` / `_CANCELLED` / `_FAILED` |
| `CANCEL` (5) | `oams_cmd_cancel` | (no token; cancels the op above) | — produces the canceled terminal of the canceled op |
| `PRELOAD` (6) | none on C1; `OAMS_PRELOAD SPOOL=<bay>` (C3 §2) | `preload` (per bay) | `PRELOAD_COMPLETE` / `_CANCELLED` / `_FAILED` |
| `EJECT` (7) | none on C1; `OAMS_EJECT SPOOL=<bay>` (C3 §2) | `eject` (per bay) | `EJECT_COMPLETE` / `_CANCELLED` / `_FAILED` |
| `HOME` (8) | none on C1 (C1's calibrate homes nothing); `OAMS_HOME` (C3 §2), selector builds only | `home` (unit) | `HOME_COMPLETE` / `HOME_FAILED` |
| `NONE` (0) | — | — | — |

**PRELOAD's counterpart UNPRELOAD is a lane-local `UNLOAD`:** C5
`KLIPPER_MMU_UNPRELOAD` issues `km_op_type` `UNLOAD` (2) against the
reservation a `PRELOAD` left, which is why the two rows above differ while
the command that reaches C2 is its own, `OAMS_UNPRELOAD SPOOL=<bay>` with
the `unpreload` per-bay token (C3 §2, C2 §5.6). The four C2 tokens are
published per the family's selector kind (§4.4.1 of C2): `preload`,
`unpreload` and `eject` on every klipper-mmu unit, `home` only where the
family has a selector to move. An AMS unit publishes none of them and refuses
all four.

### 7.2 Op states → C1 motion state and C2 lane `op`

C1's motion state is `Idle=0, Load=1, Follow=2, Unload=3, Fault=4,
Positioning=5` (`oams.py:1346-1368`, published as
`ams_ht_domain_status.motion_state`). C2's lane `op` is the coarser
`UNLOADED, LOADING, LOADED, UNLOADING, CALIBRATING` (`oams_state.py:56-60`).
A klipper-mmu state maps to the C2 `op` that a C2 client already knows.

| `km_op_state` | C1 motion state | C2 `lanes_by_fps.<l>.op` |
|---|---|---|
| `IDLE` (0) | `Idle` (0) | `UNLOADED` |
| `LOADING` (1) | `Load` (1) | `LOADING` |
| `LOADED` (2) | **none** — C1 has no loaded-idle state; a klipper-mmu unit goes `LOADED` → `FOLLOWING` on its own | `LOADED` |
| `UNLOADING` (3) | `Unload` (3) | `UNLOADING` |
| `FOLLOWING` (4) | `Follow` (2) | `LOADED` |
| `CALIBRATING` (5) | **none** (C1 has no calibration motion state) | `CALIBRATING` |
| `FAULT` (6) | `Fault` (4) | **none** — C2 reports a fault, not an `op` |
| `PRELOADING` (7) | **none** | **none** |
| `PRELOADED` (8) | **none** | **none** |
| `EJECTING` (9) | **none** | **none** |

### 7.3 Result codes → C1 op codes

`km_op_code` is the one place the two wires share a *value space*: the
`protocol_wire` helper maps legacy OpenAMS action/result values only, and it
rejects internal request IDs above 255 rather than silently truncating them.
The full-width firmware command boundary and `host/requests.py` instead retain
complete identities and explicitly associate them with the existing OpenAMS
compatibility token. **Telemetry has no generation-tagged terminal identity
and cannot complete a wait.**

| `km_op_code` | C1 `OAMS_OP_CODE_*` | Same value? |
|---|---|---|
| `SUCCESS` (0) | `OAMS_OP_CODE_SUCCESS` | yes |
| `ERROR_UNSPECIFIED` (1) | `OAMS_OP_CODE_ERROR_UNSPECIFIED` | yes |
| `ERROR_BUSY` (2) | `OAMS_OP_CODE_ERROR_BUSY` | yes |
| `SPOOL_ALREADY_IN_BAY` (3) | `OAMS_OP_CODE_SPOOL_ALREADY_IN_BAY` | yes |
| `NO_SPOOL_IN_BAY` (4) | `OAMS_OP_CODE_NO_SPOOL_IN_BAY` | yes |
| `ERROR_KLIPPER_CALL` (5) | `OAMS_OP_CODE_ERROR_KLIPPER_CALL` | yes |
| `CANCEL` (6) | `OAMS_OP_CODE_CANCEL_LOAD_SPOOL` | yes, name differs |
| `TIMEOUT` (7) | `OAMS_OP_CODE_TIMEOUT` | yes |
| — | `OAMS_OP_CODE_UNCALIBRATED` (8) | **C5 has no value** |
| — | `OAMS_OP_CODE_SESSION_LOST` (9) | **C5 has no value**; C5's `Fault.HOST_COMM_LOST` is the nearest |

### 7.4 Admission → C1 rejection codes and C2 actions

| `km_admission` | C1 equivalent | C2 action token |
|---|---|---|
| `ACCEPTED` (0) | `ams_request_result.accepted` | — (proceed) |
| `ALREADY_IN_PROGRESS` (1) | `ams_request_result.busy` | none (not a fault) |
| `ALREADY_COMPLETED` (10) | **none** | none (read `km_terminal_query`) |
| `BUSY` (2) | `OAMS_OP_CODE_ERROR_BUSY` | none (retry) |
| `INVALID_LANE` (3) | `ams_request_result.invalid_bay` | none (config error) |
| `NO_SPOOL` (4) | `OAMS_OP_CODE_NO_SPOOL_IN_BAY` | none (load a spool) |
| `PATH_OCCUPIED` (5) | **none** — C1 reports it as a per-lane in-flight op | none (reconcile the path) |
| `FAULTED` (6) | `ams_request_result.faulted` | `clear_fault` |
| `UNAVAILABLE` (7) | `ams_request_result.unavailable` | none |
| `REJECTED_STALE` (8) | **none** | none (reconnect) |
| `REJECTED_CONFLICT` (9) | **none** | none (another owner) |

### 7.5 Motion stages → C1 `ams_motion_stage` and C2 `motion_stage`

C1 names its stage values in its enumeration table (§3) and C2
republishes the name as `lanes_by_fps.<l>.motion_stage`, **diagnostic only,
never a plan step** (C2 §5.3). Both vocabularies are device-specific; the
left column is the one a C2 client actually sees on a klipper-mmu unit.

| `km_stage` | C1 `ams_motion_stage` | C2 `motion_stage` |
|---|---|---|
| `STOPPED` (0) | `stopped` (0) | `stopped` |
| `LOAD_FEED_TO_HUB` (1) | `load_feed_to_hub` (1) | `load_to_hub` |
| `LOAD_FEED_TO_BUFFER` (2) | `load_to_buffer` (3) | `load_to_buffer` |
| `FOLLOW` (3) | `follow` (4) | `follow` |
| `UNLOAD_RETRACT_TO_HUB` (4) | `unload_to_hub_clear` (5) | `unload_to_hub_clear` |
| `UNLOAD_REWIND_TO_SPOOL` (5) | `unload_to_f1s_clear` (13) | `unload_to_f1s_clear` |
| `UNLOAD_STOP_SETTLE` (6) | `unload_stop_settle` (11) | `unload_stop_settle` |
| `CALIBRATE_PROBE` (7) | **none** | **none** |
| `CALIBRATE_RETRACT` (8) | **none** | **none** |
| `BRAKING` (9) | **none** | **none** |
| `UNLOAD_RETRACT_TO_LOAD` (10) | **none** | **none** |
| `EJECT_TO_BAY` (11) | **none** | **none** |
| `LOAD_RETRY_RETRACT` (12) | **none** | **none** |
| `SELECT_HOME` (13) | **none** | **none** |
| `SELECT_MOVE` (14) | **none** | **none** |
| `SERVO_DWELL` (15) | **none** | **none** |

C1 stages with **no C5 counterpart**, among them `position_feed_to_hub` (6),
are the firmware-autonomous insertion positioning phases and the unit's own
unload phases, which a klipper-mmu unit never enters on this wire.

### 7.6 Faults → C1 `ams_fault` and C2 severity + action tokens

C1 lists the `ams_fault` values in its enumeration table (§3). C2 does
**not** publish firmware fault names verbatim: the host assigns each a
severity (`stop`, `pause`, `info`) and a set of recovery action tokens
(C2 §5.7). The severity column below is the C2 severity this C5 fault falls
into under C2 §5.7's tables, with the C1 `ams_fault` name when one exists.

C2 §5.7's default applies: **any fault name not listed is `stop`.** A UI must
withhold motion until someone looks at it, so an unmapped C5 fault is never
the safe choice to make `info`.

| `km_fault` | C1 `ams_fault` | C2 severity | C2 action tokens |
|---|---|---|---|
| `NONE` (0) | `none` (0) | — (not a fault) | — |
| `HOST_COMM_LOST` (1) | `host_communication_lost` (17) | `stop` | **none** — recovery is `FIRMWARE_RESTART` |
| `DRIVER_OVERCURRENT` (2) | `first_stage_overcurrent` (18) | `stop` | `clear_fault` |
| `DRIVER_FAULT` (3) | `motor_drive_fault` (12) | `stop` | `clear_fault` |
| `NO_PROGRESS` (4) | `motion_timeout` (10) | `pause` | `retry_load`, `clear_errors` |
| `JAM` (5) | `motion_stalled` (11) | `pause` | `retry_load`, `clear_errors` |
| `BUFFER_SIGNAL` (6) | `follower_buffer_invalid` (19) | `pause` | `clear_fault` |
| `FILAMENT_LOST` (7) | `follower_filament_lost` (20) | `pause` | `retry_load`, `clear_errors` |
| `STARVED` (8) | `follower_starved` (21) | `pause` | `retry_load`, `clear_errors` |
| `STALLED` (9) | `follower_stalled` (22) | `pause` | `retry_load`, `clear_errors` |
| `EMERGENCY_STOP` (10) | `invalid_request` (9) | `stop` | `clear_fault` |
| `SENSOR_FAILURE` (11) | **none** — see below | `stop` | `clear_fault` |
| `CONTROL_TIMING` (12) | **none** | `stop` (C2 §5.7 default) | `clear_fault` |
| `SELECTOR` (13) | **none** | `stop` (C2 §5.7 default) | `clear_fault` |

`SENSOR_FAILURE` is a single klipper-mmu fault covering the BAY, HUB,
TOOLHEAD, buffer, selector-endstop and current sensors; C1 splits those across
`invalid_heater_thermistor`, `invalid_motor_thermistor`,
`environment_sensor_invalid` and `supply_monitor_invalid`. The **severity is
`stop` either way** (C2 §5.7 makes every sensor fault `stop`), so the mapping
is safe; only the name is coarser.

`CONTROL_TIMING` and `SELECTOR` are klipper-mmu faults with **no C1 name**.
They are the two C5 faults a C2 client would see only through C2 §5.7's
default row.

**This mapping is now implemented (2026-09-29), and the token column moved.**
A klipper-mmu unit's fault reaches `devices.<unit>.faults` and
`openams_ui <unit>.fault`: the manager latches the raw `km_fault` value from
`accept_firmware_fault` and the unit itself names it, through
`firmware_fault_info(value, physical_lane)`
(`klipper_mmu/host/openams_fault_projection.py`; C2 §5.7 carries the published
table). The **severities above stand unchanged**. Three things in the last
column did not, because a UI may only be offered a token that really clears or
resumes the unit:

- **`clear_errors` is dropped from every row.** The klipper-mmu OpenAMS unit's
  `clear_errors` raises (`integration/openams/README.md`), so the token would
  clear nothing. `retry_load` survives where this table lists it: the lane's
  load again, which a klipper-mmu unit really runs.
- **`clear_fault` is withheld from `DRIVER_OVERCURRENT`, `DRIVER_FAULT` and
  `SELECTOR`**, which now offer no action at all. A lane latch survives every
  recovery except reconciliation (`docs/path-reconciliation.md`, "From a
  stopped fault"), and those three need physical work first: the two driver
  faults keep a driver latch that reconciliation refuses, repaired with
  `KLIPPER_MMU_RESTART_DRIVER` once the cause is removed, and a selector fault
  leaves the shared path unproven, so the operator states which lane occupies
  it with `KLIPPER_MMU_RECONCILE_PATH ACTION=ADOPT LANE=n`. Their `text` says
  what to do instead of offering a button ("Check the path, then clear").
- **`clear_fault` where it is still offered routes to this unit's fault
  acknowledgement.** The unit registers `OAMS_CLEAR_FAULT OAMS=<idx>` for its
  own index — the command C2 §5.7 already names, so the UIs are unchanged —
  and the handler runs the same acknowledgement `KLIPPER_MMU_ACK_FAULT` does.
  It settles the retained event and the latch's waiter; the firmware fault and
  the path stay, and only `KLIPPER_MMU_RECONCILE_PATH` clears them.

`HOST_COMM_LOST` is the one row this section's "recovery is
`FIRMWARE_RESTART`" note already covered: no token, and its `text` says so.

### 7.7 Everything with no counterpart, and what has since found one

The list as first written. Each item is a value a klipper-mmu firmware can
send that the openams host's C1/C2 vocabulary had nowhere to put.

**Mapped since (2026-09-29).** `PRELOAD` (6), `EJECT` (7), `HOME` (8) and the
lane-local `UNPRELOAD` left this list: C3 §2 publishes `OAMS_PRELOAD`,
`OAMS_UNPRELOAD`, `OAMS_EJECT` and `OAMS_HOME` under the `OAMS=<idx>` key
every per-unit command uses, and C2 §5.6 publishes the `preload`, `unpreload`,
`eject` (per bay) and `home` (unit) action tokens, with the C2 §6.2 `p`, `q`,
`e` letters. See §7.1.

| Item | C5 | Nearest in C1/C2 | Why |
|---|---|---|---|
| Op type | `PRELOAD` (6) | `OAMS_PRELOAD` (C3 §2) | **MAPPED 2026-09-29**: C1 still has no preload; the OpenAMS host reaches it through the klipper-mmu unit object. |
| Op type | `EJECT` (7) | `OAMS_EJECT` (C3 §2) | **MAPPED 2026-09-29**, as above. |
| Op type | `HOME` (8) | `OAMS_HOME` (C3 §2), selector families only | **MAPPED 2026-09-29**, as above. |
| Op state | `PRELOADING` (7), `PRELOADED` (8), `EJECTING` (9) | none | C2's lane `op` has five members; C1's motion state has six. A staged lane is visible as the unit's own bay state, not as a lane `op`. |
| Op state | `FAULT` (6) as an `op` | C2 publishes a fault, never an `op` | A C2 client reads `faults[]`, not `op=FAULT`. |
| Event type | `STOPPED` (11), `FAULT_ENTERED` (12) | none | Unsolicited; C1 has no unsolicited status. |
| Event type | `PRELOAD_*` (13-15), `EJECT_*` (16-18), `HOME_*` (19-20) | none | Terminate ops whose terminal no C1 command waits for; the host settles them into the unit's own state. |
| Event type | `FOLLOW_FAULT` (7) | none as an event; C2 has `follower_*` fault codes | C2 models follower problems as `faults[]` entries, not as an event. |
| Event flag | `OVERFLOW` (1), `STEP_ESTIMATE` (2) | none | C2 has no event stream; the host's observer does not publish the ring. |
| Stage | `CALIBRATE_PROBE` (7) … `SERVO_DWELL` (15), `BRAKING` (9) | none | Nine stages with no C1 `ams_motion_stage`. |
| Admission | `ALREADY_COMPLETED` (10), `PATH_OCCUPIED` (5), `REJECTED_STALE` (8), `REJECTED_CONFLICT` (9) | none | C1's `ams_request_result` has six values, none of which is these. |
| Fault | `CONTROL_TIMING` (12), `SELECTOR` (13) | none | Falls into C2 §5.7's `stop` default. |
| Result | `ERR_NOT_SUPPORTED` (-8), `ERR_QUEUE_FULL` (-9), `ERR_QUEUE_EMPTY` (-10), `ERR_CONFLICT` (-5), `ERR_STALE` (-6) | none | C1 has no negative result space. |
| Enum | `km_client` (4), `km_binding` (4), `km_startup_state` (4), `km_startup_step` (19), `km_pin_role` (22), `km_resource_diag` (7), `km_command` (8), `km_command_result` (8) | none | Transport and diagnostics vocabulary C1 does not have. |
| C2 field | — | `capabilities.dryer`, `capabilities.rfid`, `telemetry.*` | A klipper-mmu unit publishes none of these; its registry profile sets `dryer`/`rfid` false (C2 §4.4.1). |
| C2 capability | — | `bays` ≤ 4 assumption | A klipper-mmu unit has 1-12 lanes; the registry's `bays`/`bays_min`/`bays_max` carry the installed count. |

## 8. Motion, timing and lifecycle (EXISTS)

**Motion.** Commands specify a velocity or absolute point target with local
limits. The MCU computes its own acceleration, cruise, braking and next-edge
deadlines; it **never requires host-generated movement chunks**. At most four
analytic phases are traversed in an update. Integer absolute positions are
bounded to ±10^12 steps, velocity to 200,000 steps/s, acceleration and
braking to 2,000,000 steps/s². Outputs stay disabled through initialization
and every error path. A disrupted pulse makes position uncertain — that is
what the snapshot's uncertainty flag reports.

**Retargeting.** A point target inside stopping distance, or opposite the
current velocity, is rejected without altering the active plan. Gracefully
stop before such a retarget. Per-move limits do not overwrite configured
limits; timestamped limit updates preserve ongoing velocity and apply the new
ramp limits.

**Time.** Commands carry monotonic microsecond timestamps; modular ordering
is valid within half the 32-bit timer horizon. The adapter must service edges
independently of its slower control update. Operation requests and recovery
carry a coherent pair of local millisecond and microsecond observations, which
preserves fractional command time and the coarse epoch through fine-counter
wrap.

**Load.** Feeds to hub, then to the configured physical arrival switch, then
transitions locally into forward following and emits load completion **while
retaining path ownership**. A failed feed is retried locally, up to three
attempts within 240 s, with no intermediate event. **Buffer pressure alone is
never arrival evidence.** The host runs the printer's capture/prime macros
and awaits queued printer motion at that handoff.

**Unload and follow.** Explicit following can change direction through a new
owned intent: the host selects reverse following before printer
retraction/cutting, and requests autonomous unload after printer motion
completes. Unload retracts until hub and arrival signals clear, then brakes
and stops. Powered rewind is optional and is **not** engaged against
remaining forward feeder motion; its outputs stop immediately on
cancellation or fault. Ordinary unload does not wait for the spool's bay
sensor to clear.

**Calibration.** Requires a configured valid encoder and physical
hub/arrival sensing. It measures travel to arrival, reverses locally, waits
for clearance and settles. Probe and retract phases have separate deadlines
and progress supervision. **This is not yet a complete persisted/homed
geometry calibration workflow** — which is why the registry's
`ptfe_calibration` default is `false` for every klipper-mmu family.

**Selector.** Homing on first use for every kind except `indexed`, which
homes on gate 0's own index switch. A forward-only `indexed` machine (AFC's
Claymore) searches `FORWARD` unconditionally and has no position table to
calibrate into; `INDEXED` is a selector *shape*, not a distinct kind.

**Feeder current.** The shipped TMC feeder fixes run/hold current at
initialization and does **not** advertise runtime current changes. Its
readback verifies those fixed settings and an unexpected change faults the
feeder. The operation core also uses the configured run current as its
ceiling, so **encoder-based stall current escalation is not available in the
current firmware**. Driver register settings are not current measurements.

**Buffer.** Invalid buffer feedback suppresses the follower command
immediately, then latches a persistent signal fault after the debounce
period. Analog profiles use a proportional speed law around the buffer
midpoint with configured gain and deadband; switched profiles retain
hysteresis. Both feed velocity intent into the same local acceleration
planner. DiffA/DiffB uses explicit monotonic paired calibration along the
user's two half-ranges around 1.65 V.

**Outstanding (not implemented).** Extra profile-specific parking geometry,
full eject, and a persisted/homed geometry calibration workflow.

## 9. Gaps and open questions

1. **No C2 publication of the wire.** The host's `[klipper_mmu …]` observer
   publishes the unit's identity, lane, bay count and capabilities, but no
   `km_*` value — not the op state, the motion stage, the retained events or
   the fault — reaches the C2 `devices` object. A UI can see that an
   8-lane BoxTurtle exists; it cannot see that its selector is homing.
2. **The mapping in §7 is documentation, not code.** Nothing in `src/`
   decodes a `km_*` value into a C1 or C2 enum today.
3. **C1's eight-bit `gen` versus C5's full identity.** C1 matches a
   completion by an 8-bit `op_gen` in 0..255; C5's identity is
   `(instance, client, session, request, operation, lane)`. The protocol
   contract requires the existing 8-bit generation to be **explicitly mapped
   to a full firmware identity** rather than truncated or used as a session
   number; that mapping is not built.
4. **Sensor faults are one code.** `Fault.SENSOR_FAILURE` (11) covers every
   sensor, so a C2 client cannot tell a dead BAY switch from a dead toolhead
   switch without reading `km_diagnostic_detail`.
5. **Two C5 faults have no C1 name** (`CONTROL_TIMING`, `SELECTOR`), and nine
   C5 stages have no C1 counterpart (§7.5, §7.6).
