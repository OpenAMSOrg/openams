<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Saved DiffA/DiffB acquisition limits

The H723 pin menu exposes **DiffA/DiffB acquisition limits** when analog buffer
feedback and upstream ADC support are selected. The three deadlines start at zero
(unset). Saved configuration must explicitly supply a conversion timeout, total
pair acquisition span and maximum sample age, in microseconds. These are timing
budgets, not qualified board defaults. A fourth setting gives the minimum delay
between completed pairs; it defaults to 1 ms and is adjustable in menuconfig.

`km_config_adc_pair()` is a pure factory. It fixes the raw maximum at 4095 for
the upstream H723 ADC binding, applies the same validation as the portable reader,
then checks sample age against the operation profile's sensor age in milliseconds.
Conversion and pair deadlines must be positive and less than half the 32-bit
microsecond range. Pair span must not exceed sample age; sample age must not
exceed the profile limit. A conversion timeout may exceed the pair span because
both absolute deadlines apply independently; the tighter deadline wins.
The period plus maximum pair span must be less than maximum sample age, so a
configured acquisition can refresh feedback before its previous sample expires.

Disabled pin configuration, switched buffer feedback, missing upstream ADC support,
or any unset limit returns `KM_ERR_UNAVAILABLE`. Invalid limits return
`KM_ERR_INVALID_PARAM`; a sample age exceeding the profile returns
`KM_ERR_CONFLICT`. Failure preserves the caller's output. No HAL calls occur.

The factory does not validate wiring, ADC routes, or start acquisition. It
requires `CONFIG_WANT_ADC` with pin configuration and analog feedback, and does
not also require `CONFIG_WANT_ADC_LOCAL`. Reader startup requires both
`CONFIG_WANT_ADC` and `CONFIG_WANT_ADC_LOCAL`. A missing local API returns
`KM_ERR_UNAVAILABLE` before I/O. Switched feedback stays usable.

Saved BUFFER_A and BUFFER_B are canonical user-assigned GPIOs. SW1/PC4 and
SW2/PC5 are optional editable presets, not required pins. `km_config_pins()`
checks canonical pins, collisions, and non-H723 analog profiles.
`km_config_h723_resources()` queries the shared driver's first selected route
and accepts an external, distinct, same-unit, different-channel pair on ADC1,
ADC2, or ADC3. There is no copied route table. The shared driver keeps ordinary
clients off every channel of a locally owned unit until reset. Callers still
supply the rest of the MCU resource plan and prevent later conflicts. A legacy
HUB=PC4 or TRG1=PC5 overlap applies only when those pins are actually used;
the user remaps them.

The first successful local setup fixes that ordered pair until MCU reset. An
initially reversed valid pair is allowed. A later changed pair is busy. The
paired calibration still goes separately to `km_config_profile()`. The nominal
1.65 V center is not enough to fabricate its calibration table. Samples stay
sequential 12-bit counts with raw maximum 4095.

The [ADC binding](adc.md) documents deadline and timestamp semantics. Pair span
starts at admission and includes queued work. Sample age starts at the first
DiffA attempt. A/B are sequential conversions, and recorded timestamps are
software observations, not exact electrical capture instants. The callable
board-startup composition applies this saved configuration and the resource
plan and attaches the local controller at READY. The analog BoxTurtle build
omits the legacy pre-calibration auto-boot task; optional hosted diagnostic
startup waits for host-delivered calibration and verified boot identity.
Physical timing, measured calibration, and hardware qualification remain open.
`ACTUATION` is 0 unless the separate experimental gate is selected.
