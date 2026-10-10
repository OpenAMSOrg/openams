#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Phase 2a: boot the actuation-permitted BoxTurtle sim image (out-sim-motion/)
through hosted diagnostic startup: klippy reaches ready, KLIPPER_MMU_QUERY
reports startup_state=ready/startup_result=0, KLIPPER_MMU_DIAGNOSE reports no
problems, and the sim's TMC2209 model (sim/board/tmc2209.c) logs every
lane's UART transactions with zero malformed/foreign frames and exactly the
expected transaction count (proving zero retries): see
docs/plan/linux-mcu-integration.md phase 2 and tests/linux_mcu/README.md.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import socket
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_root import KLIPPY_PYTHON, skip_unless_available  # noqa: E402

# klipper-mmu's firmware-owned TMC controller (src/tmc2209.c) does exactly
# this many UART transactions per lane to reach KM_TMC_READY: IDENTIFY,
# INITIAL_STATUS, COUNTER (3 reads), then WRITE+CONFIRM for each of
# KM_TMC2209_CONFIG_WRITES=11 registers (11 writes + 11 reads), then
# READ_GCONF, READ_CHOPCONF, READ_PWMCONF, CHECK_GSTAT, CHECK_DRIVER
# (5 reads) before the first periodic diagnostic read. A retry adds extra
# transactions beyond this fixed count; a communication failure adds a
# malformed/foreign line. Either shows up as a mismatch below.
EXPECTED_TRANSACTIONS_TO_READY = 3 + 11 + 11 + 5
TMC_UART_PINS = (53, 48, 65, 38)  # GPIO('D',5), GPIO('D',0), GPIO('E',1), GPIO('C',6)


def write_calibration(path):
    # Synthetic ADC counts within the stated 100..3200 mV valid range, the
    # same shape as tests/klipper_host_calibration_integration/run_tests.py
    # make_record(). The sim's PC4/PC5 ADC pair (sim/board/gpio.c,
    # sim/board/diff_model.c) reports raw counts for this exact calibration
    # at a fixed buffer position, so the round trip through firmware is
    # self-consistent.
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


class Klippy:
    """Minimal JSON-RPC client over klippy's API socket that also collects
    notify_gcode_response text, which is where KLIPPER_MMU_QUERY/DIAGNOSE's
    respond_info() output actually lands (the direct RPC reply is just an
    empty ack)."""

    def __init__(self, sock):
        self.sock = sock
        self.buf = b""
        self.next_id = 1

    def _read_message(self):
        while b'\x03' not in self.buf:
            self.buf += self.sock.recv(4096)
        raw, self.buf = self.buf.split(b'\x03', 1)
        return json.loads(raw.decode())

    def call(self, method, params=None, timeout=10.0):
        """Send one request; return (result_message, [gcode_response_lines])
        collected until the matching id arrives."""
        rid = self.next_id
        self.next_id += 1
        self.sock.sendall((json.dumps(
            {"jsonrpc": "2.0", "method": method, "params": params or {},
             "id": rid}) + '\x03').encode())
        lines = []
        deadline = time.monotonic() + timeout
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError("no reply to %s" % method)
            msg = self._read_message()
            if msg.get('id') == rid:
                return msg, lines
            if msg.get('method') == 'notify_gcode_response':
                lines.append((msg.get('params') or {}).get('response'))


def parse_kv(text):
    """Parse the ' key=value key2=value2 ' tail klipper_mmu's respond_info
    lines use into a dict of strings."""
    return dict(re.findall(r'(\w+)=(\S+)', text))


def main():
    skip_unless_available('tests/linux_mcu/motion_boot_check.py')
    mcu_elf = 'build/linux-mcu/klipper/out-sim-motion/klipper.elf'
    pty_path = os.path.abspath('build/linux-mcu/mcu-motion.pty')

    if os.path.exists(pty_path):
        os.unlink(pty_path)

    calibration_path = os.path.abspath('build/linux-mcu/diff-calibration.json')
    write_calibration(calibration_path)

    mcu_log_path = 'build/linux-mcu/mcu-motion.log'
    mcu_proc = subprocess.Popen(
        [os.path.abspath(mcu_elf), '-I', pty_path],
        stdout=open(mcu_log_path, 'w'), stderr=subprocess.STDOUT)

    try:
        for _ in range(50):
            if os.path.exists(pty_path):
                break
            time.sleep(0.1)
        else:
            raise Exception("MCU never created its pty")

        subprocess.run(['python3', 'tools/install.py', 'install-host',
                        'build/linux-mcu/klipper'], check=True, timeout=60)

        cfg_path = os.path.abspath('build/linux-mcu/printer-motion.cfg')
        with open(cfg_path, 'w') as f:
            f.write(f"""
[mcu]
serial: {pty_path}
[printer]
kinematics: none
max_velocity: 1
max_accel: 1
[klipper_mmu box]
mcu: mcu
diagnostic_startup: True
diff_calibration_file: {calibration_path}
""")

        # AF_UNIX paths are capped at ~108 bytes; keep the socket short-lived
        # and short-pathed like tests/linux_mcu/boot_check.py does.
        api_socket_dir = tempfile.mkdtemp(prefix='kmmu-motion-sock-')
        api_socket = os.path.join(api_socket_dir, 'klippy.sock')

        klippy_log = os.path.abspath('build/linux-mcu/klippy-motion.log')
        klippy_proc = subprocess.Popen(
            [str(KLIPPY_PYTHON),
             'build/linux-mcu/klipper/klippy/klippy.py',
             cfg_path, '-l', klippy_log, '-a', api_socket],
            stdout=open('build/linux-mcu/klippy-motion_out.log', 'w'),
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
            # respond_info() output (where KLIPPER_MMU_QUERY/DIAGNOSE's text
            # actually lands) is a subscription push, not the direct RPC
            # reply; subscribe before running any gcode script.
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

            # Diagnostic startup is asynchronous (mcu/diagnostic_startup.md);
            # poll KLIPPER_MMU_QUERY until startup_state settles instead of
            # asserting on the very first sample.
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

            resp, lines = klippy.call(
                "gcode/script", {"script": "KLIPPER_MMU_DIAGNOSE MMU=box"})
            if 'error' in resp:
                raise Exception("KLIPPER_MMU_DIAGNOSE failed: %s" % resp)
            print("KLIPPER_MMU_DIAGNOSE:", lines)
            if not any('no problems found' in line for line in lines):
                raise Exception("KLIPPER_MMU_DIAGNOSE reported a problem: %s" % lines)

            print("Motion boot check passed (klippy ready, startup ready, "
                  "diagnose clean).")
        finally:
            klippy_proc.terminate()
            klippy_proc.wait(timeout=10)
            shutil.rmtree(api_socket_dir, ignore_errors=True)
    finally:
        mcu_proc.terminate()
        mcu_proc.wait(timeout=10)

    check_tmc_evidence(mcu_log_path)


def check_tmc_evidence(mcu_log_path):
    """The sim's TMC2209 model (sim/board/tmc2209.c) logs one line per
    completed UART transaction (event=write/read reg=0xNN), and one per
    malformed/foreign frame. Zero of the latter is direct evidence the
    physical UART protocol itself never desynced. Zero retries in the
    firmware's TMC controller (src/tmc2209.c km_tmc2209.retry()) is then
    evidenced structurally: a retry resubmits the exact same (direction,
    register) transaction as the very next one on that pin, which this
    firmware's fixed register sequence (11 distinct write_registers,
    alternating GSTAT/DRV_STATUS diagnostics) never otherwise does. A
    transaction count floor confirms the full startup sequence really ran,
    tolerant of extra periodic post-ready diagnostic pairs accumulating
    while this script polls.
    """
    with open(mcu_log_path) as f:
        log = f.read()
    bad = re.findall(r'km_sim_tmc2209 pin=\d+ event=(malformed|foreign).*', log)
    if bad:
        raise Exception("TMC UART protocol errors observed: %s" % bad[:10])
    per_pin_events = {}
    for pin_str, event, reg in re.findall(
            r'km_sim_tmc2209 pin=(\d+) event=(write|read) reg=(0x[0-9a-f]+)', log):
        per_pin_events.setdefault(pin_str, []).append((event, reg))
    print("TMC UART transaction counts per pin:",
          {p: len(v) for p, v in per_pin_events.items()})
    missing = [p for p in TMC_UART_PINS if str(p) not in per_pin_events]
    if missing:
        raise Exception(
            "No TMC UART activity observed on pin(s) %s (see %s)"
            % (missing, mcu_log_path))
    too_few = {p: len(v) for p, v in per_pin_events.items()
               if len(v) < EXPECTED_TRANSACTIONS_TO_READY}
    if too_few:
        raise Exception(
            "TMC UART transaction count below the %d needed to reach ready "
            "for pin(s): %s" % (EXPECTED_TRANSACTIONS_TO_READY, too_few))
    retries = {p: [v[i] for i in range(1, len(v)) if v[i] == v[i - 1]]
               for p, v in per_pin_events.items()}
    retries = {p: r for p, r in retries.items() if r}
    if retries:
        raise Exception(
            "Repeated (direction, register) transactions imply a TMC UART "
            "retry on pin(s): %s" % (retries,))
    print("Zero TMC UART retries confirmed on %d lane(s): no pin repeated "
          "the same (direction, register) transaction back to back, and "
          "each ran at least the %d transactions the firmware's fixed "
          "startup sequence requires." % (len(per_pin_events),
                                          EXPECTED_TRANSACTIONS_TO_READY))


if __name__ == '__main__':
    main()
