// SPDX-License-Identifier: GPL-3.0-or-later
#include "klipper_mmu/device_profile.h"
#include <string.h>

/* Shared-drive selector machines (klipper_mmu/selector.h): LINEAR/ROTARY need
 * a working stepper axis and in-range gate positions; SERVO needs a gate
 * angle for every lane; any configured servo pulse must be a real RC pulse
 * width. Kept separate from km_profile_validate for the same reason its own
 * blocks are separate: has_selector is the one thing that can never apply to
 * an existing (non-selector) profile. */
static km_result_t km_selector_profile_validate(const km_device_profile_t *p)
{
    const km_selector_profile_t *s = &p->selector;
    if (s->kind != KM_SELECTOR_LINEAR && s->kind != KM_SELECTOR_ROTARY
        && s->kind != KM_SELECTOR_SERVO && s->kind != KM_SELECTOR_INDEXED) return KM_ERR_INVALID_PARAM;
    bool needs_axis = s->kind == KM_SELECTOR_LINEAR || s->kind == KM_SELECTOR_ROTARY
        || s->kind == KM_SELECTOR_INDEXED;
    if (needs_axis) {
        const km_selector_home_config_t *h = &s->home;
        if (!s->velocity_steps_s || s->velocity_steps_s > KM_MOTION_MAX_VELOCITY_STEPS_S
            || !s->acceleration_steps_s2 || s->acceleration_steps_s2 > KM_MOTION_MAX_ACCEL_STEPS_S2
            || !h->speed_steps_s || h->speed_steps_s > KM_MOTION_MAX_VELOCITY_STEPS_S
            || !h->acceleration_steps_s2 || h->acceleration_steps_s2 > KM_MOTION_MAX_ACCEL_STEPS_S2
            || !h->deceleration_steps_s2 || h->deceleration_steps_s2 > KM_MOTION_MAX_ACCEL_STEPS_S2
            || !h->max_travel_steps || !h->deadline_us || h->deadline_us >= KM_TIME_WRAP_HORIZON_US
            || !h->max_switch_age_us || h->max_switch_age_us >= KM_TIME_WRAP_HORIZON_US
            || (h->home_direction != KM_DIR_FORWARD && h->home_direction != KM_DIR_REVERSE))
            return KM_ERR_INVALID_PARAM;
        if (s->kind == KM_SELECTOR_INDEXED) {
            /* No gate position table: gate_steps stays unused, like SERVO. */
            for (uint8_t i = 0; i < p->lane_count; ++i)
                if (s->gate_steps[i]) return KM_ERR_INVALID_PARAM;
            /* An all-zero gate_order (never a valid permutation once
             * lane_count > 1: every other lane would be missing) is this
             * kind's own sentinel for "no physical order known" -- AFC's
             * own selector, which always searches forward and never picks a
             * shorter reverse hop (extras/AFC_vivid.py:159-199 select_lane,
             * selector.h). That device also gives no switch-width
             * measurement to center with (AFC's selector_cal_dis is
             * optional and unset by default), so center_nudge_steps may be
             * 0 too. A real gate_order (Happy Hare's selector_gate_order,
             * BTT ViViD) keeps both requirements exactly as before: a full
             * permutation and a real, in-range, never-invented nudge. */
            bool has_order = false;
            for (uint8_t i = 0; i < p->lane_count; ++i)
                if (s->gate_order[i]) { has_order = true; break; }
            if (has_order) {
                bool seen[KM_MAX_LANES] = {0};
                for (uint8_t i = 0; i < p->lane_count; ++i) {
                    if (s->gate_order[i] >= p->lane_count || seen[s->gate_order[i]])
                        return KM_ERR_INVALID_PARAM;
                    seen[s->gate_order[i]] = true;
                }
                if (!s->center_nudge_steps || s->center_nudge_steps >= h->max_travel_steps)
                    return KM_ERR_INVALID_PARAM;
            } else if (s->center_nudge_steps >= h->max_travel_steps
                       || !s->unselect_steps || s->unselect_steps >= h->max_travel_steps) {
                return KM_ERR_INVALID_PARAM;
            }
        } else {
            for (uint8_t i = 0; i < p->lane_count; ++i) {
                if (s->gate_steps[i] == -1) continue; /* Uncalibrated; refused at admission, not here. */
                if (s->gate_steps[i] < 0 || (uint32_t)s->gate_steps[i] >= h->max_travel_steps)
                    return KM_ERR_INVALID_PARAM;
            }
        }
    } else {
        for (uint8_t i = 0; i < p->lane_count; ++i)
            if (s->gate_steps[i]) return KM_ERR_INVALID_PARAM; /* SERVO has no axis. */
    }
    if (s->kind == KM_SELECTOR_ROTARY) {
        for (uint8_t i = 0; i < p->lane_count; ++i) {
            if (s->gate_direction[i] != KM_DIR_FORWARD && s->gate_direction[i] != KM_DIR_REVERSE)
                return KM_ERR_INVALID_PARAM;
            if (s->release_gate[i] >= p->lane_count) return KM_ERR_INVALID_PARAM;
        }
    }
    if (s->kind == KM_SELECTOR_SERVO && !s->has_servo) return KM_ERR_INVALID_PARAM;
    if (!s->has_servo) {
        if (s->servo_engage_us || s->servo_release_us || s->servo_move_us
            || s->servo_dwell_ms || s->servo_hold) return KM_ERR_INVALID_PARAM;
        for (uint8_t i = 0; i < p->lane_count; ++i)
            if (s->servo_gate_us[i]) return KM_ERR_INVALID_PARAM;
        return KM_OK;
    }
    uint16_t pulses[3] = {s->servo_engage_us, s->servo_release_us, s->servo_move_us};
    for (unsigned i = 0; i < 3; ++i)
        if (pulses[i] && (pulses[i] < 500 || pulses[i] > 2500)) return KM_ERR_INVALID_PARAM;
    if (!s->servo_dwell_ms) return KM_ERR_INVALID_PARAM;
    for (uint8_t i = 0; i < p->lane_count; ++i) {
        if (s->kind == KM_SELECTOR_SERVO) {
            /* 0 means uncalibrated (selector.h); refused at admission, not here.
             * Happy Hare ships Pico MMU with none at all (its cam is calibrated,
             * not measured up front), so a whole-profile refusal here would keep
             * that device from ever starting. */
            if (s->servo_gate_us[i] && (s->servo_gate_us[i] < 500 || s->servo_gate_us[i] > 2500))
                return KM_ERR_INVALID_PARAM;
        } else if (s->servo_gate_us[i]) {
            return KM_ERR_INVALID_PARAM; /* servo_gate_us selects a gate; only the SERVO kind uses it. */
        }
    }
    return KM_OK;
}

km_result_t km_profile_validate(const km_device_profile_t *p)
{
    if (!p) {
        return KM_ERR_INVALID_PARAM;
    }
    if (p->lane_count == 0 || p->lane_count > KM_MAX_LANES) {
        return KM_ERR_INVALID_PARAM;
    }
    if (p->default_feed_velocity_steps_s == 0 ||
        p->default_rewind_velocity_steps_s == 0 ||
        p->default_acceleration_steps_s2 == 0 ||
        p->default_deceleration_steps_s2 == 0) {
        return KM_ERR_INVALID_PARAM;
    }
    if (p->has_current_sensing && !p->has_powered_rewind) {
        return KM_ERR_INVALID_PARAM; /* Current sensing requires powered rewind hardware */
    }
    if (p->has_load_sensors && !p->has_bay_sensors) {
        return KM_ERR_INVALID_PARAM; /* PRELOAD must verify the upstream prep switch. */
    }
    if (p->load_is_virtual_hub && (!p->has_load_sensors
        || !p->has_hub_sensor || !p->has_bay_sensors || p->has_selector
        || p->has_powered_rewind)) return KM_ERR_INVALID_PARAM;
    if (p->has_selector) {
        km_result_t selector_result = km_selector_profile_validate(p);
        if (selector_result != KM_OK) return selector_result;
    }
    if (!p->has_powered_rewind && p->rewind_pwm_duty_permille > 0) {
        return KM_ERR_INVALID_PARAM; /* Passive rewind cannot command PWM */
    }
    if (p->hub_feed_timeout_ms == 0 ||
        p->buffer_feed_timeout_ms == 0 ||
        p->unload_retract_timeout_ms == 0 ||
        p->unload_rewind_timeout_ms == 0) {
        return KM_ERR_INVALID_PARAM;
    }
    if (p->default_feed_velocity_steps_s > KM_MOTION_MAX_VELOCITY_STEPS_S
        || p->default_rewind_velocity_steps_s > KM_MOTION_MAX_VELOCITY_STEPS_S
        || p->default_acceleration_steps_s2 > KM_MOTION_MAX_ACCEL_STEPS_S2
        || p->default_deceleration_steps_s2 > KM_MOTION_MAX_ACCEL_STEPS_S2
        || p->default_jerk_steps_s3 > KM_MOTION_MAX_JERK_STEPS_S3
        || p->default_hold_current_ma > p->default_run_current_ma
        || p->rewind_pwm_duty_permille > 1000 || !p->max_sensor_age_ms
        || p->max_sensor_age_ms > 60000 || !p->host_watchdog_ms || p->host_watchdog_ms > 60000
        || !p->progress_timeout_ms
        || !p->ptfe_cal_probe_timeout_ms || !p->ptfe_cal_retract_timeout_ms)
        return KM_ERR_INVALID_PARAM;
    const uint32_t deadlines[] = {p->hub_feed_timeout_ms, p->buffer_feed_timeout_ms,
        p->unload_retract_timeout_ms, p->unload_rewind_timeout_ms,
        p->ptfe_cal_probe_timeout_ms, p->ptfe_cal_retract_timeout_ms, p->progress_timeout_ms};
    for (unsigned i = 0; i < sizeof(deadlines) / sizeof(deadlines[0]); ++i)
        if (deadlines[i] >= 0x80000000u) return KM_ERR_INVALID_PARAM;
    if (p->has_current_sensing && (!p->rewind_current_target_ma
        || p->rewind_current_target_ma >= p->rewind_current_limit_ma)) return KM_ERR_INVALID_PARAM;
    if (p->has_encoder && !p->encoder_detection_length_steps) return KM_ERR_INVALID_PARAM;
    if (p->eject_final_steps && (p->has_bay_sensors || !p->eject_timeout_ms)) return KM_ERR_INVALID_PARAM;
    if (p->feeder_rotation_distance_um) {
        /* Each lane's real rotation distance must be within 0.8x-1.25x of
         * the reference (owner decision: manufacturing variation between
         * otherwise-identical gates, not a different gearing). Checked as
         * an integer ratio: rd/ref in [4/5, 5/4] <=> 4*ref <= 5*rd and
         * 4*rd <= 5*ref. */
        for (uint8_t i = 0; i < p->lane_count; ++i) {
            uint32_t rd = p->lane_rotation_distance_um[i];
            if (!rd || 4ull * p->feeder_rotation_distance_um > 5ull * rd
                || 4ull * rd > 5ull * p->feeder_rotation_distance_um)
                return KM_ERR_INVALID_PARAM;
        }
    } else {
        for (uint8_t i = 0; i < p->lane_count; ++i)
            if (p->lane_rotation_distance_um[i]) return KM_ERR_INVALID_PARAM;
    }
    if (p->has_proportional_buffer) {
        if (!p->proportional_gain_milli
            || p->proportional_gain_milli > KM_MOTION_MAX_VELOCITY_STEPS_S * 1000u
            || p->buffer_deadband_permille >= 250) return KM_ERR_INVALID_PARAM;
        if (!p->has_diff_calibration
            || km_diff_adc_config_validate(&p->diff_calibration) != KM_OK)
            return KM_ERR_INVALID_PARAM;
    }
    if (p->fps_arrival) {
        /* Threshold lies outside the follower deadband and below the clamped
         * calibration end; the hold must fit within the existing deadline.
         * The LOAD profile needs a cruise ceiling above the crawl, a window
         * narrower than the outer travel spread, and a buffer travel to
         * brake within; a restored S stays inside those outer limits. */
        if (!p->has_proportional_buffer || !p->has_hub_sensor
            || p->fps_arrival_permille <= KM_PERMILLE_CENTER + p->buffer_deadband_permille
            || p->fps_arrival_permille >= KM_PERMILLE_MAX
            || p->fps_arrival_samples < 2 || !p->fps_arrival_hold_ms
            || p->fps_arrival_hold_ms >= p->buffer_feed_timeout_ms
            || !p->fps_arrival_min_steps || p->fps_arrival_min_steps >= p->fps_arrival_max_steps
            || !p->load_cruise_velocity_steps_s
            || p->load_cruise_velocity_steps_s > KM_MOTION_MAX_VELOCITY_STEPS_S
            || !p->load_crawl_velocity_steps_s
            || p->load_crawl_velocity_steps_s >= p->load_cruise_velocity_steps_s
            || !p->load_window_steps || 2u * p->load_window_steps
               >= p->fps_arrival_max_steps - p->fps_arrival_min_steps
            || !p->buffer_travel_steps
            || (p->fps_calibrated_steps
                && (p->fps_calibrated_steps < p->fps_arrival_min_steps
                    || p->fps_calibrated_steps > p->fps_arrival_max_steps)))
            return KM_ERR_INVALID_PARAM;
    } else if (p->fps_arrival_permille || p->fps_arrival_samples || p->fps_arrival_hold_ms
               || p->fps_arrival_min_steps || p->fps_arrival_max_steps
               || p->fps_calibrated_steps || p->load_cruise_velocity_steps_s
               || p->load_crawl_velocity_steps_s || p->load_window_steps
               || p->buffer_travel_steps) {
        return KM_ERR_INVALID_PARAM; /* Never silently ignore arrival parameters. */
    }
    return KM_OK;
}

void km_profile_init_boxturtle(km_device_profile_t *p)
{
    if (!p) {
        return;
    }
    memset(p, 0, sizeof(*p));
    p->max_sensor_age_ms = 200;
    p->host_watchdog_ms = 2000;
    /* Baseline BoxTurtle: Powered rewind with open-loop PWM; NO fabricated optional sensors */
    p->name = "BoxTurtle-Baseline";
    p->lane_count = 4;
    p->has_selector = false;
    p->has_powered_rewind = true;
    p->has_encoder = false;             /* Optional: absent in baseline */
    p->has_proportional_buffer = false; /* Optional: switched buffer in baseline */
    p->has_current_sensing = false;     /* Optional: open-loop PWM in baseline */
    p->has_bay_sensors = true;
    p->has_hub_sensor = true;
    p->has_toolhead_sensor = true;

    p->default_feed_velocity_steps_s = 4000;
    p->default_rewind_velocity_steps_s = 3000;
    p->default_acceleration_steps_s2 = 10000;
    p->default_deceleration_steps_s2 = 15000;
    p->default_run_current_ma = 800;
    p->default_hold_current_ma = 200;

    p->rewind_pwm_duty_permille = 600;
    p->rewind_current_limit_ma = 0;     /* Baseline: no current limit */
    p->rewind_current_target_ma = 0;

    p->hub_feed_timeout_ms = 8000;
    p->buffer_feed_timeout_ms = 12000;
    p->unload_retract_timeout_ms = 8000;
    p->unload_rewind_timeout_ms = 15000;
    p->unload_hub_clear_steps = 0;
    p->ptfe_cal_probe_timeout_ms = 15000;
    p->ptfe_cal_retract_timeout_ms = 15000;
    p->progress_timeout_ms = 2000;
}

void km_profile_init_boxturtle_enhanced(km_device_profile_t *p)
{
    if (!p) {
        return;
    }
    /* Enhanced BoxTurtle fixture: explicitly adds measured current, encoder, and proportional buffer */
    km_profile_init_boxturtle(p);
    p->name = "BoxTurtle-Enhanced";
    p->has_encoder = true;
    p->encoder_detection_length_steps = 7000; /* Fixture-only step count; km_profile_validate requires > 0. */
    p->proportional_gain_milli = 8000;
    p->buffer_deadband_permille = 10;
    p->has_proportional_buffer = true;
    p->has_current_sensing = true;
    p->rewind_current_limit_ma = 800;
    p->rewind_current_target_ma = 500;
}

void km_profile_init_nightowl(km_device_profile_t *p)
{
    if (!p) {
        return;
    }
    memset(p, 0, sizeof(*p));
    p->max_sensor_age_ms = 200;
    p->host_watchdog_ms = 2000;
    p->name = "NightOwl-Reference";
    p->lane_count = 4;
    p->has_selector = false;
    p->has_powered_rewind = false; /* Passive rewind: no PWM motor */
    p->has_encoder = false;
    p->has_proportional_buffer = false; /* Switched buffer */
    p->has_current_sensing = false;
    p->has_bay_sensors = true;
    p->has_hub_sensor = true;
    p->has_toolhead_sensor = true;

    p->default_feed_velocity_steps_s = 3500;
    p->default_rewind_velocity_steps_s = 2500;
    p->default_acceleration_steps_s2 = 8000;
    p->default_deceleration_steps_s2 = 12000;
    p->default_run_current_ma = 700;
    p->default_hold_current_ma = 150;

    p->rewind_pwm_duty_permille = 0; /* Passive rewind needs no PWM */
    p->rewind_current_limit_ma = 0;
    p->rewind_current_target_ma = 0;

    p->hub_feed_timeout_ms = 8000;
    p->buffer_feed_timeout_ms = 12000;
    p->unload_retract_timeout_ms = 8000;
    p->unload_rewind_timeout_ms = 15000;
    p->unload_hub_clear_steps = 0;
    p->ptfe_cal_probe_timeout_ms = 15000;
    p->ptfe_cal_retract_timeout_ms = 15000;
    p->progress_timeout_ms = 2000;
}

void km_profile_init_emu(km_device_profile_t *p)
{
    if (!p) return;
    km_profile_init_nightowl(p);
    p->name = "EMU-Reference";
    p->lane_count = 5;
    p->has_load_sensors = true;
    p->load_is_virtual_hub = true;
}
