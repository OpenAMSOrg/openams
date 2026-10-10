// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KLIPPER_MMU_DEVICE_PROFILE_H
#define KLIPPER_MMU_DEVICE_PROFILE_H

#include "klipper_mmu/types.h"
#include "klipper_mmu/buffer_sensor.h"
#include "klipper_mmu/selector.h"

#ifdef __cplusplus
extern "C" {
#endif

/**
 * Composition-based device profile.
 * Core algorithms evaluate capability flags and limits, NOT device names.
 */
typedef struct {
    const char *name;
    uint8_t lane_count;
    bool has_selector; /* Shared gear behind a selector; see selector below. */
    bool has_powered_rewind;
    bool has_encoder;
    /* has_encoder only: the follower's own clog/stall check faults (or, with
     * measured current, escalates first) once the gear has commanded this
     * much travel since the last new encoder click -- a distance, not a
     * timeout (see km_follower_set_encoder_detection). */
    uint32_t encoder_detection_length_steps;
    bool has_proportional_buffer;
    bool has_current_sensing;
    bool has_bay_sensors;
    bool has_load_sensors; /* Optional lane-local switch after the prep/bay switch. */
    bool load_is_virtual_hub; /* Owner lane's LOAD switch is its virtual hub boundary. */
    bool has_hub_sensor;
    bool has_toolhead_sensor;
    bool has_diff_calibration;
    km_diff_adc_config_t diff_calibration;
    uint32_t max_sensor_age_ms;
    uint32_t host_watchdog_ms;
    uint32_t proportional_gain_milli;
    uint16_t buffer_deadband_permille;

    /* Motion settings */
    uint32_t default_feed_velocity_steps_s;
    uint32_t default_rewind_velocity_steps_s;
    uint32_t default_acceleration_steps_s2;
    uint32_t default_deceleration_steps_s2;
    uint32_t default_jerk_steps_s3; /* 0: 20 x the lower acceleration */
    uint16_t default_run_current_ma;
    uint16_t default_hold_current_ma;

    /* Rewinder tuning */
    uint16_t rewind_pwm_duty_permille;
    uint16_t rewind_current_limit_ma;
    uint16_t rewind_current_target_ma;

    /* Timeouts / deadlines in ms */
    uint32_t hub_feed_timeout_ms;
    uint32_t buffer_feed_timeout_ms;
    uint32_t unload_retract_timeout_ms;
    uint32_t unload_rewind_timeout_ms;
    uint32_t unload_hub_clear_steps; /* Optional post-HUB clearance, in feeder microsteps. */
    uint32_t eject_final_steps;      /* No-BAY EJECT: blind move back, in feeder microsteps (0 = refused). */
    uint32_t eject_timeout_ms;       /* Deadline for that move. */
    uint32_t ptfe_cal_probe_timeout_ms;
    uint32_t ptfe_cal_retract_timeout_ms;
    uint32_t progress_timeout_ms;

    /* Optional LOAD arrival from sustained calibrated FPS compression after HUB;
     * see docs/fps-arrival.md. Disabled (all zero) keeps toolhead-switch arrival.
     * When enabled, a configured toolhead switch must corroborate the arrival. */
    bool fps_arrival;
    uint16_t fps_arrival_permille;  /* Compression threshold toward 1000. */
    uint8_t fps_arrival_samples;    /* Distinct consecutive samples at/above it. */
    uint32_t fps_arrival_hold_ms;   /* First-to-last candidate acquisition span. */
    uint32_t fps_arrival_min_steps; /* Completed feeder steps after HUB before arming. */
    uint32_t fps_arrival_max_steps; /* Completed feeder steps after HUB before failing. */

    /* Calibrated LOAD profile. S = fps_calibrated_steps is the HUB-to-arrival
     * distance in feeder microsteps: the host restores a stored value at
     * configuration, and the core replaces it with the step-estimated
     * CALIBRATE result. 0 means not calibrated, and LOAD is then refused. */
    uint32_t fps_calibrated_steps;
    uint32_t load_cruise_velocity_steps_s; /* Open-path ceiling; never a target to reach. */
    uint32_t load_crawl_velocity_steps_s;  /* Final approach and CALIBRATE speed. */
    uint32_t load_window_steps;            /* Arrival accepted within S +/- this. */
    uint32_t buffer_travel_steps;          /* Full buffer travel, for the braking check. */

    /* Per-lane feeder rotation distance, in micrometres per output rotation
     * (Happy Hare's variable_rotation_distances / AFC's per-lane
     * rotation_distance; mcu/Kconfig.control's KLIPPER_MMU_FEEDER_LANE_
     * ROTATION_DISTANCES_UM, host feeder_rotation_distances). Every other
     * distance and speed in this struct stays expressed in this unit's own
     * reference mechanics (feeder_rotation_distance_um, mcu/feeder_mechanics.h's
     * KLIPPER_MMU_FEEDER_ROTATION_DISTANCE_UM); the active lane's own value is
     * applied only at the trajectory boundary (km_trajectory_set_lane_scale,
     * src/operation_core.c engage_feeder). 0 means this profile predates the
     * feature: every lane is 1:1 with the reference and lane_rotation_distance_um
     * must be all-zero too (km_profile_validate; every existing native fixture). */
    uint32_t feeder_rotation_distance_um;
    uint32_t lane_rotation_distance_um[KM_MAX_LANES];

    km_selector_profile_t selector; /* Meaningful only with has_selector. */
} km_device_profile_t;

/**
 * Validate device profile parameters and consistency.
 */
km_result_t km_profile_validate(const km_device_profile_t *p);

/**
 * Configure baseline composition for BoxTurtle (powered DC rewind, switched buffer, no optional sensors).
 */
void km_profile_init_boxturtle(km_device_profile_t *p);

/**
 * Configure enhanced composition for BoxTurtle (powered rewind with measured-current regulation,
 * proportional analog buffer, and filament encoder).
 */
void km_profile_init_boxturtle_enhanced(km_device_profile_t *p);

/**
 * Configure reference composition for NightOwl (passive rewind, switched buffer, no PWM).
 */
void km_profile_init_nightowl(km_device_profile_t *p);

/* EMU control composition; board wiring remains explicitly configured. */
void km_profile_init_emu(km_device_profile_t *p);

#ifdef __cplusplus
}
#endif

#endif /* KLIPPER_MMU_DEVICE_PROFILE_H */
