# Happy Hare harness (HH-1, Milestone 1)

What this proves: klipper-mmu's real core (`src/*.c`, unmodified, the same
code the MCU links) can sit behind Happy Hare's own operation-level seam
(`_preload_gate`, `unload_sequence`) on HH's `boxturtle` fixture, with HH's
own 1-D filament model (`test/hh/filament.py`) as the plant instead of a
teleporting stub, and complete PRELOAD/UNLOAD in well under a second of wall
time per operation. `load_sequence` and `_eject_from_gate` stay the teleport
stubs from the review's probe this milestone (LOAD/EJECT are out of scope
here). See `build/b2/review_hh_harness_review.md` (the design, Milestone 1)
and `build/b2/review_translation_consult.md` (sections 2.1 and 4, background).

Verdict: **pass**. See the root report for the full table; summary below.

## What's here

- `hh_shim.c` - the real core + one `mock_driver.c` feeder/rewinder pair per
  lane + the reference stepper executor (`tests/stepper_reference.c`), built
  as a shared library. No new core code; the control/motion tick body is
  lifted from `sim/test_loop.c` (`run_motion_tick`/`run_for`) with the causal
  plant removed - HH's own model is the plant here. C API: `km_hh_init`,
  `km_hh_sensors`, `km_hh_request`, `km_hh_adopt`, `km_hh_run`, plus a small
  `km_hh_debug` getter for local debugging/the report (not part of the seam).
- `hh_probe.py` - extends an earlier 141-line review probe: the teleport
  stubs are unchanged (kept behind `--teleport`, reproducing its 123/138
  baseline exactly), and
  `real_preload_gate`/`real_unload_sequence` drive `hh_shim.so` tick by tick,
  applying completed steps to HH's `FilamentPath` with `.advance()` and
  reading its sensors back with `km_hh_sensors`.
- `Makefile` - builds the `.so`, extracts the pinned HH archive into
  `../build/upstream/happy-hare-c54c736/` if missing (never into the repo),
  and `make test` runs the teleport baseline (five files) followed by the
  three real-core classes, printing one `SUMMARY` line per file/class.

## Running it

```sh
make -C tests/hh_harness test          # everything, ~30s
# or by hand, from the extracted HH root:
cd ../build/upstream/happy-hare-c54c736
KM_HH_LIB=<repo>/tests/hh_harness/build/libhh_shim.so \
  <klippy-env>/bin/python <repo>/tests/hh_harness/hh_probe.py \
  test.test_mmu_motion.TestPreload
```

`HH_PYTHON`/`HH_ROOT` default to the shared `../build/` tree beside the
*main* checkout (resolved with `git rev-parse --git-common-dir`, so this
works the same from a review worktree, which nests one directory deeper than
the main checkout). Override either on the `make` command line.

## Design notes worth knowing before touching this again

- **Sensor map**: HH `mmu_entry_N` -> BAY N, `mmu_exit_N` -> LOAD N,
  `unit0:mmu_shared_exit` -> HUB. TOOLHEAD is always published `false`
  (fresh, not present): BoxTurtle's ADOPT check only requires it to be
  *fresh* (`src/operation_core.c:981-984`, `expected = occupied &&
  filament_present_toolhead` is self-referential - it can never CONFLICT,
  only go stale), and PRELOAD/UNLOAD never read it for anything else. Low/high
  switched-buffer state is never published (not needed for PRELOAD/UNLOAD,
  and BoxTurtle's baseline profile has no proportional buffer either way).
- **`has_load_sensors`**: `km_profile_init_boxturtle()` leaves this `false`
  (no fabricated optional sensor). HH's shipped `boxturtle` wires a real
  per-gate `mmu_exit` switch, and a real BoxTurtle board build gets
  `has_load_sensors=true` from its pin map
  (`tests/klipper_board_startup/test_boxturtle_boot.c`), so `hh_shim.c` sets
  it explicitly to match - without it PRELOAD feeds to the shared HUB
  instead of the lane's own gate switch, which is not what either HH or a
  real BoxTurtle does.
- **UNLOAD needs a loaded core.** LOAD stays a teleport this milestone, so
  before UNLOAD the shim publishes the (now-loaded) sensor state and calls
  `km_hh_adopt` -> `km_core_reconcile_path(core, occupied=true, lane, time)`
  (ADOPT). This worked on the first attempt with no core changes.
- **steps/mm**: read live from HH's own rendered `[mmu_stepper unit0_gear]`
  for `boxturtle` (`hh_probe.py:steps_per_mm()`), not hard-coded - it
  includes `gear_ratio` (rendered `50:10`, i.e. 5:1), which is easy to miss
  if you only look at `rotation_distance` and `microsteps` (140.77 steps/mm
  vs the correct 703.86 steps/mm - a first pass here used the wrong one and
  every distance in HH's model was 5x too large until this was fixed).
- **Timeouts**: `unload_retract_timeout_ms`/`unload_rewind_timeout_ms` are
  raised to 300000 ms in `hh_shim.c`. HH's synthetic BoxTurtle bowden
  (mmu_entry -150mm to a loaded tip at +768mm, 918mm) needs about 145s of
  *virtual* motion time at `default_rewind_velocity_steps_s`'s real-world
  speed once steps/mm is correct; the Baseline profile's 8s default assumes
  a much shorter real machine. Same category of fixture sizing as
  `sim/test_loop.c:402` shortening `hub_feed_timeout_ms` for its short
  scenario - not a change to what the timeout means.
- **Speed**: a pure 1-tick-per-call Python loop was fast enough (under 3s for
  the whole TestPreload/TestQuietPlacement files) until the steps/mm fix
  made UNLOAD's tick count 5x larger (a single UNLOAD then took ~6.3s,
  over budget). `hh_probe.py` batches `BATCH_TICKS=50` control ticks per
  `km_hh_run` call (the review's "if an operation takes more than a few
  seconds, add `km_hh_run(ticks, max_delta_steps)`" escape valve) - sensors
  are still published every batch (50ms), well inside `max_sensor_age_ms`
  (200ms), and the extra reaction lag this adds is a few hundredths of a mm
  at these feed speeds, noise next to the rest-position differences below.
  This brought every test in the milestone set under 1s.
- **Insert-driven PRELOAD is not the blocker the review flagged as a kill
  risk.** `TestQuietPlacement.test_loud_placement_past_the_entry_switch_preloads_the_gate`
  and `TestPreload.test_insert_driven_preload_ends_parked` run `_preload_gate`
  from *inside* one of HH's own reactor callbacks
  (`reactor.in_dispatch() == True`, confirmed by instrumenting the call).
  `km_hh_run`'s tick loop never touches HH's reactor clock at all - it keeps
  its own independent microsecond counter and only calls into HH through
  `FilamentPath.advance()`/HH's sensor callbacks, neither of which pumps the
  reactor - so the "`advance()` called from inside a callback" hazard the
  review raised for this exact case never comes up. Both tests ran to
  completion; the only failure is the documented rest-position difference.

## Results (measured, this run)

Teleport baseline (`--teleport`, all four operations stubbed, for
comparison - reproduces the review's numbers exactly):

| File | Tests | Pass | Fail/Error |
|---|---:|---:|---:|
| test_mmu_toolchange | 30 | 26 | 4 |
| test_mmu_motion | 57 | 46 | 11 |
| test_mmu_endless_spool | 18 | 18 | 0 |
| test_mmu_sensor_runout | 31 | 31 | 0 |
| test_mmu_check_gate | 2 | 2 | 0 |
| **Total** | **138** | **123** | **15** |

Real core (PRELOAD/UNLOAD; LOAD/EJECT still teleported):

| Class | Tests | Pass | Fail | Wall |
|---|---:|---:|---:|---:|
| test_mmu_motion.TestPreload | 9 | 5 | 4 | ~2.4s |
| test_mmu_motion.TestQuietPlacement | 4 | 3 | 1 | ~2.2s |
| test_mmu_toolchange.TestUnload | 7 | 4 | 3 | ~3.0s |

Every real-core failure is one of: **rest-position** (HH parks PRELOAD at
`mmu_exit + 10mm` and UNLOAD at `mmu_shared_exit - 100mm`; our core settles
at the LOAD/HUB switch plus a small deceleration overshoot instead - a
decision, not a bug) or **planner-only** (asserts the *names* of HH's own
homing legs or its internal `_unload_bowden` call, which our core never
makes - never portable to any real firmware behind this seam). None is a
core bug. Full per-test table in the root report.

## Known skips (not run here, and why)

- `test_mmu_roundtrip`, `test_mmu_compound_endstop`, `test_mmu_tangle_prevention`,
  `test_mmu_sync_feedback`, `test_mmu_local_gate`: not motion (NFC/Spoolman,
  pure-logic stubs, HH's own host sync/multi-unit bookkeeping) - the review's
  section 1, unchanged by this milestone.
- `test_mmu_encoder`, `TestSensorlessPreload` (7 tests): encoder-homing and
  open-loop fixed-move preload are HH features our firmware does not
  implement/does not support without a LOAD or HUB switch - AGENTS.md says
  to reject operations whose required hardware is absent, so these stay
  teleport-only (they already fail under the teleport stub for the same
  reason; see the review section 5).
