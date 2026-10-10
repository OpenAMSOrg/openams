# Physical simulator

This first component is a host-side one-lane filament, buffer and takeup model.
It is not linked into MCU firmware. Ideal paired DiffA/DiffB generation and a
first causal firmware harness are included. Eight initial scenarios pass with
the independently accepted iteration23 STEP/DIR correction. A first normalized
travel-capture importer, conditional fit, held-out comparison and profile-to-C
workflow is now implemented; see [measurement workflow](measurements.md).
The full simulation gate, physical ADC integration, broader fitting and validation
with real measurements remain unfinished under
the simulator plan.

The parameters are ordinary numbers with explicit units. There is no simulator
framework or copied controller. The initial test parameters are synthetic,
including 0.125 mm per step, a 10-mm hub location, 30-mm arrival location,
20-mm buffer capacity and 10-mm/s maximum takeup. They are neither BoxTurtle
measurements nor recommended physical configuration values.

## State and inputs

- Each physical feeder step advances shaft position. Direction-dependent
  transfer fractions determine actual material travel; blockage, an empty path
  or a full buffer can prevent transfer while the shaft counter still changes.
  Lost travel remains separately visible.
- Material downstream of the feeder first fills the path toward arrival, then
  fills the buffer. An optional lane-local LOAD switch can sit between feeder
  and hub. LOAD, hub and arrival switches derive from material position with
  configurable hysteresis. No successful sensor transition is scheduled by time.
- Forward feeding consumes upstream slack before drawing more from the spool.
  Reverse feeding returns material to upstream slack. Takeup removes that slack
  and returns it to the spool; it cannot pull material through the feeder.
- The default spool is unlimited. Setting `finite_spool` and a finite,
  nonnegative `spool_budget_mm` caps further forward payout after upstream
  slack is spent. With `bay_clear_mm` left at zero, BAY presence follows
  available upstream slack or remaining spool budget. A positive
  `bay_clear_mm` instead models one downstream retreat edge: the tip covers
  BAY after it passes that position, using the same hysteresis rule as the
  other switches. That edge must be greater than `switch_hysteresis_mm` and
  is unavailable together with `finite_spool`. It is synthetic EJECT geometry,
  not a measured BoxTurtle bay location. After both slack and a finite budget
  are exhausted, printer demand drains the buffer.
  If arrival was present, further demand pulls the trailing end through the
  remaining path. The optional LOAD, hub, then arrival switches clear in that
  order as the remaining length shrinks. Demand cannot draw a front that never
  reached arrival. Once the tail has passed the feeder, subsequent shaft pulses
  in either direction count as lost travel and cannot restore BAY presence or
  move downstream stock.
  This is a synthetic contiguous-filament approximation; it has no measured
  end position or feeder traction model.
- Printer demand consumes available buffer inventory once the path is filled.
  In the finite exhausted case it can also consume the remaining path stock.
  Unmet demand is recorded separately; the model never manufactures filament
  to satisfy an imposed printer rate.
- Rewind duty selects a target takeup speed. A first-order response with an
  explicit time constant models lag and coast after duty returns to zero.
  A zero time constant selects instantaneous response. Integration is analytic
  between input changes, so the response does not depend on arbitrary tick size.

Call `mmu_sim_advance` up to the next input event before applying a step, changed
duty, blockage or printer demand. It advances an unwrapped microsecond timeline.
The firmware harness also advances it through time spent in GPIO callbacks.
Only actual output events may move the model; a requested endpoint or estimated
firmware position is not an input.

The conservation check is:

```
initial material + net spool payout - printer consumption
    = material in path/buffer + upstream slack
```

Values use double precision. Tests allow 1e-9 mm numerical residual for their
bounded synthetic cases; this is not a claim about physical measurement accuracy.
Invalid parameters, nonfinite input, timestamp/step-counter overflow or numerical
overflow reject the update without changing the prior state.

## Forces at the drive gear

Optional; each parameter defaults to zero, which disables it and keeps the
kinematic behaviour above. Each STEP edge estimates speed and acceleration from
the commanded step timing (a step after a pause starts from rest). The load is
`drag_n + viscous_n_per_mm_s * |speed| + moving_kg * acceleration` along the
motion. The motor gives `motor_n` up to `corner_mm_s`, then
`motor_n * corner_mm_s / |speed|`. A step whose load exceeds that is lost
(`stalled_steps`); one whose load exceeds `grip_n` turns the shaft without
moving filament (`slipped_steps`); both add to `lost_travel_mm`.
`mmu_sim_load_ratio` (load over motor force) stands in for the driver's load
margin. The values are synthetic until fitted from bench measurements.

## Current model assumptions

This component uses one scalar for leading-front loading and, after finite BAY
exhaustion with no upstream slack and post-buffer printer draw, the remaining
length from the printer end to the trailing end.
The latter assumes a contiguous filament pulled through a fixed path after the
buffer drains; more detailed end tracking and printer retraction need later
explicit model extensions. Printer demand is nonnegative;
negative demand is rejected rather than interpreted as an implemented retraction.
There is no elastic filament, torque, temperature, measured current or electrical
driver model. Capacity/empty limits use lost travel to represent untransferred
shaft motion, without claiming a particular physical failure mechanism.

The rewind response describes upstream takeup only. It does not yet model powered
payout, tension acting through the feeder, or a spindle rotating against a taut
line. Those effects need observed evidence and focused extensions. Calibrated
travel fractions can represent measured net slip under stated conditions, but
commanded shaft steps alone cannot identify those fractions.

Profiles replace individual parameters with provenance and retain assumptions
for quantities not observed. The first fitter estimates effective forward/reverse
travel per completed physical step, conditional on the inherited shaft calibration;
it cannot independently identify slip. The tests
compare known travel/material outcomes, analytic response samples and integration
with different time partitions. Hardware qualification and validation against
held-out real captures remain separate work.

## Paired feedback

`mmu_sim_diff_sample` maps the actual modeled buffer inventory to normalized
travel and interpolates both raw channel values along an explicitly supplied
paired calibration curve. It rounds to ADC counts and applies the selected
physical channel mapping. There is no default calibration and no assumed
sum/difference or complementary-channel relationship. The generator reuses the
production calibration validator instead of duplicating its acceptance rules.

This models ideal readings only. The caller will own acquisition/delivery timing,
noise, stuck channels and dropped samples. The function does not claim freshness
or mark firmware feedback valid. Tests link the real firmware decoder and verify
literal endpoint/quarter readings, swapped channels, nonlinear calibration,
changing inventory and rejection of corrupted observations. Both a curved and a
piecewise half-reference example are synthetic fixtures, not measurements.

The baseline physical model remains usable without analog feedback, encoder or
measured current. These optional observations do not drive its material state.

From this directory:

```sh
make test
```

Compilation and test execution have explicit timeouts and enable address and
undefined-behavior sanitizers. This target tests the material model and paired
feedback generation/decoding.

## First firmware loop

`test_loop.c` connects the real operation core, trajectory, follower, STEP/DIR
driver and PWM bridge to one modeled lane. It does not copy their control laws.
Every completed enabled pulse moves the shaft model; actual material transfer
then changes the switches and buffer feedback. Requested positions never move
the plant directly. A truncated pulse is recorded as uncertain and contributes
no complete step in this model; this is not a claim that a real motor cannot
move on a truncated pulse.

The fixture currently tests these synthetic scenarios:

- Load, follow a 10-mm/s printer demand for three seconds, then unload, with
  switched feedback and with optional paired analog feedback across clock wrap.
- A lane-local LOAD switch supports PRELOAD to LOAD, PRELOAD to UNLOAD, and
  cancellation before the switch. When a profile supplies `load_mm`, the
  scenarios use that physical switch position. Otherwise this explicit
  scenario uses a synthetic half-hub position (5 mm in the baseline). Profiles
  without `load_mm` keep LOAD absent in the ordinary scenarios. Travel captures
  do not measure the switch edge; see [measurements.md](measurements.md).
  The fixture uses 200 steps/s for local PRELOAD so braking can settle before
  the hub with the 7-mm test profile.
- Graceful cancellation retains path ownership through actual braking pulses.
- A blocked feeder turns the shaft without transporting filament. Loading times
  out without a fabricated hub/arrival transition or successful completion.
- Dropped sensor acquisitions age naturally and stop the controller. Separate
  cases cover a timed-out ADC and stale analog data while switches stay fresh.
- During otherwise healthy analog FOLLOW, a completed paired ADC sample is
  replaced at delivery with (0, 0), outside the calibrated DiffA/DiffB path,
  and the contradiction persists. An identical uncorrupted run stays in FOLLOW
  under the same printer demand. The production decoder and follower report
  `KM_FAULT_BUFFER_SIGNAL` and disable both outputs within 52 ms of the first
  corrupt delivery (the follower's configured signal guard is 50 ms). The
  fixture records delivery and stop times, and checks that filament remains
  present, demand is met, and material is conserved. This is an electrical
  observation fault, not a plant jam.
- The follower issues new pulses during a short host heartbeat stall, then the
  configured lease expiry disables outputs.
- An opt-in finite spool runs out during FOLLOW. Fresh BAY-absent evidence
  reaches the real core, which reports `KM_FAULT_FILAMENT_LOST` and disables
  feeder and rewinder outputs. Conservation and invalid finite budgets have
  separate plant tests.

Encoder and measured current are absent in all these first scenarios. The
rewinder receives actual reverse PWM output; powered payout is explicitly
unsupported by this mechanical model. Ordinary unloading clears the hub while
leaving filament in the bay; neither full ejection nor complete spool takeup is
inferred from an unload event.

The plant has an unwrapped microsecond timeline. The real MCU clock bridge
converts wrapping raw timer ticks at synthetic 20-MHz and 3,333,333-Hz rates into
coherent coarse/fine time. An independent rational calculation checks each clock
observation. Motor and ADC deadlines go through its tick conversion and round
up to the plant's 1-us resolution. Raw counter wrap and fine-clock wrap are
exercised; this does not measure a physical MCU's execution costs.

Each GPIO/PWM/ADC callback advances time by a synthetic 2 us. Switch acquisition
repeats every 1 ms; delivery follows 200 us later with its original timestamp.
Analog mode runs the real paired-ADC component: each channel samples the current
plant at its first poll, holds that value during a 40-us conversion, and returns
it on a subsequent poll. A and B therefore observe different physical instants.
The frontend uses the first attempt's conservative timestamp, converted to
coarse time across wrap. Repeated cached samples keep that age. Conversion errors
invalidate feedback without inventing raw values or refreshing old timestamps.

Equal-time ordering is acquisition/ADC service/delivery, host heartbeat,
controller protection/update, then motor deadline service. Already-overdue events
use current time; the clock is never rewound to an old deadline. ADC observation
bounds describe calls to the portable component, not exact electrical acquisition
timestamps. Physical ADC registers, timer interrupts and board timing remain
outside this native harness.

Run the separate, bounded integration target:

```sh
make test-loop
```

It writes `trace.csv` in `BUILD_DIR`, with scenario, simulated time, controller
stage/fault, shaft steps, actual material/buffer/slack, printer consumption,
output states and acquisition time. ADC rows also retain raw channel data,
presence mask, acquisition result/validity, both attempt/completion bounds and
decoded-buffer validity. Absent ADC records use empty fields. `FIRMWARE_DIR` can select a frozen source
tree during concurrent implementation. The fixture uses 0.125 mm per step,
400/300 steps/s feed/retract limits and synthetic geometry from the material
tests. These are explicit test assumptions, not recommended BoxTurtle tuning.

The raw-tick import and normalized travel workflow pass 33 Python checks. Baseline and fitted
synthetic profiles each pass these eight firmware scenarios, including slip
accounting against actual completed pulses. Extra loss from capacity clipping
still fails the normal scenarios. A deliberately zero-transfer profile fails
loading, demonstrating that exported parameters affect the real plant. Run a
selected profile with `make test-profile PROFILE=/path/to/profile.json`; it uses
a separate binary and trace while preserving the baseline fixtures.

The [raw-tick importer](measurements.md#import-a-raw-tick-capture) accepts one
continuous 32-bit acquisition clock with consecutive recorder sequences and an
explicit interval bound. It preserves raw inputs and converts total elapsed
ticks without accumulated rounding drift. Separate clocks and arbitrary
instrument exports still need correlation before using this path.

Broader capture normalization/correlation, board latency/ADC/rewind fitting, real held-out
measurements, sensor noise/stuck channels, optional encoder/current comparisons
and more timing scenarios remain required. Passing this loop does not qualify
physical firmware or complete the simulator gate.
