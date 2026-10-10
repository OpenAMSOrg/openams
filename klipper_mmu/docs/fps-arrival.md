# Calibrated FPS load arrival

Parity finding A1: LOAD previously required a toolhead switch. A profile may now
opt in to ending the LOAD feed after HUB from a sustained response of the FPS:
the OpenAMS Hall-effect sensor's buffer position, sent as a buffered
differential pair (DiffA/DiffB) to the AFC Lite's SW1/PC4 and SW2/PC5 and
calibrated there. Arrival is that position crossing a threshold when the
filament stops at the extruder. The menu option is off by default. The
BoxTurtle AFC Lite analog release images, `boxturtle-h723-{usb,can}[-safe]`,
enable it without a toolhead switch and start with the
[defaults](#defaults-and-per-printer-bounds) below. It has no physical
qualification. The Klipper menu and MCU binding are described under
[MCU configuration](#mcu-configuration). The same arrival bounds a
step-estimated PTFE calibration; see
[Step-estimated PTFE calibration](#step-estimated-ptfe-calibration).

## What it claims

The FPS is a Hall-effect buffer that reports compression only. Its calibrated
position is 0 when uncompressed, whether the filament is slack or under
tension (tension produces no output), and rises toward 1000 as the filament is
compressed against the stationary extruder. During a print FOLLOW holds it near
mid-range (500). During rewind the filament is not coupled to the extruder, so
the buffer measures nothing then.

Arrival means that, after HUB and a minimum completed forward travel, the
calibrated buffer position rose from uncompressed to above a threshold, and
stayed there for several distinct acquisitions within the existing deadline and
a maximum travel. A buffer-position threshold alone does not prove where the
filament tip is. A jam far enough downstream, a kinked PTFE or a closed extruder idler can
produce the same response. The minimum travel, required release, maximum travel,
deadline and optional toolhead corroboration limit the error; they cannot remove
it. The subsequent FOLLOW stage continues to supervise the buffer.

## Profile parameters

All are zero and `fps_arrival` is false in every C factory profile
(`km_profile_init_*`) and in menu builds without FPS arrival, so
toolhead-switch arrival behaves exactly as before. When the feature is disabled,
any nonzero parameter is rejected rather than ignored.

| Field | Unit | Validation |
| --- | --- | --- |
| `fps_arrival` | bool | Requires `has_proportional_buffer` (thus a validated DiffA/DiffB calibration) and `has_hub_sensor`. |
| `fps_arrival_permille` | calibrated buffer position, 0..1000 | `500 + buffer_deadband_permille < value < 1000`. Forward feed against a stationary extruder drives the position toward 1000, following the follower's forward convention. |
| `fps_arrival_samples` | distinct acquisitions | `>= 2`, consecutive, all at or above the threshold. |
| `fps_arrival_hold_ms` | ms between first and last candidate acquisition times | `1 .. buffer_feed_timeout_ms - 1`. |
| `fps_arrival_min_steps` | completed feeder microsteps after HUB | `> 0` and `< fps_arrival_max_steps`. |
| `fps_arrival_max_steps` | completed feeder microsteps after HUB | Reaching it without arrival ends the attempt with `NO_PROGRESS`. |

With a configured toolhead switch, it corroborates the result: it must be
fresh and present when FPS arrival qualifies. The switch alone no longer
completes LOAD. Without a toolhead switch, LOAD is admitted only when FPS
arrival is enabled. PTFE calibration then uses the step estimate below.

## Control rules

- Admission requires a fresh buffer below `fps_arrival_permille`; the
  uncompressed rest (0) is normal.
- Each entry to `LOAD_FEED_TO_BUFFER`, from HUB or a hub-preloaded LOAD,
  records the completed trajectory position (`current_position_steps`,
  confirmed driver pulses, not the commanded model) and the current time. All
  earlier evidence is discarded. Cancellation, faults, recovery, path
  reconciliation and later operations cannot carry evidence into a new window.
- In that stage, every update ends the attempt for a stale or invalid
  buffer (off-path pair, failed acquisition) or a fully compressed reading
  (1000) (`BUFFER_SIGNAL`), an absent HUB (`FILAMENT_LOST`), or travel reaching
  `fps_arrival_max_steps` (`NO_PROGRESS`). The existing
  `buffer_feed_timeout_ms` deadline remains in force (`NO_PROGRESS`). A LOAD
  may retry some of these; see [LOAD retries](#load-retries).
- Only an acquisition timestamp newer than the last consumed one is evidence.
  Re-reading or republishing one sample never adds a count or elapsed time.
- An uncompressed acquisition resets the candidate. It arms qualification only
  after the controller first observes completed travel at or beyond
  `fps_arrival_min_steps`, using an acquisition strictly newer than that
  observation. A delayed pre-travel sample cannot arm arrival. Compression that begins
  before minimum travel, including before HUB, cannot arm arrival. Sustained
  by the same sample count and hold, it ends the attempt as an obstruction
  (`JAM`).
- Arrival needs `fps_arrival_samples` consecutive compressed acquisitions whose
  first-to-last timestamps span at least `fps_arrival_hold_ms`. Missing
  corroboration then stops with `SENSOR_FAILURE`; LOAD_COMPLETE is not emitted.
- Success reuses the single existing LOAD completion path: fresh buffer check,
  follower arm, LOAD_COMPLETE and FOLLOW.

Time and position arithmetic is bounded by existing limits. Sample comparisons
are wrap-safe within the buffer freshness limit (at most 60 s). The hold is
shorter than the stage deadline (below 2^31 ms). Positions stay within ±10^12
steps, so travel subtraction cannot overflow `int64_t`.

## Known bounds and remaining gates

- Travel is evaluated at the controller update. Arming waits for an acquisition
  after the first observation at minimum travel, so controller/sampling delay
  can delay arming but cannot credit an older release at a newer position.
  HUB is recorded when observed, so actual travel can exceed the counted travel
  by the HUB sampling latency.
- A hub-preloaded LOAD counts from admission, understating travel by the
  preload braking distance.
- A fully compressed reading (1000) fails the load. The threshold, sample count and hold must
  therefore qualify before the buffer reaches its calibrated end at the
  configured feed rate.
- Integration still needed: a measured DiffA/DiffB calibration per unit, and
  physical qualification of the threshold, hold and travel defaults. Per-printer
  travel bounds come from a stored step estimate
  ([defaults and bounds](#defaults-and-per-printer-bounds)). Composed
  startup/controller tests run in the release matrix
  ([composed checks](fps-composed-checks.md)), and the host projects an
  unconfigured toolhead (below). Physical qualification is separate from
  these native tests.

Native tests: `tests/test_fps_arrival.c`.

## LOAD retries

A LOAD feeds at most three times (`KM_LOAD_ATTEMPTS`) within one operation,
with or without FPS arrival. Retries emit no event: OpenAMS and every other
consumer still receive one LOAD_COMPLETE, LOAD_FAILED or LOAD_CANCELLED, and
the two reserved event slots are unchanged. An attempt ends as follows.

| Why | Stage | Fault | Retried |
| --- | --- | --- | --- |
| No HUB within `hub_feed_timeout_ms`: tip catching at the hub entry, spool snag, tangle, filament not gripped | LOAD_FEED_TO_HUB | NO_PROGRESS | yes |
| Compression sustained before minimum travel: obstruction or kink in the Bowden, or a path shorter than calibrated | LOAD_FEED_TO_BUFFER | JAM | yes |
| Maximum travel or `buffer_feed_timeout_ms` without resistance: lane drive slipping, PTFE disconnected between HUB and toolhead, FPS not responding (magnet, Hall, DiffA/DiffB wiring), or a path longer than the bound | LOAD_FEED_TO_BUFFER | NO_PROGRESS | yes |
| `KM_LOAD_BUDGET_MS` (240 s) since admission | any but braking | NO_PROGRESS | no |
| HUB lost: runout or broken filament | LOAD_FEED_TO_BUFFER | FILAMENT_LOST | no |
| Full compression (1000), invalid or stale buffer | LOAD_FEED_TO_BUFFER | BUFFER_SIGNAL | no |
| Missing toolhead corroboration, stale sensor, driver fault, host lease loss, emergency stop | any | its fault | no |

"Sustained" is the arrival rule: `fps_arrival_samples` distinct acquisitions
spanning `fps_arrival_hold_ms`, begun before a release after minimum travel
armed arrival. The FPS reads compression only, so any resistance at the
extruder (closed idler, clog, extruder not turning) is arrival, not a failure.
A path longer or shorter than the calibrated window needs `KLIPPER_MMU_CALIBRATE`.
A toolhead-switch profile has only the deadline causes. Cancellation always ends
the LOAD with LOAD_CANCELLED.

After a retried attempt the feeder brakes. Stage LOAD_RETRY_RETRACT (12) then
retracts at the rewind speed with the powered rewind, as UNLOAD does, to a
position target, using the unload HUB clearance
(`KLIPPER_MMU_UNLOAD_HUB_CLEAR_MM`, 65 mm on BoxTurtle, which the menu converts
to the core's `unload_hub_clear_steps`):

- after HUB, to that clearance behind the position where HUB was observed;
- before HUB, by that clearance, but never behind where the attempt's feed
  began, so a LOAD switch present then stays present.

It then feeds again from LOAD_FEED_TO_HUB, so HUB, the FPS window and both
travel limits restart from a new HUB observation. Attempt 2 feeds at the normal
speed and attempt 3 at half speed, within twice both feed deadlines. The
retract has `unload_retract_timeout_ms`; the usual sensor, driver, lease,
cancellation and emergency-stop checks apply, and a stop there is final. With a
zero clearance (the factory profiles) a retry before HUB only brakes, and one
after HUB backs up to the HUB observation. There is no back-and-forth wiggle:
it would need another motion stage and tuning with no hardware evidence, and
the retract and slower feed already change the attempt.

Each ended attempt, and the arrival, leaves a record: stage, fault (NONE for
arrival) and travel in confirmed feeder microsteps since the attempt's feed
began, or since HUB after it. A stop while retracting is recorded with stage
LOAD_RETRY_RETRACT. The latest LOAD's records stay until the next LOAD is
admitted; `km_load_attempts_query` reads them ([commands](../mcu/commands.md)).
After the terminal's ACK the host explains them
([host connection](../host/connection.md#load-attempt-report)), in millimetres
through the exported `KLIPPER_MMU_FEEDER_STEPS_PER_M` (BoxTurtle: 727,272):

```text
klipper_mmu box: lane 2 LOAD failed after 3 attempts
attempt 1, HUB to extruder: compressed at 312 mm past HUB (19% of the 1612 mm Bowden estimate), before the calibrated minimum: obstruction or kink in the Bowden, or a path shorter than calibrated (recalibrate); retract behind the HUB and retry
attempt 2, lane to HUB: no HUB after 523 mm: tip catching at the hub entry, spool snag, tangle or filament not gripped; back off and retry at half speed
attempt 3, HUB to extruder: no resistance within 1900 mm past HUB (118% of the 1612 mm Bowden estimate): lane drive slipping, PTFE disconnected between HUB and toolhead, FPS not responding (magnet, Hall or DiffA/DiffB wiring), or a path longer than the bound (recalibrate with KLIPPER_MMU_CALIBRATE)
```

Bounds: after maximum travel a BoxTurtle retry retracts about 1965 mm, within
the 90 s retract deadline by about 15% (`tests/motion_budget`). Three
worst-case attempts would take about 8 minutes, so the whole LOAD, retries
included, stops with `NO_PROGRESS` once `KM_LOAD_BUDGET_MS` (240 s) has passed
since admission, except while braking after a cancel. That holds one worst-case
attempt (20 s + 90 s) and stays below OpenAMS's 300 s disconnect backstop; it is
checked before any retry starts, and the host says the budget ran out. The
arrival record is the same HUB-to-arrival step estimate
as calibration; the host reports it but does not update the stored estimate.
The existing `STALLED` fault is left for a future stall cause (TMC
StallGuard).
Native tests: `tests/test_fps_arrival.c` (every cause and its travel, the
attempt bound, retract targets, half speed and doubled deadlines, success on
attempts 2 and 3, cancellation, a driver fault and the spent budget while
retracting).

## Step-estimated PTFE calibration

Parity finding P10: PTFE calibration required an encoder, HUB and toolhead switch,
so a BoxTurtle could not calibrate its path. A profile with both an encoder and
a toolhead switch keeps the existing encoder probe unchanged. It feeds from the
lane to the toolhead switch and reports encoder clicks. Otherwise, calibration
requires `fps_arrival` and runs these steps:

- Admission requires an empty path, BAY presence when configured, and the same
  fresh buffer as LOAD, below `fps_arrival_permille`.
- Motion uses LOAD's `FEED_TO_HUB` and `FEED_TO_BUFFER` stages, with their
  `hub_feed_timeout_ms` and `buffer_feed_timeout_ms` deadlines.
  `ptfe_cal_probe_timeout_ms` applies only to the encoder probe. Every
  [control rule](#control-rules) applies unchanged, including optional toolhead
  corroboration. A failure emits `PTFE_CAL_FAILED` with the stopping fault;
  calibration never retries.
  Calibration never emits LOAD_COMPLETE and never starts FOLLOW.
- On arrival, the result is the change in `current_position_steps`, counted in
  confirmed pulses rather than the commanded model, since the HUB observation.
  It is at least `fps_arrival_min_steps` and below `fps_arrival_max_steps`. The
  existing retract then runs until HUB (and a fitted toolhead switch) clear,
  within `ptfe_cal_retract_timeout_ms` (menu `Calibration retract timeout`;
  BoxTurtle 90 s, since the retract covers up to the 1900 mm travel limit).
- `PTFE_CAL_COMPLETE` carries that count and event flag
  `KM_EVENT_FLAG_STEP_ESTIMATE` (2). An encoder result keeps flag 0 and its
  click value. No other event carries this flag. Cancellation emits
  `PTFE_CAL_CANCELLED` with value 0 and stops outputs. If filament remains past
  HUB, the path stays reserved.

The count is an estimate, not a measured length. It includes:

- the HUB sampling latency;
- travel while the buffer moves to the threshold and through the hold;
- filament compression and feeder slip.

A jam or kink before the extruder can produce the same result. The firmware does
not report microstepping or rotation distance, so converting the count to
millimetres needs separately known feeder geometry.

Host handling:

- The legacy OpenAMS action encoder rejects the flagged event.
- Hosts without this change reject flag 2 as an unsupported event instead of
  reading it as clicks.
- The current host accepts the flag only on a successful CALIBRATE terminal.
  OpenAMS PTFE values are encoder lengths, so OpenAMS receives 0, its
  uncalibrated value, instead of the estimate.
- After the MCU ACK is confirmed, the optional `bowden_estimate_file` keeps the
  result. It is labeled as `feeder_microsteps` for the `hub_to_fps_arrival`
  span and tagged with the dictionary's integer `KLIPPER_MMU_*` constants and
  the DiffA/DiffB calibration file hash. See
  [host connection](../host/connection.md#step-estimated-bowden-length).
- That identity cannot detect changes to microstepping, feed speed or FPS
  thresholds, which the dictionary does not export (FPS arrival images export
  only the outer travel limits). Calibrate again after changing them.
- The value is used only to narrow the FPS travel window at the next start;
  see [defaults and bounds](#defaults-and-per-printer-bounds).
- `KLIPPER_MMU_CALIBRATE MMU=NAME BAY=n` runs it through the bound OpenAMS
  runtime ([host connection](../host/connection.md#step-estimated-bowden-length)).

This is software-only evidence. The following native scenarios in
`tests/test_fps_arrival.c` pass under the native sanitizers:

- admission;
- success with the exact completed pulse count;
- toolhead corroboration;
- HUB loss;
- off-path, invalid, fully compressed and stale buffer samples, and an
  uncompressed (0) reading treated as a release rather than a fault;
- maximum travel;
- the deadline;
- cancellation.

`tests/test_operation_core.c` checks that the encoder result is unflagged and
unchanged. Host decoding, OpenAMS value and persistence tests are in
`tests/klipper_event_reader`, `tests/klipper_event_delivery`,
`tests/klipper_openams_operation_bridge/test_calibrate.py` and
`tests/klipper_host/test_bowden_estimate.py`. No physical calibration has been
qualified.

## Calibration is required before LOAD

An FPS-arrival LOAD needs a calibrated HUB-to-arrival distance before it will
run at all. It is one measurement of the shared path from HUB onward, not
per lane, so calibrating any loaded bay once calibrates every lane. Without
one, `KLIPPER_MMU_LOAD_FILAMENT` (through OpenAMS) is refused with
`KM_REQ_UNAVAILABLE`; the log names the fix: run
`KLIPPER_MMU_CALIBRATE BAY=<any loaded bay>` once. `KLIPPER_MMU_CALIBRATE`
itself needs no prior calibration -- it is how you get one. Once it completes,
firmware adopts the measured distance immediately, so the very next LOAD in
that session, on any lane, uses it; nothing about it requires restarting
Klipper. Restoring the same distance from a previous session's saved
estimate (below) also counts, so a printer that has already been calibrated
once keeps working across restarts without repeating the probe.

## Calibrated LOAD speed profile

The owner's own words: calibrate at a slow, safe speed to find the distance
to the toolhead, then ramp up as far as the reverse-Bowden tube allows (never
past a fixed ceiling), then slow to a crawl before the toolhead. That is
exactly what an FPS-arrival LOAD does once a calibrated distance is known:

1. The lane-to-HUB stretch is unchanged -- short enough that a separate speed
   profile there would be effort spent on a few centimetres of PTFE. LOAD
   keeps today's `KLIPPER_MMU_FEED_SPEED_MM_S` there.
2. Past HUB, LOAD opens at `KLIPPER_MMU_LOAD_CRUISE_SPEED_MM_S`. This is a
   ceiling, not a target every LOAD must reach: a short path (a short-Bowden
   printer, or a calibration run gone wrong) never gets there and simply
   never leaves the acceleration ramp -- a triangular speed profile, not an
   error.
3. Every control tick, firmware checks whether the travel left to
   `calibrated distance - KLIPPER_MMU_LOAD_WINDOW_MM` is no more than the
   stopping distance from the current speed (the same stopping-distance math
   the rate core already uses to plan braking) plus a few ticks of slack.
   Once it isn't, LOAD retargets to `KLIPPER_MMU_LOAD_CRAWL_SPEED_MM_S` and
   never speeds back up -- the retarget happens once, not every tick, so the
   feeder actually decelerates instead of hunting.
4. `KLIPPER_MMU_CALIBRATE`'s own step-estimated probe runs the post-HUB leg
   at the same crawl speed throughout: calibration finds the distance slowly
   and deliberately, the way a first fitting should.
5. Arrival, JAM, `NO_PROGRESS`, `BUFFER_SIGNAL` and the follower hand-off keep
   every rule described elsewhere in this document; only *how fast the
   feeder gets there* changed.

BoxTurtle's defaults: cruise 200 mm/s (a ceiling, most loads will not reach
it), crawl 20 mm/s, arrival window ±50 mm, and 15 mm of usable buffer (FPS)
travel to brake within -- the same 15 mm the whole-stack sim uses
(`tests/linux_mcu/load_check.py`). A build-time check in
`mcu/feeder_mechanics.h` proves that braking from the crawl, after the
arrival sensor's own confirmation latency, always stops within half of that
remaining buffer travel; the same proof covers the follower's own feed speed.
Acceleration and braking both default to 500 mm/s²: brisk enough to shed
cruise speed before the arrival window, and about as brisk as BoxTurtle's
reference pancake NEMA motor is likely to tolerate without losing steps.

**200 mm/s needs hardware qualification before anyone trusts it.** BoxTurtle's
reference feeder is a pancake NEMA (1.8°, 200 full steps), a 50:10 gearbox and
about 22 mm of filament per output rotation. 200 mm/s at that mechanics is
roughly 2700 motor RPM -- fast enough that a pancake motor's torque is thin,
and braking at 500 mm/s² without losing steps is an open question until
someone measures it on real hardware. Qualify the cruise speed (and the
braking) for the fitted printer before trusting the default; lower both in
the menu if the motor cannot keep up.

## Defaults and per-printer bounds

| Menu value | Default | Reason |
| --- | --- | --- |
| Threshold | 700 permille | A LOAD ends once compression passes this point, well above the mid-range (500) that FOLLOW holds during a print; it must exceed 500 plus the default deadband (10). Leaves 30% of travel before the fully compressed end (1000), which fails LOAD. |
| Consecutive acquisitions | 3 | The crossing plus two confirmations, so one noisy pair cannot complete LOAD. |
| Hold | 20 ms | About 20 acquisitions at the 1 ms ADC pair and controller cadence, so it dominates the count. At BoxTurtle's 20 mm/s crawl (the speed LOAD is at by the time it nears arrival) it is 0.4 mm, 291 microsteps, far below the arrival deadline. |
| Minimum travel after HUB | 100 mm | BoxTurtle only; exported as 72,727 microsteps. |
| Maximum travel after HUB | 1900 mm (BoxTurtle), 2000 mm (every other catalogued device) | BoxTurtle's is the 1725 mm AFC reference Bowden plus 10%, exported as 1,381,817 microsteps. Every other device takes Happy Hare's own `bowden_homing_max` default (`docs/devices/*.json`, identical across the pinned dossiers) -- this option (`KLIPPER_MMU_FPS_ARRIVAL_MAX_MM`) moved out of the FPS-only menu block, since `KLIPPER_MMU_ARRIVAL_TIMEOUT`, `KLIPPER_MMU_RETRACT_TIMEOUT` and `KLIPPER_MMU_CAL_RETRACT_TIMEOUT` (`mcu/Kconfig.control`) now derive from it too, whether or not FPS arrival itself is selected. Custom stays 0 (unset). |
| LOAD cruise speed | 200 mm/s (BoxTurtle) | Ceiling for the mid-path run after HUB; see [above](#calibrated-load-speed-profile). |
| LOAD crawl speed | 20 mm/s (BoxTurtle) | Final approach and `KLIPPER_MMU_CALIBRATE`'s own probe speed. |
| LOAD arrival window | ±50 mm (BoxTurtle) | Arrival is accepted within the calibrated distance plus or minus this. |
| Buffer travel | 15 mm (BoxTurtle) | Total FPS travel from rest to fully compressed; the build-time braking proof uses it. |

The first three are MMU/FPS properties and apply to every device; the menu no
longer accepts 0 for them. Travel uses the pinned AFC geometry: 200 full steps,
16 microsteps, a 50:10 gearbox and 22 mm of filament per output rotation,
727.27 microsteps/mm. The minimum stays 0 (unset) on every device but
BoxTurtle, and FPS arrival's own startup still fails until it is entered;
the maximum is no longer 0 elsewhere (see the table above), but FPS arrival
itself still needs a real minimum too, so selecting it on a device other
than BoxTurtle without also setting one still fails startup exactly as
before. The
sample count and hold tolerate noise on the long differential line; the
firmware reads DiffA and DiffB one after the other, not simultaneously, so a
disturbance can corrupt a single pair. None of these values is physically
qualified.

The min/max travel values are the MMU's outer limits (A4); they never narrow.
What narrows arrival to a specific printer's fitted Bowden is the calibrated
distance itself, restored from a saved estimate as follows:

- Images with FPS arrival export the outer limits as `KLIPPER_MMU_FPS_MIN_STEPS`
  and `KLIPPER_MMU_FPS_MAX_STEPS`.
- Before `finalize_config`, the host may send
  `config_km_fps_steps oid=%c steps=%u` carrying a calibrated HUB-to-arrival
  distance. Firmware accepts a nonzero value only within the outer limits (0
  means "no calibration"); an out-of-range nonzero value shuts down with
  `Invalid FPS calibrated travel`, and after `finalize_config` the oid is
  refused. It is a config command, so a changed distance takes effect through
  Klipper's configuration CRC and an MCU restart -- but the *first* LOAD to
  use a freshly measured distance needs no restart at all, because
  `KLIPPER_MMU_CALIBRATE` already handed it to the running firmware directly.
- The host sends it only when `bowden_estimate_file` holds a step estimate
  for this MMU name, the connected dictionary's `KLIPPER_MMU_*` constants and
  the current `diff_calibration_file` hash, and that estimate still falls
  within the outer limits. Otherwise nothing is sent, and LOAD stays refused
  until `KLIPPER_MMU_CALIBRATE` runs again this session.

To calibrate a printer:

1. Configure `diff_calibration_file` with the unit's measured DiffA/DiffB
   calibration and set `bowden_estimate_file`.
2. Run `KLIPPER_MMU_CALIBRATE MMU=NAME BAY=n`. It feeds past HUB at the crawl
   speed until FPS arrival, within the outer limits, and reports the
   microsteps. LOAD needs no prior calibration to run this.
3. The host stores the ACK-confirmed estimate, and firmware has already
   adopted it for this session: the next LOAD on this lane's shared path can
   run immediately.
4. The next Klipper start restores the same distance from the saved file, so
   LOAD keeps working without recalibrating. Arrival is then accepted within
   the calibrated distance plus or minus the configured window.
5. Calibrate again after changing the Bowden, the calibration file or the
   firmware. A changed tag drops the stored estimate and LOAD is refused
   again until the next `KLIPPER_MMU_CALIBRATE`.

## MCU configuration

Menu (`mcu/Kconfig.control`):

- `End LOAD on calibrated DiffA/DiffB buffer position (FPS arrival)`
  (`KLIPPER_MMU_FPS_ARRIVAL`) is off by default and requires
  `Paired analog DiffA/DiffB buffer`.
- `Local toolhead arrival switch` (`KLIPPER_MMU_TOOLHEAD_SENSOR`) is shown
  only while FPS arrival is selected. Otherwise it stays on, so a switched
  buffer or an analog buffer without FPS still needs the switch.
- Under `Motion and driver defaults`, the threshold, consecutive acquisitions,
  hold, minimum and maximum travel map to the profile fields, with the
  [defaults above](#defaults-and-per-printer-bounds). `km_config_profile`
  passes them through, with host-narrowed travel bounds if delivered, and
  `km_profile_validate` rejects any unset or contradictory value.

Binding:

- Without a toolhead switch, the toolhead pin and its electrical settings are
  hidden. `km_config_pins` has no toolhead group, claim or
  `RESERVE_PINS_klipper_mmu_toolhead`, so board startup does not reserve it.
  `km_config_switches` leaves `toolhead.pin` as `KM_SWITCH_PIN_UNUSED`, and
  `has_toolhead_sensor` is false.
- `km_switch_gpio_setup` accepts a missing toolhead switch only with
  `fps_arrival` and an unassigned pin. It then configures no GPIO for it, and
  the sampler always publishes toolhead absent. A fitted switch keeps the
  5 ms debounce, freshness and invalidation of the other presence inputs.
- Sampler inputs are ordered bays, HUB (physical only), toolhead (if
  fitted), buffer low/high (switched only), then LOAD by lane. Each group is
  read at its own computed offset.
- Headers generated before this option lack these symbols. The C fallbacks
  keep the toolhead switch and leave FPS arrival off. `km_config_profile`
  rejects a header that removes the switch without FPS arrival.
- Acquisition is unchanged. `km_sensors_sample` publishes the latest ADC pair
  through `km_buffer_update_adc_pair`, dated from its original A-attempt time
  in milliseconds. Republishing the same pair keeps that time and adds no
  evidence. Two acquisitions in the same millisecond count as one.

Host projection: `project_sensor_presence` (OpenAMS projection) requires bays
and HUB to be configured, valid and fresh, and a configured toolhead switch
likewise. An unconfigured toolhead projects as `None`, not as an absence;
path ownership then relies on HUB alone, as the firmware does, so an empty path
or a loaded known lane can still be proven and a HOST session started. The host
is not told whether FPS arrival is configured; the firmware admits LOAD without
a toolhead switch only when it is.

Focused tests: `tests/test_config.py` (menu to profile, old headers),
`tests/test_pin_config.py` (claims, switch settings, H723 resources) and
`tests/klipper_switch/test_switch.c` (physical/virtual HUB, with and without
LOAD, with and without toolhead, rejection without FPS). The composed
startup/controller LOAD using production ADC samples is in
[composed checks](fps-composed-checks.md).

## Software review evidence

The full portable core suite passes under ASAN/UBSAN with FPS disabled by
default, including the 13 focused FPS scenarios. Root added a delayed-release
regression: the original arming condition fails it, and the acquisition-after-
minimum-travel guard passes. Evidence is retained in
`build/review-fps-integrated.log` and `build/fps-delayed-sample-proof/before.log`.
These checks do not close the production integration gates above.

The integrated MCU/menu review passes 16 actual-upstream profile tests, 64
pin/resource factory tests and all 26 switch/controller scenarios under the
native sanitizers. Logs are retained as `build/review-fps-menu-{config,pins,switch}.log`.
These checks validate configuration and binding; the composed ADC LOAD is
covered separately.
