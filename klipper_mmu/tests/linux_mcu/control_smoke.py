#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Phase 3a: the sim MCU's control socket (sim/board/control.c), with the
MCU process, klippy and the real firmware in the loop. See
docs/plan/linux-mcu-integration.md, "A control socket", and
tests/linux_mcu/README.md.

This is the socket's own smoke test, not a fault scenario: it starts the
actuation-permitted sim image exactly as preload_check.py does (same build,
same calibration record, same printer config, real-time priority when the
host allows it), with every lane empty and KM_SIM_CONTROL pointing at a
short-lived Unix socket, and then checks that a test can reach into the
running MCU and change what it believes:

  - "truth" answers with the plant's ground truth, one line, four lanes,
    matching the empty plant we started (so it agrees with what SIGUSR1
    would have dumped);
  - "switch <lane 0's BAY pin> stuck 0" makes lane 0's BAY switch read
    made, which KLIPPER_MMU_PHYSICAL reports as bay0=present (PHYSICAL is
    the read-only capture DIAGNOSE itself builds on; BAY is active-low on
    this wiring, so level 0 is made) and "truth" reports as bay=0, and
    "switch ... free" puts both back;
  - "plant block 0 20000" shows up in "truth" as block_um=20000 (and
    "plant unblock 0" as -1), "plant runout 0" as runout=1;
  - an unknown command and an unusable argument each get an error line;
  - "tmc <lane 0's UART pin> fault on/off" is accepted (the fault bit
    itself is checked by tests/linux_mcu/tmc2209_model_test).

A failure that coincides with a large wall_gap_us in the timer gap report
is a host stall, not a firmware problem: this script reads that report and
retries the whole attempt once, reporting both.
"""
import collections
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
import preload_check as harness
from build_root import KLIPPY_PYTHON, skip_unless_available  # noqa: E402

# sim/board/plant.c's ground-truth fields, once per lane. The last three are
# the shared buffer/toolhead/extruder model (phase 2b-2a): the buffer's
# compression in permille of its 15mm travel, whether the lane's strand
# reaches the extruder's grip point, and how far the extruder has drawn.
LANE_FIELDS = ('lane', 'position_um', 'steps', 'dir_changes', 'bay', 'load',
               'hub', 'block_um', 'runout', 'buffer_permille', 'toolhead',
               'extruder_consumed_um')

# Lane 0's pins, from sim/board/plant.c's tables for this sim board's one
# topology (BoxTurtle device profile, AFC Lite wiring).
LANE0_BAY_PIN = 'PE13'
LANE0_TMC_UART_PIN = 'PD5'

# What an empty lane's ground truth says. BAY is active-low on this wiring
# (mcu/Kconfig.switch KLIPPER_MMU_BAY_ACTIVE_HIGH="0,0,0,0"), so an open
# BAY reads electrical 1; LOAD and HUB are active-high. block_um=-1 is
# plant.c's "no jam".
# Ground truth is physical presence (1 = filament there), not pin levels.
EMPTY_LANE = {'position_um': 0, 'steps': 0, 'dir_changes': 0, 'bay': 0,
              'load': 0, 'hub': 0, 'block_um': -1, 'runout': 0,
              'buffer_permille': 0, 'toolhead': 0, 'extruder_consumed_um': 0}

# The firmware's switch-sample freshness budget (mcu/switch_gpio.h), in us:
# a wall gap beyond it can make a switch sample stale ("unknown" instead of
# made/open), so it is also the line between a slow host and a broken test.
SWITCH_FRESHNESS_BUDGET_US = 2500


class Control:
    """One control-socket client: one command line out, one reply line in."""

    def __init__(self, path, timeout=5.0):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(path)
        self.buf = b''

    def command(self, line):
        self.sock.sendall((line + '\n').encode())
        while b'\n' not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise Exception('control socket closed awaiting reply to %r'
                                % line)
            self.buf += chunk
        raw, self.buf = self.buf.split(b'\n', 1)
        return raw.decode()

    def expect_ok(self, line):
        reply = self.command(line)
        if not reply.startswith('ok '):
            raise Exception('control %r: expected "ok ...", got %r'
                            % (line, reply))
        print('control %-26s -> %s' % (line, reply))
        return reply

    def expect_error(self, line):
        reply = self.command(line)
        if not reply.startswith('error '):
            raise Exception('control %r: expected "error ...", got %r'
                            % (line, reply))
        print('control %-26s -> %s' % (line, reply))
        return reply

    def close(self):
        self.sock.close()


def truth_lanes(control):
    """The `truth` reply as one {field: int} dict per lane, in lane order."""
    reply = control.command('truth')
    if not reply.startswith('ok '):
        raise Exception('control truth: expected "ok ...", got %r' % reply)
    lanes = []
    for chunk in reply[3:].split('lane=')[1:]:
        fields = dict(re.findall(r'(\w+)=(-?\d+)', 'lane=' + chunk))
        missing = [name for name in LANE_FIELDS if name not in fields]
        if missing:
            raise Exception('truth lane missing %s: %r' % (missing, reply))
        lanes.append({name: int(fields[name]) for name in LANE_FIELDS})
    if len(lanes) != 4:
        raise Exception('truth reported %d lanes: %r' % (len(lanes), reply))
    return lanes


def physical_bay0(klippy):
    """KLIPPER_MMU_PHYSICAL's `bay0=<state>` token, and the line it came
    from. This is the host's own view of lane 0's BAY switch, read back from
    the firmware that read the socket-forced pin."""
    resp, lines = klippy.call('gcode/script',
                              {'script': 'KLIPPER_MMU_PHYSICAL MMU=box'})
    if 'error' in resp:
        raise Exception('KLIPPER_MMU_PHYSICAL failed: %s' % resp)
    for line in lines:
        if 'bay0=' in line:
            return re.search(r'bay0=(\S+)', line).group(1), line
    raise Exception('KLIPPER_MMU_PHYSICAL reported no bay0: %s' % lines)


def write_empty_plant(path):
    """Every lane empty: this test needs no filament anywhere, only the
    switches and the position the plant already reports for nothing."""
    with open(path, 'w') as f:
        for lane in range(4):
            f.write('lane=%d state=empty\n' % lane)


def worst_wall_gap_us(path):
    """The largest wall_gap_us in a KM_SIM_TIMER_GAPS_OUT report, or None."""
    if not os.path.exists(path):
        return None
    gaps = [int(m.group(1))
            for m in re.finditer(r'wall_gap_us=(\d+)', open(path).read())]
    return max(gaps) if gaps else None


# One live boot of the stack, handed to a scenario: the Klippy API client, the
# control socket path, the plant ground-truth file, the MCU process, the
# klippy process, the klippy API socket path, the MCU's own argv0/pty path
# and the exact environment it was launched with. klippy_proc/api_socket are
# for a scenario that restarts klippy itself (RESTART/FIRMWARE_RESTART): it
# needs the old process to wait on and the fixed path to reconnect to.
# mcu/pty_path/mcu_env are for a scenario that kills and restarts the sim MCU
# process directly, at the same pty path and with the same KM_SIM_* settings.
Run = collections.namedtuple(
    'Run', 'klippy control_path truth mcu klippy_proc api_socket '
           'mcu_elf pty_path mcu_env')


def restart_mcu(run, realtime, log_path):
    """Kill the sim MCU process and start a fresh one at the same pty path
    with the same environment, simulating a real MCU power cycle (a plain
    process kill, not the firmware's own `reset` command/km_sim's execv, so
    it needs no cooperation from the dying process). klippy's connection to
    the old process is left to notice the drop on its own; the caller still
    has to tell klippy to reconnect (RESTART, or FIRMWARE_RESTART if it
    should also request the MCU-side reset command first). Returns the new
    Popen; the caller owns killing it, since attempt()'s own cleanup only
    knows about the original process object."""
    run.mcu.kill()
    run.mcu.wait(timeout=10)
    if os.path.exists(run.pty_path):
        os.unlink(run.pty_path)
    return subprocess.Popen(
        [run.mcu_elf] + (['-r'] if realtime else []) + ['-I', run.pty_path],
        stdout=open(log_path, 'w'), stderr=subprocess.STDOUT, env=run.mcu_env)


def connect_klippy(api_socket, timeout=30.0):
    """A fresh Klippy API connection, subscribed to gcode output and waiting
    for `ready`: the same handshake attempt() does on first boot, factored
    out so a scenario that restarts klippy's own process (not the MCU) can
    redo it against the same, fixed socket path once klippy recreates it."""
    deadline = time.monotonic() + timeout
    s = None
    while True:
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(10.0)
            s.connect(api_socket)
            break
        except OSError:
            s.close()
            if time.monotonic() >= deadline:
                raise Exception('klippy API socket %s never accepted a '
                                'connection' % api_socket)
            time.sleep(0.1)
    klippy = harness.Klippy(s)
    klippy.call('gcode/subscribe_output',
                {'response_template': {'method': 'notify_gcode_response'}})
    while time.monotonic() < deadline:
        info, _ = klippy.call('info')
        if info.get('result', {}).get('state') == 'ready':
            return klippy
        time.sleep(0.25)
    raise Exception('klippy never became ready on %s' % api_socket)


def wait_diagnostic_ready(klippy, timeout=50.0):
    """Poll KLIPPER_MMU_QUERY until diagnostic startup settles; the same loop
    attempt() runs on first boot, reused by a scenario that needs it again
    after reconnecting (a restarted MCU redoes diagnostic startup; a
    restarted klippy alone just re-reads the board's already-settled state)."""
    status = {}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp, lines = klippy.call(
            'gcode/script', {'script': 'KLIPPER_MMU_QUERY MMU=box'})
        if 'error' in resp:
            raise Exception('KLIPPER_MMU_QUERY failed: %s' % resp)
        for line in lines:
            if 'startup_state=' in line:
                status = harness.parse_kv(line)
        if status.get('startup_state') in ('ready', 'faulted'):
            break
        time.sleep(0.25)
    if status.get('startup_state') != 'ready' or status.get('startup_result') != '0':
        raise Exception('diagnostic startup did not reach ready: %s' % status)
    return status


def gaps_path_for(number, tag='control'):
    return os.path.abspath('build/linux-mcu/timer-gaps-%s-%d.txt'
                           % (tag, number))


def attempt(number, tag='control', plant=write_empty_plant, checks_fn=None):
    """One full run of the stack: the MCU with KM_SIM_CONTROL set, host code
    installed, klippy booted against the same printer config preload_check.py
    uses, then the checks run against both. `plant(path)` writes the plant's
    initial state (empty lanes by default) and `checks_fn` is what to run
    instead of this file's own `checks`, receiving a Run of the live boot:
    a scenario can therefore boot the whole stack as many times as it likes,
    and `tag` keeps each boot's files apart. Returns the timer gap report's
    path once both processes are down."""
    mcu_elf = os.path.abspath('build/linux-mcu/klipper/out-sim-motion/klipper.elf')
    pty_path = os.path.abspath('build/linux-mcu/mcu-%s.pty' % tag)
    if os.path.exists(pty_path):
        os.unlink(pty_path)

    calibration_path = os.path.abspath('build/linux-mcu/diff-calibration.json')
    harness.write_calibration(calibration_path)
    plant_path = os.path.abspath('build/linux-mcu/plant-%s.txt' % tag)
    plant(plant_path)
    ground_truth_path = os.path.abspath(
        'build/linux-mcu/truth-%s-%d.txt' % (tag, number))
    gaps_path = gaps_path_for(number, tag)
    for path in (ground_truth_path, gaps_path):
        if os.path.exists(path):
            os.unlink(path)

    # AF_UNIX paths are capped near 108 bytes by the kernel, so the socket
    # gets a short-lived temporary home while everything else keeps its
    # long, readable build/linux-mcu path (same split as preload_check.py's
    # klippy API socket).
    control_dir = tempfile.mkdtemp(prefix='kmmu-control-')
    control_path = os.path.join(control_dir, 'control.sock')

    env = dict(os.environ)
    env['KM_SIM_PLANT'] = plant_path
    env['KM_SIM_PLANT_OUT'] = ground_truth_path
    env['KM_SIM_TIMER_GAPS_OUT'] = gaps_path
    env['KM_SIM_CONTROL'] = control_path

    realtime = harness.grant_realtime_priority(mcu_elf)
    print('Sim MCU real-time priority:', 'granted' if realtime else 'unavailable')

    mcu_proc = subprocess.Popen(
        [mcu_elf] + (['-r'] if realtime else []) + ['-I', pty_path],
        stdout=open('build/linux-mcu/mcu-%s.log' % tag, 'w'),
        stderr=subprocess.STDOUT, env=env)
    klippy_proc = None
    try:
        for _ in range(50):
            if os.path.exists(pty_path):
                break
            time.sleep(0.1)
        else:
            raise Exception('MCU never created its pty')
        for _ in range(50):
            if os.path.exists(control_path):
                break
            time.sleep(0.1)
        else:
            raise Exception('MCU never created its control socket at %s (see '
                            'build/linux-mcu/mcu-%s.log)' % (control_path, tag))

        subprocess.run(['python3', 'tools/install.py', 'install-host',
                        'build/linux-mcu/klipper'], check=True, timeout=60)
        harness.install_openams(
            os.path.abspath('build/linux-mcu/klipper/klippy/extras'))

        cfg_path = os.path.abspath('build/linux-mcu/printer-%s.cfg' % tag)
        harness.write_printer_config(cfg_path, pty_path, calibration_path)

        api_socket_dir = tempfile.mkdtemp(prefix='kmmu-control-sock-')
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
                raise Exception('klippy never created its API socket')

            try:
                klippy = connect_klippy(api_socket)
            except Exception as exc:
                raise Exception('%s (see %s)' % (exc, klippy_log))
            print('Klippy is ready.')

            try:
                status = wait_diagnostic_ready(klippy)
            except Exception as exc:
                raise Exception('%s (see %s)' % (exc, klippy_log))
            print('KLIPPER_MMU_QUERY status:', status)

            if checks_fn is None:
                checks(klippy, control_path)
            else:
                checks_fn(Run(klippy, control_path, ground_truth_path,
                              mcu_proc, klippy_proc, api_socket,
                              mcu_elf, pty_path, env))
        finally:
            klippy_proc.terminate()
            klippy_proc.wait(timeout=10)
            klippy_proc = None
            shutil.rmtree(api_socket_dir, ignore_errors=True)
    finally:
        if klippy_proc is not None:
            klippy_proc.kill()
        try:
            os.kill(mcu_proc.pid, signal.SIGUSR2)
            time.sleep(0.2)
        except Exception:
            pass
        mcu_proc.terminate()
        mcu_proc.wait(timeout=10)
        shutil.rmtree(control_dir, ignore_errors=True)
        # The full report is only worth printing for this file's own short
        # run; a caller checking long scenarios reads worst_wall_gap_us().
        if checks_fn is None and os.path.exists(gaps_path):
            print('Timer dispatch gaps (worst first):')
            print(open(gaps_path).read())
    return gaps_path


def checks(klippy, control_path):
    """The socket's contract, against a running MCU and klippy: ground truth,
    a forced switch that both the plant and the firmware must see, a jam and
    an empty spool, two kinds of bad command, and a TMC fault injection."""
    control = Control(control_path)
    try:
        lanes = truth_lanes(control)
        print('truth:', lanes)
        for lane in lanes:
            expected = dict(EMPTY_LANE, lane=lane['lane'])
            if lane != expected:
                raise Exception('truth lane %s, expected empty lane %s'
                                % (lane, expected))

        # BAY is active-low on this wiring, so level 0 is a made switch: the
        # firmware's readback (KLIPPER_MMU_PHYSICAL, the read-only capture
        # DIAGNOSE builds on) must turn it into bay0=present, while the
        # plant's ground truth (physical presence) still says the lane is
        # empty: that disagreement is what a stuck switch looks like.
        control.expect_ok('switch %s stuck 0' % LANE0_BAY_PIN)
        lanes = truth_lanes(control)
        if lanes[0]['bay'] != 0:
            raise Exception('a forced switch changed the physical truth: %s' % lanes[0])
        state, line = physical_bay0(klippy)
        print('PHYSICAL:', line)
        if state != 'present':
            raise Exception('forced BAY reads %s, expected present: %s'
                            % (state, line))

        control.expect_ok('switch %s free' % LANE0_BAY_PIN)
        lanes = truth_lanes(control)
        if lanes[0]['bay'] != 0:
            raise Exception('freeing a switch changed the physical truth: %s' % lanes[0])
        state, line = physical_bay0(klippy)
        if state != 'absent':
            raise Exception('freed BAY reads %s, expected absent: %s'
                            % (state, line))

        control.expect_ok('plant block 0 20000')
        lanes = truth_lanes(control)
        if lanes[0]['block_um'] != 20000:
            raise Exception('jam missing from truth: %s' % lanes[0])
        control.expect_ok('plant unblock 0')
        lanes = truth_lanes(control)
        if lanes[0]['block_um'] != -1:
            raise Exception('cleared jam still in truth: %s' % lanes[0])

        control.expect_ok('plant runout 0')
        lanes = truth_lanes(control)
        if lanes[0]['runout'] != 1:
            raise Exception('runout missing from truth: %s' % lanes[0])

        # An unknown verb and an unusable argument are different sentences.
        reply = control.expect_error('definitely-not-a-command')
        if 'unknown command' not in reply:
            raise Exception('unknown verb should say so: %r' % reply)
        reply = control.expect_error('plant block 0')
        if 'bad argument' not in reply:
            raise Exception('missing argument should say so: %r' % reply)

        # Last: the TMC fault injection is accepted over the socket while no
        # lane is active, where the firmware (which checks DRV_STATUS at lane
        # activation) cannot act on it. That the model really sets the bit is
        # tests/linux_mcu/tmc2209_model_test's job, not this file's.
        control.expect_ok('tmc %s fault on' % LANE0_TMC_UART_PIN)
        control.expect_ok('tmc %s fault off' % LANE0_TMC_UART_PIN)
    finally:
        control.close()


def main():
    skip_unless_available('tests/linux_mcu/control_smoke.py')
    first_failure = None
    for number in (1, 2):
        try:
            attempt(number)
            if first_failure is not None:
                print('Attempt 1 failed, the retry passed. Attempt 1: %s'
                      % first_failure)
            print('Control-socket smoke check passed.')
            return
        except Exception as exc:
            gap = worst_wall_gap_us(gaps_path_for(number))
            stall = gap is not None and gap > SWITCH_FRESHNESS_BUDGET_US
            if number == 2 or not stall:
                raise
            first_failure = exc
            print('Attempt %d failed: %s' % (number, exc))
            print('Its worst timer-dispatch wall gap was %s us, over the %d us '
                  'switch-sample freshness budget: that is a host stall, not a '
                  'firmware fault, so the whole attempt is retried once.'
                  % (gap, SWITCH_FRESHNESS_BUDGET_US))


if __name__ == '__main__':
    main()
