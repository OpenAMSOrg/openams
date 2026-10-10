<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Callable Klipper STEP/DIR GPIO binding

`stepdir_gpio.c` connects the accepted portable STEP/DIR actuator driver
(`src/step_dir_driver.c`) to Klipper's STM32/H723 GPIO primitives and the shared
scheduler clock (`mcu/clock.c`). It provides a callable firmware preparation
and binding layer for up to `KM_MAX_LANES` (8) logical driver slots without
dynamic allocation. It registers no host commands, accesses no GPIO pins at boot
or during `DECL_INIT`, and leaves actuator outputs disabled.

## Callable API

```c
KM_API km_result_t km_stepdir_gpio_prepare(uint32_t slot,
                                           uint32_t step_pin,
                                           uint32_t dir_pin,
                                           uint32_t enable_pin,
                                           const km_step_dir_config_t *config,
                                           bool (*ready)(void *context),
                                           km_fault_t (*fault)(void *context),
                                           void *context,
                                           void **out_force_off_context);
KM_API km_result_t km_stepdir_gpio_activate(uint32_t slot);
KM_API void km_stepdir_gpio_force_off(void *context);
KM_API km_result_t km_stepdir_gpio_clear_fault(uint32_t slot);
KM_API km_result_t km_stepdir_gpio_setup(uint32_t slot,
                                         uint32_t step_pin,
                                         uint32_t dir_pin,
                                         uint32_t enable_pin,
                                         const km_step_dir_config_t *config,
                                         bool (*ready)(void *context),
                                         km_fault_t (*fault)(void *context),
                                         void *context);

KM_API km_driver_t *km_stepdir_gpio_get_driver(uint32_t slot);
KM_API bool km_stepdir_gpio_is_slot_active(uint32_t slot);
KM_API bool km_stepdir_gpio_is_shutdown(void);
KM_API km_result_t km_stepdir_gpio_get_slot_pins(uint32_t slot,
                                                 uint32_t *out_step_pin,
                                                 uint32_t *out_dir_pin,
                                                 uint32_t *out_enable_pin);
void km_stepdir_gpio_shutdown(void);
```

`prepare` checks task context, clock, configuration and pins before permanently
claiming the slot. It sets inactive ENABLE, STEP and DIR outputs, then returns a
stable opaque context for `km_stepdir_gpio_force_off`. It does not query the
asynchronous ready callback, publish a driver, enable an actuator or poll. The
callback drives only inactive ENABLE, reads no clock and invokes no portable
driver method. The UART binding may use it under its serialized callback
contract.

After publication, raw safe-off latches a driver fault without changing its
logical enabled or high-STEP state. The next local driver service must observe
that fault and stop outputs. An interrupted high STEP must not be counted as a
completed pulse, and motion must not resume automatically. `activate` returns
`KM_ERR_UNAVAILABLE` while readiness is false without consuming the prepared
slot. A pre-initialization fault also leaves it prepared. Once ready, activation
initializes and publishes the portable driver exactly once. A begun failed
initialization is not retried; global shutdown prevents activation. Legacy
`setup` directly reuses prepare and activate, preserving immediate behavior.

`clear_fault` is the explicit recovery of that latch after the driver behind
the callbacks has been restarted. It runs the portable STEP/DIR clear under
the same IRQ serialization as raw safe-off. It refuses `KM_ERR_BUSY` while
ENABLE or STEP is logically active, so the controller's stop must already
have driven both inactive and recorded the interrupted pulse's fall. It
refuses `KM_ERR_UNAVAILABLE` while ready is false or the slot is unpublished,
and `KM_ERR_FAULTED` while the fault callback reports a fault or in global
shutdown. Success rebases uncertain edge timing from a fresh clock and writes
no output; enabling again is a separate driver request. A later raw safe-off
latches again. Global shutdown stays permanent.

## Setup guarantees and preparation lifecycle

1. **Preconditions**: `km_stepdir_gpio_setup()` must be called explicitly from ready
   task context with interrupts enabled (`irq_save() == 0`) and the shared scheduler
   clock ready (`km_clock_get_state() == KM_CLOCK_STATE_READY`). Calling with IRQs
   disabled returns `KM_ERR_INVALID_PARAM`; calling before clock readiness returns
   `KM_ERR_UNAVAILABLE`.
2. **Board mapper ownership**: The caller still owes full resource admission. This
   binding does not reserve GPIO or claim electrical qualification. With
   `CONFIG_WANT_GPIO_LOCAL`, STEP, DIR and ENABLE must already be locally reserved
   before the slot is consumed; a missing, ordinary or partial claim returns
   `KM_ERR_CONFLICT` before the slot is claimed or the force-off context is
   published. Callable [`km_board_startup_begin`](board_startup.md), when
   `CONFIG_WANT_GPIO_LOCAL` is enabled, reserves precisely the expected MMU plan
   `KM_RESOURCE_PIN` entries in one `gpio_local_reserve` batch after every
   validation and core init and before switch, ADC, feeder, or PWM setup. It does
   not reserve external complete-plan entries or non-PIN timer/ADC claims. It is
   not invoked on boot or by host commands. The automatic caller, command
   attachment, and true external MCU admission remain missing. The caller must
   supply a genuinely complete MCU plan and enforce host, transport, crystal, and
   other external owners and the actuation gate. The batch is not global inventory
   discovery and does not by itself admit shared timers, ADC, or transport. It does
   not hardware-qualify arbitrary boards or profiles. `ACTUATION=0` remains
   unchanged.
3. **Collision and parameter rejection**: The binding rejects invalid slot indices
   (`slot >= KM_MAX_LANES`), NULL configs, NULL ready callbacks, equal pins within
   a slot (`step == dir`, `step == enable`, `dir == enable`), and invalid portable
   timing parameters (`pulse_high_us == 0`, `maximum_velocity_steps_s > 1000000 / cycle`)
   with `KM_ERR_INVALID_PARAM` before touching any
   hardware GPIO.
4. **Slot and pin immutability**: Re-setup of an already claimed slot returns `KM_ERR_BUSY`.
   Pin collisions with any slot already claimed (including partial or failed setups)
   return `KM_ERR_CONFLICT` before touching GPIO. The same code is returned when
   `CONFIG_WANT_GPIO_LOCAL` is set and any of the three pins lacks a prior local claim;
   the slot stays reusable and no output context is published. Once setup begins, pin assignments
   are permanently immutable; on setup failure, claimed pins remain unavailable for
   reuse until MCU reset.
5. **Physical setup ordering**: Hardware GPIO configuration executes in strict sequence:
   inactive ENABLE first, inactive STEP next, then initial direction. Upstream
   `gpio_out_setup()` (`gpio_out_setup_local()` when `CONFIG_WANT_GPIO_LOCAL` is set) establishes the initial physical level before enabling output
   mode, guaranteeing no positive enable or step transition occurs during preparation or setup.
6. **Partial setup safety**: Setup flags (`enable_setup_done`, `step_setup_done`,
   `dir_setup_done`) track initialization progress. If upstream shutdown occurs during
   GPIO configuration, every touched actuator pin is left disabled by the shutdown
   hook. A prepared or partially initialized driver is never published.

## Serialization and execution contract

- Setup executes with interrupts enabled.
- The controller's edge interrupt steps the driver while the control task may
  enable or stop it, and the portable driver is a single-threaded state
  machine. Activation therefore gives the published driver a copy of the
  portable operation table whose `set_enable` and `stop_outputs` each run in one
  short `irq_save()` section (a few GPIO writes and clock reads). STEP and DIR
  edges already run masked in the timer interrupt; `clear_fault` runs under
  `km_stepdir_gpio_clear_fault()`'s lock.
- The binding exposes that portable `km_driver_t` instance via
  `km_stepdir_gpio_get_driver()`; there is no further driver layer.

## Callback and clock contract

- Immediate GPIO writes: Callbacks invoke `gpio_out_write()` directly with explicit
  polarity mapping from configuration. No host-planned pulse queues or delayed writes
  are introduced.
- Conservative physical completion observation: `slot_now_us` reads the real shared scheduler
  clock via `km_clock_get_completion_us()`, which atomically obtains a fresh hardware timer
  observation and computes the conservative upper completion bound (ceiling). When the raw
  timer tick falls between microsecond boundaries (`frac_num > 0`), the bound rounds up to the
  future microsecond edge. This ensures that when the driver records an output timestamp, the
  physical signal has genuinely completed before that microsecond label can be reached by any
  subsequent check.
  - Sub-microsecond guard cost: At fractional rates (e.g. 12.288 MHz, 520 MHz), the ceiling
    rounding introduces strictly `< 1 us` of conservative guard time (at most one fractional
    microsecond interval). For exact integer frequencies (e.g. 1 MHz where tick residue is
    zero), the bound is exact and incurs `0 us` guard cost. This completion-rounding guard
    (< 1 us ceiling on microsecond timestamps) is distinct from raw timer tick quantization
    and from electrical/physical hardware qualification. The guard guarantees that subsequent
    physical minimum intervals (pulse-high, pulse-low, direction setup/hold) measured in
    microseconds do not issue edges before physical completion, while real observed floor time
    remains the basis for trajectory model integration and command ordering.
- Clock fault handling: On clock observation failure or pre-ready access, `slot_now_us`
  invokes Klipper's non-returning `shutdown("klipper_mmu: clock unavailable")`. No fabricated
  timestamps or non-advancing sentinel values (`last_call - 1u`) are returned to the caller.
  The registered raw-GPIO shutdown hook safely forces all touched actuator pins to inactive
  levels without reading the clock.
- Readiness and fault propagation: The supplied `ready` callback reflects local driver
  state (e.g. TMC register configuration). Loss of readiness suppresses outputs and
  trips the driver through the portable contract. Optional current measurement is
  neither advertised nor fabricated.

## Emergency shutdown guarantees

- Dedicated hook: `DECL_SHUTDOWN(km_stepdir_gpio_shutdown)` executes during Klipper
  emergency shutdown.
- Physical shutdown ordering: For every touched slot, raw `gpio_out_write()`
  unconditionally drives inactive ENABLE before inactive STEP.
- Elimination of recursion hazard: The shutdown hook performs no clock reads, no
  readiness callbacks, no UART transactions, no delays, and no dynamic memory
  allocations. It intentionally does not call portable `stop_outputs()`,
  avoiding the recursion hazard where `stop_outputs()` calls
  `now_us()` when a step pulse was active.
- State consistency: Public driver state is updated to `enabled = false`,
  `step_pin_high = false`, `pwm_duty_permille = 0`, and `fault = KM_FAULT_DRIVER_FAULT`.
- Permanent latch: Global shutdown stays latched even before setup and after upstream
  `sched_clear_shutdown()`. No re-enable, re-initialization, or automatic resume is
  permitted until MCU reset. Ordinary portable stop/fault recovery remains available
  only while the global latch remains clear.

## Verification evidence

- Native integration suite: `tests/klipper_stepdir/` compiles the binding, production
  `mcu/clock.c`, `src/mcu_clock.c`, and `src/step_dir_driver.c` under strict C11 warnings
  (`-std=c11 -Wall -Wextra -Werror -pedantic -Wconversion -Wsign-conversion`) with
  AddressSanitizer and UndefinedBehaviorSanitizer (`-fno-sanitize-recover=all`).
- Isolated process execution: 36 distinct test scenarios execute in isolated child
  processes via `fork()` with a 15-second per-scenario timeout to verify clean static state:
  1. `test_no_boot_gpio`: Verifies zero GPIO access during boot / `DECL_INIT`.
  2. `test_pre_ready_clock_rejection_boundary`: Verifies rejection before clock task.
  3. `test_irq_disabled_rejection_boundary`: Verifies rejection when IRQs are disabled.
  4. `test_invalid_config_rejection_boundary`: Verifies boundary validation of timing parameters.
  5. `test_inactive_levels_and_polarities`: Verifies all 8 polarity combinations maintain inactive levels.
  6. `test_duplicate_and_cross_slot_pin_rejection_boundary`: Verifies pin collision rejection without GPIO writes.
  7. `test_partial_setup_nonreturning_failure`: Verifies touched pins are disabled if GPIO setup faults.
  8. `test_per_slot_isolation`: Verifies independent control across slots.
  9. `test_readiness_suppression_and_loss`: Verifies output suppression on loss of readiness.
  10. `test_normal_timed_pulses_physical_edges`: Verifies physical step pulses match configured microsecond widths.
  11. `test_12mhz_timed_pulse`: Verifies physical pulse timing under integral tick clock rates (12 MHz, 12 ticks/us).
  12. `test_clock_wrap_physical_pulses`: Verifies correct physical stepping across 32-bit tick counter wrap.
  13. `test_delayed_write_budget_failure_boundary`: Verifies budget boundary (`<= limit` passes, `> limit` faults).
  14. `test_enable_before_step_shutdown_ordering_active_pulse`: Verifies physical ENABLE is driven inactive before STEP during shutdown of an active pulse.
  15. `test_clock_fault_inside_gpio_callback_nonreturning`: Verifies non-returning shutdown on clock fault inside write.
  16. `test_no_recursive_clock_calls_during_shutdown`: Asserts zero clock reads occur during shutdown.
  17. `test_no_resume_after_clear_shutdown`: Verifies shutdown remains latched after `clear_shutdown`.
  18. `test_slot_pins_query`: Verifies canonical pin query interface.
  19. `test_output_bound_accessor_lifecycle_and_coherence`: Verifies standalone and global completion bound accessors, lifecycle validation, and coarse/fine coherence.
  20. `test_reproduced_six_minima_too_early_rejection`: Reproduces the six reproduced Iteration 27 timing intervals and verifies that calls presented earlier than the conservative physical completion bound are rejected with `KM_ERR_FAULTED`.
  21. `test_reproduced_six_minima_conservative_success`: Verifies all six physical intervals succeed when scheduled with conservative deadlines, and asserts raw tick counts strictly satisfy physical microsecond durations.
  22. `test_before_exact_after_physical_bounds`: Boundary sweep verifying rejection 1 tick before deadline, success at exact deadline, and success after deadline.
  23. `test_1mhz_exact_zero_residue_timing`: Verifies 1 MHz zero-residue clock has exact equality between floor and completion times with 0 us overhead.
  24. `test_520mhz_fractional_phase_and_callback_latency`: Verifies 520 MHz production clock timing with sub-microsecond callback latency and fractional phase.
  25. `test_clock_and_fine_us_wraparound`: Verifies 32-bit hardware timer wrap at 520 MHz and 32-bit fine-microsecond rollover with strict coarse/fine coherence.
  26. `test_callback_clock_failure_and_latency`: Verifies that callback execution latency is reflected in completion timestamps and that mid-callback clock failure triggers non-returning shutdown.
  27. `test_real_trajectory_with_conservative_completion`: Executes continuous trajectory motion driving the GPIO binding with nonzero callback cost under IRQ serialization, commanding smooth deceleration to stop and verifying complete settlement with exact pulse and position accounting and zero driver faults.
  28. `test_immediate_retarget_and_cancellation_1mhz`: Verifies immediate retargeting and graceful cancellation after falling STEP edges and direction changes under exact integer clock (1 MHz) without rejecting valid commands or violating physical minima.
  29. `test_immediate_retarget_and_cancellation_12_288mhz`: Verifies immediate retargeting and graceful cancellation after falling STEP edges and direction changes under fractional clock (12.288 MHz) with ceiling completion bounds separated from observed trajectory time.
  30. `test_immediate_retarget_and_cancellation_520mhz`: Verifies immediate retargeting and graceful cancellation after falling STEP edges and direction changes under high-frequency fractional phase (520 MHz).
  31. `test_stale_command_rejection_and_missed_deadline_fault`: Verifies that genuinely stale timestamps are strictly rejected with `KM_ERR_INVALID_PARAM` and that missed deadlines exceeding maximum lateness trigger emergency stop and driver shutdown.
  32. `test_forward_reverse_motion_wrap_and_settle`: Verifies bidirectional motion across 32-bit hardware timer wrap at 520 MHz, settling smoothly to idle with exact pulse accounting.
  33. `test_prepare_delayed_activation`: Verifies no early publication, repeated prepare rejection, pin collision, delayed readiness, fault gating and one-time publication.
  34. `test_prepare_validation_and_raw_off`: Verifies preclaim rejection, raw safe-off during high STEP, fault latching and shutdown without safe-off clock reads.
  35. `test_raw_off_high_step_not_counted`: Verifies a high STEP interrupted by raw safe-off faults the local trajectory, preserves position uncertainty and does not count a completed pulse.
  36. `test_clear_fault_after_raw_off`: Verifies the explicit latch clear refuses while ENABLE or STEP is active, unpublished, not ready or faulted, clears without output writes once stopped and ready, lets a later pulse honor the recorded fall, relatches on the next raw safe-off, and stays refused after global shutdown.
- Upstream compilation: Verified clean ARM Cortex-M7 cross-compilation (`arm-none-eabi-gcc`)
  against upstream Klipper STM32H723 headers with `-std=gnu11 -Wall -Wextra -Werror -Wconversion -Wsign-conversion`.
- acceptance in iteration29 additionally checks the unchanged immediate-retarget
  reproduction at three clock rates, 216 physical-boundary cases, and 18 bidirectional
  compositions with independent raw-edge minima and pulse accounting. The binding is
  included only in enabled H723 builds; three H723 configurations rebuild successfully.
  A disposable command fixture retains the callable setup/motion path in a linked
  H723 image. The fixture is neither installed nor flashed. See the
  review evidence.

## Remaining integration boundaries

This slice implements only the callable GPIO binding and preparation layer. It does not
implement or claim:
- Trajectory timer or scheduler timer dispatch loop.
- Automatic invocation, host-command attachment, or external MCU admission. Callable composition in [`board_startup.md`](board_startup.md) reserves only the expected MMU pin batch when `CONFIG_WANT_GPIO_LOCAL` is enabled and attaches the controller only after readiness.
- TMC UART register configuration or communication lifecycle.
- Electrical or physical hardware safety qualification (tests run in native simulation against synthetic clocks and shims; no physical hardware was accessed or exercised).
- End-to-end MMU motion or release of actuation (`ACTUATION=0` unchanged).
