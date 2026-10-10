<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Klipper MMU firmware integration

This directory implements the native Klipper firmware integration for `klipper-mmu`:
1. The initial `make menuconfig` Kconfig menu surface.
2. The Klipper scheduler clock binding for the accepted portable MCU clock bridge (`klipper_mmu/mcu_clock.h`).
3. The read-only diagnostic command `km_clock_query`.
4. Typed response emitters and the [firmware command boundary](commands.md),
   including retained events and coherent status snapshots.
5. The callable H723 paired-ADC binding for menuconfig-assigned BUFFER_A and
   BUFFER_B GPIOs. SW1/PC4 and SW2/PC5 are optional presets. The reader uses
   the shared driver's first same-unit route on ADC1, ADC2, or ADC3 and
   requires `CONFIG_WANT_ADC` plus `CONFIG_WANT_ADC_LOCAL`.
6. The [saved control-profile factory](config.md), driven by ordinary menuconfig
   choices and the common profile validator.
7. The callable [H723 STEP/DIR GPIO binding](stepdir_gpio.md), with disabled
   preparation, conservative edge timing and a clock-free shutdown hook.
8. Optional [H723 pin configuration](pin_config.md), canonical pin validation
   and reservations consumed by Klipper's existing host pin resolver.
9. Saved feeder electrical levels and timing, validated before initialization
   through `km_config_feeders()` and the common STEP/DIR configuration validator.
10. The callable [local controller scheduler](controller.md), joining operation
    protection and trajectory deadlines without a host motion queue.
11. Saved [TMC2209 register settings](tmc_config.md), using the same pure
    current/register/transport validation as actual driver initialization.
12. Callable [local switch acquisition](switch_gpio.md), with explicit bias and
    polarity, conservative acquisition timestamps and shared-path lane uncertainty.
13. Saved [switch electrical settings](switch_config.md), including explicit
    switched-buffer direction and separation from analog DiffA/DiffB inputs.
14. Saved [rewind bridge limits](rewind_config.md), sharing duty and reversal
    timing validation with the portable output driver.
15. [H723 resource planning](h723_resources.md), expanding saved pins into shared
    PWM and ADC claims. Callable board startup runs this plan before hardware setup.

## Important Limitations & Scope

The default build advertises **ACTUATION=0**. A separate experimental H723
build option can advertise ACTUATION=1 only after diagnostic startup establishes
board readiness; the command path rechecks that permission for every START.
Neither build has accepted hardware motion qualification and neither was
flashed by the software tests. Operational commands remain unavailable until
firmware attaches an initialized controller with a verified instance. Device
selection seeds common control settings; board choice seeds pin defaults. Only
enabling the separate pin menu emits pin reservations. None of these menu
choices establishes physical hardware qualification.

## Upstream Integration Layout

The agreed installer contract with upstream Klipper integrates this add-on as follows:
- Symlinks the `klipper-mmu` package root as `src/klipper_mmu` in upstream Klipper.
- Adds the optional `osource "src/klipper_mmu/mcu/Kconfig"` inside `src/Kconfig`.
- Adds the optional `-include src/klipper_mmu/mcu/Makefile` inside `src/Makefile`.
- Declares the explicit driver API options in `mcu/Kconfig.driver_api`; the
  separate `install-driver-api` action implements them in upstream drivers.

When disabled (`CONFIG_KLIPPER_MMU` not set), no additional flags, objects, sources, or runtime hooks are linked into firmware.

When enabled (`CONFIG_KLIPPER_MMU=y`), the build includes the clock, response and
command modules plus their common-core dependencies, using include search path
`src/klipper_mmu/include` and libm. The H723 ADC reader and startup also require
`CONFIG_MACH_STM32H723` for board startup, which is compiled independently of ADC
and `CONFIG_WANT_ADC_LOCAL`. The ADC reader, portable pair core, and
acquisition-limit factory are included when `CONFIG_WANT_ADC` is set, including
with `CONFIG_WANT_ADC_LOCAL` off. In that API-off build the reader and board
analog startup return UNAVAILABLE while switched feedback still works; the limit
factory does not require `CONFIG_WANT_ADC_LOCAL`. The STEP/DIR and switch GPIO
bindings require `CONFIG_MACH_STM32H723`; pure pin/electrical configuration
factories and portable validators also build on other STM32s. The callable
board-startup composition applies saved pins. The legacy switched BoxTurtle
build has an automatic boot task; the hosted diagnostic or analog build omits
that task and starts only after host configuration and calibration delivery.

## Configuration Surface (Kconfig)

- **`CONFIG_KLIPPER_MMU`** (bool, default `n`):
  Common configuration on STM32 targets with at least 64 KiB flash, or the host
  simulator, with `CLOCK_FREQ >= 1000000`. Physical bindings remain H723-specific.
- **`CONFIG_KLIPPER_MMU_DEVICE_ID`** (int):
  - `1`: BoxTurtle (4-lane default, motorized DC rewind, switched buffer metadata)
  - `2`: NightOwl (4-lane default, passive rewind, switched buffer metadata)
  - `3`: Custom (custom multi-material composition metadata)
  - `4..25`: Planned catalogue identities, defined explicitly in
    [Kconfig.devices](Kconfig.devices). Unimplemented profiles report NOT_SUPPORTED.
- **`CONFIG_KLIPPER_MMU_BOARD_ID`** (int):
  - `1`: AFC Lite logical map (`MACH_STM32H723` only)
  - `2`: Host simulation (`MACH_SIMU` only)
  - `3`: Manual pin configuration (new STM32 default)
- **`CONFIG_KLIPPER_MMU_LANE_COUNT`** (int, range 1..8, default 4):
  Configured number of MMU lanes.
  Additional mechanism, feedback, motion and protection settings are described
  in [the control configuration contract](config.md).
- **`CONFIG_KLIPPER_MMU_LOAD_SENSORS`** (bool, default `y` for BoxTurtle on AFC
  Lite, otherwise `n`): Distinct per-lane LOAD inputs, separate from BAY/prep.
  The AFC Lite preset assigns SW7–SW10/PE8–PE11; all pins and electrical
  settings remain editable.
- **`CONFIG_KLIPPER_MMU_PROTOCOL_VERSION`** (int, default 1):
  Current integration protocol version.
- **`CONFIG_KLIPPER_MMU_INTEGRATION_VERSION`** (int, default 1):
  Current firmware integration revision.

## Exact Exported Constants

Exported to Klipper's compile-time data dictionary (`klipper.dict`):

| Constant Name | Value | Meaning |
|---|---|---|
| `KLIPPER_MMU_PROTOCOL_VERSION` | 1 | Integration protocol version |
| `KLIPPER_MMU_INTEGRATION_VERSION` | 1 | Firmware integration revision |
| `KLIPPER_MMU_ACTUATION` | 0 or 1 | Default-off build permission; 1 is experimental and still requires board readiness and hardware qualification |
| `KLIPPER_MMU_SENSOR_LOADS` | 1 | Firmware can return two additional retained LOAD-sensor capture parts; configured/valid/fresh flags still determine whether any reading is usable |
| `KLIPPER_MMU_DEVICE_ID` | 1..25 | Configured catalogue identity; separate from implementation readiness |
| `KLIPPER_MMU_BOARD_ID` | 1..3 | Configured board mapping identity |
| `KLIPPER_MMU_LANE_COUNT` | 1..8 | Configured lane count |
| `KLIPPER_MMU_CLOCK_STATE_UNAVAILABLE` | 0 | Clock uninitialized (pre-ready) |
| `KLIPPER_MMU_CLOCK_STATE_READY` | 1 | Clock tracking configured timer frequency and actively maintained |
| `KLIPPER_MMU_CLOCK_STATE_FAULTED` | 2 | Clock faulted (backward / ambiguous tick detected) |
| `KLIPPER_MMU_CLOCK_STATE_SHUTDOWN` | 3 | Firmware entered emergency stop / shutdown |

## Diagnostic Command

### Request
```text
km_clock_query
```
- Declared with `HF_IN_SHUTDOWN` so the host can query diagnostics during emergency stops and fault states.

### Response
```text
km_clock_state state=%u ms=%u us=%u device_id=%u board_id=%u lane_count=%u
```
- Parameters:
  - `state` (uint32_t): `0` (UNAVAILABLE), `1` (READY), `2` (FAULTED), `3` (SHUTDOWN).
  - `ms` (uint32_t): Coherent local milliseconds snapshot (or `0` if unavailable/faulted/shutdown).
  - `us` (uint32_t): Coherent local microseconds snapshot (or `0` if unavailable/faulted/shutdown).
  - `device_id` (uint32_t): Evaluates to `CONFIG_KLIPPER_MMU_DEVICE_ID`.
  - `board_id` (uint32_t): Evaluates to `CONFIG_KLIPPER_MMU_BOARD_ID`.
  - `lane_count` (uint32_t): Evaluates to `CONFIG_KLIPPER_MMU_LANE_COUNT`.
- Execution semantics:
  - Snapshot is taken atomically under an IRQ critical section.
  - Transmission (`sendf`) occurs strictly outside the critical section.
  - Fault or unavailable states are reported explicitly; stale or missing times are never presented as valid.
  - If this command discovers a new clock fault, upstream shutdown exits the
    command through its non-local shutdown path. The global shutdown response
    occurs first; a subsequent diagnostic query reports the latched clock fault.

## Scheduler Lifecycle & Shutdown/Restart Contract

1. **Startup Ordering**:
   - `DECL_INIT(km_clock_init)` is called early during firmware startup. Board timer hardware may not be initialized yet. Init only wakes the task via `sched_wake_task` and does not access hardware or timers.
   - `DECL_TASK(km_clock_task)` runs once the scheduler task loop is entered. The first ready task invocation establishes origin `{0, 0}` at the current `timer_read_time()` and arms a persistent 1ms maintenance timer (`sched_add_timer`).
   - Subsequent calls to `km_clock_task` are strict no-ops (`sched_check_wake` clears the wake bit). The maintenance timer is never duplicated or reset.
2. **Pre-Ready State**:
   - Calls to `km_clock_get_time()` and `km_clock_deadline_ticks()` before the task runs return `KM_ERR_UNAVAILABLE`.
   - Caller output arguments are preserved untouched; no fabricated timestamps are returned.
3. **Periodic Maintenance**:
   - A short 1ms timer (`km_maintenance_timer`) invokes `km_clock_timer_event()`.
   - The callback reads `timer_read_time()` and calls `km_mcu_clock_observe()`, keeping raw elapsed ticks far below the raw half-range horizon (< 2^31 ticks) across all frequencies (including 520MHz).
   - The callback is bounded and non-blocking, performs no `sendf`, and advances the next deadline from actual observations (`now_ticks + timer_from_us(1000)`) without replaying backlogs in a loop.
4. **Fatal Fault & Shutdown Handling**:
   - Any backward or ambiguous tick observation (`res != KM_OK`) latches `KM_CLOCK_STATE_FAULTED` and invokes upstream `try_shutdown()`.
   - `DECL_SHUTDOWN(km_clock_shutdown)` latches shutdown even before the first
     clock task. A previously latched clock fault retains its more specific state.
   - Upstream Klipper invokes `sched_timer_reset()` during shutdown. The adapter respects this ordering, returns `SF_DONE` from the timer callback, and never reschedules or re-arms timers during or after shutdown.
   - The adapter **never automatically reinitializes** after fault or shutdown.
     Clearing Klipper's global shutdown does not clear this latch. A fresh MCU
     startup is required to establish a new epoch.

The timer frequency is configured, not independently measured or calibrated.
Bounded native tests and an instrumented upstream host simulator establish
functional behavior, not physical MCU latency or oscillator accuracy.
