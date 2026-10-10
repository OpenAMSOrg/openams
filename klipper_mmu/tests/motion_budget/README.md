# BoxTurtle reference motion budget

The BoxTurtle Kconfig values are editable provisional starting points based on
the pinned AFC archive template: 16 microsteps, a 50:10 gearbox, 22 mm of
filament per output rotation, lane-to-HUB distance up to 155 mm,
HUB-to-toolhead Bowden length 1725 mm, and 65 mm post-HUB clearing. At 200 full
steps/revolution the menu derives
`200 * 16 * 50 * 1000000 / (10 * 22000) = 727272` microsteps per metre, i.e.
727.27 microsteps/mm; the 65 mm clear default is 47,273 microsteps.

The travel estimate uses a trapezoidal move with the configured velocity and
acceleration. A conservative upper bound is `distance / speed + speed /
acceleration`, allowing one full velocity ramp's time for acceleration and
deceleration. At the 26 mm/s feed default and 500 mm/s² acceleration, 155 mm
has an upper estimate of about 6.0 s and 1725 mm about 66.4 s. The 20 s HUB
and 90 s retract deadlines leave time beyond that estimate. These estimates do not
account for friction, path variation, startup, or sensor delays.

FPS arrival's outer travel limits after HUB default to 100 mm (72,727
microsteps) and 1900 mm (1,381,817 microsteps), the reference Bowden plus 10%.
The upper estimate for 1900 mm at the 20 mm/s crawl (the whole run after HUB
crawls at a step-estimated CALIBRATE) is about 95.0 s, so with the same 25%
margin the maximum travel is reached before the 118.75 s arrival deadline
(`KLIPPER_MMU_ARRIVAL_TIMEOUT`, `mcu/Kconfig.control`). The host narrows this
window per printer from a stored step estimate. A step-estimated PTFE
calibration retracts from arrival back past HUB, up to the same 1900 mm, at
the 26 mm/s retract speed instead (about 73.1 s at the same 500 mm/s²: 1900 /
26 + 26 / 500), so its calibration retract deadline still fits comfortably
within the same 90 s as the ordinary retract deadline.

A LOAD retry retracts from up to 1900 mm back behind HUB (about 1965 mm, an
upper estimate of about 75.6 s, roughly 19% inside the 90 s retract
deadline), and the last attempt feeds at
half speed within twice both feed deadlines. The whole LOAD, retries included,
ends within `KM_LOAD_BUDGET_MS` (240 s): one worst-case attempt (20 s HUB +
118.75 s arrival = 138.75 s) fits, and 240 s with 20% margin stays below
OpenAMS's 300 s disconnect backstop.

## Every other device

BoxTurtle is the only device whose `KLIPPER_MMU_HUB_TIMEOUT`,
`KLIPPER_MMU_ARRIVAL_TIMEOUT`, `KLIPPER_MMU_RETRACT_TIMEOUT` and
`KLIPPER_MMU_CAL_RETRACT_TIMEOUT` are a hand-picked, hand-qualified Kconfig
literal like the numbers above -- every other device's is 0 (unset), and
`mcu/config.c`'s `KM_STAGE_DEADLINE_MS()` derives it instead from the *same*
rule: 1.25x this stage's own furthest travel at its own slowest configured
speed, plus one acceleration ramp (`distance / speed + speed / accel`, this
file's own upper bound, times 5/4 in place of the 1.2-1.25 used above so the
same integer arithmetic reproduces on every build).

The travel comes from `KLIPPER_MMU_GATE_HOMING_MAX_MM` (gate to HUB) and the
now-general `KLIPPER_MMU_FPS_ARRIVAL_MAX_MM` (HUB to arrival, the Bowden;
`KLIPPER_MMU_RETRACT_TIMEOUT` adds `KLIPPER_MMU_UNLOAD_HUB_CLEAR_MM` to it, the
LOAD-retry case above), both per-device Kconfig defaults sourced from Happy
Hare's own dossiers (`docs/devices/*.json`'s `gate_homing_max` and
`bowden_homing_max`). A device with neither (Custom, or any uncatalogued
device) keeps the old flat guess (8/12/8/15 s) instead of deriving from a
distance nobody measured. `KLIPPER_MMU_REWIND_TIMEOUT` derives the same way
from `KLIPPER_MMU_UNLOAD_HUB_CLEAR_MM` alone (the settle-then-clear move, not
the Bowden); BoxTurtle's own 15000 ms there is also a pinned literal, not
derived, since its own 65 mm clear already fits many times over. See
`mcu/Kconfig.control`'s help text for each symbol and
`tests/test_config.py`'s `test_stage_timeouts_derive_per_device_geometry` for
worked examples (a selector machine and a virtual-HUB device).

These defaults are not qualification for any fitted machine. Measure the
installed path and calibrate the drive, then bench-qualify speed, acceleration,
clear distance, and deadlines before enabling motion.

Run `python3 klipper-mmu/tests/motion_budget/check_budget.py` for the focused
default-budget check.
