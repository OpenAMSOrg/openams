# Firmware command boundary

`commands.c` registers ordinary Klipper commands around one common operation
core. The firmware chooses a controller instance and explicitly attaches the
prepared core with `km_commands_attach(core, instance, changed, allow_start)`.
There is no host attachment command or default-ready motor. Opt-in diagnostic
startup attaches after local clock, identity, calibration (when analog), and
board checks, initially with `allow_start=false`. Until attachment, commands
report UNBOUND. An actuation-enabled build may then grant START through the
module's checked board-readiness callback. The exported `ACTUATION` constant
reports the build option; it is not sufficient by itself to establish readiness.

`km_switch_delay_query challenge=%u instance_hi=%u instance_lo=%u` is an
explicit read-only diagnostic. It replies once with
`km_switch_delay_state challenge=%u instance_hi=%u instance_lo=%u valid=%c delay_us=%u count=%u max_us=%u`.
The reply carries the attached instance (zero before attachment), echoes the
challenge, and reports `valid=0 delay_us=0` for a wrong requested instance or
when the latest controller cycle has no accepted switch sample. It reads only
the controller's retained software interval; it does not sample a GPIO or
change the status snapshot or its protocol version.
`count` is the saturated number of accepted intervals since controller
attachment; `max_us` is their maximum. Both remain available after an invalid
latest cycle or shutdown. A wrong requested instance returns zero for both.

`allow_start` is stored only in the successful-attachment critical section,
with the controller, identity and callback. A failed or duplicate attach cannot
change it. Only firmware can grant a later upgrade with
`km_commands_enable_start(core, instance, ready)`, and only with the compiled
actuation opt-in, matching attachment, usable clock and a ready board callback.
START checks that callback again before admission. Explicit revocation or
shutdown prevents upgrades until MCU reset, including after upstream
`clear_shutdown`. The host cannot set this permission.

A START rejection for missing permission does not consume operation identity
or mutate the core. Cancel, heartbeat, emergency stop, retained acknowledgements
and snapshots remain available within their individual guards. Attachment and
identity otherwise remain immutable until MCU reset. `km_transport_query`
observes this binding; it does not establish boot identity or negotiate ownership.

Handlers run in Klipper task context, as does the controller's control task.
Tasks never preempt one another, so handlers take no lock around the core; only
the controller's edge interrupt runs meanwhile. The mandatory `changed`
callback (`km_controller_changed()`) exchanges the trajectory with that
interrupt: START/CANCEL and snapshot capture call it first, to plan or capture
from the interrupt's latest output, and operation, stop, heartbeat-expiry and
reconciliation calls call it after changing the core. Responses are emitted
once per request. Periodic control, sensing, host-lease checks and raw shutdown
safe-off remain the caller's duties.
An eligible heartbeat also checks expiry before renewing an owned host lease.
At or after its deadline, it stops through the common communication-fault path
and returns STALE, even if the periodic check has not run yet. Later heartbeats
can restore communication but cannot clear that fault or resume motion.

## Commands

Every command except the transport observation includes `instance_hi` and
`instance_lo`. An operational command rejects a mismatch before accessing the
core; the read-only switch-delay query instead returns `valid=0 delay_us=0`.

| Command | Other fields | Behavior |
|---|---|---|
| `km_transport_query` | `challenge` | Echo challenge, instance, last snapshot query and binding state. |
| `km_switch_delay_query` | `challenge`, requested instance | Echo challenge, current instance, latest-cycle validity/delay, and historical accepted count/maximum since attachment. |
| `km_start` | `client session request operation lane direction` | Admit a full-identity operation when attachment allowed start; direction is 0 reverse / 1 forward. Without that permission the same request returns unavailable and does not touch the core. |
| `km_cancel` | `client session request operation lane` | Cancel that complete existing owner identity. |
| `km_heartbeat` | `session` | Renew host lease for an eligible host session; never clear a fault. |
| `km_stop` | none | Enter the common emergency-stop path. |
| `km_path_reconcile` | `query occupied lane` | Explicit operator CLEAR/ADOPT of an idle, unknown-lane occupied path; never moves. Replies once with `km_path_reconcile_result`. See `docs/path-reconciliation.md`. |
| `km_driver_restart` | `query lane` | Shared-TMC-UART builds only. One step of an explicit stopped-lane TMC feeder restart with the reconciliation query guard; clears the lane's STEP/DIR latch once the chip is ready, never enables an output or changes the core. Replies once with `km_driver_restart_result`. See `docs/path-reconciliation.md`. |
| `km_event_peek` | none | Return the retained queue head without removing it. |
| `km_event_ack` | `client session request operation lane seq` | Remove only the exact matching head. |
| `km_terminal_query` | `client session request operation lane` | Query bounded history without re-enqueueing a result. |
| `km_load_attempts_query` | `client session request operation lane` | Read-only. Replies once with the attempt records of the latest admitted LOAD with that identity, or none. |
| `km_snapshot_part` | `query part` | Return one of five parts of the same retained capture. |
| `km_sensor_part` | `query part` | Return one of five physical-sensor parts from that same capture; parts 0–2 retain the original bay/hub/toolhead format, and parts 3–4 report optional per-lane LOAD inputs. No actuation. |
| `km_diag_part` | `query part` | Diagnostic only (docs/diagnostics.md): return one of six parts of switch bounce/idle-edge counters (mcu/switch_gpio.c, src/operation_core.c) and TMC gstat/drv_status registers (mcu/tmc_uart_gpio.c), from that same capture. No actuation, no polling added. |

Wire operations always belong to HOST. They cannot impersonate LOCAL_UI or
INTERNAL to bypass host liveness. Declared byte fields are checked before
narrowing: upstream's integer parser does not enforce `%c` range on reception.
Session/request IDs remain full 32-bit values. Accepted or already-completed
admission never substitutes for a terminal event.

Operation value 6 is sensor-terminated PRELOAD. It uses the full-identity
`km_start` and retained event path (complete/cancel/fail event values 13–15),
then retains a conservative reservation for that lane, even when filament
has not reached the shared hub. A same-lane LOAD can
continue toward the toolhead, or a same-lane UNLOAD can retract until its
local LOAD input clears; another lane cannot claim the path. A sensor fault
or failed clearance keeps the reservation. The legacy
OpenAMS status/action encoder rejects PRELOAD rather than presenting it as a
completed LOAD. No OpenAMS preload command bridge or measured parking offset is
implied by this firmware command.

A LOAD feeds up to three times within one operation and one terminal event
([LOAD retries](../docs/fps-arrival.md#load-retries)); stage 12,
LOAD_RETRY_RETRACT, is its retract between attempts. `km_load_attempts_query`
replies with
`km_load_attempts instance_hi=%u instance_lo=%u session=%u request=%u count=%c stage0=%c fault0=%c travel0=%u stage1=%c fault1=%c travel1=%u stage2=%c fault2=%c travel2=%u`,
echoing the requested session and request. The first `count` (0–3) entries
give, in order, the stage and fault that ended each attempt (fault 0 for
arrival, stage 12 for a stopped retract) and its confirmed feeder microsteps
since that feed began, or since HUB in LOAD_FEED_TO_BUFFER; the rest are 0.
Another identity, an unbound controller or an instance mismatch reports
`count=0`. It is answered during shutdown and never changes the core.
`KLIPPER_MMU_FEEDER_STEPS_PER_M` exports the feeder microsteps per metre
derived from the menu's rotation distance, gear ratio, full steps and
microsteps, for the host's millimetres. It is never unknown: the menu
guarantees nonzero mechanics, so the build refuses a zero scale.

Operation value 4 is PTFE calibration. Its `PTFE_CAL_COMPLETE` event (value 8)
normally carries encoder clicks from the probe start to the toolhead switch.
Without an encoder and toolhead switch, a profile with calibrated FPS arrival
instead reports completed feeder microsteps from HUB to qualified arrival. That
event sets flag 2 (`KM_EVENT_FLAG_STEP_ESTIMATE`), and no other event uses the
flag. The legacy OpenAMS status encoder rejects the flagged event. See
`docs/fps-arrival.md`.

Snapshot queries increase without wrapping for the entire attached instance,
including reconnects. A fresh transport observation reports the high-water mark.
Its challenge and the local connection generation must be verified before using
it. Any valid part can open a newer query; identical query IDs retransmit the
original capture, and older IDs are stale. A new query replaces the one retained
capture. Status and sensor queries share this high-water and capture. UINT32_MAX
exhaustion requires a new verified controller instance, not silently resetting
the counter. Acquisition time is separate from retained core control time.
One part is sent per request to stay within Klipper's serial frame limit.

The first three sensor parts carry separate configured, present, valid and
fresh masks for up to eight bays plus one shared hub and toolhead reading,
along with the MCU observation and original sample times. Parts 3–4 carry
the same independent flags and sample times for up to eight optional lane LOAD
inputs. A present or absent bit is usable only when that input is configured,
valid and fresh. Firmware advertises `KLIPPER_MMU_SENSOR_LOADS=1` when the
extended replies are available; hosts without that constant request only the
original three parts and leave LOAD inputs unknown. Sensor responses neither
assign a shared hub reading to a bay nor establish OpenAMS loaded state.

`km_diag_part` shares the status/sensor query high-water and one retained
capture (`capture_snapshot`), so a replayed query ID serves the same
counters rather than re-reading them. Parts `km_diag_bay_bounce` and
`km_diag_load_bounce` carry per-lane rejected-candidate ("bounce") counts
from `mcu/switch_gpio.c`'s debounce; `km_diag_bay_idle` carries per-lane BAY
idle-edge counts plus HUB's idle-edge count and the HUB/toolhead bounce
counts; `km_diag_load_idle` carries per-lane LOAD idle-edge counts. An
idle-edge is an accepted level change (`src/operation_core.c`) observed
while that lane, or for HUB any lane, was not being driven by this MMU; all
four are saturating `uint16` counters since the switch binding's setup (for
bounce) or MCU boot (for idle-edge), diagnostic only and never read by
control logic. `km_diag_tmc0`/`km_diag_tmc1` carry each lane's last-read
GSTAT (low byte) and DRV_STATUS (`mcu/tmc_uart_gpio.c`'s
`km_tmc_uart_gpio_get_status`, the value already retained before a driver
fault, never separately polled); both are 0 for a lane with no TMC, or for
any lane without `KLIPPER_MMU_TMC_UART_SHARED`. `KLIPPER_MMU_DIAG_COUNTERS`
(always 1) advertises the command; `KLIPPER_MMU_TMC_DIAG` says whether the
TMC parts are this build's real registers.

`km_admission`, `km_event`, the five status messages and three sensor messages use
the existing codecs.
`km_command_result` is an administrative response, never operation completion.
Its `query` echoes request ID for start/cancel/history errors, session for lease
replies, sequence for acknowledgements, snapshot query for snapshot errors, or
zero for stop/peek. Operation waiters require full matching admission/event
identities, not this administrative field.

Binding states (`enum km_binding_state`) are 0 unbound, 1 attached with usable
clock, 2 global shutdown, 3 attached with unavailable clock. State 1 is binding
and clock state, not start permission or motor readiness. Administrative result
codes are 0 OK, 1 unbound, 2 instance mismatch, 3 invalid parameters, 4 stale,
5 empty queue, 6 global shutdown, 7 unavailable. Code 7 covers an unavailable
clock and, for start only, start permission fixed false at attachment. Message
formats are unchanged, and the existing generic host decoder already accepts
this code. These, the command kinds and every other numeric reply field are
also dictionary enumerations (`km_binding`, `km_command`, `km_command_result`,
...) that the host compares at connect; see
[wire enumerations](../docs/protocol-contract.md#wire-enumerations).

Global shutdown latches even before attachment and survives upstream
`clear_shutdown` and soft configuration reset. The command hook does not call
drivers or read the clock. Reads/acknowledgements of retained events/history and
retransmissions of an existing snapshot remain available during shutdown. A
fresh capture cannot fabricate an acquisition time from an unavailable clock.
Retained snapshots are historical observations; consult binding state separately.

The `km_status_motion` part also carries `follower_state` (0 disarmed, 1
armed and idle, 2 driving, 3 faulted), `follower_direction` (0 reverse, 1
forward; meaningful while armed), and `follower_fault`. These fields are copied
from the controller in the same retained capture as the operation identity,
not inferred from motor pulses or buffer pressure. A host must still require a
fresh, same-instance query and reconcile the active FOLLOW identity before
using them as follower confirmation. An accepted command alone is insufficient.
These commands do not clear faults, recover uncertain position or resume motion.

Native verification uses actual registered handlers, production core/clock,
simulated drivers and upstream's Python wire codec. Separate upstream compilation
and C-parser execution are required before claiming installed transport support.
