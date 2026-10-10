# Explicit Klipper driver API extensions

Klipper-MMU remains a separately maintained optional add-on. Its control core,
profiles, commands and host adapter live in this package. The pinned Klipper
source does not expose every driver operation needed by an autonomous MCU
caller, so `drivers-local.patch` adds explicit optional APIs to the existing
drivers. It is **not a zero-patch integration**. Original attribution and
GPL-3.0-only licensing remain in upstream sources; see Klipper's COPYING.

Pinned source: Klipper `ce7002bedf37e938bb483572949f3703ac6476cb`.
[source-hashes.json](source-hashes.json) is the authoritative file inventory.
At this review it contains thirteen paths: nine existing sources and four new
headers. Both the manifest and patch bytes are pinned in `tools/install.py`.
Use the explicit install/check/remove-driver-api actions in
[installation](../docs/install.md). Unknown source versions of those thirteen
paths and partially installed bundles are refused. Remove an older bundle using
its matching package before installing this one; do not force-apply a
mismatched patch.

## Kconfig ownership

The bundle does not modify `src/Kconfig`. This package declares the eight
driver options in [`mcu/Kconfig.driver_api`](../mcu/Kconfig.driver_api) with
the earlier names, types, dependencies, ranges, defaults and help, so saved
`.config` files and the add-on's `select` statements keep working. The
ordinary add-on hook reaches that file, so `src/Kconfig` changes only by the
marked `osource "src/klipper_mmu/mcu/Kconfig"` line; `src/Makefile` gets
`-include src/klipper_mmu/mcu/Makefile`. Both are optional includes, so a
moved or deleted package cannot break other builds from that checkout. Any
upstream `src/Kconfig` is accepted; a local Klipper checkout at `57a018a`, for
example, matches the thirteen pinned paths but not the pinned `src/Kconfig`.

Without the hook the options are undefined. The patched sources test them only
with `#if`, so they preprocess exactly as with the options off. A portable test
compares preprocessed installed sources with the options undefined and defined
as Klipper writes disabled options (0). The release matrix compares the exact
compiler input for the patched drivers across stock builds without the hook,
with it, and with a moved package. With the hook but no driver API, an enabled
option stops `make` with an `install-driver-api` message.

The previous twelve-path bundle is this bundle's other eleven files plus the
`src/Kconfig` hunk kept as [`previous-kconfig.patch`](previous-kconfig.patch).
The installer recognizes only its exact pinned `src/Kconfig` bytes.
`check-driver-api` refuses that state and names the repairs:
`install-driver-api` reverses only the hunk, and `remove-driver-api` restores
all twelve files. Edited copies of those declarations are refused. This is one
recognized predecessor, not a general patch manager.

## Extension audit (A2)

| Paths | Purpose and reason to retain |
| --- | --- |
| `src/tmcuart.c`, new `src/tmcuart.h` | Reuse the existing UART bit engine for local setup/submit/cancel and task-context completion. The MMU must initialize and supervise its own TMC drivers; a host-only command interface cannot provide that ownership. Ordinary and local clients share the driver and enforce pin exclusion. |
| `src/stm32/hard_pwm.c`, new `src/stm32/pwm.h` | Query the actual route table before hardware mutation and expose exclusive paired outputs for powered rewind. This avoids a copied route table or a second timer/PWM implementation. Timer ownership, compare confirmation and ENABLE-off shutdown remain in the shared driver. |
| `src/stm32/stm32h7_adc.c`, new `src/stm32/adc_local.h` | Reserve a same-unit external pin pair while reusing upstream setup/sample/read/cancel. This prevents a normal ADC client from interleaving conversions with the local pair. It adds sequential paired acquisition, not simultaneous sampling or current sensing. |
| `src/stm32/stm32h7_gpio.c`, `src/stm32/gpio.c`, new `src/stm32/gpio_local.h` | Atomic boot-lifetime pin reservations and local input/output setup in the respective upstream family driver. Ordinary setup refuses local pins before mutation; local setup requires an existing claim. Sharing remains allowed between ordinary clients. |
| `src/stm32/gpioperiph.c`, `src/stm32/internal.h` | Apply the same ownership checks to peripheral alternate-function setup. Guarded local and ordinary entrypoints use the same register implementation; omitting this hook would allow a peripheral client to bypass the GPIO reservation. |
| Package-owned `mcu/Kconfig.driver_api` (formerly patched into `src/Kconfig`) | Default-off gates and fixed capacity settings for these driver APIs. They are no longer part of the upstream patch; the ordinary add-on hook sources them, and undefined options behave as off. |
| `src/stepper.c`, `src/stepper.h` | Queue locally generated step segments into Klipper's existing step engine instead of adding a second edge interrupt for the local rate core. Each local stepper owns a private move-node pool, so locally queued segments never consume the host's shared move queue and host flow control stays true. An empty private pool, a zero step count or too little start-clock lead is refused rather than shut down. |

No driver group is removed by this review: each supplies an inspected
capability or prevents a real ownership bypass. Only the option declarations
moved out of the patch. The architecture constraint is to prefer public
upstream APIs, not to remove conflict checks or return motor ownership to the
host. This audit does not establish that every line is the smallest possible
implementation. Exact source pinning of the thirteen remaining paths is still a
maintenance cost and limits installation to reviewed upstream bytes.

A future upstream API can replace a group only after it provides the same
setup, cancellation, completion, shutdown and ownership behavior. Acceptance
requires ordinary-client coexistence, disabled-build equivalence, failed/partial
setup tests, and install/remove preservation. A successful cross-compile alone
cannot waive those checks. A reviewable source overlay may improve update
maintenance; it must not duplicate the peripheral driver or silently bypass the
source-compatibility gate.

## Current capability limits

- Local GPIO and TMC UART are admitted for H723, H743, H750, F207, F405,
  F407, F429, F765, G431, G474, G0B0 and G0B1. These are selected MCU targets,
  not a promise about every package or board pin.
- PWM route queries use upstream STM32 routing. Local paired outputs and local
  ADC pairs remain H723-only options. Their support does not follow merely
  from enabling GPIO/TMC on another family.
- ADC1, ADC2 and ADC3 external pairs are supported on that H723 path. Reads are
  sequential and must satisfy configured conversion, pair-span and freshness
  limits. `ADC_TEMPERATURE` is not an external GPIO. This is neither synchronized
  ADC acquisition nor a BLDC/current-control implementation.
- Driver current settings and confirmation are distinct from measured current.
  The shipped feeder confirms fixed startup settings; runtime current escalation
  remains unimplemented.

## Ownership and startup

With `WANT_GPIO_LOCAL`, the complete-batch caller first reserves all intended
local pins through `gpio_local_reserve`. This performs no clock/MMIO setup.
UART, ADC and paired PWM setup consume those claims and never reserve pins
individually. A missing claim fails before slot/unit/timer ownership or physical
mutation. Read-only PWM clock calculations may occur before that check. UART
permits a reserved single-wire pin; ADC owns its unit until reset; paired PWM
owns its timer and initializes output levels inactive before alternate-function
setup. Claims persist until reset and shutdown cannot reopen them.

`km_board_startup_begin` reserves exactly the expected plan's PIN entries after
configuration/resource validation and core initialization. It omits timer/ADC
claims from the GPIO batch. Configured admission is available only with the
ownership API; an explicitly supplied external plan still undergoes full
validation and inclusion checks. Trusted Klipper configuration also validates
`RESERVE_PINS`, aliases and `BUS_PINS`. Participating driver APIs enforce the
runtime ownership rules; arbitrary direct register access is outside them.

Hosted diagnostic startup now invokes that composition after Klipper
configuration, checks boot identity, and attaches commands after readiness on
admitted configurations. The actuation option is off by default and is an
actual START admission gate. It does not prevent safe inactive initialization or
observation. The implementation and retained runtime/build checks are described
in [implementation status](../IMPLEMENTATION_STATUS.md) and the
[release matrix](../tests/release_matrix/README.md). Physical qualification is
separate; neither a driver API choice nor a catalogue name proves a wired MMU.

Independent driver evidence:
- the per-driver acceptance records in `../docs/plan/status-history.md`
