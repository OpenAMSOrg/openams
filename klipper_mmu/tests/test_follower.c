// SPDX-License-Identifier: GPL-3.0-or-later
#include <stdio.h>
#include <assert.h>
#include "klipper_mmu/follower.h"

static void test_follower_basic_cycles(void)
{
    km_follower_t f;
    km_follower_init(&f, 2000, 500, 5000, 500, 300, 1000);

    km_follower_inputs_t in = {0};
    in.has_bay_sensor = true;
    in.has_hub_sensor = true;
    in.has_current_setpoint = true;
    in.buffer_valid = true;
    in.buffer_position_permille = 500;
    in.filament_present_bay = true;
    in.filament_present_hub = true;
    in.encoder_clicks = 100;
    in.has_encoder = false;

    /* Arm forward follow */
    km_follower_arm(&f, KM_DIR_FORWARD, KM_FOLLOWER_DRIVE_STEP_VELOCITY, 1000);
    assert(f.state == KM_FOLLOWER_IDLE);

    /* Neutral buffer (500) -> stays idle */
    km_follower_command_t cmd = km_follower_update(&f, &in, 1010);
    assert(cmd.state == KM_FOLLOWER_IDLE);
    assert(!cmd.motor_enabled);

    /* Buffer compressed (200 <= 250) -> triggers driving */
    in.buffer_position_permille = 200;
    cmd = km_follower_update(&f, &in, 1020);
    assert(cmd.state == KM_FOLLOWER_DRIVING);
    assert(cmd.motor_enabled);
    assert(cmd.direction == KM_DIR_FORWARD);
    assert(cmd.target_velocity_steps_s > 0);

    /* Buffer relaxed (800 >= 750) -> stops driving and increments cycle count */
    in.buffer_position_permille = 800;
    cmd = km_follower_update(&f, &in, 1070);
    assert(cmd.state == KM_FOLLOWER_IDLE);
    assert(!cmd.motor_enabled);
    assert(cmd.cycle_count == 1);

    printf("PASS: test_follower_basic_cycles\n");
}

/* This file's own unit scale (100 steps/mm), used only to keep these
 * numbers readable as mm; not a real device conversion. */
#define KM_TEST_DETECTION_LENGTH_STEPS 1000u /* 10 mm: the Kconfig default */
#define KM_TEST_CLICK_PITCH_STEPS      140   /* 1.4 mm/click: ERCF's own ballpark */

static void test_follower_stall_escalation_and_fault(void)
{
    km_follower_t f;
    /* Current limits: min 300, max 400 (allows 2 escalations of +50mA before faulting) */
    km_follower_init(&f, 2000, 500, 5000, 300, 300, 400);
    km_follower_set_encoder_detection(&f, KM_TEST_DETECTION_LENGTH_STEPS);

    km_follower_arm(&f, KM_DIR_FORWARD, KM_FOLLOWER_DRIVE_STEP_VELOCITY, 1000);

    km_follower_inputs_t in = {0};
    in.has_bay_sensor = true;
    in.has_hub_sensor = true;
    in.has_current_setpoint = true;
    in.buffer_valid = true;
    in.buffer_position_permille = 200; /* Demand active */
    in.filament_present_bay = true;
    in.filament_present_hub = true;
    in.encoder_clicks = 100;   /* never changes below: a true jam, zero real clicks */
    in.has_encoder = true;
    in.encoder_valid = true;
    in.gear_position_steps = 5000; /* arbitrary nonzero start */

    /* Start driving; this call anchors the encoder baseline at gear_position_steps=5000. */
    km_follower_command_t cmd = km_follower_update(&f, &in, 1010);
    assert(cmd.state == KM_FOLLOWER_DRIVING);
    assert(cmd.current_setpoint_ma == 300);

    /* The gear is commanded one full detection length forward with no new click. */
    in.gear_position_steps += (int32_t)KM_TEST_DETECTION_LENGTH_STEPS;
    cmd = km_follower_update(&f, &in, 1020);

    /* Expect first escalation: current increases, stall_recoveries increments */
    assert(cmd.state == KM_FOLLOWER_DRIVING);
    assert(cmd.current_setpoint_ma == 350);
    assert(cmd.stall_recoveries == 1);

    /* Another full detection length: reaches max current (400mA) */
    in.gear_position_steps += (int32_t)KM_TEST_DETECTION_LENGTH_STEPS;
    cmd = km_follower_update(&f, &in, 1030);
    assert(cmd.state == KM_FOLLOWER_DRIVING);
    assert(cmd.current_setpoint_ma == 400);
    assert(cmd.stall_recoveries == 2);

    /* Another full detection length: at max current, cannot escalate further -> enters STALLED fault! */
    in.gear_position_steps += (int32_t)KM_TEST_DETECTION_LENGTH_STEPS;
    cmd = km_follower_update(&f, &in, 1040);
    assert(cmd.state == KM_FOLLOWER_FAULT);
    assert(cmd.fault == KM_FAULT_STALLED);
    assert(!cmd.motor_enabled);

    printf("PASS: test_follower_stall_escalation_and_fault\n");
}

static void test_follower_slow_follow_never_faults(void)
{
    /* A real print's feed rate, not a burst: about 1 mm/s of commanded gear
     * travel, clicking once every 1.4 mm (ERCF's own coarse resolution
     * ballpark) -- roughly one click every 1.4 real seconds. Distance-based
     * detection must not fault here no matter how much wall-clock time
     * passes between clicks; a fixed time window (the reverted approach)
     * held only 0.5-2 clicks at this rate and could. */
    km_follower_t f;
    km_follower_init(&f, 2000, 100, 5000, 0, 0, 0);
    km_follower_set_encoder_detection(&f, KM_TEST_DETECTION_LENGTH_STEPS);
    km_follower_arm(&f, KM_DIR_FORWARD, KM_FOLLOWER_DRIVE_STEP_VELOCITY, 0);

    km_follower_inputs_t in = {0};
    in.has_bay_sensor = true;
    in.has_hub_sensor = true;
    in.has_current_setpoint = false;
    in.buffer_valid = true;
    in.buffer_position_permille = 200; /* Demand active */
    in.filament_present_bay = true;
    in.filament_present_hub = true;
    in.encoder_clicks = 0;
    in.has_encoder = true;
    in.encoder_valid = true;
    in.gear_position_steps = 0;

    km_follower_command_t cmd = km_follower_update(&f, &in, 0);
    assert(cmd.state == KM_FOLLOWER_DRIVING);

    /* 1 mm/s at 100 steps/mm = 100 steps/s = 20 steps per 200 ms tick; 150
     * ticks is 30 real seconds, more than two full detection lengths of
     * travel, ticking a click every 1.4 mm of it. */
    const int32_t steps_per_tick = 20;
    int32_t since_click = 0;
    uint32_t now_ms = 0;
    for (int tick = 1; tick <= 150; ++tick) {
        now_ms += 200;
        in.gear_position_steps += steps_per_tick;
        since_click += steps_per_tick;
        if (since_click >= (int32_t)KM_TEST_CLICK_PITCH_STEPS) {
            since_click -= (int32_t)KM_TEST_CLICK_PITCH_STEPS;
            in.encoder_clicks += 1;
        }
        cmd = km_follower_update(&f, &in, now_ms);
        assert(cmd.state == KM_FOLLOWER_DRIVING);
        assert(cmd.fault == KM_FAULT_NONE);
    }

    printf("PASS: test_follower_slow_follow_never_faults\n");
}

static void test_follower_zero_clicks_faults_without_current(void)
{
    /* The other half of the same rule: with no current to escalate, zero
     * real clicks over one full detection length faults outright --
     * accepting slow-but-real progress (above) must not also accept none. */
    km_follower_t f;
    km_follower_init(&f, 2000, 500, 5000, 0, 0, 0);
    km_follower_set_encoder_detection(&f, KM_TEST_DETECTION_LENGTH_STEPS);
    km_follower_arm(&f, KM_DIR_FORWARD, KM_FOLLOWER_DRIVE_STEP_VELOCITY, 1000);

    km_follower_inputs_t in = {0};
    in.has_bay_sensor = true;
    in.has_hub_sensor = true;
    in.has_current_setpoint = false;
    in.buffer_valid = true;
    in.buffer_position_permille = 200;
    in.filament_present_bay = true;
    in.filament_present_hub = true;
    in.encoder_clicks = 100;
    in.has_encoder = true;
    in.encoder_valid = true;
    in.gear_position_steps = 0;

    km_follower_command_t cmd = km_follower_update(&f, &in, 1010);
    assert(cmd.state == KM_FOLLOWER_DRIVING);

    in.gear_position_steps += (int32_t)KM_TEST_DETECTION_LENGTH_STEPS;
    cmd = km_follower_update(&f, &in, 1020);
    assert(cmd.state == KM_FOLLOWER_FAULT);
    assert(cmd.fault == KM_FAULT_STALLED);
    assert(!cmd.motor_enabled);

    printf("PASS: test_follower_zero_clicks_faults_without_current\n");
}

static void test_follower_detection_distance_across_position_wrap(void)
{
    /* The gear position is 32-bit: travel measured across its wrap is the
     * short distance actually driven, not ~2^32 steps. */
    km_follower_t f;
    km_follower_init(&f, 2000, 500, 5000, 0, 0, 0);
    km_follower_set_encoder_detection(&f, KM_TEST_DETECTION_LENGTH_STEPS);
    km_follower_arm(&f, KM_DIR_FORWARD, KM_FOLLOWER_DRIVE_STEP_VELOCITY, 1000);

    km_follower_inputs_t in = {0};
    in.has_bay_sensor = true;
    in.has_hub_sensor = true;
    in.buffer_valid = true;
    in.buffer_position_permille = 200;
    in.filament_present_bay = true;
    in.filament_present_hub = true;
    in.encoder_clicks = 100;
    in.has_encoder = true;
    in.encoder_valid = true;
    in.gear_position_steps = INT32_MAX - 10;

    km_follower_command_t cmd = km_follower_update(&f, &in, 1010);
    assert(cmd.state == KM_FOLLOWER_DRIVING);
    in.gear_position_steps = (int32_t)((uint32_t)in.gear_position_steps
                                       + KM_TEST_DETECTION_LENGTH_STEPS - 1u);
    cmd = km_follower_update(&f, &in, 1020);
    assert(cmd.state == KM_FOLLOWER_DRIVING);
    in.gear_position_steps = (int32_t)((uint32_t)in.gear_position_steps + 1u);
    cmd = km_follower_update(&f, &in, 1030);
    assert(cmd.state == KM_FOLLOWER_FAULT && cmd.fault == KM_FAULT_STALLED);

    printf("PASS: test_follower_detection_distance_across_position_wrap\n");
}

static void test_follower_fast_burst_with_clicks_is_fine(void)
{
    /* A fast burst producing many clicks well inside one detection length
     * is obviously fine -- the baseline sanity check for the distance rule. */
    km_follower_t f;
    km_follower_init(&f, 2000, 500, 5000, 0, 0, 0);
    km_follower_set_encoder_detection(&f, KM_TEST_DETECTION_LENGTH_STEPS);
    km_follower_arm(&f, KM_DIR_FORWARD, KM_FOLLOWER_DRIVE_STEP_VELOCITY, 0);

    km_follower_inputs_t in = {0};
    in.has_bay_sensor = true;
    in.has_hub_sensor = true;
    in.has_current_setpoint = false;
    in.buffer_valid = true;
    in.buffer_position_permille = 200;
    in.filament_present_bay = true;
    in.filament_present_hub = true;
    in.encoder_clicks = 0;
    in.has_encoder = true;
    in.encoder_valid = true;
    in.gear_position_steps = 0;

    km_follower_command_t cmd = km_follower_update(&f, &in, 0);
    assert(cmd.state == KM_FOLLOWER_DRIVING);

    uint32_t now_ms = 0;
    for (int click = 1; click <= 20; ++click) {
        now_ms += 5; /* a fast burst: one click every 5 ms of commanded time */
        in.gear_position_steps += (int32_t)KM_TEST_CLICK_PITCH_STEPS;
        in.encoder_clicks += 1;
        cmd = km_follower_update(&f, &in, now_ms);
        assert(cmd.state == KM_FOLLOWER_DRIVING);
        assert(cmd.fault == KM_FAULT_NONE);
    }

    printf("PASS: test_follower_fast_burst_with_clicks_is_fine\n");
}

static void test_follower_starvation_and_signal_loss(void)
{
    km_follower_t f;
    km_follower_init(&f, 2000, 500, 5000, 500, 300, 1000);
    km_follower_arm(&f, KM_DIR_FORWARD, KM_FOLLOWER_DRIVE_STEP_VELOCITY, 1000);

    km_follower_inputs_t in = {0};
    in.has_bay_sensor = true;
    in.has_hub_sensor = true;
    in.has_current_setpoint = true;
    in.buffer_valid = true;
    in.buffer_position_permille = 10; /* <= 20 permille: Hard starvation zone */
    in.filament_present_bay = true;
    in.filament_present_hub = true;
    in.encoder_clicks = 0;
    in.has_encoder = false;

    /* Start driving in hard low */
    km_follower_command_t cmd = km_follower_update(&f, &in, 1010);
    assert(cmd.state == KM_FOLLOWER_DRIVING);

    /* Within debounce period (< 250ms), remains driving */
    cmd = km_follower_update(&f, &in, 1100);
    assert(cmd.state == KM_FOLLOWER_DRIVING);

    /* Past debounce period (>= 250ms) -> enters STARVED fault */
    cmd = km_follower_update(&f, &in, 1300);
    assert(cmd.state == KM_FOLLOWER_FAULT);
    assert(cmd.fault == KM_FAULT_STARVED);
    assert(!cmd.motor_enabled);

    /* Test buffer signal loss debounce */
    km_follower_init(&f, 2000, 500, 5000, 500, 300, 1000);
    km_follower_arm(&f, KM_DIR_FORWARD, KM_FOLLOWER_DRIVE_STEP_VELOCITY, 2000);

    in.buffer_valid = false; /* Signal invalid */
    in.buffer_position_permille = 500;
    cmd = km_follower_update(&f, &in, 2010);
    assert(cmd.state == KM_FOLLOWER_IDLE); /* Debouncing */

    /* After 50ms of invalid signal -> BUFFER_SIGNAL fault */
    cmd = km_follower_update(&f, &in, 2070);
    assert(cmd.state == KM_FOLLOWER_FAULT);
    assert(cmd.fault == KM_FAULT_BUFFER_SIGNAL);

    printf("PASS: test_follower_starvation_and_signal_loss\n");
}

int main(void)
{
    printf("--- Running Follower Tests ---\n");
    test_follower_basic_cycles();
    test_follower_stall_escalation_and_fault();
    test_follower_slow_follow_never_faults();
    test_follower_zero_clicks_faults_without_current();
    test_follower_detection_distance_across_position_wrap();
    test_follower_fast_burst_with_clicks_is_fine();
    test_follower_starvation_and_signal_loss();
    printf("All Follower Tests Passed.\n\n");
    return 0;
}
