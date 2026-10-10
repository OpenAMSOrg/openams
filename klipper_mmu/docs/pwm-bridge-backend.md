# Portable PWM bridge output boundary

`km_pwm_bridge_driver_t` implements the common `km_driver_t` interface for a
logical two-input DC motor bridge. The operation core can use it for powered
rewind. It owns enable, direction, duty, reversal delay and latched protection;
the host supplies operation intent. No host round trip participates in these
output decisions.

This is portable software over immediate board callbacks. It does not select a
bridge chip, configure AFC Lite pins, implement BLDC commutation or establish
measured timing, current accuracy or electrical safety.

## Board contract

The board must exclusively reserve the enable pin, both PWM channels and their
shared timer resources. It maps logical forward/reverse, enable and 0..1000 duty
to the specific circuit's polarity and register values. Zero means inactive on
either channel, independent of physical pin levels. PWM frequency, shared timer
period compatibility, reset levels and any hardware interlock belong to that
adapter. Do not implement PWM by delayed host commands.

All calls, including supervision, output changes, fault clear and fault trips,
must be serialized and non-reentrant. Callbacks complete immediately with bounded
execution time and cannot queue writes, wait for conversions or perform blocking
bus transactions. A successful write means that the output has actually changed;
this includes any PWM update latch delay. A failed write may have taken effect.
The backend retains conservative history and removes enable before cleanup.

Required callbacks are `force_disable`, `write_enable`, `write_pwm`, `now_us` and
`ready`. `force_disable` must physically remove enable even if either normal PWM
write fails. It has no failure return; the board must provide that guarantee.
This contract cannot prove that failed hardware obeys it. Optional `fault` is a
cached physical fault query. All status callbacks must also be bounded.

Use `km_pwm_bridge_init` only for a fresh, quiescent instance. The context must be
separate from the driver storage. Config/HAL inputs are copied before clearing
the driver, so those inputs can alias their corresponding driver fields. Invalid
initialization leaves an inert instance and calls any available disable/zero
callbacks, including when the driver pointer is null. It cannot remove enable
when no valid force-disable callback was supplied. Initialization does not inspect
or shut down arbitrary prior driver memory; a live instance must first be stopped
and all prior use quiesced.

Configuration requires:

| Setting | Accepted range | Purpose |
| --- | --- | --- |
| `maximum_duty_permille` | 1..1000 | Maximum logical energization duty |
| `reversal_deadtime_us` | 1..2^31-1 | Required observed off interval before opposite drive |
| `maximum_call_lateness_us` | 1..1000 | Maximum age of a supplied direction timestamp |

There are no electrically qualified defaults. The board's characterized settings
must determine these values. Software reversal delay does not replace the bridge
chip's switching interlock or its specified transistor dead time.

`km_pwm_bridge_config_validate()` checks these limits without I/O. Initialization
uses the same validator and retains its disable/zero behavior on invalid input.
The H723 [saved rewind factory](../mcu/rewind_config.md) uses it before publishing
menu choices and checks the operation profile against the bridge duty ceiling.

## Outputs and reversal

Initialization, disable, trip and output failure first force-disable the bridge,
then attempt to zero both PWM channels. Cleanup failures latch a fault even when
later writes succeed. Enable first confirms both channels are zero. Repeated
enable while enabled is idempotent but still checks time and physical status.
Enable and fault clear never restore a saved duty.

Positive duty requires initialized, enabled, ready and fault-free state. The
opposite channel is explicitly zeroed before writing positive duty to the chosen
channel. Zero duty writes both channels inactive and may leave enable asserted.
The backend records when those immediate writes finish, not when they started.
Changing direction while duty is nonzero returns `KM_ERR_BUSY` without changing
the running direction or output; command zero or disable first.

Either direction is allowed on first energization. After a direction was driven
(or a failed positive write might have driven it), opposite drive requires the
configured continuous off interval. The interval starts at actual observed
completion of disable or confirmed zero outputs. Same-direction restart does not
need an opposite-direction delay. Repeated zero, stop and early retry calls retain
a known continuous off interval. Age saturates at the configured requirement, so
a long serviced idle cannot resurrect a delay after fine-counter wrap.

An early opposite positive-duty request returns `KM_ERR_BUSY` and leaves outputs
disabled and zero. A failed cleanup returns `KM_ERR_FAULTED`. No duty is queued:
the caller must explicitly re-enable and retry once the off interval is satisfied.
The public driver wrapper also shuts down on an unsuccessful PWM call. The first
powered-unload sequence (`PWM 0`, reverse, enable, positive duty) and subsequent
same-direction unload are supported. A future reversing assist operation must
handle this synchronous retry contract itself.

## Clock and faults

`now_us` is a local wrapping 32-bit microsecond clock. Direction selection rejects
a supplied future timestamp or one older than `maximum_call_lateness_us` and
reports its actual observed completion time. Each compared interval and each
gap between observations must be less than 2^31 us. The board must service the
backend even while idle or faulted. A backward or half-range-ambiguous observation
latches a fault and disables output. A full counter cycle cannot be detected if
service stops altogether; meeting the service horizon is a caller obligation.

That horizon is a clock interpretation bound, not an acceptable protection
latency. The adapter must choose a supervision cadence suited to its hardware
and use direct fault handling when necessary. `km_pwm_bridge_service` performs
one bounded supervision call, with no waiting loop. Call it before operation-core
fault evaluation. A fault interrupt can call `km_pwm_bridge_trip` only with the
same serialization guarantee as other calls.

`km_driver_get_fault` returns the backend's cached latch without touching hardware.
Service and accepted output paths ingest physical status. Loss of readiness while
enabled, a physical fault, failed output I/O or invalid time removes enable and
latches the first observed reason. Later failures cannot replace that reason.
Rejected parameter/direction commands are not a substitute for supervision.

Clear is explicit and allowed only while disabled, with the physical fault gone,
readiness restored and both zero writes successful. It re-establishes the clock
epoch and starts conservative off age at zero while retaining energized-direction
history. This can recover from a clock fault without inventing prior dead time.
Output remains off. `init` returning `KM_OK` means a valid initialized instance;
a currently reported physical fault can still be latched, or readiness pending.
Every subsequent enable observes these conditions again.

## Optional current

Current measurement capability is advertised only when `read_current_ma` exists.
The callback must return a real, validity-qualified measurement. Missing or failed
measurement remains an error and leaves the caller's output untouched; an actual
zero is valid. There is no current-setpoint capability and no conversion from a
setpoint into a purported measurement.

The getter cannot supply an acquisition timestamp. The board's dated sample stream
must independently feed `km_core_set_rewind_current`; it must not date an old
cached measurement with the current query time. Hardware current protection and
measurement calibration remain board responsibilities. Open-loop rewind remains
available without that optional measurement capability.

## Software evidence and remaining work

The native suite uses a chronological simulated bridge, checks safe write order,
and composes powered unload/cancel with the real operation core and timed feeder.
The separate review fixture exercises callback delays, write failures that may
have applied, wrap, reversal endpoints, cleanup, recovery, optional current and
powered-unload protection faults. These tests run production portable sources
under strict compiler warnings, AddressSanitizer and UndefinedBehaviorSanitizer.

The isolated upstream Klipper experiment retains the three public PWM APIs and
their dependencies in enabled images and checks disabled-build isolation. It does
not instantiate this driver against physical pins. Real GPIO/PWM ownership,
initialization/shutdown wiring, interrupt cost, fault latency, current acquisition
and BoxTurtle motion qualification remain required. No hardware is accessed by
these tests or build checks.
