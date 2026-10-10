# Diagnostics and guided commissioning

Most BoxTurtle owners have an improper mechanical build: switch polarity,
missing pull-ups, reversed motors, swapped lanes, or filament/string stuck in
the hub. See BoxTurtle field failures for
the ranked research this design is built from. `host/diagnostics.py` is
plain functions over one `Finding(severity, subject, meaning, action)`
shape, applied to a fresh capture. No registries, no class hierarchy.

Phase 1 rules (1–9 below) need only the existing physical capture
(`KLIPPER_MMU_PHYSICAL`'s path), the retained fault, the LOAD attempt report
and the stored Bowden estimate. Phase 2 rules (10–12) add one new read-only
firmware query, `km_diag_part` (mcu/commands.md,
[R1 task boundary](controller-task-boundary-audit.md)): per-switch debounce
bounce counts (`mcu/switch_gpio.c`), per-switch idle-edge counts
(`src/operation_core.c`) and per-lane TMC2209 GSTAT/DRV_STATUS
(`mcu/tmc_uart_gpio.c`). Firmware adds no polling and changes no fault
policy; it only exports counters and registers it already keeps. Both
phases are the same small-function shape; nothing here needed a different
one.

Lane numbers in every message are 0-based, matching `LANE=` on every other
`KLIPPER_MMU_*` command and the LOAD attempt report.

## `KLIPPER_MMU_DIAGNOSE MMU=NAME`

Takes one fresh combined capture (the same path as `KLIPPER_MMU_PHYSICAL`),
applies every rule below, and prints one line per finding, or `no problems
found` plus a one-line state summary. Read-only: it never moves anything and
never affects an ACK.

When the board never finished starting, there is nothing to capture, and
DIAGNOSE used to shrug with "requires an available connection" at exactly
the moment you needed it most. Now it explains the failed startup instead
(`KLIPPER_MMU_QUERY` prints the same line): which step stopped, on which
lane, the real cause, and any offending pins by name. For example, on a
board with no drivers fitted:

```text
klipper_mmu box error: startup: startup stopped while waiting for the TMC2209
driver to answer on lane 0: it did not answer, or answered with a fault --
check that this lane's TMC2209 is fitted and has motor power, and that its
UART pin is right (expected on a bare board with no drivers), rebuild if a
setting changed, then FIRMWARE_RESTART
```

A startup failure stays latched until the MCU resets, so fix the cause and
`FIRMWARE_RESTART`. Pin maps that could never start (a respooler pair on the
wrong timer, DiffA/DiffB on two ADCs, a pin used twice) are refused by the
build before you get as far as flashing.

The same rules run once after every ACKed operation terminal (LOAD, UNLOAD,
PRELOAD, EJECT, CALIBRATE), using the hook already used for the LOAD attempt
report. Only *new* error/warning findings are printed, so a problem that
persists across many operations is not repeated every time; if it clears and
then comes back, it is reported again. A diagnostics failure here is caught
and logged, like the LOAD attempt report; it never affects the ACK or motion.

### Rules

1. **Switch not valid/fresh.** Any configured BAY, LOAD, HUB or toolhead
   switch whose reading is not valid or not fresh is named directly:
   wiring, noise, or a disconnected sensor; a floating input needs a
   pull-up (`^`) in the pin config.
2. **LOAD made, BAY open (at rest).** With nothing in progress, a lane's LOAD
   switch reading made while its own BAY reads open means the BAY lever,
   wiring or polarity (`!`), or filament fed in without a spool at the bay.
3. **HUB made, path reported empty (at rest).** Filament or a string stuck in
   the hub, a stuck switch lever, or inverted hub polarity. This never
   suggests ejecting every lane (field failure #2) — check the hub itself.
4. **Firmware says lane N is loaded, but its BAY or LOAD reads empty.**
   Runout, broken filament, or a switch fault on that lane.
5. **More than one lane's LOAD made with HUB made.** Which lane is actually
   in the hub cannot be known from switches alone; reconcile explicitly with
   `KLIPPER_MMU_RECONCILE_PATH` rather than guessing.
6. **FPS buffer.** At or above the 700-permille arrival threshold with
   nothing loaded: a displaced magnet or inverted/wrong DiffA/DiffB
   calibration. Invalid or stale: DiffA/DiffB wiring or the FPS cable. The
   FPS reads compression only; 0 is the normal rest (slack or tension),
   never a fault by itself.
7. **A retained fault.** Every `host/wire.py` `Fault` code gets a plain
   meaning and an action, for example `DRIVER_FAULT` (motor power, cable, or
   ground the motor screws after a static reset, then
   `KLIPPER_MMU_RESTART_DRIVER`) or `HOST_COMM_LOST` (an overloaded host or a
   flaky USB/CAN link). `CONTROL_TIMING` is the opposite kind of advice: the
   controller missed its timing (the step queue ran dry, the control task
   stalled, or the MCU clock failed), which is MCU load or a firmware problem,
   not wiring. `NO_PROGRESS`/`JAM`/`FILAMENT_LOST` point at the last
   LOAD attempt report.
8. **Bowden drift.** If the last LOAD's arrival travel differs from the
   stored `KLIPPER_MMU_CALIBRATE` estimate by more than 10%, the path length
   probably changed (PTFE moved, cut, or a coupler slipped); recalibrate.
9. **Board-startup driver fault.** A faulted diagnostic startup, like
   Klipper's own TMC startup errors, needs a firmware restart. Most often
   motor power was off when the MCU started (MCU on USB power alone), or a
   UART/motor cable is loose: power on, then `FIRMWARE_RESTART`.
10. **Switch bounce.** A switch's rejected-debounce-candidate count grew by 3
    or more since the previous capture: a noisy or floating input. Check the
    connector/crimp, or add a pull-up (`^`) in the pin config.
11. **Idle edges.** A switch's accepted-level-change count, while nothing was
    driving it, grew since the previous capture: by 2 or more for a LOAD or
    HUB switch, or 4 or more for a BAY switch. A loose connector, a worn
    lever, or filament being pushed by hand.
12. **TMC GSTAT/DRV_STATUS.** Absolute bits from the last capture (not a
    delta): `reset` — the driver reset, usually static discharge from PTFE
    in dry air or a supply dip; ground the motor screws/frame. `uv_cp` —
    motor supply undervoltage: 24 V off or sagging. `drv_err` with
    `s2ga`/`s2gb` — a motor phase shorted to ground; with `s2vsa`/`s2vsb` —
    shorted to the supply: check the motor cable. `ot` — the driver
    overheated and shut down; `otpw` (warning) — the driver is hot: improve
    cooling or lower the run current. `ola`/`olb` (warning, "possible") —
    open load; the TMC reports this unreliably at standstill or high speed,
    so only a hint.
13. **Lane drive slip.** The HUB-to-extruder path is shared, so every lane
    should need the same feed to reach the extruder. The host keeps each
    lane's last 5 LOAD arrival travels; once a lane has 3, the median of its
    last 3 is compared with the median of the other lanes (or with the
    `KLIPPER_MMU_CALIBRATE` estimate when there are no other lanes, or when
    they are below it: slip only adds steps). 3% or more above is reported
    as that lane's drive gear slipping (idler tension, dirty gear teeth, or a
    heavy or binding spool). Every lane long together is a path change, left
    to rule 8, and a slipping lane replaces rule 8's "recalibrate" for its
    own LOAD, since recalibrating would hide the slip. The history is host
    memory only and starts empty after a Klipper restart. The 3% threshold
    is provisional until FPS arrival repeatability is measured on hardware.

Rules 10–11 need a baseline (the previous capture) to compute a delta; the
first-ever capture only seeds `KlipperMMU._diag_baseline` (like
`_diagnostic_seen`) and reports nothing. A counter that reads lower than the
baseline is a rebind or firmware restart, not a finding; the baseline resets
to it. Rule 12 has no baseline and fires from a single capture. All three
are silently skipped (no findings, no error) on firmware without
`KLIPPER_MMU_DIAG_COUNTERS`, and TMC-specific findings never fire without
`KLIPPER_MMU_TMC_DIAG` (both counters then read zero).

## `KLIPPER_MMU_COMMISSION MMU=NAME STEP=switches|lane|fps [LANE=n] [ANSWER=yes|no]`

Each step prints what to do, then reads physical evidence (bounded, using the
reactor the way other waits in `host/` do — never a busy-poll or a sleep
loop) and prints a verdict with what to check or change. The instruction text
for every step lives once in `host/diagnostics.py` (`INSTRUCTIONS`). Motion
only ever runs through the existing PRELOAD/EJECT managed-unit command paths
(`KLIPPER_MMU_PRELOAD`/`KLIPPER_MMU_EJECT`). `STEP=switches` and `STEP=fps`
need no motion and work on a `-safe` (`ACTUATION=0`) image, so a builder can
check switches and the FPS before any motor can move; only `STEP=lane`
requires `openams_motion` and its bound `OpenAMSManagedUnit`.

- **`STEP=switches`.** Remove all filament first. Every configured switch is
  read once and printed as a table: `open` (OK), `made` (with nothing
  inserted — most likely inverted polarity, toggle `!` on that pin, or
  leftover filament), or `unknown` (invalid/stale — a floating input, add a
  pull-up `^`, or disconnected). With `KLIPPER_MMU_DIAG_COUNTERS`, each row
  also shows that switch's bounce count since setup, and the step then asks
  the user to gently wiggle each switch cable and connector for a few
  seconds, samples again after about 10 s, and reports (`info`) every switch
  whose bounce or idle-edge count moved during that window — a way to
  correlate "I touched this connector" with the evidence.
- **`STEP=lane LANE=n`.** "Insert filament into lane n until the BAY switch
  clicks." Waits up to 60 s for lane n's BAY switch to make. If a *different*
  lane's BAY or LOAD changes first, the two lanes are reported wired swapped
  and nothing moves. Otherwise it runs `PRELOAD` on lane n (success means
  feeder direction and the LOAD switch are OK; failure means the feeder
  motor is reversed or not gripping, or the LOAD switch is not working), then
  `EJECT` (powered rewind) and asks the user to watch the spool. Re-run with
  `ANSWER=yes` or `ANSWER=no` (no motion, no wait) to record whether the
  spool wound the filament back; `no` means the N20 respooler is likely
  wired reversed or slipping.
- **`STEP=fps`.** "Push the FPS buffer fully, then release it." Samples the
  buffer position for about 10 s at roughly 10-20 Hz. Never moved: the FPS
  cable, DiffA/DiffB wiring, or a displaced magnet. Did not reach 900:
  calibration, or the buffer lacks its full mechanical travel. Did not
  return near its start: a sticking slide. High before any push: inverted
  calibration.

## Tests

`tests/klipper_host/test_diagnostics.py` is table-driven over the rules and
the commissioning verdict functions (no MCU or reactor). `tests/klipper_host/
test_diagnose_command.py` covers the `KLIPPER_MMU_DIAGNOSE`/
`KLIPPER_MMU_COMMISSION` command glue, the bounded waits, and that the
post-terminal hook only reports new findings and never affects the ACK.
`tests/klipper_diag_reader/test_reader.py` covers the `km_diag_part` host
reader the same way `tests/klipper_sensor_reader` does for sensors.
`tests/klipper_switch/test_switch.c` (`bounce_counters`) and
`tests/test_operation_core.c` (`test_idle_edges`) cover the firmware
counters natively; `tests/commands/capture.c` (`test_diag_wire`, run through
`tests/test_commands.py` with `KLIPPER_SOURCE`) checks the wire shape
against Klipper's actual codec and size limit.
