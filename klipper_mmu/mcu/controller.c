// SPDX-License-Identifier: GPL-3.0-or-later
#include "autoconf.h" // CONFIG_*, before stepper.h tests CONFIG_WANT_STEPPER_LOCAL
#include "controller.h"
#include "stepdir_gpio.h"
struct timer;
#include "stepper.h"
#include "clock.h"
#include "board/irq.h"
#include "board/misc.h"
#include "command.h"
#include "sched.h"

/* The timer interrupt services STEP/DIR edges of `edge`, a trajectory copy
 * holding the adopted integer image and the output state, and wakes the
 * control task every period. The task (sensors, km_core_update, planning,
 * events) and the command handlers own the core; Klipper tasks never preempt
 * one another. km_controller_changed() exchanges the two copies. */
static km_operation_core_t *attached_core = NULL;
static km_controller_sample_fn attached_sample_fn = NULL;
static void *attached_sample_ctx = NULL;
static km_trajectory_t edge;
static bool edge_fault_seen, edge_idle_seen; /* As of the last exchange */
static uint32_t lease_us; /* Edges end here without a completed control cycle */

static struct timer controller_timer;
static struct task_wake control_wake;
static bool timer_active = false;
static bool controller_shutdown_latched = false;

static uint32_t next_periodic_us = 0u;
static uint32_t switch_acquisition_us, switch_delay_us;
static uint32_t switch_sample_count, switch_delay_max_us;
static bool switch_sample_pending, switch_delay_valid;

static bool is_driver_clean(const km_driver_t *d)
{
    return !d || (!d->enabled && !d->step_pin_high && !d->pwm_duty_permille
                  && km_driver_get_fault(d) == KM_FAULT_NONE);
}

static bool validate_drivers(const km_operation_core_t *core)
{
    for (uint8_t i = 0u; i < KM_MAX_LANES; ++i) {
        if (!is_driver_clean(core->feeder_drivers[i]) ||
            !is_driver_clean(core->rewinder_drivers[i]))
            return false;
    }
    return is_driver_clean(core->selector_driver)
        && is_driver_clean(core->active_feeder)
        && is_driver_clean(core->active_rewinder)
        && is_driver_clean(core->trajectory.driver);
}

static uint32_t queued_end;  /* start clock of the next window */
static bool queued_valid;    /* queued_end chains from the last window */

/* Klipper runs timers with interrupts disabled, so this whole callback is a
 * masked section. It is integer only and never touches the core. */
static uint_fast8_t km_controller_timer_event(struct timer *t)
{
    if (controller_shutdown_latched || sched_is_shutdown() || !attached_core) {
        timer_active = false;
        return SF_DONE;
    }
    km_core_timepoint_t now;
    if (km_clock_get_time(&now) != KM_OK) {
        timer_active = false;
        return SF_DONE;
    }

    if (!km_time_before(now.us, next_periodic_us)) {
        sched_wake_task(&control_wake);
        /* Advance without replaying missed periods. */
        if (km_time_before(now.us, next_periodic_us + KM_CONTROLLER_PERIOD_US))
            next_periodic_us += KM_CONTROLLER_PERIOD_US;
        else
            next_periodic_us = now.us + KM_CONTROLLER_PERIOD_US;
    }

    struct stepper *stepper = km_stepdir_gpio_get_stepper_for_driver(edge.driver);
    bool active = stepper && stepper_local_active(stepper);
    int64_t completed = km_stepdir_gpio_get_position_for_driver(edge.driver);

    /* The lease is checked before generating: once it expires no new window
     * is generated, the queued windows drain, and a stepper that drains with
     * motion still commanded is a fault, so motion after the control task
     * dies ends within lease + LEAD + one tick.
     *
     * Windows chain: window k starts at queued_end, which advances one tick
     * per window and is only taken afresh (now + LEAD) after attach, a stop,
     * or at rest. An idle stepper between slow steps is normal and is queued
     * at queued_end; the one refusal, a start clock inside the local
     * stepper's lead, means the tick ran more than LEAD late: an underrun
     * fault, never a quiet re-anchor, because the motor has already stalled. */
    bool moving = km_trajectory_is_moving(&edge);
    bool fault = false;
    bool chained = false;
    if (edge.faulted) {
        /* Already latched: generate nothing further. */
    } else if (!km_time_before(now.us, lease_us)) {
        fault = moving && !active;
    } else {
        if (!moving && !active)
            queued_valid = false;
        if (!queued_valid) {
            queued_end = timer_read_time()
                + timer_from_us(2 * CONFIG_KLIPPER_MMU_RATE_TICK_US);
            queued_valid = true;
        }
        km_trajectory_window_t out;
        uint32_t min_interval_ticks = edge.driver
            ? timer_from_us(edge.driver->caps.min_step_interval_us) : 0;
        km_result_t res = km_trajectory_window(&edge, CONFIG_KLIPPER_MMU_RATE_TICK_US,
                                               timer_from_us(1), min_interval_ticks,
                                               !active, &out);
        /* All or nothing: room is checked for the whole window first. */
        fault = res != KM_OK
            || (stepper && stepper_local_room(stepper) < out.count);
        for (uint8_t i = 0; !fault && stepper && i < out.count; i++)
            fault = km_stepdir_gpio_queue(edge.driver, queued_end, out.segments[i].interval,
                                        out.segments[i].count, out.segments[i].add,
                                        out.segments[i].dir) != 0;
        if (!fault) {
            km_trajectory_commit(&edge, &out);
            queued_end += timer_from_us(CONFIG_KLIPPER_MMU_RATE_TICK_US);
            chained = moving;
        }
    }
    if (fault) {
        /* Stop, resync to the completed steps (exact; the position is
         * uncertain only mechanically, when motion was cut off), latch. */
        edge.faulted = true;
        queued_valid = false;
        if (stepper)
            stepper_local_stop(stepper);
        km_driver_shutdown(edge.driver);
        km_trajectory_resync(&edge, completed, moving || active);
        sched_wake_task(&control_wake);
    }

    /* Activity after this tick's queueing, so the mirror does not lag. */
    km_trajectory_observe(&edge, completed, stepper && stepper_local_active(stepper));
    edge.tick_seq++;

    /* A tick that chained a moving window replays if late: queued_end's LEAD
     * depends on every missed tick actually being generated. Otherwise (idle,
     * faulted, or lease-expired) resync to now, so a host stall drains toward
     * the lease/fault above instead of Klipper's "Rescheduled" shutdown. */
    uint32_t period = timer_from_us(CONFIG_KLIPPER_MMU_RATE_TICK_US);
    uint32_t now_ticks = timer_read_time();
    t->waketime += period;
    if (!chained && timer_is_before(t->waketime, now_ticks))
        t->waketime = now_ticks + period;
    return SF_RESCHEDULE;
}

/* Task context. Publish the core's trajectory to the edge service and collect
 * its output. A task-side stop reaches the edge (the stop itself already
 * disabled the driver). A new image is adopted on the live output. An edge
 * fault the task has not seen refuses the publication and stops the core. */
static void exchange(bool renew, uint32_t renew_until_us)
{
    km_operation_core_t *core = attached_core;
    if (!core || controller_shutdown_latched || sched_is_shutdown())
        return;
    km_trajectory_t *plan = &core->trajectory;
    km_core_timepoint_t now;
    bool clock_ok = km_clock_get_time(&now) == KM_OK;
    bool replan = !plan->faulted && !edge_idle_seen && plan->command_seq != edge.command_seq;
    irqstatus_t flag = irq_save();
    bool fault = !clock_ok || (edge.faulted && !edge_fault_seen);
    
    if (fault || plan->faulted) {
        if (!edge.faulted)
            km_trajectory_emergency_stop(&edge);
    } else if (edge_idle_seen) {
        edge = *plan; /* The edge has not changed since the last exchange. */
        
    } else if (replan) {
        km_trajectory_publish(&edge, plan, now.us);
    }
    
    /* publish itself stops the edge on a target now inside the stopping
     * distance: that fault must stop the core like any other. */
    if (edge.faulted && !edge_fault_seen)
        fault = true;
    if (renew && !fault)
        lease_us = renew_until_us;
    km_trajectory_collect(plan, &edge);
    edge_fault_seen = edge.faulted;
    edge_idle_seen = !km_trajectory_is_moving(&edge);
    irq_restore(flag);
    if (fault) {
        km_fault_t reason = km_driver_get_fault(core->active_feeder);
        if (reason == KM_FAULT_NONE)
            reason = km_driver_get_fault(core->active_rewinder);
        if (reason == KM_FAULT_NONE)
            reason = KM_FAULT_CONTROL_TIMING;
        km_core_explicit_stop(core, reason, clock_ok ? now.ms : core->last_control_ms);
    }
}

void km_controller_changed(void)
{
    exchange(false, 0u);
}

void km_controller_task(void)
{
    if (!sched_check_wake(&control_wake))
        return;
    km_operation_core_t *core = attached_core;
    if (!core || controller_shutdown_latched || sched_is_shutdown())
        return;
    exchange(false, 0u);
    switch_sample_pending = false;
    switch_delay_valid = false;
    if (attached_sample_fn)
        attached_sample_fn(core, attached_sample_ctx);
    km_core_timepoint_t now;
    if (km_clock_get_time(&now) != KM_OK) {
        km_core_explicit_stop(core, KM_FAULT_CONTROL_TIMING, core->last_control_ms);
        exchange(false, 0u);
        return;
    }
    if (switch_sample_pending && !km_time_before(now.us, switch_acquisition_us)) {
        uint32_t elapsed = km_time_elapsed(now.us, switch_acquisition_us);
        if (elapsed < KM_TIME_WRAP_HORIZON_US) {
            switch_delay_us = elapsed;
            switch_delay_valid = true;
            if (switch_sample_count != UINT32_MAX) ++switch_sample_count;
            if (elapsed > switch_delay_max_us) switch_delay_max_us = elapsed;
        }
    }
    km_core_update(core, now.ms, now.us);
    exchange(true, now.us + KM_CONTROLLER_LEASE_US);
}
DECL_TASK(km_controller_task);

km_result_t km_controller_attach(km_operation_core_t *core,
                                 km_controller_sample_fn sample_fn,
                                 void *sample_context)
{
    irqstatus_t flag = irq_save();
    km_result_t result = KM_OK;

    /* Validate call context: must be called from task context with IRQs enabled */
    if (flag != 0ul) {
        result = KM_ERR_INVALID_PARAM;
    } else if (!core) {
        result = KM_ERR_INVALID_PARAM;
    } else if (controller_shutdown_latched || sched_is_shutdown()) {
        result = KM_ERR_FAULTED;
    } else if (attached_core != NULL) {
        result = KM_ERR_BUSY;
    } else if (km_clock_get_state() != KM_CLOCK_STATE_READY) {
        result = KM_ERR_UNAVAILABLE;
    } else if (km_profile_validate(&core->profile) != KM_OK
               || core->op_state != KM_OP_STATE_IDLE
               || core->stage != KM_STAGE_STOPPED
               || core->shared_path_owned
               || core->terminal_pending
               || core->system_fault != KM_FAULT_NONE
               || core->motion_fault != KM_FAULT_NONE
               || core->trajectory.is_active
               || core->trajectory.faulted
               || core->trajectory.position_uncertain
               
               || km_trajectory_is_moving(&core->trajectory)
               || !validate_drivers(core)) {
        result = KM_ERR_INVALID_PARAM;
    } else {
        km_core_timepoint_t now;
        result = km_clock_get_time(&now);
        if (result == KM_OK) {
            attached_core = core;
            attached_sample_fn = sample_fn;
            attached_sample_ctx = sample_context;
            switch_sample_pending = false;
            switch_delay_valid = false;
            switch_sample_count = 0u;
            switch_delay_max_us = 0u;
            edge = core->trajectory;
            edge_idle_seen = true;
            edge_fault_seen = false;
            lease_us = now.us;
            queued_valid = false;

            next_periodic_us = now.us + KM_CONTROLLER_PERIOD_US;

            controller_timer.func = km_controller_timer_event;
            controller_timer.waketime = timer_read_time() + timer_from_us(CONFIG_KLIPPER_MMU_RATE_TICK_US);
            sched_add_timer(&controller_timer);
            timer_active = true;

        }
    }

    irq_restore(flag);
    return result;
}

void km_controller_shutdown(void)
{
    /* Upstream resets user timers before running shutdown hooks.
     * Peripheral raw shutdown hooks own physical safe-off.
     * Latch shutdown permanently until real MCU reset without reading the clock
     * or recursing through portable driver operations. */
    controller_shutdown_latched = true;
    timer_active = false;
    switch_sample_pending = false;
    switch_delay_valid = false;
}
DECL_SHUTDOWN(km_controller_shutdown);

bool km_controller_is_attached(void)
{
    return attached_core != NULL;
}

bool km_controller_is_shutdown(void)
{
    return controller_shutdown_latched || sched_is_shutdown() != 0u;
}

/* Task-context statistics: the sample, the cycle and the queries all run in
 * the control task or command handlers. A note outside a control cycle is
 * cleared when the next cycle begins. */
void km_controller_note_switch_sample(km_operation_core_t *core, uint32_t acquisition_us)
{
    if (!controller_shutdown_latched && core == attached_core) {
        switch_acquisition_us = acquisition_us;
        switch_sample_pending = true;
    }
}

bool km_controller_get_switch_sample_delay(uint32_t *out_us)
{
    if (!out_us) return false;
    bool valid = switch_delay_valid && !controller_shutdown_latched;
    if (valid) *out_us = switch_delay_us;
    return valid;
}

void km_controller_get_switch_sample_stats(uint32_t *count, uint32_t *max_us)
{
    if (!count || !max_us) return;
    *count = switch_sample_count;
    *max_us = switch_delay_max_us;
}
