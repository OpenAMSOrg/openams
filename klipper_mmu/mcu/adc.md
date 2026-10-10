<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# H723 paired ADC binding

`adc.c` connects the portable paired-acquisition state machine to Klipper's
`gpio_adc_*` API and the shared MCU clock. It starts only through an explicit
firmware call from ready task context with interrupts enabled. It registers no
host commands, touches no pins at boot and produces no actuator output.

Users choose canonical GPIO pins in native `make menuconfig`. AFC Lite SW1/PC4
(numeric pin 36, ADC1 channel 4) and SW2/PC5 (pin 37, ADC1 channel 8) are
optional editable presets. On H723 the pins must be external and distinct, and
the first route selected by the shared driver must be the same ADC unit on
different channels. ADC1, ADC2, and ADC3 pairs are supported. The raw maximum
is 4095. This binding records raw channels without inventing a calibration or
sum/difference transfer function. Samples are sequential. They are not a
synchronized conversion, a current measurement, or a BLDC control loop.

Saved BUFFER_A and BUFFER_B pins are the pair passed at startup. Before
activation the caller validates a complete resource plan that includes every
other MCU user. The resource factory queries actual Klipper routes and does
not keep a copied pin table. The shared driver also keeps ordinary clients off
every channel of a locally owned unit until reset. This helper cannot discover
reservations made outside that plan. When PC4 or PC5 is the analog pin, the
HUB=PC4 and TRG1=PC5 preset aliases still have to be remapped. An initially
reversed valid pair is allowed. The first successful `gpio_adc_setup_local_pair_timeout`
fixes that ordered pair until MCU reset. Ordinary `gpio_adc_setup` is not a
fallback. If local ADC support is absent, the analog profile is rejected before
any hardware mutation and switch feedback stays usable. This binding adds no
host command and no boot-time start. ACTUATION stays zero.

`km_adc_start(pin_a, pin_b, config, timeout_us)` accepts a positive timeout
below 2^31 microseconds and checks lifecycle, clock, pins, and configuration
before hardware setup. It exclusively calls `gpio_adc_setup_local_pair_timeout`
with interrupts enabled. One observed start interval covers shared-driver setup
and reader publication; the driver receives the remaining time, capped to a raw
tick span below 2^31. Fractional or sub-MHz clocks are rejected before setup.
Regulator, calibration, and ready waits on this API are bounded. Ordinary Klipper
ADC setup calls retain their existing behavior.

A driver timeout latches the reader FAULTED, leaves its unit unpublished, and
requires MCU reset. A deadline rejection after successful setup leaves the reader
STOPPED without arming its timer, while retaining the ordered pair identity.
Stopping a running reader permits restart with that same pair. A different pair
returns busy before hardware access; a duplicate start while running leaves it
running. The shared driver cancels failed partial setup and publishes both handles
only on success.

Each timer callback services at most one channel poll. A starts before B, and B
cannot start until A is consumed or cancelled. The ADC is read only when its
poll returns ready. Positive retry ticks are converted with ceiling division by
the actual clock frequency. The portable retry is anchored to the service-entry
observation, as upstream acquisition schedules retries from its previous wake;
it is not a fresh delay starting at callback return. A strictly future timer
guard prevents insertion in the past, but subsequent instruction/scheduler cost
can still cause a `Timer too close` fault. This is not measured timing assurance.
After a pair completes or fails, the next pair waits at least the configured
sample period (1 ms by default). Conversion retries retain their shorter
hardware deadline. This avoids an otherwise continuous roughly 1 µs re-arm
between pairs while keeping the last completed sample available to the control
loop. The saved period plus maximum pair span must fit below maximum sample age.

A fresh coherent clock observation after each sample/read callback checks both
original absolute deadlines: channel conversion and total pair span. Equality
expires the budget. Queued time and callback time never extend it. Busy overrun
cancels the attempted conversion; a failed callback's raw value is absent in the
result. Already accepted partial raw data and original observation timestamps
remain available. A final post-service observation also rejects overrun before
publishing a valid sample. Timeout records remain invalid while later cycles run.

The portable sample fields retain service-entry attempt/readiness observations;
`a_complete_us` and `b_complete_us` are not callback-return timestamps or measured
electrical conversion instants. Completion clock reads enforce budgets separately.
The B deadline begins at A's retained readiness observation, conservatively
including time spent reading A. Sample age is measured from the first A attempt.

Cancellation acts only on this reader's attempted channel. The first shared-clock
fault enters Klipper's non-returning shutdown; the ADC shutdown hook cancels any
pending conversion without calling the clock again. Ordinary stop cancels work,
removes its timer and invalidates cached data, permitting an explicit restart
of the latched ordered pair.
Shutdown/fault stays latched until MCU reset, including after `clear_shutdown`.

An ordinary `km_adc_get_sample()` preserves acquisition state and original sample
age while observing/advancing the shared clock. It can trigger global shutdown on
an invalid clock, which cancels acquisition and invalidates cached data. A successful
query return means a record was copied; callers must still inspect `is_valid`.
`km_adc_get_sample_at()` additionally returns the same coherent clock observation
used by that query. The [local sensor callback](sensors.md) uses it to preserve
the acquisition's original millisecond epoch when publishing calibrated feedback.

Deadline evidence remains in
`../docs/plan/status-history.md`.
Accepted reader, startup, resource-plan, and image scope is reviewed in
`../docs/plan/status-history.md`.
The earlier combined acceptance record
includes 56 saved-configuration tests; they were not rerun here. The
current ADC acceptance
integrates 13 files from iterations 139–143. Fresh results cover 34 mock timing,
24 sensor, 15 shared-driver composition, 40 GPIO-local startup, and 25 legacy
startup cases. Two deliberate deadline defects fail precisely the new cases they
should. The enabled and disabled H723 builds match the tested production source
and keep ACTUATION=0. Startup still polls synchronously with interrupts enabled;
physical electrical and analog timing remain unqualified. No board was accessed
or flashed.

Saved acquisition limits are available through [the DiffA/DiffB menu factory](adc_config.md).
Its validation does not create a temporary reader or invoke HAL callbacks.
