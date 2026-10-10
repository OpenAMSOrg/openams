#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""A soak harness for the whole-stack sim (klipper-mmu).

It boots the firmware on the sim board once, then runs N cycles to detect leaks,
host stalls, timing regressions, or ground-truth disagreement over time.
One cycle consists of:
  - For each lane with a spool (reported by KLIPPER_MMU_PHYSICAL):
      - PRELOAD, wait for terminal
      - UNPRELOAD, wait for terminal; in the final cycle also EJECT (EJECT needs a clear path, and the plant cannot re-insert filament)
      - Check ground truth against expected physical positions
  - Record duration, VmRSS, diagnostic state, and terminal events to a CSV.

A host stall might result in a sensor failure fault; this is recorded but not hidden.
Failures include unexpected faults, poll timeouts, ground truth disagreement, RSS
growth over 10% after cycle 3, or a single cycle taking more than 3x the median time.
"""
import argparse
import csv
import os
import signal
import subprocess
import time
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import preload_check as harness
import control_smoke as smoke

def get_vm_rss(pid):
    if not pid: return 0
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except Exception:
        pass
    return 0

def get_klippy_pid(tag):
    try:
        out = subprocess.check_output(['pgrep', '-f', f'klippy.py.*printer-{tag}.cfg'])
        pids = out.decode().strip().split()
        return int(pids[0]) if pids else None
    except Exception:
        return None

def write_plant_all(path):
    with open(path, 'w') as f:
        f.write("lane=0 state=bay\nlane=1 state=bay\nlane=2 state=bay\nlane=3 state=bay\n")

def get_diagnose(klippy):
    resp, lines = klippy.call("gcode/script", {"script": "KLIPPER_MMU_DIAGNOSE MMU=box"})
    return " ".join(lines) if 'error' not in resp else str(resp)

def get_spools(klippy):
    resp, lines = klippy.call("gcode/script", {"script": "KLIPPER_MMU_PHYSICAL MMU=box"})
    if 'error' in resp:
        return []
    spools = []
    for line in lines:
        for m in re.finditer(r'bay(\d+)=present', line):
            spools.append(int(m.group(1)))
    return spools

def do_operation(klippy, name, bay, terminal_prefix, start_timeout=60.0, terminal_timeout=60.0):
    script = f"KLIPPER_MMU_{name} MMU=box"
    if bay is not None:
        script += f" BAY={bay}"
    
    deadline = time.monotonic() + start_timeout
    started = False
    while time.monotonic() < deadline:
        resp, lines = klippy.call("gcode/script", {"script": script})
        if 'error' not in resp:
            for line in lines:
                m = re.search(r'admission=(\d+)', line)
                if m and m.group(1) == '0':
                    started = True
            if started:
                break
        time.sleep(0.5)
    
    if not started:
        raise Exception(f"{name} never started cleanly")
    
    deadline = time.monotonic() + terminal_timeout
    while time.monotonic() < deadline:
        resp, lines = klippy.call("gcode/script", {"script": f"KLIPPER_MMU_{name} MMU=box"})
        if 'error' in resp:
            raise Exception(f"{name} poll failed: {resp}")
        for line in lines:
            m = re.search(terminal_prefix + r' terminal request=(\d+) bay=(\d+) event=(\d+) code=(\d+)', line)
            if m:
                return int(m.group(3)), int(m.group(4))
            if 'rejected' in line or 'administrative' in line:
                raise Exception(f"{name} failed: {line}")
        time.sleep(0.5)
    raise Exception(f"{name} BAY={bay} never reached a terminal result (timeout)")

def run_openams_macro(klippy, script, start_timeout=60.0):
    deadline = time.monotonic() + start_timeout
    while True:
        resp, lines = klippy.call("gcode/script", {"script": script}, timeout=180.0)
        if 'error' not in resp:
            break
        if 'OpenAMS motion unavailable' in str(resp['error']) and time.monotonic() < deadline:
            time.sleep(0.5)
            continue
        raise Exception(f"{script} failed: {resp['error']}")

def clear_fault(klippy):
    klippy.call("gcode/script", {"script": "KLIPPER_MMU_CLEAR_FAULT MMU=box"})

def get_gap_and_stall():
    gap = smoke.worst_wall_gap_us(smoke.gaps_path_for(1, "soak"))
    stall = 1 if gap is not None and gap > smoke.SWITCH_FRESHNESS_BUDGET_US else 0
    return gap, stall

def run_cycle(run, klippy_pid, cycle, args, csv_writer, final=False):
    start_time = time.monotonic()
    klippy = run.klippy
    terminal_events = []
    failures = []
    spools = get_spools(klippy)

    def step(cmd, lane, expect_ev):
        # A fault is always a failure; one that coincides with a reported host
        # stall is labelled as such rather than hidden.
        try:
            ev, code = do_operation(klippy, cmd, lane, cmd)
            terminal_events.append(f"{cmd}:{ev},{code}")
            if ev != expect_ev or code != 0:
                gap, stall = get_gap_and_stall()
                failures.append(f"{cmd}_FAULT_LANE{lane}_{ev}_{code}"
                                + (f"_DURING_HOST_STALL_{gap}us" if stall else ""))
                if ev == 15: clear_fault(klippy)
        except Exception as e:
            failures.append(f"{cmd}_LANE{lane}_ERR_{e}")
            clear_fault(klippy)

    def truth(lane):
        os.kill(run.mcu.pid, signal.SIGUSR1)
        time.sleep(0.1)
        return harness.read_ground_truth(run.truth).get(lane, {})

    for lane in spools:
        step('PRELOAD', lane, harness.EVENT_PRELOAD_COMPLETE)
        # EJECT needs a clear shared path, so it only ever follows UNPRELOAD.
        step('UNPRELOAD', lane, harness.EVENT_UNLOAD_COMPLETE)
        lane_gt = truth(lane)
        if not (lane_gt.get('bay') == 1 and lane_gt.get('load') == 0 and lane_gt.get('hub') == 0):
            failures.append(f"GT_UNPRELOAD_LANE{lane}")
        # The plant cannot re-insert filament, so each lane is ejected once, in
        # the final cycle.
        if final:
            step('EJECT', lane, harness.EVENT_EJECT_COMPLETE)
            lane_gt = truth(lane)
            if not (lane_gt.get('bay') == 0 and lane_gt.get('load') == 0 and lane_gt.get('hub') == 0):
                failures.append(f"GT_EJECT_LANE{lane}")

    if args.load and spools:
        lane = spools[cycle % len(spools)]
        try:
            run_openams_macro(klippy, f"OAMSM_LOAD_FILAMENT GROUP=T{lane}")
            run_openams_macro(klippy, "OAMSM_UNLOAD_FILAMENT")
            terminal_events.append(f"LOAD_UNLOAD_LANE{lane}:OK")
        except Exception as e:
            # load path is not a gate
            terminal_events.append(f"LOAD_UNLOAD_LANE{lane}:ERR_{e}")

    diag = get_diagnose(klippy)
    rss_klippy = get_vm_rss(klippy_pid)
    rss_mcu = get_vm_rss(run.mcu.pid)
    
    os.kill(run.mcu.pid, signal.SIGUSR2)
    time.sleep(0.1)
    gap, stall = get_gap_and_stall()
    
    duration = time.monotonic() - start_time
    csv_writer.writerow([cycle, duration, " ".join(terminal_events), diag, rss_klippy, rss_mcu, stall])
    return duration, rss_klippy, rss_mcu, stall, failures

def checks_fn(run, args):
    klippy_pid = get_klippy_pid("soak")
    
    if args.load:
        run.klippy.call("gcode/script", {"script": "KLIPPER_MMU_CALIBRATE BAY=0"})
        time.sleep(1)

    cycles_durations = []
    start_rss_k = None
    start_rss_m = None
    all_failures = []
    
    with open("build/linux-mcu/soak.csv", "w", newline='') as f:
        writer = csv.writer(f)
        writer.writerow(["cycle", "duration", "terminals", "diagnose", "rss_klippy", "rss_mcu", "stall"])
        
        for c in range(args.cycles):
            dur, rk, rm, stall, fails = run_cycle(run, klippy_pid, c, args, writer,
                                                  final=(c == args.cycles - 1))
            cycles_durations.append(dur)
            all_failures.extend(fails)
            
            if c == 3:
                start_rss_k = rk
                start_rss_m = rm
            if c >= 3:
                if start_rss_k and rk > start_rss_k * 1.10:
                    all_failures.append(f"RSS_KLIPPY_GROWTH_{c}")
                if start_rss_m and rm > start_rss_m * 1.10:
                    all_failures.append(f"RSS_MCU_GROWTH_{c}")
                    
            if len(cycles_durations) > 1:
                sorted_durs = sorted(cycles_durations[:-1])
                median_dur = sorted_durs[len(sorted_durs)//2]
                if dur > 3 * median_dur:
                    all_failures.append(f"SLOW_CYCLE_{c}")
                    
            f.flush()
    
    sorted_durs = sorted(cycles_durations)
    median_dur = sorted_durs[len(sorted_durs)//2] if sorted_durs else 0
    worst_dur = max(cycles_durations) if cycles_durations else 0
    end_rss_k = get_vm_rss(klippy_pid)
    end_rss_m = get_vm_rss(run.mcu.pid)
    
    print("\n--- Soak Summary ---")
    print(f"Cycles: {args.cycles}")
    print(f"Failures: {len(all_failures)}")
    if all_failures:
        print(f"Failure list: {', '.join(all_failures)}")
    print(f"Median cycle time: {median_dur:.2f}s")
    print(f"Worst cycle time: {worst_dur:.2f}s")
    print(f"RSS klippy (start->end): {start_rss_k or 0} -> {end_rss_k} kB")
    print(f"RSS mcu (start->end): {start_rss_m or 0} -> {end_rss_m} kB")
    
    if all_failures:
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description="""Whole-stack sim soak harness for klipper-mmu.
It runs N cycles of PRELOAD and UNPRELOAD/EJECT on the sim board to detect leaks, 
stalls, timing regressions, or ground truth disagreement over time.
""")
    parser.add_argument("--cycles", type=int, default=25, help="Number of cycles to run")
    parser.add_argument("--load", action="store_true", help="Also run LOAD/UNLOAD through OpenAMS")
    args = parser.parse_args()
    smoke.attempt(1, tag="soak", plant=write_plant_all, checks_fn=lambda run: checks_fn(run, args))

if __name__ == '__main__':
    main()
