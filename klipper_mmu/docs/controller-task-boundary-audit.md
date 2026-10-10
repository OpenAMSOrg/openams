# Controller task/interrupt boundary

Implemented for  R1 on top of the [integer STEP schedule](step-dir-backend.md)
(R2). This is software evidence only: native tests, static cross-compiles and
the release image build. No interrupt latency or stack depth was measured on
a target; worst-case latency is measured with DWT at bench qualification.

Before R1, `km_controller_timer_event()` ran, in one Klipper timer callback
with interrupts masked, the sensor sample callback (switch debounce, DiffA/DiffB
decode), `km_core_update()` (operation state, FPS arrival, follower, events,
and the `double` model and planning) and STEP edge service. Command handlers
planned in `double` under their own `irq_save()` sections. Nothing else, USB,
CAN or the TMC UART bit timer included, ran meanwhile.

## Ownership and synchronization

| Context | Work | State it owns | Synchronization |
|---|---|---|---|
| Timer interrupt (`km_controller_timer_event`) | STEP/DIR edges from the rate state; wakes the control task every 1 ms; motion lease; timer rearm | `edge`, a controller-owned `km_trajectory_t`: rate state, deadline, position, completed-pulse count, pulse in flight, direction and timing, faults | Klipper timers run with interrupts disabled. Integer only; never touches the core. |
| Control task (`km_controller_task`, `DECL_TASK`) | Exchange; sensor sample callback; `km_core_update()`; exchange again and renew the lease | The core, its trajectory copy (rate state and commands), events, sensor and debounce state, switch-delay statistics | Task context, interrupts enabled. Tasks never preempt one another. |
| Command handlers (`commands.c`) | Admission, heartbeat, stop, reconcile, restart, event and snapshot commands | Command state; the core, as the control task | Task context: no lock against the control task. `changed` (`km_controller_changed()`) before planning or capturing, and after changing the core. |
| Exchange (`km_controller_changed()`) | Publish the core's trajectory; collect the edge output | `edge_fault_seen`, `edge_idle_seen`, `lease_us` (task written) | Short `irq_save()` sections (below). |

Shared with interrupts, and nothing else of the controller path:

- **`edge`**, published and collected only by the exchange.
- **The STEP/DIR driver.** The interrupt steps it while the task may enable,
  stop or rebind it. The portable driver is a single-threaded state machine
  (`last_call` monotonic check, pulse state), so `stepdir_gpio.c` runs the
  task's `set_enable` and `stop_outputs` in one short critical section each;
  STEP and DIR edges already run masked. Without it, a task stop racing a
  rising edge can latch a spurious driver fault.
- **The lease** (`lease_us`), one aligned 32-bit word written under the lock.
- Existing accessors with their own locks: the clock adapter, the ADC pair
  (`km_adc_get_sample_at`) and TMC UART status. Raw safe-off paths (TMC
  `force_off`, STEP/DIR `force_off`, shutdown hooks) are unchanged.

The switch sampler, sensor composition and command handlers no longer take
`irq_save()` at all: no interrupt touches their state or the core. The deleted
masked sections are the timer's outer lock, the sample callback and switch
sampler locks (twenty GPIO reads and the 64-bit Diff decode), and every command
handler lock (admission and planning). `in_timer_callback` and its special
cases are gone: the interrupt never calls back into the controller.

## Handoff

The task's copy is `core->trajectory`; the interrupt's copy is `edge`. Every
exchange ends by collecting the edge output into the task copy
(`km_trajectory_collect()`): position, pulse count, direction, deadline,
timing and faults. Output still due is
collected as `KM_STEP_STATE_PENDING`, which counts as moving. The core
therefore cannot settle and disable a driver the interrupt still drives.

Publication, under the same lock:

1. **Unseen edge fault.** If the edge faulted after the last exchange, nothing
   is published. The task copy collects the fault, and the exchange stops the
   core with the driver's own fault when it has one, otherwise
   `KM_FAULT_CONTROL_TIMING` (the same timing reasons the old timer used).
2. **Task-side stop.** A faulted task copy stops the edge. The core's own stop
   has already disabled the driver, so no later edge can raise STEP.
3. **Idle edge.** If the edge had no deadline and no pulse at the last exchange,
   the whole task copy is taken, including a plan from rest, a recovery or a
   rebinding.
4. **New command during motion.** `command_seq` counts commands.
   `km_trajectory_publish()` copies the command part (targets, limits, jerk)
   into the edge copy, keeping its live state and output. A target now inside
   the stopping distance stops the edge with a fault, and the exchange stops
   the core.

After publishing a deadline, the exchange rearms the timer. The edge keeps its
own authority to finish a high pulse, count the falling edge once, enforce
pulse, interval, DIR, lateness and missed-pulse limits, and stop outputs on
failure.

**Lease (underrun).** Each completed control cycle sets the lease to its control
time plus five periods (`KM_CONTROLLER_LEASE_US`). The interrupt emits no edge
at or after the lease. At its first wake there with output due, it stops the
driver and latches a fault. Motion without fresh sensor processing is thus
bounded by five periods after the last completed cycle, and the fault latches
within one more. Commands do not renew the lease.

**Faults and coincident edges.** The core's stop disables the driver
immediately and the exchange stops the edge copy, so no edge follows a fault the
task has observed. An edge that falls due before the task has observed a
coincident fault is emitted and counted. The old single callback sampled
first, then stepped.

**Not covered.** PWM rewinders are task-commanded; under starvation the lease
stops STEP/DIR motion, but PWM keeps its last duty until the task runs.
Sensors and the host watchdog are task checks too.

## Evidence

Native tests, all under ASan/UBSan:

- `tests/klipper_controller` (29 scenarios, 6 new). Interrupt preemption is
  modeled by running the timer at a chosen `irq_restore()` to enabled (a
  pending interrupt fires when task code unmasks):
  - `test_task_starvation_underrun_fails_closed`: timers without the task. STEP
    rises only before the lease; the driver is off within one period after it;
    the next task run reports `KM_FAULT_CONTROL_TIMING` and `LOAD_FAILED`; later
    exchanges do not revive the motion.
  - `test_command_between_task_cycles`: `km_cancel` 1.5 ms after the last
    cycle, when the stale copy's deadline is more than 1 ms old. The command's
    exchange collects first, so no lateness fault; the stop is adopted on the
    live output with exact accounting.
  - `test_isr_fault_before_exchange_collect` and
    `test_isr_fault_after_exchange`: the task issues a new command, then the
    interrupt faults. Its emergency stop turns the outputs off at once. A fault
    before the exchange collects is refused by that exchange; one after its
    single locked section (no seek window remains) is latched by the next
    exchange, one control cycle later. No STEP follows either way.
  - `test_coincident_sensor_fault_prevents_next_edge`: the task observes a
    sensor fault while the next STEP is due; that edge is not emitted.
  - `test_exact_accounting_across_handoffs`: 40 retargets, with interrupt edges
    before and during the exchange. Rising edges equal the
    travel and the completed-pulse count, with pulse and interval limits kept.
- `tests/test_trajectory.c` `test_trajectory_publish_and_collect`: a command
  published during motion keeps the edge's live position and is served at
  once; publishing again with nothing new is a no-op; output still due
  collects as pending.
- `tests/klipper_sensors` `superseded_decode` covers a pair completed during a
  decode (decoded once more; twice leaves feedback invalid). Switch, sensor and
  command fixtures now assert sampling and exchanges run with interrupts
  enabled.
- Mutation checks: removing the unseen-fault refusal, the commands' pre-exchange
  or the underrun check each fails the matching controller test.
- The composed FPS suite (`test-fps-composed`, five scenarios) runs the
  production controller timer and task with the ADC timer.

Static reachability from the masked roots (every function reachable, over-
approximating indirect driver calls; instructions are code size, not a timed
path; loops count once). Non-LTO `arm-none-eabi-gcc -O2 -mthumb`, test shims:

| Masked root | Cortex-M4 before | after | Cortex-M7 before | after |
|---|---|---|---|---|
| Timer callback: functions / instructions / longest call chain | 77 / 7,044 / 2,177 | 29 / 1,707 / 836 | 77 / 7,145 / 2,209 | 29 / 1,726 / 848 |
| Timer callback: soft-float and libm call sites / 64-bit divides | 173 / 14 | 0 / 2 | 173 / 14 | 0 / 2 |
| Command start/cancel lock: instructions / float sites | 5,280 / 122 | none | 5,349 / 122 | none |
| Exchange sections (collect, adopt, stop, rearm): instructions / float | - | 827 / 0 | - | 837 / 0 |
| Locked task-side STEP/DIR `set_enable` and `stop_outputs` | - | 484 / 0 | - | 486 / 0 |

Re-measured for the rate core with `tools/masked_reach.py` on the linked LTO
`boxturtle-h723-usb` release image (the tool counts differently from the
non-LTO table above, so compare rows of the same method):

| From `km_controller_timer_event` | RC (phase-list planner) | Rate core |
|---|---|---|
| Functions / instructions / longest call chain | 14 / 1,342 / 886 | 19 / 2,963 / 1,478 |
| Soft-float and libm call sites | 0 | 0 |
| 64-bit divide call sites | 2 | 2 |

The interrupt path grew about 2.2 times (the edge solver's Newton steps and the
closed-form stopping distance) and still reaches no floating point. Both 64-bit
divides are the same clock conversions as before (`km_clock_deadline_ticks`,
`observe_locked`); the rate core's own divides stay in `km_rate_set_limits`,
which runs in the task. Timing is still unmeasured: the interrupt's worst case
is measured with DWT at bench qualification.

The exchange also copies the trajectory when the edge was idle.
The two 64-bit divides are in the clock conversions' non-whole-MHz branches,
unused on Klipper STM32 clocks. The now-unmasked control task reaches 6,963
(M4) and 7,054 (M7) instructions and all 173 float call sites.

The linked LTO release images agree. From `km_controller_timer_event` in
`boxturtle-h723-usb` (Cortex-M7) 7,577 instructions and 174 float or libm call
sites were reachable at `e34e7de`, and 2,290 and 0 at `d883045`. In
`nightowl-f407-usb` (Cortex-M4) the counts were 7,043 and 174, and are now
1,854 and 0.

Stack frames were not re-measured under LTO. The edge interrupt no longer
nests `km_core_update()` or planner frames (656-664 bytes non-LTO) on the
shared stack; those now run in the task. The 4 KiB [reservation](stack-reservation.md)
is unchanged.
