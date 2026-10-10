# Motion core: one rate state for steppers and BLDC motors

Status: the core is implemented and behind `km_trajectory_*` in every image;
native and Klipper-built tests pass; nothing is qualified on hardware; the R1
static figures are re-measured separately with `tools/masked_reach.py`.

## Idea

A stepper is a synchronous motor whose commutation is quantized into
microsteps; a BLDC motor is commutated continuously. Both are a **phase
advanced by one jerk-limited rate state**. The shared core owns the rate state
and its limits; a motor adapter only decides *when* the state is advanced and
*what* the phase drives. Targets are written, never planned; the state bends toward them at the jerk
limit, so acceleration is continuous and there is no replanning, no phase list
and no plan hand-over.

## Shared core (`src/rate.c`, `include/klipper_mmu/rate.h`)

State, integer fixed point only (it runs in the edge interrupt):

- `phase`: position in microsteps with a sub-step fraction; range at least
  `KM_MOTION_{MIN,MAX}_POSITION_STEPS` (±1e12), so whole steps and fraction are
  kept separately rather than one Q32.32 `int64_t`.
- `rate`, `accel`: current speed and acceleration, signed, with enough fraction
  that integrating 1 µs steps from rest to `vmax` and back loses less than one
  microstep.
- Targets: `target_rate`, and an optional `target_phase` (whole microsteps).
- Limits: `vmax`, `amax` (speeding up), `dmax` (slowing down), `jmax`.

Operations, all integer, bounded, allocation-free, no division except by
constants the compiler turns into multiply/shift:

- `km_rate_advance(r, dt)`: move `accel` toward the desired acceleration by at
  most `jmax·dt`, then integrate `rate` and `phase` exactly for that
  (constant-jerk) interval. The desired acceleration is `±amax` toward
  `target_rate` (`±dmax` when the speed magnitude falls), and 0 once the
  remaining rate change fits in `accel²/(2·jmax)`: the state arrives at the
  target rate with zero acceleration.
- Position target: the jerk-limited **stopping distance** has a closed form
  (reduce acceleration to `-dmax` at `jmax`, hold, release at `jmax`; the
  triangular form when the speed runs out first), in integer fixed point with
  normalized reciprocals precomputed by `km_rate_set_limits`, saturating
  rather than overflowing. The direction is always toward `target_phase`,
  with the target rate's magnitude as the speed cap. Each update predicts the
  state one update ahead at the cruise target; if the distance left then
  reaches that state's stopping distance, braking **latches** (target rate 0,
  not re-decided every update, which chattered). With a fixed target the exact
  rate lookahead ends at rate 0 and acceleration 0, short by at most about one
  update's travel. More than one step short starts a new attempt; within one
  step the core creeps at 10 steps/s and lands exactly on the target. Rate and
  phase never move away from the target (no DIR reversal).
- `km_rate_set_target_rate`, `km_rate_set_target_phase`, `km_rate_set_limits`:
  plain writes. A new target never resets rate or acceleration.
- `km_rate_stop(r, emergency)`: target rate 0 at `dmax`; the emergency form
  (fault, lease expiry) is an immediate output stop handled by the adapter.

## Stepper adapter (replaces the step image in `src/trajectory.c`)

The STEP edge is the clock, bounded to `KM_RATE_MAX_UPDATE_US` (1 ms): the
interrupt fires at the next microstep boundary or after 1 ms, whichever is
sooner, and advances the rate state by the elapsed time. The microstep the
driver is at (`model_position_steps`) is state, not derived from the phase: a
step is due when the phase reaches the next microstep exactly, in either
direction (`steps_due()`), so a reversal inside a microstep emits nothing. The
phase is not pinned to the boundary: pinning discarded the up-to-1-us overshoot
at each edge and ran 12% slow at 200000 steps/s (25000 steps expected in 200
ms, 22024 measured); the exact-reach rule keeps pulses one per crossing. More
than one step due at an edge is a missed-pulse fault. The bound matters at low
speed, where an edge interval can outlast a whole jerk phase. The next boundary
time is refined by up to three integer Newton steps; from rest it follows from
the jerk-limited start in closed form. While the motor is stopped nothing runs;
a target write re-arms the timer. Direction changes keep the existing DIR
setup/hold timing. Existing protections stay: minimum pulse width, lateness
bound (`KM_MOTION_MAX_LATENESS_US`), missed-pulse fault, outputs off on any
failure.

The control task no longer hands over plans (R1's seek/adopt/collect and the
three-step adoption window go away). Commands on the task's copy increment
`command_seq`; the exchange publishes (`km_trajectory_publish(edge, plan,
now)`) only the command part (targets, limits, jerk), keeps the edge's newer
rate state and output, re-validates a position target against the edge's state
(a target now inside the stopping distance stops the edge with a fault, and the
exchange stops the core), and serves the command at once.
`km_trajectory_collect` copies the edge's state and output back, with output
still due collected as `KM_STEP_STATE_PENDING`. When the edge was idle at the
previous exchange, the whole copy is published, as before. The motion lease
stays: if no control cycle refreshes it in time, the interrupt stops the
outputs and latches the fault, as today.

## BLDC adapter

The commutation or PWM interrupt calls `km_rate_advance` every period (or a
slower ramp tick) and drives the electrical angle from `phase`. Not built in
this change: klipper-mmu has no BLDC board yet; the core is written and tested
so that a BLDC board can adopt it.

## Measured phase (optional capability)

`phase` is the commanded electrical position. A board that can measure the
rotor (an encoder, or a Hall/observer estimate) supplies it as a second
input; without one the core stays open loop. With it, the adapter keeps the
commanded phase at most a configured lead ahead of the measured one (for a
stepper at most one full step, 90° electrical), so a stepper cannot lose
position, and a board that drives its own H-bridges with current feedback can
apply commutation with phase advance to either motor type. The
TMC2209 STEP/DIR adapter has neither input and does not use this.

## API kept for callers

`km_trajectory_*` keeps its role for `operation_core.c`, `selector_home.c`,
`mcu/controller.c`, `mcu/commands.c` and `mcu/switch_gpio.c`:
`set_target_velocity` becomes a rate target, `set_target_position` a phase
target with its speed cap, `is_moving`, `get_position`, `get_velocity`,
`emergency_stop`, `recover`, `cancel`, `set_limits` keep their meaning. Anything
that only existed to exchange plans is removed rather than emulated. Callers
must expect consequences from jerk limiting: from speed v with braking
acceleration D and jerk J a stop takes v/D + D/J (v >= D^2/J) or 2*sqrt(v/J)
(about 77 ms from 300 steps/s at J = 200000, where a trapezoid took about 20 ms);
the first microstep from rest comes after (6/J)^(1/3) (31 ms at 200000). A
position move ends at 10 steps/s creep within its last microstep, at most about 100 ms.

Jerk: `KLIPPER_MMU_JERK_MM_S3` (`mcu/Kconfig.control`), in mm/s³, 0 = automatic:
20 x the lower of the acceleration and braking acceleration (reached in 50 ms;
300 mm/s³, i.e. 218180 microsteps/s³, at the BoxTurtle defaults of 15 mm/s²
acceleration and 22 mm/s² braking), applied through the profile field
`default_jerk_steps_s3` and `km_trajectory_set_jerk`. The menu's mm/s³ value is
converted with the same feeder mechanics as the speeds.

## Evidence required

Native tests (`tests/test_rate.c`, and `tests/test_rate_crosscheck.c` against an
independent `double` reference written by a local model):

- Rate: acceleration never changes faster than `jmax`, never exceeds
  `amax`/`dmax`; the rate reaches each target with zero acceleration; retargets
  mid-ramp (up, down, reversal) stay jerk-limited.
- Position: stops exactly on the target microstep, zero rate, no overshoot, for
  short moves that never reach `vmax`, long moves, moves retargeted mid-way
  (longer, shorter than the stopping distance: a target inside the stopping
  distance is refused, as today), and both directions.
- Rate lockstep: 5000 1 ms updates to 18000 steps/s match the reference within
  one microstep and 0.01% of the limits.
- Stopping distance over a grid of rates and accelerations: within 0.01 steps
  of the closed form; the reference simulation agrees with the closed form
  within its own 0.1 ms sampling error (worst 0.093 steps).
- Stepper edges: each edge lands within 1 µs of the core's own trajectory
  (edge times are whole microseconds); edge count equals the position change;
  edges at least 2 µs apart. Lateness and missed-pulse faults belong to the
  adapter and are tested there.
- Existing controller, board, composed FPS, host-join and release-matrix tests
  pass with the new core; the masked-callback static figures (R1) are
  re-measured and contain no float or runtime division.
