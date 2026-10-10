# The Linux MCU sim board: phase 1 (boot, identify, diagnose), phase 2a (TMC2209 + motion boot), phase 2b-1 (a filament plant, PRELOAD/UNPRELOAD/EJECT) and phase 2b-2a (a buffer, toolhead and extruder model, traced through a LOAD)

Klipper's real Linux MCU target runs klipper-mmu code "for Linux," which
proves nothing about the STM32H723 firmware anyone actually flashes. This
test instead builds **the release BoxTurtle H723 config, unchanged**, on a
test-only board directory (`simboard`), so the same `autoconf.h` symbols
(`MACH_STM32H723`, 520 MHz, both-edge stepping) drive the same add-on code
as the real image — and links it as a native process instead of an ARM
binary, because native processes are wonderfully easy to run without a
soldering iron. See `docs/plan/linux-mcu-integration.md` for the full plan;
this directory is step one of it.

`make -f tests/linux_mcu/Makefile test` runs eight gates, in order:

0. **`tmc2209_model_test/run.py` — the TMC2209 model alone.** Compiles
   `sim/board/tmc2209.c` natively (ASan/UBSan) against a tiny scheduler-free
   shim (`shim/`) plus the real firmware CRC/framing helpers it reuses
   (`src/tmc2209.c`, `src/tmc_uart_codec.c`) and drives it with synthetic
   edges (`test_tmc2209_model.c`) — no Klipper build, no klippy, no pty. Cheap
   and fast; run it first so a model bug fails in seconds, not minutes.
1. **`build.py` — the dictionary gate.** Builds two release fragments, each
   as an ARM/sim pair gated against its own twin: `configs/boxturtle-h723-usb-safe.config`
   (the receive-only default — actuator START refused, the only fragment
   that compiles the diagnostic-only `diff_probe` command `boot_check.py`
   needs) and `configs/boxturtle-h723-usb.config` (actuation-permitted,
   `motion_boot_check.py`'s target). For each pair it compares
   `klipper.dict`. Commands, responses and `static_string_id` are compared
   as sets — their numeric ids are link-order accidents, not protocol, so
   two builds that only differ in which files got linked first would fail a
   byte-equality check for no real reason. Every other enumeration (`pin`
   ranges, error codes, ...) and every config constant is compared exactly:
   those numbers are load-bearing. Differences are printed before the gate
   fails, so "what broke" doesn't require a debugger.
2. **`boot_check.py` — does the -safe image actually come up.** Starts
   `out-sim/klipper.elf` against a pty, installs the klipper-mmu host extra,
   boots klippy against a minimal generated `printer.cfg` (`[klipper_mmu box]`
   with `diff_probe: True`), and talks to klippy's API socket until it
   reports `ready`, then sends `KLIPPER_MMU_QUERY MMU=box` and checks it
   gets an answer instead of a fault. Both processes are killed either way.
3. **`motion_boot_check.py` — the actuation-permitted image, through real
   diagnostic startup.** Starts `out-sim-motion/klipper.elf`, writes a
   synthetic DiffA/DiffB calibration record (same shape as
   `tests/klipper_host_calibration_integration/run_tests.py`'s
   `make_record()`) to `build/linux-mcu/diff-calibration.json`, and boots
   klippy against `[klipper_mmu box]` with `diagnostic_startup: True` and
   `diff_calibration_file:` that path (`diff_probe` and `diagnostic_startup`
   are mutually exclusive, so this is a separate printer.cfg from
   `boot_check.py`'s). It subscribes to gcode output (respond_info text is a
   push notification, not the direct RPC reply), polls `KLIPPER_MMU_QUERY`
   until `startup_state` settles, asserts `ready`/`startup_result=0`, then
   asserts `KLIPPER_MMU_DIAGNOSE` reports "no problems found". Finally it
   greps the MCU log for the TMC2209 model's per-transaction evidence lines
   (see below) and asserts zero malformed/foreign frames and no repeated
   (direction, register) pair back to back on any lane — the model's
   evidence that zero UART retries occurred, tolerant of extra periodic
   post-ready diagnostic pairs that accumulate while this script polls.
4. **`preload_check.py` — a filament plant in the loop, driving real
   firmware motion through the real host extra and OpenAMS.** Starts
   `out-sim-motion/klipper.elf` with lane 0 pre-inserted up to its BAY
   switch (`sim/board/plant.c`'s `KM_SIM_PLANT` file; see "The filament
   plant" below), grants it real-time (`SCHED_FIFO`) scheduling when
   possible (see "Real-time priority" below), and boots klippy against
   `[klipper_mmu box]` with `openams_motion: True` and the OpenAMS lease
   options from `docs/boxturtle-quick-start.md` section 7. OpenAMS itself
   (the ams-family fork, pinned at `b4ba938`) is extracted with `git
   archive` into `build/linux-mcu/openams-src` and its `src/*.py` symlinked
   into `klippy/extras` — the fork's own repository is never modified. It
   then drives `KLIPPER_MMU_PRELOAD MMU=box BAY=0` through the real host
   extra to its terminal event, asserting both the firmware's own
   event/result code (`host/wire.py` `EventType`/`OpCode`) and the plant's
   independent ground truth (position, switch levels, steps seen, direction
   changes — written by `sim/board/plant.c` at exit or `SIGUSR1`), then does
   the same for `KLIPPER_MMU_UNPRELOAD` and `KLIPPER_MMU_EJECT`. On any
   failure it also fetches `KLIPPER_MMU_DIAGNOSE` for extra evidence before
   raising.

   Current status: all three pass end to end, firmware and plant ground
   truth agreeing at every step. On lane 0, starting inserted up to its BAY
   switch: `PRELOAD` feeds forward to `bay=1 load=1 hub=0` (tip resting
   just past the LOAD switch); `UNPRELOAD` retracts to `bay=1 load=0
   hub=0` (tip back between BAY and LOAD); `EJECT` retracts further to
   `bay=0 load=0 hub=0` (tip fully withdrawn past BAY). Two prior gaps
   blocked this along the way, both fixed upstream and no longer relevant:
   a physical-ownership classification gap in `host/physical_ownership.py`
   (missing cases for an in-flight PRELOAD/EJECT's own occupied path, and
   for UNPRELOAD's `Stage.UNLOAD_RETRACT_TO_LOAD`), and an unrelated
   `NameError` in `host/__init__.py` (`CommandResult` used but not
   imported in `cmd_KLIPPER_MMU_UNPRELOAD`/`cmd_KLIPPER_MMU_EJECT`'s poll
   branches).
5. **`fault_check.py` — four fault scenarios over the control socket.** An
   injected TMC driver fault and a jam during PRELOAD, a stuck HUB switch
   before it and a bouncing BAY switch during it, each with its own MCU and
   klippy boot so one failure cannot cascade into the next. Every scenario
   asserts three things: the firmware's terminal result (`host/wire.py`
   `EventType`/`OpCode`), the `KLIPPER_MMU_DIAGNOSE` text it prints, and the
   plant's independent ground truth that the lane really stopped or
   completed where the firmware says it did. A failure next to a large
   `wall_gap_us` in the timer gap report is treated as a host stall, not a
   firmware fault, and that scenario is retried once.
6. **`control_smoke.py` — the control socket's own contract.** Boots the
   stack once with every lane empty and checks that `truth` agrees with what
   `SIGUSR1` would have dumped, that a BAY switch forced to read made still
   reads `present` to `KLIPPER_MMU_PHYSICAL` while the plant's physical truth
   stays empty (a stuck switch, exactly), that a jam and an empty spool show
   up in `truth`, that an unknown verb and an unusable argument get different
   errors, and that a TMC fault injection is accepted.
7. **`load_check.py` — a LOAD traced through the buffer, toolhead and
   extruder model.** See "Tracing a LOAD" below.

Everything lands under `build/linux-mcu/` (the extracted disposable Klipper
tree, all four ELF images, klippy/MCU logs, the generated `printer*.cfg`
files, the calibration JSON). The one exception is klippy's API socket,
which lives in a short-lived `/tmp` directory — `AF_UNIX` paths are capped
at roughly 108 bytes by the kernel, and a worktree checkout path plus
`build/linux-mcu/klippy.sock` blows right through that, so the socket gets a
short home and everything else keeps its long, readable one.

## What's real and what's a stand-in

The generic Klipper code, the whole klipper-mmu add-on, and `pin_config.c`
compile **unchanged** against the sim's `gpio.h` (STM32's real struct layout
and `GPIO()` pin encoding) — that's the entire point. What the board layer
(`sim/board/`) supplies instead of real silicon:

- `timer.c`: exact ns/tick conversion at 520 MHz (no truncated
  `NSECS_PER_TICK`), plus virtual time — inside a timer callback,
  `timer_read_time()` returns that timer's own scheduled waketime; outside
  one, it's the real wall clock. The dispatcher's own "am I late" checks
  (`timer_dispatch()`) call `real_time_now()` directly, bypassing the
  monotonic clamp described below, so a genuinely slow host is still
  reported as a stall, not hidden by it. Phase 2a found and fixed two ways
  this virtual-time model could hand out a value *behind* one it had
  already returned, each surfacing as `mcu/clock.c`'s
  `km_mcu_clock_observe()` (`src/mcu_clock.c`) latching a
  `"klipper_mmu: clock fault"` shutdown on the very first backward or
  ambiguous-size step it sees (this is a real platform contract violation,
  not a firmware bug: real hardware's `timer_read_time()` is monotonic).
  Phase 1's `diff_probe` path never read the clock this early or this
  often; phase 2a's `diagnostic_startup` does, on every lane, which is what
  surfaced both:
  - `timer_init()` anchored `start_sec` one second *ahead* of the actual
    start. `timespec_to_time()`'s unsigned tick diff then went negative
    (huge, unsigned) for the whole first second of process life, so
    anything that read the clock that early saw a nonsense tick value and
    then an enormous apparent backward jump a second later. Fixed by
    anchoring at the current second.
  - Even with that fixed, two *different* timers' own nominal waketimes
    are not required to be mutually ordered the way one timer's own
    reschedule chain is, and `timer_dispatch()`'s catch-up loop can run a
    batch of due timers faster than their nominal spacing implied. Either
    way, a caller could see a value a handful of microseconds *behind* one
    `timer_read_time()` already returned to an earlier caller. Fixed with
    `high_water_mark`: every value this function returns, from either
    branch, is clamped to be monotonic non-decreasing against every value
    it has ever returned before — the platform contract real hardware
    already gives for free, made explicit here instead of assumed.
- `gpio.c`: plain in-process pin state, the same `gpio_local`/ADC-pair/
  PWM-pair driver-API extensions the add-on links against, and the identical
  `pin` enumeration `src/stm32/gpio.c` would emit for H723 (ports A-H; H723
  has no GPIOI, so neither does this). Also: the wired-AND of the MCU's own
  output and `tmc2209.c`'s chip model on the four single-wire TMC UART pins
  (see below); the PC4/PC5 DiffA/DiffB ADC pair, answered from
  `diff_model.c` against whatever calibration the firmware has published,
  at the buffer compression the plant currently reports (see "The filament
  plant" below, phase 2b-2a), so FPS arrival and the follower watch a buffer
  that really fills and empties; and three
  latent phase-1 stub bugs this phase found and fixed:
  `gpio_local_reserve()` returned a bare truthy `1` where callers compare
  the result against the named `GPIO_LOCAL_OK` (`0`) status codes, so every
  local pin reservation silently fell through to "faulted"; `gpio_pwm_pair_setup()`
  never wrote its `struct gpio_pwm_pair **out` output parameter, so every
  powered-rewind lane's PWM bridge looked "set up" by return code and was
  dead by internal state (a NULL pair fails `mcu/pwm_bridge_gpio.c`'s
  `live()` check); and `gpio_pwm_pair_get_info()` returned success without
  filling `out_info`, so `io_write_pwm()`'s read-back-and-confirm check
  (`is_ready` plus the just-written forward/reverse counts) saw garbage and
  latched a driver fault on the very first safe-stop write every rewind
  lane's setup makes. All three were reachable only once something actually
  checked the result strictly, or once `diagnostic_startup` exercised board
  bring-up at all — phase 1's `diff_probe` never did. The fix keeps a tiny
  per-pair confirmed-forward/confirmed-reverse state (an ideal PWM: written
  is instantly confirmed, no timer lag) instead of returning canned zeros.
- `tmc2209.c`: a pin-level TMC2209 model, one instance per single-wire UART
  pin (`RESERVE_PINS_klipper_mmu_uart`: PD5, PD0, PE1, PC6 for the
  BoxTurtle/AFC Lite preset this sim board targets), address 0 (the AFC
  Lite Kconfig default). It decodes the firmware's timed TX edges the same
  way the real chip's UART auto-baud sync does (the sync+reserved byte's
  fixed low nibble always yields 5 clean edges 1 bit-time apart, so
  edge5-edge1 gives the bit period with no foreknowledge of it), applies
  register writes (GSTAT is write-1-to-clear; SLAVECONF's SENDDELAY field
  follows the datasheet's rev1.09 table, floor 8 bit-times, because
  `tmcuart.c` always waits a fixed 4 bit-times before it starts polling for
  a reply and an earlier reply would be missed), and answers reads by
  driving the wire after SENDDELAY bit-times plus a half-bit reply phase.
  CRC and the 8-byte reply's 8N1 framing reuse the firmware's own
  `km_tmc2209_crc`/`km_tmc_uart_encode` (`src/tmc2209.c`,
  `src/tmc_uart_codec.c`) instead of a second implementation. It logs one
  `km_sim_tmc2209 pin=... event=write|read|malformed|foreign ...` line per
  completed transaction to stderr (captured into the MCU log), which
  `motion_boot_check.py` uses as its zero-retry evidence. What it does not
  model: current sensing, DRV_STATUS beyond a clean default, or any
  register the firmware never actually reads (seeded to 0 from
  `tmc2209_regs.h`'s table but never exercised).
- `h723_resources_sim.c` / `boot_identity_sim.c`: stand-ins for the two
  files that touch real H723 registers (timer/PWM route identities and the
  hardware RNG). Good enough to boot, answer `KLIPPER_MMU_QUERY`, and now
  get an actuation-permitted image through `diagnostic_startup` to `READY`;
  still not exercised against real motion, so treat their output as
  "plausible," not "verified," until phase 3 puts a plant behind them.
- `stubs.c`: SPI/I2C stubs that shut the MCU down if actually called (this
  board doesn't model those buses yet), and a `reset` command that
  re-execs the process — the sim's answer to "pull the plug."
- `dict_stubs.c`: hand-transcribed `DECL_CONSTANT`/`DECL_ENUMERATION` calls
  for the stm32 peripheral files (`gpioperiph.c`, `adc.c`, `pwm.c`, ...) that
  the sim board doesn't compile. Verified against the real ARM dictionary,
  not guessed and hoped.

`sim/board/diff_model.{c,h}` (the DiffA/DiffB forward model) is wired in via
`gpio.c`, and `gpio_adc_read()` feeds it the plant's current buffer
compression (`km_sim_plant_buffer_permille()`, below) instead of a fixed
position, so the raw counts the firmware reads move as the feeder fills the
buffer and the extruder draws it back down. A full plant (rewind spool,
buffer forces) is still a later phase.

## The filament plant (phase 2b-1, extended past the HUB in phase 2b-2a)

`sim/board/plant.{c,h}` models one number per lane: the filament tip's
position along the feed path, in micrometres. It is deliberately small: the
feeder's STEP/DIR pins move the tip, and the BAY/LOAD/HUB switch pins read
it back through the exact pin table and polarity `mcu/Kconfig.pins`/
`.switch`/`.feeder` configure for this sim board's one topology (BoxTurtle
device profile, AFC Lite wiring preset, DiffA/DiffB buffer, `LOAD_SENSORS`
and `POWERED_REWIND` both on, no toolhead switch), transcribed from a
resolved build's `autoconf.h` rather than guessed. `KLIPPER_MMU_FEEDER_STEPS_PER_M`
(727272, the real BoxTurtle conversion) is used exactly for step-to-micrometre
math; the three switches' *spacing* along the path is not a firmware
constant at all — klipper-mmu only ever watches switch levels, never a
compiled distance between them — so the plant's own short, sim-only
geometry (`KM_SIM_PLANT_BAY_UM`/`_LOAD_UM`/`_HUB_UM`) is a deliberate choice
for fast CI runs, matching `docs/plan/linux-mcu-integration.md`'s own
guidance that continuous integration uses short sim geometry, not real
BoxTurtle mm distances. A lane's tip only moves once it has reached the
feeder's grip point (modeled at the BAY switch); an empty lane's STEP
pulses are counted but do not move a tip that was never inserted.

What it models: STEP (active-edge counted, `KLIPPER_MMU_FEEDER_STEP_HIGH`),
DIR (`KLIPPER_MMU_FEEDER_FORWARD_HIGH`) and ENABLE pins per lane; BAY and
LOAD switches per lane; the shared HUB switch (active if any lane's tip
reaches it); and, since phase 2b-2a, the path past the HUB. That last part
is the buffer: `KM_SIM_PLANT_TOOLHEAD_MM` (150) of travel from the HUB to
the point where the extruder's own drive gears grip the strand, and a
`KM_SIM_PLANT_BUFFER_MM` (15, the real BoxTurtle FPS travel) range between
the feeder and those gears whose compression is the permille
`gpio_adc_read()` shows DiffA/DiffB. The extruder's STEP/DIR pins (PF0/PF1
from `[extruder]`, forward = DIR high) are counted as filament the extruder
has drawn, so once the tip has passed the toolhead the buffer empties as
the extruder pulls and refills as the feeder pushes — the same arithmetic
that makes the follower's bang-bang loop visible to a test. Initial
per-lane state (`empty`, `bay`, `load` or `hub` — inserted up to the BAY,
per-lane LOAD or shared HUB switch) comes from a small file named by the
`KM_SIM_PLANT` environment variable, one `lane=<n> state=<...>` line per
lane, so a test sets up a scenario without touching the socket. Ground
truth (position, total steps seen, direction changes, the three switches'
current levels, and the buffer/toolhead/extruder trio) is written to the
file named by `KM_SIM_PLANT_OUT` at exit or `SIGUSR1`, for a test to assert
on independently of what the firmware itself reports.

What it does not model: the powered rewind spool/PWM (already stubbed
separately in `gpio.c`, and unaffected by the plant), a toolhead switch
(this profile doesn't have one), or the buffer's *forces* — the plant
tracks compression as the number the DiffA/DiffB forward model reads, it
does not simulate what pushes back. Jams, runouts and switch bounce are
modeled, but as injections through the control socket (`plant block`,
`plant runout`, `switch ... stuck|bounce`), not as anything the plant's own
kinematics discovers; a lane with no injection still transitions cleanly.

## Tracing a LOAD (phase 2b-2a)

`load_check.py` is the first check whose subject is a *trace*, not just a
verdict. Lane 0 starts at its LOAD switch (`state=load`: `bay=1 load=1
hub=0`), the real `OAMSM_LOAD_FILAMENT GROUP=T0` drives the strand across
the HUB into the modeled buffer, a real `G1 E50` draws it back out of that
buffer, and `OAMSM_UNLOAD_FILAMENT` retracts it again — all on the real
firmware and host extra, with the plant's real 15 mm FPS travel behind
DiffA/DiffB. Two threads watch while that happens:

  - every 50 ms the control socket's `truth` (feeder position, buffer
    permille, toolhead, extruder consumed) plus a wall-clock timestamp. The
    socket is serviced by the MCU's own timer, so it keeps answering while
    klippy is busy with the move;
  - every 500 ms `KLIPPER_MMU_DIAGNOSE` on a *second* klippy API
    connection, derived from the first one's socket path (the first belongs
    to the scenario). DIAGNOSE takes klippy's MCU lock, which the load
    itself holds between steps, so a timeout is recorded as a blank state
    and retried — noisy evidence is allowed, a fatal error is not.

Both land in `build/linux-mcu/load-trace.csv` (`t_s`, `feeder_um`,
`buffer_permille`, `toolhead`, `extruder_consumed_um`, `fw_state`,
`fw_stage`), and the summary prints whether or not the assertions hold: the
buffer's peak, the first moment it passed 900 permille together with the
feeder's speed over the preceding second, the last sample, and the firmware
states seen.

The assertions are the phase 2b-2a prototype's, unchanged: the buffer stays
inside 100..900 permille while the extruder draws 50 mm, the extruder
consumes at least 49 mm of filament, and the strand is behind the HUB after
UNLOAD. A *buffer* failure is the expected outcome on a host that cannot
grant real-time priority — the follower is bang-bang (feed below ~250
permille, coast above ~750) and this image feeds up to 18000 steps/s, so
15 mm of travel is smaller than the follower's ~15.7 mm braking distance
and the buffer can saturate before feeding stops. That is the point of
recording the trace: read the summary for how fast and by how much, not
just the exit code.

A second, unrelated way to fail on such a host looks nothing like a buffer
problem: a wall gap over the 2.5 ms switch-freshness budget leaves a switch
sample stale, and the firmware's own protection stops the LOAD with
`SENSOR_FAILURE` long before the strand reaches the toolhead. One dev-box
run died at 43 mm past the HUB, with a 3004 us gap in that same second and
an empty buffer trace (`buffer_permille=0` throughout); the same code
re-run reached the buffer and failed on the assertion above instead.
`load_check.py`, unlike `control_smoke.py` and `fault_check.py`, does not
retry, so check the gap report and the LOAD's attempt line before blaming
the plant.

Before the traced LOAD, the script issues a small forward `G1 E5` and waits
for the plant to credit at least 4.9 mm of it: the extruder's STEP/DIR pins
are the only witness that the extruder pulled anything, so if that
arithmetic is broken every later buffer number is meaningless. This
config's `max_velocity: 1` scales the extruder down to ~0.27 mm/s
(`max_extrude_ratio`), so the check waits for the move to finish (~19 s for
the E5, a few minutes for the E50) rather than assuming a fixed sleep.

## Real-time priority

The firmware's switch-sample freshness rule (`mcu/switch_gpio.h`
`KM_SWITCH_MAX_SAMPLE_GAP_US = 2500`: a gap over 2.5 ms between
`km_switch_gpio_sample()` calls invalidates prior evidence) assumes the
~1 ms `KM_CONTROLLER_PERIOD_US` controller tick (`mcu/controller.h`) that
calls it actually runs on schedule — true on real hardware's dedicated
timer, not guaranteed on an ordinary Linux `SCHED_OTHER` process sharing a
loaded box. `sim/board/timer.c` carries a small, permanent diagnostic for
exactly this: it tracks the worst 5 gaps, in both real (wall) and virtual
(scheduled) time, between successive timer dispatches — cheap (one
`clock_gettime()` and a few comparisons per dispatch, no per-event output,
so it doesn't itself perturb the timing it measures) — and dumps them to
the MCU log at exit or `SIGUSR2` (deliberately not `SIGUSR1`, so it never
fights the plant's own handler for that signal) via the `KM_SIM_TIMER_GAPS_OUT`
environment variable. It can't name the exact timer function
(`sched_timer_dispatch()`, Klipper's own generic scheduler, doesn't expose
that, and this sim board doesn't modify generic Klipper code to add it), but
reports each gap's scheduled-time delta, which is a reliable proxy: about
1000 us means the ~1 kHz controller tick, since nothing else in this
profile is periodic at that rate.

Measured on this project's dev box under ordinary background load (a
concurrent local server and another build, load average
~0.7–1.2): worst controller-tick dispatch gaps of up to 4531 us at normal
Linux scheduling priority — over the firmware's 2.5 ms budget, and
correlated 1:1 with `KLIPPER_MMU_PRELOAD`'s `KM_FAULT_SENSOR_FAILURE` in
`preload_check.py`. Granting the sim MCU real-time (`SCHED_FIFO`)
scheduling brought the worst gap down to 1077–1125 us across repeated
runs — noise-floor close to the nominal 1000 us period — and eliminated the
fault in every run.

`preload_check.py`'s `grant_realtime_priority()` grants this with `sudo -n
setcap cap_sys_nice,cap_ipc_lock+ep` on the disposable sim binary (once,
idempotent) rather than wrapping it under `chrt`, so there is no wrapper
process between the test and the PID it sends `SIGUSR1`/`SIGUSR2` to.
Klipper's own `src/linux/main.c` already calls `sched_setscheduler(SCHED_FIFO)`
and `mlockall()` at startup behind its existing `-r` flag (reporting the
errno on failure); this only supplies the capabilities that call needs and
passes `-r`. It is best-effort: without passwordless `sudo`, it prints why
and falls back to normal priority, so this test still runs (just possibly
flakier under load) on a host that can't grant it. Set `KM_SIM_NO_REALTIME=1`
to force normal priority deliberately (for example, to reproduce the gap
numbers above).

Inside a bubblewrap sandbox, bubblewrap's no-new-privileges drops that file
capability, so the launcher raises `RLIMIT_RTPRIO` to 50 before starting the
sandbox, and Klipper's own call succeeds without it.
`grant_realtime_priority()` recognises that limit and reports real time as
granted. The kernel's real-time throttling (`kernel.sched_rt_runtime_us`)
still reserves CPU time for everything else. Verified: the sim image builds
and `preload_check.py` passes inside the sandbox at `SCHED_FIFO`.

5. **`soak_check.py` — a whole-stack soak harness.** Runs the full simulation
   (sim board, real firmware, host extra, and klippy) for `N` cycles (default 25)
   to detect memory leaks, host stalls, and timing regressions over an extended
   run. In each cycle, it drives each populated lane through `PRELOAD` and then
   `UNPRELOAD` (and, in the final cycle only, `EJECT`), waiting for the terminal event and verifying
   the simulated plant's ground truth matches the expected filament position. It logs 
   wall duration, process VmRSS, and any reported host stalls to `build/linux-mcu/soak.csv`. 
   Optionally, with `--load`, it also exercises the OpenAMS load/unload macro path.

## Selector mode (shared-drive machines: ERCF, Tradrack, MMX6, 3D Chameleon, HTLF, MMX, BTT ViViD, Claymore)

`sim/board/plant.c` also models the shared-drive selector topology
(`include/klipper_mmu/selector.h`): one gear STEP/DIR/ENABLE moves the tip of
whichever lane the selector currently sits at, gated on LINEAR by the axis
being within a small tolerance of that gate's configured position *and* the
servo holding its engage pulse (ROTARY: axis position only, no servo; pure
SERVO: the servo pulse alone selects the gate, no axis at all; INDEXED: the
axis sitting inside that gate's own index-switch window -- see below). Gate
positions and servo engage/release/move pulses are read from the same
`CONFIG_KLIPPER_MMU_SELECTOR_*` Kconfig strings `mcu/config.c` resolves the
firmware's own profile from, not a second copy of the numbers. Whichever
mode a given image's `autoconf.h` selects (`CONFIG_KLIPPER_MMU_SELECTOR_STEPPER`/
`_SERVO`) is the only one compiled in; the original four-lane per-lane
feeder tables are unchanged on a non-selector image. The ground truth dump
gains three fields on a selector image only: `selector_position_um`,
`servo_pulse_us` and `engaged_gate` (repeated per lane like `buffer_permille`
already is, since the hardware is shared) -- see `sim/board/plant.h`.

**INDEXED's own geometry (BTT ViViD, Claymore -- no gate position table at
all).** The firmware never computes an absolute gate position for this kind
(docs/selectors.md "Indexed Gates"): it only ever searches for a switch. The
plant still needs *some* physical layout to decide when each gate's own
index switch (`KLIPPER_MMU_PINS_SELECTOR_GATE`, one pin per lane) reads
asserted, and that layout is sim wiring, not a device fact -- a drum that
wraps, one gate every `gate_pitch_steps` around it, built from what the
image *does* carry (`CONFIG_KLIPPER_MMU_SELECTOR_GATE_ORDER` and
`CONFIG_KLIPPER_MMU_SELECTOR_INDEXED_MAX_TRAVEL_MM`) rather than a bare
invented pitch: Happy Hare's own `IndexedSelector` hardcodes exactly two
possible drum rotations for every device it knows
(`mmu_indexed_selector.py:45-49`, unoverridden anywhere), so inverting the
image's own whole-axis travel bound with that same "2" gives one drum
circumference, and dividing by the lane count gives the pitch -- for BTT
ViViD this reconstructs the real CAD pitch exactly (720 mm / 2 / 4 gates =
90 mm, `docs/devices/vivid.json`'s own "gate_positions" decision); Claymore
carries no CAD data at all (`mcu/Kconfig.selector`'s own help text), so its
pitch is this sim's own documented invention, not a measurement. An empty
`GATE_ORDER` (Claymore's forward-only sentinel) places lanes at identity
physical slots (lane *i* at slot *i*) as this plant's own choice -- a
forward-only device never picks a direction from the order anyway, so any
placement serves its bookkeeping equally well. Unlike LINEAR/ROTARY's
endstop-clamped axis, the raw selector position here goes negative freely
(no endstop exists to clamp it to), and every gate's own window is checked
against that position folded to its shortest signed distance around the
circumference -- which is also why INDEXED's home lands *inside* gate
0's own window from the very first instant (raw position 0 is by
construction that gate's centre), unlike LINEAR/ROTARY's CAD-offset gate 0.

**`plant_model_test/run.py` — native unit tests for the plant alone,
selector mode and the original mode.** Same shape as `tmc2209_model_test`
(a tiny scheduler-free shim, no Kconfig/Klipper build, no klippy, no pty):
`test_plant_default.c` proves the original four-lane mode is unaffected,
`test_plant_selector.c` drives an ERCF-2.0-shaped image (LINEAR selector +
grip servo, 8 lanes) through homing, per-gate engagement and the gear only
ever moving the currently engaged gate's own filament, and
`test_plant_indexed.c` drives a BTT-ViViD-shaped image (INDEXED, a real
gate order, no servo, 4 lanes) through the same engagement/gear proof plus
the drum wrap both forward and reverse (including a negative raw axis
position). Only LINEAR and INDEXED get a native-only test; ROTARY and
SERVO are proven solely by the whole-stack `device_scenarios.py` below,
same as before this pass. Run alone with
`python3 tests/linux_mcu/plant_model_test/run.py`; wired into `test` above.

**The five selector kinds** (`devices.DEVICES['ercf_2_0']`,
`['chameleon_3d']`, `['mmx']`, `['vivid']` and `['claymore']` run the one
`selector_scenario` in `device_scenarios.py`; each has its own fragment in
`configs/`) are proven end to end by that same function. It reads the built
image instead of this directory's own table: `<device.sim_out()>/autoconf.h`'s
`CONFIG_KLIPPER_MMU_SELECTOR_KIND_*` and every `CONFIG_KLIPPER_MMU_SELECTOR_*`
value (gate positions, per-gate gear directions, release gates, servo gate
angles, and for INDEXED the gate order/switch width/max travel bound), and
fails loudly if one is missing, so a fragment can never silently drift from
what the scenario expects. Per device:

- **ERCF 2.0** (LINEAR, 8 lanes): `BOARD_CUSTOM` (the AFC Lite preset has
  no selector pins), one shared gear reusing the original lane-0 feeder
  pins, a selector axis + grip servo reusing three of the now-unused
  lane-1..3 feeder pins, 8 per-gate BAY switches (docs/devices/ercf_2_0.json's
  own `mmu_entry_switch_pin_0..7`) reusing the original BAY/LOAD pins plus 4
  fresh ones, and the plant's existing shared HUB switch standing in for the
  family's shared gate sensor. It proves the axis lands on each gate's own
  position and the grip servo engages only there (the pair is gate 0 and the
  last gate, so the whole list is travelled), and that PRELOAD/UNLOAD release
  the grip (`engaged_gate` -1) with the axis left at the gate.
- **3D Chameleon** (ROTARY, 4 lanes): the same shared gear and selector
  axis, no servo, and `docs/devices/3d_chameleon.json`'s two reversed gates
  (`selector_gate_directions "1,1,0,0"`) and non-cyclic release map
  (`selector_release_gates "2,3,0,1"`). Its gate pair is the first reversed
  gate and the first normal one, and a LOAD that arrives through each covers
  `sim/board/plant.c`'s `gate_dir_reversed()` both ways -- the reverse-gate
  LOAD is the direction proof. After PRELOAD and UNLOAD the axis must sit at
  that gate's *release* gate position with `engaged_gate` reading the release
  gate, because ROTARY releases by moving, not by a servo pulse.
- **MMX** (SERVO, 4 lanes): no selector stepper at all -- one servo pin
  (reusing lane 3's unused feeder STEP pin) and the per-gate angles
  `mcu/Kconfig.selector` defaults from `docs/devices/mmx.json`'s
  `servo_gate_angles "60,0,180,120"`. It drives gates 0 and 2, whose pulses
  are the farthest apart, and grades each by the plant's own `engaged_gate`
  field (`sim/board/plant.c` derives it from the firmware's
  `KM_SELECTOR_SERVO_US`, so no Python copy of the angle-to-pulse formula is
  involved). MMX's `servo_release_angle` is -1, so after UNLOAD the servo
  stays on the gate and `engaged_gate` stays that gate -- the scenario
  asserts exactly that, since it is the device's real behaviour.
- **BTT ViViD** (INDEXED, 4 lanes): `BOARD_CUSTOM`, the same shared gear and
  selector axis as the other stepper kinds, no servo, no gate position
  table -- one per-gate index switch instead
  (`KLIPPER_MMU_PINS_SELECTOR_GATE`, reusing the four-lane preset's own
  now-unused LOAD pins) and a real gate order
  (`docs/devices/vivid.json`'s `selector_gate_order "0, 3, 1, 2"`). Its gate
  pair is `order[0]` (lane 0, home) and `order[-1]` (lane 2, one physical
  slot *behind* home going reverse but three slots *ahead* going forward):
  `check_indexed_travel` asserts the actual net raw selector travel between
  them is the shorter reverse hop, not the longer forward one, proving
  `_best_rotation_direction` (selector.h) rather than just "some path that
  happens to land on the right gate."
- **Claymore** (INDEXED, 4 lanes): the same wiring shape as BTT ViViD, plus
  a per-lane powered rewind motor
  (`mcu/Kconfig.control`'s own `KLIPPER_MMU_POWERED_REWIND` default for
  this device) and no dossier at all -- AFC is its only written source
  (`docs/devices/afc/claymore.md`), so its `Device` row passes
  `dossier=None` rather than fabricating one. Its `GATE_ORDER` is left
  empty on purpose (AFC's own forward-only sentinel,
  `extras/AFC_vivid.py:159-199 select_lane`), so the plant places its
  lanes at identity physical slots and `check_indexed_travel` asserts the
  *opposite* of BTT ViViD's proof for the identical gate pair: the actual
  travel is always the longer forward hop, never the shorter reverse one,
  because AFC's own selector never compares directions in the first place.
  **Known gap, both devices:** `host/__init__.py`'s `_OPENAMS_DEVICE_IDS`
  has no entry for `'btt_vivid'` (device id 22) or `'claymore'` (id 23) --
  it stops at `'tradrack'=21`. Every other selector device in this
  catalogue is already in that dict, so their scenarios PRELOAD/LOAD/UNLOAD
  through real firmware end to end; BTT ViViD's and Claymore's own
  `openams_motion: True` PRELOAD instead fails immediately with `Cold HOST
  session is unavailable` (`host/openams_managed_unit.py`'s
  `_motion_composition_matches` returning `False` for an unrecognised
  family), confirmed in the klippy log both scenarios currently produce.
  Both devices boot clean through `KLIPPER_MMU_QUERY` (`startup_state=ready`,
  `startup_result=0`) and the plant's own ground truth already shows
  `engaged_gate=0` at boot and the correct BAY state for both pre-inserted
  lanes -- the firmware, pin map and plant model are proven; only the
  PRELOAD/LOAD/UNLOAD leg through OpenAMS needs that one dict fixed
  (in `host/`).

For all five, the dossier's PRELOAD/SELECT/LOAD/UNLOAD end blocks either
carry no sensor at all (`ercf_2_0`), name a shared-exit switch that
`SENSOR_TO_FIELD` does not map (the family's one shared switch, which this
sim models as the HUB), or -- BTT ViViD/Claymore -- have no dossier to name
one from at all, so no dossier rest-side comparison applies to any of them.
`selector_scenario` therefore checks the plant's own selector ground truth
(axis position against the image's own gate list, `engaged_gate` per kind,
and for INDEXED the actual net travel direction) instead.

## No-BAY-sensor devices (Angry Beaver, 3MS, QuattroBox 1.0/1.1/2): boot, PRELOAD-skip, LOAD, extrude and UNLOAD all pass

`devices.DEVICES['angry_beaver']`, `['3ms']` and `['quattrobox_2']`
(`configs/angry-beaver-h723-sim-motion.config`, `3ms-h723-sim-motion.config`,
`quattrobox-2-h723-sim-motion.config`) run `phase_one_boot`, same as
NightOwl/QIDI Box, on a shape none of those three have: no per-lane BAY
switch at all (`CONFIG_KLIPPER_MMU_BAY_SENSORS` unset -- their traced Happy
Hare profiles render no per-gate entry sensor, docs/devices/angry_beaver.json,
3ms.json, quattrobox_1_0/1_1/2_0.json). `quattrobox_2` alone stands in for
all three QuattroBox versions: their dossiers trace to identical mechanics,
layout and `steps_per_mm`. Angry Beaver and QuattroBox keep 3MS's real
shared HUB switch pin (PE12); 3MS's own dossier has no shared-exit sensor at
all, but this firmware's device profile always carries `has_hub_sensor=true`
(`mcu/config.c`) with no per-device way to unset it, and 3MS has no per-lane
LOAD switch either (ruling out the virtual-hub path), so its sim bench wires
the same physical HUB pin purely for the firmware's own state machine, not as
a claim about real 3MS hardware (`devices.py`'s own row comment explains
this, and is why `has_hub_switch=False` there is a dossier-accuracy flag,
not a claim about this sim's wiring).

`device_scenarios.py`'s `phase_one_boot` reads
`CONFIG_KLIPPER_MMU_BAY_SENSORS` from the built image (never the device
name) to decide the scenario: without a BAY sensor, `km_core_request_preload`
(`src/operation_core.c`) always refuses (`!has_bay_sensors`), so PRELOAD is
skipped and lane 0's initial plant state is `"empty"` (position 0 -- filament
at the spool, not at a BAY switch that does not exist) instead of `"bay"`.

That skip alone was not enough to reach LOAD: `sim/board/plant.c`'s STEP-edge
handler used to refuse every step below the BAY switch's own position
(`grip_threshold_steps()`, tied unconditionally to `KM_SIM_PLANT_GRIP_UM`),
so a lane starting at position 0 with no BAY switch to clear could never
move at all -- forever. Fixed: `grip_threshold_steps()` now reads 0 without
`CONFIG_KLIPPER_MMU_BAY_SENSORS` (a `filament_always_gripped` feeder, per
these devices' own dossiers, has nothing to grip up to). Native regression:
`plant_model_test/test_plant_no_bay.c` proves a lane starting "empty" moves
on its first STEP pulse and can still feed all the way to the shared HUB;
`plant_model_test/shim_default/autoconf.h` now defines
`CONFIG_KLIPPER_MMU_BAY_SENSORS=1` explicitly so the *ordinary* BAY-sensored
mode this fix sits next to keeps its original grip point, unaffected.

Diagnostic startup reaches `ready` for all three, PRELOAD is correctly
skipped, and `OAMSM_LOAD_FILAMENT GROUP=T0` now gets admitted too --
`device_scenarios.py angry_beaver`/`3ms`/`quattrobox_2` each pass PRELOAD-skip,
LOAD, extrude and UNLOAD end to end, agreeing with all three dossiers' own
rest-side expectations with no `rest_decisions` entry needed
(`tests/linux_mcu/devices.py`'s own `check_rest`, checked directly against
each dossier's `operations.{load,unload}.end` block). The three-part chain
that used to block this (host sensor projection, host path ownership, and
the pinned OpenAMS fork's own world build) is fixed:

- `host/openams_sensor_projection.py`'s `project_sensor_presence` no longer
  requires every per-lane BAY reading to be `configured` before it will
  report anything; an unconfigured BAY's `bay_present` entry is `None`,
  tolerated exactly like an already-optional LOAD or toolhead reading
  (kept minimal and local to the BAY reading).
- `host/physical_ownership.py`'s `reconcile_path_ownership` now tolerates
  that `None` everywhere LOAD's own `None` was already tolerated (the
  `'feeding'`, `'empty'`, `'local'` and `'known'` branches, plus the type
  check): a real BAY switch reporting the lane empty still blocks exactly
  as before (`is not False`/`is False` in place of a bare truthy check,
  which is the identical predicate on the real booleans every other device
  still reports -- BoxTurtle, NightOwl, QIDI Box and every BAY-sensored
  selector family are bit-for-bit unchanged).
- The pinned OpenAMS fork (`openams`, now on a new local
  branch `klipper-mmu/no-bay-gates` at `ab4f767`, with `preload_check.py`'s
  `OPENAMS_COMMIT` pointed at it for this pass) no longer reads
  `oam.f1s_hes_value[bay]` directly in `oams_manager.py`'s `build_world()`;
  it calls `oam.bay_loadable(bay)` instead. `OAMS` and `Follower` both grow
  a `bay_loadable()` that is `bool(self.f1s_hes_value[bay_index])` --
  exactly their old inline behavior, so every stock AMS/OpenAMS unit is
  unchanged. `host/openams_device.py`'s `bay_loadable()` answers the real
  switch directly when it has one, and otherwise from this MMU's own
  persisted per-lane gate status: Happy Hare's own three gate states
  (`GATE_UNKNOWN`/`GATE_EMPTY`/`GATE_AVAILABLE`,
  `extras/mmu/mmu_constants.py:79-82` in the pinned oracle) and its
  tool-change rule of refusing only a confirmed-empty gate
  (`mmu_controller.py`'s `_select_and_load_tool`) -- an unknown gate is left
  attemptable, same as an available one, never fabricated as refused.

Gate status lives in the existing `KLIPPER_MMU_SET_GATE_METADATA` command's
`AVAILABLE` field (`host/gate_metadata.py`'s new `gate_status()` read
accessor over the same file), so a user can set it directly, and it is also
learned automatically from each LOAD's own outcome
(`host/load_report.py`'s `gate_learned_from_load`, wired into
`host/__init__.py`'s `record_acked` by `_learn_gate_status`): any attempt
that reaches `Stage.LOAD_FEED_TO_BUFFER` (past the shared HUB) proves the
gate had filament regardless of what the LOAD goes on to do, mirroring
Happy Hare's own downstream `FILAMENT_POS_START_BOWDEN` promotion for a gate
with no local sensor; a failed LOAD that never reaches it proves the gate
was empty, the same "couldn't pick up filament at gate" case that marks a
gate empty in `_home_to_gate`'s exhausted-attempts branch. A device with a
real BAY switch is never touched by this learning step. A cancelled LOAD, or
one with no attempt records, proves nothing and leaves the stored status
alone -- so a user's own override always wins until the next LOAD outcome
that actually proves something.

No regression: `nightowl`, `qidi_box` (`device_scenarios.py`) and BoxTurtle's
own `load_check.py` all still pass PRELOAD/LOAD/extrude/UNLOAD unchanged.
