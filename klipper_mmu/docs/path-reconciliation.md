# Explicit path reconciliation

## Problem

`km_core_set_presence_at` publishes one physical acquisition batch. When HUB or
TOOLHEAD asserts and no operation owns the path, the core sets retained
`path_occupied` with `path_occupant_lane = KM_LANE_UNKNOWN`. This is correct:
bay switches cannot tell which lane supplied filament in the shared path, and
several bays often hold filament. Absence or an invalid reading never erases
that retained occupancy. Every motion admission then refuses with
`PATH_OCCUPIED`, and the host cannot prove either an empty or a known path. So a
healthy MCU that boots with filament in the path, or whose path was cleared by
hand, has no way forward except an operator decision.

`km_core_recover` cannot be used here. It resets position, faults and
reservations, and is intended for fault recovery, so it is too broad for this
case.

## Service

`km_core_reconcile_path(core, occupied, lane, time)` in the shared core is the
only mutation. `mcu/commands.c` exposes it as:

```
km_path_reconcile instance_hi=%u instance_lo=%u query=%u occupied=%c lane=%c
km_path_reconcile_result instance_hi=%u instance_lo=%u query=%u result=%c status=%c
```

`result` is the ordinary administrative code (0 OK, 1 unbound, 2 instance
mismatch, 3 invalid, 4 stale query, 6 shutdown, 7 clock unavailable). `status` is the negated
core `km_result_t` and is non-zero only when `result=0`. `query` is a
non-zero query of the latest retained physical capture. Firmware permits only
one reconciliation attempt per capture, including refused attempts. An older
query or duplicate attempt is rejected, so a delayed duplicate cannot act on a
later occupied path. The capture must also be no older than the profile's
sensor-age limit, even when new sensor readings are available. Byte fields are range-checked before narrowing.

| Action | Wire | Evidence required (all fresh, valid, not future) | Result |
|---|---|---|---|
| CLEAR | `occupied=0 lane=255` | Configured TOOLHEAD absent; physical HUB absent; on a virtual hub every lane's LOAD absent | `path_occupied=false`, state stays IDLE |
| ADOPT | `occupied=1 lane=n` | Physical HUB present (virtual hub: LOAD `n` present and every other LOAD absent); BAY `n` present; LOAD `n` present when configured; TOOLHEAD fresh (any value) | occupant `n`, state LOADED |

### What ADOPT means

ADOPT is the operator's statement that the filament currently in the shared
path is one continuous strand that starts at lane `n`'s spool and runs through
that lane's feeder. The firmware cannot verify this. It checks only that the
fresh sensors do not contradict the statement: an absent BAY or LOAD on lane
`n`, or an absent HUB, is refused. Bay presence alone is never treated as
evidence of which lane supplied the filament. After ADOPT, only lane `n` can be
admitted, and the expected next step is an ordinary same-lane UNLOAD that
drives lane `n`'s feeder in reverse. If the statement is wrong, that UNLOAD
drives the wrong feeder. Protection then depends on its normal clearance
timeouts and sensor checks.

### Preconditions (checked in this order; any failure changes nothing)

1. `INVALID_PARAM`: bad time, ADOPT lane outside `lane_count`, or CLEAR with
   any lane other than 255.
2. `FAULTED`: a fault event still waiting to be queued, or a buffer fault.
   Outside FAULT state also a system/motion fault, trajectory fault or
   position uncertainty, or follower fault.
3. `BUSY`: an unsettled terminal, a stage other than STOPPED, a pending
   rewind, event reservations, any queued (unacknowledged) event, or a moving
   trajectory. Outside FAULT state also a path owner, active client/operation,
   or armed follower.
4. `FAULTED`: any bound feeder, rewinder or selector enabled, stepping, driving
   PWM, or reporting a driver fault. Reconciliation never calls a driver, so
   it never turns an output off either.
5. `STALE`: nothing to reconcile. Outside FAULT state: the state is not
   IDLE, the path is not occupied, or its lane is already known.
6. `NOT_SUPPORTED`: no physical or virtual HUB for ADOPT, or no HUB/TOOLHEAD
   for CLEAR.
7. `UNAVAILABLE` if any required reading is stale, invalid or later than the
   observation; otherwise `CONFLICT` if a fresh reading contradicts the
   action.

A success from IDLE commits the command time only. It does not change events,
terminal history, per-client session/request counters, trajectory position,
faults, or the host lease, and it does not grant motion permission. A host lease is not
required, because the host cannot prepare a session while occupancy is
unknown. The exchange itself carries the attached MCU instance. Global
shutdown refuses the command. Reconciliation is not gated on start
permission, because it does not actuate anything.

## From a stopped fault

The same statement repairs an operation fault (parity P2). `km_core_explicit_stop`
has already disabled every output and emitted the fault or failed terminal, and
it retains the owner and path so that nothing is silently forgotten. Once the
host has acknowledged every event, CLEAR or ADOPT with fresh, consistent
sensors replaces that retained state:

- the latched system and motion fault, trajectory fault and position
  uncertainty clear; the confirmed step count is kept unchanged;
- the owner identity (client, session, request, operation) and shared-path
  ownership clear, and the follower is re-initialized disarmed;
- the state becomes IDLE (CLEAR) or LOADED for the adopted lane (ADOPT).

Events, terminal history and per-client session/request high-water marks are
kept. Replaying the failed request returns its retained terminal, and any older
request identity stays stale. A later fault behaves exactly like the first.
A driver that reports its own fault (TMC communication after its retries, TMC
electrical protection, a PWM bridge emergency latch) still refuses with
`FAULTED`: reconciliation never clears a driver latch. A TMC feeder lane is
first repaired with the separate [driver restart](#restarting-a-lane-driver).
The PWM bridge emergency latch, clock discontinuity and Klipper shutdown still
require an MCU reset.

Operator sequence after a fault: acknowledge its event through the normal
delivery or `KLIPPER_MMU_ACK_FAULT`; inspect the filament; if a lane's TMC
driver faulted, remove the cause and run `KLIPPER_MMU_RESTART_DRIVER` for that
lane until it reports ready; then
`KLIPPER_MMU_RECONCILE_PATH ACTION=CLEAR` if the shared path is empty, or
`ACTION=ADOPT LANE=n` if lane `n`'s filament is still in it. New motion still
needs a HOST session: `KLIPPER_MMU_RECOVER_SESSION` then releases the retired
session on the same connection, and the managed unit's next refresh prepares
a session for the adopted lane with `choose_occupied_host_session` (a Klipper
restart prepares it at connect). The host command reports `fault cleared` when
its fresh capture showed the FAULT state.

An OpenAMS-bound unit additionally latches the physical lane (see
[OpenAMS integration](../integration/openams/README.md)) when it accepted
that fault through `accept_firmware_fault`. On the same call that clears a
stopped fault, this command takes one more fresh capture proving the firmware
left FAULT (state not FAULT, `system_fault` NONE), then calls the bound
OpenAMS runtime's `accept_firmware_fault_cleared(lane, identity)` with the
exact pair `retry_retained_fault_ack` accepted, and reports the outcome in the
same response: `; OpenAMS lane cleared` on success, or `; OpenAMS lane still
latched, restart Klipper before using it` when the OpenAMS source lacks that
hook (ams-family before its verified-clear commit) or the clear was refused.
Only a Klipper restart clears the OpenAMS latch after that; a later
`KLIPPER_MMU_RECONCILE_PATH` no longer sees a stopped fault to clear, so it
does not retry the OpenAMS call.

## Restarting a lane driver

A TMC2209 feeder lane latches a driver fault on a communication failure after
its retries, a wrong identity or configuration readback, GSTAT reset or
driver error, or DRV_STATUS protection. The UART binding forces ENABLE off and
latches the lane's STEP/DIR driver fault, and nothing clears either latch on
its own. Images built with the shared TMC UART binding
(`WANT_TMCUART_LOCAL` and `KLIPPER_MMU_TMC_UART_SHARED`) add one explicit,
stopped-only repair in `mcu/commands.c`:

```
km_driver_restart instance_hi=%u instance_lo=%u query=%u lane=%c
km_driver_restart_result instance_hi=%u instance_lo=%u query=%u result=%c status=%c
```

The guard matches reconciliation: attached instance, no Klipper shutdown,
`query` equal to the latest retained capture and no older than the sensor-age
limit, and one restart step per capture (a separate high-water mark from
reconciliation). `result` is the same administrative code. `status` is the
negated `km_result_t`, non-zero only with `result=0`:

| status | Meaning |
|---|---|
| 0 OK | The chip is ready with the lane's fixed run/hold currents and the STEP/DIR latch is cleared. No output was enabled. |
| 3 UNAVAILABLE | A restart is in progress: just started or still reconfirming. Ask again with a newer capture. |
| 2 BUSY | Not stopped: stage other than STOPPED, a pending terminal or rewind, a moving trajectory, or the lane's ENABLE or STEP logically active. |
| 4 FAULTED | The restart failed or the chip faulted again. The latch stays; the next step starts a new restart. |
| 1 INVALID_PARAM | Lane outside the profile's `lane_count`. |
| 8 NOT_SUPPORTED | The lane's bound feeder is not that lane's TMC feeder driver. |

Each step is `km_tmc_feeder_restart` in `mcu/tmc_feeder.c`:

1. The first step calls `km_tmc_uart_gpio_restart`. It forces the actuator off,
   which also latches the STEP/DIR fault if it was clear, so START is refused
   for the lane throughout. It cancels the shared UART transfer, applies the
   256-bit quiet guard and repeats identification, register writes with IFCNT
   confirmation and readback. The handler returns at once; it never waits.
2. Later steps return UNAVAILABLE until the UART status is ready with
   requested and confirmed currents equal to the lane's fixed targets, or
   FAULTED if a fault latched meanwhile.
3. The final step calls `km_stepdir_gpio_clear_fault`. Under the IRQ
   serialization used by raw safe-off, it runs the portable STEP/DIR clear:
   refused while ENABLE or STEP is logically active (the core's stop has
   already driven both inactive), while the ready callback is false, or while
   the fault callback reports a fault. It rebases edge timing left uncertain
   by an interrupted pulse from a fresh clock and writes no output.

The command never changes the operation core. A stopped operation fault, its
owner, position uncertainty and events stay for reconciliation. A fault that
recurs latches the UART and STEP/DIR state exactly as the first one did.
Global Klipper shutdown and the PWM bridge emergency latch remain reset-only.

After a fault while idle, the core stays IDLE; the latch refuses START on
that lane and any reconciliation, and the restart alone clears it. After an
operation fault, the operator sequence is: acknowledge the event, remove the
cause (for example restore the motor supply), `KLIPPER_MMU_RESTART_DRIVER`,
then `KLIPPER_MMU_RECONCILE_PATH`.

## Host

`path_reconciliation: True` in a `[klipper_mmu NAME]` section registers:

```
KLIPPER_MMU_RECONCILE_PATH MMU=NAME ACTION=CLEAR
KLIPPER_MMU_RECONCILE_PATH MMU=NAME ACTION=ADOPT LANE=n
```

The command requires an attached, connected instance and no other exchange in
progress. It first obtains a fresh same-instance physical capture, then uses
that capture's query for one guarded exchange in `host/path_reconciliation.py`, which follows
the `command_transport` register/send/wait/unregister pattern. Success is
reported only for `result=0 status=0`. Every refusal, a connection change, or
a timeout raises a G-code error. A timeout leaves the outcome unknown, so read
`KLIPPER_MMU_SNAPSHOT` before retrying. Nothing is sent automatically, and
lane choice is never inferred.

`driver_restart: True` registers
`KLIPPER_MMU_RESTART_DRIVER MMU=NAME LANE=n` with the same connection and
fresh-capture checks, through `send_driver_restart` in the same module. It
takes a new capture for every step. While firmware reports UNAVAILABLE it
pauses 0.1 s and repeats, for at most 2 s. It reports success only for
`result=0 status=0`, and says when the capture showed a stopped fault that
reconciliation must still clear. Any refusal, connection change, timeout or
the 2 s bound raises a G-code error. After the bound a later invocation
continues the restart in progress rather than starting another.

After CLEAR, the existing cold HOST session preparation can see an empty path.
After ADOPT, `choose_occupied_host_session` can see a LOADED, known lane,
provided that the lane's BAY and the shared path are present in the next
capture.

## Not covered

- The PWM bridge emergency latch, clock discontinuity, a fault event that
  could not be queued, and session takeover. These still need an MCU reset or
  their own explicit paths.
- A TMC fault during board startup, including a motor supply that is off
  when hosted diagnostic startup runs. Identification fails, board startup
  latches its failure and shuts every component down before commands attach,
  so the driver restart is unreachable. An MCU reset after power returns is
  still required.
- An end-to-end joined host test with a driver restart. The FPS host join
  (`tests/host_h723_command_join`, `MODE=fps`) covers a FOLLOW fault, its ACK,
  ADOPT, `KLIPPER_MMU_RECOVER_SESSION`, a new HOST session and UNLOAD.
  Natively, the registered restart and reconcile handlers run against the real
  feeder, UART binding, virtual TMC and core (`tests/klipper_tmc_feeder`).
- A lane already known to the firmware, including PRELOADED reservations and a
  single asserted virtual-hub exit, which is attributed at acquisition.
- Physical qualification. Native, wire-codec and host tests exercise these
  services. No hardware result is claimed, including UART timing, driver
  electrical behaviour and motor state after a restart.
