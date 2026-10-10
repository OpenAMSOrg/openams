// SPDX-License-Identifier: GPL-3.0-or-later
#include "klipper_mmu/follower.h"
#include <string.h>

km_result_t km_follower_init(km_follower_t *f,
                             uint32_t initial_velocity_steps_s,
                             uint32_t min_velocity_steps_s,
                             uint32_t max_velocity_steps_s,
                             uint16_t initial_current_ma,
                             uint16_t min_current_ma,
                             uint16_t max_current_ma)
{
    if (!f || !min_velocity_steps_s || initial_velocity_steps_s < min_velocity_steps_s
        || initial_velocity_steps_s > max_velocity_steps_s
        || max_velocity_steps_s > KM_MOTION_MAX_VELOCITY_STEPS_S
        || min_current_ma > initial_current_ma || initial_current_ma > max_current_ma) {
        return KM_ERR_INVALID_PARAM;
    }
    memset(f, 0, sizeof(*f));
    f->state = KM_FOLLOWER_DISARMED;
    f->fault = KM_FAULT_NONE;
    f->direction = KM_DIR_FORWARD;
    f->drive_mode = KM_FOLLOWER_DRIVE_STEP_VELOCITY;
    f->learned_velocity_steps_s = initial_velocity_steps_s > 0 ? initial_velocity_steps_s : 2000;
    f->burst_velocity_steps_s = f->learned_velocity_steps_s;
    f->min_velocity_steps_s = min_velocity_steps_s > 0 ? min_velocity_steps_s : 500;
    f->max_velocity_steps_s = max_velocity_steps_s > 0 ? max_velocity_steps_s : 10000;
    f->initial_current_ma = initial_current_ma;
    f->current_setpoint_ma = initial_current_ma;
    f->min_current_ma = min_current_ma;
    f->max_current_ma = max_current_ma;
    f->pwm_duty_permille = 0;

    f->idle_started_ms = 0;
    f->feed_started_ms = 0;
    f->last_boost_ms = 0;
    f->signal_bad_since_ms = KM_INVALID_TIME;
    f->starved_since_ms = KM_INVALID_TIME;
    f->last_encoder_clicks = 0;
    f->encoder_baseline_position_steps = 0;
    f->encoder_detection_length_steps = 0;
    f->feed_start_position_permille = 0;

    f->last_on_ms = 0;
    f->last_off_ms = 0;
    f->cycle_count = 0;
    f->stall_recoveries = 0;
    f->burst_escalated = false;
    return KM_OK;
}

km_result_t km_follower_set_feedback(km_follower_t *f, bool proportional,
                                     uint32_t gain_milli, uint16_t deadband)
{
    if (!f || deadband >= 250 || (proportional && (!gain_milli
        || gain_milli > KM_MOTION_MAX_VELOCITY_STEPS_S * 1000u))) return KM_ERR_INVALID_PARAM;
    if (f->state != KM_FOLLOWER_DISARMED) return KM_ERR_BUSY;
    f->proportional_feedback = proportional;
    f->proportional_gain_milli = gain_milli;
    f->buffer_deadband_permille = deadband;
    return KM_OK;
}

km_result_t km_follower_set_encoder_detection(km_follower_t *f,
                                              uint32_t detection_length_steps)
{
    if (!f || !detection_length_steps) return KM_ERR_INVALID_PARAM;
    if (f->state != KM_FOLLOWER_DISARMED) return KM_ERR_BUSY;
    f->encoder_detection_length_steps = detection_length_steps;
    return KM_OK;
}

static void proportional_command(km_follower_t *f, int32_t error)
{
    uint64_t speed = (uint64_t)(uint32_t)(error - f->buffer_deadband_permille)
        * f->proportional_gain_milli / 1000u;
    if (speed > f->max_velocity_steps_s) speed = f->max_velocity_steps_s;
    if (speed < f->min_velocity_steps_s) speed = f->min_velocity_steps_s;
    f->burst_velocity_steps_s = (uint32_t)speed;
    if (f->drive_mode == KM_FOLLOWER_DRIVE_PWM_DUTY)
        f->pwm_duty_permille = (uint16_t)(speed * 1000u / f->max_velocity_steps_s);
}

km_result_t km_follower_arm(km_follower_t *f,
                            km_direction_t dir,
                            km_follower_drive_mode_t mode,
                            uint32_t now_ms)
{
    if (!f || (dir != KM_DIR_FORWARD && dir != KM_DIR_REVERSE)
        || (mode != KM_FOLLOWER_DRIVE_STEP_VELOCITY && mode != KM_FOLLOWER_DRIVE_PWM_DUTY))
        return KM_ERR_INVALID_PARAM;
    if (f->fault != KM_FAULT_NONE) return KM_ERR_FAULTED;
    f->signal_bad = false;
    f->starved = false;
    f->direction = dir;
    f->drive_mode = mode;
    f->state = KM_FOLLOWER_IDLE;
    f->fault = KM_FAULT_NONE;
    f->burst_velocity_steps_s = f->learned_velocity_steps_s;
    f->current_setpoint_ma = f->initial_current_ma;
    f->pwm_duty_permille = (mode == KM_FOLLOWER_DRIVE_PWM_DUTY) ? 500 : 0;

    f->idle_started_ms = now_ms;
    f->feed_started_ms = now_ms;
    f->last_boost_ms = now_ms;
    f->signal_bad_since_ms = KM_INVALID_TIME;
    f->starved_since_ms = KM_INVALID_TIME;
    f->burst_escalated = false;
    f->last_on_ms = 0;
    f->last_off_ms = 0;
    f->cycle_count = 0;
    f->stall_recoveries = 0;
    return KM_OK;
}

void km_follower_disarm(km_follower_t *f)
{
    if (!f) {
        return;
    }
    f->state = f->fault == KM_FAULT_NONE ? KM_FOLLOWER_DISARMED : KM_FOLLOWER_FAULT;
    f->pwm_duty_permille = 0;
}

km_follower_command_t km_follower_get_command(const km_follower_t *f)
{
    km_follower_command_t cmd;
    if (!f) {
        cmd.state = KM_FOLLOWER_DISARMED;
        cmd.fault = KM_FAULT_NONE;
        cmd.motor_enabled = false;
        cmd.direction = KM_DIR_FORWARD;
        cmd.target_velocity_steps_s = 0;
        cmd.pwm_duty_permille = 0;
        cmd.current_setpoint_ma = 0;
        cmd.cycle_count = 0;
        cmd.stall_recoveries = 0;
        return cmd;
    }

    cmd.state = f->state;
    cmd.fault = f->fault;
    cmd.motor_enabled = (f->state == KM_FOLLOWER_DRIVING && !f->signal_bad);
    cmd.direction = f->direction;
    cmd.target_velocity_steps_s = (cmd.motor_enabled && f->drive_mode == KM_FOLLOWER_DRIVE_STEP_VELOCITY)
                                      ? f->burst_velocity_steps_s
                                      : 0;
    cmd.pwm_duty_permille = (cmd.motor_enabled && f->drive_mode == KM_FOLLOWER_DRIVE_PWM_DUTY)
                                ? f->pwm_duty_permille
                                : 0;
    cmd.current_setpoint_ma = cmd.motor_enabled ? f->current_setpoint_ma : 0;
    cmd.cycle_count = f->cycle_count;
    cmd.stall_recoveries = f->stall_recoveries;
    return cmd;
}

km_follower_command_t km_follower_update(km_follower_t *f,
                                         const km_follower_inputs_t *inputs,
                                         uint32_t now_ms)
{
    if (!f || f->state == KM_FOLLOWER_DISARMED || f->state == KM_FOLLOWER_FAULT) {
        return km_follower_get_command(f);
    }
    if (!inputs) {
        f->state = KM_FOLLOWER_FAULT;
        f->fault = KM_FAULT_SENSOR_FAILURE;
        return km_follower_get_command(f);
    }

    /* Buffer validity check */
    if (!inputs->buffer_valid || inputs->buffer_position_permille < 0
        || inputs->buffer_position_permille > 1000) {
        if (!f->signal_bad) {
            f->signal_bad = true;
            f->signal_bad_since_ms = now_ms;
        } else if (km_time_elapsed(now_ms, f->signal_bad_since_ms) >= KM_FOLLOWER_SIGNAL_FAULT_MS) {
            f->state = KM_FOLLOWER_FAULT;
            f->fault = KM_FAULT_BUFFER_SIGNAL;
        }
        return km_follower_get_command(f);
    }
    if (f->signal_bad) {
        f->signal_bad = false;
        f->state = KM_FOLLOWER_IDLE;
        f->idle_started_ms = now_ms;
    }
    f->signal_bad_since_ms = KM_INVALID_TIME;

    /* Configured encoder validity check: failure of a configured sensor cannot fall back silently */
    if (inputs->has_encoder && !inputs->encoder_valid) {
        f->state = KM_FOLLOWER_FAULT;
        f->fault = KM_FAULT_SENSOR_FAILURE;
        return km_follower_get_command(f);
    }

    /* Filament presence check */
    if ((inputs->has_bay_sensor && !inputs->filament_present_bay)
        || (inputs->has_hub_sensor && !inputs->filament_present_hub)) {
        f->state = KM_FOLLOWER_FAULT;
        f->fault = KM_FAULT_FILAMENT_LOST;
        return km_follower_get_command(f);
    }

    const int32_t pos = inputs->buffer_position_permille;
    const int32_t error = f->direction == KM_DIR_FORWARD ? 500 - pos : pos - 500;

    if (f->state == KM_FOLLOWER_IDLE) {
        const bool demand = f->proportional_feedback ? error > f->buffer_deadband_permille
            : (f->direction == KM_DIR_FORWARD ? pos <= KM_FOLLOWER_ON_POSITION_PERMILLE
                                              : pos >= KM_FOLLOWER_OFF_POSITION_PERMILLE);
        if (demand) {
            f->state = KM_FOLLOWER_DRIVING;
            f->feed_started_ms = now_ms;
            f->last_boost_ms = now_ms;
            f->last_encoder_clicks = inputs->encoder_clicks;
            f->encoder_baseline_position_steps = inputs->gear_position_steps;
            f->feed_start_position_permille = pos;
            f->burst_velocity_steps_s = f->learned_velocity_steps_s;
            if (f->proportional_feedback) proportional_command(f, error);
            f->starved = f->direction == KM_DIR_FORWARD && pos <= KM_FOLLOWER_HARD_LOW_PERMILLE;
            f->starved_since_ms = (f->direction == KM_DIR_FORWARD && pos <= KM_FOLLOWER_HARD_LOW_PERMILLE)
                                      ? now_ms
                                      : KM_INVALID_TIME;
            f->last_off_ms = km_time_elapsed(now_ms, f->idle_started_ms);
        }
        return km_follower_get_command(f);
    }

    /* Active DRIVING state checks */
    const bool should_stop = f->proportional_feedback ? error <= f->buffer_deadband_permille
        : (f->direction == KM_DIR_FORWARD ? pos >= KM_FOLLOWER_OFF_POSITION_PERMILLE
                                          : pos <= KM_FOLLOWER_ON_POSITION_PERMILLE);
    if (should_stop) {
        f->last_on_ms = km_time_elapsed(now_ms, f->feed_started_ms);
        f->cycle_count++;

        /* Velocity adaptation based on on/off times */
        if (!f->proportional_feedback && f->last_off_ms > 0 && f->last_on_ms > 0) {
            const uint64_t cycle_total = (uint64_t)f->last_off_ms + f->last_on_ms;
            if (cycle_total > 0) {
                const uint64_t duty_scaled = ((uint64_t)f->last_on_ms * 1000u) / cycle_total;
                if (duty_scaled > 700u && f->learned_velocity_steps_s + 100u <= f->max_velocity_steps_s) {
                    f->learned_velocity_steps_s += 100u;
                } else if (duty_scaled < 300u && f->learned_velocity_steps_s >= f->min_velocity_steps_s + 100u) {
                    f->learned_velocity_steps_s -= 100u;
                }
            }
        }

        f->state = KM_FOLLOWER_IDLE;
        f->idle_started_ms = now_ms;
        f->starved_since_ms = KM_INVALID_TIME;
        return km_follower_get_command(f);
    }

    if (f->proportional_feedback) proportional_command(f, error);

    /* Reverse timeout check */
    if (f->direction == KM_DIR_REVERSE && km_time_elapsed(now_ms, f->feed_started_ms) >= KM_FOLLOWER_REVERSE_TIMEOUT_MS) {
        f->state = KM_FOLLOWER_FAULT;
        f->fault = KM_FAULT_NO_PROGRESS;
        return km_follower_get_command(f);
    }

    /* Stall/clog monitoring if encoder is fitted and valid. Fault (or, with
     * measured current, escalate first) once the gear has commanded
     * encoder_detection_length_steps of travel since the last new encoder
     * click. Any click resets the count. */
    if (inputs->has_encoder && inputs->encoder_valid && f->encoder_detection_length_steps) {
        int64_t click_delta = (int64_t)inputs->encoder_clicks - f->last_encoder_clicks;
        if (click_delta > INT32_MAX) click_delta -= 4294967296LL;
        if (click_delta < INT32_MIN) click_delta += 4294967296LL;
        if (click_delta != 0) {
            f->last_encoder_clicks = inputs->encoder_clicks;
            f->encoder_baseline_position_steps = inputs->gear_position_steps;
        } else {
            /* Wrap-safe: positions are 32-bit and may wrap on a long print. */
            const int32_t travel = (int32_t)((uint32_t)inputs->gear_position_steps
                                             - (uint32_t)f->encoder_baseline_position_steps);
            const uint32_t moved_steps = travel >= 0 ? (uint32_t)travel : 0u - (uint32_t)travel;
            if (moved_steps >= f->encoder_detection_length_steps) {
                if (inputs->has_current_setpoint && f->current_setpoint_ma < f->max_current_ma) {
                    const uint16_t step = 50u;
                    f->current_setpoint_ma = (uint16_t)((f->current_setpoint_ma + step <= f->max_current_ma)
                                                 ? (f->current_setpoint_ma + step)
                                                 : f->max_current_ma);
                    f->encoder_baseline_position_steps = inputs->gear_position_steps;
                    f->burst_escalated = true;
                    f->stall_recoveries++;
                } else {
                    f->state = KM_FOLLOWER_FAULT;
                    f->fault = KM_FAULT_STALLED;
                    return km_follower_get_command(f);
                }
            }
        }
    }

    /* Starvation monitoring in forward direction */
    if (f->direction == KM_DIR_FORWARD && pos <= KM_FOLLOWER_HARD_LOW_PERMILLE) {
        if (!f->starved) {
            f->starved = true;
            f->starved_since_ms = now_ms;
        } else if (km_time_elapsed(now_ms, f->starved_since_ms) >= KM_FOLLOWER_STARVATION_DEBOUNCE_MS) {
            f->state = KM_FOLLOWER_FAULT;
            f->fault = KM_FAULT_STARVED;
            return km_follower_get_command(f);
        }
    } else {
        f->starved = false;
        f->starved_since_ms = KM_INVALID_TIME;
    }

    return km_follower_get_command(f);
}
