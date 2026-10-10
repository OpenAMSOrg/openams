# Local sensor composition

`km_sensors_sample(core, NULL)` is the H723 controller's callable sensor callback.
It joins the existing digital presence reader and optional paired DiffA/DiffB
reader to the common operation core. It adds no host commands. The callable
board-startup composition invokes resource planning and saved pins and attaches
the local controller at READY. An automatic startup caller, verified boot
identity, and host-command attachment remain pending.

Bay, hub and arrival switches retain the digital reader's conservative timestamp
from before the first read. Switched-buffer profiles use that same reader without
ADC access. Analog profiles query the latest completed pair and decode it with
the configured calibration; no nominal voltage transfer function is installed.
Sequential acquisition uses raw maximum 4095, so calibration must use that
maximum. It is not synchronized sampling, a measured current, or a BLDC loop.
A missing local ADC API leaves the analog reader unavailable before I/O;
switched feedback stays usable. Missing, partial, failed, off-path, or expired
pairs invalidate buffer feedback. Encoder and current measurements are not
synthesized.

`km_adc_get_sample_at` returns the pair and the coherent clock observation used
to check its age. The shared `km_buffer_update_adc_pair` converts the first-A
attempt timestamp into the original coarse millisecond epoch, including across
both counter wraps. The simulator uses the same conversion. A cached reading
never receives a new acquisition timestamp merely because it is queried again.
The ADC's microsecond age limit and the profile's millisecond limit both apply.
The callback rechecks the ADC after decoding, so time spent decoding cannot
silently consume the remaining ADC age budget. It runs in the controller's
control task with interrupts enabled; the ADC accessor copies one complete pair
under its own lock. A pair completed during a decode supersedes it and is
decoded once instead, without refreshing either acquisition time; a second
supersession in one callback leaves feedback invalid.

Ordinary ADC stop invalidates feedback on the next callback; explicit restart
requires a new completed pair before feedback becomes valid. Global shutdown
invalidates the bound buffer immediately without reading a clock and remains
latched after `clear_shutdown`. The operation core handles invalid feedback and
stops active motion through its existing protection path.

Verification is recorded in
`../docs/plan/status-history.md`. Native tests use
the production ADC, clock, switch and controller code with simulated pins and a
strict timer queue. They cover acquisition age, fractional clock rates, wrap,
shutdown, stop/restart, decoding cost and load-to-follow with feedback loss.
Builds without ADC preserve switched behavior and leave analog input invalid.
These checks do not measure interrupt cost or qualify electrical behavior.
The shared ADC driver excludes ordinary clients from a locally owned unit
until reset. Other resource claims and later conflict prevention remain caller
requirements. Hardware qualification remains open, and `KLIPPER_MMU_ACTUATION`
remains zero. There is no automatic output setup.
