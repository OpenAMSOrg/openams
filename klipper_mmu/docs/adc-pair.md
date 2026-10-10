<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# ADC Pair Acquisition Component

The `adc_pair` component provides a bounded, state-machine driven ADC acquisition flow for the Klipper-MMU project. It ensures that analog sensor channels (like DiffA and DiffB) are sampled correctly, bounding the completion time for both conversions while recording caller-supplied observation timestamps.

## Overview
This module acts as a portable bridge between the Klipper MCU hardware abstraction layer and the higher-level buffer sensor logic. It handles the low-level polling, scheduling, timeouts, and temporal validation required for trustworthy dual-channel ADC observations, independent of any specific GPIO or device implementation.

## Features
- **Bounded execution**: Ensures that no persistent loop occurs. Each conversion must complete within `conversion_timeout_us` and the full A/B pair must complete within `max_pair_span_us`. Wait times for the first service poll (queued time) are also bounded by the overall pair span.
- **Clock Fault Detection**: Rejects ambiguous or backward timestamps and latches into a fault state.
- **Strict Ordering**: Polls Channel A until ready, then transitions immediately to polling Channel B, without parallel overlap or uncontrolled wait recursion.
- **Pure Queries**: The `km_adc_pair_get_sample` query validates staleness based on `max_sample_age_us` without altering the internal component state.
- **Fail-Safe**: Errors, cancellations, and resets predictably clear progress, clear metadata of new acquisitions, and invoke the hardware `cancel` callback to free physical converter resources.

## Result Validity and Caller Responsibilities
The component maintains strict boundaries on result validity. An older completed sample is expired during time-advancing `begin` or `service` operations when its age exceeds `max_sample_age_us`, permanently invalidating it until a fresh completed pair is acquired. Timeouts, out-of-range values, cancellation, or clock faults will also invalidate the result.

**Crucially, it is the caller's responsibility to propagate this invalidity or staleness to the local control core.** The local controller must not treat missing, stale, or out-of-bounds readings as valid analog data.

## Timestamps
The timestamps recorded (`a_attempt_us`, `a_complete_us`, etc.) are **caller-supplied observation bounds**, not exact hardware acquisition times. The actual physical sample acquisition lies somewhere within the attempt-to-readiness interval, subject to callback latency. 
The queued time (time between `begin` and the first actual poll attempt in `service`) is bounded by the total pair span timeout, but first-poll timestamps correctly reflect when the HAL is initially invoked for each channel, independent of prior queued time or busy retries.

`pair_start_us` records admission. The A conversion budget begins at admission;
the B budget begins when A is consumed. These conservative budgets include
dispatch delay before each first poll. They are not measurements of hardware
conversion duration. Zero is a legitimate timestamp after wrap, so do not infer
presence from a nonzero timestamp; inspect validity and `present_mask`.

All calls and callbacks must be serialized and non-reentrant. Callbacks must be
immediate and bounded; physical acquisition timing and callback latency remain
board qualification work. Time-advancing calls must be less than half a 32-bit
microsecond range apart, including during long idle. Queries do not advance the
clock or satisfy that requirement. An explicit reset establishes a new epoch.

## API Documentation

- `km_adc_pair_init(pair, config, hal, context, now_us)`: 
  Initializes a new `km_adc_pair_t` object. It validates the configuration bounds (positive, <2^31, and `max_sample_age_us >= max_pair_span_us`). If initialization fails, it invokes the HAL cancel callback and zero-initializes the pair to leave it inert.

- `km_adc_pair_begin(pair, now_us)`:
  Queues a new pair acquisition sequence, targeting Channel A; no HAL poll occurs until service. Fails if the component is busy or in a clock fault. Rejects backwards timestamps relative to the previous time-advancing call. Clears metadata from prior acquisitions so partial errors cannot inherit old completion times.

- `km_adc_pair_service(pair, now_us)`:
  Progresses the state machine. Enforces pair span and channel deadlines before polling the HAL. Advances from A to B upon successful reading. Handles busy retries by setting the next evaluation deadline. Records raw observations and observation timestamps immediately upon a successful poll.

- `km_adc_pair_next_deadline(pair, uint32_t *deadline_us)`:
  Calculates the earliest next service time (from either a channel conversion retry, a channel timeout, or a span timeout). Clamps overdue timer work to the latest observed time, returning the current time if the deadline is in the past. Returns `KM_ERR_UNAVAILABLE` when idle or faulted.

- `km_adc_pair_get_sample(pair, now_us, sample_out)`:
  Populates `sample_out` with the most recently completed sample (or a partial failed sample). Evaluates staleness against the provided `now_us` without altering the component state. Caller must inspect `sample_out->is_valid`.

- `km_adc_pair_reset(pair, now_us)`:
  Immediately cancels any in-progress acquisition, invokes the HAL cancel callback, invalidates all prior data, and establishes a new timestamp epoch, bringing the component to an `IDLE` state.

`service` returning `KM_OK` does not imply a valid conversion. Ordinary
acquisition errors are recorded in the available sample, with `is_valid=false`
and its error `result`. `get_sample` returning `KM_OK` likewise means a record
is available, not that its data is valid. Always inspect the copied record and
propagate invalidity to local control; never refresh an old sample's timestamp
when forwarding it. This component neither programs pins nor installs a default
DiffA/DiffB calibration. Raw out-of-range observations remain visible for diagnosis.
