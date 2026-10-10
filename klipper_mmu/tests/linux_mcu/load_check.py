#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Phase 2b-2a: a LOAD traced through the plant's buffer, toolhead and
extruder model (sim/board/plant.c) on the sim board. See
docs/plan/linux-mcu-integration.md and tests/linux_mcu/README.md.

preload_check.py stops at the LOAD switch, short of the HUB. This check
starts lane 0 already there (plant state "load") and lets the real
OAMSM_LOAD_FILAMENT drive the strand past the HUB and into the buffer
between the feeder and the extruder's own drive gears, which the plant
models as 15 mm of travel (real BoxTurtle FPS geometry) and reports on
DiffA/DiffB. FPS arrival and the follower therefore see a buffer that
moves, fills and empties: the extruder's STEP/DIR pulses (PF0/PF1) are
counted as filament drawn, and that draw is what compresses the buffer
less again.

This branch's LOAD calibrates before it feeds: KLIPPER_MMU_CALIBRATE BAY=0
runs first, at the crawl speed, and only then does OAMSM_LOAD_FILAMENT run,
aimed at the distance it just measured.
KM_SIM_PLANT_TOOLHEAD_MM is raised to 600 mm (default 150) so there is room
to see the calibrated LOAD actually reach cruise before it has to slow down
for the arrival window; the plant's buffer stays the real 15 mm BoxTurtle
FPS stroke.

What makes this worth running is the trace, not the verdict: for the whole
scenario the control socket's ground truth is sampled every 50 ms and the
firmware's own DIAGNOSE state every 500 ms, into
build/linux-mcu/load-trace.csv, and summarised when the check fails. The
follower is bang-bang (feed below ~250 permille, coast above ~750) and this
image feeds up to 18000 steps/s, so a 15 mm buffer is expected to fill
faster than the follower can stop feeding -- the trace is what says how
fast and by how much. The assertions are 467f74d's, unchanged: the buffer
stays inside 100..900 permille while extruding 50 mm, the extruder consumes
at least 49 mm of filament, and the strand ends up behind the HUB after
UNLOAD. Added on this branch: during the LOAD itself (HUB to arrival, before
extrusion starts) the buffer never passes 900 permille either, and the
feeder is traced going above 150 mm/s at some point -- proof it actually
cruised rather than crawling the whole 600 mm.
"""
import os
import re
import signal
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import preload_check as harness
import control_smoke as smoke
from build_root import skip_unless_available  # noqa: E402

TRACE_PATH = os.path.abspath('build/linux-mcu/load-trace.csv')
TRUTH_PERIOD_S = 0.05
DIAGNOSE_PERIOD_S = 0.5
BUFFER_MIN_PERMILLE = 100
BUFFER_MAX_PERMILLE = 900
CONSUMED_TARGET_UM = 49000
EXTRUDE_MM = 50
MIN_LOAD_SPEED_MM_S = 150
# Default 150 mm; long enough to see the calibrated LOAD actually cruise
# before it slows for the arrival window (BoxTurtle: 200 mm/s ceiling, a
# 50 mm window, 500 mm/s^2 accel/braking -- roughly 80 mm of ramp either
# side of a plateau).
os.environ.setdefault('KM_SIM_PLANT_TOOLHEAD_MM', '600')
# The extrusion moves 50 mm at the extruder's max_extrude_only_velocity
# (10 mm/s); the wait is a generous bound, not an expected duration.
EXTRUSION_TIMEOUT_S = 240.0


def write_load_plant(path):
    """Lane 0's tip starts at the LOAD switch, already past BAY, so the load
    this check traces is the one the plant's buffer model is about: HUB and
    beyond. The rest stay empty."""
    with open(path, 'w') as f:
        f.write('lane=0 state=load\n')
        for lane in (1, 2, 3):
            f.write('lane=%d state=empty\n' % lane)


def release(mcu, truth_path):
    """SIGUSR1 the MCU for a ground-truth dump, then read it back: the same
    plant view preload_check.py asserts against, one file snapshot at a
    time."""
    os.kill(mcu.pid, signal.SIGUSR1)
    time.sleep(0.2)
    return harness.read_ground_truth(truth_path)


class Trace:
    """The LOAD, recorded while it happens: one CSV row per truth sample.

    The truth comes from the control socket, which an MCU-process timer
    services, so it keeps answering while klippy is busy (or blocked) with
    the load. The firmware's own state comes from KLIPPER_MMU_DIAGNOSE on a
    second API connection; DIAGNOSE takes klippy's MCU lock, which the load
    itself holds between steps, so it is allowed to time out -- recorded
    blank and retried, never fatal. Two threads, one writer: only the truth
    thread appends rows."""

    COLUMNS = ('t_s', 'feeder_um', 'buffer_permille', 'toolhead',
               'extruder_consumed_um', 'fw_state', 'fw_stage')

    def __init__(self, run, path):
        self.run = run
        self.path = path
        self.control = smoke.Control(run.control_path)
        self.rows = []
        self.fw_failures = 0
        self._lock = threading.Lock()
        self._fw = ('', '')
        self._t0 = None
        self._stop = threading.Event()
        self._fw_klippy = None
        self._threads = []

    def _open_firmware_client(self):
        """A second klippy connection: the first one belongs to the scenario,
        and this one has to keep talking while that one runs a script."""
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(self.run.klippy.sock.getpeername())
        s.settimeout(5.0)
        klippy = harness.Klippy(s)
        klippy.call('gcode/subscribe_output',
                    {'response_template': {'method': 'notify_gcode_response'}})
        return klippy

    def start(self):
        self._t0 = time.monotonic()
        self._fw_klippy = self._open_firmware_client()
        for target in (self._truth_loop, self._firmware_loop):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self._threads.append(thread)

    def _truth_loop(self):
        while not self._stop.is_set():
            deadline = time.monotonic() + TRUTH_PERIOD_S
            try:
                lane0 = smoke.truth_lanes(self.control)[0]
                with self._lock:
                    fw_state, fw_stage = self._fw
                    self.rows.append((time.monotonic() - self._t0,
                                      lane0['position_um'],
                                      lane0['buffer_permille'],
                                      lane0['toolhead'],
                                      lane0['extruder_consumed_um'],
                                      fw_state, fw_stage))
            except Exception as exc:
                # A missing sample is a gap in the trace, not a failed test.
                print('trace: truth sample failed: %r' % exc)
            time.sleep(max(0.0, deadline - time.monotonic()))

    def _firmware_loop(self):
        while not self._stop.is_set():
            deadline = time.monotonic() + DIAGNOSE_PERIOD_S
            try:
                _, lines = self._fw_klippy.call(
                    'gcode/script', {'script': 'KLIPPER_MMU_DIAGNOSE MMU=box'},
                    timeout=2.0)
                text = ' '.join(lines)
                state = re.search(r'state=(\S+)', text)
                stage = re.search(r'stage=(\S+)', text)
                with self._lock:
                    self._fw = (state.group(1) if state else '',
                                stage.group(1) if stage else '')
            except Exception as exc:
                self.fw_failures += 1
                if self.fw_failures == 1:
                    print('trace: DIAGNOSE unavailable (%r); firmware state '
                          'will be blank where it could not be read' % exc)
            time.sleep(max(0.0, deadline - time.monotonic()))

    def state(self):
        with self._lock:
            return self._fw

    def now(self):
        """Elapsed trace time, the same clock self.rows' t_s column uses."""
        return time.monotonic() - self._t0

    def snapshot(self):
        """A coherent copy of the rows so far; self.rows keeps growing."""
        with self._lock:
            return list(self.rows)

    def stop(self):
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=10.0)
        self.control.close()
        if self._fw_klippy is not None:
            self._fw_klippy.sock.close()
        with open(self.path, 'w') as f:
            f.write(','.join(self.COLUMNS) + '\n')
            for row in self.rows:
                f.write('%.3f,%d,%d,%d,%d,%s,%s\n' % row)
        duration = self.rows[-1][0] if self.rows else 0.0
        print('trace: %d rows over %.1fs -> %s' % (len(self.rows), duration,
                                                   self.path))


def parse_trace(path):
    """The CSV back as rows, so a summary survives the run that wrote it."""
    if not os.path.exists(path):
        return []
    rows = []
    with open(path) as f:
        for line in f.read().splitlines()[1:]:
            cols = line.split(',')
            if len(cols) != 7:
                continue
            rows.append((float(cols[0]), int(cols[1]), int(cols[2]),
                         int(cols[3]), int(cols[4]), cols[5], cols[6]))
    return rows


def summarise(path):
    """What the trace says about the buffer: how high it got, the first
    moment it passed 900 permille and how fast the feeder was driving into
    the second before that, and where the last sample left things."""
    rows = parse_trace(path)
    if not rows:
        return ['no trace rows in %s' % path]
    lines = []
    peak = max(rows, key=lambda r: r[2])
    lines.append('buffer peak %d permille at t=%.2fs' % (peak[2], peak[0]))
    over = next((r for r in rows if r[2] > BUFFER_MAX_PERMILLE), None)
    if over is None:
        lines.append('buffer never passed %d permille' % BUFFER_MAX_PERMILLE)
    else:
        before = next((r for r in reversed(rows) if r[0] <= over[0] - 1.0),
                      rows[0])
        dt = over[0] - before[0]
        speed = (over[1] - before[1]) / 1000.0 / dt if dt > 0.0 else 0.0
        lines.append('first buffer > %d permille at t=%.2fs: feeder %.1f mm/s '
                     'over the preceding %.2fs, tip %.1f mm from the feeder'
                     % (BUFFER_MAX_PERMILLE, over[0], speed, dt,
                        over[1] / 1000.0))
    last = rows[-1]
    lines.append('last sample t=%.2fs: feeder %.1f mm, buffer %d permille, '
                 'toolhead=%d, extruder consumed %.1f mm'
                 % (last[0], last[1] / 1000.0, last[2], last[3],
                    last[4] / 1000.0))
    states = sorted({r[5] for r in rows if r[5]})
    lines.append('firmware states seen: %s'
                 % (', '.join(states) if states else 'none recorded'))
    return lines


def scenario(run):
    """One traced LOAD, extrusion and UNLOAD on lane 0, asserted against the
    plant's ground truth the way 467f74d did."""
    mcu = run.mcu
    trace = Trace(run, TRACE_PATH)
    trace.start()
    try:
        def ground_truth():
            return release(mcu, run.truth)

        def run_macro(script, assert_following=False):
            """Issue a gcode script, absorbing OpenAMS's bootstrap window
            (its own state cache needs a lane's reservation to catch up with
            the firmware's last terminal event, which lags the poll that
            just observed it), then optionally wait for the firmware to
            report the follower engaged."""
            deadline = time.monotonic() + 30.0
            while True:
                print('Running %s...' % script)
                resp, lines = run.klippy.call('gcode/script',
                                              {'script': script},
                                              timeout=180.0)
                if 'error' not in resp:
                    break
                if ('OpenAMS motion unavailable' in str(resp['error'])
                        and time.monotonic() < deadline):
                    time.sleep(0.5)
                    continue
                raise Exception('%s failed: %s' % (script, resp['error']))
            print('%s output: %s' % (script, lines))
            if assert_following:
                # The firmware's own account of the load, sampled by the
                # trace's DIAGNOSE thread: the feeder is only "following" the
                # buffer once the strand has actually reached it.
                for _ in range(75):
                    if trace.state()[0] == 'following':
                        return
                    time.sleep(0.2)
                raise Exception('the firmware never reported state=following '
                                'after %s (last: %s)' % (script, trace.state()))

        # The extruder's DIR pin (PF1, high = forward) and its STEP counter
        # are the plant's only witness that the extruder pulled anything, so
        # a small forward move has to show up as consumed filament before the
        # buffer's arithmetic means anything. Wait for it to finish: this
        # config runs the extruder at ~0.27 mm/s (max_velocity scaled by the
        # extruder's own ratio), so an E5 takes about 19s, and leaving it
        # moving would have it eat into the buffer the load is about to fill.
        run.klippy.call('gcode/script', {'script': 'G91\nG1 E5 F120\nG90'})
        deadline = time.monotonic() + 60.0
        consumed = 0
        while time.monotonic() < deadline:
            consumed = ground_truth()[0]['extruder_consumed_um']
            if consumed >= 4900:
                break
            time.sleep(0.5)
        print('Extruder DIR/STEP self-check: consumed=%d um' % consumed)
        if consumed < 4900:
            raise Exception('a forward extruder move credited only %d um of '
                            'filament; the plant is not counting PF0/PF1'
                            % consumed)

        # Calibrate before loading: the owner's own order. LOAD is refused
        # without a calibrated HUB-to-arrival distance, and firmware adopts
        # the measured one at once, so the LOAD right after aims at it with
        # no restart.
        run_macro('KLIPPER_MMU_CALIBRATE MMU=box BAY=0')

        load_start = trace.now()
        run_macro('OAMSM_LOAD_FILAMENT GROUP=T0', assert_following=True)
        load_end = trace.now()
        load_rows = [r for r in trace.snapshot() if load_start <= r[0] <= load_end]
        if not load_rows:
            raise Exception('no trace rows captured during the LOAD (%.2f..%.2fs)'
                            % (load_start, load_end))
        peak_load_buffer = max(r[2] for r in load_rows)
        if peak_load_buffer > BUFFER_MAX_PERMILLE:
            raise Exception('buffer reached %d permille during LOAD itself, '
                            'before extrusion (> %d)'
                            % (peak_load_buffer, BUFFER_MAX_PERMILLE))
        peak_load_speed = max(
            (max(0.0, cur[1] - prev[1]) / 1000.0 / (cur[0] - prev[0])
             for prev, cur in zip(load_rows, load_rows[1:]) if cur[0] > prev[0]),
            default=0.0)
        if peak_load_speed < MIN_LOAD_SPEED_MM_S:
            raise Exception(
                'the feeder never traced above %d mm/s during LOAD (peak '
                '%.1f mm/s over %s mm of toolhead distance); the calibrated '
                'LOAD never reached cruise' % (
                    MIN_LOAD_SPEED_MM_S, peak_load_speed,
                    os.environ['KM_SIM_PLANT_TOOLHEAD_MM']))
        print('LOAD phase (%.2f..%.2fs): peak buffer %d permille, peak feeder '
             'speed %.1f mm/s' % (load_start, load_end, peak_load_buffer,
                                  peak_load_speed))

        run.klippy.call('gcode/script',
                        {'script': 'G91\nG1 E%d F120\nG90' % EXTRUDE_MM})

        deadline = time.monotonic() + EXTRUSION_TIMEOUT_S
        last_consumed = 0
        while time.monotonic() < deadline:
            lane0 = ground_truth()[0]
            last_consumed = lane0['extruder_consumed_um']
            if not (BUFFER_MIN_PERMILLE <= lane0['buffer_permille']
                    <= BUFFER_MAX_PERMILLE):
                raise Exception(
                    'buffer permille out of bounds: %d (toolhead=%d, '
                    'extruder consumed %d um)' % (lane0['buffer_permille'],
                                                  lane0['toolhead'],
                                                  last_consumed))
            if last_consumed >= CONSUMED_TARGET_UM:
                break
            time.sleep(0.5)

        if last_consumed < CONSUMED_TARGET_UM:
            raise Exception('extrusion did not complete, consumed only %d um'
                            % last_consumed)

        gt = ground_truth()
        print('Ground truth after Extrude:', gt[0])

        run_macro('OAMSM_UNLOAD_FILAMENT')

        gt = ground_truth()
        print('Ground truth after UNLOAD:', gt[0])
        if not (gt[0]['bay'] == 1 and gt[0]['load'] == 0 and gt[0]['hub'] == 0):
            raise Exception('plant ground truth disagrees with UNLOAD '
                            'success: %s' % gt[0])
        print('LOAD/Extrude/UNLOAD plant-in-the-loop check passed.')
    finally:
        trace.stop()


def main():
    skip_unless_available('tests/linux_mcu/load_check.py')
    passed = False
    try:
        smoke.attempt(1, tag='load', plant=write_load_plant, checks_fn=scenario)
        passed = True
    finally:
        print('Trace summary (%s):' % TRACE_PATH)
        for line in summarise(TRACE_PATH):
            print('  ' + line)
    if passed:
        print('LOAD/Extrude/UNLOAD trace check passed.')


if __name__ == '__main__':
    main()
