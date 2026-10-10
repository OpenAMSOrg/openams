#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Phase 3a-2/3b: fault scenarios on the sim MCU's control socket.

Nine injections, each with its own MCU and klippy boot so one failure cannot
cascade into the next. The first four just prove a fault is reported
correctly: an injected TMC driver fault and a jam during PRELOAD, a stuck HUB
switch before it, and a bouncing BAY switch during it -- asserting the
firmware's terminal result (host/wire.py EventType/OpCode), the
KLIPPER_MMU_DIAGNOSE text it prints, and the plant's independent ground truth
(sim/board/plant.c) that the lane really stopped or completed where the
firmware says it did. The next five (phase 3b, "whole-stack" scenarios) go
further: after the injection they also work through the documented recovery
(docs/path-reconciliation.md, docs/g-code-commands.md) and prove it actually
restores a working PRELOAD -- a runout during PRELOAD, a lost host heartbeat,
killing and restarting the sim MCU process, a stale host session across a
plain klippy RESTART with the firmware's own state untouched, and clearing a
latched driver fault through the full ACK/RESTART_DRIVER/RECONCILE_PATH/
RECOVER_SESSION sequence. See docs/plan/linux-mcu-integration.md, "Scenarios"
item 3, and tests/linux_mcu/README.md for the socket verbs. Where the sim MCU
cannot get real-time priority, a failure next to a large wall_gap_us in the
timer gap report is a host stall, not a firmware fault: that scenario is
retried once.
"""
import json
import os
import re
import signal
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import control_smoke as smoke
import preload_check as harness
from build_root import skip_unless_available  # noqa: E402

# Lane 0's pins on this build (sim/board/plant.c's tables for the BoxTurtle/AFC
# Lite wiring, out-sim-motion's CONFIG_KLIPPER_MMU_PINS_*): BAY active-low,
# LOAD and the shared HUB active-high.
BAY_PIN = smoke.LANE0_BAY_PIN         # PE13
TMC_PIN = smoke.LANE0_TMC_UART_PIN    # PD5
HUB_PIN = 'PE12'
PRELOAD_FAILED = 15                   # host/wire.py EventType.PRELOAD_FAILED
PRELOAD_COMPLETE = harness.EVENT_PRELOAD_COMPLETE


def check(cond, fmt, *args):
    if not cond:
        raise Exception(fmt % args if args else fmt)


def require(text, pattern, what):
    check(re.search(pattern, text), 'no %s (%s) in: %s', what, pattern, text)


def start(r, refusal_ok=False, op='PRELOAD', bay=0, timeout=30.0):
    """KLIPPER_MMU_<op> BAY=<bay>, retrying while OpenAMS's own state cache
    catches up (preload_check.run_operation does the same); returns the start
    reply's text, admitted or not. With refusal_ok, a host that keeps
    withholding motion permission (as it must with a switch reporting
    filament that is not there) is a refusal, returned as its error text."""
    deadline = time.monotonic() + timeout
    while True:
        resp, lines = r.klippy.call(
            'gcode/script',
            {'script': 'KLIPPER_MMU_%s MMU=box BAY=%d' % (op, bay)})
        if 'error' not in resp:
            return ' '.join(lines)
        if time.monotonic() >= deadline and refusal_ok:
            return 'refused: %s' % resp['error'].get('message', resp['error'])
        check(time.monotonic() < deadline, '%s never started: %s', op,
              resp['error'])
        time.sleep(0.25)


def poll(r, timeout, op='PRELOAD'):
    """Poll KLIPPER_MMU_<op> MMU=box (no BAY) until a terminal result or a
    rejection; one extra poll past the terminal lets the host's ACK-path
    DIAGNOSE land. Returns (lines, (event, code) or None)."""
    deadline = time.monotonic() + timeout
    seen = []
    script = 'KLIPPER_MMU_%s MMU=box' % op
    while time.monotonic() < deadline:
        _, lines = r.klippy.call('gcode/script', {'script': script})
        seen += lines
        for line in lines:
            m = re.search(op + r' terminal .*?event=(\d+) code=(\d+)', line)
            if m:
                time.sleep(0.3)
                _, tail = r.klippy.call('gcode/script', {'script': script})
                return seen + tail, (int(m.group(1)), int(m.group(2)))
            if 'rejected' in line or 'administrative' in line:
                return seen, None
        time.sleep(0.1)
    return seen, None


def diagnose(r):
    """KLIPPER_MMU_DIAGNOSE prints every finding, baseline or not."""
    _, lines = r.klippy.call('gcode/script',
                             {'script': 'KLIPPER_MMU_DIAGNOSE MMU=box'})
    return lines


def evidence(r, lines, extra=None):
    return ' || '.join(([extra] if extra else []) + list(lines) + diagnose(r))


def failed(text, terminal):
    check(terminal is not None, 'PRELOAD never reached a terminal: %s', text)
    check(terminal[0] == PRELOAD_FAILED,
          'expected PRELOAD_FAILED, got event=%d code=%d: %s',
          terminal[0], terminal[1], text)


def stopped(r, note):
    """The plant's own proof that nothing moved after the terminal: SIGUSR1
    dumps ground truth now and again a second later."""
    def sample():
        os.kill(r.mcu.pid, signal.SIGUSR1)
        time.sleep(0.2)
        return harness.read_ground_truth(r.truth)[0]
    before = sample()
    time.sleep(1.0)
    after = sample()
    check((before['position_um'], before['steps'])
          == (after['position_um'], after['steps']),
          '%s: the plant kept moving after the terminal: %s -> %s',
          note, before, after)
    return after


HOST_WATCHDOG_MS = 2000               # printer.cfg's openams_host_watchdog_ms
ADMISSION_NO_SPOOL = 4                 # host/wire.py AdmissionResult.NO_SPOOL


def gcode(r, script):
    """One administrative KLIPPER_MMU_* command; returns its response text,
    raising with the host's own message on a G-code error."""
    resp, lines = r.klippy.call('gcode/script', {'script': script})
    if 'error' in resp:
        raise Exception('%s: %s' % (script, resp['error']))
    return ' '.join(lines)


def recover(r, driver_lane=None, local=False, recover_session=True):
    """The documented fault-recovery sequence (docs/path-reconciliation.md
    "From a stopped fault", and docs/g-code-commands.md "Recovering after a
    dropped connection"): finish delivering a PRELOAD/UNPRELOAD/EJECT result
    that a lost connection stranded before the host could ACK it through the
    normal path (`local` -- KLIPPER_MMU_RECOVER_LOCAL, needed only when the
    connection dropped mid-request, e.g. the heartbeat-loss scenario below;
    every other scenario here keeps its connection live throughout, so the
    normal poll already drained and ACKed the terminal), restart a latched
    driver if one is named (KLIPPER_MMU_RESTART_DRIVER), reconcile the
    shared path (KLIPPER_MMU_RECONCILE_PATH ACTION=CLEAR -- every scenario
    here stops before HUB, so the path is always empty), then release the
    retired HOST session (KLIPPER_MMU_RECOVER_SESSION) so the next
    PRELOAD/UNPRELOAD/EJECT prepares a fresh one. Every step's reply is
    printed for the evidence log; the caller decides what proves recovery."""
    steps = []
    if local:
        steps.append(gcode(r, 'KLIPPER_MMU_RECOVER_LOCAL MMU=box'))
    if driver_lane is not None:
        deadline = time.monotonic() + 5.0
        while True:
            reply = gcode(r, 'KLIPPER_MMU_RESTART_DRIVER MMU=box LANE=%d' % driver_lane)
            steps.append(reply)
            if 'still restarting' not in reply:
                break
            check(time.monotonic() < deadline,
                  'lane %d driver restart never finished: %s', driver_lane, reply)
            time.sleep(0.2)
    steps.append(gcode(r, 'KLIPPER_MMU_RECONCILE_PATH MMU=box ACTION=CLEAR'))
    if recover_session:
        steps.append(gcode(r, 'KLIPPER_MMU_RECOVER_SESSION MMU=box'))
    print('    recovery: %s' % ' || '.join(steps))
    return steps


def preload_completes(r, note):
    """A fresh PRELOAD BAY=0 reaching PRELOAD_COMPLETE: proof that recovery
    actually restored a working PRELOAD, not just a clean DIAGNOSE."""
    start_line = start(r)
    check('admission=0' in start_line,
          '%s: recovery PRELOAD was not accepted: %s', note, start_line)
    lines, terminal = poll(r, 30.0)
    text = evidence(r, lines)
    check(terminal == (PRELOAD_COMPLETE, harness.OPCODE_SUCCESS),
          '%s: recovery PRELOAD did not complete: terminal=%s: %s',
          note, terminal, text)
    print('    recovery PRELOAD terminal: event=%d code=%d' % terminal)


def tmc_fault(r, control):
    """(a) A TMC fault during PRELOAD: the lane stops at once, a driver fault
    is reported, and the plant stops advancing."""
    start_line = start(r)
    check('admission=0' in start_line, 'PRELOAD was not accepted: %s', start_line)
    control.expect_ok('tmc %s fault on' % TMC_PIN)
    lines, terminal = poll(r, 30.0)
    text = evidence(r, lines)
    failed(text, terminal)
    require(text, 'the driver stopped responding or reported a fault',
            'driver fault')
    lane = stopped(r, 'TMC fault')
    check(not lane['load'] and lane['position_um'] < 14000,
          'the lane did not stop mid-lane: %s', lane)
    print('    plant: %s\n    DIAGNOSE: %s' % (lane, text))


def jam(r, control):
    """(b) A jam before the LOAD switch during PRELOAD: the firmware stops with
    its no-progress fault when the FEED_TO_HUB stage times out; the plant is
    held at the jam."""
    control.expect_ok('plant block 0 10000')
    start_line = start(r)
    check('admission=0' in start_line, 'PRELOAD was not accepted: %s', start_line)
    lines, terminal = poll(r, 40.0)
    text = evidence(r, lines)
    failed(text, terminal)
    require(text, 'the lane made no progress', 'no-progress fault')
    lane = stopped(r, 'jam')
    check(lane['block_um'] == 10000 and 9000 <= lane['position_um'] <= 10000,
          'the plant is not held at the jam: %s', lane)
    print('    plant: %s\n    DIAGNOSE: %s' % (lane, text))


def stuck_hub(r, control):
    """(c) A stuck-active HUB switch before PRELOAD starts: the request is
    refused and the firmware reports the sensor mismatch; nothing moves."""
    control.expect_ok('switch %s stuck 1' % HUB_PIN)
    start_line = start(r, refusal_ok=True)
    admitted = 'admission=0' in start_line
    lines, terminal = poll(r, 30.0 if admitted else 10.0)
    text = evidence(r, lines, start_line)
    if admitted:
        failed(text, terminal)
    else:
        check(terminal is None, 'a refused PRELOAD still ran: %s', text)
    require(text, r'the shared path reads occupied \(HUB made\) but no lane is known to be in it',
            'stuck HUB switch')
    lane = stopped(r, 'stuck HUB')
    check(4900 <= lane['position_um'] <= 5100,
          'the lane moved with HUB stuck: %s', lane)
    print('    plant: %s\n    DIAGNOSE: %s' % (lane, text))


def bouncing_bay(r, control):
    """(d) A bouncing BAY switch during PRELOAD: whatever the firmware does must
    be safe (complete or stop), and DIAGNOSE reports the bounce."""
    start_line = start(r)
    check('admission=0' in start_line, 'PRELOAD was not accepted: %s', start_line)
    control.expect_ok('switch %s bounce 300' % BAY_PIN)
    lines, terminal = poll(r, 30.0)
    text = evidence(r, lines)
    check(terminal is not None, 'PRELOAD never settled with BAY bouncing: %s',
          text)
    check(terminal[0] in (PRELOAD_FAILED, PRELOAD_COMPLETE),
          'unexpected terminal event=%d code=%d: %s',
          terminal[0], terminal[1], text)
    # Rule 10 is a delta since the previous capture and the ACK path consumed
    # one; a second capture names the growth since the first.
    text += ' || ' + ' || '.join(diagnose(r))
    lane = stopped(r, 'bouncing BAY')
    print('    terminal: event=%d code=%d' % terminal)
    print('    plant: %s\n    DIAGNOSE: %s' % (lane, text))


def runout_during_preload(r, control):
    """(e) A runout during PRELOAD: `plant runout 0` marks the strand's tail
    at the tip's current position (sim/board/plant.c km_sim_plant_set_runout);
    as the tip keeps advancing, BAY presence is lost the very next step (the
    tail no longer reaches it), and the firmware stops at once with a sensor
    fault. Recovery clears that fault and reconciles the (still-empty) path.
    The plant models a runout as a strand of zero remaining length: once the
    tail is set, no lane position ever covers BAY again in this boot (unlike
    a real spool change, there is no "insert fresh filament" verb on the
    control socket), so PRELOAD on lane 0 cannot physically complete again
    here. What proves recovery instead is that the firmware is no longer
    latched at all: a fresh PRELOAD is admitted and correctly reports
    NO_SPOOL (not FAULTED/BUSY/UNAVAILABLE) instead of hanging or refusing
    for the wrong reason -- exactly the same answer a genuinely empty bay
    would get."""
    start_line = start(r)
    check('admission=0' in start_line, 'PRELOAD was not accepted: %s', start_line)
    time.sleep(0.4)  # let the tip clear the grip point before the runout
    control.expect_ok('plant runout 0')
    lines, terminal = poll(r, 30.0)
    text = evidence(r, lines)
    failed(text, terminal)
    require(text, 'a sensor reading failed', 'sensor failure')
    lane = stopped(r, 'runout')
    check(not lane['bay'], 'BAY still reads present after the runout: %s', lane)
    print('    plant: %s\n    DIAGNOSE: %s' % (lane, text))

    recover(r)
    start_line = start(r, refusal_ok=True)
    no_spool = 'admission=%d' % ADMISSION_NO_SPOOL
    check(no_spool in start_line,
          'recovery did not leave a clean NO_SPOOL refusal: %s', start_line)
    print('    post-recovery PRELOAD: %s' % start_line)


def heartbeat_loss(r, control):
    """(f) Losing the host heartbeat during PRELOAD: SIGSTOPping klippy holds
    its whole reactor, including the service timer that renews the firmware's
    HOST lease (host_watchdog_ms=2000, tests/linux_mcu/preload_check.py's
    write_printer_config), for longer than that lease. A jam just past BAY
    (its own no-progress timeout is 8000ms, comfortably longer) keeps the
    lane from naturally finishing PRELOAD before the 2000ms watchdog can
    matter, so the stop is provably the lease, not a race with completion.
    The plant (queried straight off the control socket, which the paused
    klippy process cannot touch) proves motion stopped within the lease;
    resuming klippy, the host reports the lost lease once it notices; and
    the documented recovery -- KLIPPER_MMU_RECOVER_LOCAL for the local
    PRELOAD request the dead connection stranded before its terminal could
    be ACKed through the normal path, then RECONCILE_PATH and
    RECOVER_SESSION -- restores a working PRELOAD."""
    control.expect_ok('plant block 0 8000')
    start_line = start(r)
    check('admission=0' in start_line, 'PRELOAD was not accepted: %s', start_line)
    before = smoke.truth_lanes(control)[0]
    os.kill(r.klippy_proc.pid, signal.SIGSTOP)
    try:
        time.sleep((HOST_WATCHDOG_MS / 1000.) + 0.6)
        during = smoke.truth_lanes(control)[0]
        time.sleep(0.6)
        after = smoke.truth_lanes(control)[0]
        check((during['position_um'], during['steps'])
              == (after['position_um'], after['steps']),
              'the plant kept moving with klippy stopped: %s -> %s',
              during, after)
        check(during['position_um'] > before['position_um'],
              'the lane never moved before klippy was stopped: %s -> %s',
              before, during)
    finally:
        os.kill(r.klippy_proc.pid, signal.SIGCONT)
    # The service loop that would normally deliver and ACK this terminal
    # through KLIPPER_MMU_PRELOAD's own poll died with the lease (host/
    # openams_managed_unit.py _service() returns reactor.NEVER on the same
    # failure that calls _retain_host_recovery()): unlike every other
    # scenario here, polling PRELOAD's own status never reaches a terminal
    # line, and DIAGNOSE is the only place the retained fault shows up.
    # KLIPPER_MMU_RECOVER_LOCAL below is what actually retrieves and ACKs it.
    lines, terminal = poll(r, 10.0)
    check(terminal is None,
          'expected no normal terminal delivery once the lease died: %s',
          terminal)
    text = evidence(r, lines)
    require(text, 'Klipper stopped heartbeating the MCU', 'lost heartbeat')
    stopped(r, 'heartbeat loss')
    print('    DIAGNOSE: %s' % text)

    control.expect_ok('plant unblock 0')
    recover(r, local=True)
    preload_completes(r, 'heartbeat loss')


def mcu_restart(r, control):
    """(g) MCU restart: kill the sim MCU process and start a fresh one at the
    same pty path, exactly a physical power cycle (see
    control_smoke.restart_mcu -- a plain SIGKILL, not the firmware's own
    `reset` command, so it needs no cooperation from the dying process).
    klippy notices the dropped connection on its own (b'Got EOF...'); RESTART
    (no `FIRMWARE_RESTART`: the old MCU is already gone, nothing to send it
    the reset command) makes it reconnect. The whole reconnect handshake is
    retried for a while: klippy's own reconnect can itself briefly flap
    right after a restart, closing a socket this test only just opened."""
    start_line = start(r)
    check('admission=0' in start_line, 'PRELOAD was not accepted: %s', start_line)
    lines, terminal = poll(r, 30.0)
    check(terminal == (PRELOAD_COMPLETE, harness.OPCODE_SUCCESS),
          'pre-restart PRELOAD did not complete: %s' % evidence(r, lines))

    new_mcu = smoke.restart_mcu(r, True, 'build/linux-mcu/mcu-restart-2.log')
    try:
        deadline = time.monotonic() + 30.0
        while not os.path.exists(r.pty_path):
            check(time.monotonic() < deadline,
                  'the restarted MCU never recreated its pty')
            time.sleep(0.1)
        check(new_mcu.poll() is None, 'the restarted MCU exited immediately')

        r.klippy.sock.sendall((json.dumps(
            {'jsonrpc': '2.0', 'method': 'gcode/script',
             'params': {'script': 'RESTART'}, 'id': 999}) + '\x03').encode())
        time.sleep(2.0)
        deadline = time.monotonic() + 60.0
        new_klippy = None
        while new_klippy is None:
            try:
                new_klippy = smoke.connect_klippy(r.api_socket, timeout=20.0)
                status = smoke.wait_diagnostic_ready(new_klippy, timeout=20.0)
            except Exception as exc:
                new_klippy = None
                check(time.monotonic() < deadline,
                      'klippy never reconnected after the MCU restart: %s', exc)
                time.sleep(1.0)
        print('    post-restart status: %s' % status)

        new_r = smoke.Run(new_klippy, r.control_path, r.truth, new_mcu,
                          r.klippy_proc, r.api_socket, r.mcu_elf, r.pty_path,
                          r.mcu_env)
        preload_completes(new_r, 'MCU restart')
    finally:
        # control_smoke.attempt()'s own cleanup only knows about the
        # *original* MCU process object (its docstring says so), so it can
        # never dump this replacement's timer-gap report; without this, a
        # genuine host stall that hits the restarted MCU (it repeats the
        # whole identify/config handshake, exactly when a loaded host is
        # most likely to fall behind) is invisible to run()'s own
        # worst_wall_gap_us() stall check, which then hard-fails instead of
        # retrying. Mirror attempt()'s SIGUSR2-before-kill so this process's
        # own dispatch gaps land in the same gaps_path_for() file run() reads.
        try:
            os.kill(new_mcu.pid, signal.SIGUSR2)
            time.sleep(0.2)
        except Exception:
            pass
        new_mcu.kill()
        new_mcu.wait(timeout=10)


def stale_session(r, control):
    """(h) Stale session: plain RESTART reloads klippy's own config and
    reconnects to the *same* running MCU -- no `reset` command, no execv, so
    the firmware keeps every bit of state exactly as PRELOAD left it. The new
    host session must recognize the retained PRELOADED lane on its own
    (docs/occupied-host-bootstrap.md's choose_preloaded_host_session, wired
    in automatically at connect -- no explicit recovery command needed here,
    unlike the other scenarios), and UNPRELOAD/PRELOAD must both still work."""
    start_line = start(r)
    check('admission=0' in start_line, 'PRELOAD was not accepted: %s', start_line)
    lines, terminal = poll(r, 30.0)
    check(terminal == (PRELOAD_COMPLETE, harness.OPCODE_SUCCESS),
          'pre-restart PRELOAD did not complete: %s' % evidence(r, lines))

    r.klippy.sock.sendall((json.dumps(
        {'jsonrpc': '2.0', 'method': 'gcode/script',
         'params': {'script': 'RESTART'}, 'id': 999}) + '\x03').encode())
    time.sleep(2.0)
    deadline = time.monotonic() + 60.0
    new_klippy = None
    while new_klippy is None:
        try:
            new_klippy = smoke.connect_klippy(r.api_socket, timeout=20.0)
            status = smoke.wait_diagnostic_ready(new_klippy, timeout=20.0)
        except Exception as exc:
            new_klippy = None
            check(time.monotonic() < deadline,
                  'klippy never reconnected after RESTART: %s', exc)
            time.sleep(1.0)
    print('    post-restart status: %s' % status)

    new_r = smoke.Run(new_klippy, r.control_path, r.truth, r.mcu,
                      r.klippy_proc, r.api_socket, r.mcu_elf, r.pty_path,
                      r.mcu_env)
    unpreload_line = start(new_r, op='UNPRELOAD')
    check('admission=0' in unpreload_line,
          'UNPRELOAD did not recognize the retained session: %s', unpreload_line)
    lines, terminal = poll(new_r, 30.0, op='UNPRELOAD')
    check(terminal == (harness.EVENT_UNLOAD_COMPLETE, harness.OPCODE_SUCCESS),
          'stale-session UNPRELOAD did not complete: terminal=%s: %s',
          terminal, evidence(new_r, lines))
    preload_completes(new_r, 'stale session')


def fault_clear_reconcile(r, control):
    """(i) Fault clear and reconcile: reuse the TMC-fault injection, then
    clear it and work through the documented recovery -- KLIPPER_MMU_
    RESTART_DRIVER for the latched driver, KLIPPER_MMU_RECONCILE_PATH to
    clear the retained fault and empty path, KLIPPER_MMU_RECOVER_SESSION for
    the connection the service loop marked ambiguous once the fault blocked
    its cycle -- proving a working PRELOAD afterward."""
    tmc_fault(r, control)
    control.expect_ok('tmc %s fault off' % TMC_PIN)
    recover(r, driver_lane=0)
    preload_completes(r, 'fault clear and reconcile')


SCENARIOS = (('tmc-fault', tmc_fault), ('jam', jam),
             ('stuck-hub', stuck_hub), ('bouncing-bay', bouncing_bay),
             ('runout-during-preload', runout_during_preload),
             ('heartbeat-loss', heartbeat_loss),
             ('mcu-restart', mcu_restart),
             ('stale-session', stale_session),
             ('fault-clear-reconcile', fault_clear_reconcile))


def run(name, body, plant=harness.write_plant_state):
    """Boot the stack for one scenario and run body on it, retrying once if the
    attempt's worst wall gap shows a host stall (a slow, non-real-time sim MCU,
    not a firmware fault). True when the scenario passed. `plant` is the
    initial plant state writer (every lane empty by default); a caller whose
    scenario needs lanes already staged past BAY/LOAD (e.g. a LOAD-side
    scenario reusing this runner) passes its own."""
    first = None
    for number in (1, 2):
        tag = '%s-%d' % (name, number)

        def scenario(r, b=body):
            control = smoke.Control(r.control_path)
            try:
                b(r, control)
            finally:
                control.close()

        try:
            print('=== scenario %s, attempt %d ===' % (name, number))
            smoke.attempt(number, tag=tag, plant=plant,
                          checks_fn=scenario)
            if first is not None:
                print('%s: attempt 1 failed (%s); the retry passed.'
                      % (name, first))
            return True
        except Exception as exc:
            gap = smoke.worst_wall_gap_us(smoke.gaps_path_for(number, tag))
            if (number == 2 or gap is None
                    or gap <= smoke.SWITCH_FRESHNESS_BUDGET_US):
                print('scenario %s: FAIL: %s' % (name, exc))
                traceback.print_exc()
                return False
            first = exc
            print('%s attempt 1 failed: %s' % (name, exc))
            print('its worst wall gap was %s us, over the %d us switch-sample '
                  'freshness budget: a host stall, so it is retried once.'
                  % (gap, smoke.SWITCH_FRESHNESS_BUDGET_US))


def main():
    skip_unless_available('tests/linux_mcu/fault_check.py')
    broken = [name for name, body in SCENARIOS if not run(name, body)]
    if broken:
        raise SystemExit('Fault scenarios failed: %s' % ', '.join(broken))
    print('All fault scenarios passed.')


if __name__ == '__main__':
    main()
