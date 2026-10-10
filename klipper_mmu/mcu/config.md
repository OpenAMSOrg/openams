# Saved firmware control settings

Klipper `make menuconfig` selects the MCU, then the MMU device and the common
control profile through `Kconfig.control` and `km_config_profile`. Device
selection seeds build defaults; saved user values override them. The catalogue
in `Kconfig.devices` has 28 entries (planned HH, AFC and OpenAMS devices, plus
Custom). `DEVICE_NAME` and `PROFILE_IMPLEMENTED` stay hidden. BoxTurtle
defaults to powered rewind; NightOwl and Custom default to passive rewind.
Those three default to switched buffer feedback, without encoder or
measured-current requirements. Only BoxTurtle, NightOwl and Custom profile
factories are implemented. Any other catalogue selection returns
`NOT_SUPPORTED` and leaves the caller's output unchanged.

The menu exposes rewind mode, optional encoder, current and analog feedback,
feeder mechanics, feed and retract speeds, acceleration and braking,
driver-current defaults, rewind duty, proportional gain and deadband, sensor
freshness, host lease and operation deadlines. Motion is millimetres, the way
Klipper asks for a stepper: rotation distance, gear ratio, full steps and
microsteps, with speeds in mm/s, acceleration, braking and jerk in mm/s² and
mm/s³ (jerk 0 means automatic). Everything else uses steps, milliseconds and
the units on each prompt. `mcu/feeder_mechanics.h` derives the scale once at
build time,

```
steps_per_m = full_steps * microsteps * A * 1000000
              / (B * rotation_distance_um)
```

so BoxTurtle's 200 full steps (a 1.8 degree motor), 16 microsteps, 50:10
gearbox and 22 mm of filament per output rotation give 727272 microsteps per
metre, 727.27 microsteps/mm. The common control core and every exported
constant stay in microsteps, and a build whose mechanics push a menu value
past the core's limits fails instead of moving wrongly.

Measure the rotation distance rather than trusting a datasheet: mark the
filament at the feeder inlet, turn the output exactly one rotation and measure
how many millimetres disappeared. From a known steps/mm figure, invert the
formula: `rotation_distance_um = full_steps * microsteps * A * 1000000 /
(B * steps_per_mm * 1000)`.

Driver current settings are requests the selected driver must check
against its electrical limit. They are not measurements, and this menu does
not invent sense-resistor or motor-current values.

`km_config_profile(out, calibration)` builds the existing `km_device_profile_t`
and calls its common validator. It does not create another control algorithm.
The result initializes the same core used by native and simulator tests. Bay,
hub and arrival sensing remain baseline requirements for the implemented
compositions. Selector mechanisms and detailed hardware routing are not
supplied by this factory.

Analog mode requires a separately supplied, explicit calibration table. Missing
calibration returns UNAVAILABLE; malformed tables and incompatible control values
return INVALID_PARAM. A calibration supplied in switched mode is rejected
rather than ignored. The factory copies a valid table and leaves the output
untouched on any error. Runtime calibration storage and transport are still
pending;
no synthetic table is installed as a default.

`km_diff_adc_config_validate()` is shared by the profile factory,
sensor initialization and simulator feedback generation. Profile validation
checks calibration without constructing temporary sensor state. It does not
establish acquisition freshness or replace validation of the board ADC format.

Four dictionary constants reflect these exact saved build choices:

| Constant | Configured controller behavior |
|---|---|
| `KLIPPER_MMU_CONFIG_POWERED_REWIND` | Powered rewind rather than passive rewind |
| `KLIPPER_MMU_CONFIG_ENCODER` | Encoder feedback selected |
| `KLIPPER_MMU_CONFIG_DIFF_BUFFER` | Paired analog rather than switched buffer |
| `KLIPPER_MMU_CONFIG_MEASURED_CURRENT` | Measured rewind-current regulation selected |

FPS-arrival images also export `KLIPPER_MMU_FPS_MIN_STEPS` and
`KLIPPER_MMU_FPS_MAX_STEPS`, the menu travel bounds. They are outer limits and
never narrow. Before `finalize_config` the host may send
`config_km_fps_steps oid=%c steps=%u` carrying a calibrated HUB-to-arrival
distance (0 means uncalibrated); `km_config_profile` then uses it, and LOAD
accepts arrival within it plus or minus the configured window. A nonzero
value outside the outer limits, or one sent after `finalize_config`, shuts
the MCU down. Without a calibrated distance, LOAD is refused
(`KM_REQ_UNAVAILABLE`) until `KLIPPER_MMU_CALIBRATE` measures one. See
[FPS arrival](../docs/fps-arrival.md#calibrated-load-speed-profile).

These constants are requested configuration. The integrated product still
advertises ACTUATION=0, with no automatic core attachment, and a host must
honor ACTUATION plus verified instance readiness and acquired sensor status.
Physical H723 GPIO, UART, PWM and ADC bindings stay gated pending per-MCU
integration. The board layer resolves and reserves resources and initializes
drivers and sensors before using the profile. Selecting measured current does
not create current-sense hardware, and the menu does not claim SW1/SW2 are
already reserved.

The six native configuration checks use upstream's Kconfig library to save
and reload choices, compile the production factory, and initialize the
production core with ASan/UBSan. They cover named defaults, customized
profiles, dependencies, range warnings and calibration and error behavior.
Separate upstream build and interactive-menu checks are recorded in
`../docs/plan/status-history.md`.
