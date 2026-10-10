# BoxTurtle quick start

One path from a built BoxTurtle with an AFC Lite (STM32H723) board and
OpenAMS to a first successful LOAD, plus what to do when something goes
wrong.

Most unfinished BoxTurtle kits fail at bring-up, not at the printer: a
switch wired with the wrong polarity, a floating input with no pull-up, a
reversed feeder motor, two lanes swapped at the AFC board, or a string stuck
in the hub. Step 7 (guided commissioning) checks every switch, every lane
and the FPS buffer before you load filament, and explains each fault in
plain language instead of failing partway through a print.

This is a condensed path for the BoxTurtle/AFC Lite/OpenAMS combination, not
a replacement for the referenced docs. Every command below is read from
`host/__init__.py`; every menu option is read from `mcu/Kconfig*`. For
every command and its parameters, see the
[G-code command reference](g-code-commands.md).

## 1. What you need

- A built BoxTurtle with an AFC Lite (STM32H723) board, wired per the
  [manufacturer's manual](https://raw.githubusercontent.com/xbst/AFC-Lite/refs/heads/master/Docs/AFC-Lite_Manual.pdf)
  and the [BoxTurtle H723 bench acceptance](boxturtle-h723-acceptance.md).
- A Klipper checkout, plus this `klipper-mmu` package
  ([install](install.md)).
- OpenAMS, which this repository contains
  ([OpenAMS integration](../integration/openams/README.md)).
- A Raspberry Pi or other host running Klipper/Moonraker.

The AFC Lite firmware is one of four images, saved by the
[release matrix](../tests/release_matrix/README.md):

- `boxturtle-h723-usb` / `boxturtle-h723-can`: actuator-permitted, for the
  board you will run. USB uses PA11/PA12 with no bootloader offset; CAN uses
  PB8/PB9 with a 128 KiB offset. Pick the transport your board is wired for.
- `boxturtle-h723-usb-safe` / `boxturtle-h723-can-safe`: same wiring and
  startup, but diagnostic-only. Actuator START is refused and a
  receive-only raw DiffA/DiffB probe is compiled in for calibration.
  **Build and bench-check this one first**; only build the actuator-permitted
  image once the safe image's checks (sections 5-6) pass.

All four end LOAD on FPS arrival (the OpenAMS Hall-effect buffer) instead of
a toolhead switch; see [FPS arrival](fps-arrival.md). None has passed
physical qualification — see [section 11](#11-what-is-not-yet-qualified).

## 2. Build

Use a disposable Klipper checkout, not the one behind your running printer,
so its `.config` and build output stay separate:

```sh
git clone https://github.com/Klipper3d/klipper /path/to/klipper
cd /path/to/klipper
git checkout ce7002bedf37e938bb483572949f3703ac6476cb
```

From the `klipper-mmu` package directory, install the package hooks and the
explicit driver-API extension (local TMC UART, paired PWM and paired ADC
access; required for STEP/DIR, powered rewind and DiffA/DiffB on this
board):

```sh
python3 tools/install.py install /path/to/klipper
python3 tools/install.py install-driver-api /path/to/klipper
```

This package ships a maintained Kconfig fragment for each of the four
images under [`configs/`](../configs). Copy the `-safe` one first, then let
`olddefconfig` fill in every other symbol from its Kconfig default:

```sh
cd /path/to/klipper
cp /path/to/klipper-mmu/configs/boxturtle-h723-usb-safe.config boxturtle.config
make KCONFIG_CONFIG="$PWD/boxturtle.config" OUT=out-boxturtle/ olddefconfig
make KCONFIG_CONFIG="$PWD/boxturtle.config" OUT=out-boxturtle/ -j4
```

(Use `boxturtle-h723-can-safe.config` for a CAN board.) Each fragment is
exactly what the [release matrix](../tests/release_matrix/README.md) builds
and tests under that image name (`tests/release_matrix/run.py`), so this
reproduces a tested configuration instead of a hand-walked menu. To
customize wiring (a different pin, polarity or pull), run
`make KCONFIG_CONFIG=... OUT=... menuconfig` afterward: **klipper-mmu >
Device, board and lanes > Configure and reserve MMU pins** holds every pin,
and the AFC Lite preset's connector map is in the
[wiring table](../tests/release_matrix/README.md).

Once the `-safe` image has passed the checks in sections 5-6, repeat with
`boxturtle-h723-usb.config` (or `-can`) to get the actuator-permitted image.

## 3. Flash

Follow Klipper's own [DFU](https://www.klipper3d.org/Installation.html) or
[Katapult](https://github.com/Arksine/katapult) flashing instructions for the
AFC Lite's STM32H723 and your chosen transport (USB or CAN). This repository
does not add a flashing tool; only the firmware built above.

## 4. Host

On the machine running Klipper, install the host extra into the same
Klipper checkout your printer actually runs (do this on your live checkout,
not the disposable one from step 2):

```sh
python3 tools/install.py install-host /path/to/real/klipper
```

Start with the smallest section that works with the `-safe` image's raw
DiffA/DiffB probe (section 5 needs exactly this):

```ini
[klipper_mmu box]
mcu: mcu
diff_probe: True
```

The following sections add to this config in stages. Every option name is
read directly from `host/__init__.py`'s `config.get(...)` calls:

- `diagnostic_startup`, `diff_calibration_file` (section 5): hosted board
  startup and the measured DiffA/DiffB calibration record.
- `bowden_estimate_file` (section 8): the stored Bowden-length estimate.
- `openams_motion`, `openams_idx`, `openams_fps`, `openams_family`,
  `openams_bays`, `openams_path_length_mm`, `openams_host_watchdog_ms`,
  `openams_lease_max_age_ms` (section 7): OpenAMS topology and the guarded
  motion lease.
- `path_reconciliation`, `driver_restart` (section 10, optional but
  recommended): enable `KLIPPER_MMU_RECONCILE_PATH` and
  `KLIPPER_MMU_RESTART_DRIVER`.

Restart Klipper after every config change below.

## 5. FPS (DiffA/DiffB) calibration

The FPS is the OpenAMS Hall-effect buffer, read as a differential pair on
SW1/PC4 (DiffA) and SW2/PC5 (DiffB). It reports compression only: 0 at rest
(slack or tension look the same), rising toward 1000 as filament is
compressed against the stationary extruder. FOLLOW holds it near 500 during
a print. LOAD ends once it crosses 700 for several samples in a row. See
[FPS arrival](fps-arrival.md).

Firmware refuses to finish configuration with the analog buffer compiled in
until a calibration is delivered (`mcu/config.c`: `DIFF_BUFFER && !calibration`
returns `UNAVAILABLE`), so calibrate before turning on `diagnostic_startup`.
With the `-safe` image flashed, motor power isolated and the probe-mode
config from section 4:

```
KLIPPER_MMU_QUERY MMU=box
```

should report `state=attached`, `connected=True` (probe mode never sends
`diagnostic_startup`, so `startup_state` is unused here — that is expected).
Then follow the full procedure in item 2 ("Electrical inputs") of
[BoxTurtle H723 bench acceptance](boxturtle-h723-acceptance.md); summary:

1. At 2 to 8 known, manually-positioned buffer locations (including the two
   physical ends), run `KLIPPER_MMU_DIFF_PROBE MMU=box` and record the
   printed `a_raw`, `b_raw` and timestamps.
2. Build a JSON calibration record with the exact keys from
   [host DiffA/DiffB calibration](../host/calibration.md#record):
   `channel_map` (`a_low_b_high` or `b_low_a_high`), `vref_mv`,
   `min_valid_mv`, `max_valid_mv`, `path_tolerance_counts`, and `points`
   (each `a_raw`, `b_raw`, `position_permille`, endpoints at 0 and 1000).
   Do not use the synthetic example values from that file or from the
   tests; they are not a measurement.
3. Save it to an absolute path, reset the MCU (probe mode claims the ADC
   pair until reset), then replace `diff_probe: True` with:

   ```ini
   diagnostic_startup: True
   diff_calibration_file: ~/printer_data/config/box-diff-calibration.json
   ```

Restart Klipper, then continue to section 6 to confirm startup reached READY.

## 6. First boot check

With the calibration loaded (section 5), check the connection:

```
KLIPPER_MMU_QUERY MMU=box
KLIPPER_MMU_SENSORS MMU=box
KLIPPER_MMU_DIAGNOSE MMU=box
```

`KLIPPER_MMU_QUERY` should now report `state=attached`, `connected=True`,
`startup_state=ready` and `startup_result=0`. `KLIPPER_MMU_SENSORS` lists
every configured switch's state. With nothing wrong, `KLIPPER_MMU_DIAGNOSE`
prints:

```
klipper_mmu box: no problems found; state=idle path_occupied=False hub=open
```

Anything else is a `Finding` line naming the switch or condition, what it
means, and what to change — the same rules used in step 7.

## 7. Guided commissioning

`KLIPPER_MMU_COMMISSION` guides you through per-switch, per-lane and FPS
checks. Source: [diagnostics](diagnostics.md).

1. **Switches.** Remove all filament, then:

   ```
   KLIPPER_MMU_COMMISSION MMU=box STEP=switches
   ```

   Every switch reads `open` (OK), `made` (nothing inserted — likely
   inverted polarity: flip that pin's `*_ACTIVE_HIGH` value, the `!`
   convention, or check for leftover filament), or `unknown` (a floating
   input — set its `*_PULL` field to add a pull-up, the `^` convention, or
   check the connector).

2. **FPS.** Push the buffer fully, then release it:

   ```
   KLIPPER_MMU_COMMISSION MMU=box STEP=fps
   ```

   Samples for about 10 s. Never moved: DiffA/DiffB wiring, the FPS cable,
   or a displaced magnet. Did not reach 900: recalibrate, or the buffer
   lacks full travel. Did not return near its start: a sticking slide. High
   before any push: an inverted calibration.

3. **Each lane.** This step drives motion (PRELOAD/EJECT), so it needs the
   actuator-permitted image and `openams_motion: True`: firmware
   `KLIPPER_MMU_ACTUATION=1`, `diagnostic_startup` (already on) and the
   lease bounds below — see
   [OpenAMS integration](../integration/openams/README.md) and
   [host connection](../host/connection.md). Build and flash
   `boxturtle-h723-usb.config` (or `-can`, section 2) once the `-safe`
   image's checks above pass, then add to `[klipper_mmu box]`:

   ```ini
   openams_motion: True
   openams_idx: 1
   openams_fps: fps0
   openams_family: boxturtle
   openams_bays: 4
   openams_path_length_mm: 842.5
   openams_host_watchdog_ms: 2000
   openams_lease_max_age_ms: 1000
   ```

   and add the OpenAMS side:

   ```ini
   [fps fps0]
   source_oams: klipper_mmu box
   extruder: extruder

   [oams_manager]

   [filament_group T0]
   group: box-0
   [filament_group T1]
   group: box-1
   [filament_group T2]
   group: box-2
   [filament_group T3]
   group: box-3
   ```

   Notes:

   - `openams_host_watchdog_ms` must equal the firmware's exported
     `KLIPPER_MMU_HOST_LEASE_MS` (2000 for the default BoxTurtle build; see
     [host lease bounds](host-lease-bounds.md)); `openams_lease_max_age_ms`
     must be smaller.
   - `openams_family: boxturtle` must match firmware `KLIPPER_MMU_DEVICE_ID`
     (1); `openams_bays` must match `KLIPPER_MMU_LANE_COUNT` (4 by default).
   - `openams_path_length_mm: 842.5` above is the placeholder from
     [integration/openams/README.md](../integration/openams/README.md), not
     a measurement. Replace it with your BoxTurtle's actual measured
     HUB-to-extruder PTFE path length in mm; until you have measured it, use
     your best measured estimate of that path. `KLIPPER_MMU_CALIBRATE`
     (section 8) reports that span directly in millimetres (BoxTurtle's
     exported `KLIPPER_MMU_FEEDER_STEPS_PER_M`, 727,272, converts its
     feeder-microstep measurement); use its reported mm value here.

   Restart Klipper, then for lane `n` (0..3):

   ```
   KLIPPER_MMU_COMMISSION MMU=box STEP=lane LANE=n
   ```

   Insert filament until the BAY switch clicks (60 s timeout). If a
   *different* lane's switch changes first, those two lanes are wired
   swapped at the AFC board and nothing moves. Otherwise it runs PRELOAD
   (failure means the feeder motor is reversed, not gripping, or the LOAD
   switch is bad) then EJECT and asks you to watch the spool. Answer:

   ```
   KLIPPER_MMU_COMMISSION MMU=box STEP=lane LANE=n ANSWER=yes
   ```

   (or `ANSWER=no` if the spool did not wind back — likely a reversed or
   slipping N20 respooler).

## 8. Bowden calibration

With an empty path and `bowden_estimate_file` configured:

```
KLIPPER_MMU_CALIBRATE MMU=box BAY=n
```

This feeds bay `n` past HUB, at the crawl speed, to FPS arrival and reports
the measured HUB-to-extruder length in feeder microsteps and, since
BoxTurtle exports a nonzero `KLIPPER_MMU_FEEDER_STEPS_PER_M`, also in
millimetres — use that mm value for `openams_path_length_mm` above. It is one
measurement of the shared path from HUB onward, not per lane, so any loaded
bay calibrates every lane. LOAD is refused
(`run KLIPPER_MMU_CALIBRATE BAY=<any loaded bay> once`) until this succeeds;
firmware adopts the measured distance immediately, so the very next LOAD in
this session, on any lane, already uses it. The confirmed estimate is also stored
in `bowden_estimate_file`, so the next Klipper restart restores the same
calibrated distance without recalibrating. A later LOAD arrives when it
reaches that distance within the configured arrival window
(`KLIPPER_MMU_LOAD_WINDOW_MM`, ±50 mm on BoxTurtle) and fails `NO_PROGRESS`
past it. Recalibrate after changing the Bowden path, the DiffA/DiffB
calibration file, or the firmware. See
[FPS arrival](fps-arrival.md#calibrated-load-speed-profile) and
[host connection](../host/connection.md#step-estimated-bowden-length).

## 9. First LOAD

Load a bay through OpenAMS:

```
OAMSM_LOAD_TO_TOOLHEAD GROUP=T0
```

A LOAD tries up to three times and stops after `KM_LOAD_BUDGET_MS` (240 s)
since admission. After the ACK, klipper-mmu logs a report explaining each
attempt: segment (lane to HUB, or HUB to extruder), cause, distance and the
next tactic. Example:

```
klipper_mmu box: lane 2 LOAD failed after 3 attempts
attempt 1, HUB to extruder: compressed at 312 mm past HUB (19% of the 1612 mm Bowden estimate), before the calibrated minimum: obstruction or kink in the Bowden, or a path shorter than calibrated (recalibrate); retract behind the HUB and retry
attempt 2, lane to HUB: no HUB after 523 mm: tip catching at the hub entry, spool snag, tangle or filament not gripped; back off and retry at half speed
attempt 3, HUB to extruder: no resistance within 1900 mm past HUB (118% of the 1612 mm Bowden estimate): lane drive slipping, PTFE disconnected between HUB and toolhead, FPS not responding (magnet, Hall or DiffA/DiffB wiring), or a path longer than the bound (recalibrate with KLIPPER_MMU_CALIBRATE)
```

A single successful attempt only logs; a retried or failed LOAD also prints
to the console. See [LOAD retries](fps-arrival.md#load-retries).

## 10. When something goes wrong

After a fault, in order:

1. `KLIPPER_MMU_DIAGNOSE MMU=box` — read the plain-language cause.
2. `KLIPPER_MMU_ACK_FAULT MMU=box` — acknowledge a retained `FAULT_ENTERED`
   or `FOLLOW_FAULT` event.
3. If a lane's TMC driver faulted, fix the physical cause (motor power,
   cable), then `KLIPPER_MMU_RESTART_DRIVER MMU=box LANE=n` until it reports
   ready.
4. `KLIPPER_MMU_RECONCILE_PATH MMU=box ACTION=CLEAR` if the path is empty,
   or `ACTION=ADOPT LANE=n` if lane `n`'s filament is still in it.
5. `KLIPPER_MMU_RECOVER_SESSION MMU=box` — releases the retired HOST session
   so a new one can be prepared.

This is the order [path reconciliation](path-reconciliation.md#from-a-stopped-fault)
documents ("acknowledge its event ... if a lane's TMC driver faulted ...
then `KLIPPER_MMU_RECONCILE_PATH` ... `KLIPPER_MMU_RECOVER_SESSION` then
releases the retired session") and the order
`tests/host_h723_command_join/run_join.py` exercises end to end
(`FOLLOW_FAULT` -> `ACK_FAULT` -> `RECONCILE_PATH ACTION=ADOPT` ->
`RECOVER_SESSION`).

OpenAMS latches a lane as faulted once it accepts a
firmware fault. Step 4's `RECONCILE_PATH` now also clears that OpenAMS latch:
once it clears a stopped fault and a fresh capture proves the firmware left
FAULT, it calls the bound OpenAMS runtime's verified
`accept_firmware_fault_cleared`, which this repository's OpenAMS provides, and
its response says `OpenAMS lane
cleared`. With an older OpenAMS that lacks
the hook, the response instead says to restart Klipper (`RESTART`, not
`FIRMWARE_RESTART`) before using that lane again; Klipper's own reconnect
prepares the adopted lane's session either way.

`ACK_FAULT` itself needs a current OpenAMS host. Older versions
paused from inside the command by queueing `PAUSE` behind the command that
was running it, so step 2 never returned. Now it pauses in place; an older
host makes `ACK_FAULT` fail with an error rather than wait forever.

| You see | Run | It means |
| --- | --- | --- |
| `KLIPPER_MMU_DIAGNOSE` finding, any time | `KLIPPER_MMU_DIAGNOSE MMU=box` | Read-only explanation of the current state; never moves anything, safe to run any time. |
| Retained `DRIVER_FAULT` | Power the motors, then `KLIPPER_MMU_RESTART_DRIVER MMU=box LANE=n` | Restarts that lane's TMC driver and clears its STEP/DIR latch. Never enables an output; the operation fault still needs RECONCILE_PATH. |
| Operation still faulted after a driver restart | `KLIPPER_MMU_RECONCILE_PATH MMU=box ACTION=CLEAR` | Clears a stopped operation fault once every event is ACKed and drivers are healthy. Never moves. |
| "More than one lane's LOAD made with HUB made" (unknown path) | `KLIPPER_MMU_RECONCILE_PATH MMU=box ACTION=ADOPT LANE=n` | Asserts which lane physically occupies the shared path, as an explicit operator statement. Never moves. |
| Retained `FAULT_ENTERED` or `FOLLOW_FAULT` | `KLIPPER_MMU_ACK_FAULT MMU=box` | ACKs the retained fault once a verified follower stop is proven. Does not clear the firmware fault or grant motion. |
| `HOST_COMM_LOST`, or a disconnect with no pending ACK/waiter | `KLIPPER_MMU_RECOVER_SESSION MMU=box` | Releases a clean retired HOST session so a new one can be prepared. Does not heartbeat or grant motion. |
| "Board-startup driver fault" (usually motor power was off at MCU boot, or a loose UART/motor cable) | Power on the motors, then `FIRMWARE_RESTART` | Reruns hosted diagnostic startup from a clean board reset. |

## 11. What is not yet qualified

This package is software-complete for the BoxTurtle/AFC Lite/OpenAMS path;
physical qualification is separate and still pending (parity review findings
A1, A4 and P10 in parity review). The motion
defaults you just built with are provisional starting points from the AFC
reference geometry, not measurements of your machine. They are millimetres,
the way Klipper asks for a stepper: 200 full steps (a 1.8 degree motor), 16
microsteps, a 50:10 gearbox and 22 mm of filament per output rotation, which
`mcu/feeder_mechanics.h` turns into 727.27 microsteps/mm at build time.

- Feed and retract speed 26 mm/s, acceleration 500 mm/s², deceleration
  500 mm/s² (`KLIPPER_MMU_FEED_SPEED_MM_S` and friends). LOAD cruises
  toward a 200 mm/s ceiling (`KLIPPER_MMU_LOAD_CRUISE_SPEED_MM_S`) and slows
  to a 20 mm/s crawl (`KLIPPER_MMU_LOAD_CRAWL_SPEED_MM_S`) before the toolhead.
  LOAD is refused until `KLIPPER_MMU_CALIBRATE` has measured the
  HUB-to-arrival distance.
- Jerk: automatic, 0 (`KLIPPER_MMU_JERK_MM_S3`), which is 20 x the lower
  acceleration here, 10,000 mm/s³ or 7,272,727 microsteps/s³.
- HUB and arrival deadlines (20 s to HUB, 118.75 s to arrival, 90 s to retract),
  and the 240 s whole-LOAD budget.
- The 65 mm post-HUB clearing distance (47,273 microsteps at the reference
  mechanics).
- FPS arrival travel bounds, 100-1900 mm after HUB (the 1725 mm AFC
  reference Bowden plus 10%). The outer travel bounds never narrow; instead,
  a calibrated LOAD uses the stored Bowden length estimate (step 8).

See [BoxTurtle reference motion budget](../tests/motion_budget/README.md)
for how these numbers were derived, and
[BoxTurtle H723 bench acceptance](boxturtle-h723-acceptance.md) for the full
staged bring-up (unpowered wiring, electrical inputs, diagnostic image,
actuator image) before running unattended prints.
