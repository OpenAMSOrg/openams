// SPDX-License-Identifier: GPL-3.0-or-later
#include "klipper_mmu/trajectory.h"
#include <string.h>

#define CONTROL_INTERVAL_US 1000000u

static bool valid_limits(uint32_t velocity, uint32_t acceleration, uint32_t braking)
{
    return velocity && velocity <= KM_MOTION_MAX_VELOCITY_STEPS_S
        && acceleration && acceleration <= KM_MOTION_MAX_ACCEL_STEPS_S2
        && braking && braking <= KM_MOTION_MAX_ACCEL_STEPS_S2;
}

static bool valid_position(int64_t position)
{
    return position >= KM_MOTION_MIN_POSITION_STEPS
        && position <= KM_MOTION_MAX_POSITION_STEPS;
}

static km_result_t driver_ready(const km_driver_t *driver)
{
    if (!driver) return KM_OK; /* Explicit driverless model, never physical output. */
    if (!driver->initialized) return KM_ERR_UNAVAILABLE;
    if (km_driver_get_fault(driver) != KM_FAULT_NONE) return KM_ERR_FAULTED;
    if (!driver->enabled) return KM_ERR_UNAVAILABLE;
    if (!driver->ops || !driver->ops->set_direction
        || !driver->ops->stop_outputs || !driver->caps.supports_step_dir)
        return KM_ERR_NOT_SUPPORTED;
    if (!driver->caps.max_velocity_steps_s || !driver->caps.min_pulse_width_us
        || driver->caps.min_step_interval_us <= driver->caps.min_pulse_width_us)
        return KM_ERR_INVALID_PARAM;
    return KM_OK;
}

static uint32_t km_gcd_u32(uint32_t a, uint32_t b)
{
    while (b) {
        uint32_t r = a % b;
        a = b;
        b = r;
    }
    return a;
}

/* Scale `value` by num/den, rounding to the nearest integer (ties away from
 * zero), symmetric for negative values. num == den is the identity: no
 * multiply, no rounding, bit-for-bit the value passed in -- every trajectory
 * that never calls km_trajectory_set_lane_scale takes this path forever. */
static int64_t scale_round(int64_t value, uint32_t num, uint32_t den)
{
    if (num == den) return value;
    bool negative = value < 0;
    uint64_t magnitude = negative ? (uint64_t)(-value) : (uint64_t)value;
    uint64_t scaled = magnitude * (uint64_t)num;
    uint64_t rounded = (scaled + den / 2u) / den;
    return negative ? -(int64_t)rounded : (int64_t)rounded;
}

/* reference -> physical (km_trajectory_t's scale comment). */
static int64_t to_physical(const km_trajectory_t *t, int64_t reference_value)
{
    return scale_round(reference_value, t->scale_num, t->scale_den);
}

/* physical -> reference: the exact inverse conversion. */
static int64_t to_reference(const km_trajectory_t *t, int64_t physical_value)
{
    return scale_round(physical_value, t->scale_den, t->scale_num);
}

static uint32_t effective_limit(const km_trajectory_t *t, uint32_t requested)
{
    uint32_t limit = t->max_velocity_steps_s;
    if (requested && requested < limit) limit = requested;
    if (t->driver) {
        const km_driver_caps_t *caps = &t->driver->caps;
        if (caps->max_velocity_steps_s < limit) limit = caps->max_velocity_steps_s;
        if (caps->min_step_interval_us) {
            uint32_t electrical = 1000000u / caps->min_step_interval_us;
            if (electrical < limit) limit = electrical;
        }
    }
    return limit;
}

/* Microsteps the phase has passed beyond model_position_steps, the microstep
 * the driver is at once due pulses complete. The motor reaches microstep k when
 * the phase reaches k exactly, in either direction, so a reversal inside a
 * microstep emits nothing and a sub-step overshoot is kept, not pinned. */
static int64_t steps_due(const km_trajectory_t *t)
{
    int64_t up = t->rate.phase_whole - t->model_position_steps;
    int64_t down = t->model_position_steps - t->rate.phase_whole - (t->rate.phase_frac != 0);
    return up > 0 ? up : (down > 0 ? -down : 0);
}

static void refresh_mirrors(km_trajectory_t *t)
{
    /* A positional move ends when the rate core consumes its phase target:
     * the core clears its own target, so rest it before the leftover cruise
     * rate keeps the edge generator running past the target. */
    int64_t sps = t->rate.rate * 1000000; /* Q32 steps/us -> Q32 steps/s */
    int64_t mag = ((sps < 0 ? -sps : sps) + 2147483648LL) >> 32;
    t->current_velocity_steps_s = (int32_t)(sps < 0 ? -mag : mag);
    if (!t->driver) { /* driverless model: its position is the model's */
        t->model_position_steps += steps_due(t);
        t->current_position_steps = t->model_position_steps;
    }
    t->is_active = (t->rate.rate != 0 || t->rate.accel != 0 || t->rate.target_rate_internal != 0 || t->rate.target_phase_active);
}

static void rate_advance_span(km_rate_t *r, uint32_t elapsed)
{
    while (elapsed > 0) {
        uint32_t step = elapsed > KM_RATE_MAX_UPDATE_US ? KM_RATE_MAX_UPDATE_US : elapsed;
        km_rate_advance(r, step);
        elapsed -= step;
    }
}

static void advance_to(km_trajectory_t *t, uint32_t now)
{
    rate_advance_span(&t->rate, (uint32_t)(now - t->last_update_us));
    t->last_update_us = now;
    refresh_mirrors(t);
}

/* The rate state is not at rest or carries a pending target. */


/* An accepted command is serviced at once and is visible to callers at once. */
static void accept_command(km_trajectory_t *t, uint32_t now)
{
    (void)now;
    t->command_seq++;
    refresh_mirrors(t);
}

static km_result_t check_command(km_trajectory_t *t, uint32_t now)
{
    if (!t || t->faulted) return t ? KM_ERR_FAULTED : KM_ERR_INVALID_PARAM;
    km_result_t ready = driver_ready(t->driver);
    if (ready != KM_OK) {
        if (km_trajectory_is_moving(t)) km_trajectory_emergency_stop(t);
        return ready;
    }
    if (t->clock_valid && km_time_before(now, t->last_update_us)) return KM_ERR_INVALID_PARAM;
    return KM_OK;
}

km_result_t km_trajectory_init(km_trajectory_t *t, uint32_t max_velocity,
                               uint32_t acceleration, uint32_t braking)
{
    if (!t || !valid_limits(max_velocity, acceleration, braking)) return KM_ERR_INVALID_PARAM;
    memset(t, 0, sizeof(*t));
    km_rate_init(&t->rate);
    t->scale_num = t->scale_den = 1u;
    t->max_velocity_steps_s = max_velocity;
    t->acceleration_steps_s2 = acceleration;
    t->deceleration_steps_s2 = braking;
    t->jerk_steps_s3 = 20 * (acceleration < braking ? acceleration : braking);
    if (t->jerk_steps_s3 > KM_MOTION_MAX_JERK_STEPS_S3) t->jerk_steps_s3 = KM_MOTION_MAX_JERK_STEPS_S3;
    km_rate_set_limits(&t->rate, max_velocity, acceleration, braking, t->jerk_steps_s3);
    return KM_OK;
}

km_result_t km_trajectory_set_jerk(km_trajectory_t *t, uint32_t jerk_steps_s3)
{
    if (!t || jerk_steps_s3 == 0) return KM_ERR_INVALID_PARAM;
    /* Reference units in, like set_limits_at. */
    int64_t jerk = to_physical(t, (int64_t)jerk_steps_s3);
    if (jerk <= 0 || jerk > (int64_t)KM_MOTION_MAX_JERK_STEPS_S3) return KM_ERR_INVALID_PARAM;
    if (t->faulted) return KM_ERR_FAULTED;
    t->jerk_steps_s3 = (uint32_t)jerk;
    km_rate_set_limits(&t->rate, t->max_velocity_steps_s, t->acceleration_steps_s2, t->deceleration_steps_s2, t->jerk_steps_s3);
    t->command_seq++; /* published to the edge copy like any command */
    return KM_OK;
}

void km_trajectory_bind_driver(km_trajectory_t *t, km_driver_t *driver)
{
    if (!t) return;
    if (t->driver != driver && km_trajectory_is_moving(t)) {
        km_trajectory_emergency_stop(t);
        return;
    }
    t->driver = driver;
}

KM_API km_result_t km_trajectory_set_lane_scale(km_trajectory_t *t, uint32_t scale_num, uint32_t scale_den)
{
    if (!t || !scale_num || !scale_den) return KM_ERR_INVALID_PARAM;
    uint32_t g = km_gcd_u32(scale_num, scale_den);
    scale_num /= g;
    scale_den /= g;
    uint32_t old_num = t->scale_num, old_den = t->scale_den;
    /* Unchanged is a no-op even while moving: an UNLOAD re-engages the lane
     * that is still following. */
    if (scale_num == old_num && scale_den == old_den) return KM_OK;
    if (km_trajectory_is_moving(t)) return KM_ERR_BUSY;

    /* Every physical field with boundary meaning is rebased: reference under
     * the OLD scale, then physical under the NEW one -- the same conversion
     * get_position()/set_target_position() apply to a caller's argument,
     * just applied here to the trajectory's own state. At rest (checked
     * above) phase_whole, model_position_steps and current_position_steps
     * already agree and phase_frac is 0, so rebasing the one physical value
     * is exact for all three. */
#define KM_RESCALE(field) \
    t->field = scale_round(scale_round((t->field), old_den, old_num), scale_num, scale_den)
    KM_RESCALE(current_position_steps);
    KM_RESCALE(model_position_steps);
    t->rate.phase_whole = scale_round(scale_round(t->rate.phase_whole, old_den, old_num),
                                       scale_num, scale_den);
#undef KM_RESCALE
    int64_t max_v = scale_round(scale_round((int64_t)t->max_velocity_steps_s, old_den, old_num),
                                 scale_num, scale_den);
    int64_t amax = scale_round(scale_round((int64_t)t->acceleration_steps_s2, old_den, old_num),
                                scale_num, scale_den);
    int64_t dmax = scale_round(scale_round((int64_t)t->deceleration_steps_s2, old_den, old_num),
                                scale_num, scale_den);
    int64_t jerk = scale_round(scale_round((int64_t)t->jerk_steps_s3, old_den, old_num),
                                scale_num, scale_den);
    if (max_v <= 0 || max_v > (int64_t)KM_MOTION_MAX_VELOCITY_STEPS_S
        || amax <= 0 || amax > (int64_t)KM_MOTION_MAX_ACCEL_STEPS_S2
        || dmax <= 0 || dmax > (int64_t)KM_MOTION_MAX_ACCEL_STEPS_S2
        || jerk <= 0 || jerk > (int64_t)KM_MOTION_MAX_JERK_STEPS_S3)
        return KM_ERR_INVALID_PARAM; /* Out of the motion core's range under the new scale. */

    t->scale_num = scale_num;
    t->scale_den = scale_den;
    t->max_velocity_steps_s = (uint32_t)max_v;
    t->acceleration_steps_s2 = (uint32_t)amax;
    t->deceleration_steps_s2 = (uint32_t)dmax;
    t->jerk_steps_s3 = (uint32_t)jerk;
    km_rate_set_limits(&t->rate, t->max_velocity_steps_s, t->acceleration_steps_s2,
                       t->deceleration_steps_s2, t->jerk_steps_s3);
    return KM_OK;
}

km_result_t km_trajectory_set_limits(km_trajectory_t *t, uint32_t maximum,
                                     uint32_t acceleration, uint32_t braking)
{
    return km_trajectory_set_limits_at(t, maximum, acceleration, braking,
                                        t ? t->last_update_us : 0);
}

km_result_t km_trajectory_set_limits_at(km_trajectory_t *t, uint32_t maximum,
                                        uint32_t acceleration, uint32_t braking,
                                        uint32_t now)
{
    if (!t || !valid_limits(maximum, acceleration, braking)) return KM_ERR_INVALID_PARAM;
    if (t->faulted) return KM_ERR_FAULTED;
    km_result_t result = check_command(t, now);
    if (result != KM_OK) return result;
    
    if (!t->clock_valid || (t->rate.rate == 0 && t->rate.accel == 0)) {
        t->last_update_us = now;
        t->clock_valid = true;
    } else {
        advance_to(t, now);
    }

    /* maximum/acceleration/braking arrive in reference units (the caller's
     * profile values, unchanged); convert to this trajectory's physical
     * domain before applying them, and refuse a scale that pushes the
     * physical result outside the motion core's own range. */
    int64_t phys_max = to_physical(t, (int64_t)maximum);
    int64_t phys_accel = to_physical(t, (int64_t)acceleration);
    int64_t phys_braking = to_physical(t, (int64_t)braking);
    if (phys_max <= 0 || phys_max > (int64_t)KM_MOTION_MAX_VELOCITY_STEPS_S
        || phys_accel <= 0 || phys_accel > (int64_t)KM_MOTION_MAX_ACCEL_STEPS_S2
        || phys_braking <= 0 || phys_braking > (int64_t)KM_MOTION_MAX_ACCEL_STEPS_S2)
        return KM_ERR_INVALID_PARAM;

    t->max_velocity_steps_s = (uint32_t)phys_max;
    t->acceleration_steps_s2 = (uint32_t)phys_accel;
    t->deceleration_steps_s2 = (uint32_t)phys_braking;
    km_rate_set_limits(&t->rate, t->max_velocity_steps_s, t->acceleration_steps_s2,
                       t->deceleration_steps_s2, t->jerk_steps_s3);

    int64_t eff = (int64_t)effective_limit(t, t->has_target_position ? t->move_velocity_limit : 0);
    int64_t req = (int64_t)t->target_velocity_steps_s;
    if (req > eff) req = eff;
    if (req < -eff) req = -eff;
    if (t->has_target_position) {
        km_rate_set_target_rate(&t->rate, (int32_t)eff);
    } else {
        /* The clamped request is the live target: keep the mirror in step with
         * the rate core, or a lowered limit stays invisible to callers. */
        t->target_velocity_steps_s = (int32_t)req;
        km_rate_set_target_rate(&t->rate, (int32_t)req);
    }
    
    accept_command(t, now);
    return KM_OK;
}

km_result_t km_trajectory_set_target_velocity(km_trajectory_t *t, int32_t target, uint32_t now)
{
    km_result_t result = check_command(t, now);
    if (result != KM_OK) return result;
    int64_t limit = (int64_t)effective_limit(t, 0);
    if (!limit) return KM_ERR_INVALID_PARAM;
    /* target arrives in reference units; the rate core and every clamp
     * below work in this trajectory's physical domain. */
    int64_t requested = to_physical(t, (int64_t)target);
    if (requested > limit) requested = limit;
    if (requested < -limit) requested = -limit;
    
    if (!t->clock_valid || (t->rate.rate == 0 && t->rate.accel == 0)) {
        t->last_update_us = now;
        t->clock_valid = true;
    } else {
        advance_to(t, now);
    }
    
    /* A velocity command supersedes any positional move: the rate core must
     * drop its phase target too, or its stale target keeps the trajectory
     * "active" after the stop and no terminal can follow the brake. */
    km_rate_stop(&t->rate, false);
    km_rate_set_target_rate(&t->rate, (int32_t)requested);
    t->target_velocity_steps_s = (int32_t)requested;
    t->has_target_position = false;
    
    accept_command(t, now);
    return KM_OK;
}

km_result_t km_trajectory_set_target_position(km_trajectory_t *t, int64_t target,
                                              uint32_t maximum, uint32_t now)
{
    if (!valid_position(target) || !maximum || maximum > KM_MOTION_MAX_VELOCITY_STEPS_S)
        return KM_ERR_INVALID_PARAM;
    km_result_t result = check_command(t, now);
    if (result != KM_OK) return result;

    /* target and maximum arrive in reference units; the rate core works in
     * this trajectory's physical domain. */
    int64_t phys_target = to_physical(t, target);
    uint32_t phys_maximum = (uint32_t)to_physical(t, (int64_t)maximum);
    uint32_t limit = effective_limit(t, phys_maximum);
    if (!limit) return KM_ERR_INVALID_PARAM;

    if (!t->clock_valid || (t->rate.rate == 0 && t->rate.accel == 0)) {
        t->last_update_us = now;
        t->clock_valid = true;
    } else {
        /* The acceptance test needs the state at now, but a rejected target
         * must leave the trajectory untouched: try it on a copy first, so a
         * refusal commits neither the target nor the elapsed integration. */
        km_rate_t probe = t->rate;
        rate_advance_span(&probe, (uint32_t)(now - t->last_update_us));
        if (km_rate_set_target_phase(&probe, phys_target) != 0) return KM_ERR_INVALID_PARAM;
        advance_to(t, now);
    }

    /* Validate the target before touching the rate target: a rejected target
     * leaves the state unchanged (km_rate_set_target_phase is a no-op then). */
    if (km_rate_set_target_phase(&t->rate, phys_target) != 0) {
        return KM_ERR_INVALID_PARAM;
    }
    km_rate_set_target_rate(&t->rate, (int32_t)limit);

    t->move_velocity_limit = phys_maximum;
    t->target_position_steps = phys_target;
    t->has_target_position = true;
    t->target_velocity_steps_s = 0;
    
    accept_command(t, now);
    return KM_OK;
}

km_result_t km_trajectory_cancel(km_trajectory_t *t, uint32_t now)
{
    km_result_t result = check_command(t, now);
    if (result != KM_OK) return result;
    
    if (!t->clock_valid || (t->rate.rate == 0 && t->rate.accel == 0)) {
        t->last_update_us = now;
        t->clock_valid = true;
    } else {
        advance_to(t, now);
    }
    
    km_rate_stop(&t->rate, false);
    t->target_velocity_steps_s = 0;
    t->has_target_position = false;
    
    accept_command(t, now);
    return KM_OK;
}

void km_trajectory_emergency_stop(km_trajectory_t *t)
{
    if (!t) return;
    t->position_uncertain = true;
    t->faulted = true;
    km_rate_stop(&t->rate, true);
    t->current_velocity_steps_s = 0;
    t->target_velocity_steps_s = 0;
    t->is_active = false;
    t->has_target_position = false;
    t->stepper_active = false;
    if (t->driver) km_driver_shutdown(t->driver);
}

km_result_t km_trajectory_recover(km_trajectory_t *t, int64_t position, uint32_t now)
{
    if (!t || !valid_position(position) || km_trajectory_is_moving(t)) return KM_ERR_INVALID_PARAM;
    if (t->driver && (t->driver->enabled || km_driver_get_fault(t->driver) != KM_FAULT_NONE))
        return KM_ERR_UNAVAILABLE;
    km_driver_t *driver = t->driver;
    uint32_t jerk = t->jerk_steps_s3;
    uint32_t max_v = t->max_velocity_steps_s;
    uint32_t amax = t->acceleration_steps_s2;
    uint32_t dmax = t->deceleration_steps_s2;
    uint32_t scale_num = t->scale_num, scale_den = t->scale_den;

    uint32_t reset_seq = t->reset_seq;
    km_result_t result = km_trajectory_init(t, max_v, amax, dmax);
    if (result != KM_OK) return result;
    t->reset_seq = reset_seq + 1u;

    km_trajectory_set_jerk(t, jerk);
    t->driver = driver;
    /* km_trajectory_init just reset the scale to 1:1; restore this lane's
     * own scale before converting `position` (a caller-visible reference
     * distance, km_trajectory_t's scale comment) to physical steps. */
    t->scale_num = scale_num;
    t->scale_den = scale_den;
    int64_t physical = to_physical(t, position);
    t->rate.phase_whole = physical;
    t->model_position_steps = physical;
    refresh_mirrors(t);
    t->current_position_steps = physical;
    /* The real offset cannot be computed here: this may follow
     * km_trajectory_bind_driver switching to a different physical driver
     * (select_bind_stepper), whose own raw count bears no relation to
     * whatever current_position_steps held for the *previous* one.
     * km_trajectory_observe seeds it from the first raw count it actually
     * sees for the now-current driver, against the position declared
     * above. */
    t->position_offset_pending = true;
    t->clock_valid = true;
    t->last_update_us = now;
    return KM_OK;
}

uint32_t km_trajectory_update(km_trajectory_t *t, uint32_t now)
{
    if (!t || t->faulted) return 0;
    if (!t->clock_valid) { t->clock_valid = true; t->last_update_us = now; return 0; }
    if (km_time_before_or_eq(now, t->last_update_us)) return 0;
    if (t->driver) return 0;

    int64_t previous = t->model_position_steps;
    advance_to(t, now);
    int64_t difference = t->model_position_steps - previous;
    return (uint32_t)(difference < 0 ? -difference : difference);
}

KM_API int64_t km_trajectory_get_position(const km_trajectory_t *t)
{
    return t ? to_reference(t, t->current_position_steps) : 0;
}

KM_API int64_t km_trajectory_get_model_position(const km_trajectory_t *t)
{
    return t ? to_reference(t, t->model_position_steps) : 0;
}

KM_API int32_t km_trajectory_get_velocity(const km_trajectory_t *t)
{
    if (!t) return 0;
    return (int32_t)to_reference(t, (int64_t)t->current_velocity_steps_s);
}

KM_API int64_t km_trajectory_get_stop_distance(const km_trajectory_t *t)
{
    if (!t) return 0;
    int64_t physical = km_rate_stop_distance_q32(&t->rate) >> 32;
    return to_reference(t, physical);
}

KM_API km_result_t km_trajectory_window(const km_trajectory_t *traj, uint32_t tick_us,
                                        uint32_t ticks_per_us,
                                        uint32_t min_interval_ticks,
                                        bool anchor_restart,
                                        km_trajectory_window_t *out)
{
    if (!traj || !out) return KM_ERR_INVALID_PARAM;
    out->rate = traj->rate;
    out->microstep = traj->model_position_steps;
    out->anchor_ticks = traj->anchor_ticks;
    out->count = 0;
    
    if (traj->faulted || !traj->driver) {
        return KM_OK;
    }
    
    km_rate_advance(&out->rate, tick_us);
    
    int result = km_step_segments_generate(&traj->rate, &out->rate, tick_us, ticks_per_us,
                                           min_interval_ticks, anchor_restart,
                                           &out->microstep, &out->anchor_ticks,
                                           out->segments, &out->count, KM_STEP_SEGMENTS_MAX);
    if (result != KM_STEP_SEGMENTS_OK) {
        return KM_ERR_FAULTED;
    }
    if (traj->invert_direction) {
        for (uint8_t i = 0; i < out->count; ++i)
            out->segments[i].dir = (uint8_t)(out->segments[i].dir ? 0 : 1);
    }
    return KM_OK;
}

KM_API void km_trajectory_commit(km_trajectory_t *traj, const km_trajectory_window_t *w)
{
    if (!traj || !w) return;
    traj->rate = w->rate;
    traj->model_position_steps = w->microstep;
    traj->anchor_ticks = w->anchor_ticks;
    if (w->count > 0) {
        traj->stepper_active = true;
    }
    /* The window clock advances the core, so wall time no longer tracks it: a
     * command before the next window must not integrate the span between them
     * on top of the tick the core already took. */
    traj->clock_valid = false;
    refresh_mirrors(traj);
    traj->tick_seq++;
}

KM_API void km_trajectory_observe(km_trajectory_t *traj, int64_t completed_steps,
                                  bool stepper_active)
{
    if (!traj) return;
    /* An inverted DIR (km_trajectory_window) makes the driver count the other
     * way; map it back so positions keep the model's sign. */
    if (traj->invert_direction) completed_steps = -completed_steps;
    /* A pending recover declared current_position_steps as an absolute
     * baseline but could not know this driver's own raw count yet (it may
     * be a different driver than last time): seed the offset from the
     * first raw count observed since, against that declared baseline,
     * exactly once. */
    if (traj->position_offset_pending) {
        traj->position_offset = traj->current_position_steps - completed_steps;
        traj->position_offset_pending = false;
    }
    /* completed_steps is the driver's own raw accumulator, never rebased by
     * km_trajectory_recover; position_offset carries whatever baseline
     * recover last declared so this stays in that same frame. */
    int64_t adjusted = completed_steps + traj->position_offset;
    traj->current_position_steps = adjusted;
    traj->stepper_active = stepper_active;
    if (traj->has_target_position && !traj->rate.target_phase_active &&
        adjusted == traj->target_position_steps && !stepper_active) {
        traj->has_target_position = false;
        traj->is_active = false;
    }
}

KM_API void km_trajectory_resync(km_trajectory_t *traj, int64_t completed_steps,
                                 bool uncertain)
{
    if (!traj) return;
    if (traj->invert_direction) completed_steps = -completed_steps; /* As in observe. */
    /* Recover from any stop: hold at the completed count, drop every target,
     * and make the outputs usable again so the next command is accepted.
     * A fault re-grounds trust in the driver's own raw count, so any
     * baseline km_trajectory_recover had declared no longer applies. */
    traj->position_offset = 0;
    traj->position_offset_pending = false;
    traj->model_position_steps = completed_steps;
    traj->current_position_steps = completed_steps;
    traj->rate.phase_whole = completed_steps;
    traj->rate.phase_frac = 0;
    km_rate_stop(&traj->rate, true);
    traj->target_velocity_steps_s = 0;
    traj->has_target_position = false;
    traj->stepper_active = false;
    traj->position_uncertain = uncertain;
    refresh_mirrors(traj);
}

KM_API bool km_trajectory_publish(km_trajectory_t *edge, const km_trajectory_t *plan,
                                  uint32_t now)
{
    (void)now;
    if (plan->command_seq == edge->command_seq) return false;
    /* A rebound driver (selector vs. gear) or a plan reset by
     * km_trajectory_recover outside this exchange (selector homing's found())
     * replaces the edge's live phase, rate and positions with the plan's;
     * otherwise the edge keeps them, as it always has. */
    bool driver_changed = edge->driver != plan->driver || edge->reset_seq != plan->reset_seq;
    edge->reset_seq = plan->reset_seq;
    edge->driver = plan->driver;
    edge->invert_direction = plan->invert_direction;
    edge->scale_num = plan->scale_num;
    edge->scale_den = plan->scale_den;
    km_rate_t state = edge->rate;
    edge->rate = plan->rate;
    if (!driver_changed) {
        edge->rate.phase_whole = state.phase_whole;
        edge->rate.phase_frac = state.phase_frac;
        edge->rate.rate = state.rate;
        edge->rate.accel = state.accel;
    }
    edge->rate.braking = false;
    if (driver_changed) {
        edge->current_position_steps = plan->current_position_steps;
        edge->model_position_steps = plan->model_position_steps;
        edge->stepper_active = plan->stepper_active;
        edge->position_offset = plan->position_offset;
        edge->position_offset_pending = plan->position_offset_pending;
    }
    edge->target_velocity_steps_s = plan->target_velocity_steps_s;
    edge->has_target_position = plan->has_target_position;
    edge->target_position_steps = plan->target_position_steps;
    edge->move_velocity_limit = plan->move_velocity_limit;
    edge->max_velocity_steps_s = plan->max_velocity_steps_s;
    edge->acceleration_steps_s2 = plan->acceleration_steps_s2;
    edge->deceleration_steps_s2 = plan->deceleration_steps_s2;
    edge->jerk_steps_s3 = plan->jerk_steps_s3;
    edge->command_seq = plan->command_seq;
    if (edge->has_target_position
        && km_rate_set_target_phase(&edge->rate, edge->target_position_steps) != 0) {
        km_trajectory_emergency_stop(edge);
        return true;
    }
    return true;
}

KM_API void km_trajectory_collect(km_trajectory_t *plan, const km_trajectory_t *edge)
{
    plan->rate.phase_whole = edge->rate.phase_whole;
    plan->rate.phase_frac = edge->rate.phase_frac;
    plan->rate.rate = edge->rate.rate;
    plan->rate.accel = edge->rate.accel;
    plan->rate.target_phase_active = edge->rate.target_phase_active;
    plan->rate.braking = edge->rate.braking;
    
    plan->current_position_steps = edge->current_position_steps;
    plan->stepper_active = edge->stepper_active;
    plan->tick_seq = edge->tick_seq;
    plan->faulted = edge->faulted;
    plan->position_uncertain = edge->position_uncertain;
    /* km_trajectory_resync (a fault, edge-only) clears this back to 0;
     * without mirroring it here too, plan's own copy of the baseline a
     * prior km_trajectory_recover declared would outlive the reset that
     * made it meaningless. */
    plan->position_offset = edge->position_offset;
    plan->position_offset_pending = edge->position_offset_pending;
    /* km_trajectory_observe (edge-only) clears has_target_position on
     * arrival; without mirroring that back here, plan's own copy -- set
     * once by km_trajectory_set_target_position/cancel and never touched
     * again on this side -- stays true forever after the move actually
     * finishes. km_trajectory_set_limits_at (called by every fresh
     * selection, src/operation_core.c resume_operation_motion) reads this
     * stale flag to decide whether to re-arm rate.target_rate_internal to
     * the velocity limit; its own accept_command then recomputes is_active
     * from that freshly nonzero rate, making an already-arrived, fully
     * idle trajectory look like it is still moving to the very next
     * km_trajectory_bind_driver (selector -> gear), which then
     * emergency-stops it and reports a misleading driver fault instead of
     * rebinding cleanly. */
    plan->has_target_position = edge->has_target_position;
    
    plan->current_velocity_steps_s = edge->current_velocity_steps_s;
    plan->model_position_steps = edge->model_position_steps;
    plan->is_active = edge->is_active;
}
