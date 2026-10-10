# Bringing measured travel into the simulator

`measurements.py` imports normalized travel captures, fits forward and reverse
filament travel per completed motor step, checks a separate run, and produces a
versioned plant profile. `make test-profile` uses that profile in the existing
C plant and production-firmware loop. It does not change firmware tuning.
The synthetic acceptance test also exports a fitted profile into the C plant,
replays completed forward/reverse steps, checks material conservation, and
rejects a held-out trace with too few reverse observations. This verifies the
conversion path without representing the fixture as hardware data.

This is the first measurement workflow. The raw-tick importer below reconstructs
one continuously sampled clock with an explicit interval bound. Board latency,
rewind dynamics, paired ADC curves, geometry fitting, arbitrary instrument formats
and correlation between separate clocks remain separate work.
All checked-in example captures are **synthetic**. No physical
measurements or hardware qualification have occurred.

Schema v1 profiles may include `parameters.load_mm` and a matching nonempty
`parameter_sources.load_mm`. Missing or zero means the LOAD switch is absent;
legacy profiles remain valid. A positive value is the physical switch position
in millimeters from the feeder reference, strictly beyond the hysteresis band
and with `load_mm + 2 * switch_hysteresis_mm < hub_mm`. Measure it from an
actual LOAD switch edge with a stated source. The travel captures below only
measure filament displacement at the feeder exit and cannot establish that
edge. Travel fitting preserves an existing LOAD value and source; it never
infers one. Export emits `.load_mm = 0` for legacy profiles and the supplied
value for profiles with a measured switch position.

Profiles may also include `board_timing.switch_delivery_us` (integer 0–999)
with a nonempty `board_timing_sources.switch_delivery_us` provenance label.
Legacy profiles use the synthetic 200 µs delivery delay; this timing value is
kept separate from the mechanism parameters and is not a hardware measurement.

## Import a raw tick capture

`normalize_travel.py` accepts a flat export of positions and completed physical
pulse counters sampled together on a continuous 32-bit clock. It preserves the
raw files, unwraps ticks under an explicit bound, and validates the resulting
capture with the same parser used by the fitter. Run it separately for training
and held-out data:

```sh
timeout 30s python3 -B normalize_travel.py /path/to/raw-run /path/to/new-normalized-run
```

The raw directory contains `metadata.json` with the capture/measurement fields
below, and `travel.csv` with this exact header:

```csv
sequence,ticks,forward_steps,reverse_steps,filament_mm,valid,saturated
```

`sequence` is an unwrapped uint64 recorder sequence, increasing by exactly one
per row. `ticks` is the raw uint32 acquisition timestamp. Physical forward and
reverse pulse counters are already unwrapped uint64 values; this tool does not
reconstruct pulse counts from motion requests. Position/validity fields have
the same meaning as in normalized captures.

The raw metadata's `clock` object replaces the normalized clock fields with:

```json
{
  "source": "describe the acquisition clock and its calibration",
  "units": "ticks",
  "counter_bits": 32,
  "frequency_hz": 3333333,
  "resolution_ticks": 1,
  "maximum_interval_ticks": 1000000,
  "continuous_clock": true,
  "interval_bound_evidence": "describe the independent sample-spacing guarantee",
  "synchronization": "describe how pulse and position observations share this clock",
  "alignment_error_us": 0.25
}
```

These numbers illustrate the schema; they are not board calibration values.
`maximum_interval_ticks` must be positive and below 2^31, and every observed
modular interval must lie between the stated resolution and this maximum.
Sequence gaps, out-of-bound intervals, duplicate timestamps, resets declared in
metadata and dropped records are rejected. A declared interval bound is a
capture precondition: wrapped counters alone cannot expose an undisclosed
whole wrap, a reset that mimics valid progression, or a recorder that renumbers
missing observations. Split captures at clock resets and justify continuity
and the interval bound using the acquisition method.

The first sample defines time zero. The importer converts **total** elapsed ticks
with exact integer arithmetic, rounding down once per observation; it never
accumulates individually rounded intervals. The output records the full raw
clock description, initial counter/sequence, observed wraps, elapsed ticks and
input hashes. `raw/metadata.json` and `raw/travel.csv` are byte-preserved copies.
The normalized clock records an additional conservative 1-us alignment bound
for integer timestamp quantization; its spacing resolution is the raw tick
resolution converted downward with a 1-us minimum. This does not calibrate
frequency or remove oscillator drift. Input spacing that collapses to the same
microsecond is rejected rather than silently dropping records.

All schema and fitting-sample checks run before creating the destination, which
must not exist. The importer is travel-specific and requires the fitter's three
usable intervals per direction. Invalid/saturated observations remain present;
the fitter excludes their adjacent intervals. Input files and existing outputs
are never replaced. An I/O failure may leave a new incomplete destination; it
is not a successful capture and is never reported as one.

The normalized output can be passed directly to `fit-travel` below. Only a
separate held-out run validates a fit; importing a trace does not validate the
mechanics or qualify hardware.

## Capture format

Each capture directory contains `metadata.json` and `travel.csv`. Preserve raw
instrument files beside them; the importer never modifies its inputs. Convert
and correlate the original observations into one unwrapped microsecond timeline
before import. The importer rejects wrapping/backward counters; it does not guess
how many clock cycles or records are missing.

Metadata schema version 1 requires:

- `capture_id`, `run_id`, `source_kind` (`synthetic` or `measured`). A held-out
  capture needs a different capture ID, run ID and normalized observation trace.
- `firmware_revision`, `configuration_id`, `board`, `mechanism`, `material`,
  `spool`, `conditions`. Training and validation must share mechanism,
  configuration and source kind. Retain operating-condition differences.
- `dropped_records`: zero for this first fitter. A dropped-record capture is
  rejected rather than silently repaired.
- `clock`: `source`, `units` (`us`), `unwrapped` (`true`), `reconstruction`,
  `synchronization`, positive `resolution_us`, and `alignment_error_us`.
- `measurement`: `step_source` (`completed_physical_pulses`), `location`
  (`feeder_exit`), `method`, `units` (`mm`), and `position_error_mm`.

The complete synthetic metadata and traces used for acceptance are retained
under measurement-profile-01.
Their stated uncertainty is a test input, not an instrument specification.

The CSV header is exact:

```csv
time_us,forward_steps,reverse_steps,filament_mm,valid,saturated
```

Times are strictly increasing unsigned 64-bit integers. Forward and reverse
counts are separate, cumulative, nondecreasing unsigned 64-bit counters of
completed physical pulses. They are not requested endpoints or a firmware
position estimate. `filament_mm` is signed, observed cumulative filament travel
at the feeder exit, with an arbitrary fixed origin. Flags are `0` or `1`; an
invalid position may be empty. NaN/infinity and incomplete rows are rejected.

Capture unconstrained feeder transport: do not confuse printer consumption,
buffer-capacity limits, a path endpoint or a separately driven motor with this
simple step-to-travel relationship. The method and conditions must describe
how physical pulses and independently observed filament position were obtained.
Synchronize samples so their stated alignment error is acceptable for the chosen
measurement tolerance. Software callback timestamps alone do not establish
electrical edge timing or filament movement.

## Fit and validation

Run from the `sim` directory, using separately saved training and held-out runs:

```sh
timeout 30s python3 -B measurements.py fit-travel \
  --base profiles/synthetic-lane.json \
  --training /path/to/training-capture \
  --validation /path/to/held-out-capture \
  --id travel-profile-v1 --tolerance-mm 0.1 \
  --output /path/to/travel-profile-v1.json
```

Choose the tolerance from the intended use and measurement accuracy before
fitting; `0.1` above is only command syntax, not a recommended physical tolerance.
The output must be a new file. A failed comparison still saves a rejected report
and exits 1; malformed/incompatible input exits 2 without a fitted profile.

Least squares through zero estimates travel per step separately in each
direction, using at least three usable intervals per direction in each run.
Invalid/saturated endpoints exclude their adjacent intervals without bridging
the gap. Mixed-direction intervals are excluded from fitting and counted in the
report; their net displacement is still checked during trace validation. Idle
motion is checked too. Validation never adjusts the fitted coefficients.

Acceptance requires both interval travel errors and cumulative position error
within each contiguous valid segment to meet the declared tolerance in training
and validation. Reports retain excluded intervals, sample counts, RMS/maximum
errors, observed step-rate ranges, source metadata and original/normalized
input hashes. Validation therefore states what was tested rather than claiming
a bound on every future speed, load or material.

These observations identify **`step_mm * transfer`**, not shaft calibration and
slip independently. `step_mm` remains fixed from the parent profile; only
`forward_transfer` and `reverse_transfer` change. Their sources explicitly state
this conditional interpretation. A fit outside the model's 0..1 transfer range
is rejected, never clipped. Other geometry/dynamics values and their assumed or
measured provenance remain inherited and visible.

The reported position-error sensitivity propagates the declared endpoint error
through the coefficient calculation. It is not a statistical confidence
interval and does not include shaft-calibration error, clock-alignment error,
model mismatch or unobserved operating conditions. Real captures require those
uncertainties to be justified separately. The profile remains unqualified for
hardware use even when the measured-trace comparison passes.

## Use the profile in the real native loop

```sh
make test-profile PROFILE=/path/to/travel-profile-v1.json \
  BUILD_DIR=../build/profile-travel-v1
```

This generates a header containing the nine numeric plant parameters and the
profile hash, then compiles the existing `plant.c` and production control core
with the selected parameters. Rejected fits or fitted transfer values edited
after validation cannot be exported. The generated header can replace only a
previously generated header, preserving unrelated files. `test_loop_profile`
and `profile-trace.csv` are separate from the fixed baseline test artifacts.

The same eight closed-loop scenarios run without changing controller limits or
expected outcomes. A profile can fail those scenarios even after its travel fit
passes. Keep that failure visible: it may reveal a controller issue, a profile
outside the fixture's operating range, or an insufficient model. Passing this
suite does not validate parameters that these travel captures did not measure.

```sh
make test-measurements
make test test-loop
```

All test/build targets have explicit timeouts. The Python workflow uses only
the standard library. Physical capture remains a separately authorized activity.
