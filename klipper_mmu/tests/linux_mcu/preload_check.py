#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Phase 2b-1: put a filament plant (sim/board/plant.c) in the loop of the
actuation-permitted sim image and run PRELOAD, UNPRELOAD and EJECT on lane 0
through the real firmware (src/operation_core.c) and the real host extra
(host/__init__.py) plus OpenAMS (host/openams_managed_unit.py), exactly as a
frontend would issue them: see docs/plan/linux-mcu-integration.md, "The
plant in the loop", and tests/linux_mcu/README.md.

Unlike motion_boot_check.py (diagnostic startup only, no motion), this test
starts lane 0 already inserted up to its BAY switch (sim/board/plant.c's
KM_SIM_PLANT file), then drives real feeder motion and asserts both the
firmware's own reported terminal event/code (host/wire.py EventType/OpCode)
and the plant's independent ground truth (position, switch levels) agree on
where the tip ended up. This build has LOAD_SENSORS and POWERED_REWIND on
and no TOOLHEAD switch (see sim/board/plant.c's header comment), so:
  - PRELOAD feeds lane 0 forward until its LOAD switch trips, then settles
    with EventType.PRELOAD_COMPLETE: bay=1 load=1 hub=0.
  - UNPRELOAD (a lane-local UNLOAD, host/openams_managed_unit.py's
    _LOCAL_OPERATIONS) retracts until LOAD clears, settling with
    EventType.UNLOAD_COMPLETE: bay=1 load=0 hub=0.
  - EJECT retracts further until BAY clears, settling with
    EventType.EJECT_COMPLETE: bay=0 load=0 hub=0.
"""
import glob
import json
import os
import re
import resource
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_root import KLIPPY_PYTHON, skip_unless_available  # noqa: E402

# The openams checkout this runs against: the repository this file lives in,
# unless OPENAMS_SOURCE names another one.
OPENAMS_REPO = os.environ.get(
    'OPENAMS_SOURCE',
    os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..')))
OPENAMS_COMMIT = 'c560655'

# host/wire.py: EventType and OpCode, the wire enums klippy's respond_info
# text below reports by number.
EVENT_PRELOAD_COMPLETE = 13
EVENT_UNLOAD_COMPLETE = 4
EVENT_EJECT_COMPLETE = 16
OPCODE_SUCCESS = 0
ADMISSION_ACCEPTED = 0


def write_calibration(path):
    # Same shape as motion_boot_check.py's write_calibration(): synthetic
    # ADC counts within the stated 100..3200 mV valid range. This test never
    # reaches FPS arrival (PRELOAD/UNPRELOAD/EJECT stop at LOAD/BAY, well
    # before HUB), so only diagnostic startup's own calibration validation
    # depends on this file.
    record = {
        'channel_map': 'a_low_b_high', 'vref_mv': 3300,
        'min_valid_mv': 100, 'max_valid_mv': 3200,
        'path_tolerance_counts': 20,
        'points': [{'a_raw': 250, 'b_raw': 2200, 'position_permille': 0},
                   {'a_raw': 500, 'b_raw': 2600, 'position_permille': 500},
                   {'a_raw': 1900, 'b_raw': 3900, 'position_permille': 1000}],
    }
    with open(path, 'w') as f:
        json.dump(record, f)


def write_plant_state(path):
    # sim/board/plant.c's KM_SIM_PLANT format: "lane=<n> state=<empty|bay>".
    # Lane 0 starts inserted up to its BAY switch; the rest stay empty.
    with open(path, 'w') as f:
        f.write("lane=0 state=bay\n"
                "lane=1 state=empty\n"
                "lane=2 state=empty\n"
                "lane=3 state=empty\n")


def install_openams(klipper_extras_dir):
    """Extract a clean copy of the ams-family OpenAMS fork at the pinned
    commit (never modifying that repository) and symlink its src/*.py into
    klippy/extras, the same links install-openams.sh makes."""
    extract_dir = os.path.abspath('build/linux-mcu/openams-src')
    if os.path.exists(extract_dir):
        shutil.rmtree(extract_dir)
    os.makedirs(extract_dir)
    archive = subprocess.run(
        ['git', '-C', OPENAMS_REPO, 'archive', OPENAMS_COMMIT],
        check=True, timeout=30, stdout=subprocess.PIPE).stdout
    subprocess.run(['tar', '-x', '-C', extract_dir], input=archive,
                    check=True, timeout=30)
    src_dir = os.path.join(extract_dir, 'src')
    for py in glob.glob(os.path.join(src_dir, '*.py')):
        dest = os.path.join(klipper_extras_dir, os.path.basename(py))
        if os.path.islink(dest) or os.path.exists(dest):
            os.remove(dest)
        os.symlink(os.path.abspath(py), dest)


def read_ground_truth(path):
    """Parse sim/board/plant.c's SIGUSR1/exit dump: one "lane=N
    position_um=.. steps=.. dir_changes=.. bay=.. load=.. hub=.." line per
    lane. Returns {lane_int: {field: int}}."""
    out = {}
    with open(path) as f:
        for line in f:
            fields = dict(re.findall(r'(\w+)=(-?\d+)', line))
            if 'lane' not in fields:
                continue
            out[int(fields['lane'])] = {k: int(v) for k, v in fields.items()}
    return out


class Klippy:
    """Minimal JSON-RPC client over klippy's API socket; see
    motion_boot_check.py for the same pattern and why respond_info text
    (where KLIPPER_MMU_*'s output actually lands) needs a subscription.

    A reader thread drains the socket all the time: klippy broadcasts gcode
    output to every subscribed client, and closes one that stops reading
    ("Closing unresponsive client"), so a client that only read inside
    call() was dropped while another connection's commands produced output
    during a long wait."""

    def __init__(self, sock):
        self.sock = sock
        self.buf = b""
        self.next_id = 1
        self._cond = threading.Condition()
        self._replies = {}
        self._lines = []
        self._closed = None
        sock.settimeout(None)
        threading.Thread(target=self._reader, daemon=True).start()

    def _read_message(self):
        while b'\x03' not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError('klippy API socket closed awaiting a reply')
            self.buf += chunk
        raw, self.buf = self.buf.split(b'\x03', 1)
        return json.loads(raw.decode())

    def _reader(self):
        try:
            while True:
                msg = self._read_message()
                with self._cond:
                    if 'id' in msg:
                        self._replies[msg['id']] = msg
                    elif msg.get('method') == 'notify_gcode_response':
                        self._lines.append((msg.get('params') or {}).get('response'))
                    self._cond.notify_all()
        except Exception as exc:
            with self._cond:
                self._closed = exc
                self._cond.notify_all()

    def call(self, method, params=None, timeout=10.0):
        with self._cond:
            rid = self.next_id
            self.next_id += 1
            first = len(self._lines)
        self.sock.sendall((json.dumps(
            {"jsonrpc": "2.0", "method": method, "params": params or {},
             "id": rid}) + '\x03').encode())
        start = time.monotonic()
        deadline = start + timeout
        with self._cond:
            while rid not in self._replies:
                if self._closed is not None:
                    raise ConnectionError('klippy API socket closed: %r' % (self._closed,))
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError("no reply to %s %s after %.1fs"
                                       % (method, params, time.monotonic() - start))
                self._cond.wait(left)
            return self._replies.pop(rid), self._lines[first:]


def parse_kv(text):
    return dict(re.findall(r'(\w+)=(\S+)', text))


def run_operation(klippy, name, bay, terminal_prefix, expect_event,
                   start_timeout=20.0, terminal_timeout=30.0):
    """Start a KLIPPER_MMU_<name> BAY=<bay> request (retrying the start
    itself for up to start_timeout: OpenAMS's own state cache
    (host/openams_managed_unit.py start_unpreload/start_eject) needs a
    lane's reservation to have caught up with the firmware's last terminal
    event, which lags slightly behind the KLIPPER_MMU_<name> poll this
    script itself just used to observe that event), then polls
    KLIPPER_MMU_<name> MMU=box (no BAY) until a terminal line appears.
    Returns (event_type, result_code)."""
    script = "KLIPPER_MMU_%s MMU=box BAY=%d" % (name, bay)
    deadline = time.monotonic() + start_timeout
    while True:
        resp, lines = klippy.call("gcode/script", {"script": script})
        if 'error' not in resp:
            break
        if time.monotonic() > deadline:
            raise Exception("%s never started: %s" % (name, resp['error']))
        time.sleep(0.25)
    started = " ".join(lines)
    print("%s start: %s" % (name, started))
    def diagnose():
        try:
            _, diag_lines = klippy.call(
                "gcode/script", {"script": "KLIPPER_MMU_DIAGNOSE MMU=box"})
            return diag_lines
        except Exception as exc:
            return ["DIAGNOSE unavailable: %r" % exc]

    m = re.search(r'admission=(\d+)', started)
    if m and int(m.group(1)) != ADMISSION_ACCEPTED:
        raise Exception("%s BAY=%d was not accepted: %s DIAGNOSE: %s"
                        % (name, bay, started, diagnose()))

    deadline = time.monotonic() + terminal_timeout
    while time.monotonic() < deadline:
        resp, lines = klippy.call(
            "gcode/script", {"script": "KLIPPER_MMU_%s MMU=box" % name})
        if 'error' in resp:
            raise Exception("%s poll failed: %s DIAGNOSE: %s"
                            % (name, resp, diagnose()))
        for line in lines:
            if 'retained fault' in line:
                print("%s fault line: %s" % (name, line))
            m = re.search(terminal_prefix + r' terminal request=(\d+) bay=(\d+) '
                          r'event=(\d+) code=(\d+)', line)
            if m:
                event, code = int(m.group(3)), int(m.group(4))
                print("%s terminal: %s" % (name, line))
                if event != expect_event or code != OPCODE_SUCCESS:
                    raise Exception(
                        "%s did not complete cleanly: event=%d code=%d "
                        "(wanted event=%d code=%d): %s; DIAGNOSE: %s"
                        % (name, event, code, expect_event, OPCODE_SUCCESS, line,
                           diagnose()))
                return event, code
            if 'rejected' in line or 'administrative' in line:
                raise Exception("%s failed: %s DIAGNOSE: %s"
                                % (name, line, diagnose()))
        time.sleep(0.1)
    raise Exception("%s BAY=%d never reached a terminal result; DIAGNOSE: %s"
                    % (name, bay, diagnose()))


# Extra [klipper_mmu box] lines for one scenario (load_scenarios.py lane-gear).
EXTRA_MMU_OPTIONS = ''


def write_printer_config(cfg_path, pty_path, calibration_path):
    """The sim printer config: the MCU on its pty, a cold extruder and the
    klipper-mmu box with OpenAMS. Shared with the other linux_mcu checks.
    min_extrude_temp is 0 so load_check.py can count extruder STEPs (a
    forward G1 E) without heating anything: these checks care about where
    the filament went, not how hot it is. bowden_estimate_file is one sibling
    path per cfg_path (tag-specific, like the other per-run artifacts), so
    KLIPPER_MMU_CALIBRATE has somewhere to store the calibrated distance;
    stale state from a previous run at the same path is cleared first."""
    bowden_path = cfg_path + '.bowden.json'
    if os.path.exists(bowden_path):
        os.unlink(bowden_path)
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
diff_calibration_file: {calibration_path}
bowden_estimate_file: {bowden_path}
openams_motion: True
openams_idx: 1
openams_fps: fps0
openams_family: boxturtle
openams_bays: 4
openams_path_length_mm: 842.5
openams_host_watchdog_ms: 2000
openams_lease_max_age_ms: 1000
path_reconciliation: True
driver_restart: True
{EXTRA_MMU_OPTIONS}
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
""")


def grant_realtime_priority(mcu_elf):
    """Klipper's own linux/main.c already calls sched_setscheduler(SCHED_FIFO)
    at startup (and reports the errno if it fails) -- it just needs
    CAP_SYS_NICE to succeed. Grant that directly on the disposable sim
    binary with `setcap` (no sudo-wrapper process, so PID/signal handling
    for KM_SIM_PLANT_OUT/KM_SIM_TIMER_GAPS_OUT stays exactly as it is
    without it) instead of wrapping under `chrt`, so a 2.5ms firmware
    freshness budget (mcu/switch_gpio.h KM_SWITCH_MAX_SAMPLE_GAP_US) isn't
    at the mercy of ordinary Linux CFS scheduling on a shared, loaded box --
    see sim/board/timer.c's dispatch-gap diagnostic (KM_SIM_TIMER_GAPS_OUT)
    for the numbers that motivated this. Real hardware has no such
    contention; this only makes the sim match that assumption instead of
    being lucky about it. Best-effort and idempotent: prints a reason and
    leaves the binary alone when `sudo -n setcap` isn't usable, so this test
    still runs (at normal priority) somewhere without passwordless sudo."""
    if os.environ.get('KM_SIM_NO_REALTIME'):
        return False
    result = subprocess.run(
        ['sudo', '-n', 'setcap', 'cap_sys_nice,cap_ipc_lock+ep', mcu_elf],
        capture_output=True, timeout=10)
    if result.returncode != 0:
        # In a sandbox (no-new-privs) Klipper's own sched_setscheduler still
        # succeeds when RLIMIT_RTPRIO covers its priority 49.
        if resource.getrlimit(resource.RLIMIT_RTPRIO)[0] >= 49:
            return True
        print("Real-time priority unavailable (sudo -n setcap failed: %s); "
              "running the sim MCU at normal priority."
              % result.stderr.decode().strip())
        return False
    return True


def main():
    skip_unless_available('tests/linux_mcu/preload_check.py')
    mcu_elf = os.path.abspath('build/linux-mcu/klipper/out-sim-motion/klipper.elf')
    pty_path = os.path.abspath('build/linux-mcu/mcu-preload.pty')
    if os.path.exists(pty_path):
        os.unlink(pty_path)

    calibration_path = os.path.abspath('build/linux-mcu/diff-calibration.json')
    write_calibration(calibration_path)
    plant_path = os.path.abspath('build/linux-mcu/plant-initial.txt')
    write_plant_state(plant_path)
    ground_truth_path = os.path.abspath('build/linux-mcu/plant-ground-truth.txt')
    if os.path.exists(ground_truth_path):
        os.unlink(ground_truth_path)

    gaps_path = os.path.abspath('build/linux-mcu/timer-gaps.txt')
    if os.path.exists(gaps_path):
        os.unlink(gaps_path)

    env = dict(os.environ)
    env['KM_SIM_PLANT'] = plant_path
    env['KM_SIM_PLANT_OUT'] = ground_truth_path
    env['KM_SIM_TIMER_GAPS_OUT'] = gaps_path

    realtime = grant_realtime_priority(mcu_elf)
    print("Sim MCU real-time priority:", "granted" if realtime else "unavailable")

    mcu_log_path = 'build/linux-mcu/mcu-preload.log'
    mcu_argv = [mcu_elf] + (['-r'] if realtime else []) + ['-I', pty_path]
    mcu_proc = subprocess.Popen(
        mcu_argv,
        stdout=open(mcu_log_path, 'w'), stderr=subprocess.STDOUT, env=env)

    try:
        for _ in range(50):
            if os.path.exists(pty_path):
                break
            time.sleep(0.1)
        else:
            raise Exception("MCU never created its pty")

        subprocess.run(['python3', 'tools/install.py', 'install-host',
                        'build/linux-mcu/klipper'], check=True, timeout=60)
        install_openams(
            os.path.abspath('build/linux-mcu/klipper/klippy/extras'))

        cfg_path = os.path.abspath('build/linux-mcu/printer-preload.cfg')
        write_printer_config(cfg_path, pty_path, calibration_path)

        api_socket_dir = tempfile.mkdtemp(prefix='kmmu-preload-sock-')
        api_socket = os.path.join(api_socket_dir, 'klippy.sock')

        klippy_log = os.path.abspath('build/linux-mcu/klippy-preload.log')
        klippy_proc = subprocess.Popen(
            [str(KLIPPY_PYTHON),
             'build/linux-mcu/klipper/klippy/klippy.py',
             cfg_path, '-l', klippy_log, '-a', api_socket],
            stdout=open('build/linux-mcu/klippy-preload_out.log', 'w'),
            stderr=subprocess.STDOUT)

        try:
            for _ in range(150):
                if os.path.exists(api_socket):
                    break
                time.sleep(0.1)
            else:
                raise Exception("klippy never created its API socket")

            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(api_socket)
            s.settimeout(10.0)
            klippy = Klippy(s)
            klippy.call("gcode/subscribe_output",
                       {"response_template": {"method": "notify_gcode_response"}})

            for _ in range(200):
                info, _ = klippy.call("info")
                if info.get('result', {}).get('state') == 'ready':
                    print("Klippy is ready.")
                    break
                time.sleep(0.5)
            else:
                raise Exception("Klippy never became ready (see %s)" % klippy_log)

            status = {}
            for _ in range(200):
                resp, lines = klippy.call(
                    "gcode/script", {"script": "KLIPPER_MMU_QUERY MMU=box"})
                if 'error' in resp:
                    raise Exception("KLIPPER_MMU_QUERY failed: %s" % resp)
                for line in lines:
                    if 'startup_state=' in line:
                        status = parse_kv(line)
                if status.get('startup_state') in ('ready', 'faulted'):
                    break
                time.sleep(0.25)
            print("KLIPPER_MMU_QUERY status:", status)
            if status.get('startup_state') != 'ready' or status.get('startup_result') != '0':
                raise Exception(
                    "diagnostic startup did not reach ready: %s (see %s)"
                    % (status, klippy_log))

            # openams_motion's HOST session bootstraps asynchronously
            # (host/openams_managed_unit.py's OpenAMSManagedUnit._activate(),
            # driven by oams_manager's 1s monitor tick). KLIPPER_MMU_QUERY's
            # own 'motion_available' field is not a useful gate for this --
            # host/__init__.py's get_status() hardcodes 'motion_available':
            # False unconditionally (true since the add-on's very first
            # commit, fff91f13; the real value only ever lives on the
            # OpenAMSManagedUnit object oams_manager.py reads directly), so
            # it never reports True even once motion genuinely is available.
            # run_operation's own start-call retry loop absorbs this
            # bootstrap window instead of polling a field that cannot answer.
            def run_checked(*args, **kwargs):
                # On failure, add the plant's view of the lanes to the error.
                try:
                    run_operation(klippy, *args, **kwargs)
                except Exception as e:
                    os.kill(mcu_proc.pid, signal.SIGUSR1)
                    time.sleep(0.2)
                    raise Exception("%s; plant ground truth: %s"
                                    % (e, read_ground_truth(ground_truth_path)))

            run_checked('PRELOAD', 0, 'PRELOAD', EVENT_PRELOAD_COMPLETE,
                        start_timeout=30.0)
            os.kill(mcu_proc.pid, signal.SIGUSR1)
            time.sleep(0.2)
            gt = read_ground_truth(ground_truth_path)
            print("Ground truth after PRELOAD:", gt[0])
            if not (gt[0]['bay'] == 1 and gt[0]['load'] == 1 and gt[0]['hub'] == 0):
                raise Exception("Plant ground truth disagrees with PRELOAD "
                                "success: %s" % gt[0])

            run_checked('UNPRELOAD', 0, 'UNPRELOAD', EVENT_UNLOAD_COMPLETE)
            os.kill(mcu_proc.pid, signal.SIGUSR1)
            time.sleep(0.2)
            gt = read_ground_truth(ground_truth_path)
            print("Ground truth after UNPRELOAD:", gt[0])
            if not (gt[0]['bay'] == 1 and gt[0]['load'] == 0 and gt[0]['hub'] == 0):
                raise Exception("Plant ground truth disagrees with UNPRELOAD "
                                "success: %s" % gt[0])

            run_checked('EJECT', 0, 'EJECT', EVENT_EJECT_COMPLETE)
            os.kill(mcu_proc.pid, signal.SIGUSR1)
            time.sleep(0.2)
            gt = read_ground_truth(ground_truth_path)
            print("Ground truth after EJECT:", gt[0])
            if not (gt[0]['bay'] == 0 and gt[0]['load'] == 0 and gt[0]['hub'] == 0):
                raise Exception("Plant ground truth disagrees with EJECT "
                                "success: %s" % gt[0])

            print("PRELOAD/UNPRELOAD/EJECT plant-in-the-loop check passed.")
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
            print("Timer dispatch gaps (worst first):")
            print(open(gaps_path).read())


if __name__ == '__main__':
    main()
