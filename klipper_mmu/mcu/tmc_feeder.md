<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Fixed-current TMC feeder composition

`tmc_feeder.c` is a callable, board-local composition of the existing STEP/DIR
GPIO binding and single-wire TMC2209 UART binding. It has no boot hook, host
command, controller attachment, automatic retry, current-update path or motor
start. It keeps one static immutable lane identity and fixed run/hold current
target per slot.

The caller must first own and validate the complete board pin/peripheral plan,
profile, electrical current ceiling and actuation gate. `km_tmc_feeder_setup`
checks all four distinct canonical pins, both portable configurations, the
actual `CONFIG_CLOCK_FREQ`, exact profile run/hold targets and existing claims
in both GPIO bindings before touching any output. Once physical preparation
begins, claims remain immutable even if UART setup later fails.

`claims_available`'s own UART check lets exactly one case through instead of
refusing it: this slot's UART pin landing on another lane's UART pin. That is
Happy Hare's EASY-BRD wiring (gear and selector TMC2209 on one shared wire,
distinct addresses; `docs/selectors.md`), and `km_tmc_uart_gpio_setup` alone
decides whether it is actually valid -- still refusing equal addresses. The
same pin landing on another lane's STEP/DIR/ENABLE remains a conflict.

Setup prepares inactive ENABLE/STEP/DIR outputs and passes the STEP/DIR raw
safe-off callback/context directly to UART setup. UART then initializes
asynchronously through Klipper's shared bit timers and task callbacks, with
independent MMU safety deadlines. The feeder remains unpublished and cannot be
enabled. A caller may attempt `km_tmc_feeder_activate(slot)` from ready task
context after a local state transition; an unready UART returns
`KM_ERR_UNAVAILABLE` without polling or consuming the prepared STEP/DIR slot.
Activation requires one coherent UART status snapshot with no fault, confirmed
configuration, and requested **and confirmed** run/hold values exactly matching
the immutable targets. The STEP/DIR activation checks again before publication.
Only after success does the published driver's run/hold fields reflect those
confirmed register settings. They are not measurements of physical current.

During motion, UART safe-off immediately drives ENABLE inactive and latches the
STEP/DIR driver fault without fabricating a falling STEP edge. The local
controller/trajectory must service the fault, mark position uncertain and stop;
the interrupted pulse is not counted. A bare UART restart never clears the
STEP/DIR fault, reconciles position or reenables the actuator.

`km_tmc_feeder_restart(slot)` is the explicit, stepwise lane restart used by
the `km_driver_restart` command. It requires an activated lane and a caller
that has already proven nothing moves or can be admitted and outputs are
stopped. The first call restarts the UART controller (safe-off, which latches
the STEP/DIR fault, then full identification and configuration) and returns
`KM_ERR_UNAVAILABLE`. Later calls return `KM_ERR_UNAVAILABLE` until one
status snapshot shows ready, no fault and requested/confirmed currents equal
to the immutable targets; they then call `km_stepdir_gpio_clear_fault` and
return its result. A fault meanwhile returns `KM_ERR_FAULTED` and ends that
restart, keeping the latch. No call enables an output or touches the
operation core; position and operation faults are reconciled separately. A
startup failure is not covered: board startup fails as a whole before this
path is reachable.

`supports_current_setpoint` remains false: the UART current-update API
acknowledges asynchronous acceptance, so it cannot satisfy the common
operation core's same-turn set-current/enable contract.

The composed native suite uses the real STEP/DIR, local UART binding, shared Klipper bit engine, register,
clock and trajectory sources with a virtual TMC and timer shim. It exercises
delayed readiness, preclaim rejection, startup failure, mismatched current,
two-lane isolation, a UART fault during high STEP, explicit restart and latch
clear, and global shutdown. A joined case also links the registered command
handlers and operation core: a DRV_STATUS protection fault during LOAD stops
the core; `km_driver_restart` fails while the virtual chip does not answer
(a modeled supply that is still off), then restarts and clears the lane while
the other lane keeps its diagnostics; reconciliation then clears the operation
fault, and the same fault latches again. Physical timing and electrical
behavior still require target-board qualification.
