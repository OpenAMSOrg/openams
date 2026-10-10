// SPDX-License-Identifier: GPL-3.0-or-later
#include "config.h"
#include "autoconf.h"
#include "klipper_mmu/operation_core.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* Klipper stops the command at shutdown; report it as the whole result. */
void shutdown(const char *msg)
{
    printf("{\"shutdown\":\"%s\"}\n", msg);
    exit(0);
}

void *oid_alloc(uint8_t oid, void (*type)(uint32_t *args), size_t size)
{
    (void)oid; (void)type; (void)size;
    return NULL;
}

#if CONFIG_KLIPPER_MMU_SELECTOR_STEPPER
/* Local stand-in: mcu/config.h does not declare this yet.
 * config_km_selector_gate is defined in mcu/config.c, matching this exact
 * signature. */
extern void command_config_km_selector_gate(uint32_t *args);
#endif
extern void command_config_km_feeder_rotation(uint32_t *args);

int main(int argc, char **argv)
{
    assert(argc == 2 || argc == 3 || argc == 4);
#if CONFIG_KLIPPER_MMU_FPS_ARRIVAL
    if (argc == 3) {  /* profile STEPS: a host-restored calibrated distance first */
        uint32_t args[] = {0, (uint32_t)strtoul(argv[2], NULL, 10)};
        command_config_km_fps_steps(args);
    }
#endif
#if CONFIG_KLIPPER_MMU_SELECTOR_STEPPER
    if (!strcmp(argv[1], "selector-gate")) {
        /* selector-gate GATE STEPS: a host-calibrated gate position first,
         * exercising config_km_selector_gate (shutdown() exits before the
         * rest of main() runs if GATE is out of range). */
        assert(argc == 4);
        uint32_t args[] = {0, (uint32_t)strtoul(argv[2], NULL, 10),
                           (uint32_t)strtol(argv[3], NULL, 10)};
        command_config_km_selector_gate(args);
    }
#endif
    if (!strcmp(argv[1], "feeder-rotation")) {
        /* feeder-rotation LANE UM: a host-calibrated per-lane rotation
         * distance first, exercising config_km_feeder_rotation (shutdown()
         * exits before the rest of main() runs if LANE or UM is invalid). */
        assert(argc == 4);
        uint32_t args[] = {0, (uint32_t)strtoul(argv[2], NULL, 10),
                           (uint32_t)strtoul(argv[3], NULL, 10)};
        command_config_km_feeder_rotation(args);
    }
    km_device_profile_t profile, before;
    memset(&profile, 0xa5, sizeof(profile)); before = profile;
    /* Explicit synthetic test calibration, never an installed default. */
    km_diff_adc_config_t calibration = {
        .channel_map = KM_DIFF_CH_A_LOW_B_HIGH, .vref_mv = 3300, .adc_max_counts = 3300,
        .path_tolerance_counts = 50, .point_count = 3,
        .points = {{0,1650,0},{825,2475,500},{1650,3300,1000}}
    };
    assert(km_config_profile(NULL, NULL) == KM_ERR_INVALID_PARAM);
    const km_diff_adc_config_t *table = CONFIG_KLIPPER_MMU_DIFF_BUFFER ? &calibration : NULL;
    if (!strcmp(argv[1], "missing")) table = NULL;
    if (!strcmp(argv[1], "unexpected")) table = &calibration;
    if (!strcmp(argv[1], "bad-cal")) calibration.points[1].position_permille = 0;
    km_result_t result = km_config_profile(&profile, table);
    if (strcmp(argv[1], "profile") && strcmp(argv[1], "selector-gate")
        && strcmp(argv[1], "feeder-rotation")) {
        assert(result == (!strcmp(argv[1], "missing") ? KM_ERR_UNAVAILABLE : KM_ERR_INVALID_PARAM));
        assert(!memcmp(&profile, &before, sizeof(profile)));
        puts("{\"rejected_without_mutation\":true}");
        return 0;
    }
    assert(result == KM_OK);
    calibration.points[1].position_permille = 123;
    if (table) assert(profile.diff_calibration.points[1].position_permille == 500);
    km_operation_core_t core;
    assert(km_core_init(&core, &profile) == KM_OK);
    assert(!core.motor_ready && !core.host_connected && !core.shared_path_owned);
    assert(core.trajectory.acceleration_steps_s2 == profile.default_acceleration_steps_s2);
    assert(core.follower.proportional_feedback == profile.has_proportional_buffer);
    printf("{\"name\":\"%s\",\"lanes\":%u,\"powered\":%u,\"encoder\":%u,\"load\":%u,\"analog\":%u,\"measured\":%u,"
           "\"bay\":%u,\"virtual_hub\":%u,"
           "\"feed\":%u,\"retract\":%u,\"acceleration\":%u,\"braking\":%u,\"run_ma\":%u,\"hold_ma\":%u,"
           "\"duty\":%u,\"current_target\":%u,\"current_limit\":%u,\"gain\":%u,\"deadband\":%u,"
           "\"sensor_age\":%u,\"lease\":%u,\"progress\":%u,\"hub\":%u,\"arrival\":%u,\"unload\":%u,"
           "\"rewind\":%u,\"cal_probe\":%u,\"cal_retract\":%u,\"toolhead\":%u,"
           "\"fps\":[%u,%u,%u,%u,%u,%u],"
           "\"load_profile\":[%u,%u,%u,%u,%u],",
           profile.name,(unsigned)profile.lane_count,(unsigned)profile.has_powered_rewind,
           (unsigned)profile.has_encoder,(unsigned)profile.has_load_sensors,
           (unsigned)profile.has_proportional_buffer,(unsigned)profile.has_current_sensing,
           (unsigned)profile.has_bay_sensors,(unsigned)profile.load_is_virtual_hub,
           profile.default_feed_velocity_steps_s,profile.default_rewind_velocity_steps_s,
           profile.default_acceleration_steps_s2,profile.default_deceleration_steps_s2,
           (unsigned)profile.default_run_current_ma,(unsigned)profile.default_hold_current_ma,
           (unsigned)profile.rewind_pwm_duty_permille,(unsigned)profile.rewind_current_target_ma,
           (unsigned)profile.rewind_current_limit_ma,profile.proportional_gain_milli,
           (unsigned)profile.buffer_deadband_permille,profile.max_sensor_age_ms,profile.host_watchdog_ms,
           profile.progress_timeout_ms,profile.hub_feed_timeout_ms,profile.buffer_feed_timeout_ms,
           profile.unload_retract_timeout_ms,profile.unload_rewind_timeout_ms,
           profile.ptfe_cal_probe_timeout_ms,profile.ptfe_cal_retract_timeout_ms,
           (unsigned)profile.has_toolhead_sensor,(unsigned)profile.fps_arrival,
           (unsigned)profile.fps_arrival_permille,(unsigned)profile.fps_arrival_samples,
           profile.fps_arrival_hold_ms,profile.fps_arrival_min_steps,profile.fps_arrival_max_steps,
           profile.fps_calibrated_steps,profile.load_cruise_velocity_steps_s,
           profile.load_crawl_velocity_steps_s,profile.load_window_steps,
           profile.buffer_travel_steps);

    printf("\"feeder_rotation_distance_um\":%u,\"lane_rotation_distance_um\":[",
           profile.feeder_rotation_distance_um);
    for (unsigned i = 0; i < KM_MAX_LANES; ++i)
        printf("%s%u", i ? "," : "", profile.lane_rotation_distance_um[i]);
    printf("],");

    /* Selector fields (selector.h): 0/false throughout for a non-selector
     * device (has_selector 0). Arrays always dump KM_MAX_LANES entries so
     * the caller can slice to its own lane count. */
    const km_selector_profile_t *sel = &profile.selector;
    printf("\"has_selector\":%u,\"selector_kind\":%u,"
           "\"selector_velocity\":%u,\"selector_accel\":%u,"
           "\"home_speed\":%u,\"home_accel\":%u,\"home_decel\":%u,"
           "\"home_max_travel\":%u,\"home_deadline_us\":%u,\"home_switch_age_us\":%u,"
           "\"home_direction\":%u,",
           (unsigned)profile.has_selector,(unsigned)sel->kind,
           sel->velocity_steps_s,sel->acceleration_steps_s2,
           sel->home.speed_steps_s,sel->home.acceleration_steps_s2,sel->home.deceleration_steps_s2,
           sel->home.max_travel_steps,sel->home.deadline_us,sel->home.max_switch_age_us,
           (unsigned)sel->home.home_direction);

    printf("\"gate_steps\":[");
    for (unsigned i = 0; i < KM_MAX_LANES; ++i) printf("%s%d", i ? "," : "", sel->gate_steps[i]);
    printf("],\"gate_direction\":[");
    for (unsigned i = 0; i < KM_MAX_LANES; ++i) printf("%s%u", i ? "," : "", (unsigned)sel->gate_direction[i]);
    printf("],\"release_gate\":[");
    for (unsigned i = 0; i < KM_MAX_LANES; ++i) printf("%s%u", i ? "," : "", (unsigned)sel->release_gate[i]);
    printf("],\"gate_order\":[");
    for (unsigned i = 0; i < KM_MAX_LANES; ++i) printf("%s%u", i ? "," : "", (unsigned)sel->gate_order[i]);
    printf("],\"center_nudge_steps\":%u,", sel->center_nudge_steps);

    printf("\"has_servo\":%u,\"servo_engage_us\":%u,\"servo_release_us\":%u,\"servo_move_us\":%u,"
           "\"servo_dwell_ms\":%u,\"servo_hold\":%u,",
           (unsigned)sel->has_servo,sel->servo_engage_us,sel->servo_release_us,sel->servo_move_us,
           sel->servo_dwell_ms,(unsigned)sel->servo_hold);
    printf("\"servo_gate_us\":[");
    for (unsigned i = 0; i < KM_MAX_LANES; ++i) printf("%s%u", i ? "," : "", sel->servo_gate_us[i]);
    printf("]}\n");
    return 0;
}
