# Implementation status

Updated 2026-09-27 UTC. The objective is buildable software qualification firmware
for all major STM32 families and all MMUs supported by the pinned AFC and Happy
Hare source inventory. Physical qualification is separate. Each device needs
its actual mechanisms and host integration, not only a catalogue entry. The
rollout tracks both coverage axes.

## Where klipper-mmu stands (2026-09-27)

klipper-mmu is a Klipper MCU add-on: driver ownership, motion and protection
stay on the MCU, the host plans and observes. Every device this project has
taken on from Happy Hare's and AFC's inventory is now an H723 software
candidate on one shared, data-driven control core -- no per-device controller
copies. Twenty-two devices/variants are implemented:

- **Independent-feeder devices** (`KLIPPER_MMU_SELECTOR_KIND_NONE`): BoxTurtle
  (4/8 lanes, powered rewind and an analog DiffA/DiffB buffer), NightOwl
  (passive, 2/4 lanes), EMU (5 lanes, a virtual hub from each lane's own LOAD
  switch), KMS (4 lanes, virtual hub, the only no-selector device with both
  per-lane BAY and LOAD switches), QIDI Box (4 lanes, a real shared hub, no
  shared TMC UART -- below), and the three no-BAY devices Angry Beaver, 3MS
  and QuattroBox 1.0/1.1/2 (4 lanes each).
- **LINEAR selector** (stepper carriage plus grip servo): ERCF 1.1 (9 lanes),
  ERCF 2.0 (8), ERCF 3.0 (8, also Jack Rabbit's servo-less variant of it) and
  Tradrack (10 lanes).
- **ROTARY selector** (stepper axis, per-gate gear direction): 3D Chameleon
  and HTLF (4 lanes each), MMX6 (6).
- **SERVO selector** (no selector stepper, one servo angle per gate): MMX
  (4 lanes), Low Rider (6), Pico MMU (4).
- **INDEXED selector** (no gate position table, per-gate index switches): BTT
  ViViD (shortest hop along its gate order) and Claymore (AFC's forward-only
  rule).
- Stock AMS units, OpenAMS units and the inline follower keep their own
  firmware and transport; klipper-mmu does not run on them.

All twelve selector devices have a hosted runtime proof:
`tests/klipper_selector_runtime` boots each selector image to READY with the
selector bound, homes it and lands one selection (`3bff463`). The
independent-feeder devices each have their own hosted runtime harness:
`tests/klipper_nightowl_runtime` and `tests/klipper_emu_runtime` predate this
wave; the six devices it added -- KMS (virtual hub) and the no-BAY Angry
Beaver, 3MS and QuattroBox 1.0/1.1/2 -- share one parametrised
`tests/klipper_passive_runtime` instead of six copies. QIDI Box's hosted proof
is its own release-matrix row and `KLIPPER_MMU_QIDI_BOOT_GATES` diagnostic-
startup admission rather than a dedicated runtime harness. Twenty built images
also run end to end in the whole-stack simulator
(`tests/linux_mcu`, `make -C tests/linux_mcu test-devices`): nightowl, ercf_2_0,
chameleon_3d, mmx, vivid, claymore, emu, qidi_box, kms, angry_beaver, 3ms,
quattrobox_2 (its sim run stands in for QuattroBox 1.0/1.1 too -- identical
mechanics, layout and `steps_per_mm` per their dossiers), ercf_1_1, ercf_3_0,
jack_rabbit, tradrack, htlf, mmx6, low_rider and pico_mmu, each proving
PRELOAD/CALIBRATE/LOAD/extrude/UNLOAD (and, where the device supports it,
reselect) against its own dossier. BoxTurtle keeps its own separate
`make check-integration` whole-stack cycle. The STM32 release matrix
(`tests/release_matrix/run.py`) builds this catalogue across the project's
H723, F207, F405/F407, F429, F765, G431/G474, H743/H750 and G0B0/G0B1 targets;
its last full run passed 129 configurations and saved 104 images across every
target.

Notable capabilities landed in this wave: ERCF 2.0's filament encoder now runs
in the whole-stack sim, with distance-based clog detection replacing a
750&nbsp;ms stall window that false-faulted slow following -- a stall is now no
encoder click over 10&nbsp;mm of gear travel, Happy Hare's own
`detection_length` (`4383235`). Selector gate calibration
(`KLIPPER_MMU_CALIBRATE_SELECTOR`/`KLIPPER_MMU_MOTORS_OFF`/`KLIPPER_MMU_SERVO`)
is one generic, kind-agnostic command for every stepper (LINEAR/ROTARY) and
SERVO selector, saved to an opt-in `selector_calibration_file`; INDEXED needs
none of it, since its gates are found by switch, not measured. Every device
now takes its own HUB/arrival/retract/rewind/calibration deadlines and its own
feed/retract speeds and gearing from its dossier (AFC's own numbers for
Claymore) instead of BoxTurtle's values or a generic crawl (`abf4916`,
`089ca0d`). Devices with no per-lane BAY switch (Angry Beaver, 3MS, QuattroBox
1.0/1.1/2) load through OpenAMS the Happy Hare way: an unknown gate is
attempted, the LOAD's own outcome marks it available or empty, and
`KLIPPER_MMU_SET_GATE_METADATA` records it (`eee643a`). QIDI Box's gear has no
shared TMC UART at all -- Happy Hare wires it plain STEP/DIR -- so
`mcu/board_startup.c` now binds a plain `km_stepdir_gpio` driver instead of
refusing the device outright (`44d97cb`). Angry Beaver and ERCF 2.0/3.0 now
also build on Happy Hare's real MMB 2.0 board (STM32G0B0/G0B1), fitting its
128&nbsp;KiB flash with `KLIPPER_MMU_SIZE_OPT_BUILD`'s `-Os` on klipper-mmu's
own object files only (`014a0d4`). EMU's whole-stack sim now runs all five of
its lanes, with the sim plant and TMC model sized from the image's own lane
count instead of a fixed four (`88cdfae`).

The sims found real bugs, each fixed with a regression test: a selection phase
(homing, a selector move or a servo dwell) was silently deducted from the next
stage's own timeout, so Pico MMU's first PRELOAD after calibration could starve
its own budget (`e718452`/`515fa7e`); `host/requests.py`'s LOAD/UNLOAD/CALIBRATE
path capped lanes at 8, a leftover from before any 9-12 lane device existed, so
a real one (ERCF 1.1, Tradrack) could PRELOAD a high lane but never LOAD,
UNLOAD or CALIBRATE it, and three call sites looked up selector commands by a
bare name instead of Klipper's full wire format, so calibrated gate positions
never reached the firmware on any selector device (`6d21319`); the sim's
shared-path encoder pin collided with a 9-lane device's own BAY switch, and the
plant answered the encoder on every four-lane image regardless of whether that
image actually had one, silently disabling lane 1's TMC UART (`bec86d2`); the
host's device-identity table was missing Low Rider, Pico MMU, QIDI Box, BTT
ViViD and Claymore entirely (no motion session at all), a forward-only INDEXED
selector faulted on a switch already pressed at the start of a search, and
every selector search discarded its caller's position baseline, so a
forward-only selector's ever-growing step count eventually tripped the travel
bound (`fc25560`).

- **Per-lane rotation distance.** Each lane may have its own feeder rotation distance (Happy Hare's variable_rotation_distances, AFC's per-lane rotation_distance): `feeder_rotation_distances` on the host or `KLIPPER_MMU_FEEDER_LANE_ROTATION_DISTANCES_UM`, 0.8-1.25x of the unit value. The trajectory scales every position, speed and limit for the active lane; `KLIPPER_MMU_CALIBRATE BAY=n SAVE=0` measures a lane against the stored Bowden length and suggests its value. Native, host and a BoxTurtle whole-stack test (an 8% gear on an 800 mm Bowden) cover it.

- **EJECT on units without BAY switches.** Angry Beaver, 3MS and QuattroBox 1.0/1.1/2 eject the Happy Hare way: one blind move back by `KLIPPER_MMU_EJECT_FINAL_DISTANCE_MM` (20 mm; 1500 mm on 3MS, from each dossier's gate_final_eject_distance) from where UNLOAD parked the lane, then the host marks the gate empty. Units with BAY switches keep the sensor-ended EJECT. Every whole-stack device sim now ejects; the three no-BAY sims check the motor stepped exactly that distance.

Open, honestly:
- **No hardware qualification anywhere.** Every device above is a software
  candidate against example wiring; none has been flashed to or measured on its
  own MMU hardware (the Nucleo H723 bench is flashed only for boot, link and
  timing checks).
- **EMU's intermittent UNLOAD terminal race: fixed** (`87153eb`). A
  virtual-hub device's LOAD switch is its hub, and the host read a capture
  as 'unknown' while feeding toward or retracting past it; one such tick
  made OpenAMS resync mid-operation and drop the op the firmware's
  successful terminal belonged to. The host now reads that switch
  correctly, OpenAMS's monitor resync keeps an in-flight op (fork commit
  `c560655`), and a waiter the host cannot serve fails with the recovery
  command named instead of hanging. EMU passed 10/10 and KMS 4/4
  whole-stack runs afterwards (2 failures in 14 before).
- **Sim configs that diverge from real hardware.** KMS's and NightOwl's sim
  fragments use the bench's real DiffA/DiffB analog-buffer pins for FPS
  arrival, because neither dossier wires a toolhead switch and
  `km_config_profile()` requires one or the other; EMU's and KMS's sim
  fragments locally raise `KLIPPER_MMU_RETRACT_TIMEOUT` to 90000&nbsp;ms to fit
  this rig's shared HUB-to-arrival geometry -- a sim-config choice, not a
  measured device fact.
- **Selector calibration now has whole-stack coverage for LINEAR/ROTARY.**
  ERCF 1.1/2.0/3.0, Jack Rabbit, Tradrack, 3D Chameleon, HTLF and MMX6 each
  run `KLIPPER_MMU_CALIBRATE_SELECTOR` end to end in their whole-stack sim and
  check the measured travel against the compiled gate position, alongside the
  SERVO angle coverage MMX/Low Rider/Pico MMU already had. This caught a real
  firmware bug: the LINEAR/ROTARY home endstop shared the ordinary 5&nbsp;ms
  presence-switch debounce (mcu/switch_gpio.c), adding enough homing overshoot
  at selector speed to miscalibrate a gate outside the sim's 200&nbsp;um
  tolerance; it now qualifies in about 1&nbsp;ms (two consecutive 1&nbsp;ms
  samples), and every other switch is unaffected. Tradrack has no optional
  encoder implemented (none is wired in its traced dossier). Claymore's
  forward-only INDEXED kind has no calibration operation at all by design --
  there is no gate order or switch width to measure into.
- Occasional sim-MCU stalls of 20-60&nbsp;ms remain unreproduced on a quiet
  machine; whole-stack harnesses retry once when a failed run's own gap report
  shows one.

Historical checkpoints predating this wave are in the
status history.

## Current core limits

One shared feed path per lane; up to eight distinct lane feeders on
non-selector devices, or up to twelve (`KM_MAX_LANES`) on the LINEAR, ROTARY,
SERVO and INDEXED selector devices described above.

- **LOAD** ends on the unit's own board: at a local toolhead switch, or when
  the calibrated FPS buffer compresses inside the arrival window around the
  measured Bowden length (+/-50 mm on BoxTurtle). Buffer pressure alone never
  counts as arrival. An FPS-arrival LOAD is refused until the Bowden has been
  measured.
- **PRELOAD** stops at the lane's LOAD switch where it has one; EMU and KMS
  back off behind that switch, their virtual hub boundary; a device with no
  LOAD switch parks behind its real HUB (Happy Hare's `gate_parking_distance`
  shape), and a selector machine then releases the shared path. The final
  parking position has no measured offset guarantee.
- **UNLOAD** waits for hub and arrival clearance, brakes, then travels the
  configured post-HUB distance before releasing the shared path. BoxTurtle's
  distance is still unmeasured on fitted hardware.
- **EJECT** reverses until fresh BAY-clear evidence on units with BAY
  switches; units without them back out a set distance, as Happy Hare does.
  Neither has physical qualification.
- **Calibration** is kept by the host and restored at every restart: the
  Bowden length (`bowden_estimate_file`, measured by `KLIPPER_MMU_CALIBRATE`
  from FPS arrival or the encoder), selector gates (`selector_calibration_file`)
  and per-lane rotation distance (`feeder_rotation_distances`). Storing it in
  the MCU's own flash is post-v1.
- ADC path validation cannot prove every open/short fault: valid physical
  endpoints can lie on the electrical rails.

The eight-entry terminal history supports recent replay; older request IDs are
rejected by per-client session/request high-water marks. Session and request
numbers must increase without wrapping; begin a newer session before request
exhaustion. After a firmware restart the host restores calibration from its
files, and path ownership is re-established with `KLIPPER_MMU_RECONCILE_PATH`.
No stale identity is inferred from an 8-bit alias.
