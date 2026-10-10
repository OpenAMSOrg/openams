# STEP/DIR output boundary

`step_dir_driver.c` implements the common actuator interface over immediate board
GPIO callbacks. It does not allocate or claim pins itself. A future MCU adapter
must claim exclusive resources, establish inactive pin levels before startup,
and supply the board's polarity, pulse and direction timing, maximum step rate,
monotonic physical clock, and actual register-controller readiness.
Readiness and fault callbacks must read cached local state in bounded time;
they must never initiate a UART transaction or block a timer callback. A hardware
fault source must remain latched until explicit recovery, as the TMC controller
does. Optional hardware fault sensing is not fabricated when absent.

## Conservative completion timestamps and callback contract

Board callbacks are serialized, non-reentrant, bounded immediate physical writes;
they never queue delayed writes, perform historical replay, spin/wait until a deadline,
or allocate memory. Sampling `now_us` after a write callback returns establishes a
conservative completion timestamp. This read reflects the earliest moment software
confirms the write has executed; it does not claim to observe the exact electrical
edge during HAL execution.

The driver records conservative completion timestamps for enable (`last_enable`),

Position accounting uses a 64-bit unwrap via unsigned difference on Klipper's 32-bit count.
Any stop, safe-off, or shutdown triggers a call to `stepper_local_stop`. The frontend
maintains readiness and fault gates on enable and clear, keeping upstream logic safe from
our hardware fault domains.

**FOLLOW.** FOLLOW repeats the same velocity target every control tick. On
the rate core that is a harmless write of the same target: there is no plan to
rebuild, so there is nothing to skip.

**Timer clock.** On whole-MHz timers (all Klipper STM32 clocks),
`km_mcu_clock_observe()` and `km_mcu_clock_deadline_ticks()` use 32-bit
division only; their `__aeabi_uldivmod` lives in the fractional-MHz branch.

**Evidence** (native and static; interrupt timing is measured at bench
qualification, not before):
- `tests/test_rate.c`: every update keeps jerk, acceleration and rate within
  their limits; moves of 1 to 200,000 steps in both directions land exactly,
  at no more than the 10 steps/s creep, with 1 ms and edge-clocked updates;
  edges land within 1 us of the core's own trajectory and at least 2 us apart.
- `tests/test_rate_crosscheck.c`: the core in lockstep with an independent
  `double` reference, and its stopping distance against the closed form
  (0.01 steps) over a grid of rates and accelerations.
- `tests/test_trajectory.c` and `tests/test_safety_contracts.c`: the API on
  the rate core, including 25,000 steps in 200 ms at 200,000 steps/s (the
  jerk-limited S-curve's exact value) with no pulse-width or interval
  violations.
- R1 static figures from `tools/masked_reach.py` on the release image: no
  floating point on the interrupt path, the same two clock divides as before
  ([controller-task-boundary-audit.md](controller-task-boundary-audit.md)).
