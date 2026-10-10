<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Firmware Local Controller Scheduler

`mcu/controller.c` joins the common operation core (`km_operation_core_t`) to
Klipper's scheduler with one timer, one task and the public `sched_*`,
`irq_save`/`irq_restore` and `timer_read_time` interfaces. It runs STEP/DIR
edges and the periodic protection loop on the MCU without host motion queueing.

## Interrupt and task split

- **Timer interrupt** (`km_controller_timer_event`). Klipper runs timers with
  interrupts disabled, so the whole callback is masked. It is integer-only and
  touches only `edge`, a controller-owned trajectory copy holding the rate state
  and output state. Instead of per-edge service, it runs on a fixed rate tick
  to generate step segments. Windows are chained two ticks ahead (`queued_end`),
  taken afresh only after attach, a stop, or at rest. It wakes the control task
  every `KM_CONTROLLER_PERIOD_US` and enforces the lease below. It never touches
  the core and calls no floating point or planning code.
- **Control task** (`km_controller_task`, `DECL_TASK`). Every period it
  exchanges with the edge copy, samples sensors (switch debounce, DiffA/DiffB
  decode), runs `km_core_update()` (operation state, follower, trajectory commands,
  events), then exchanges again and renews the lease.
- **Command handlers** also run in task context. Klipper tasks never preempt one
  another, so the core needs no lock between commands and the control task.
- **Exchange** (`km_controller_changed()`, the commands' `changed` callback).
  Under short `irq_save()` sections it publishes the core's trajectory to the
  edge copy and then collects the edge output back (`km_trajectory_collect()`).
  Publication is one of:
  - a task-side stop, or an edge fault the task has not yet collected: the edge
    stops, and the exchange stops the core with the driver's own fault when it
    has one, otherwise `KM_FAULT_CONTROL_TIMING` (a controller timing fault, not
    a driver or wiring one — report it with `KLIPPER_MMU_DIAGNOSE`);
  - the whole copy, when the edge was idle at the previous exchange;
  - a published command during motion: `km_trajectory_publish()` copies the
    command part into the edge copy while keeping its live state and output.
  A publish-time fault (a position target now inside the stopping distance)
  stops the edge, and the exchange stops the core.
  Callers outside the control task exchange before planning or capturing a
  snapshot and again after changing the core. Output still due is collected as
  `KM_STEP_STATE_PENDING`, which keeps the core's trajectory moving, so a
  settle cannot disable a driver the interrupt still drives.
- **Shared STEP/DIR driver.** The task still enables, stops and binds the driver
  the interrupt steps. `stepdir_gpio.c` runs the task's `set_enable` and
  `stop_outputs` in one short critical section each; STEP and DIR edges already
  run masked. Raw safe-off paths (TMC `force_off`, STEP/DIR `force_off`,
  shutdown hooks) are unchanged and interrupt safe.

## Motion lease and underrun

At the end of each cycle the task sets the lease to its control time plus
`KM_CONTROLLER_LEASE_US` (five periods, the switch qualification time). The
lease is checked before generating segments: once it expires, no new window is
generated and the queued windows drain. A stepper that drains with motion still
commanded is a fault, so motion after the control task dies ends within the
lease plus the lead and one tick. The next task exchange reports
`KM_FAULT_CONTROL_TIMING`. Motion also needs a completed cycle first: a START
before the first cycle stops.

Windows chain sequentially: window k starts at `queued_end`, advancing one tick
per window. If the local stepper refuses a start clock because it falls inside
its lead time, it means the tick ran more than the lead time late. This is an
underrun fault. It is never quietly re-anchored, because by then the motor has
already stalled.

A fault raised in the tick (underrun, lease expiry, a generator refusal) stops
the local stepper, shuts the driver down via `km_driver_shutdown`, resyncs to
completed steps (position uncertain when motion was cut off, since the count is
exact but the motor may have slipped) and latches. A fault the task observes
takes the core's stop, whose `km_driver_shutdown` stops the local stepper
through `locked_stop` in `mcu/stepdir_gpio.c`, so no queued step runs after
it. The tick observes completed
steps after queueing via `km_trajectory_observe`, and `tick_seq` advances to
prove liveness to the follower. The bound is software only; interrupt latency is
measured at bench qualification.

PWM rewinders and sensors are task-owned. If the task starves, the lease stops
STEP/DIR motion; PWM keeps its last command until the task runs again.

## Architecture and Scope

- **Direct Module**: One Klipper timer and one task, no generic event or queue
  framework. The handoff is the edge copy plus the command sequence.
- **Optional Build**: Linked only when the add-on is enabled. The H723 board startup
  attaches this controller after driver readiness; hosted diagnostic startup
  separately gates actuator START. H723 and simulator builds are verified.
- **Caller-Owned Resources**: The firmware caller owns static allocation of the `km_operation_core_t`,
  hardware pin mappings, motor drivers, and profile configuration. Attachment takes ownership
  only after validating all resources are stopped and clean.

## Lifecycle and Startup Contract

Attachment occurs via `km_controller_attach(core, sample_fn, sample_context)`:
1. **Precondition Validation**:
   - Must be invoked from task context with interrupts enabled (`irq_save() == 0`).
   - Clock adapter must be in `KM_CLOCK_STATE_READY`.
   - Neither Klipper nor this controller module may be in global shutdown.
   - Profile must be valid per `km_profile_validate(&core->profile)`.
   - Operation core must be clean, initialized, and stopped:
     - `op_state == KM_OP_STATE_IDLE` (e.g. cores in `LOADED` or `FAULT` are rejected).
     - `stage == KM_STAGE_STOPPED`.
     - `shared_path_owned == false`, `terminal_pending == false`.
     - `system_fault == KM_FAULT_NONE` and `motion_fault == KM_FAULT_NONE`.
     - Trajectory must be inactive (`!is_active`, `!faulted`, `!position_uncertain`,
       `step_state == KM_STEP_STATE_IDLE`, and `!km_trajectory_is_moving`).
   - Every bound driver (feeder, rewinder, selector, active, trajectory) must be clean:
     disabled (`!enabled`), logical STEP inactive (`!step_pin_high`), PWM inactive (`pwm_duty_permille == 0`),
     and free of faults (`km_driver_get_fault(d) == KM_FAULT_NONE`).
   - Unbound lanes are permitted; no implicit driver initialization is performed.
   - On rejection, no outputs are changed, no driver state is altered, and no timer is acquired.
2. **Immutability and Duplicate Rejection**:
   - Attachment succeeds exactly once per MCU boot lifecycle.
   - Subsequent calls return `KM_ERR_BUSY` and preserve the existing attachment.
3. **Shutdown Invariant**:
   - Registered with Klipper's `DECL_SHUTDOWN(km_controller_shutdown)`.
   - Latches `controller_shutdown_latched = true` permanently until MCU reset, persisting
     across `sched_clear_shutdown()`.
   - Shutdown performs no clock reads, no dynamic allocation, and does not recurse through
     driver operations; physical pin safe-off is handled by peripheral shutdown hooks.

## Scheduling, Cadence, and Limits

- **Periodic cadence**: the interrupt wakes the task every 1 ms
  (`KM_CONTROLLER_PERIOD_US`). If a wake is late, the next target advances
  without replaying a backlog: by 1000 us while still within one period,
  otherwise to `now.us + 1000u`.
- **Finite scheduling lead**: `KM_CONTROLLER_MIN_LEAD_US = 1u` keeps the raw
  timer target after the last observed tick. It does not prevent Klipper's
  `try_shutdown("Timer too close")` under every real interrupt latency.
- **Switch-delay statistic**: the task records the interval from the switch
  batch's acquisition to the clock read just before `km_core_update()`. Its
  meaning is unchanged, but it is now measured in task context and includes
  any interrupt service in between. Only a sample taken in the current cycle
  counts; a note outside a cycle is cleared when the next cycle begins.
- **Execution cost**: native tests and static cross-compiles establish software
  behavior and code paths only. Real worst-case interrupt and task latency
  require DWT measurement at bench qualification.

Sampling must retain real acquisition timestamps and validity, and must not
perform host I/O, blocking waits or motor commands. It runs with interrupts
enabled: the ADC accessor copies one pair under its own lock, and a pair
completed during a decode supersedes it and is decoded once instead. Shutdown
hooks perform physical safe-off without clock reads and cannot be undone by
`clear_shutdown`.

The earlier acceptance (23 integration scenarios and an H723 image with the
controller path under LTO, never executed) predates this split; see
review evidence.
The split's evidence is in [the boundary audit](../docs/controller-task-boundary-audit.md).

## Public Interface

```c
typedef void (*km_controller_sample_fn)(km_operation_core_t *core, void *context);

KM_API km_result_t km_controller_attach(km_operation_core_t *core,
                                        km_controller_sample_fn sample_fn,
                                        void *sample_context);
KM_API void km_controller_changed(void);   /* task context: exchange */
void km_controller_task(void);             /* DECL_TASK */
KM_API bool km_controller_is_attached(void);
KM_API bool km_controller_is_shutdown(void);
void km_controller_shutdown(void);
```
