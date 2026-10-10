<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Local TMC2209 policy over Klipper's shared UART driver

`tmc_uart_gpio.c` connects the common TMC2209 register controller and small byte
codec to the explicit local API in Klipper's existing `src/tmcuart.c`. Klipper
owns the pad, bit generation and receive sampling. The MMU binding owns startup,
current confirmation, periodic diagnostics and actuator protection. It contains
no GPIO implementation or second UART bit engine.

Build the reviewed driver extension with `WANT_TMCUART_LOCAL`, then select
`KLIPPER_MMU_TMC_UART_SHARED` in the H723 feeder menu. Both are default-off; the
binding is compiled only when both are selected. Installation and menu selection
do not configure physical outputs. There is no board startup caller yet and
ACTUATION remains zero. See [installation](../docs/install.md).

## Ownership and setup

Up to eight independent static slots are supported. The caller must establish
exclusive resource ownership, including conflicts with other Klipper clients,
and provide a raw actuator safe-off callback. It must be bounded, clock-free,
nonblocking and non-reentrant, and must not invoke host communication or a
clock-dependent driver operation. The binding never positively enables a motor.

Setup, restart and current changes require task context with interrupts enabled.
The interrupt-mask check is not a general interrupt-handler detector. The common
clock must already be READY. Slot/pin range, collisions and the common
`km_tmc_uart_config_validate()` are checked before UART setup. Once setup begins,
claims are immutable until MCU reset, including partial failure. The upstream
channel pool must have enough free entries; pool exhaustion fails disabled.
The binding does not take over or release another user's claimed resources.

One pin collision is allowed rather than refused: another already-active slot
on the same pin whose TMC2209 address differs (Happy Hare's classic ERCF 1.1
EASY-BRD wiring, where the gear's and the selector's TMC2209 share one
single-wire UART pin at distinct addresses). That sharing slot reuses the
existing physical channel outright instead of claiming a second one; equal
addresses on one wire, or a pin already poisoned by an earlier failed claim,
are still refused with `KM_ERR_CONFLICT`. `tmcuart_local_submit`'s own busy
check serializes every sharer's transactions onto the one wire, so no two
sharers' frames interleave; each sharer keeps its own confirmation, restart
and fault state, and a fault on one never latches the other (`transport_abort`
only cancels a transfer this slot itself started, never a sharer's).

Successful setup means asynchronous initialization, not readiness. The common
controller identifies and checks the chip, writes the register configuration,
verifies IFCNT and reads back registers. Status distinguishes requested and
confirmed current settings; neither is a measured physical current. Readiness
also requires active protection and no latched fault. Queries return copied
snapshots without servicing the driver or initiating recovery.

## Timing and execution context

The shared Klipper driver owns bit timers and supplies one completion callback
in IRQ-enabled task context after timer finalization. TX data are copied by the
shared API. There is no host command round trip in this local control path.
The binding checks the token, receive framing/CRC, actual completion tick and
common clock before accepting a controller result. It never backdates time to
make a late task completion valid.

The binding uses a separate scheduler timer for task wakes and hard safety
expiry. A pending transaction has its configured deadline. Once ready, the
GSTAT/DRV_STATUS diagnostic pair has a fixed overall stale deadline at
`next_diagnostic_us + km_tmc_uart_diagnostic_budget_us(transaction_timeout_us)`:
both reads, each with every permitted attempt (`KM_TMC2209_MAX_ATTEMPTS`) and
its 256-bit post-error quiet guard. Starting a later read or attempt does not
extend it. The timer can force the actuator off during task starvation.
It latches the fault and defers shared-driver cancellation to task context.

At exact 40000 baud, successful transfers have a six-bit quiet guard; startup,
failed/cancelled transfers and restart use 256 bits. A due diagnostic task wake
has a 200-us scheduling lead while retaining its independent hard deadline.
The pure validator checks the integral-MHz clock, minimum 264-bit transaction
budget and converted half-range bounds used by all three callers: binding,
saved configuration and feeder preflight.

Klipper's existing UART engine determines physical edge/sample behavior. Its
idle pad is driven high and it uses the requested pull during receive input.
The retired separate engine's `baud_ticks / 16` GPIO-lateness detector and input-idle
behavior are not guarantees of this shared implementation. Aggregate IRQ cost,
wire waveforms and board-specific margins require physical qualification.

## Faults and recovery

Communication, clock, deadline and driver-status failures disable the actuator
and remove readiness. Specific controller faults, including overcurrent status,
remain distinguishable from a general transport fault. Explicit restart requires
fresh valid timing and a full quiet guard; it cannot clear the associated
STEP/DIR fault or reconcile uncertain filament position. Its production caller
is the feeder's stepwise `km_tmc_feeder_restart`, which clears that latch only
after this binding reports ready again (see [feeder](tmc_feeder.md)).

Current changes are asynchronous. Accepted changes disable the actuator and
clear readiness until reconfirmation. Invalid/busy rejections preserve existing
monitoring. This is not a synchronous current-change-during-motion capability.

Global shutdown latches through `clear_shutdown` until MCU reset. The binding
runs raw safe-off only, without reading clocks or using task-only UART methods.
Klipper's shared driver handles its pad and shutdown bookkeeping.

See [the public header](tmc_uart_gpio.h) for exact return codes and lifetime rules.
The native shared-driver test target builds the installed upstream source and
checks actual-controller/virtual-chip behavior; it is not hardware qualification.
