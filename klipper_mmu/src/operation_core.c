// SPDX-License-Identifier: GPL-3.0-or-later
#include "klipper_mmu/operation_core.h"
#include <string.h>

/* Whether this image drives any selector actuator at all (klipper_mmu/
 * selector.h: LINEAR/ROTARY/INDEXED use a selector stepper, SERVO uses a
 * servo instead). mcu/Makefile passes 0 for a KLIPPER_MMU_SELECTOR_KIND_NONE
 * image (independent per-lane feeders, no shared selector) so its state
 * machine does not carry the selection phase's code -- flash on the
 * smallest targets (e.g. G0B0) has no room for a phase that image can never
 * enter. This file stays platform-independent (no autoconf.h): native and
 * test builds that never define it get the full selector-capable build,
 * matching every existing test. */
#ifndef KM_SELECTOR_BUILD
#define KM_SELECTOR_BUILD 1
#endif

static bool fresh(bool valid, uint32_t sample, uint32_t now, uint32_t maximum)
{
    return valid && !km_time_before(now, sample) && km_time_elapsed(now, sample) <= maximum;
}

static uint8_t virtual_hub_lane(const km_operation_core_t *c)
{
    if (c->path_occupied) return c->path_occupant_lane;
    if (c->shared_path_owned) return c->active_lane;
    /* The legacy one-lane profile has an unambiguous projection. A multi-lane
     * idle profile has no shared hub reading until one lane owns the path. */
    return c->profile.lane_count == 1 ? 0 : KM_LANE_UNKNOWN;
}

/* Diagnostic-only accepted-edge counting (docs/diagnostics.md). A lane's own
 * feeder is "moving" only while this MMU actively owns and steps it; any
 * other observed change is physically implausible without a person or a
 * mechanical fault. */
static bool lane_feeder_moving(const km_operation_core_t *c, uint8_t lane)
{
    return c->shared_path_owned && c->active_lane == lane
        && km_trajectory_is_moving(&c->trajectory);
}

static void bump_idle_edge(uint16_t *counter)
{
    if (*counter < UINT16_MAX) ++*counter;
}

static void project_virtual_hub(km_operation_core_t *c)
{
    if (!c->profile.load_is_virtual_hub) return;
    uint8_t lane = virtual_hub_lane(c);
    c->hub_sensor_valid = lane < c->profile.lane_count && c->load_valid[lane];
    c->filament_present_hub = c->hub_sensor_valid && c->filament_present_load[lane];
}

/* Every other exit switch must be freshly clear before motion on a virtual
 * hub. A stale or invalid reading cannot prove that another lane is empty. */
static bool virtual_other_lanes_clear(const km_operation_core_t *c, uint8_t lane,
                                      uint32_t now)
{
    if (!c->profile.load_is_virtual_hub) return true;
    for (uint8_t i = 0; i < c->profile.lane_count; ++i) {
        if (i == lane) continue;
        if (!fresh(c->load_valid[i], c->load_sample_ms[i], now,
                   c->profile.max_sensor_age_ms) || c->filament_present_load[i]) return false;
    }
    return true;
}

static km_result_t init_follower(km_operation_core_t *c)
{
    uint32_t maximum = c->profile.default_feed_velocity_steps_s;
    uint32_t initial = maximum > 1 ? maximum / 2u : 1u;
    km_result_t result = km_follower_init(&c->follower, initial, 1, maximum,
        c->profile.default_run_current_ma, c->profile.default_hold_current_ma,
        c->profile.default_run_current_ma);
    if (result != KM_OK) return result;
    result = km_follower_set_feedback(&c->follower, c->profile.has_proportional_buffer,
                                      c->profile.proportional_gain_milli,
                                      c->profile.buffer_deadband_permille);
    if (result != KM_OK || !c->profile.has_encoder) return result;
    return km_follower_set_encoder_detection(&c->follower,
        c->profile.encoder_detection_length_steps);
}

km_result_t km_core_init(km_operation_core_t *c, const km_device_profile_t *p)
{
    if (!c || km_profile_validate(p) != KM_OK) return KM_ERR_INVALID_PARAM;
    km_device_profile_t saved_profile = *p;
    memset(c, 0, sizeof(*c));
    c->profile = saved_profile;
    p = &c->profile;
    c->host_connected = false;
    c->calibrated_arrival_steps = p->fps_calibrated_steps;
    uint32_t maximum = p->default_feed_velocity_steps_s > p->default_rewind_velocity_steps_s
        ? p->default_feed_velocity_steps_s : p->default_rewind_velocity_steps_s;
    if (p->load_cruise_velocity_steps_s > maximum) maximum = p->load_cruise_velocity_steps_s;
    c->base_max_velocity_steps_s = maximum;
    c->base_acceleration_steps_s2 = p->default_acceleration_steps_s2;
    c->base_deceleration_steps_s2 = p->default_deceleration_steps_s2;
    km_result_t result = km_trajectory_init(&c->trajectory, maximum,
        p->default_acceleration_steps_s2, p->default_deceleration_steps_s2);
    if (result == KM_OK && p->default_jerk_steps_s3) /* 0: the trajectory's default */
        result = km_trajectory_set_jerk(&c->trajectory, p->default_jerk_steps_s3);
    if (result != KM_OK) return result;
    result = init_follower(c);
    if (result != KM_OK) return result;
    result = p->has_proportional_buffer
        ? km_buffer_init_diff_adc(&c->buffer_sensor, &p->diff_calibration, p->max_sensor_age_ms)
        : km_buffer_init_switches(&c->buffer_sensor, 250, 750, 500, p->max_sensor_age_ms);
    if (result != KM_OK) return result;
    if (p->has_current_sensing) {
        result = km_current_regulator_init(&c->current_regulator, p->rewind_current_target_ma,
            p->rewind_current_limit_ma, p->rewind_pwm_duty_permille, p->max_sensor_age_ms);
        if (result != KM_OK) return result;
    }
    km_event_queue_init(&c->event_queue);
    return KM_OK;
}

static bool feeder_ready(const km_driver_t *d, const km_device_profile_t *p)
{
    return d && d->initialized && d->ops && d->ops->set_enable && d->ops->stop_outputs
        && d->caps.supports_step_dir && d->ops->set_direction
        && d->caps.max_velocity_steps_s >= p->default_feed_velocity_steps_s
        && d->caps.max_velocity_steps_s >= p->default_rewind_velocity_steps_s
        && d->caps.min_pulse_width_us
        && d->caps.min_step_interval_us > d->caps.min_pulse_width_us;
}

static bool rewinder_ready(const km_driver_t *d, bool measured)
{
    return d && d->initialized && d->ops && d->ops->set_enable && d->ops->stop_outputs
        && d->ops->set_direction && d->ops->set_pwm_duty && d->caps.supports_pwm
        && (!measured || d->caps.supports_current_measurement);
}

km_result_t km_core_bind_lane_driver(km_operation_core_t *c, uint8_t lane,
                                     km_driver_t *feeder, km_driver_t *rewinder)
{
    if (!c || lane >= c->profile.lane_count || !feeder) return KM_ERR_INVALID_PARAM;
    if (c->shared_path_owned || km_trajectory_is_moving(&c->trajectory)) return KM_ERR_BUSY;
    if (!feeder_ready(feeder, &c->profile) || feeder->enabled) return KM_ERR_UNAVAILABLE;
    if (rewinder && (!rewinder_ready(rewinder, c->profile.has_current_sensing) || rewinder->enabled))
        return KM_ERR_UNAVAILABLE;
    if (feeder == rewinder) return KM_ERR_CONFLICT;
    /* Drivers are initialized by the board backend with its real configuration,
     * never by passing an invented NULL configuration from the common core.
     * With a selector, the gear is ONE feeder driver shared by every lane;
     * that is the intended binding, not a conflict. */
    for (unsigned i = 0; i < c->profile.lane_count; ++i) {
        if (i == lane) continue;
        bool shared_gear = c->profile.has_selector && feeder == c->feeder_drivers[i];
        if ((!shared_gear && feeder == c->feeder_drivers[i]) || feeder == c->rewinder_drivers[i]
            || (rewinder && (rewinder == c->feeder_drivers[i] || rewinder == c->rewinder_drivers[i])))
            return KM_ERR_CONFLICT;
    }
    c->feeder_drivers[lane] = feeder;
    c->rewinder_drivers[lane] = rewinder;
    c->motor_ready = true;
    return KM_OK;
}

km_result_t km_core_bind_selector(km_operation_core_t *c, km_driver_t *stepper, km_servo_t *servo)
{
    if (!c || !c->profile.has_selector) return KM_ERR_INVALID_PARAM;
    if (c->profile.selector.kind == KM_SELECTOR_SERVO ? stepper != NULL : stepper == NULL)
        return KM_ERR_INVALID_PARAM;
    if (c->profile.selector.has_servo != (servo != NULL)) return KM_ERR_INVALID_PARAM;
    if (c->shared_path_owned || km_trajectory_is_moving(&c->trajectory)) return KM_ERR_BUSY;
    c->selector_driver = stepper;
    c->selector_servo = servo;
    return KM_OK;
}

void km_core_set_selector_endstop_at(km_operation_core_t *c, bool asserted, bool valid, uint32_t sample_us)
{
    if (!c) return;
    c->selector_endstop_asserted = asserted;
    c->selector_endstop_valid = valid;
    c->selector_endstop_sample_us = sample_us;
}

void km_core_set_selector_gate_switch_at(km_operation_core_t *c, uint8_t gate, bool asserted,
                                         bool valid, uint32_t sample_us)
{
    if (!c || gate >= c->profile.lane_count) return;
    c->selector_gate_asserted[gate] = asserted;
    c->selector_gate_valid[gate] = valid;
    c->selector_gate_sample_us[gate] = sample_us;
}

km_result_t km_core_set_selector_gate_steps(km_operation_core_t *c, const int32_t *steps, uint8_t count)
{
    if (!c || !c->profile.has_selector || !steps || !count || count > c->profile.lane_count
        || c->profile.selector.kind == KM_SELECTOR_SERVO
        || c->profile.selector.kind == KM_SELECTOR_INDEXED) return KM_ERR_INVALID_PARAM;
    if (c->stage != KM_STAGE_STOPPED && c->stage != KM_STAGE_FOLLOW) return KM_ERR_BUSY;
    for (uint8_t i = 0; i < count; ++i) {
        if (steps[i] == -1) continue; /* Deliberately uncalibrated. */
        if (steps[i] < 0 || (uint32_t)steps[i] >= c->profile.selector.home.max_travel_steps)
            return KM_ERR_INVALID_PARAM;
    }
    for (uint8_t i = 0; i < count; ++i) c->profile.selector.gate_steps[i] = steps[i];
    /* A calibrated table replaces positions this session trusted; require a
     * fresh SELECT_MOVE before any further filament motion. */
    c->selector_engaged = false;
    return KM_OK;
}

/* Runtime single-gate calibration write (operation_core.h): stricter idle
 * check than km_core_set_selector_gate_steps above, since this is meant for
 * the interactive MMU_CALIBRATE_SELECTOR-style flow, never mid-print. */
static bool selector_fully_idle(const km_operation_core_t *c)
{
    return c->op_state != KM_OP_STATE_FAULT && c->stage == KM_STAGE_STOPPED
        && c->active_op == KM_OP_TYPE_NONE;
}

km_result_t km_core_set_selector_gate(km_operation_core_t *c, uint8_t gate, int32_t value)
{
    if (!c || !c->profile.has_selector || gate >= c->profile.lane_count) return KM_ERR_INVALID_PARAM;
    if (!selector_fully_idle(c)) return KM_ERR_BUSY;
    const km_selector_profile_t *s = &c->profile.selector;
    if (s->kind == KM_SELECTOR_SERVO) {
        if (value != 0 && (value < 500 || value > 2500)) return KM_ERR_INVALID_PARAM;
        c->profile.selector.servo_gate_us[gate] = (uint16_t)value;
    } else if (s->kind == KM_SELECTOR_INDEXED) {
        return KM_ERR_INVALID_PARAM; /* No per-gate table to write; found by its own switch. */
    } else {
        if (value != -1 && (value < 0 || (uint32_t)value >= s->home.max_travel_steps))
            return KM_ERR_INVALID_PARAM;
        c->profile.selector.gate_steps[gate] = value;
    }
    /* A calibrated position replaces one this session trusted; same rule as
     * km_core_set_selector_gate_steps. */
    c->selector_engaged = false;
    return KM_OK;
}

km_result_t km_core_selector_servo_test(km_operation_core_t *c, uint16_t pulse_us)
{
    if (!c || !c->profile.has_selector || c->profile.selector.kind != KM_SELECTOR_SERVO)
        return KM_ERR_INVALID_PARAM;
    if (!c->selector_servo || !c->selector_servo->set_pulse_us) return KM_ERR_UNAVAILABLE;
    if (pulse_us && (pulse_us < 500 || pulse_us > 2500)) return KM_ERR_INVALID_PARAM;
    if (!selector_fully_idle(c)) return KM_ERR_BUSY;
    /* A scratch jog, not a selection: selector_engaged/selector_gate and the
     * gate table are untouched, so the host can try several angles freely. */
    return c->selector_servo->set_pulse_us(c->selector_servo, pulse_us);
}

km_result_t km_core_set_calibrated_arrival_steps(km_operation_core_t *c, uint32_t steps)
{
    if (!c || !c->profile.fps_arrival) return KM_ERR_INVALID_PARAM;
    if (!selector_fully_idle(c)) return KM_ERR_BUSY;
    if (steps && (steps < c->profile.fps_arrival_min_steps
                  || steps > c->profile.fps_arrival_max_steps))
        return KM_ERR_INVALID_PARAM;
    c->calibrated_arrival_steps = steps;
    return KM_OK;
}

km_result_t km_core_selector_motors_off(km_operation_core_t *c)
{
    if (!c || !c->profile.has_selector) return KM_ERR_INVALID_PARAM;
    if (!selector_fully_idle(c)) return KM_ERR_BUSY;
    km_driver_shutdown(c->selector_driver);
    /* The shared gear (one feeder driver bound to every lane, selector.h);
     * shutting it down here too means MOTORS_OFF never leaves it holding. */
    for (unsigned i = 0; i < c->profile.lane_count; ++i)
        km_driver_shutdown(c->feeder_drivers[i]);
    /* Same as any other loss of holding torque (km_core_explicit_stop): a
     * selector moved by hand while off is never trusted, so the next
     * selection homes and re-engages from scratch. */
    c->selector_homed = false;
    c->selector_engaged = false;
    return KM_OK;
}

void km_core_bind_drivers(km_operation_core_t *c, km_driver_t *f, km_driver_t *r, km_driver_t *s)
{
    if (km_core_bind_lane_driver(c, 0, f, r) != KM_OK && c) c->motor_ready = false;
    if (s && c && c->profile.has_selector) (void)km_core_bind_selector(c, s, c->selector_servo);
}

static void set_path_sensors(km_operation_core_t *c, uint8_t source_lane,
                            bool hub, bool toolhead, bool valid, uint32_t sample)
{
    if (!c->profile.load_is_virtual_hub) {
        if (valid && c->hub_sensor_valid && hub != c->filament_present_hub
            && !km_trajectory_is_moving(&c->trajectory))
            bump_idle_edge(&c->hub_idle_edges);
        c->filament_present_hub = hub;
        c->hub_sensor_valid = valid;
    }
    c->filament_present_toolhead = toolhead;
    c->toolhead_sensor_valid = valid;
    c->path_sample_ms = sample;
    if (valid && ((!c->profile.load_is_virtual_hub && hub) || toolhead)) {
        if (!c->path_occupied)
            c->path_occupant_lane = c->shared_path_owned ? c->active_lane : source_lane;
        c->path_occupied = true;
    }
    /* Absence alone never erases occupancy after an interrupted operation. */
}

void km_core_set_sensors_at(km_operation_core_t *c, uint8_t lane, bool bay,
                           bool hub, bool toolhead, bool valid, uint32_t sample)
{
    if (!c || lane >= c->profile.lane_count) return;
    if (valid && c->bay_valid[lane] && bay != c->filament_present_bay[lane]
        && !lane_feeder_moving(c, lane))
        bump_idle_edge(&c->bay_idle_edges[lane]);
    c->filament_present_bay[lane] = bay;
    c->bay_valid[lane] = valid;
    c->bay_sample_ms[lane] = sample;
    c->bay_sensors_valid = valid;
    set_path_sensors(c, lane, hub, toolhead, valid, sample);
}

void km_core_set_load_sensor_at(km_operation_core_t *c, uint8_t lane,
                                bool present, bool valid, uint32_t sample)
{
    if (!c || lane >= c->profile.lane_count) return;
    if (valid && c->load_valid[lane] && present != c->filament_present_load[lane]
        && !lane_feeder_moving(c, lane))
        bump_idle_edge(&c->load_idle_edges[lane]);
    c->filament_present_load[lane] = present;
    c->load_valid[lane] = valid;
    c->load_sample_ms[lane] = sample;
    if (c->profile.load_is_virtual_hub) {
        if (valid && present) {
            if (!c->path_occupied) {
                c->path_occupant_lane = c->shared_path_owned && c->active_lane != lane
                    ? KM_LANE_UNKNOWN : lane;
                c->path_occupied = true;
            } else if (c->path_occupant_lane != lane) {
                /* Two asserted exits cannot identify one filament path. */
                c->path_occupant_lane = KM_LANE_UNKNOWN;
            }
        }
        project_virtual_hub(c);
    }
}

km_result_t km_core_set_presence_at(km_operation_core_t *c, const bool *bays,
                                  bool hub, bool toolhead, bool valid, uint32_t sample)
{
    if (!c || !bays || !c->profile.lane_count || c->profile.lane_count > KM_MAX_LANES)
        return KM_ERR_INVALID_PARAM;
    for (unsigned i = 0; i < c->profile.lane_count; ++i) {
        if (valid && c->bay_valid[i] && bays[i] != c->filament_present_bay[i]
            && !lane_feeder_moving(c, (uint8_t)i))
            bump_idle_edge(&c->bay_idle_edges[i]);
        c->filament_present_bay[i] = bays[i];
        c->bay_valid[i] = valid;
        c->bay_sample_ms[i] = sample;
    }
    c->bay_sensors_valid = valid;
    set_path_sensors(c, KM_LANE_UNKNOWN, hub, toolhead, valid, sample);
    return KM_OK;
}

static km_sensor_sample_t sensor_sample(bool configured, bool present,
                                       bool valid, uint32_t sampled,
                                       uint32_t observed, uint32_t maximum_age)
{
    km_sensor_sample_t result = {0};
    if (configured) {
        result.configured = true;
        result.present = present;
        result.valid = valid;
        result.fresh = fresh(valid, sampled, observed, maximum_age);
        result.sample_ms = sampled;
    }
    return result;
}

km_result_t km_core_get_sensor_snapshot(const km_operation_core_t *c,
                                        uint32_t observed_ms,
                                        km_sensor_snapshot_t *out)
{
    if (!c || !out || !c->profile.lane_count || c->profile.lane_count > KM_MAX_LANES)
        return KM_ERR_INVALID_PARAM;
    km_sensor_snapshot_t result = {0};
    result.lane_count = c->profile.lane_count;
    result.observed_ms = observed_ms;
    uint32_t maximum_age = c->profile.max_sensor_age_ms;
    for (unsigned lane = 0; lane < result.lane_count; ++lane) {
        result.bays[lane] = sensor_sample(c->profile.has_bay_sensors,
            c->filament_present_bay[lane], c->bay_valid[lane],
            c->bay_sample_ms[lane], observed_ms, maximum_age);
        result.loads[lane] = sensor_sample(c->profile.has_load_sensors,
            c->filament_present_load[lane], c->load_valid[lane],
            c->load_sample_ms[lane], observed_ms, maximum_age);
    }
    uint8_t hub_lane = c->profile.load_is_virtual_hub ? virtual_hub_lane(c) : KM_LANE_UNKNOWN;
    result.hub = sensor_sample(c->profile.has_hub_sensor,
        c->filament_present_hub, c->hub_sensor_valid,
        hub_lane < c->profile.lane_count ? c->load_sample_ms[hub_lane] : c->path_sample_ms,
        observed_ms, maximum_age);
    if (c->profile.load_is_virtual_hub && hub_lane >= c->profile.lane_count)
        result.hub.valid = result.hub.fresh = false;
    result.toolhead = sensor_sample(c->profile.has_toolhead_sensor,
        c->filament_present_toolhead, c->toolhead_sensor_valid,
        c->path_sample_ms, observed_ms, maximum_age);
    *out = result;
    return KM_OK;
}

void km_core_set_sensors(km_operation_core_t *c, uint8_t lane, bool bay, bool hub, bool toolhead)
{
    /* Compatibility helper for synchronous callers at the last controller time.
     * Acquisition frontends must use the explicit timestamped entry point. */
    km_core_set_sensors_at(c, lane, bay, hub, toolhead, true,
                          c ? c->last_control_ms : 0);
}

void km_core_set_encoder_at(km_operation_core_t *c, int32_t clicks, bool valid, uint32_t sample)
{
    if (!c) return;
    c->encoder_clicks = clicks; c->encoder_valid = valid; c->encoder_sample_ms = sample;
}
void km_core_set_encoder(km_operation_core_t *c, int32_t clicks, bool valid)
{
    km_core_set_encoder_at(c, clicks, valid, c ? c->last_control_ms : 0);
}
void km_core_set_rewind_current(km_operation_core_t *c, uint16_t ma, bool valid, uint32_t sample)
{
    if (!c) return;
    c->measured_rewind_ma = ma; c->rewind_sample_valid = valid; c->rewind_sample_ms = sample;
}
void km_core_set_motor_ready(km_operation_core_t *c, bool ready)
{
    if (!c) return;
    c->motor_ready = ready;
    if (!ready && c->shared_path_owned)
        km_core_explicit_stop(c, KM_FAULT_DRIVER_FAULT, c->last_control_ms);
}

static bool event_matches(const km_event_t *e, const km_request_identity_t *r)
{
    return e->client_id == r->client && e->session_id == r->session_id
        && e->req_id == r->req_id && e->op_type == r->op_type && e->lane_idx == r->lane_idx;
}
km_result_t km_core_find_terminal(const km_operation_core_t *c,
                                 const km_request_identity_t *r, km_event_t *out)
{
    if (!c || !r || !out) return KM_ERR_INVALID_PARAM;
    for (unsigned i = 0; i < c->terminal_history_count; ++i)
        if (event_matches(&c->terminal_history[i], r)) { *out = c->terminal_history[i]; return KM_OK; }
    return KM_ERR_STALE;
}

uint8_t km_core_get_load_attempts(const km_operation_core_t *c, const km_request_identity_t *r,
                                  km_load_attempt_t out[KM_LOAD_ATTEMPTS])
{
    if (!c || !r || !out || r->op_type != KM_OP_TYPE_LOAD) return 0;
    const km_request_identity_t *l = &c->load_identity;
    if (l->op_type != r->op_type || l->client != r->client || l->session_id != r->session_id
        || l->req_id != r->req_id || l->lane_idx != r->lane_idx) return 0;
    memcpy(out, c->load_attempts, c->load_attempt_count * sizeof(*out));
    return c->load_attempt_count;
}

/* Record where a LOAD attempt, or the retract before the next one, ended. */
static void record_attempt(km_operation_core_t *c, km_fault_t fault)
{
    if (c->active_op != KM_OP_TYPE_LOAD || c->load_attempt_count >= KM_LOAD_ATTEMPTS
        || (c->stage != KM_STAGE_LOAD_FEED_TO_HUB && c->stage != KM_STAGE_LOAD_FEED_TO_BUFFER
            && c->stage != KM_STAGE_LOAD_RETRY_RETRACT)) return;
    int64_t travel = km_trajectory_get_position(&c->trajectory) - c->fps_hub_steps;
    c->load_attempts[c->load_attempt_count++] = (km_load_attempt_t){(uint8_t)c->stage,
        (uint8_t)fault, travel <= 0 ? 0u : travel >= UINT32_MAX ? UINT32_MAX : (uint32_t)travel};
}

static void release_reservations(km_operation_core_t *c)
{
    km_event_queue_release(&c->event_queue, c->event_reservations);
    c->event_reservations = 0;
}

static void emit(km_operation_core_t *c, km_event_type_t type, uint8_t code,
                 uint32_t value, uint32_t now)
{
    km_event_t e = {0};
    e.timestamp_ms = now; e.client_id = c->active_client; e.session_id = c->active_session_id;
    e.req_id = c->active_req_id; e.lane_idx = c->active_lane; e.op_type = c->active_op;
    e.type = type; e.result_code = code; e.value = value;
    if (type == KM_EVENT_PTFE_CAL_COMPLETE && c->cal_step_estimate)
        e.flags = KM_EVENT_FLAG_STEP_ESTIMATE;
    km_result_t result;
    if (c->event_reservations) {
        result = km_event_queue_push_reserved(&c->event_queue, &e);
        if (result == KM_OK) --c->event_reservations;
    } else result = km_event_queue_push(&c->event_queue, &e);
    if (result != KM_OK) {
        /* Only unsolicited idle faults use this latch. Admitted terminals own
         * reserved slots. No new work/recovery is allowed until this is queued. */
        c->fault_event = e;
        c->pending_fault_event = true;
    } else {
        unsigned last = (c->event_queue.tail + KM_EVENT_QUEUE_CAPACITY - 1u) % KM_EVENT_QUEUE_CAPACITY;
        e.seq = c->event_queue.events[last].seq;
    }
    if (c->terminal_pending) {
        c->last_terminal_event = e;
        c->has_last_terminal_event = true;
        c->terminal_history[c->terminal_history_next] = e;
        c->terminal_history_next = (uint8_t)((c->terminal_history_next + 1u) % 8u);
        if (c->terminal_history_count < 8) ++c->terminal_history_count;
        c->terminal_pending = false;
    }
}

static km_event_type_t terminal_type(km_operation_type_t op, bool cancelled)
{
    switch (op) {
    case KM_OP_TYPE_LOAD: return cancelled ? KM_EVENT_LOAD_CANCELLED : KM_EVENT_LOAD_FAILED;
    case KM_OP_TYPE_PRELOAD: return cancelled ? KM_EVENT_PRELOAD_CANCELLED : KM_EVENT_PRELOAD_FAILED;
    case KM_OP_TYPE_UNLOAD: return cancelled ? KM_EVENT_UNLOAD_CANCELLED : KM_EVENT_UNLOAD_FAILED;
    case KM_OP_TYPE_EJECT: return cancelled ? KM_EVENT_EJECT_CANCELLED : KM_EVENT_EJECT_FAILED;
    case KM_OP_TYPE_CALIBRATE: return cancelled ? KM_EVENT_PTFE_CAL_CANCELLED : KM_EVENT_PTFE_CAL_FAILED;
    case KM_OP_TYPE_HOME: return KM_EVENT_HOME_FAILED; /* No cancelled variant; HOME cannot be cancelled. */
    default: return cancelled ? KM_EVENT_STOPPED : KM_EVENT_FOLLOW_FAULT;
    }
}

void km_core_explicit_stop(km_operation_core_t *c, km_fault_t reason, uint32_t now)
{
    if (!c) return;
    bool already_faulted = c->op_state == KM_OP_STATE_FAULT;
    km_trajectory_emergency_stop(&c->trajectory);
    km_follower_disarm(&c->follower);
    for (unsigned i = 0; i < c->profile.lane_count; ++i) {
        km_driver_shutdown(c->feeder_drivers[i]);
        km_driver_shutdown(c->rewinder_drivers[i]);
    }
    km_driver_shutdown(c->selector_driver);
    /* Never trust a grip/angle across an unplanned stop; the next selection
     * re-verifies it with a fresh SELECT_MOVE/SERVO_DWELL. The selector driver
     * just lost holding torque above, exactly Happy Hare's own motors-off rule
     * (mmu_linear_selector.py disable_motors/_invalidate_persisted_position):
     * a selector that can now be pushed by hand is no longer known to be at
     * its last homed position, so the next selection homes again first. A
     * no-op for SERVO (no axis to home) and for non-selector profiles
     * (selector_homed is never read). */
    c->selector_engaged = false;
    c->selector_homed = false;
    c->rewind_pending = false;
    if (!already_faulted) {
        if (reason == KM_FAULT_NONE) reason = KM_FAULT_EMERGENCY_STOP;
        record_attempt(c, reason);
        c->system_fault = c->motion_fault = reason;
        km_event_type_t type = c->terminal_pending ? terminal_type(c->active_op, false)
            : (c->stage == KM_STAGE_FOLLOW ? KM_EVENT_FOLLOW_FAULT : KM_EVENT_FAULT_ENTERED);
        emit(c, type, km_protocol_result_code(KM_REQ_FAULTED, reason), (uint32_t)reason, now);
    }
    release_reservations(c);
    c->stage = KM_STAGE_STOPPED;
    c->op_state = KM_OP_STATE_FAULT;
    /* Retain logical path and identity until deliberate reconciliation. */
}

static bool sensors_ready(const km_operation_core_t *c, uint8_t lane, uint32_t now)
{
    uint32_t age = c->profile.max_sensor_age_ms;
    return (!c->profile.has_bay_sensors || fresh(c->bay_valid[lane], c->bay_sample_ms[lane], now, age))
        && (!c->profile.has_load_sensors || fresh(c->load_valid[lane], c->load_sample_ms[lane], now, age))
        && (!c->profile.has_hub_sensor || c->profile.load_is_virtual_hub
            || fresh(c->hub_sensor_valid, c->path_sample_ms, now, age))
        && (!c->profile.has_toolhead_sensor || fresh(c->toolhead_sensor_valid, c->path_sample_ms, now, age))
        && (!c->profile.has_encoder || fresh(c->encoder_valid, c->encoder_sample_ms, now, age));
}

static bool is_valid_time(const km_operation_core_t *c, km_core_timepoint_t time)
{
    uint32_t phase = (uint32_t)(time.us - time.ms * 1000u);
    if (phase >= 1000u) return false;

    if (c->control_clock_valid) {
        uint32_t old_phase = c->last_control_us - c->last_control_ms * 1000u;
        uint32_t elapsed_ms = time.ms - c->last_control_ms;
        int64_t elapsed_us = (int64_t)elapsed_ms * 1000LL + (int64_t)phase - (int64_t)old_phase;
        if (elapsed_us < 0 || elapsed_us >= INT64_C(2147483648)) return false;
    }
    /* Motion service may run after the previous operation update. A faulted
     * trajectory retains its old time until explicit physical reconciliation. */
    if (c->trajectory.clock_valid && !c->trajectory.faulted
        && km_time_before(time.us, c->trajectory.last_update_us)) return false;
    return true;
}

static void commit_time(km_operation_core_t *c, km_core_timepoint_t time)
{
    c->last_control_ms = time.ms;
    c->last_control_us = time.us;
    c->control_clock_valid = true;
}

/* True while the selector is already positioned and gripped/angled for
 * `lane`: LINEAR/ROTARY also need a valid home; SERVO has no axis to home. */
static bool selector_at(const km_operation_core_t *c, uint8_t lane)
{
    return c->selector_engaged && c->selector_gate == lane
        && (c->profile.selector.kind == KM_SELECTOR_SERVO || c->selector_homed);
}

/* False for a gate whose position (LINEAR/ROTARY, -1) or angle (SERVO, 0) has
 * never been measured (klipper_mmu/selector.h); selecting it is refused.
 * INDEXED has no per-gate table to check -- every gate is found by its own
 * switch at selection time, so there is nothing to be uncalibrated. */
static bool selector_gate_calibrated(const km_operation_core_t *c, uint8_t lane)
{
    const km_selector_profile_t *s = &c->profile.selector;
    if (s->kind == KM_SELECTOR_INDEXED) return true;
    return s->kind == KM_SELECTOR_SERVO ? s->servo_gate_us[lane] != 0 : s->gate_steps[lane] != -1;
}

static bool selector_hardware_ready(const km_operation_core_t *c)
{
    const km_selector_profile_t *s = &c->profile.selector;
    if (s->kind != KM_SELECTOR_SERVO) {
        const km_driver_t *d = c->selector_driver;
        if (!d || !d->initialized || !d->ops || !d->ops->set_enable || !d->ops->stop_outputs
            || !d->caps.supports_step_dir || !d->ops->set_direction
            || km_driver_get_fault(d) != KM_FAULT_NONE) return false;
    }
    if (s->has_servo && (!c->selector_servo || !c->selector_servo->set_pulse_us)) return false;
    return true;
}

/* The selection phase (klipper_mmu/selector.h) runs before a lane op's own
 * first stage; these defer/resume the op-specific tails defined further down. */
static km_admission_result_t enter_load_motion(km_operation_core_t *c, bool from_preload,
                                                uint32_t now, uint32_t us);
static km_admission_result_t enter_preload_motion(km_operation_core_t *c, uint32_t now, uint32_t us);
static km_admission_result_t enter_unload_motion(km_operation_core_t *c, bool local_preload,
                                                 bool virtual_preload_clear, uint32_t now, uint32_t us);
static km_admission_result_t enter_eject_motion(km_operation_core_t *c, uint32_t now, uint32_t us);
static km_admission_result_t enter_ptfe_cal_motion(km_operation_core_t *c, bool encoder,
                                                   uint32_t now, uint32_t us);
#if KM_SELECTOR_BUILD
static km_admission_result_t start_select(km_operation_core_t *c, uint8_t lane, uint32_t now, uint32_t us);
#endif

static km_admission_result_t check_request(km_operation_core_t *c, km_request_identity_t r, km_core_timepoint_t time)
{
    if (!c || r.client <= KM_CLIENT_NONE || r.client >= KM_MAX_CLIENTS || !r.session_id || !r.req_id)
        return KM_REQ_REJECTED_STALE;
    if (!is_valid_time(c, time)) return KM_REQ_REJECTED_STALE;
    if (r.lane_idx >= c->profile.lane_count) return KM_REQ_INVALID_LANE;
    km_event_t old;
    if (km_core_find_terminal(c, &r, &old) == KM_OK) return KM_REQ_ALREADY_COMPLETED;
    if (c->terminal_pending && c->active_client == r.client && c->active_session_id == r.session_id
        && c->active_req_id == r.req_id && c->active_op == r.op_type && c->active_lane == r.lane_idx)
        return KM_REQ_ALREADY_IN_PROGRESS;
    if (c->shared_path_owned && c->active_client != r.client) return KM_REQ_REJECTED_CONFLICT;
    if (c->shared_path_owned && c->active_session_id != r.session_id) return KM_REQ_REJECTED_STALE;
    uint32_t session = c->client_session[r.client];
    if (r.session_id < session || (r.session_id == session && r.req_id <= c->client_request[r.client]))
        return KM_REQ_REJECTED_STALE;
    if (c->op_state == KM_OP_STATE_FAULT || c->trajectory.faulted) return KM_REQ_FAULTED;
    if (c->pending_fault_event || !c->motor_ready || (r.client == KM_CLIENT_HOST
        && (!c->host_connected || km_time_elapsed(time.ms, c->last_host_heartbeat_ms) >= c->profile.host_watchdog_ms)))
        return KM_REQ_UNAVAILABLE;
    if (c->stage != KM_STAGE_STOPPED && c->stage != KM_STAGE_FOLLOW) return KM_REQ_BUSY;
    if ((c->shared_path_owned && c->path_owner_lane != r.lane_idx)
        || (c->path_occupied && c->path_occupant_lane != r.lane_idx)) return KM_REQ_PATH_OCCUPIED;
    /* The selector never moves while filament is in the shared path; the
     * lane it is already selected/gripped for needs no move. */
    if (c->profile.has_selector && c->path_occupied && !selector_at(c, r.lane_idx))
        return KM_REQ_PATH_OCCUPIED;
    if (!virtual_other_lanes_clear(c, r.lane_idx, time.ms)) return KM_REQ_UNAVAILABLE;
    if (!feeder_ready(c->feeder_drivers[r.lane_idx], &c->profile)) return KM_REQ_UNAVAILABLE;
    if (c->profile.has_selector && !selector_hardware_ready(c)) return KM_REQ_UNAVAILABLE;
    /* An uncalibrated gate (host: "gate N is not calibrated") is refused here
     * regardless of whether the selector is already sitting on it: the
     * calibration table, not the current physical position, is what a host
     * can no longer trust once it is cleared. */
    if (c->profile.has_selector && !selector_gate_calibrated(c, r.lane_idx)) return KM_REQ_UNAVAILABLE;
    if (km_driver_get_fault(c->feeder_drivers[r.lane_idx]) != KM_FAULT_NONE) return KM_REQ_FAULTED;
    if (!sensors_ready(c, r.lane_idx, time.ms)) return KM_REQ_UNAVAILABLE;
    return KM_REQ_ACCEPTED;
}

/* Bind the trajectory to the lane's own feeder and enable it: the tail of
 * admission shared with resume_operation_motion(), which runs it once the
 * selector has finished positioning for the lane instead of at admission. */
/* Engage the active lane's gear: bind it, scale the shared trajectory to its
 * own rotation distance if this build configures one, then enable it. `us`
 * only matters when a lane scale is configured -- it re-derives the gear's
 * own limits (base_max_velocity_steps_s et al.) under the just-set scale,
 * since km_trajectory_set_limits_at requires an already-enabled driver. */
static km_result_t engage_feeder(km_operation_core_t *c, uint32_t us)
{
    km_trajectory_bind_driver(&c->trajectory, c->active_feeder);
    /* ROTARY's single gear motor drives different gates by turning
     * different ways (klipper_mmu/selector.h, gate_direction): invert the
     * emitted step direction while a REVERSE gate is active_lane's, leaving
     * position/HUB/arrival accounting exactly as every other lane sees it. */
    c->trajectory.invert_direction = c->profile.has_selector
        && c->profile.selector.kind == KM_SELECTOR_ROTARY
        && c->profile.selector.gate_direction[c->active_lane] == KM_DIR_REVERSE;
    km_result_t result = KM_OK;
    /* feeder_rotation_distance_um == 0: this profile predates per-lane
     * rotation distance (every native fixture, every build that leaves
     * KLIPPER_MMU_FEEDER_LANE_ROTATION_DISTANCES_UM at its default) -- skip
     * entirely, so the trajectory's scale (and behavior) never leaves 1:1. */
    if (c->profile.feeder_rotation_distance_um)
        result = km_trajectory_set_lane_scale(&c->trajectory, c->profile.feeder_rotation_distance_um,
                                               c->profile.lane_rotation_distance_um[c->active_lane]);
    if (result == KM_OK && c->active_feeder->caps.supports_current_setpoint)
        result = km_driver_set_current_ma(c->active_feeder, c->profile.default_run_current_ma,
                                           c->profile.default_hold_current_ma);
    if (result == KM_OK) result = km_driver_set_enable(c->active_feeder, true);
    if (result == KM_OK && c->profile.feeder_rotation_distance_um)
        result = km_trajectory_set_limits_at(&c->trajectory, c->base_max_velocity_steps_s,
                                              c->base_acceleration_steps_s2,
                                              c->base_deceleration_steps_s2, us);
    return result;
}

static km_admission_result_t begin(km_operation_core_t *c, km_request_identity_t r,
                                    uint8_t slots, km_core_timepoint_t time)
{
    if (km_event_queue_reserve(&c->event_queue, slots) != KM_OK) return KM_REQ_BUSY;
    commit_time(c, time);
    uint32_t now = time.ms;
    /* End an explicitly requested follow before handing the path to the next
     * intent. An automatic post-load follow has already completed its request. */
    if (c->terminal_pending) emit(c, KM_EVENT_STOPPED, KM_WIRE_OP_CODE_SUCCESS, 0, now);
    release_reservations(c);
    c->event_reservations = slots;
    c->active_client = r.client; c->active_session_id = r.session_id; c->active_req_id = r.req_id;
    c->active_op = r.op_type; c->active_lane = r.lane_idx;
    c->client_session[r.client] = r.session_id; c->client_request[r.client] = r.req_id;
    c->terminal_pending = true; c->shared_path_owned = true; c->path_owner_lane = r.lane_idx;
    project_virtual_hub(c);
    c->active_feeder = c->feeder_drivers[r.lane_idx]; c->active_rewinder = c->rewinder_drivers[r.lane_idx];
    c->stage_entered_time_ms = c->last_progress_time_ms = now;
    c->last_progress_clicks = c->encoder_clicks;
    /* A selector machine that is not already positioned/gripped for this lane
     * selects first; the gear is engaged only once that finishes (see
     * start_select/resume_operation_motion). */
    c->select_pending = c->profile.has_selector && !selector_at(c, r.lane_idx);
    if (c->select_pending) return KM_REQ_ACCEPTED;
    km_result_t result = engage_feeder(c, time.us);
    if (result != KM_OK || c->trajectory.faulted) {
        km_core_explicit_stop(c, KM_FAULT_DRIVER_FAULT, now);
        return KM_REQ_FAULTED;
    }
    return KM_REQ_ACCEPTED;
}

static bool velocity(km_operation_core_t *c, int32_t target, uint32_t now, uint32_t us)
{
    if (km_trajectory_set_target_velocity(&c->trajectory, target, us) == KM_OK) return true;
    km_core_explicit_stop(c, KM_FAULT_DRIVER_FAULT, now);
    return false;
}

/* The last LOAD attempt feeds at half speed, within twice the deadlines. */
static bool load_slow(const km_operation_core_t *c)
{
    return c->active_op == KM_OP_TYPE_LOAD && c->load_attempt_count + 1u >= KM_LOAD_ATTEMPTS;
}

/* Calibrated LOAD approach. Cruise after the HUB is a ceiling, not a target:
 * the feeder stays on it until the travel left to the slowdown point is no
 * more than it takes to stop, then takes the crawl and keeps it to arrival.
 * The stop distance is measured from the current rate, so a LOAD whose path
 * is shorter than that simply never accelerates: a triangle, not an error. */
static bool load_approach(km_operation_core_t *c, uint32_t now, uint32_t us)
{
    if (c->active_op != KM_OP_TYPE_LOAD || c->load_crawl) return true;
    int64_t slowdown = (int64_t)c->calibrated_arrival_steps
        - (int64_t)c->profile.load_window_steps;
    if (slowdown < 0) slowdown = 0;
    /* Travel since the HUB, the same origin fps_update() and calibrate_retract()
     * measure S from; the raw trajectory position also carries the feed-to-HUB
     * leg. */
    int64_t position = km_trajectory_get_position(&c->trajectory) - c->fps_hub_steps;
    int64_t speed = km_trajectory_get_velocity(&c->trajectory);
    if (speed < 0) speed = -speed;
    /* Stopping distance from here, plus three ticks of travel: a retarget
     * that lands a tick late still starts the crawl before the window. */
    int64_t room = km_trajectory_get_stop_distance(&c->trajectory)
        + speed * 3 * KM_RATE_MAX_UPDATE_US / 1000000;
    if (slowdown - position > room) return true;
    c->load_crawl = true;
    return velocity(c, (int32_t)c->profile.load_crawl_velocity_steps_s, now, us);
}

/* A LOAD feed without arrival (NO_PROGRESS: deadline or maximum travel) or
 * with early compression (JAM) is retried while attempts remain: record it,
 * brake, and retract before feeding again. Any other stop is final. */
static void load_fail(km_operation_core_t *c, km_fault_t fault, uint32_t now, uint32_t us)
{
    if (c->active_op != KM_OP_TYPE_LOAD || load_slow(c)
        || (fault != KM_FAULT_NO_PROGRESS && fault != KM_FAULT_JAM)
        || (c->stage != KM_STAGE_LOAD_FEED_TO_HUB && c->stage != KM_STAGE_LOAD_FEED_TO_BUFFER)) {
        km_core_explicit_stop(c, fault, now);
        return;
    }
    record_attempt(c, fault);
    c->stage = KM_STAGE_LOAD_RETRY_RETRACT;
    c->stage_entered_time_ms = now;
    c->rewind_pending = true; /* The retract starts once the feeder stops. */
    (void)velocity(c, 0, now, us);
}

/* FPS arrival starts from a fresh buffer below the arrival threshold. The
 * Hall buffer reports compression only: 0 is its normal uncompressed rest
 * (slack or tension alike), never a fault. */
static bool fps_neutral(km_operation_core_t *c, uint32_t now)
{
    return km_buffer_check_freshness(&c->buffer_sensor, now)
        && c->buffer_sensor.position_permille < c->profile.fps_arrival_permille;
}

/* Every entry to FEED_TO_BUFFER starts a new arrival window. Evidence must
 * be acquired after this HUB observation and its completed feeder position. */
static void fps_begin(km_operation_core_t *c, uint32_t now)
{
    c->fps_hub_steps = km_trajectory_get_position(&c->trajectory);
    c->fps_sample_ms = c->fps_start_ms = now;
    c->fps_count = 0;
    c->fps_armed = false;
    c->fps_min_reached = false;
    c->fps_min_ms = now;
}

/* PRELOAD parks behind the HUB (retracts until it clears) rather than
 * resting on it whenever there is no per-lane LOAD switch to stop at and a
 * real, physical HUB to retract off of: Happy Hare preloads every such gate
 * this way, parking it behind the shared exit. Selector machines already
 * need this (one lane must give up the shared path so the selector can move
 * to another); a device with its own per-lane gear but the same sensor
 * shape (no LOAD switch, a real HUB, not a virtual one) rests the same way. */
static bool preload_rests_behind_hub(const km_device_profile_t *p)
{
    return !p->has_load_sensors && p->has_hub_sensor && !p->load_is_virtual_hub;
}

/* The lane's own PRELOAD rest still counts as an idle path for LOAD and
 * CALIBRATE; any other occupancy does not. */
static bool preloaded_at(const km_operation_core_t *c, uint8_t lane)
{
    return c->op_state == KM_OP_STATE_PRELOADED && c->path_occupied
        && c->path_occupant_lane == lane && !c->filament_present_toolhead
        && (c->profile.load_is_virtual_hub ? !c->filament_present_load[lane]
            : c->profile.has_load_sensors
            ? (c->filament_present_load[lane] && !c->filament_present_hub)
            : !c->filament_present_hub);
}

static km_admission_result_t enter_load_motion(km_operation_core_t *c, bool from_preload,
                                               uint32_t now, uint32_t us)
{
    /* Resuming a preload rest that sits AT a LOAD switch (real or virtual)
     * starts past the HUB already; a preload_rests_behind_hub rest does not
     * -- it still needs to feed through the HUB again before the buffer. */
    c->stage = from_preload && !c->profile.has_load_sensors
        && !preload_rests_behind_hub(&c->profile)
        ? KM_STAGE_LOAD_FEED_TO_BUFFER : KM_STAGE_LOAD_FEED_TO_HUB;
    fps_begin(c, now);
    return velocity(c, (int32_t)c->profile.default_feed_velocity_steps_s, now, us)
        ? KM_REQ_ACCEPTED : KM_REQ_FAULTED;
}

km_admission_result_t km_core_request_load(km_operation_core_t *c, km_client_id_t client,
                                            uint32_t session, uint32_t req, uint8_t lane, km_core_timepoint_t time)
{
    km_request_identity_t r = {client, session, req, KM_OP_TYPE_LOAD, lane};
    km_admission_result_t admission = check_request(c, r, time);
    if (admission != KM_REQ_ACCEPTED) return admission;
    bool from_preload = preloaded_at(c, lane);
    if (c->stage == KM_STAGE_FOLLOW || (c->path_occupied && !from_preload)) return KM_REQ_PATH_OCCUPIED;
    if (!c->profile.has_hub_sensor || (!c->profile.has_toolhead_sensor && !c->profile.fps_arrival)
        || !km_buffer_check_freshness(&c->buffer_sensor, time.ms)
        || (c->profile.fps_arrival && !fps_neutral(c, time.ms))) return KM_REQ_UNAVAILABLE;
    /* An FPS-arrival LOAD slows down for a measured distance. Without one
     * (no stored value and no CALIBRATE this session) the approach would have
     * no target to aim at, so refuse it instead of feeding the whole path at
     * one speed. CALIBRATE itself needs no calibrated distance. */
    if (c->profile.fps_arrival && !c->calibrated_arrival_steps) return KM_REQ_UNAVAILABLE;
    if (c->profile.has_bay_sensors && !c->filament_present_bay[lane]) return KM_REQ_NO_SPOOL;
    if (c->profile.has_load_sensors && !c->profile.load_is_virtual_hub
        && !c->filament_present_load[lane]) return KM_REQ_UNAVAILABLE;
    admission = begin(c, r, 2, time); /* Load terminal plus autonomous-follow fault/stop. */
    if (admission != KM_REQ_ACCEPTED) return admission;
    c->load_identity = r;
    c->load_attempt_count = 0;
    c->load_started_ms = time.ms;
    c->load_crawl = false;
    c->op_state = KM_OP_STATE_LOADING;
#if KM_SELECTOR_BUILD
    if (c->select_pending) { c->select_flag_a = from_preload; return start_select(c, lane, time.ms, time.us); }
#endif
    return enter_load_motion(c, from_preload, time.ms, time.us);
}

km_admission_result_t km_core_request_preload(km_operation_core_t *c, km_client_id_t client,
                                               uint32_t session, uint32_t req, uint8_t lane,
                                               km_core_timepoint_t time)
{
    km_request_identity_t r = {client, session, req, KM_OP_TYPE_PRELOAD, lane};
    km_admission_result_t admission = check_request(c, r, time);
    if (admission != KM_REQ_ACCEPTED) return admission;
    if (c->stage == KM_STAGE_FOLLOW || c->path_occupied) return KM_REQ_PATH_OCCUPIED;
    if (!c->profile.has_hub_sensor || !c->profile.has_bay_sensors
        || !c->filament_present_bay[lane] || c->filament_present_hub
        || (c->profile.has_load_sensors && c->filament_present_load[lane])
        || (c->profile.has_toolhead_sensor && c->filament_present_toolhead))
        return c->profile.has_bay_sensors && !c->filament_present_bay[lane]
            ? KM_REQ_NO_SPOOL : KM_REQ_UNAVAILABLE;
    admission = begin(c, r, 1, time);
    if (admission != KM_REQ_ACCEPTED) return admission;
    c->op_state = KM_OP_STATE_PRELOADING;
#if KM_SELECTOR_BUILD
    if (c->select_pending) return start_select(c, lane, time.ms, time.us);
#endif
    return enter_preload_motion(c, time.ms, time.us);
}

static km_admission_result_t enter_preload_motion(km_operation_core_t *c, uint32_t now, uint32_t us)
{
    c->stage = KM_STAGE_LOAD_FEED_TO_HUB;
    return velocity(c, (int32_t)c->profile.default_feed_velocity_steps_s, now, us)
        ? KM_REQ_ACCEPTED : KM_REQ_FAULTED;
}

static bool start_rewind(km_operation_core_t *c, uint32_t now_ms, uint32_t now_us)
{
    if (!c->profile.has_powered_rewind) return true;
    km_driver_t *d = c->active_rewinder;
    km_result_t result = km_driver_set_pwm_duty(d, 0);
    if (result == KM_OK) result = km_driver_set_direction(d, KM_DIR_REVERSE, now_us);
    if (result == KM_OK) result = km_driver_set_enable(d, true);
    uint16_t duty = c->profile.rewind_pwm_duty_permille;
    if (result == KM_OK && c->profile.has_current_sensing) {
        km_current_regulator_reset(&c->current_regulator);
        result = km_current_regulator_update_sample(&c->current_regulator, c->measured_rewind_ma,
            c->rewind_sample_valid, c->rewind_sample_ms, now_ms, &duty);
    }
    if (result == KM_OK) result = km_driver_set_pwm_duty(d, duty);
    if (result == KM_OK) return true;
    km_core_explicit_stop(c, KM_FAULT_DRIVER_FAULT, now_ms);
    return false;
}

static km_admission_result_t request_unload(km_operation_core_t *c,
                                            km_request_identity_t r, km_core_timepoint_t time)
{
    km_admission_result_t admission = check_request(c, r, time);
    if (admission != KM_REQ_ACCEPTED) return admission;
    /* A cancelled preload with a transient LOAD signal retains a conservative
     * LOADED reservation. If LOAD later asserts, retract past it before release. */
    bool local_preload = c->profile.has_load_sensors
        && (c->op_state == KM_OP_STATE_PRELOADED
            || (c->op_state == KM_OP_STATE_LOADED
                && c->filament_present_load[r.lane_idx]
                && !c->filament_present_hub && !c->filament_present_toolhead));
    bool virtual_preload_clear = c->profile.load_is_virtual_hub
        && c->op_state == KM_OP_STATE_PRELOADED
        && !c->filament_present_load[r.lane_idx];
    if (!c->path_occupied || !c->profile.has_hub_sensor) return KM_REQ_UNAVAILABLE;
    if (local_preload && (!c->filament_present_bay[r.lane_idx]
        || (!c->filament_present_load[r.lane_idx] && !virtual_preload_clear)
        || c->filament_present_hub
        || c->filament_present_toolhead)) return KM_REQ_UNAVAILABLE;
    if (c->profile.has_powered_rewind && (!rewinder_ready(c->rewinder_drivers[r.lane_idx], c->profile.has_current_sensing)
        || km_driver_get_fault(c->rewinder_drivers[r.lane_idx]) != KM_FAULT_NONE)) return KM_REQ_UNAVAILABLE;
    if (c->profile.has_current_sensing && !fresh(c->rewind_sample_valid, c->rewind_sample_ms,
                                                time.ms, c->profile.max_sensor_age_ms)) return KM_REQ_UNAVAILABLE;
    admission = begin(c, r, 1, time);
    if (admission != KM_REQ_ACCEPTED) return admission;
    c->unload_from_load_preload = local_preload;
    km_follower_disarm(&c->follower);
    c->op_state = KM_OP_STATE_UNLOADING;
#if KM_SELECTOR_BUILD
    if (c->select_pending) {
        c->select_flag_a = local_preload; c->select_flag_b = virtual_preload_clear;
        return start_select(c, r.lane_idx, time.ms, time.us);
    }
#endif
    return enter_unload_motion(c, local_preload, virtual_preload_clear, time.ms, time.us);
}

static km_admission_result_t enter_unload_motion(km_operation_core_t *c, bool local_preload,
                                                 bool virtual_preload_clear, uint32_t now, uint32_t us)
{
    if (virtual_preload_clear) {
        /* PRELOAD already retracted behind this physical switch. Release the
         * reservation through normal braking verification without more motion. */
        c->settling_event = KM_EVENT_UNLOAD_COMPLETE;
        c->stage = KM_STAGE_BRAKING;
        return KM_REQ_ACCEPTED;
    }
    c->stage = local_preload ? KM_STAGE_UNLOAD_RETRACT_TO_LOAD : KM_STAGE_UNLOAD_RETRACT_TO_HUB;
    if (!velocity(c, -(int32_t)c->profile.default_rewind_velocity_steps_s, now, us))
        return KM_REQ_FAULTED;
    c->rewind_pending = c->profile.has_powered_rewind;
    if (km_trajectory_get_velocity(&c->trajectory) <= 0 && c->rewind_pending) {
        c->rewind_pending = false;
        if (!start_rewind(c, now, us)) return KM_REQ_FAULTED;
    }
    return KM_REQ_ACCEPTED;
}

static bool eject_path_clear(const km_operation_core_t *c, uint8_t lane)
{
    return !c->filament_present_hub
        && (!c->profile.has_toolhead_sensor || !c->filament_present_toolhead)
        && (!c->profile.has_load_sensors || !c->filament_present_load[lane]);
}

km_admission_result_t km_core_request_eject(km_operation_core_t *c,
                                            km_client_id_t client, uint32_t session,
                                            uint32_t req, uint8_t lane, km_core_timepoint_t time)
{
    km_request_identity_t r = {client, session, req, KM_OP_TYPE_EJECT, lane};
    km_admission_result_t admission = check_request(c, r, time);
    if (admission != KM_REQ_ACCEPTED) return admission;
    if (c->stage != KM_STAGE_STOPPED || c->op_state != KM_OP_STATE_IDLE
        || c->path_occupied || c->shared_path_owned) return KM_REQ_PATH_OCCUPIED;
    /* Without BAY switches EJECT is Happy Hare's blind move back by a set
     * distance; there is no switch to tell whether a spool is there. */
    if (!c->profile.has_hub_sensor
        || (!c->profile.has_bay_sensors && !c->profile.eject_final_steps))
        return KM_REQ_UNAVAILABLE;
    if (c->profile.has_bay_sensors && !c->filament_present_bay[lane]) return KM_REQ_NO_SPOOL;
    if (!eject_path_clear(c, lane)) return KM_REQ_PATH_OCCUPIED;
    if (c->profile.has_powered_rewind &&
        (!rewinder_ready(c->rewinder_drivers[lane], c->profile.has_current_sensing)
         || km_driver_get_fault(c->rewinder_drivers[lane]) != KM_FAULT_NONE))
        return KM_REQ_UNAVAILABLE;
    if (c->profile.has_current_sensing
        && !fresh(c->rewind_sample_valid, c->rewind_sample_ms,
                  time.ms, c->profile.max_sensor_age_ms)) return KM_REQ_UNAVAILABLE;
    admission = begin(c, r, 1, time);
    if (admission != KM_REQ_ACCEPTED) return admission;
    c->op_state = KM_OP_STATE_EJECTING;
#if KM_SELECTOR_BUILD
    if (c->select_pending) return start_select(c, lane, time.ms, time.us);
#endif
    return enter_eject_motion(c, time.ms, time.us);
}

static km_admission_result_t enter_eject_motion(km_operation_core_t *c, uint32_t now, uint32_t us)
{
    c->stage = KM_STAGE_EJECT_TO_BAY;
    if (!c->profile.has_bay_sensors) {
        int64_t target = km_trajectory_get_model_position(&c->trajectory)
            - (int64_t)c->profile.eject_final_steps;
        if (km_trajectory_set_target_position(&c->trajectory, target,
                c->profile.default_rewind_velocity_steps_s, us) != KM_OK) {
            km_core_explicit_stop(c, KM_FAULT_DRIVER_FAULT, now);
            return KM_REQ_FAULTED;
        }
    } else if (!velocity(c, -(int32_t)c->profile.default_rewind_velocity_steps_s, now, us))
        return KM_REQ_FAULTED;
    c->rewind_pending = c->profile.has_powered_rewind;
    if (km_trajectory_get_velocity(&c->trajectory) <= 0 && c->rewind_pending) {
        c->rewind_pending = false;
        if (!start_rewind(c, now, us)) return KM_REQ_FAULTED;
    }
    return KM_REQ_ACCEPTED;
}

km_admission_result_t km_core_request_unload(km_operation_core_t *c, km_client_id_t client,
                                              uint32_t session, uint32_t req, km_core_timepoint_t time)
{
    km_request_identity_t r = {client, session, req, KM_OP_TYPE_UNLOAD, c ? c->active_lane : 0};
    return request_unload(c, r, time);
}

static km_admission_result_t request_follow(km_operation_core_t *c, km_request_identity_t r,
                                            km_direction_t direction, km_core_timepoint_t time)
{
    if (direction != KM_DIR_FORWARD && direction != KM_DIR_REVERSE) return KM_REQ_UNAVAILABLE;
    km_admission_result_t admission = check_request(c, r, time);
    if (admission == KM_REQ_ALREADY_IN_PROGRESS && c->follower.direction != direction)
        return KM_REQ_REJECTED_CONFLICT;
    if (admission != KM_REQ_ACCEPTED) return admission;
    if (!c->path_occupied || !km_buffer_check_freshness(&c->buffer_sensor, time.ms)) return KM_REQ_UNAVAILABLE;
    admission = begin(c, r, 1, time);
    if (admission != KM_REQ_ACCEPTED) return admission;
    if (km_follower_arm(&c->follower, direction, KM_FOLLOWER_DRIVE_STEP_VELOCITY, time.ms) != KM_OK) {
        km_core_explicit_stop(c, KM_FAULT_BUFFER_SIGNAL, time.ms); return KM_REQ_FAULTED;
    }
    c->stage = KM_STAGE_FOLLOW; c->op_state = KM_OP_STATE_FOLLOWING;
    return KM_REQ_ACCEPTED;
}

km_admission_result_t km_core_request_follow(km_operation_core_t *c, km_client_id_t client,
                                              uint32_t session, uint32_t req, bool enable,
                                              km_direction_t direction, km_core_timepoint_t time)
{
    if (!enable) return km_core_request_cancel5(c, client, session, req, time);
    km_request_identity_t r = {client, session, req, KM_OP_TYPE_FOLLOW, c ? c->active_lane : 0};
    return request_follow(c, r, direction, time);
}

km_admission_result_t km_core_request_ptfe_cal(km_operation_core_t *c, km_client_id_t client,
                                                uint32_t session, uint32_t req, uint8_t lane, km_core_timepoint_t time)
{
    km_request_identity_t r = {client, session, req, KM_OP_TYPE_CALIBRATE, lane};
    km_admission_result_t admission = check_request(c, r, time);
    if (admission != KM_REQ_ACCEPTED) return admission;
    /* An encoder measures to the toolhead switch. Otherwise FPS arrival bounds
     * a HUB-to-arrival step estimate through the LOAD feed stages. */
    bool encoder = c->profile.has_encoder && c->profile.has_toolhead_sensor;
    if (!c->profile.has_hub_sensor
        || (!encoder && (!c->profile.fps_arrival || !fps_neutral(c, time.ms))))
        return KM_REQ_UNAVAILABLE;
    if ((c->path_occupied && !preloaded_at(c, lane)) || c->stage == KM_STAGE_FOLLOW)
        return KM_REQ_PATH_OCCUPIED;
    if (c->profile.has_bay_sensors && !c->filament_present_bay[lane]) return KM_REQ_NO_SPOOL;
    admission = begin(c, r, 1, time);
    if (admission != KM_REQ_ACCEPTED) return admission;
    c->op_state = KM_OP_STATE_CALIBRATING;
#if KM_SELECTOR_BUILD
    if (c->select_pending) { c->select_flag_a = encoder; return start_select(c, lane, time.ms, time.us); }
#endif
    return enter_ptfe_cal_motion(c, encoder, time.ms, time.us);
}

static km_admission_result_t enter_ptfe_cal_motion(km_operation_core_t *c, bool encoder,
                                                    uint32_t now, uint32_t us)
{
    c->stage = encoder ? KM_STAGE_CALIBRATE_PROBE : KM_STAGE_LOAD_FEED_TO_HUB;
    c->cal_step_estimate = !encoder;
    c->cal_start_clicks = c->encoder_clicks;
    return velocity(c, (int32_t)c->profile.default_feed_velocity_steps_s, now, us)
        ? KM_REQ_ACCEPTED : KM_REQ_FAULTED;
}

#if KM_SELECTOR_BUILD
/* The selector axis has no per-lane rotation distance; always 1:1 on the
 * shared trajectory regardless of which lane's feeder last used it
 * (harmless no-op once it already is, e.g. every build that never
 * configures feeder_rotation_distance_um). Must run while not moving --
 * both call sites below reach it right before their own km_trajectory_recover,
 * which requires exactly that already. */
static void selector_trajectory_scale_reset(km_operation_core_t *c)
{
    if (c->trajectory.scale_num != 1u || c->trajectory.scale_den != 1u)
        km_trajectory_set_lane_scale(&c->trajectory, 1u, 1u);
}

/* Bind the trajectory to the selector's stepper, enable it, and give it its
 * own (typically slower) motion limits; restored to the gear's own limits by
 * resume_operation_motion once the lane is selected. km_trajectory_set_limits_at
 * requires an already-enabled driver, so enable comes before it. */
static km_result_t select_bind_stepper(km_operation_core_t *c, uint32_t velocity_steps_s,
                                       uint32_t acceleration_steps_s2, uint32_t us)
{
    km_trajectory_bind_driver(&c->trajectory, c->selector_driver);
    c->trajectory.invert_direction = false; /* The selector axis is never inverted. */
    selector_trajectory_scale_reset(c);
    /* Restore the selector axis's own absolute baseline: intervening gear
     * motion (LOAD/UNLOAD, always relative to its own origins) otherwise
     * leaves the trajectory's position wherever feeding/retracting stopped.
     * km_trajectory_recover needs the driver disabled first; it was left
     * enabled (holding) since the selector's last move. */
    km_driver_shutdown(c->selector_driver);
    km_result_t r = km_trajectory_recover(&c->trajectory, c->selector_position_steps, us);
    if (r == KM_OK) r = km_driver_set_enable(c->selector_driver, true);
    if (r != KM_OK) return r;
    return km_trajectory_set_limits_at(&c->trajectory, velocity_steps_s, acceleration_steps_s2,
                                       acceleration_steps_s2, us);
}

/* Shared body of a homing-style seek: `sw`/`cfg` name the switch to look for
 * and the config (speed/travel/deadline/direction) to look for it with. */
static km_result_t enter_select_home_ex(km_operation_core_t *c, km_selector_home_switch_t sw,
                                        const km_selector_home_config_t *cfg,
                                        uint32_t now, uint32_t us)
{
    /* A previous failure latches km_selector_home_t in FAILED "until
     * deliberate reset" (selector_home.h); each fresh selection is exactly
     * that reset, so the next attempt is never refused for a fault this
     * core has already recovered from. */
    memset(&c->selector_home, 0, sizeof(c->selector_home));
    /* Homing starts from a disabled driver (selector_home.h); an earlier
     * selector move, such as the endstop back-off, leaves it holding. */
    km_driver_shutdown(c->selector_driver);
    /* This may run before the trajectory is ever rebound to the selector
     * (a home with no endstop backoff, straight from a gear operation): its
     * scale could still be a feeder lane's own, which must not apply to the
     * selector_position_steps recovered below. */
    selector_trajectory_scale_reset(c);
    /* Re-baseline the shared trajectory into the selector's own frame before
     * homing: a preceding gear operation (LOAD/UNLOAD, bound to the feeder)
     * otherwise leaves it at that driver's own leftover position, unrelated
     * to c->selector_position_steps. The pressed-switch backoff already does
     * this through select_bind_stepper; a home with no backoff comes straight
     * here instead. Needed so HOME_COMPLETE's measured travel (km_core_update)
     * and any such backoff's own delta share one continuous frame. */
    km_result_t r = km_trajectory_recover(&c->trajectory, c->selector_position_steps, us);
    if (r != KM_OK) return r;
    r = km_selector_home_start(&c->selector_home, &c->trajectory, c->selector_driver, cfg, sw, us);
    if (r != KM_OK) return r;
    c->stage = KM_STAGE_SELECT_HOME;
    c->stage_entered_time_ms = now;
    return KM_OK;
}

static km_result_t enter_select_home(km_operation_core_t *c, uint32_t now, uint32_t us)
{
    km_selector_home_switch_t sw;
    sw.asserted = c->selector_endstop_asserted;
    sw.sampled_at_us = c->selector_endstop_sample_us;
    sw.valid = c->selector_endstop_valid;
    return enter_select_home_ex(c, sw, &c->profile.selector.home, now, us);
}

static km_result_t enter_select_move(km_operation_core_t *c, int64_t target, uint32_t now, uint32_t us);

/* Homing needs a released switch at its start. A selector resting on its
 * endstop first moves clear by gate 0's position, as Happy Hare does ("Ensure
 * we are clear off the physical endstop", mmu_linear_selector.py:453). */
static km_result_t begin_select_home(km_operation_core_t *c, uint32_t now, uint32_t us)
{
    const km_selector_profile_t *s = &c->profile.selector;
    c->select_backoff = false;
    if (!(c->selector_endstop_valid && c->selector_endstop_asserted))
        return enter_select_home(c, now, us);
    if (s->gate_steps[0] <= 0) return KM_ERR_UNAVAILABLE;
    c->select_backoff = true;
    int64_t away = s->home.home_direction == KM_DIR_REVERSE ? s->gate_steps[0] : -s->gate_steps[0];
    return enter_select_move(c, c->selector_position_steps + away, now, us);
}

/* Move the selector stepper to an absolute gate position (either the
 * requested lane's gate_steps, pre-op, or a ROTARY release_gate's, post-op;
 * see c->select_releasing). */
static km_result_t enter_select_move(km_operation_core_t *c, int64_t target, uint32_t now, uint32_t us)
{
    const km_selector_profile_t *s = &c->profile.selector;
    km_result_t r = select_bind_stepper(c, s->velocity_steps_s, s->acceleration_steps_s2, us);
    if (r == KM_OK) r = km_trajectory_set_target_position(&c->trajectory, target, s->velocity_steps_s, us);
    if (r != KM_OK) return r;
    c->select_target_steps = target;
    c->stage = KM_STAGE_SELECT_MOVE;
    c->stage_entered_time_ms = now;
    return KM_OK;
}

static km_result_t enter_select_servo(km_operation_core_t *c, uint16_t pulse_us, uint32_t now)
{
    if (!c->selector_servo || !c->selector_servo->set_pulse_us) return KM_ERR_UNAVAILABLE;
    km_result_t r = c->selector_servo->set_pulse_us(c->selector_servo, pulse_us);
    if (r != KM_OK) return r;
    c->stage = KM_STAGE_SERVO_DWELL;
    c->stage_entered_time_ms = now;
    return KM_OK;
}

/* INDEXED (klipper_mmu/selector.h): every selection is itself a homing-style
 * search, so there is no separate "already homed, jump to the absolute
 * position" path -- every helper below stands in for enter_select_move on
 * the other stepper kinds. */

static km_selector_home_switch_t gate_switch(const km_operation_core_t *c, uint8_t gate)
{
    km_selector_home_switch_t sw;
    sw.asserted = c->selector_gate_asserted[gate];
    sw.sampled_at_us = c->selector_gate_sample_us[gate];
    sw.valid = c->selector_gate_valid[gate];
    return sw;
}

static int32_t indexed_dir_sign(km_direction_t d) { return d == KM_DIR_REVERSE ? -1 : 1; }

/* device_profile.c's own sentinel: an all-zero gate_order means no physical
 * order is known, so there is nothing to pick a shorter direction along
 * (AFC's own selector: extras/AFC_vivid.py:159-199 select_lane always
 * searches forward, never a shorter reverse hop). */
static bool indexed_has_gate_order(const km_selector_profile_t *s, uint8_t n)
{
    for (uint8_t i = 0; i < n; ++i) if (s->gate_order[i]) return true;
    return false;
}

/* The direction that reaches `to` soonest along profile.selector.gate_order
 * from the current gate (mmu_indexed_selector.py:246-261 _best_rotation_
 * direction). Not yet homed this cycle == HH's lgate_selected is None, or no
 * gate_order at all (AFC's own forward-only rule above): takes the forward
 * direction, same fallback either way. */
static km_direction_t indexed_direction(const km_operation_core_t *c, uint8_t to)
{
    if (!c->selector_homed
        || !indexed_has_gate_order(&c->profile.selector, c->profile.lane_count))
        return KM_DIR_FORWARD;
    const uint8_t *order = c->profile.selector.gate_order;
    uint8_t n = c->profile.lane_count, start_idx = 0, end_idx = 0;
    for (uint8_t i = 0; i < n; ++i) {
        if (order[i] == c->selector_gate) start_idx = i;
        if (order[i] == to) end_idx = i;
    }
    uint8_t forward = (uint8_t)((end_idx + n - start_idx) % n);
    uint8_t reverse = (uint8_t)((start_idx + n - end_idx) % n);
    return forward <= reverse ? KM_DIR_FORWARD : KM_DIR_REVERSE;
}

/* The plain search for gate's own switch (mmu_indexed_selector.py:218-236
 * _find_gate's homing move): requires the switch inactive at the start
 * (selector_home.c), so a caller that has not already ruled that out must
 * back off first (indexed_enter_seek below). */
static km_result_t indexed_start_home_seek(km_operation_core_t *c, uint8_t gate, uint32_t now, uint32_t us)
{
    km_selector_home_config_t cfg = c->profile.selector.home;
    cfg.home_direction = indexed_direction(c, gate);
    return enter_select_home_ex(c, gate_switch(c, gate), &cfg, now, us);
}

/* Seek `gate`: already trusted there (software position, same as LINEAR/
 * ROTARY's own zero-distance re-select) needs no motion; its switch already
 * asserted but not yet trusted needs a clearing back-off first (own
 * convention: the full calibrated switch width, opposite the seek direction,
 * mirroring HH's home-only back-off at mmu_indexed_selector.py:189-201 but
 * sized from a value this profile actually carries); otherwise a plain
 * search. Sets select_seek_gate either way, for km_core_update's dispatch. */
static km_result_t indexed_enter_seek(km_operation_core_t *c, uint8_t gate, uint32_t now, uint32_t us)
{
    c->select_seek_gate = gate;
    if (c->selector_homed && c->selector_gate == gate) {
        c->select_backoff = false;
        c->select_centering = true;
        return enter_select_move(c, c->selector_position_steps, now, us);
    }
    if (c->selector_gate_valid[gate] && c->selector_gate_asserted[gate]) {
        km_direction_t dir = indexed_direction(c, gate);
        c->select_backoff = true;
        c->select_centering = false;
        /* With a gate order: back off the known switch width against the seek.
         * Without one (AFC): move forward off it by unselect_steps, then search
         * forward (extras/AFC_vivid.py:175-185, unselect_lane :262). */
        int64_t back = indexed_has_gate_order(&c->profile.selector, c->profile.lane_count)
            ? -indexed_dir_sign(dir) * (int64_t)(2u * c->profile.selector.center_nudge_steps)
            : (int64_t)c->profile.selector.unselect_steps;
        return enter_select_move(c, c->selector_position_steps + back, now, us);
    }
    c->select_backoff = false;
    return indexed_start_home_seek(c, gate, now, us);
}

/* SELECT_HOME found select_seek_gate's switch (km_selector_home_t.state ==
 * HOMED): center on it, half the switch width in the direction just used
 * (mmu_indexed_selector.py:237-241). */
static void indexed_found(km_operation_core_t *c, uint32_t now, uint32_t us)
{
    c->selector_homed = true;
    /* Unlike LINEAR/ROTARY's single endstop (their one absolute zero),
     * INDEXED re-finds a different gate's switch on every selection, so
     * found()'s own km_trajectory_recover(t, 0, ...) is only a fresh frame
     * for ITS internal bookkeeping -- the core's own running absolute
     * position is found_position (captured before that reset), never 0. */
    c->selector_position_steps = c->selector_home.found_position;
    int64_t nudge = indexed_dir_sign(c->selector_home.config.home_direction)
        * (int64_t)c->profile.selector.center_nudge_steps;
    c->select_centering = true;
    if (enter_select_move(c, c->selector_position_steps + nudge, now, us) != KM_OK)
        km_core_explicit_stop(c, KM_FAULT_SELECTOR, now);
}

/* SELECT_MOVE landed: either the clearing back-off (re-seek for real) or the
 * centering nudge (or a zero-distance already-there confirmation) landing on
 * select_seek_gate. A standalone HOME always wants gate 0, so it is always
 * done here; a lane selection may still need to seek onward from gate 0 (the
 * auto-home detour) to the lane it actually wants. */
static void resume_operation_motion(km_operation_core_t *c, uint32_t now, uint32_t us);
static void home_complete(km_operation_core_t *c, uint32_t travel_steps, uint32_t now);
static void indexed_after_move(km_operation_core_t *c, uint32_t now, uint32_t us)
{
    if (c->select_backoff) {
        c->select_backoff = false;
        if (indexed_start_home_seek(c, c->select_seek_gate, now, us) != KM_OK)
            km_core_explicit_stop(c, KM_FAULT_SELECTOR, now);
        return;
    }
    c->select_centering = false;
    c->selector_gate = c->select_seek_gate;
    c->selector_engaged = true;
    if (c->active_op == KM_OP_TYPE_HOME) {
        int64_t delta = c->selector_position_steps - c->home_start_steps;
        home_complete(c, (uint32_t)(delta < 0 ? -delta : delta), now);
        return;
    }
    if (c->select_seek_gate == c->active_lane) { resume_operation_motion(c, now, us); return; }
    if (indexed_enter_seek(c, c->active_lane, now, us) != KM_OK)
        km_core_explicit_stop(c, KM_FAULT_SELECTOR, now);
}

static km_admission_result_t start_select(km_operation_core_t *c, uint8_t lane, uint32_t now, uint32_t us)
{
    const km_selector_profile_t *s = &c->profile.selector;
    c->select_releasing = false; /* This is a pre-op selection, not a release. */
    km_result_t r;
    if (s->kind == KM_SELECTOR_SERVO) {
        r = enter_select_servo(c, s->servo_gate_us[lane], now);
    } else if (s->kind == KM_SELECTOR_INDEXED) {
        r = indexed_enter_seek(c, c->selector_homed ? lane : 0, now, us);
    } else {
        /* LINEAR releases the grip before any carriage motion; a rotary
         * carousel has none. */
        r = (s->has_servo && s->servo_move_us)
            ? c->selector_servo->set_pulse_us(c->selector_servo, s->servo_move_us) : KM_OK;
        if (r == KM_OK) r = c->selector_homed ? enter_select_move(c, s->gate_steps[lane], now, us)
                                              : begin_select_home(c, now, us);
    }
    if (r != KM_OK) { km_core_explicit_stop(c, KM_FAULT_SELECTOR, now); return KM_REQ_FAULTED; }
    return KM_REQ_ACCEPTED;
}

/* Dispatch to the op's own deferred motion once the selector is positioned
 * and gripped/angled for c->active_lane. The single shared implementation
 * for every op that can defer through the selection phase (step 5). */
static void resume_operation_motion(km_operation_core_t *c, uint32_t now, uint32_t us)
{
    /* The op's own first stage starts now, not whenever the selection phase
     * (SELECT_HOME/SELECT_MOVE/SERVO_DWELL) began: begin() stamped these for
     * that phase's own first stage, same as it always has for a lane that
     * needed no selection, but homing/moving/dwelling can take real time of
     * its own. Left stale, that time is silently spent out of the op's own
     * stage_timeout()/progress_timeout_ms budget before its first step. */
    c->stage_entered_time_ms = c->last_progress_time_ms = now;
    c->last_progress_clicks = c->encoder_clicks;
    km_trajectory_set_limits_at(&c->trajectory, c->base_max_velocity_steps_s,
                                c->base_acceleration_steps_s2, c->base_deceleration_steps_s2, us);
    if (engage_feeder(c, us) != KM_OK || c->trajectory.faulted) {
        km_core_explicit_stop(c, KM_FAULT_DRIVER_FAULT, now);
        return;
    }
    km_admission_result_t admission;
    switch (c->active_op) {
    case KM_OP_TYPE_LOAD: admission = enter_load_motion(c, c->select_flag_a, now, us); break;
    case KM_OP_TYPE_PRELOAD: admission = enter_preload_motion(c, now, us); break;
    case KM_OP_TYPE_UNLOAD: admission = enter_unload_motion(c, c->select_flag_a, c->select_flag_b, now, us); break;
    case KM_OP_TYPE_EJECT: admission = enter_eject_motion(c, now, us); break;
    case KM_OP_TYPE_CALIBRATE: admission = enter_ptfe_cal_motion(c, c->select_flag_a, now, us); break;
    default: admission = KM_REQ_FAULTED; break;
    }
    if (admission != KM_REQ_ACCEPTED) km_core_explicit_stop(c, KM_FAULT_DRIVER_FAULT, now);
}

/* Shared tail of a successful HOME, whether serviced through the stepper
 * home state machine or completed immediately (SERVO: no axis). travel_steps
 * is HOME_COMPLETE's value. */
static void home_complete(km_operation_core_t *c, uint32_t travel_steps, uint32_t now)
{
    c->selector_homed = true;
    emit(c, KM_EVENT_HOME_COMPLETE, KM_WIRE_OP_CODE_SUCCESS, travel_steps, now);
    release_reservations(c);
    c->shared_path_owned = false;
    c->stage = KM_STAGE_STOPPED;
    c->active_op = KM_OP_TYPE_NONE;
    c->active_client = KM_CLIENT_NONE;
}

/* HOME (klipper_mmu/selector.h) has no lane of its own: the host sends
 * KM_LANE_UNKNOWN (0xff) and it is echoed back verbatim in the terminal
 * event so the host can match it, never validated against lane_count. */
static km_admission_result_t request_home(km_operation_core_t *c, km_request_identity_t r,
                                          km_core_timepoint_t time)
{
    if (!c || r.client <= KM_CLIENT_NONE || r.client >= KM_MAX_CLIENTS || !r.session_id || !r.req_id)
        return KM_REQ_REJECTED_STALE;
    if (!is_valid_time(c, time)) return KM_REQ_REJECTED_STALE;
    if (!c->profile.has_selector) return KM_REQ_UNAVAILABLE;
    km_event_t old;
    if (km_core_find_terminal(c, &r, &old) == KM_OK) return KM_REQ_ALREADY_COMPLETED;
    if (c->terminal_pending && c->active_client == r.client && c->active_session_id == r.session_id
        && c->active_req_id == r.req_id && c->active_op == r.op_type) return KM_REQ_ALREADY_IN_PROGRESS;
    if (c->shared_path_owned && c->active_client != r.client) return KM_REQ_REJECTED_CONFLICT;
    uint32_t session = c->client_session[r.client];
    if (r.session_id < session || (r.session_id == session && r.req_id <= c->client_request[r.client]))
        return KM_REQ_REJECTED_STALE;
    if (c->op_state == KM_OP_STATE_FAULT || c->trajectory.faulted) return KM_REQ_FAULTED;
    if (c->pending_fault_event || !c->motor_ready) return KM_REQ_UNAVAILABLE;
    if (c->stage != KM_STAGE_STOPPED) return KM_REQ_BUSY; /* Refused while any operation runs. */
    if (c->path_occupied) return KM_REQ_PATH_OCCUPIED;
    if (!selector_hardware_ready(c)) return KM_REQ_UNAVAILABLE;
    if (km_event_queue_reserve(&c->event_queue, 1) != KM_OK) return KM_REQ_BUSY;
    commit_time(c, time);
    if (c->terminal_pending) emit(c, KM_EVENT_STOPPED, KM_WIRE_OP_CODE_SUCCESS, 0, time.ms);
    release_reservations(c);
    c->event_reservations = 1;
    c->active_client = r.client; c->active_session_id = r.session_id; c->active_req_id = r.req_id;
    c->active_op = KM_OP_TYPE_HOME; c->active_lane = r.lane_idx;
    c->client_session[r.client] = r.session_id; c->client_request[r.client] = r.req_id;
    c->terminal_pending = true;
    if (c->profile.selector.kind == KM_SELECTOR_SERVO) {
        /* No selector axis to home; selector_at() never needs selector_homed
         * for SERVO (line ~505). HOME just confirms the servo is ready and
         * reports no travel, synchronously, like Happy Hare's own selector
         * has nothing to do here. */
        home_complete(c, 0, time.ms);
        return KM_REQ_ACCEPTED;
    }
    c->home_start_steps = c->selector_position_steps;
    km_result_t hr = c->profile.selector.kind == KM_SELECTOR_INDEXED
        ? indexed_enter_seek(c, 0, time.ms, time.us) /* Always gate 0; selector.h. */
        : begin_select_home(c, time.ms, time.us);
    if (hr != KM_OK) { km_core_explicit_stop(c, KM_FAULT_SELECTOR, time.ms); return KM_REQ_FAULTED; }
    return KM_REQ_ACCEPTED;
}
#endif /* KM_SELECTOR_BUILD */

km_admission_result_t km_core_request_start(km_operation_core_t *c,
                                            const km_request_identity_t *r,
                                            km_direction_t direction, km_core_timepoint_t time)
{
    if (!r) return KM_REQ_UNAVAILABLE;
    switch (r->op_type) {
    case KM_OP_TYPE_LOAD:
        return km_core_request_load(c, r->client, r->session_id, r->req_id, r->lane_idx, time);
    case KM_OP_TYPE_PRELOAD:
        return km_core_request_preload(c, r->client, r->session_id, r->req_id, r->lane_idx, time);
    case KM_OP_TYPE_UNLOAD:
        return request_unload(c, *r, time);
    case KM_OP_TYPE_EJECT:
        return km_core_request_eject(c, r->client, r->session_id, r->req_id, r->lane_idx, time);
    case KM_OP_TYPE_FOLLOW:
        return request_follow(c, *r, direction, time);
    case KM_OP_TYPE_CALIBRATE:
        return km_core_request_ptfe_cal(c, r->client, r->session_id, r->req_id, r->lane_idx, time);
    case KM_OP_TYPE_HOME:
#if KM_SELECTOR_BUILD
        return request_home(c, *r, time);
#else
        return KM_REQ_UNAVAILABLE; /* No selector on this image: same as request_home's own !has_selector guard. */
#endif
    default:
        return KM_REQ_UNAVAILABLE;
    }
}

static void settle(km_operation_core_t *c, km_event_type_t event, uint32_t now, uint32_t us)
{
    km_follower_disarm(&c->follower);
    km_driver_shutdown(c->active_rewinder);
    c->rewind_pending = false;
    if (!velocity(c, 0, now, us)) return;
    c->settling_event = event;
    c->stage = KM_STAGE_BRAKING;
}

/* The tail of a settled BRAKING (or, on a ROTARY selector, a release move
 * after it): terminal event, reservation/path/state cleanup. A selector
 * machine's completed PRELOAD clears path_occupied like UNLOAD/EJECT (item
 * 3: parked behind the shared HUB sensor, not occupying it), so another
 * lane can be selected and preloaded next; non-selector PRELOAD is
 * unaffected (it never sets this settling_event's success bit). */
static void finish_settled_op(km_operation_core_t *c, uint32_t now)
{
    bool success = c->settling_event == KM_EVENT_UNLOAD_COMPLETE
        || c->settling_event == KM_EVENT_EJECT_COMPLETE
        || c->settling_event == KM_EVENT_PTFE_CAL_COMPLETE
        || (c->settling_event == KM_EVENT_PRELOAD_COMPLETE && c->profile.has_selector);
    bool preloaded = c->settling_event == KM_EVENT_PRELOAD_COMPLETE
        || (c->settling_event == KM_EVENT_PRELOAD_CANCELLED
            && c->profile.has_load_sensors && c->path_occupied
            && c->filament_present_bay[c->active_lane]
            && c->filament_present_load[c->active_lane]
            && !c->filament_present_hub && !c->filament_present_toolhead);
    emit(c, c->settling_event,
         success || c->settling_event == KM_EVENT_PRELOAD_COMPLETE
             ? KM_WIRE_OP_CODE_SUCCESS : KM_WIRE_OP_CODE_CANCEL,
         c->settling_event == KM_EVENT_PTFE_CAL_COMPLETE ? c->calibrated_ptfe_value : 0, now);
    release_reservations(c);
    km_driver_shutdown(c->active_feeder);
    c->shared_path_owned = false;
    if (success) c->path_occupied = false;
    c->unload_from_load_preload = false;
    c->op_state = preloaded ? KM_OP_STATE_PRELOADED : (c->path_occupied ? KM_OP_STATE_LOADED : KM_OP_STATE_IDLE);
    c->stage = KM_STAGE_STOPPED; c->active_op = KM_OP_TYPE_NONE;
    c->active_client = KM_CLIENT_NONE;
    project_virtual_hub(c);
}

km_admission_result_t km_core_request_cancel5(km_operation_core_t *c, km_client_id_t client,
                                               uint32_t session, uint32_t req, km_core_timepoint_t time)
{
    if (!c || !session || !req || !c->shared_path_owned) return KM_REQ_REJECTED_STALE;
    if (!is_valid_time(c, time)) return KM_REQ_REJECTED_STALE;
    if (client != c->active_client) return KM_REQ_REJECTED_CONFLICT;
    if (session != c->active_session_id || req != c->active_req_id) return KM_REQ_REJECTED_STALE;
    if (c->op_state == KM_OP_STATE_FAULT) return KM_REQ_FAULTED;
    commit_time(c, time);
    if (c->stage == KM_STAGE_BRAKING) {
        /* Cancellation can override a success being settled, never vice versa. */
        c->settling_event = c->terminal_pending ? terminal_type(c->active_op, true) : KM_EVENT_STOPPED;
        return KM_REQ_ALREADY_IN_PROGRESS;
    }
    settle(c, c->terminal_pending ? terminal_type(c->active_op, true) : KM_EVENT_STOPPED, time.ms, time.us);
    return c->op_state == KM_OP_STATE_FAULT ? KM_REQ_FAULTED : KM_REQ_ACCEPTED;
}

km_admission_result_t km_core_request_cancel4(km_operation_core_t *c, km_client_id_t client,
                                               uint32_t req, km_core_timepoint_t time)
{
    (void)c; (void)client; (void)req; (void)time;
    return KM_REQ_REJECTED_STALE; /* Sessionless cancellation is deliberately unsupported. */
}

km_admission_result_t km_core_request_cancel_target(km_operation_core_t *c,
                                                    const km_request_identity_t *target,
                                                    km_core_timepoint_t time)
{
    if (!c || !target || target->op_type == KM_OP_TYPE_NONE
        || target->op_type != c->active_op || target->lane_idx != c->active_lane)
        return KM_REQ_REJECTED_STALE;
    return km_core_request_cancel5(c, target->client, target->session_id, target->req_id, time);
}

km_result_t km_core_clear_fault(km_operation_core_t *c)
{
    if (!c) return KM_ERR_INVALID_PARAM;
    return c->trajectory.position_uncertain ? KM_ERR_UNAVAILABLE : KM_OK;
}

km_result_t km_core_recover(km_operation_core_t *c, int64_t position, bool occupied,
                            uint8_t lane, km_core_timepoint_t time)
{
    if (!c || lane >= c->profile.lane_count) return KM_ERR_INVALID_PARAM;
    if (!is_valid_time(c, time)) return KM_ERR_INVALID_PARAM;
    if (!virtual_other_lanes_clear(c, lane, time.ms)
        || (c->profile.load_is_virtual_hub
            && (!fresh(c->load_valid[lane], c->load_sample_ms[lane], time.ms,
                       c->profile.max_sensor_age_ms)
                || (!occupied && c->filament_present_load[lane]))))
        return KM_ERR_UNAVAILABLE;
    if (c->pending_fault_event || !c->host_connected || c->terminal_pending) return KM_ERR_UNAVAILABLE;
    for (unsigned i = 0; i < c->profile.lane_count; ++i) {
        if ((c->feeder_drivers[i] && (c->feeder_drivers[i]->enabled || km_driver_get_fault(c->feeder_drivers[i]) != KM_FAULT_NONE))
            || (c->rewinder_drivers[i] && (c->rewinder_drivers[i]->enabled || km_driver_get_fault(c->rewinder_drivers[i]) != KM_FAULT_NONE)))
            return KM_ERR_FAULTED;
    }
    if (c->buffer_sensor.fault != KM_FAULT_NONE) return KM_ERR_FAULTED;
    km_result_t result = km_trajectory_recover(&c->trajectory, position, time.us);
    if (result != KM_OK) return result;
    commit_time(c, time);
    release_reservations(c);
    c->system_fault = c->motion_fault = KM_FAULT_NONE;
    c->op_state = occupied ? KM_OP_STATE_LOADED : KM_OP_STATE_IDLE;
    c->stage = KM_STAGE_STOPPED;
    c->shared_path_owned = false; c->path_occupied = occupied;
    c->active_lane = c->path_occupant_lane = lane;
    project_virtual_hub(c);
    c->active_client = KM_CLIENT_NONE; c->active_op = KM_OP_TYPE_NONE;
    c->active_session_id = c->active_req_id = 0;
    return init_follower(c);
}

static bool output_quiet(const km_driver_t *d)
{
    return !d || (!d->enabled && !d->step_pin_high && !d->pwm_duty_permille
                  && km_driver_get_fault(d) == KM_FAULT_NONE);
}

/* A stale reading outranks a fresh contradiction; neither is overwritten by OK. */
static void check_reading(const km_operation_core_t *c, km_result_t *result, bool valid,
                          uint32_t sample, bool present, bool expected, uint32_t now)
{
    if (!fresh(valid, sample, now, c->profile.max_sensor_age_ms)) *result = KM_ERR_UNAVAILABLE;
    else if (present != expected && *result == KM_OK) *result = KM_ERR_CONFLICT;
}

km_result_t km_core_reconcile_path(km_operation_core_t *c, bool occupied,
                                   uint8_t lane, km_core_timepoint_t time)
{
    if (!c || !is_valid_time(c, time)
        || (occupied ? lane >= c->profile.lane_count : lane != KM_LANE_UNKNOWN))
        return KM_ERR_INVALID_PARAM;
    const km_device_profile_t *p = &c->profile;
    /* A stopped operation fault is reconciled the same way: its latched motion
     * state and owner identity are what the operator's statement replaces. */
    bool faulted = c->op_state == KM_OP_STATE_FAULT;
    if ((!faulted && (c->system_fault != KM_FAULT_NONE || c->motion_fault != KM_FAULT_NONE
                      || c->trajectory.faulted || c->trajectory.position_uncertain
                      || c->follower.state == KM_FOLLOWER_FAULT))
        || c->pending_fault_event || c->buffer_sensor.fault != KM_FAULT_NONE)
        return KM_ERR_FAULTED;
    /* Every event, the fault's included, must already be acknowledged. */
    if ((!faulted && (c->shared_path_owned || c->active_client != KM_CLIENT_NONE
                      || c->active_op != KM_OP_TYPE_NONE
                      || c->follower.state != KM_FOLLOWER_DISARMED))
        || c->terminal_pending || c->stage != KM_STAGE_STOPPED
        || c->rewind_pending || c->event_reservations || km_event_queue_count(&c->event_queue)
        || km_trajectory_is_moving(&c->trajectory))
        return KM_ERR_BUSY;
    /* Outputs must already be off and every driver healthy; reconciliation
     * never shuts them down or clears a driver's own latch. */
    if (!output_quiet(c->selector_driver)) return KM_ERR_FAULTED;
    for (unsigned i = 0; i < p->lane_count; ++i)
        if (!output_quiet(c->feeder_drivers[i]) || !output_quiet(c->rewinder_drivers[i]))
            return KM_ERR_FAULTED;
    if (!faulted && (c->op_state != KM_OP_STATE_IDLE || !c->path_occupied
                     || c->path_occupant_lane != KM_LANE_UNKNOWN)) return KM_ERR_STALE;
    if (!p->has_hub_sensor && (occupied || !p->has_toolhead_sensor)) return KM_ERR_NOT_SUPPORTED;

    /* BAY presence alone never selects a lane; a fresh absent BAY or LOAD on
     * the selected lane does contradict the operator's assertion. */
    km_result_t result = KM_OK;
    if (p->has_toolhead_sensor)
        check_reading(c, &result, c->toolhead_sensor_valid, c->path_sample_ms,
                      c->filament_present_toolhead,
                      occupied && c->filament_present_toolhead, time.ms);
    if (p->has_hub_sensor && !p->load_is_virtual_hub)
        check_reading(c, &result, c->hub_sensor_valid, c->path_sample_ms,
                      c->filament_present_hub, occupied, time.ms);
    for (uint8_t i = 0; i < p->lane_count; ++i) {
        bool selected = occupied && i == lane;
        if (p->load_is_virtual_hub || (selected && p->has_load_sensors))
            check_reading(c, &result, c->load_valid[i], c->load_sample_ms[i],
                          c->filament_present_load[i], selected, time.ms);
        if (selected && p->has_bay_sensors)
            check_reading(c, &result, c->bay_valid[i], c->bay_sample_ms[i],
                          c->filament_present_bay[i], true, time.ms);
    }
    if (result != KM_OK) return result;
    if (faulted) {
        /* Keep the confirmed step count; clear only the uncertainty. Session
         * and request high-water marks stay, so old requests remain stale. */
        km_trajectory_t trajectory = c->trajectory;
        km_follower_t follower = c->follower;
        result = km_trajectory_recover(&trajectory, km_trajectory_get_position(&trajectory), time.us);
        if (result == KM_OK) result = init_follower(c);
        if (result != KM_OK) { c->follower = follower; return result; }
        c->trajectory = trajectory;
        c->system_fault = c->motion_fault = KM_FAULT_NONE;
        c->shared_path_owned = false; c->unload_from_load_preload = false;
        c->active_client = KM_CLIENT_NONE; c->active_op = KM_OP_TYPE_NONE;
        c->active_session_id = c->active_req_id = 0;
        c->op_state = KM_OP_STATE_IDLE;
    }
    commit_time(c, time);
    c->path_occupied = occupied;
    if (occupied) {
        c->path_occupant_lane = lane;
        c->op_state = KM_OP_STATE_LOADED;
    }
    project_virtual_hub(c);
    return KM_OK;
}

void km_core_host_comm_lost(km_operation_core_t *c, uint32_t now)
{
    if (!c) return;
    c->host_connected = false;
    km_core_explicit_stop(c, KM_FAULT_HOST_COMM_LOST, now);
}
void km_core_host_comm_ready(km_operation_core_t *c, uint32_t now)
{
    if (c) {
        c->host_connected = true;
        c->last_host_heartbeat_ms = now;
    } /* Reconnect neither clears faults nor resumes output. */
}

static uint32_t stage_timeout(const km_operation_core_t *c)
{
    switch (c->stage) {
    case KM_STAGE_LOAD_FEED_TO_HUB: return c->profile.hub_feed_timeout_ms << load_slow(c);
    case KM_STAGE_LOAD_FEED_TO_BUFFER: return c->profile.buffer_feed_timeout_ms << load_slow(c);
    case KM_STAGE_LOAD_RETRY_RETRACT:
    case KM_STAGE_UNLOAD_RETRACT_TO_HUB: return c->profile.unload_retract_timeout_ms;
    case KM_STAGE_UNLOAD_RETRACT_TO_LOAD: return c->profile.unload_retract_timeout_ms;
    case KM_STAGE_UNLOAD_STOP_SETTLE: return c->profile.unload_rewind_timeout_ms;
    case KM_STAGE_UNLOAD_REWIND_TO_SPOOL: return c->profile.unload_rewind_timeout_ms;
    case KM_STAGE_EJECT_TO_BAY: return c->profile.has_bay_sensors
        ? c->profile.unload_retract_timeout_ms : c->profile.eject_timeout_ms;
    case KM_STAGE_CALIBRATE_PROBE: return c->profile.ptfe_cal_probe_timeout_ms;
    case KM_STAGE_CALIBRATE_RETRACT: return c->profile.ptfe_cal_retract_timeout_ms;
    case KM_STAGE_SELECT_MOVE: return c->profile.selector.home.deadline_us / 1000u;
    default: return 0;
    }
}

/* Record a calibration result and retract until HUB and toolhead clear. */
static void calibrate_retract(km_operation_core_t *c, uint32_t value, uint32_t now, uint32_t us)
{
    c->calibrated_ptfe_value = value;
    /* A step-estimated CALIBRATE measured the calibrated LOAD's HUB-to-arrival
     * distance: adopt it at once, so the next LOAD of this session aims at it
     * without a restart. The encoder path measures clicks, not travel. */
    if (c->cal_step_estimate) c->calibrated_arrival_steps = value;
    c->stage = KM_STAGE_CALIBRATE_RETRACT;
    c->stage_entered_time_ms = c->last_progress_time_ms = now;
    (void)velocity(c, -(int32_t)c->profile.default_rewind_velocity_steps_s, now, us);
}

/* Calibrated FPS arrival during FEED_TO_BUFFER. Returns <0 after the attempt
 * ended, 0 while pending and >0 once a sustained response qualifies. Only a
 * newer acquisition is evidence; re-reading one sample never extends the hold. */
static int fps_update(km_operation_core_t *c, uint32_t now, uint32_t us)
{
    const km_device_profile_t *p = &c->profile;
    km_buffer_sensor_t *b = &c->buffer_sensor;
    int64_t travel = km_trajectory_get_position(&c->trajectory) - c->fps_hub_steps;
    /* With a calibrated distance the arrival is S +/-, not the outer travel
     * spread: the follower is already at the crawl well before S - window
     * (load_approach), so compression before the window is a real
     * obstruction and travel past it is a real overshoot. CALIBRATE, which
     * has no S yet, still probes the outer limits. */
    uint32_t min_steps = p->fps_arrival_min_steps;
    uint32_t max_steps = p->fps_arrival_max_steps;
    if (c->calibrated_arrival_steps) {
        min_steps = c->calibrated_arrival_steps > p->load_window_steps
            ? c->calibrated_arrival_steps - p->load_window_steps : 0;
        max_steps = c->calibrated_arrival_steps + p->load_window_steps;
    }
    km_fault_t fault = KM_FAULT_NONE;
    /* Stale or off-path readings, or full compression (the calibrated end),
     * cannot bound the response. Uncompressed (0) is the normal rest. A lost
     * HUB means runout or broken filament. */
    if (!km_buffer_check_freshness(b, now) || b->position_permille >= KM_PERMILLE_MAX)
        fault = KM_FAULT_BUFFER_SIGNAL;
    else if (!c->filament_present_hub) fault = KM_FAULT_FILAMENT_LOST;
    else if (travel >= (int64_t)max_steps) fault = KM_FAULT_NO_PROGRESS;
    if (fault != KM_FAULT_NONE) { load_fail(c, fault, now, us); return -1; }
    if (!c->fps_min_reached && travel >= (int64_t)min_steps) {
        c->fps_min_reached = true;
        c->fps_min_ms = now;
    }
    uint32_t sample = b->last_sample_time_ms;
    if (!km_time_after(sample, c->fps_sample_ms)) return 0;
    c->fps_sample_ms = sample;
    if (b->position_permille < p->fps_arrival_permille) {
        /* Arrival may only start from release after minimum travel. */
        c->fps_armed = c->fps_min_reached && km_time_after(sample, c->fps_min_ms);
        c->fps_count = 0;
        return 0;
    }
    if (!c->fps_count) c->fps_start_ms = sample;
    if (c->fps_count < p->fps_arrival_samples) ++c->fps_count;
    if (c->fps_count < p->fps_arrival_samples
        || km_time_elapsed(sample, c->fps_start_ms) < p->fps_arrival_hold_ms) return 0;
    /* The same sustained compression begun before minimum travel is an
     * obstruction before the extruder. */
    fault = !c->fps_armed ? KM_FAULT_JAM
        : p->has_toolhead_sensor && !c->filament_present_toolhead ? KM_FAULT_SENSOR_FAILURE
        : KM_FAULT_NONE;
    if (fault == KM_FAULT_NONE) return 1;
    load_fail(c, fault, now, us);
    return -1;
}

#if KM_SELECTOR_BUILD
/* SELECT_MOVE's pressed-switch backoff (begin_select_home) has reached its
 * target and cleared the switch, or is still pressed (stuck): start real
 * homing, or fault. Shared by a lane selection's own SELECT_MOVE dispatch and
 * a standalone HOME's, which cannot fall through to that dispatch (below)
 * because HOME's active_lane (KM_LANE_UNKNOWN) must never reach the
 * lane-indexed checks between them. */
static void finish_select_backoff(km_operation_core_t *c, uint32_t now, uint32_t us)
{
    c->select_backoff = false;
    if ((c->selector_endstop_valid && c->selector_endstop_asserted)
        || enter_select_home(c, now, us) != KM_OK)
        km_core_explicit_stop(c, KM_FAULT_SELECTOR, now);
}
#endif /* KM_SELECTOR_BUILD */

void km_core_update(km_operation_core_t *c, uint32_t now, uint32_t us)
{
    if (!c) return;
    km_core_timepoint_t time = {now, us};
    if (!is_valid_time(c, time)) {
        km_core_explicit_stop(c, KM_FAULT_DRIVER_FAULT, c->last_control_ms);
        return;
    }
    commit_time(c, time);
    
    if (c->pending_fault_event && km_event_queue_push(&c->event_queue, &c->fault_event) == KM_OK)
        c->pending_fault_event = false;
    if (c->op_state == KM_OP_STATE_FAULT || c->stage == KM_STAGE_STOPPED) {
        if (c->stage == KM_STAGE_STOPPED && c->op_state != KM_OP_STATE_FAULT) {
            km_trajectory_update(&c->trajectory, us);
        }
        return;
    }
    if (c->active_client == KM_CLIENT_HOST && (!c->host_connected
        || km_time_elapsed(now, c->last_host_heartbeat_ms) >= c->profile.host_watchdog_ms)) {
        km_core_host_comm_lost(c, now); return;
    }
    /* HOME has no lane (active_lane may be KM_LANE_UNKNOWN): handled on its
     * own, never through the lane-indexed checks below. Unreachable on a
     * KM_SELECTOR_BUILD==0 image: request_home() (gated with the rest of the
     * selection phase) is the only place active_op becomes KM_OP_TYPE_HOME. */
#if KM_SELECTOR_BUILD
    if (c->active_op == KM_OP_TYPE_HOME) {
        if (c->stage == KM_STAGE_SELECT_MOVE) {
            /* INDEXED's own back-off/centering (indexed_enter_seek/_found);
             * every other kind's SELECT_MOVE here is begin_select_home's
             * pressed-switch backoff (never a lane engagement or release). */
            if (km_trajectory_is_moving(&c->trajectory) || c->trajectory.stepper_active) return;
            c->selector_position_steps = c->select_target_steps;
            if (c->profile.selector.kind == KM_SELECTOR_INDEXED) { indexed_after_move(c, now, us); return; }
            finish_select_backoff(c, now, us);
            return;
        }
        km_selector_home_switch_t sw = c->profile.selector.kind == KM_SELECTOR_INDEXED
            ? gate_switch(c, c->select_seek_gate)
            : (km_selector_home_switch_t){c->selector_endstop_asserted, c->selector_endstop_sample_us,
                                          c->selector_endstop_valid};
        km_selector_home_service(&c->selector_home, sw, us);
        if (c->selector_home.state == KM_SELECTOR_HOME_RUNNING) return;
        if (c->selector_home.state == KM_SELECTOR_HOME_HOMED) {
            if (c->profile.selector.kind == KM_SELECTOR_INDEXED) { indexed_found(c, now, us); return; }
            /* Net travel from where this HOME started (before any
             * pressed-switch backoff, home_start_steps) to the switch
             * (found_position, the same continuous frame -- nothing rebinds
             * the trajectory between the two): a selector that started right
             * on the switch backs off and returns to about the same spot,
             * reporting ~0 instead of the backoff-and-return distance. */
            int64_t delta = c->selector_home.found_position - c->home_start_steps;
            uint32_t travel_steps = (uint32_t)(delta < 0 ? -delta : delta);
            c->selector_position_steps = 0;
            home_complete(c, travel_steps, now);
            return;
        }
        c->selector_homed = false;
        km_core_explicit_stop(c, KM_FAULT_SELECTOR, now);
        return;
    }
#endif /* KM_SELECTOR_BUILD */
    km_fault_t fault = km_driver_get_fault(c->active_feeder);
    if (fault == KM_FAULT_NONE) fault = km_driver_get_fault(c->active_rewinder);
    if (fault != KM_FAULT_NONE) { km_core_explicit_stop(c, fault, now); return; }
    if (!sensors_ready(c, c->active_lane, now)
        || !virtual_other_lanes_clear(c, c->active_lane, now)) {
        km_core_explicit_stop(c, KM_FAULT_SENSOR_FAILURE, now); return;
    }
    if (c->active_op == KM_OP_TYPE_PRELOAD && c->profile.has_load_sensors
        && (!c->filament_present_bay[c->active_lane]
            || (c->filament_present_hub && !c->profile.load_is_virtual_hub)
            || c->filament_present_toolhead)) {
        km_core_explicit_stop(c, KM_FAULT_SENSOR_FAILURE, now); return;
    }
    if (c->active_op == KM_OP_TYPE_LOAD && c->profile.has_load_sensors
        && !c->profile.load_is_virtual_hub
        && c->stage == KM_STAGE_LOAD_FEED_TO_HUB
        && !c->filament_present_load[c->active_lane]) {
        km_core_explicit_stop(c, KM_FAULT_SENSOR_FAILURE, now); return;
    }
    if (c->active_op == KM_OP_TYPE_UNLOAD && c->unload_from_load_preload
        && (!c->filament_present_bay[c->active_lane] || c->filament_present_hub
            || c->filament_present_toolhead)) {
        km_core_explicit_stop(c, KM_FAULT_SENSOR_FAILURE, now); return;
    }
    if (c->active_op == KM_OP_TYPE_UNLOAD && !c->unload_from_load_preload
        && (c->stage == KM_STAGE_UNLOAD_STOP_SETTLE
            || c->stage == KM_STAGE_UNLOAD_REWIND_TO_SPOOL)
        && (c->filament_present_hub || c->filament_present_toolhead)) {
        km_core_explicit_stop(c, KM_FAULT_SENSOR_FAILURE, now); return;
    }
    if (c->active_op == KM_OP_TYPE_EJECT && !eject_path_clear(c, c->active_lane)) {
        km_core_explicit_stop(c, KM_FAULT_SENSOR_FAILURE, now); return;
    }
    km_trajectory_update(&c->trajectory, us);
    if (c->trajectory.faulted) { km_core_explicit_stop(c, KM_FAULT_DRIVER_FAULT, now); return; }
    /* Checked before every load_fail() of this update, so a spent budget
     * never starts another attempt. Cancellation keeps its braking. */
    if (c->op_state == KM_OP_STATE_LOADING && c->stage != KM_STAGE_BRAKING
        && km_time_elapsed(now, c->load_started_ms) >= KM_LOAD_BUDGET_MS) {
        km_core_explicit_stop(c, KM_FAULT_NO_PROGRESS, now); return;
    }
    uint32_t timeout = stage_timeout(c);
    if (timeout && km_time_elapsed(now, c->stage_entered_time_ms) >= timeout) {
        load_fail(c, KM_FAULT_NO_PROGRESS, now, us); return;
    }
    /* KM_STAGE_SELECT_HOME/SELECT_MOVE/SERVO_DWELL (klipper_mmu/selector.h)
     * only exist between request_home()/start_select() and the operation
     * they gate; both are unreachable on a KM_SELECTOR_BUILD==0 image, so
     * c->stage never takes these values there either. */
#if KM_SELECTOR_BUILD
    if (c->stage == KM_STAGE_SELECT_HOME) {
        km_selector_home_switch_t sw = c->profile.selector.kind == KM_SELECTOR_INDEXED
            ? gate_switch(c, c->select_seek_gate)
            : (km_selector_home_switch_t){c->selector_endstop_asserted, c->selector_endstop_sample_us,
                                          c->selector_endstop_valid};
        km_selector_home_service(&c->selector_home, sw, us);
        if (c->selector_home.state == KM_SELECTOR_HOME_RUNNING) return;
        if (c->selector_home.state == KM_SELECTOR_HOME_HOMED) {
            if (c->profile.selector.kind == KM_SELECTOR_INDEXED) { indexed_found(c, now, us); return; }
            c->selector_homed = true;
            c->selector_engaged = false; /* Freshly homed; not yet at any gate. */
            c->selector_position_steps = 0;
            if (enter_select_move(c, c->profile.selector.gate_steps[c->active_lane], now, us) != KM_OK)
                km_core_explicit_stop(c, KM_FAULT_SELECTOR, now);
            return;
        }
        c->selector_homed = false;
        km_core_explicit_stop(c, KM_FAULT_SELECTOR, now);
        return;
    }
    if (c->stage == KM_STAGE_SELECT_MOVE) {
        /* The rate core settles on the gate before every queued selector step
         * is delivered; the gear must not take the trajectory until they are. */
        if (km_trajectory_is_moving(&c->trajectory) || c->trajectory.stepper_active) return;
        c->selector_position_steps = c->select_target_steps;
        if (c->profile.selector.kind == KM_SELECTOR_INDEXED) { indexed_after_move(c, now, us); return; }
        if (c->select_backoff) { finish_select_backoff(c, now, us); return; }
        if (c->select_releasing) {
            /* ROTARY release_gate move after UNLOAD/EJECT/PRELOAD completed;
             * the lane is released (not engaged) and the operation now
             * finishes its terminal event. */
            c->select_releasing = false;
            c->selector_engaged = false;
            finish_settled_op(c, now);
            return;
        }
        if (c->profile.selector.kind == KM_SELECTOR_LINEAR && c->profile.selector.has_servo) {
            if (enter_select_servo(c, c->profile.selector.servo_engage_us, now) != KM_OK)
                km_core_explicit_stop(c, KM_FAULT_SELECTOR, now);
            return;
        }
        /* ROTARY has no grip; arriving at the gate position is the engagement. */
        c->selector_gate = c->active_lane;
        c->selector_engaged = true;
        resume_operation_motion(c, now, us);
        return;
    }
    if (c->stage == KM_STAGE_SERVO_DWELL) {
        if (km_time_elapsed(now, c->stage_entered_time_ms) < c->profile.selector.servo_dwell_ms) return;
        if (!c->profile.selector.servo_hold && c->selector_servo && c->selector_servo->set_pulse_us)
            (void)c->selector_servo->set_pulse_us(c->selector_servo, 0);
        c->selector_gate = c->active_lane;
        c->selector_engaged = true;
        resume_operation_motion(c, now, us);
        return;
    }
#endif /* KM_SELECTOR_BUILD */
    if (c->stage == KM_STAGE_BRAKING) {
        /* A cancelled preload may reach LOAD while the feeder is braking.
         * Retain that lane before cancellation releases operation ownership. */
        if (c->active_op == KM_OP_TYPE_PRELOAD && c->profile.has_load_sensors
            && c->filament_present_load[c->active_lane]) {
            c->path_occupied = true;
            c->path_occupant_lane = c->active_lane;
        }
        if (km_trajectory_is_moving(&c->trajectory)) return;
        if (c->settling_event == KM_EVENT_PRELOAD_COMPLETE
            && (c->profile.has_load_sensors
                ? (c->profile.load_is_virtual_hub
                    ? c->filament_present_load[c->active_lane] || c->filament_present_toolhead
                    : !c->filament_present_load[c->active_lane] || c->filament_present_hub
                    || c->filament_present_toolhead)
                : (preload_rests_behind_hub(&c->profile)
                    /* Retracted until the shared HUB cleared: it must stay
                     * clear through braking, the opposite of the parked-on-
                     * the-HUB invariant just below (reachable only without a
                     * HUB sensor at all, since has_load_sensors is already
                     * false on this side and load_is_virtual_hub implies
                     * has_load_sensors). */
                    ? (c->filament_present_hub || c->filament_present_toolhead)
                    : (!c->filament_present_hub
                        || (c->profile.has_toolhead_sensor && c->filament_present_toolhead))))) {
            km_core_explicit_stop(c, KM_FAULT_SENSOR_FAILURE, now);
            return;
        }
        if (c->settling_event == KM_EVENT_UNLOAD_COMPLETE
            && (c->unload_from_load_preload
                ? c->filament_present_load[c->active_lane]
                : (c->filament_present_hub || c->filament_present_toolhead))) {
            km_core_explicit_stop(c, KM_FAULT_SENSOR_FAILURE, now);
            return;
        }
        if (c->settling_event == KM_EVENT_EJECT_COMPLETE
            && c->filament_present_bay[c->active_lane]) {
            km_core_explicit_stop(c, KM_FAULT_SENSOR_FAILURE, now);
            return;
        }
        /* UNLOAD, EJECT and PRELOAD release the selector on completion; LOAD
         * and CALIBRATE stay engaged (FOLLOW, and a retried feed, need the
         * gear). LINEAR/SERVO release by pulse immediately; ROTARY moves to
         * release_gate[lane]'s position first (Happy Hare _grip_release),
         * finishing the terminal event only once that move lands. */
#if KM_SELECTOR_BUILD
        /* has_selector is always false on a KM_SELECTOR_BUILD==0 image. */
        if (c->profile.has_selector && (c->settling_event == KM_EVENT_PRELOAD_COMPLETE
            || c->settling_event == KM_EVENT_UNLOAD_COMPLETE
            || c->settling_event == KM_EVENT_EJECT_COMPLETE)) {
            if (c->profile.selector.kind == KM_SELECTOR_ROTARY) {
                uint8_t release_lane = c->profile.selector.release_gate[c->active_lane];
                c->select_releasing = true;
                if (enter_select_move(c, c->profile.selector.gate_steps[release_lane], now, us) == KM_OK)
                    return; /* KM_STAGE_SELECT_MOVE finishes with finish_settled_op. */
                c->select_releasing = false; /* Move refused; finalize without it. */
            } else {
                if (c->profile.selector.has_servo && c->profile.selector.servo_release_us
                    && c->selector_servo && c->selector_servo->set_pulse_us)
                    (void)c->selector_servo->set_pulse_us(c->selector_servo, c->profile.selector.servo_release_us);
                c->selector_engaged = false;
            }
        }
#endif /* KM_SELECTOR_BUILD */
        finish_settled_op(c, now);
        return;
    }
    if (c->active_rewinder && c->active_rewinder->enabled && c->profile.has_current_sensing) {
        uint16_t duty = 0;
        km_result_t result = km_current_regulator_update_sample(&c->current_regulator, c->measured_rewind_ma,
            c->rewind_sample_valid, c->rewind_sample_ms, now, &duty);
        if (result != KM_OK) { km_core_explicit_stop(c, c->current_regulator.fault, now); return; }
        if (km_driver_set_pwm_duty(c->active_rewinder, duty) != KM_OK) {
            km_core_explicit_stop(c, KM_FAULT_DRIVER_FAULT, now); return;
        }
    }
    if (c->stage == KM_STAGE_UNLOAD_STOP_SETTLE) {
        if (km_trajectory_is_moving(&c->trajectory)) return;
        if (c->rewind_pending) {
            c->rewind_pending = false;
            if (!start_rewind(c, now, us)) return;
        }
        int64_t origin = km_trajectory_get_model_position(&c->trajectory);
        uint32_t clear_steps = c->profile.unload_hub_clear_steps;
        if (clear_steps > (uint64_t)(origin - KM_MOTION_MIN_POSITION_STEPS)) {
            km_core_explicit_stop(c, KM_FAULT_DRIVER_FAULT, now); return;
        }
        int64_t target = origin - (int64_t)clear_steps;
        if (km_trajectory_set_target_position(&c->trajectory, target,
                c->profile.default_rewind_velocity_steps_s, us) != KM_OK) {
            km_core_explicit_stop(c, KM_FAULT_DRIVER_FAULT, now); return;
        }
        c->stage = KM_STAGE_UNLOAD_REWIND_TO_SPOOL;
        c->stage_entered_time_ms = now;
        return;
    }
    if (c->stage == KM_STAGE_UNLOAD_REWIND_TO_SPOOL) {
        if (km_trajectory_is_moving(&c->trajectory)) return;
        km_driver_shutdown(c->active_rewinder);
        c->rewind_pending = false;
        if (!velocity(c, 0, now, us)) return;
        c->settling_event = KM_EVENT_UNLOAD_COMPLETE;
        c->stage = KM_STAGE_BRAKING;
        return;
    }
    if (c->stage == KM_STAGE_LOAD_RETRY_RETRACT) {
        /* Once stopped, retract with the powered rewind: to the unload HUB
         * clearance behind the HUB observation after HUB, else by that
         * clearance but not behind where the feed began. Then feed again. */
        if (km_trajectory_is_moving(&c->trajectory)) return;
        int64_t position = km_trajectory_get_position(&c->trajectory);
        int64_t clear = (int64_t)c->profile.unload_hub_clear_steps;
        int64_t target = c->fps_hub_steps - clear;
        if (c->load_attempts[c->load_attempt_count - 1u].stage == KM_STAGE_LOAD_FEED_TO_HUB)
            target = position - clear > c->fps_hub_steps ? position - clear : c->fps_hub_steps;
        if (c->rewind_pending && target < position) {
            c->rewind_pending = false;
            if (!start_rewind(c, now, us)) return;
            if (km_trajectory_set_target_position(&c->trajectory, target,
                    c->profile.default_rewind_velocity_steps_s, us) != KM_OK)
                km_core_explicit_stop(c, KM_FAULT_DRIVER_FAULT, now);
            return;
        }
        km_driver_shutdown(c->active_rewinder);
        c->rewind_pending = false;
        c->stage = KM_STAGE_LOAD_FEED_TO_HUB;
        c->stage_entered_time_ms = c->last_progress_time_ms = now;
        c->last_progress_clicks = c->encoder_clicks;
        fps_begin(c, now);
        uint32_t feed = c->profile.default_feed_velocity_steps_s;
        (void)velocity(c, (int32_t)(load_slow(c) ? (feed + 1u) / 2u : feed), now, us);
        return;
    }
    if (c->rewind_pending && km_trajectory_get_velocity(&c->trajectory) <= 0) {
        c->rewind_pending = false;
        if (!start_rewind(c, now, us)) return;
    }
    if (c->stage == KM_STAGE_FOLLOW) {
        km_follower_inputs_t in = {0};
        in.has_bay_sensor = c->profile.has_bay_sensors; in.has_hub_sensor = c->profile.has_hub_sensor;
        in.has_current_setpoint = c->active_feeder->caps.supports_current_setpoint;
        in.buffer_valid = km_buffer_check_freshness(&c->buffer_sensor, now);
        in.buffer_position_permille = c->buffer_sensor.position_permille;
        in.filament_present_bay = c->filament_present_bay[c->active_lane];
        in.filament_present_hub = c->filament_present_hub;
        in.encoder_clicks = c->encoder_clicks; in.has_encoder = c->profile.has_encoder; in.encoder_valid = c->encoder_valid;
        in.gear_position_steps = (int32_t)km_trajectory_get_position(&c->trajectory);
        km_follower_command_t cmd = km_follower_update(&c->follower, &in, now);
        if (cmd.fault != KM_FAULT_NONE) { km_core_explicit_stop(c, cmd.fault, now); return; }
        int32_t target = cmd.motor_enabled ? (int32_t)cmd.target_velocity_steps_s : 0;
        if (cmd.direction == KM_DIR_REVERSE) target = -target;
        if (!velocity(c, target, now, us)) return;
        if (cmd.motor_enabled && in.has_current_setpoint
            && km_driver_set_current_ma(c->active_feeder, cmd.current_setpoint_ma,
                                        c->profile.default_hold_current_ma) != KM_OK)
            km_core_explicit_stop(c, KM_FAULT_DRIVER_FAULT, now);
        return;
    }
    if (c->profile.has_encoder) {
        if (c->encoder_clicks != c->last_progress_clicks) {
            c->last_progress_clicks = c->encoder_clicks; c->last_progress_time_ms = now;
        } else if (km_time_elapsed(now, c->last_progress_time_ms) >= c->profile.progress_timeout_ms) {
            km_core_explicit_stop(c, KM_FAULT_NO_PROGRESS, now); return;
        }
    }
    bool arrived = c->filament_present_toolhead;
    if (c->stage == KM_STAGE_LOAD_FEED_TO_BUFFER && c->profile.fps_arrival) {
        if (!load_approach(c, now, us)) return;
        int fps = fps_update(c, now, us);
        if (fps < 0) return;
        arrived = fps > 0;
    }
    if (c->stage == KM_STAGE_LOAD_FEED_TO_HUB
        && (c->active_op == KM_OP_TYPE_PRELOAD && c->profile.has_load_sensors
            ? c->filament_present_load[c->active_lane] : c->filament_present_hub)) {
        c->path_occupied = true; c->path_occupant_lane = c->active_lane;
        if (c->active_op == KM_OP_TYPE_PRELOAD) {
            if (c->profile.load_is_virtual_hub) {
                c->stage = KM_STAGE_UNLOAD_RETRACT_TO_LOAD;
                c->stage_entered_time_ms = now;
                (void)velocity(c, -(int32_t)c->profile.default_rewind_velocity_steps_s, now, us);
            } else if (preload_rests_behind_hub(&c->profile)) {
                /* Retract until the shared HUB clears (Happy Hare preloads
                 * every such gate, parking it behind the shared exit): a
                 * selector machine's PRELOAD then finishes with the path
                 * clear (finish_settled_op), not occupied, so the selector
                 * can move on to another lane; a non-selector device with
                 * the same no-LOAD-switch, real-HUB shape rests the same
                 * way but keeps the path reserved for its own next LOAD. */
                c->stage = KM_STAGE_UNLOAD_RETRACT_TO_HUB;
                c->stage_entered_time_ms = now;
                (void)velocity(c, -(int32_t)c->profile.default_rewind_velocity_steps_s, now, us);
            } else settle(c, KM_EVENT_PRELOAD_COMPLETE, now, us);
        } else {
            c->stage = KM_STAGE_LOAD_FEED_TO_BUFFER;
            c->stage_entered_time_ms = c->last_progress_time_ms = now;
            fps_begin(c, now);
            /* Past the HUB the calibrated LOAD opens at cruise (a ceiling,
             * not a target: load_approach() slows it for the window) and a
             * step-estimated CALIBRATE crawls the rest of the path, so the
             * distance it measures is the arrival approach. */
            if (c->profile.fps_arrival) {
                (void)velocity(c, (int32_t)(c->active_op == KM_OP_TYPE_LOAD
                    ? c->profile.load_cruise_velocity_steps_s
                    : c->profile.load_crawl_velocity_steps_s), now, us);
            }
        }
    } else if (c->stage == KM_STAGE_LOAD_FEED_TO_BUFFER && arrived
               && c->active_op == KM_OP_TYPE_CALIBRATE) {
        /* Completed pulses since HUB, below fps_arrival_max_steps: an
         * estimate of path length, never an encoder measurement. */
        calibrate_retract(c, (uint32_t)(km_trajectory_get_position(&c->trajectory)
                                        - c->fps_hub_steps), now, us);
    } else if (c->stage == KM_STAGE_LOAD_FEED_TO_BUFFER && arrived) {
        if (!km_buffer_check_freshness(&c->buffer_sensor, now)
            || km_follower_arm(&c->follower, KM_DIR_FORWARD, KM_FOLLOWER_DRIVE_STEP_VELOCITY, now) != KM_OK) {
            km_core_explicit_stop(c, KM_FAULT_BUFFER_SIGNAL, now); return;
        }
        if (!velocity(c, 0, now, us)) return;
        record_attempt(c, KM_FAULT_NONE); /* HUB-to-arrival travel. */
        emit(c, KM_EVENT_LOAD_COMPLETE, KM_WIRE_OP_CODE_SUCCESS, 0, now);
        c->op_state = KM_OP_STATE_FOLLOWING; c->stage = KM_STAGE_FOLLOW;
    } else if (c->stage == KM_STAGE_UNLOAD_RETRACT_TO_LOAD
               && c->active_op == KM_OP_TYPE_PRELOAD
               && !c->filament_present_load[c->active_lane]) {
        settle(c, KM_EVENT_PRELOAD_COMPLETE, now, us);
    } else if (c->stage == KM_STAGE_UNLOAD_RETRACT_TO_HUB
               && c->active_op == KM_OP_TYPE_PRELOAD
               && !c->filament_present_hub && !c->filament_present_toolhead) {
        settle(c, KM_EVENT_PRELOAD_COMPLETE, now, us);
    } else if ((c->stage == KM_STAGE_UNLOAD_RETRACT_TO_LOAD
                && !c->filament_present_load[c->active_lane])
               || (c->stage == KM_STAGE_UNLOAD_RETRACT_TO_HUB
                   && !c->filament_present_hub && !c->filament_present_toolhead)) {
        /* The selected clearance sensor must remain clear through braking.
         * The bay switch stays occupied; full ejection is a separate intent. */
        if (c->stage == KM_STAGE_UNLOAD_RETRACT_TO_HUB
            && !c->unload_from_load_preload && c->profile.unload_hub_clear_steps) {
            km_follower_disarm(&c->follower);
            if (!velocity(c, 0, now, us)) return;
            c->stage = KM_STAGE_UNLOAD_STOP_SETTLE;
            c->stage_entered_time_ms = now;
        } else settle(c, KM_EVENT_UNLOAD_COMPLETE, now, us);
    } else if (c->stage == KM_STAGE_EJECT_TO_BAY
               && (c->profile.has_bay_sensors ? !c->filament_present_bay[c->active_lane]
                                              : !km_trajectory_is_moving(&c->trajectory))) {
        settle(c, KM_EVENT_EJECT_COMPLETE, now, us);
    } else if (c->stage == KM_STAGE_CALIBRATE_PROBE && c->filament_present_toolhead) {
        int64_t delta = (int64_t)c->encoder_clicks - c->cal_start_clicks;
        if (delta > INT32_MAX) delta -= 4294967296LL;
        if (delta < INT32_MIN) delta += 4294967296LL;
        if (!delta) { km_core_explicit_stop(c, KM_FAULT_NO_PROGRESS, now); return; }
        calibrate_retract(c, (uint32_t)(delta < 0 ? -delta : delta), now, us);
    } else if (c->stage == KM_STAGE_CALIBRATE_RETRACT
               && !c->filament_present_hub && !c->filament_present_toolhead) {
        settle(c, KM_EVENT_PTFE_CAL_COMPLETE, now, us);
    }
}

km_status_snapshot_t km_core_get_snapshot(const km_operation_core_t *c)
{
    km_status_snapshot_t s = {0};
    if (!c) return s;
    s.op_state = c->op_state; s.stage = c->stage; s.active_op = c->active_op;
    s.active_client = c->active_client;
    s.active_session = c->active_session_id; s.active_req_id = c->active_req_id; s.active_lane = c->active_lane;
    s.lane_active = c->shared_path_owned; s.terminal_pending = c->terminal_pending;
    s.path_owner_lane = c->path_owner_lane;
    s.path_occupied = c->path_occupied; s.path_occupant_lane = c->path_occupant_lane;
    s.current_velocity_steps_s = km_trajectory_get_velocity(&c->trajectory);
    s.current_position_steps = km_trajectory_get_position(&c->trajectory);
    s.position_uncertain = c->trajectory.position_uncertain;
    s.buffer_position_permille = c->buffer_sensor.position_permille; s.buffer_valid = c->buffer_sensor.is_valid;
    s.commanded_current_ma = c->active_feeder ? c->active_feeder->run_current_ma : 0;
    /* The snapshot distinguishes a configured current target from an actual
     * frontend sample. Acquisition freshness remains visible as availability. */
    s.measured_current_available = c->profile.has_current_sensing
        && fresh(c->rewind_sample_valid, c->rewind_sample_ms, c->last_control_ms, c->profile.max_sensor_age_ms);
    s.measured_current_sample_ms = c->rewind_sample_ms;
    s.measured_current_ma = s.measured_current_available ? c->measured_rewind_ma : 0;
    s.system_fault = c->system_fault; s.motion_fault = c->motion_fault; s.host_connected = c->host_connected;
    s.follower_state = c->follower.state;
    s.follower_direction = c->follower.direction;
    s.follower_fault = c->follower.fault;
    s.motor_ready = c->motor_ready; s.control_time_valid = c->control_clock_valid;
    s.control_time_ms = c->last_control_ms; s.control_time_us = c->last_control_us;
    memcpy(s.client_session, c->client_session, sizeof(s.client_session));
    memcpy(s.client_request, c->client_request, sizeof(s.client_request));
    s.queued_events = c->event_queue.count;
    km_event_t head;
    if (km_event_queue_peek(&c->event_queue, &head) == KM_OK) s.head_event_seq = head.seq;
    s.pending_fault_event = c->pending_fault_event;
    s.selector_homed = c->selector_homed;
    s.selector_gate = c->selector_gate;
    return s;
}
