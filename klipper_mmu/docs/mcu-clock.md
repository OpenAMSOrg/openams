<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# MCU clock bridge

The portable clock bridge converts a wrapping 32-bit hardware tick counter into
`km_core_timepoint_t {ms, us}` and converts local microsecond deadlines back to
raw ticks. It is allocation-free integer arithmetic. It neither samples hardware,
arms an interrupt nor switches an output. The MCU adapter owns those actions and
must serialize all accesses to each clock instance.

## Origin and observations

Initialize with the **actual timer frequency**, not an assumed CPU frequency.
Every integer rate from 1,000,000 through UINT32_MAX Hz is supported, including
non-integral MHz. The lower bound ensures a legal observation gap in ticks also
fits the common controller's microsecond horizon. Example test frequencies are
conversion inputs, not qualified physical board clocks.

The supplied origin describes one conceptual local microsecond count `T`:

- `origin.ms = floor(T / 1000) mod 2^32`
- `origin.us = T mod 2^32`

The unsigned difference `origin.us - origin.ms * 1000` must be below 1000.
Initialization places that origin at `initial_ticks`, with zero fractional
microsecond phase; the initial raw counter value is not elapsed time.

Each observation takes one new raw tick value. Its unsigned delta from the last
accepted tick must be strictly below 2^31. Zero delta is valid. A backward or
half-range-ambiguous observation latches a clock fault without advancing the last
accepted tick/time or changing caller output. Later valid-looking samples do not
clear the latch. The adapter must immediately stop outputs on this fault.

A complete unseen raw counter cycle is indistinguishable from zero elapsed time.
The caller must sample within the horizon during active, idle and fault states:

| Timer frequency | Required interval between raw observations |
| --- | --- |
| 1 MHz | Less than 2147.484 seconds |
| 2 MHz | Less than 1073.742 seconds |
| 20 MHz | Less than 107.374 seconds |
| 520 MHz | Less than 4.130 seconds |
| UINT32_MAX Hz | Less than approximately 0.500 seconds |

Rounded values describe scale only; the exact bound is `delta_ticks < 2^31`.
These are modular interpretation limits, not acceptable motor or protection
latencies. Real output and sensor deadlines require much shorter service.

## Fractional phase and both time counters

The implementation keeps fractional numerator `R` in units of `1 / clock_hz`
microseconds, with `0 <= R < clock_hz`. Fractional ticks matter at integral-MHz
frequencies as well as non-integral-MHz frequencies. For each accepted delta:

```text
N          = R + delta_ticks * 1,000,000
elapsed_us = floor(N / clock_hz)
R          = N mod clock_hz
```

The millisecond counter advances from elapsed whole microseconds plus a retained
0..999 microsecond phase. It is never obtained by dividing the wrapping `us`
field by 1000. Both counters therefore remain coherent across their wraps.

Combining the quotient/remainder identities for consecutive observations gives
`R_initial + total_ticks * 1,000,000 = total_elapsed_us * clock_hz + R_final`.
Thus observation chunking causes no cumulative rounding drift. This compares
mathematical totals; combining chunks into one API observation is allowed only
when the combined raw delta remains below half range.

For observation, `N <= (2^32 - 1) + (2^31 - 1) * 1,000,000 < 2^51`.
`elapsed_us <= delta_ticks < 2^31`, and adding the 0..999 phase fits uint32_t.
Only bounded residues and wrapping counters persist; no ever-growing elapsed
counter can silently overflow. The deadline expression below has its own wider
bound and must not be truncated to this observation bound.

## Deadline conversion

Conversion is a pure query at the last accepted observation. Refresh the clock
inside the serialized dispatch boundary before relying on it. For unsigned
`D = deadline_us - now.us`:

- `D == 0` or `D > 2^31`: the deadline is due within the caller's modular-time
  contract. Return the last observed raw tick and `out_due = true`.
- `D == 2^31`: reject the ambiguous deadline.
- `0 < D < 2^31`: calculate the earliest raw tick reaching the deadline:

```text
required_ticks = ceil((D * clock_hz - R) / 1,000,000)
target_ticks   = last_ticks + required_ticks (mod 2^32)
```

`D * clock_hz - R` is positive and below 2^63; adding 999,999 for ceiling division
still fits uint64_t. Return a future tick only when `required_ticks < 2^31`.
A farther hardware deadline returns `KM_ERR_INVALID_PARAM`, preserves both
outputs and leaves the clock healthy. The scheduler may choose an intermediate
wakeup and convert again; this helper never silently clamps the deadline.

At the returned future tick the integer-microsecond deadline has been reached;
at the immediately preceding tick it has not. The retained fraction matters:
two snapshots with the same integer `us` can have different next deadline ticks.

An overdue deadline remains overdue. Service it using actual current time and
retain the trajectory's original deadline for its normal lateness checks. Do not
turn it into a fresh future edge. A far-future deadline beyond the common horizon
cannot be distinguished from an old deadline using one wrapping microsecond
value; callers must keep deadline interpretation within that horizon.

## Results, initialization and recovery

For observe, get-time and deadline queries, result precedence is:

1. Null required pointers: `KM_ERR_INVALID_PARAM`.
2. Zero-initialized or invalid-init state: `KM_ERR_UNAVAILABLE`.
3. Latched clock fault: `KM_ERR_FAULTED`.
4. Operation-specific observation or deadline checks.

All errors preserve output parameters. Null argument and deadline range errors
do not advance or fault a healthy clock. An ambiguous observation does latch a
fault. `get_time` returns a coherent snapshot, not a new hardware observation or
a physical acquisition timestamp. Outputs must be independent caller storage;
clock fields are private and must not be mutated through output pointers.

Initialization is different from a query: invalid non-null initialization clears
the instance to inert zero state; it does not read prior storage. Other APIs may
be used only after zero-initialization or an init call, not on arbitrary
uninitialized memory. Init and re-init are fresh/quiescent operations. Stop all
actuators and reconcile/reinitialize dependent consumers before establishing a
new epoch. Reinitialization must never mask a clock fault during live motion.

## Integration and verification

The adapter must obtain timestamps at serialized command dispatch and after
physical output changes, rather than reuse stale cached time. It must preserve
the same epoch for the operation core, trajectory and dated sensor inputs.
Actual sensor acquisition time remains the sensor adapter's responsibility.

Native tests compose load and cancellation with the real operation core and
timed feeder. Independent tests compare exact rational reference traces,
exercise complete fine-counter cycles with legal service gaps, check future
rounding and fractional boundary changes, and verify that overdue trajectory
service still faults. Testing spans ordinary and synthetic boundary frequencies.
These establish conversion behavior, not MCU execution cost, interrupt latency,
GPIO timing or physical board qualification. The actual MCU adapter remains
separate integration work.
