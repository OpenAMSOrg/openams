#include <stdlib.h>
// SPDX-License-Identifier: GPL-3.0-or-later
#include "klipper_mmu/selector_home.h"
#include <string.h>
static bool fresh(km_selector_home_switch_t s, uint32_t now, uint32_t age)
{
    return s.valid && !km_time_before(now, s.sampled_at_us) &&
           km_time_elapsed(now, s.sampled_at_us) <= age;
}
static km_result_t fail(km_selector_home_t *h, km_result_t r)
{
    km_trajectory_emergency_stop(h->trajectory);
    h->position_known = false;
    h->state = KM_SELECTOR_HOME_FAILED;
    h->result = r;
    return r;
}
static km_result_t found(km_selector_home_t *h, uint32_t now)
{
    h->found_position = km_trajectory_get_position(h->trajectory);
    km_trajectory_emergency_stop(h->trajectory);
    km_result_t r = km_trajectory_recover(h->trajectory, 0, now);
    if (r != KM_OK)
        return fail(h, r);
    h->position_known = true;
    h->state = KM_SELECTOR_HOME_HOMED;
    h->result = KM_OK;
    return KM_OK;
}
km_result_t km_selector_home_start(km_selector_home_t *h, km_trajectory_t *t, km_driver_t *d,
                                   const km_selector_home_config_t *c, km_selector_home_switch_t s,
                                   uint32_t now)
{
    if (!h || !t || !d || !c)
        return KM_ERR_INVALID_PARAM;
    if (h->state == KM_SELECTOR_HOME_RUNNING)
        return KM_ERR_BUSY;
    if (h->state == KM_SELECTOR_HOME_FAILED)
        return KM_ERR_FAULTED;
    if (!c->speed_steps_s || c->speed_steps_s > KM_MOTION_MAX_VELOCITY_STEPS_S ||
        !c->acceleration_steps_s2 || c->acceleration_steps_s2 > KM_MOTION_MAX_ACCEL_STEPS_S2 ||
        !c->deceleration_steps_s2 || c->deceleration_steps_s2 > KM_MOTION_MAX_ACCEL_STEPS_S2 ||
        !c->max_travel_steps || !c->deadline_us || c->deadline_us >= KM_TIME_WRAP_HORIZON_US ||
        !c->max_switch_age_us || c->max_switch_age_us >= KM_TIME_WRAP_HORIZON_US ||
        (c->home_direction != KM_DIR_FORWARD && c->home_direction != KM_DIR_REVERSE))
        return KM_ERR_INVALID_PARAM;
    if (km_trajectory_is_moving(t) || d->enabled || d->step_pin_high)
        return KM_ERR_BUSY;
    if (!d->initialized || !d->ops || !d->ops->stop_outputs || !d->ops->set_enable ||
        !d->ops->set_direction || !d->caps.supports_step_dir)
        return KM_ERR_UNAVAILABLE;
    if (km_driver_get_fault(d) != KM_FAULT_NONE)
        return KM_ERR_FAULTED;
    memset(h, 0, sizeof(*h));
    h->trajectory = t;
    h->driver = d;
    h->config = *c;
    h->started_at_us = now;
    h->last_sample_us = s.sampled_at_us;
    h->state = KM_SELECTOR_HOME_RUNNING;
    h->result = KM_ERR_BUSY;
    if (!fresh(s, now, c->max_switch_age_us))
        return fail(h, KM_ERR_STALE);
    if (s.asserted)
        return fail(h, KM_ERR_FAULTED);
    /* t may already be tracking a real absolute position that a caller
     * declared with its own km_trajectory_recover just before this call
     * (operation_core.c's enter_select_home_ex, re-baselining a trajectory a
     * driver switch left elsewhere). Plain km_trajectory_init resets position
     * to 0 and clears position_offset_pending, so the next km_trajectory_observe
     * would reseed current_position_steps from the driver's own raw, never-
     * rebased step accumulator instead of that baseline -- on a forward-only
     * selector, whose raw accumulator only ever grows, that eventually reads
     * as runaway travel and trips max_travel_steps for no real motion at all.
     * Capture the position and reset_seq first and recover them after, so
     * this search starts exactly where the caller left it. */
    int64_t position = km_trajectory_get_position(t);
    uint32_t reset_seq = t->reset_seq;
    km_result_t r =
        km_trajectory_init(t, c->speed_steps_s, c->acceleration_steps_s2, c->deceleration_steps_s2);
    if (r != KM_OK)
        return fail(h, r);
    t->reset_seq = reset_seq;
    r = km_trajectory_recover(t, position, now);
    if (r != KM_OK)
        return fail(h, r);
    km_trajectory_bind_driver(t, d);
    h->starting_position = km_trajectory_get_position(t);
    r = km_driver_set_enable(d, true);
    if (r != KM_OK)
        return fail(h, r);
    int32_t speed = (int32_t)c->speed_steps_s;
    if (c->home_direction == KM_DIR_REVERSE)
        speed = -speed;
    r = km_trajectory_set_target_velocity(t, speed, now);
    if (r != KM_OK)
        return fail(h, r);
    return KM_OK;
}
km_result_t km_selector_home_service(km_selector_home_t *h, km_selector_home_switch_t s,
                                     uint32_t now)
{
    if (!h || h->state != KM_SELECTOR_HOME_RUNNING)
        return KM_ERR_UNAVAILABLE;
    if (km_time_before(now, h->started_at_us) ||
        km_time_elapsed(now, h->started_at_us) >= h->config.deadline_us)
        return fail(h, KM_ERR_TIMEOUT);
    if (!fresh(s, now, h->config.max_switch_age_us) ||
        km_time_before(s.sampled_at_us, h->last_sample_us))
        return fail(h, KM_ERR_STALE);
    /* A cached inactive sample cannot later become proof of a home edge. */
    if (s.asserted && s.sampled_at_us == h->last_sample_us)
        return fail(h, KM_ERR_STALE);
    h->last_sample_us = s.sampled_at_us;
    if (km_driver_get_fault(h->driver) != KM_FAULT_NONE || !h->driver->enabled ||
        h->trajectory->faulted || h->trajectory->position_uncertain)
        return fail(h, KM_ERR_FAULTED);
    if (s.asserted &&
        km_trajectory_get_position(h->trajectory) == h->starting_position)
        return fail(h, KM_ERR_FAULTED);
    if (s.asserted)
        return found(h, now);
    if (llabs(km_trajectory_get_position(h->trajectory) - h->starting_position) >= h->config.max_travel_steps)
        return fail(h, KM_ERR_FAULTED);
    km_trajectory_update(h->trajectory, now);
    if (h->trajectory->faulted)
        return fail(h, KM_ERR_FAULTED);
    if (llabs(km_trajectory_get_position(h->trajectory) - h->starting_position) >= h->config.max_travel_steps)
        return fail(h, KM_ERR_FAULTED);
    return KM_OK;
}
km_result_t km_selector_home_cancel(km_selector_home_t *h)
{
    if (!h || h->state != KM_SELECTOR_HOME_RUNNING)
        return KM_ERR_UNAVAILABLE;
    return fail(h, KM_ERR_CONFLICT);
}
