// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KLIPPER_MMU_FOLLOWER_H
#define KLIPPER_MMU_FOLLOWER_H

#include "klipper_mmu/types.h"

#ifdef __cplusplus
extern "C" {
#endif

#define KM_FOLLOWER_ON_POSITION_PERMILLE     250
#define KM_FOLLOWER_OFF_POSITION_PERMILLE    750
#define KM_FOLLOWER_HARD_LOW_PERMILLE        20
#define KM_FOLLOWER_SIGNAL_FAULT_MS          50u
#define KM_FOLLOWER_STARVATION_DEBOUNCE_MS   250u
#define KM_FOLLOWER_REVERSE_TIMEOUT_MS       10000u
#define KM_FOLLOWER_RESPONSE_BOOST_MS        1000u
#define KM_FOLLOWER_VELOCITY_BOOST_STEPS_S   500u

typedef enum {
    KM_FOLLOWER_DISARMED = 0,
    KM_FOLLOWER_IDLE     = 1,
    KM_FOLLOWER_DRIVING  = 2,
    KM_FOLLOWER_FAULT    = 3
} km_follower_state_t;

typedef enum {
    KM_FOLLOWER_DRIVE_STEP_VELOCITY = 0,
    KM_FOLLOWER_DRIVE_PWM_DUTY      = 1
} km_follower_drive_mode_t;

typedef struct {
    bool has_bay_sensor;
    bool has_hub_sensor;
    bool has_current_setpoint;
    bool buffer_valid;
    int32_t buffer_position_permille;
    bool filament_present_bay;
    bool filament_present_hub;
    int32_t encoder_clicks;
    bool has_encoder;
    bool encoder_valid;
    /* The gear's own commanded/actual position, in feeder microsteps
     * (km_trajectory_get_position); only meaningful with has_encoder, for
     * distance-based stall detection (km_follower_set_encoder_detection). */
    int32_t gear_position_steps;
} km_follower_inputs_t;

typedef struct {
    km_follower_state_t state;
    km_fault_t fault;
    bool motor_enabled;
    km_direction_t direction;
    uint32_t target_velocity_steps_s;
    uint16_t pwm_duty_permille;
    uint16_t current_setpoint_ma;
    uint32_t cycle_count;
    uint32_t stall_recoveries;
} km_follower_command_t;

typedef struct {
    km_follower_state_t state;
    km_fault_t fault;
    km_direction_t direction;
    km_follower_drive_mode_t drive_mode;
    uint32_t learned_velocity_steps_s;
    uint32_t burst_velocity_steps_s;
    uint32_t min_velocity_steps_s;
    uint32_t max_velocity_steps_s;
    uint16_t current_setpoint_ma;
    uint16_t min_current_ma;
    uint16_t max_current_ma;
    uint16_t pwm_duty_permille;

    uint32_t idle_started_ms;
    uint32_t feed_started_ms;
    uint32_t last_boost_ms;
    uint32_t signal_bad_since_ms;
    uint32_t starved_since_ms;
    int32_t last_encoder_clicks;
    /* gear_position_steps at the last confirmed click (or DRIVING start):
     * the baseline km_follower_update() measures travel-since-click from. */
    int32_t encoder_baseline_position_steps;
    /* has_encoder only (km_follower_set_encoder_detection); 0 = unconfigured. */
    uint32_t encoder_detection_length_steps;
    int32_t feed_start_position_permille;

    uint32_t last_on_ms;
    uint32_t last_off_ms;
    uint32_t cycle_count;
    uint32_t stall_recoveries;
    bool burst_escalated;
    bool signal_bad;
    bool starved;
    uint16_t initial_current_ma;
    bool proportional_feedback;
    uint32_t proportional_gain_milli;
    uint16_t buffer_deadband_permille;
} km_follower_t;

/**
 * Initialize follower controller.
 */
km_result_t km_follower_init(km_follower_t *f,
                             uint32_t initial_velocity_steps_s,
                             uint32_t min_velocity_steps_s,
                             uint32_t max_velocity_steps_s,
                             uint16_t initial_current_ma,
                             uint16_t min_current_ma,
                             uint16_t max_current_ma);

/* Optional analog feedback law. Gain is (steps/s)/permille, scaled by 1000.
 * Configure only while disarmed. The trajectory still owns acceleration. */
km_result_t km_follower_set_feedback(km_follower_t *f, bool proportional,
                                     uint32_t gain_milli, uint16_t deadband_permille);

/* Configure the shared-path encoder's clog/stall detection distance (feeder
 * microsteps; device_profile_t.encoder_detection_length_steps, itself
 * KLIPPER_MMU_ENCODER_DETECTION_LENGTH_MM converted once with the feeder
 * mechanics). Required, and detection_length_steps must be > 0, whenever the
 * device profile has an encoder; configure only while disarmed. */
km_result_t km_follower_set_encoder_detection(km_follower_t *f,
                                              uint32_t detection_length_steps);

/**
 * Arm follower in given direction.
 */
km_result_t km_follower_arm(km_follower_t *f,
                            km_direction_t dir,
                            km_follower_drive_mode_t mode,
                            uint32_t now_ms);

/**
 * Disarm follower and stop drive.
 */
void km_follower_disarm(km_follower_t *f);

/**
 * Update follower controller with sensor inputs.
 * Enforces configured sensor validity: missing data on a configured sensor latches fault.
 */
km_follower_command_t km_follower_update(km_follower_t *f,
                                         const km_follower_inputs_t *inputs,
                                         uint32_t now_ms);

/**
 * Get current follower command snapshot without mutating state.
 */
km_follower_command_t km_follower_get_command(const km_follower_t *f);

#ifdef __cplusplus
}
#endif

#endif /* KLIPPER_MMU_FOLLOWER_H */
