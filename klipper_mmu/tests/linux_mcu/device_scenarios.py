#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Sim-devices: prove the whole-stack sim (real firmware on Klipper's Linux
MCU, sim/board/plant.c's plant, real klippy, OpenAMS and the host extra) on
a device other than BoxTurtle, with the phase order and rest positions
checked against that device's Happy Hare dossier (docs/devices/<name>.json,
via tests/linux_mcu/devices.py) -- see tests/linux_mcu/README.md and
build/b2/passive_sonnet2_sim.txt.

Requires that device's sim image already built:
  python3 tests/linux_mcu/build.py nightowl emu
then:
  python3 tests/linux_mcu/device_scenarios.py [nightowl] [emu]
(no arguments runs every entry in devices.DOSSIER_DEVICES).

Reuses preload_check.py's Klippy socket client, install_openams(),
write_calibration(), read_ground_truth(), parse_kv(), run_operation() and
grant_realtime_priority() rather than duplicating them -- only the
device-parametrized printer.cfg (OpenAMS family/lane count) and the
dossier-derived rest-position checks (tests/linux_mcu/devices.py) are new.
One device at a time, exactly like every other check in this directory:
KLIPPER_MMU_QUERY, the control socket and the ground-truth file are all
single-instance per klipper.elf, so proving two devices means two
independent invocations of this script, not two lanes of one.

Each device runs one boot, the way a person uses it: PRELOAD lane 0 from
an empty-to-BAY start, then CALIBRATE/LOAD/extrude/UNLOAD on the same
boot. That sequence once hit a "filament already detected in the hub"
refusal because CALIBRATE treated the lane's own PRELOAD rest as an
occupied path; the core now admits it the way LOAD does.
"""
import contextlib
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import preload_check as harness  # noqa: E402
import control_smoke as smoke  # noqa: E402
from build_root import KLIPPY_PYTHON, skip_unless_available  # noqa: E402
from devices import DEVICES, DOSSIER_DEVICES  # noqa: E402

EVENT_PRELOAD_COMPLETE = 13
MACRO_TIMEOUT_S = 180.0
EXTRUDE_MM = 5
CONSUMED_TARGET_UM = 4900
# host/wire.py AdmissionResult.UNAVAILABLE.
ADMISSION_UNAVAILABLE = 7


def eject_expected_outcome(cfg):
    """True when this image admits EJECT (km_core_request_eject needs BAY
    sensors). The one place to change when no-BAY units gain Happy Hare's
    distance-based eject."""
    return (cfg.get('CONFIG_KLIPPER_MMU_BAY_SENSORS') == '1'
            or int(cfg.get('CONFIG_KLIPPER_MMU_EJECT_FINAL_DISTANCE_MM', '0')) > 0)


def check_eject(run_checked, klippy, dump_ground_truth, lane, tag, cfg):
    """Drive EJECT for `lane` and check whichever outcome
    eject_expected_outcome(cfg) predicts."""
    if eject_expected_outcome(cfg):
        before = dump_ground_truth()[lane]
        run_checked('EJECT', lane, 'EJECT', harness.EVENT_EJECT_COMPLETE,
                   start_timeout=30.0)
        if cfg.get('CONFIG_KLIPPER_MMU_BAY_SENSORS') == '1':
            check_eject_withdrawn(dump_ground_truth, lane, tag, cfg)
        else:
            check_eject_distance(dump_ground_truth, lane, tag, cfg, before)
    else:
        eject_refused_no_bay(klippy, lane, tag)


def eject_refused_no_bay(klippy, lane, tag):
    """Without BAY sensors EJECT must be refused with exactly UNAVAILABLE."""
    resp, lines = klippy.call(
        'gcode/script', {'script': 'KLIPPER_MMU_EJECT MMU=box BAY=%d' % lane})
    if 'error' in resp:
        raise Exception('%s: EJECT lane %d failed to start: %s'
                        % (tag, lane, resp['error']))
    text = ' '.join(lines)
    m = re.search(r'admission=(\d+)', text)
    if not m or int(m.group(1)) != ADMISSION_UNAVAILABLE:
        raise Exception(
            '%s: EJECT lane %d expected admission=%d (UNAVAILABLE, no BAY '
            'sensor on this image); got: %s'
            % (tag, lane, ADMISSION_UNAVAILABLE, text))
    print('%s: EJECT lane %d refused as expected (no CONFIG_KLIPPER_MMU_'
         'BAY_SENSORS on this image): %s' % (tag, lane, text))


def check_eject_distance(dump_ground_truth, lane, tag, cfg, before):
    """Without BAY switches EJECT steps back a set distance (Happy Hare's
    gate_final_eject_distance): the motor must step that far (+/-2 mm), and
    the filament moves back by it or leaves the gear."""
    want_mm = int(cfg['CONFIG_KLIPPER_MMU_EJECT_FINAL_DISTANCE_MM'])
    steps_per_mm = (int(cfg['CONFIG_KLIPPER_MMU_FEEDER_FULL_STEPS'])
                    * int(cfg.get('CONFIG_KLIPPER_MMU_TMC_MICROSTEPS') or 16)
                    * int(cfg['CONFIG_KLIPPER_MMU_FEEDER_GEAR_RATIO_A']) * 1000.0
                    / (int(cfg['CONFIG_KLIPPER_MMU_FEEDER_GEAR_RATIO_B'])
                       * int(cfg['CONFIG_KLIPPER_MMU_FEEDER_ROTATION_DISTANCE_UM'])))
    after = dump_ground_truth()[lane]
    stepped_mm = (after['steps'] - before['steps']) / steps_per_mm
    if (abs(stepped_mm - want_mm) > 2
            or after['position_um'] > max(before['position_um'] - want_mm * 1000, 0) + 2000):
        raise Exception('%s: EJECT lane %d stepped %.1f mm and moved from %d to %d um, '
                        'wanted %d mm back' % (tag, lane, stepped_mm, before['position_um'],
                                               after['position_um'], want_mm))
    print('%s: EJECT lane %d stepped %.1f mm back (%d mm asked), filament %d -> %d um'
         % (tag, lane, stepped_mm, want_mm, before['position_um'], after['position_um']))


def check_eject_withdrawn(dump_ground_truth, lane, tag, cfg):
    """EJECT retracts until BAY clears; the plant does not model powered
    rewind, so that part is only noted."""
    gt = dump_ground_truth()[lane]
    if not (gt.get('bay') == 0 and gt.get('load') == 0 and gt.get('hub') == 0):
        raise Exception('%s: EJECT lane %d ground truth still shows '
                        'filament: %s' % (tag, lane, gt))
    print('%s: EJECT lane %d ground truth confirms filament withdrawn '
         'behind BAY: %s' % (tag, lane, gt))
    if cfg.get('CONFIG_KLIPPER_MMU_POWERED_REWIND') == '1':
        print('%s: lane %d has powered rewind, but the plant models only '
             'the feeder tip position -- ground truth has no rewind field '
             'to check it ran.' % (tag, lane))


def write_initial_plant(path, lane_count, lane0_state='bay'):
    """Lane 0 starts inserted up to its BAY switch (like preload_check.py's
    write_plant_state()) so PRELOAD has something to admit; every other
    lane stays empty. A device with no BAY sensor at all
    (CONFIG_KLIPPER_MMU_BAY_SENSORS unset -- Angry Beaver, 3MS, QuattroBox
    1.0/1.1/2) has no BAY switch to have been inserted up to: its lane 0
    starts at the plant's "empty" (position 0) state instead, which
    phase_one_boot() selects by passing lane0_state='empty'. The last lane
    starts like lane 0 and is left alone for the final EJECT check."""
    with open(path, 'w') as f:
        f.write('lane=0 state=%s\n' % lane0_state)
        for lane in range(1, lane_count - 1):
            f.write('lane=%d state=empty\n' % lane)
        if lane_count > 1:
            f.write('lane=%d state=%s\n' % (lane_count - 1, lane0_state))


def write_printer_config(cfg_path, pty_path, calibration_path, device):
    """Same shape as preload_check.write_printer_config(), parametrized by
    device: openams_family/openams_bays must equal the firmware's own
    KLIPPER_MMU_DEVICE_ID/KLIPPER_MMU_LANE_COUNT dictionary constants
    (host/__init__.py _OPENAMS_DEVICE_IDS, checked by
    KlipperMMU._motion_composition_matches()), which is exactly
    device.openams_family/device.lane_count from tests/linux_mcu/devices.py."""
    bowden_path = cfg_path + '.bowden.json'
    if os.path.exists(bowden_path):
        os.unlink(bowden_path)
    filament_groups = '\n'.join(
        '[filament_group T%d]\ngroup: box-%d' % (i, i)
        for i in range(device.lane_count))
    # A device with no DiffA/DiffB buffer (QIDI Box's own tension-only
    # switched buffer) never delivers calibration: passing the option
    # anyway is a host config_error (klippy/extras/klipper_mmu/__init__.py
    # _config_calibration_and_startup's own CONFIG_DIFF_BUFFER gate), not a
    # firmware capability this scenario should fake past.
    diff_calibration_line = (
        'diff_calibration_file: %s\n' % calibration_path if device.diff_buffer else '')
    with open(cfg_path, 'w') as f:
        f.write(f"""
[mcu]
serial: {pty_path}
[printer]
kinematics: none
max_velocity: 50
max_accel: 500
[pause_resume]
[extruder]
step_pin: PF0
dir_pin: PF1
enable_pin: !PF2
microsteps: 16
rotation_distance: 22.6789511
max_extrude_only_velocity: 10
max_extrude_only_accel: 100
nozzle_diameter: 0.400
filament_diameter: 1.750
heater_pin: PF3
sensor_type: EPCOS 100K B57560G104F
sensor_pin: PA3
control: pid
pid_Kp: 22.2
pid_Ki: 1.08
pid_Kd: 114
min_temp: 0
max_temp: 250
min_extrude_temp: 0
[klipper_mmu box]
mcu: mcu
diagnostic_startup: True
{diff_calibration_line}bowden_estimate_file: {bowden_path}
openams_motion: True
openams_idx: 1
openams_fps: fps0
openams_family: {device.openams_family}
openams_bays: {device.lane_count}
openams_path_length_mm: 842.5
openams_host_watchdog_ms: 2000
openams_lease_max_age_ms: 1000
path_reconciliation: True
driver_restart: True
[fps fps0]
source_oams: klipper_mmu box
extruder: extruder
[oams_manager]
{filament_groups}
""")


def run_macro(klippy, script):
    """Same bounded retry as tests/linux_mcu/load_scenarios.py's run_macro:
    absorbs OpenAMSManagedUnit's async bind_runtime()/monitor-tick bootstrap
    window instead of racing it."""
    deadline = time.monotonic() + 30.0
    while True:
        resp, lines = klippy.call('gcode/script', {'script': script},
                                  timeout=MACRO_TIMEOUT_S)
        if 'error' not in resp:
            print('%s output: %s' % (script, lines))
            return lines
        text = str(resp['error'])
        if (('OpenAMS motion unavailable' in text
                or 'stopped by klipper monitor' in text
                or 'still detected in a hub' in text
                # CALIBRATE's own phrasing of the same stale-hub-occupied
                # OpenAMS monitor-tick race load_scenarios.py's run_macro
                # documents ("filament already detected in the hub").
                or 'detected in the hub' in text)
                and time.monotonic() < deadline):
            time.sleep(0.5)
            continue
        raise Exception('%s failed: %s' % (script, resp['error']))


@contextlib.contextmanager
def _boot(name, device, plant_writer, suffix, extra_env=None):
    """Start this device's sim MCU and klippy against a fresh plant/ground-
    truth file pair (tagged with `suffix` so two boots for the same device
    in one run don't collide), install the host extra and OpenAMS, wait for
    diagnostic startup to reach READY, then yield (klippy, dump_ground_truth,
    mcu_proc, gaps_path) for the caller's body. Teardown (klippy, socket dir,
    MCU, gap report) always runs, mirroring preload_check.py's own
    try/finally nesting. extra_env (optional) adds sim-only board overrides
    on top of KM_SIM_PLANT/KM_SIM_PLANT_OUT/KM_SIM_TIMER_GAPS_OUT below, e.g.
    selector_scenario's KM_SIM_PLANT_SERVO_GATE_PULSE_US for Pico MMU."""
    tag = '%s-%s' % (name, suffix)
    mcu_elf = os.path.abspath(
        'build/linux-mcu/klipper/%s/klipper.elf' % device.sim_out())
    if not os.path.exists(mcu_elf):
        raise Exception('%s missing; run `python3 tests/linux_mcu/build.py '
                        '%s` first' % (mcu_elf, name))
    pty_path = os.path.abspath('build/linux-mcu/mcu-%s.pty' % tag)
    if os.path.exists(pty_path):
        os.unlink(pty_path)

    calibration_path = os.path.abspath(
        'build/linux-mcu/diff-calibration-%s.json' % tag)
    harness.write_calibration(calibration_path)
    plant_path = os.path.abspath('build/linux-mcu/plant-initial-%s.txt' % tag)
    plant_writer(plant_path, device.lane_count)
    ground_truth_path = os.path.abspath(
        'build/linux-mcu/plant-ground-truth-%s.txt' % tag)
    if os.path.exists(ground_truth_path):
        os.unlink(ground_truth_path)
    gaps_path = os.path.abspath('build/linux-mcu/timer-gaps-%s.txt' % tag)
    if os.path.exists(gaps_path):
        os.unlink(gaps_path)

    env = dict(os.environ)
    env['KM_SIM_PLANT'] = plant_path
    env['KM_SIM_PLANT_OUT'] = ground_truth_path
    env['KM_SIM_TIMER_GAPS_OUT'] = gaps_path
    if extra_env:
        env.update(extra_env)

    realtime = harness.grant_realtime_priority(mcu_elf)
    print('%s: sim MCU real-time priority: %s'
         % (tag, 'granted' if realtime else 'unavailable'))

    mcu_log_path = 'build/linux-mcu/mcu-%s.log' % tag
    mcu_argv = [mcu_elf] + (['-r'] if realtime else []) + ['-I', pty_path]
    mcu_proc = subprocess.Popen(
        mcu_argv, stdout=open(mcu_log_path, 'w'),
        stderr=subprocess.STDOUT, env=env)

    try:
        for _ in range(50):
            if os.path.exists(pty_path):
                break
            time.sleep(0.1)
        else:
            raise Exception('%s: MCU never created its pty' % tag)

        subprocess.run(['python3', 'tools/install.py', 'install-host',
                        'build/linux-mcu/klipper'], check=True, timeout=60)
        harness.install_openams(
            os.path.abspath('build/linux-mcu/klipper/klippy/extras'))

        cfg_path = os.path.abspath('build/linux-mcu/printer-%s.cfg' % tag)
        write_printer_config(cfg_path, pty_path, calibration_path, device)

        api_socket_dir = tempfile.mkdtemp(prefix='kmmu-%s-sock-' % tag)
        api_socket = os.path.join(api_socket_dir, 'klippy.sock')
        klippy_log = os.path.abspath('build/linux-mcu/klippy-%s.log' % tag)
        klippy_proc = subprocess.Popen(
            [str(KLIPPY_PYTHON),
             'build/linux-mcu/klipper/klippy/klippy.py',
             cfg_path, '-l', klippy_log, '-a', api_socket],
            stdout=open('build/linux-mcu/klippy-%s_out.log' % tag, 'w'),
            stderr=subprocess.STDOUT)

        try:
            for _ in range(150):
                if os.path.exists(api_socket):
                    break
                time.sleep(0.1)
            else:
                raise Exception('%s: klippy never created its API socket' % tag)

            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(api_socket)
            s.settimeout(10.0)
            klippy = harness.Klippy(s)
            klippy.call('gcode/subscribe_output',
                       {'response_template': {'method': 'notify_gcode_response'}})

            for _ in range(200):
                info, _ = klippy.call('info')
                if info.get('result', {}).get('state') == 'ready':
                    print('%s: klippy is ready.' % tag)
                    break
                time.sleep(0.5)
            else:
                raise Exception('%s: klippy never became ready (see %s)'
                                % (tag, klippy_log))

            status = {}
            for _ in range(200):
                resp, lines = klippy.call(
                    'gcode/script', {'script': 'KLIPPER_MMU_QUERY MMU=box'})
                if 'error' in resp:
                    raise Exception('%s: KLIPPER_MMU_QUERY failed: %s'
                                    % (tag, resp))
                for line in lines:
                    if 'startup_state=' in line:
                        status = harness.parse_kv(line)
                if status.get('startup_state') in ('ready', 'faulted'):
                    break
                time.sleep(0.25)
            print('%s: KLIPPER_MMU_QUERY status: %s' % (tag, status))
            if (status.get('startup_state') != 'ready'
                    or status.get('startup_result') != '0'):
                raise Exception(
                    '%s: diagnostic startup did not reach ready: %s (see %s)'
                    % (tag, status, klippy_log))

            def dump_ground_truth():
                # Wait for a fresh dump that reads the same twice: under load
                # a fixed sleep could catch the file half-written.
                def mtime():
                    try:
                        return os.stat(ground_truth_path).st_mtime_ns
                    except FileNotFoundError:
                        return None
                before = mtime()
                os.kill(mcu_proc.pid, signal.SIGUSR1)
                deadline = time.monotonic() + 5.0
                last = None
                while True:
                    time.sleep(0.1)
                    truth = (harness.read_ground_truth(ground_truth_path)
                             if mtime() != before else None)
                    if (truth and truth == last) or time.monotonic() > deadline:
                        return truth or {}
                    last = truth

            yield klippy, dump_ground_truth, tag
        finally:
            klippy_proc.terminate()
            klippy_proc.wait(timeout=10)
            shutil.rmtree(api_socket_dir, ignore_errors=True)
    finally:
        try:
            os.kill(mcu_proc.pid, signal.SIGUSR2)
            time.sleep(0.2)
        except Exception:
            pass
        mcu_proc.terminate()
        mcu_proc.wait(timeout=10)
        if os.path.exists(gaps_path):
            print('%s: timer dispatch gaps (worst first):' % tag)
            print(open(gaps_path).read())


def phase_one_boot(name, device):
    """One boot: lane 0 from its BAY switch through PRELOAD, then (a device
    that supports it, devices.py Device.supports_calibrate) CALIBRATE,
    LOAD/extrude/UNLOAD. Adapted by capability read from the built image,
    never by device name (build/b2/passivesim.txt): a device with no BAY
    sensor at all (CONFIG_KLIPPER_MMU_BAY_SENSORS unset) cannot admit
    PRELOAD (src/operation_core.c km_core_request_preload requires one), so
    such a device's lane 0 starts at the plant's "empty" state (filament at
    the spool, not at a BAY switch it has none of) and the scenario starts
    straight at LOAD."""
    cfg = _image_config(device)
    has_bay = cfg.get('CONFIG_KLIPPER_MMU_BAY_SENSORS') == '1'
    plant_writer = (write_initial_plant if has_bay else
                    (lambda path, lane_count: write_initial_plant(path, lane_count, 'empty')))
    with _boot(name, device, plant_writer, 'oneboot') as (
            klippy, dump_ground_truth, tag):
        def run_checked(*args, **kwargs):
            try:
                return harness.run_operation(klippy, *args, **kwargs)
            except Exception as e:
                raise Exception('%s: %s; plant ground truth: %s'
                                % (tag, e, dump_ground_truth()))

        if has_bay:
            run_checked('PRELOAD', 0, 'PRELOAD', EVENT_PRELOAD_COMPLETE,
                       start_timeout=30.0)
            gt = dump_ground_truth()
            print('%s: ground truth after PRELOAD: %s' % (tag, gt[0]))
            device.check_rest('preload', gt[0])
            print('%s: PRELOAD passed; dossier rest position agreed.' % tag)
        else:
            print('%s: no BAY sensor on this image (CONFIG_KLIPPER_MMU_'
                 'BAY_SENSORS unset); PRELOAD is unavailable '
                 '(km_core_request_preload requires one) -- starting at '
                 'LOAD.' % tag)

        if device.supports_calibrate:
            run_macro(klippy, 'KLIPPER_MMU_CALIBRATE MMU=box BAY=0')
        run_macro(klippy, 'OAMSM_LOAD_FILAMENT GROUP=T0')
        gt = dump_ground_truth()
        print('%s: ground truth after LOAD: %s' % (tag, gt[0]))
        device.check_rest('load', gt[0])

        baseline = gt[0]['extruder_consumed_um']
        klippy.call('gcode/script',
                   {'script': 'G91\nG1 E%d F120\nG90' % EXTRUDE_MM})
        deadline = time.monotonic() + 60.0
        consumed = baseline
        while time.monotonic() < deadline:
            consumed = dump_ground_truth()[0]['extruder_consumed_um']
            if consumed - baseline >= CONSUMED_TARGET_UM:
                break
            time.sleep(0.5)
        else:
            raise Exception('%s: extruder move only credited %d um '
                            '(baseline %d, now %d)'
                            % (tag, consumed - baseline, baseline, consumed))
        print('%s: extruder consumed %d um' % (tag, consumed - baseline))

        run_macro(klippy, 'OAMSM_UNLOAD_FILAMENT')
        gt = dump_ground_truth()
        print('%s: ground truth after UNLOAD: %s' % (tag, gt[0]))
        device.check_rest('unload', gt[0])

        print('%s: CALIBRATE/LOAD/extrude/UNLOAD passed; dossier rest '
             'positions agreed (recorded decisions logged above, not '
             'failed on).' % tag)

        # The path is clear after UNLOAD. With BAY switches eject the untouched
        # last lane; without them eject lane 0, parked behind the hub.
        eject_lane = device.lane_count - 1 if has_bay else 0
        check_eject(run_checked, klippy, dump_ground_truth, eject_lane, tag, cfg)


# Tolerances for the ground-truth checks below: a whole-step/whole-
# microsecond sim's "close enough", the same spirit as the plant's own
# ~0.1 mm selector engagement tolerance (sim/board/plant.c
# KM_SIM_PLANT_SELECTOR_TOLERANCE_STEPS). Not a device value, and never a
# fallback for one: every position and angle comes from the image.
SELECTOR_TOLERANCE_UM = 200
# sim/board/plant.c KM_SIM_PLANT_SERVO_TOLERANCE_US: a servo pulse rounded to
# whole microseconds, used below only for Pico MMU's own calibrate-first
# path, which checks a raw servo_pulse_us instead of engaged_gate (see
# selector_scenario's calibrate_gate_angles).
SELECTOR_SERVO_TOLERANCE_US = 20


def _image_config(device):
    """The built image's own resolved Kconfig (`autoconf.h`): the one source
    of this scenario's selector kind and every selector value, so they are
    exactly what mcu/config.c compiled into the firmware the boot below runs
    -- sim/board/plant.c's selector_load_profile() parses the same CONFIG_
    strings, which is why a check against the plant is a check against the
    image."""
    path = os.path.join('build', 'linux-mcu', 'klipper', device.sim_out(),
                        'autoconf.h')
    cfg = {}
    with open(path) as f:
        for line in f:
            if not line.startswith('#define CONFIG_'):
                continue
            macro, _, value = line[len('#define '):].partition(' ')
            cfg[macro] = value.strip().strip('"')
    return cfg


def selector_scenario(name, device):
    """One scenario for all four shared-drive selector kinds (selector.h):
    LINEAR, ROTARY, SERVO and INDEXED. The kind, gate positions, gear
    directions, release gates, servo angles and (INDEXED) gate order/switch
    width/max travel all come from the built image's own autoconf.h --
    never from the device name, never a fallback, so a value the kind needs
    but the image does not define is a hard failure. One boot, two gates (a
    ROTARY pair spans a reversed and a normal gear direction, so a LOAD
    that reaches arrival through the reversed one proves the direction; an
    INDEXED pair spans a shorter-reverse-hop and a longer-forward one, so
    reaching each proves which direction was actually driven -- see
    check_indexed_travel): PRELOAD each, CALIBRATE, LOAD/extrude/UNLOAD the
    first, LOAD the second. Every check reads the plant's ground truth;
    engaged_gate in particular is the plant's own KM_SELECTOR_SERVO_US (or,
    INDEXED, indexed_gate_asserted) answer, so no pulse width or switch
    window is re-derived here beyond the drum geometry itself
    (circumference/pitch), which sim/board/plant.c documents as this sim's
    own choice, not a firmware or device fact."""
    cfg = _image_config(device)

    def image_int(macro):
        if macro not in cfg:
            raise Exception('%s defines no %s; rebuild the image'
                            % (device.sim_out(), macro))
        return int(cfg[macro])

    def image_list(macro, cast):
        if not cfg.get(macro):
            raise Exception('%s defines no %s; rebuild the image'
                            % (device.sim_out(), macro))
        return [cast(field) for field in cfg[macro].split(',')]

    def image_selector_um_to_steps(um):
        """KM_SELECTOR_UM_TO_STEPS (mcu/selector_mechanics.h) from the image's constants."""
        full_steps = image_int('CONFIG_KLIPPER_MMU_SELECTOR_FULL_STEPS')
        microsteps = image_int('CONFIG_KLIPPER_MMU_SELECTOR_MICROSTEPS')
        gear_a = image_int('CONFIG_KLIPPER_MMU_SELECTOR_GEAR_RATIO_A')
        gear_b = image_int('CONFIG_KLIPPER_MMU_SELECTOR_GEAR_RATIO_B')
        rotation_um = image_int('CONFIG_KLIPPER_MMU_SELECTOR_ROTATION_DISTANCE_UM')
        steps_per_m = (full_steps * microsteps * gear_a * 1000000) // (gear_b * rotation_um)
        return (um * steps_per_m + 500000) // 1000000

    kinds = [kind for kind in ('LINEAR', 'ROTARY', 'SERVO', 'INDEXED')
             if image_int('CONFIG_KLIPPER_MMU_SELECTOR_KIND_' + kind)]
    if len(kinds) != 1:
        raise Exception('%s selects no single selector kind: %s'
                        % (device.sim_out(), kinds))
    kind = kinds[0]

    release_gate = {}
    indexed = None  # (position_of, pitch_um, lane_count, forward_only, half_um)
    # Pico MMU ships CONFIG_KLIPPER_MMU_SELECTOR_SERVO_GATE_ANGLES_DEG empty
    # (mcu/Kconfig.selector: Happy Hare calibrates its gates instead of
    # shipping a table) -- every gate starts uncalibrated and selecting one
    # is refused (docs/selectors.md) until KLIPPER_MMU_CALIBRATE_SELECTOR
    # saves an angle, the real runtime path tests/klipper_selector_runtime
    # also exercises. calibrate_gate_angles holds {gate: degrees} this
    # scenario applies itself, right after boot, before the first PRELOAD;
    # its own angles/spacing are this sim's choice, not a device fact.
    calibrate_gate_angles = None
    servo_pulse_expected = {}
    sim_extra_env = None
    # SERVO with a shipped angle table: the gate recalibrated at runtime and
    # the pulse it must hold after its LOAD.
    calibrated_gate = calibrated_pulse = None
    if kind == 'SERVO':
        positions = None
        raw_angles = cfg.get('CONFIG_KLIPPER_MMU_SELECTOR_SERVO_GATE_ANGLES_DEG', '')
        if raw_angles:
            angles = [int(x) for x in raw_angles.split(',')]
            gate_1 = 0
            gate_2 = max(range(len(angles)),
                         key=lambda i: abs(angles[i] - angles[0]))
            if angles[gate_2] == angles[0]:
                raise Exception('%s: every gate holds servo angle %d, so '
                                'engaged_gate cannot tell the pair apart'
                                % (device.sim_out(), angles[0]))
            # Recalibrate gate_1 to its mirrored angle. The plant only knows
            # the compiled table, so seed it with gate_1's new pulse.
            max_deg = image_int('CONFIG_KLIPPER_MMU_SELECTOR_SERVO_MAX_ANGLE_DEG')
            min_us = image_int('CONFIG_KLIPPER_MMU_SELECTOR_SERVO_MIN_PULSE_US')
            max_us = image_int('CONFIG_KLIPPER_MMU_SELECTOR_SERVO_MAX_PULSE_US')
            def pulse_for(deg):
                return min_us + (deg * (max_us - min_us) + max_deg // 2) // max_deg
            new_angle = max_deg - angles[gate_1]
            if new_angle == angles[gate_1]:
                raise Exception(
                    "%s: gate %d's own angle %d degrees is its own mirror "
                    'at max_deg %d; recalibrating it would not change '
                    'anything' % (device.sim_out(), gate_1, angles[gate_1], max_deg))
            calibrate_gate_angles = {gate_1: new_angle}
            calibrated_gate = gate_1
            calibrated_pulse = pulse_for(new_angle)
            pulses = [pulse_for(deg) for deg in angles]
            pulses[gate_1] = calibrated_pulse
            sim_extra_env = {'KM_SIM_PLANT_SERVO_GATE_PULSE_US':
                             ','.join(str(v) for v in pulses)}
        else:
            lane_count = image_int('CONFIG_KLIPPER_MMU_LANE_COUNT')
            gate_1, gate_2 = 0, lane_count - 1
            max_deg = image_int('CONFIG_KLIPPER_MMU_SELECTOR_SERVO_MAX_ANGLE_DEG')
            angle_1, angle_2 = max_deg // 7, (max_deg * 5) // 7
            calibrate_gate_angles = {gate_1: angle_1, gate_2: angle_2}
            # min/max pulse and max angle are this image's own servo geometry
            # (unconditionally defined whenever KLIPPER_MMU_SERVO is on,
            # unlike the empty gate-angle table), so replicating mcu/
            # selector_mechanics.h's KM_SELECTOR_SERVO_US formula here
            # predicts exactly what mcu/commands.c's command_km_selector_
            # gate_set will have converted and stored -- computed before
            # boot so it can also seed the plant's own KM_SIM_PLANT_SERVO_
            # GATE_PULSE_US below (sim/board/plant.c: it has no visibility
            # into a host runtime calibration, only the wire command the
            # firmware receives, so it needs telling separately what to
            # expect).
            min_us = image_int('CONFIG_KLIPPER_MMU_SELECTOR_SERVO_MIN_PULSE_US')
            max_us = image_int('CONFIG_KLIPPER_MMU_SELECTOR_SERVO_MAX_PULSE_US')
            servo_pulse_expected = {
                gate: min_us + (deg * (max_us - min_us) + max_deg // 2) // max_deg
                for gate, deg in calibrate_gate_angles.items()}
            pulses = [0] * lane_count
            for gate, us in servo_pulse_expected.items():
                pulses[gate] = us
            sim_extra_env = {'KM_SIM_PLANT_SERVO_GATE_PULSE_US':
                             ','.join(str(v) for v in pulses)}
    elif kind == 'INDEXED':
        # No gate position table at all (docs/selectors.md "Indexed
        # Gates"): positions stays None (check_selector's generic LINEAR/
        # ROTARY path skips), and this scenario derives its own drum
        # geometry the exact same way sim/board/plant.c's
        # selector_load_profile() does, from the same image macros --
        # never a second, hand-copied set of numbers.
        positions = None
        lane_count = image_int('CONFIG_KLIPPER_MMU_LANE_COUNT')
        raw_order = cfg.get('CONFIG_KLIPPER_MMU_SELECTOR_GATE_ORDER', '')
        forward_only = not raw_order
        order = ([int(x) for x in raw_order.split(',')] if raw_order
                 else list(range(lane_count)))
        position_of = {lane: i for i, lane in enumerate(order)}
        max_travel_mm = image_int('CONFIG_KLIPPER_MMU_SELECTOR_INDEXED_MAX_TRAVEL_MM')
        pitch_um = (max_travel_mm * 1000 // 2) // lane_count
        # A gate is found by its switch, so the firmware parks anywhere in
        # that switch's window (debounced switch latency at search speed puts
        # it past the edge): the plant's own window half-width, from the
        # same image macro sim/board/plant.c uses, is the tolerance.
        half_um = image_int('CONFIG_KLIPPER_MMU_SELECTOR_ENDSTOP_WIDTH_MM') * 1000 // 2
        indexed = (position_of, pitch_um, lane_count, forward_only, half_um)
        # order[-1] sits exactly one pitch behind home (order[0]) going
        # reverse, but (lane_count - 1) pitches ahead going forward -- the
        # least ambiguous pair for lane_count >= 3: reverse is unambiguously
        # shorter for a real gate_order (ViViD), and forward-only Claymore's
        # own rule (always drive forward regardless) makes that the same
        # pair prove it does NOT take the shorter hop.
        gate_1, gate_2 = order[0], order[-1]
    else:
        positions = [int(round(mm * 1000.0)) for mm in image_list(
            'CONFIG_KLIPPER_MMU_SELECTOR_GATE_POSITIONS_MM', float)]
        if kind == 'ROTARY':
            directions = image_list(
                'CONFIG_KLIPPER_MMU_SELECTOR_GATE_DIRECTIONS', int)
            release_gate = dict(enumerate(image_list(
                'CONFIG_KLIPPER_MMU_SELECTOR_RELEASE_GATES', int)))
            reversed_gate = next(
                (i for i, d in enumerate(directions) if d), None)
            normal_gate = next(
                (i for i, d in enumerate(directions) if not d), None)
            if reversed_gate is not None and normal_gate is not None:
                gate_1, gate_2 = reversed_gate, normal_gate
            elif reversed_gate is None:
                # Some real ROTARY devices never reverse the gear at all --
                # Happy Hare's own selector_gate_directions is all-zero for
                # both HTLF (docs/devices/htlf.json:160) and MMX6
                # (mcu/Kconfig.selector's own default), unlike 3D Chameleon's
                # mixed table. Any two distinct gates still prove selector
                # position and release-gate logic; there is simply no
                # gear-reversal claim to prove on this device.
                gate_1, gate_2 = 0, len(positions) - 1
            else:
                raise Exception('%s: every ROTARY gate reverses the gear; '
                                'directions=%s' % (device.sim_out(), directions))
        else:
            gate_1, gate_2 = 0, len(positions) - 1

    lane_count = image_int('CONFIG_KLIPPER_MMU_LANE_COUNT')
    # A third lane, never PRELOADed/LOADed above, stays inserted to its own
    # BAY switch for this scenario's own trailing EJECT check.
    gate_3 = next(g for g in range(lane_count) if g not in (gate_1, gate_2))
    if kind == 'SERVO' and not cfg.get('CONFIG_KLIPPER_MMU_SELECTOR_SERVO_GATE_ANGLES_DEG', ''):
        # No shipped angle table (Pico MMU): gate_3 must be calibrated too.
        max_deg = image_int('CONFIG_KLIPPER_MMU_SELECTOR_SERVO_MAX_ANGLE_DEG')
        min_us = image_int('CONFIG_KLIPPER_MMU_SELECTOR_SERVO_MIN_PULSE_US')
        max_us = image_int('CONFIG_KLIPPER_MMU_SELECTOR_SERVO_MAX_PULSE_US')
        angle_3 = (max_deg * 3) // 7
        calibrate_gate_angles[gate_3] = angle_3
        pulse_3 = min_us + (angle_3 * (max_us - min_us) + max_deg // 2) // max_deg
        servo_pulse_expected[gate_3] = pulse_3
        pulses = [int(v) for v in sim_extra_env['KM_SIM_PLANT_SERVO_GATE_PULSE_US'].split(',')]
        pulses[gate_3] = pulse_3
        sim_extra_env['KM_SIM_PLANT_SERVO_GATE_PULSE_US'] = ','.join(str(v) for v in pulses)

    def released_gate(gate):
        """Where the axis waits after PRELOAD/UNLOAD of `gate`: ROTARY moves
        to that gate's release gate; LINEAR's servo releases in place;
        INDEXED has no release move either (no servo, klipper_mmu/
        selector.h -- the axis simply stays); a pure SERVO image has no axis
        (its servo holds the gate's angle)."""
        return release_gate.get(gate, gate)

    # LINEAR's release-by-servo-pulse only makes engaged_gate observably
    # different (-1) when there IS a servo: sim/board/plant.c's own
    # selector_engaged_gate() only consults the servo pulse at all inside
    # `#if CONFIG_KLIPPER_MMU_SELECTOR_KIND_LINEAR` guarded by `if
    # (CONFIG_KLIPPER_MMU_SERVO)`. Jack Rabbit clears KLIPPER_MMU_SERVO
    # (docs/selectors.md's own "Jack Rabbit" section): the firmware still
    # marks the lane un-engaged internally on release
    # (src/operation_core.c's KM_STAGE_BRAKING release path sets
    # selector_engaged=false unconditionally for any non-ROTARY kind), but
    # that is a software-only flag the plant cannot observe without a servo
    # pin, and the axis itself never moves for a release on this kind (no
    # release_gate map either) -- so the plant's own ground truth keeps
    # reporting the same gate engaged, exactly like a servo-less ROTARY axis.
    has_release_pulse = kind == 'LINEAR' and cfg.get('CONFIG_KLIPPER_MMU_SERVO') == '1'
    if kind == 'SERVO':
        # A pure SERVO device can ALSO have a real release angle distinct
        # from -1, mcu/Kconfig.selector's own "never release" sentinel for
        # MMX/Pico MMU (docs/devices/mmx.json's servo_release_angle "-1"):
        # Low Rider's own is 0 degrees (docs/devices/low_rider.json:161).
        # mcu/config.c's servo-only km_fill_selector only fills servo_
        # release_us `#if ... RELEASE_ANGLE_DEG >= 0`; when it does, PRELOAD/
        # UNLOAD/EJECT's completion (operation_core.c's KM_STAGE_BRAKING,
        # same non-ROTARY release path Jack Rabbit's comment above
        # describes) actually moves the pulse there, which the plant's own
        # pure-SERVO engaged_gate check (a plain pulse-vs-table scan, no
        # LINEAR-style has_servo guard to skip) then sees -- and reports
        # -1 unless the release angle happens to equal a real gate's own
        # angle, true for every device this catalogue has today.
        has_release_pulse = image_int(
            'CONFIG_KLIPPER_MMU_SELECTOR_SERVO_RELEASE_ANGLE_DEG') >= 0

    def released_engage(gate):
        """engaged_gate after PRELOAD/UNLOAD of `gate`: a LINEAR grip servo
        is released (-1, nothing gripped); ROTARY's axis now sits at the
        release gate; INDEXED's axis (no servo) stays right where it is; a
        pure SERVO image's servo stays at the gate's own angle unless this
        device configures a real release angle (has_release_pulse above,
        Low Rider); a servo-less LINEAR selector (Jack Rabbit) has nothing
        observable to change either, so it stays at its own gate too."""
        return -1 if has_release_pulse else released_gate(gate)

    def write_two_gates(path, lane_count):
        with open(path, 'w') as f:
            f.write('lane=%d state=bay\nlane=%d state=bay\nlane=%d state=bay\n'
                    % (gate_1, gate_2, gate_3))
            for lane in range(lane_count):
                if lane not in (gate_1, gate_2, gate_3):
                    f.write('lane=%d state=empty\n' % lane)

    with _boot(name, device, write_two_gates, 'oneboot',
               extra_env=sim_extra_env) as (
            klippy, dump_ground_truth, tag):
        def run_checked(*args, **kwargs):
            try:
                return harness.run_operation(klippy, *args, **kwargs)
            except Exception as e:
                raise Exception('%s: %s; plant ground truth: %s'
                                % (tag, e, dump_ground_truth()))

        def check_selector(expected_gate, moment):
            """LINEAR/ROTARY: the axis sits at `expected_gate`'s own gate
            position (a pure SERVO image has no selector axis). INDEXED
            folds both sides through the drum's own circumference first --
            selector_position_um is the plant's raw, unwrapped axis counter
            (sim/board/plant.c km_sim_plant_selector_position_um), so a
            correct park after going all the way around once still differs
            from the naive target by a whole circumference, which is not a
            failure. Returns the raw value, so a caller can measure travel
            between two calls (see check_indexed_travel)."""
            gt = dump_ground_truth()[0]
            got = gt.get('selector_position_um')
            if indexed is not None:
                position_of, pitch_um, lane_count, _, half_um = indexed
                circumference_um = pitch_um * lane_count
                want = position_of[expected_gate] * pitch_um
                diff = (got - want) % circumference_um if got is not None else None
                if diff is not None and diff > circumference_um // 2:
                    diff -= circumference_um
                if got is None or abs(diff) > half_um:
                    raise Exception(
                        '%s: %s: selector_position_um=%s, expected gate %d '
                        'at ~%dum (mod %dum drum): %s'
                        % (tag, moment, got, expected_gate, want,
                           circumference_um, gt))
                print('%s: %s: selector at gate %d (%s um, %+d um from its '
                     'switch centre ~%d um mod %dum drum)'
                     % (tag, moment, expected_gate, got, diff, want,
                        circumference_um))
                return got
            if positions is None:
                return None
            want = positions[expected_gate]
            if got is None or abs(got - want) > SELECTOR_TOLERANCE_UM:
                raise Exception(
                    '%s: %s: selector_position_um=%s, expected gate %d at '
                    '~%dum: %s' % (tag, moment, got, expected_gate, want, gt))
            print('%s: %s: selector at gate %d (%s um, expected %d um)'
                  % (tag, moment, expected_gate, got, want))
            return got

        def check_engaged(want, moment):
            """The plant's own engaged_gate, for every kind: LINEAR's servo
            grip, ROTARY's axis position, INDEXED's index-switch window
            (sim/board/plant.c indexed_gate_asserted), a pure SERVO image's
            pulse width. Pico MMU's calibrate-first path (servo_pulse_expected
            below) checks the raw servo_pulse_us instead: the plant's own
            per-gate angle table is a compile-time snapshot of this image's
            (empty) CONFIG_KLIPPER_MMU_SELECTOR_SERVO_GATE_ANGLES_DEG, so it
            never learns the angle this scenario calibrates into the
            firmware's live table at runtime (mcu/commands.c's
            command_km_selector_gate_set updates only the firmware side) --
            engaged_gate would just report whichever gate the plant's own
            stale (all-zero) table happens to match, not the real one."""
            gt = dump_ground_truth()[0]
            if servo_pulse_expected:
                got = gt.get('servo_pulse_us')
                target = servo_pulse_expected[want]
                if got is None or abs(got - target) > SELECTOR_SERVO_TOLERANCE_US:
                    raise Exception(
                        '%s: %s: servo_pulse_us=%s, wanted ~%dus for gate %d '
                        '(calibrated at runtime, this image ships no gate '
                        'angle table): %s' % (tag, moment, got, target, want, gt))
                print('%s: %s: servo_pulse_us=%s as expected for gate %d '
                     '(calibrated at runtime)' % (tag, moment, got, want))
                return
            got = gt.get('engaged_gate')
            if got != want:
                raise Exception('%s: %s: engaged_gate=%s, wanted %s: %s'
                                % (tag, moment, got, want, gt))
            print('%s: %s: engaged_gate=%s as expected' % (tag, moment, got))

        def check_encoder_travel(before, after, moment):
            """Only for a device that wires the shared-path encoder
            (mcu/encoder.c) in this image, e.g. ERCF 2.0 (mcu/Kconfig.control
            defaults it on for the whole ERCF family). before/after are two
            of this lane's own dump_ground_truth() rows spanning a window
            that only ever moves this gear forward (LOAD's own HUB-to-
            arrival feed, before FOLLOW gets a chance to reverse anything).
            The plant's own click generator (sim/board/plant.c
            km_sim_plant_encoder_step, called from the exact gear STEP edge
            the firmware's real encoder driver polls every control tick --
            mcu/encoder.c) should track this lane's real travel (position_um)
            to within one click's rounding at each end of the window.
            encoder_um_per_click comes from ground truth, not repeated here:
            the firmware carries no encoder resolution setting of its own to
            read instead (checked: mcu/Kconfig.pins/.control and
            include/klipper_mmu/device_profile.h have no such field), so
            sim/board/plant.c's KM_SIM_PLANT_ENCODER_A_PIN comment records
            that finding and where this sim-only number comes from."""
            per_click = after['encoder_um_per_click']
            d_travel = after['position_um'] - before['position_um']
            d_clicks = after['encoder_clicks'] - before['encoder_clicks']
            if d_clicks <= 0:
                raise Exception('%s: %s: encoder produced no clicks over %d um '
                                'of real travel: before=%s after=%s'
                                % (tag, moment, d_travel, before, after))
            click_travel = d_clicks * per_click
            diff = click_travel - d_travel
            if abs(diff) > 2 * per_click:
                raise Exception(
                    '%s: %s: encoder clicks (%d, %d um) do not track %d um of '
                    'real travel within 2 clicks (%d um each): before=%s after=%s'
                    % (tag, moment, d_clicks, click_travel, d_travel, per_click,
                       before, after))
            print('%s: %s: encoder ticked %d clicks (%d um) for %d um of real '
                 'travel (resolution %d um/click)'
                 % (tag, moment, d_clicks, click_travel, d_travel, per_click))

        def check_indexed_travel(before_um, after_um, gate_from, gate_to, moment):
            """INDEXED only: the net *raw* selector travel actually driven
            between two known positions, which (unlike check_selector's
            folded position) tells the two possible paths apart -- ViViD's
            real gate_order lets it pick whichever of the two directions is
            shorter (selector.h _best_rotation_direction); forward-only
            Claymore (no order) always drives KM_DIR_FORWARD
            (extras/AFC_vivid.py:159-199 select_lane), so its own 'shorter'
            is always the full-forward hop, never a reverse one."""
            position_of, pitch_um, lane_count, forward_only, half_um = indexed
            circumference_um = pitch_um * lane_count
            steps = (position_of[gate_to] - position_of[gate_from]) % lane_count
            want = steps * pitch_um
            if not forward_only and want > circumference_um // 2:
                want -= circumference_um
            got = after_um - before_um
            if abs(got - want) > 2 * half_um:  # Each end anywhere in its window.
                raise Exception(
                    '%s: %s: selector travelled %dum (before=%dum '
                    'after=%dum), expected %dum (%s)'
                    % (tag, moment, got, before_um, after_um, want,
                       'forward-only' if forward_only else 'shortest hop'))
            print('%s: %s: selector travelled %dum as expected (%s)'
                 % (tag, moment, got,
                    'forward-only' if forward_only else 'shortest hop'))

        if calibrate_gate_angles is not None:
            # Save each gate's angle through the exact host path
            # docs/selectors.md's "Calibrating a Selector Machine" (SERVO
            # kind) describes, before touching anything else: no
            # selector_calibration_file is configured on this sim printer
            # (write_printer_config), so SAVE=0 only applies it to the
            # firmware's live table for this boot, same as the doc's own
            # SAVE=0 example. servo_pulse_expected (computed before boot,
            # above) also already seeded the plant's own KM_SIM_PLANT_SERVO_
            # GATE_PULSE_US, so it recognizes these exact pulses too.
            for gate, deg in sorted(calibrate_gate_angles.items()):
                run_macro(klippy, 'KLIPPER_MMU_CALIBRATE_SELECTOR MMU=box '
                                  'GATE=%d ANGLE=%d SAVE=0' % (gate, deg))
            print('%s: calibrated gate angle(s) at runtime (no gate angle '
                 'table shipped): %s -> expected pulse widths %s'
                 % (tag, calibrate_gate_angles, servo_pulse_expected))
        elif indexed is not None:
            print('%s: INDEXED has no per-gate calibration to exercise '
                 '(gates are found by their own switch, docs/selectors.md '
                 "\"Indexed Gates\"): skipping KLIPPER_MMU_CALIBRATE_"
                 'SELECTOR for this kind.' % tag)

        run_checked('PRELOAD', gate_1, 'PRELOAD', EVENT_PRELOAD_COMPLETE,
                    start_timeout=30.0)
        pos_gate1 = check_selector(released_gate(gate_1),
                                   'after PRELOAD gate %d' % gate_1)
        check_engaged(released_engage(gate_1),
                      'after PRELOAD gate %d' % gate_1)

        run_checked('PRELOAD', gate_2, 'PRELOAD', EVENT_PRELOAD_COMPLETE,
                    start_timeout=30.0)
        pos_gate2 = check_selector(released_gate(gate_2),
                                   'after PRELOAD gate %d' % gate_2)
        check_engaged(released_engage(gate_2),
                      'after PRELOAD gate %d' % gate_2)
        print('%s: PRELOAD gates %d and %d passed.' % (tag, gate_1, gate_2))
        if indexed is not None:
            check_indexed_travel(pos_gate1, pos_gate2, gate_1, gate_2,
                                 'PRELOAD %d->%d' % (gate_1, gate_2))

        if kind in ('LINEAR', 'ROTARY'):
            # Calibrate the gate the axis actually rests at after PRELOAD
            # gate_2 (a ROTARY release can park it elsewhere) and compare the
            # measured travel with its compiled position.
            settle_gate = released_gate(gate_2)
            run_macro(klippy, 'KLIPPER_MMU_MOTORS_OFF MMU=box')
            cal_lines = run_macro(klippy, 'KLIPPER_MMU_CALIBRATE_SELECTOR MMU=box '
                                          'GATE=%d SAVE=0' % settle_gate)
            cal_text = ' '.join(cal_lines)
            m = re.search(r'measured (\d+) step', cal_text)
            if not m:
                raise Exception('%s: CALIBRATE_SELECTOR reported no measured '
                                'step count: %s' % (tag, cal_lines))
            measured_steps = int(m.group(1))
            expected_steps = image_selector_um_to_steps(positions[settle_gate])
            tolerance_steps = max(image_selector_um_to_steps(SELECTOR_TOLERANCE_UM), 1)
            if abs(measured_steps - expected_steps) > tolerance_steps:
                raise Exception(
                    '%s: CALIBRATE_SELECTOR gate %d measured %d step(s), '
                    'expected %d +/- %d: %s'
                    % (tag, settle_gate, measured_steps, expected_steps,
                       tolerance_steps, cal_lines))
            print('%s: CALIBRATE_SELECTOR gate %d measured %d step(s), '
                 'expected %d +/- %d step(s)'
                 % (tag, settle_gate, measured_steps, expected_steps, tolerance_steps))

        run_macro(klippy, 'KLIPPER_MMU_CALIBRATE MMU=box BAY=%d' % gate_1)
        before_load = dump_ground_truth()[gate_1]
        run_macro(klippy, 'OAMSM_LOAD_FILAMENT GROUP=T%d' % gate_1)
        pos_load1 = check_selector(gate_1, 'after LOAD gate %d' % gate_1)
        check_engaged(gate_1, 'after LOAD gate %d' % gate_1)
        if calibrated_gate is not None and calibrated_gate == gate_1:
            # check_selector has no axis to check for SERVO; read the pulse
            # while gate_1 is still engaged.
            got = dump_ground_truth()[0].get('servo_pulse_us')
            if got is None or abs(got - calibrated_pulse) > SELECTOR_SERVO_TOLERANCE_US:
                raise Exception(
                    '%s: after LOAD gate %d: servo_pulse_us=%s, wanted ~%d '
                    'for its freshly calibrated angle: %s'
                    % (tag, gate_1, got, calibrated_pulse, dump_ground_truth()[0]))
            print('%s: after LOAD gate %d: servo_pulse_us=%s matches its '
                 'freshly calibrated angle as expected'
                 % (tag, gate_1, got))
        if cfg.get('CONFIG_KLIPPER_MMU_ENCODER') == '1':
            after_load = dump_ground_truth()[gate_1]
            check_encoder_travel(before_load, after_load, 'LOAD gate %d' % gate_1)
        if indexed is not None:
            check_indexed_travel(pos_gate2, pos_load1, gate_2, gate_1,
                                 'LOAD %d->%d (reselect after release)'
                                 % (gate_2, gate_1))

        baseline = dump_ground_truth()[0]['extruder_consumed_um']
        klippy.call('gcode/script',
                    {'script': 'G91\nG1 E%d F120\nG90' % EXTRUDE_MM})
        deadline = time.monotonic() + 60.0
        consumed = baseline
        while time.monotonic() < deadline:
            consumed = dump_ground_truth()[0]['extruder_consumed_um']
            if consumed - baseline >= CONSUMED_TARGET_UM:
                break
            time.sleep(0.5)
        else:
            raise Exception('%s: extruder move only credited %d um '
                            '(baseline %d, now %d)'
                            % (tag, consumed - baseline, baseline, consumed))
        print('%s: extruder consumed %d um' % (tag, consumed - baseline))

        run_macro(klippy, 'OAMSM_UNLOAD_FILAMENT')
        check_selector(released_gate(gate_1), 'after UNLOAD gate %d' % gate_1)
        check_engaged(released_engage(gate_1),
                      'after UNLOAD gate %d' % gate_1)

        run_macro(klippy, 'OAMSM_LOAD_FILAMENT GROUP=T%d' % gate_2)
        check_selector(gate_2, 'after LOAD gate %d' % gate_2)
        check_engaged(gate_2, 'after LOAD gate %d' % gate_2)

        # Clear the shared path, then eject the untouched third lane.
        run_macro(klippy, 'OAMSM_UNLOAD_FILAMENT')
        check_selector(released_gate(gate_2), 'after final UNLOAD gate %d' % gate_2)
        check_engaged(released_engage(gate_2), 'after final UNLOAD gate %d' % gate_2)

        check_eject(run_checked, klippy, dump_ground_truth, gate_3, tag, cfg)

        print('%s: %s selector scenario passed.' % (tag, name))


def run_device(name):
    device = DEVICES[name]
    if name in ('ercf_2_0', 'chameleon_3d', 'mmx', 'vivid', 'claymore',
               'ercf_1_1', 'ercf_3_0', 'jack_rabbit', 'tradrack', 'htlf',
               'mmx6', 'low_rider', 'pico_mmu'):
        selector_scenario(name, device)
    else:
        phase_one_boot(name, device)


def run_device_retrying(name):
    """run_device(), retrying once if the failure coincides with a
    host-stall-sized wall_gap_us. control_smoke.py, fault_check.py's run()
    (and, through it, load_scenarios.py) already retry this way; this file
    never did, which is why a host scheduling hiccup during 3D Chameleon's
    selector homing or after an UNLOAD showed up as a hard
    SENSOR_FAILURE/CONTROL_TIMING failure instead of the same clean retry.
    _boot()'s own suffix is always 'oneboot' (one boot per device call), so
    the gap report path is fixed per device rather than numbered per
    attempt; that's fine here since the file is read immediately on
    failure, before a retry's own boot truncates it again."""
    path = os.path.abspath('build/linux-mcu/timer-gaps-%s-oneboot.txt' % name)
    first = None
    for attempt in (1, 2):
        try:
            run_device(name)
            if first is not None:
                print('%s: attempt 1 failed (%s); the retry passed.'
                     % (name, first))
            return
        except Exception as exc:
            gap = smoke.worst_wall_gap_us(path)
            if (attempt == 2 or gap is None
                    or gap <= smoke.SWITCH_FRESHNESS_BUDGET_US):
                raise
            first = exc
            print('%s attempt 1 failed: %s' % (name, exc))
            print('its worst wall gap was %s us, over the %d us '
                 'switch-sample freshness budget: a host stall, so it is '
                 'retried once.' % (gap, smoke.SWITCH_FRESHNESS_BUDGET_US))


def main():
    skip_unless_available('tests/linux_mcu/device_scenarios.py')
    names = sys.argv[1:] or list(DOSSIER_DEVICES)
    unknown = [n for n in names if n not in DEVICES or n == 'boxturtle']
    if unknown:
        raise SystemExit('unknown device(s): %s (have %s)'
                         % (', '.join(unknown), ', '.join(DOSSIER_DEVICES)))
    failed = []
    for name in names:
        try:
            run_device_retrying(name)
        except Exception as e:
            print('%s FAILED: %s' % (name, e))
            failed.append(name)
    if failed:
        raise SystemExit('Device scenarios failed: %s' % ', '.join(failed))
    print('All device scenarios passed: %s' % ', '.join(names))


if __name__ == '__main__':
    main()
