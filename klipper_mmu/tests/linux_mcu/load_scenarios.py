#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Phase 3b (LOAD side): two more whole-stack scenarios that reuse
load_check.py's plant-in-the-loop harness and fault_check.py's runner --
neither is load_check.py's own traced-buffer-bounds check, so they don't
duplicate it. See tests/linux_mcu/README.md and
docs/path-reconciliation.md#from-a-stopped-fault.

lane-swap: after a LOAD/extrude/UNLOAD on lane 0, the same on lane 1, off the
one KLIPPER_MMU_CALIBRATE both lanes share (the calibrated distance is one
HUB-to-arrival measurement for the whole shared path, not per lane) -- ground
truth must show each lane where the commands say: lane 0 back behind its HUB,
then lane 1 loaded, then it too behind its HUB, and neither lane disturbs the
other's switches.

runout-while-following: a runout injected during extrusion after a LOAD (not
fault_check.py's already-covered PRELOAD-time runout). The follower's own
presence check (src/follower.c) faults the instant BAY or HUB stops reading
covered, which happens immediately once `plant runout <lane>` is injected on
a lane already deep past HUB (sim/board/plant.c's segment_covers: the tail
freezes at the injection point, which is already past every switch threshold
behind the tip). The firmware raises KM_FAULT_FILAMENT_LOST; the host's
DIAGNOSE explains it in plain words ("HUB was lost while feeding",
host/diagnostics.py); recovery is the documented one for a fault nothing is
actively polling (docs/path-reconciliation.md#from-a-stopped-fault):
KLIPPER_MMU_ACK_FAULT, then RECONCILE_PATH/RECOVER_SESSION -- and must leave
the unit able to LOAD a different lane (this plant models a runout as a
zero-length remaining strand, so lane 0 itself cannot LOAD again this boot,
exactly as fault_check.runout_during_preload documents for the PRELOAD case).
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import preload_check as harness
import control_smoke as smoke
import fault_check as fc
from build_root import skip_unless_available  # noqa: E402

EXTRUDE_MM = 5
CONSUMED_TARGET_UM = 4900
MACRO_TIMEOUT_S = 180.0


def write_two_lane_plant(path):
    """Lane 0 and lane 1 both start at their own per-lane LOAD switch --
    independent feed paths that only share the HUB downstream (sim/board/
    plant.c), so both can be state=load at once. Lanes 2 and 3 stay empty."""
    with open(path, 'w') as f:
        f.write('lane=0 state=load\n')
        f.write('lane=1 state=load\n')
        for lane in (2, 3):
            f.write('lane=%d state=empty\n' % lane)


def run_macro(run, script):
    """Issue one klipper_mmu/OpenAMS gcode script, absorbing OpenAMS's own
    bootstrap window like load_check.py's run_macro (its state cache needs a
    lane's reservation to catch up with the firmware's last terminal event).
    Blocks until the firmware operation itself completes (CALIBRATE/LOAD/
    UNLOAD hold the MCU lock for their whole physical duration), so the
    caller is free to read the control socket or ground truth immediately
    after this returns.

    load_check.py's own first macro is preceded by a ~19s E5 self-check move,
    which incidentally gives OpenAMSManagedUnit's async bind_runtime() time to
    wire up its command bridges before the first real one fires. These
    scenarios have no such warm-up, so the very first CALIBRATE right after
    boot can race that bind: the Start* effect raises "OpenAMS ... bridge is
    not configured" (klippy log only), which oams_runtime.py's
    _effect_failed() turns into a generic OAMS_OP_CODE_ERROR_KLIPPER_CALL
    completion -- surfaced to this RPC as "Calibration failed (stopped by
    klipper monitor)", not the "OpenAMS motion unavailable" text the existing
    retry already covers. Same bootstrap window, so it gets the same bounded
    retry. A LOAD issued right after CALIBRATE completes can hit a third,
    same-shaped race: CALIBRATE's own probe retracts the tip back to sit at
    the HUB switch (it measured hub-to-arrival, so that is where it ends),
    and OpenAMS's own lane-world cache (oams_state._load_guard's
    _hub_occupied(), openams-src/src/oams_state.py) is refreshed by its
    manager's periodic monitor timer, not synchronously with CALIBRATE's own
    reply -- so a LOAD issued before that timer's next tick can see a
    momentarily stale "hub occupied" and be refused with "filament still
    detected in a hub on this lane; unload it first" even though CALIBRATE
    really did finish. Same bounded retry."""
    deadline = time.monotonic() + 30.0
    while True:
        resp, lines = run.klippy.call('gcode/script', {'script': script},
                                      timeout=MACRO_TIMEOUT_S)
        if 'error' not in resp:
            print('%s output: %s' % (script, lines))
            return lines
        text = str(resp['error'])
        if (('OpenAMS motion unavailable' in text
                or 'stopped by klipper monitor' in text
                or 'still detected in a hub' in text)
                and time.monotonic() < deadline):
            time.sleep(0.5)
            continue
        raise Exception('%s failed: %s' % (script, resp['error']))


def lane_truth(control, lane):
    return smoke.truth_lanes(control)[lane]


def extrude(run, control):
    """A small forward extrude (load_check.py's own self-check amount), waited
    out on the control socket's extruder_consumed_um (shared across every
    lane's row -- one extruder, sim/board/plant.c) rather than the heavier
    SIGUSR1 ground-truth dump, since this can be polled every loop cheaply."""
    baseline = lane_truth(control, 0)['extruder_consumed_um']
    run.klippy.call('gcode/script',
                    {'script': 'G91\nG1 E%d F120\nG90' % EXTRUDE_MM})
    deadline = time.monotonic() + 60.0
    consumed = baseline
    while time.monotonic() < deadline:
        consumed = lane_truth(control, 0)['extruder_consumed_um']
        if consumed - baseline >= CONSUMED_TARGET_UM:
            return consumed
        time.sleep(0.5)
    raise Exception('extruder move only credited %d um (baseline %d, now %d)'
                    % (consumed - baseline, baseline, consumed))


def behind_hub(lane, note):
    if not (lane['bay'] == 1 and lane['load'] == 0 and lane['hub'] == 0):
        raise Exception('%s: ground truth disagrees with "behind HUB": %s'
                        % (note, lane))


def lane_swap(run, control):
    """Item 3: LOAD lane 0, extrude, UNLOAD; LOAD lane 1, extrude, UNLOAD.
    One CALIBRATE covers both lanes."""
    run_macro(run, 'KLIPPER_MMU_CALIBRATE MMU=box BAY=0')

    run_macro(run, 'OAMSM_LOAD_FILAMENT GROUP=T0')
    extrude(run, control)
    run_macro(run, 'OAMSM_UNLOAD_FILAMENT')
    lane0 = lane_truth(control, 0)
    behind_hub(lane0, 'lane 0 after its own UNLOAD')
    print('lane 0 after UNLOAD: %s' % lane0)

    lane1_untouched = lane_truth(control, 1)
    if not (lane1_untouched['bay'] == 1 and lane1_untouched['load'] == 1
            and lane1_untouched['hub'] == 0):
        raise Exception('lane 1 moved during lane 0''s own cycle: %s'
                        % lane1_untouched)

    run_macro(run, 'OAMSM_LOAD_FILAMENT GROUP=T1')
    extrude(run, control)
    run_macro(run, 'OAMSM_UNLOAD_FILAMENT')
    lane1 = lane_truth(control, 1)
    behind_hub(lane1, 'lane 1 after its own UNLOAD')
    print('lane 1 after UNLOAD: %s' % lane1)

    lane0_again = lane_truth(control, 0)
    behind_hub(lane0_again, 'lane 0 after lane 1''s cycle')
    print('Lane swap: lane 0 then lane 1, each LOAD/extrude/UNLOAD, ground '
         'truth agreed at every step.')


def runout_while_following(run, control):
    """Item 4: a runout injected during extrusion, after a LOAD (not
    fault_check.py's PRELOAD-time runout)."""
    run_macro(run, 'KLIPPER_MMU_CALIBRATE MMU=box BAY=0')
    run_macro(run, 'OAMSM_LOAD_FILAMENT GROUP=T0')

    baseline = lane_truth(control, 0)['extruder_consumed_um']
    run.klippy.call('gcode/script',
                    {'script': 'G91\nG1 E%d F120\nG90' % EXTRUDE_MM})
    deadline = time.monotonic() + 30.0
    while lane_truth(control, 0)['extruder_consumed_um'] <= baseline:
        if time.monotonic() >= deadline:
            raise Exception('extrude never started before the runout '
                            'injection')
        time.sleep(0.1)

    control.expect_ok('plant runout 0')

    deadline = time.monotonic() + 15.0
    text = ''
    while time.monotonic() < deadline:
        text = ' '.join(fc.diagnose(run))
        if 'HUB was lost while feeding' in text:
            break
        time.sleep(0.3)
    else:
        raise Exception('DIAGNOSE never reported the runout: %s' % text)
    print('runout during following: DIAGNOSE reports Fault.FILAMENT_LOST '
         '("HUB was lost while feeding"): %s' % text)

    # The extrusion queued before the runout is still running in klippy;
    # recovery commands would wait behind it past their 10 s timeout.
    run.klippy.call('gcode/script', {'script': 'M400'}, timeout=120.0)
    lane = fc.stopped(run, 'runout while following')
    if lane['bay'] or lane['hub']:
        raise Exception('BAY/HUB still read present after the runout: %s'
                        % lane)

    print('    ACK_FAULT: %s' % fc.gcode(run, 'KLIPPER_MMU_ACK_FAULT MMU=box'))
    fc.recover(run)

    run_macro(run, 'OAMSM_LOAD_FILAMENT GROUP=T1')
    lane1 = lane_truth(control, 1)
    if not (lane1['bay'] == 1 and lane1['load'] == 1 and lane1['hub'] == 1):
        raise Exception('lane 1 LOAD after recovery did not reach the '
                        'buffer: %s' % lane1)
    print('Recovery proved: LOAD on lane 1 succeeded after the lane 0 '
         'runout, ground truth %s' % lane1)


GEAR_FACTOR = 1.08


def unit_rotation_um():
    """The image's own KLIPPER_MMU_FEEDER_ROTATION_DISTANCE_UM."""
    with open('build/linux-mcu/klipper/out-sim-motion/autoconf.h') as f:
        for line in f:
            if line.startswith('#define CONFIG_KLIPPER_MMU_FEEDER_ROTATION_DISTANCE_UM '):
                return int(line.split()[2])
    raise Exception('no CONFIG_KLIPPER_MMU_FEEDER_ROTATION_DISTANCE_UM in the sim image')


def lane_gear_setup():
    """Lane 1's plant gear moves 4% more filament per turn, and the host is
    told so with feeder_rotation_distances; every other lane is the unit's."""
    unit = unit_rotation_um()
    lane1 = int(round(unit * GEAR_FACTOR))
    os.environ['KM_SIM_PLANT_LANE_ROTATION_UM'] = '0,%d' % lane1
    # 8% of an 800 mm Bowden (64 mm) is outside the +/-50 mm arrival window.
    os.environ['KM_SIM_PLANT_TOOLHEAD_MM'] = '800'
    mm = ['%.3f' % (unit / 1000.0)] * 4
    mm[1] = '%.3f' % (lane1 / 1000.0)
    harness.EXTRA_MMU_OPTIONS = 'feeder_rotation_distances: ' + ', '.join(mm)


def lane_gear_teardown():
    os.environ.pop('KM_SIM_PLANT_LANE_ROTATION_UM', None)
    os.environ.pop('KM_SIM_PLANT_TOOLHEAD_MM', None)
    harness.EXTRA_MMU_OPTIONS = ''


def lane_gear(run, control):
    """CALIBRATE on lane 0, then LOAD lane 1, whose gear differs by 8%: over
    the Bowden that is more than the +/-50 mm arrival window unless the
    firmware scales lane 1 by its own rotation distance."""
    run_macro(run, 'KLIPPER_MMU_CALIBRATE MMU=box BAY=0')
    run_macro(run, 'OAMSM_LOAD_FILAMENT GROUP=T1')
    extrude(run, control)
    run_macro(run, 'OAMSM_UNLOAD_FILAMENT')
    lane1 = lane_truth(control, 1)
    behind_hub(lane1, 'lane 1 (4% gear) after its UNLOAD')
    print('Lane gear: lane 1 with a %.2fx gear loaded, extruded and unloaded '
         'off lane 0\'s calibration: %s' % (GEAR_FACTOR, lane1))


SCENARIOS = (('lane-swap', lane_swap),
             ('runout-while-following', runout_while_following),
             ('lane-gear', lane_gear))
SETUP = {'lane-gear': (lane_gear_setup, lane_gear_teardown)}


def main():
    skip_unless_available('tests/linux_mcu/load_scenarios.py')
    names = sys.argv[1:] or [name for name, _ in SCENARIOS]
    by_name = dict(SCENARIOS)
    unknown = [n for n in names if n not in by_name]
    if unknown:
        raise SystemExit('unknown scenario(s): %s (have %s)'
                         % (', '.join(unknown), ', '.join(by_name)))
    broken = []
    for name in names:
        setup, teardown = SETUP.get(name, (None, None))
        if setup:
            setup()
        try:
            if not fc.run(name, by_name[name], plant=write_two_lane_plant):
                broken.append(name)
        finally:
            if teardown:
                teardown()
    if broken:
        raise SystemExit('Load scenarios failed: %s' % ', '.join(broken))
    print('All load scenarios passed.')


if __name__ == '__main__':
    main()
