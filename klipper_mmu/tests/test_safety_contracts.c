// SPDX-License-Identifier: GPL-3.0-or-later
#include <assert.h>
#include <math.h>
#include <stdio.h>
#include <string.h>
#include "klipper_mmu/current_regulator.h"
#include "klipper_mmu/follower.h"
#include "klipper_mmu/trajectory.h"
#include "klipper_mmu/protocol_wire.h"
#include "mock_driver.h"
#include "fake_clock.h"

static void motion_setup(km_trajectory_t *t, mock_driver_t *d)
{
    mock_driver_init(d, 2, 10, false);
    assert(km_driver_set_enable(&d->base, true) == KM_OK);
    assert(km_trajectory_init(t, 20000, 1000, 1000) == KM_OK);
    km_trajectory_bind_driver(t, &d->base);
}
static void drain(km_trajectory_t *t, km_fake_clock_t *clock)
{
    km_fake_clock_run_trajectory(clock, t, clock->now_us + 10000000);
    assert(!km_trajectory_is_moving(t));
}
/* test_point_retarget_and_pending_pulse tested whether a retarget mid-pulse
 * keeps the in-flight pulse's already-scheduled fall time (t.next_deadline_us)
 * -- the removed per-edge service's own state; the field is gone, and a
 * window-based tick has no "pulse currently high" to keep pending. The
 * portable half of what it checked -- a retarget mid-move lands exactly on
 * the new target, and a rejected retarget leaves the trajectory untouched --
 * is the B2 judge's case 6 (tests/test_trajectory_window.c) plus
 * check_command's rejection-preserves-state path exercised throughout this
 * file, so it is deleted rather than ported.
 */
static void test_large_endpoints_and_active_limits(void)
{
    for (int sign = -1; sign <= 1; sign += 2) {
        km_trajectory_t t; mock_driver_t d;
        motion_setup(&t, &d);
        km_driver_shutdown(&d.base);
        int64_t end = sign > 0 ? KM_MOTION_MAX_POSITION_STEPS : KM_MOTION_MIN_POSITION_STEPS;
        assert(km_trajectory_recover(&t, end - sign * 10, 0) == KM_OK);
        assert(km_driver_set_enable(&d.base, true) == KM_OK);
        assert(km_trajectory_set_target_position(&t, end, 1000, 0) == KM_OK);
        km_fake_clock_t clock; km_fake_clock_init(&clock, 0);
        drain(&t, &clock);
        /* Stepping runs through the shared reference executor, not the
         * driver's own edge callback. */
        assert(t.current_position_steps == end && km_ref_position(&clock.ref) == sign * 10);
        assert(!t.position_uncertain);
    }
    km_trajectory_t t; mock_driver_t d;
    motion_setup(&t, &d);
    assert(km_trajectory_set_target_velocity(&t, 1000, 0) == KM_OK);
    km_fake_clock_t clock; km_fake_clock_init(&clock, 0);
    assert(km_fake_clock_run_trajectory(&clock, &t, 100000) == KM_OK);
    int32_t velocity = t.current_velocity_steps_s;
    assert(km_trajectory_set_limits_at(&t, 50, 1000, 1000, clock.now_us) == KM_OK);
    assert(t.current_velocity_steps_s == velocity && t.target_velocity_steps_s == 50);
    assert(km_fake_clock_run_trajectory(&clock, &t, 200000) == KM_OK);
    assert(t.current_velocity_steps_s == 50);
    assert(km_trajectory_cancel(&t, clock.now_us) == KM_OK);
    drain(&t, &clock);
}
/* test_late_service_and_intent_faults tested the removed per-edge service's
 * own missed-deadline lateness fault (KM_MOTION_MAX_LATENESS_US, t.has_deadline)
 * and a disrupted mid-pulse (d.step_pin_high) -- both gone with the edge
 * service. Their tick-API equivalents are the underrun fault and the empty-
 * window-after-a-stop invariant, already covered by the B2 judge (cases 4, 8
 * and 11 in tests/test_trajectory_window.c), so this is deleted rather than
 * ported.
 */
static void test_driver_readiness(void)
{
    mock_driver_t d; mock_driver_init(&d, 2, 10, false);
    d.base.initialized = false;
    assert(km_driver_set_enable(&d.base, true) == KM_ERR_UNAVAILABLE);
    assert(km_driver_set_pwm_duty(&d.base, 500) == KM_ERR_UNAVAILABLE);
    assert(km_driver_init(&d.base, NULL) == KM_OK);
    assert(km_driver_set_enable(&d.base, true) == KM_OK);
    assert(km_driver_set_pwm_duty(&d.base, 1001) == KM_ERR_INVALID_PARAM);
    assert(km_driver_set_pwm_duty(&d.base, 500) == KM_OK);
    assert(km_driver_set_direction(&d.base, KM_DIR_REVERSE, 0) == KM_ERR_BUSY);
    assert(km_driver_set_pwm_duty(&d.base, 0) == KM_OK);
    assert(km_driver_set_direction(&d.base, KM_DIR_REVERSE, 0) == KM_OK);
    uint16_t measured = 99;
    assert(km_driver_get_measured_current_ma(&d.base, &measured) == KM_ERR_NOT_SUPPORTED && measured == 99);
    assert(km_driver_set_pwm_duty(&d.base, 500) == KM_OK);
    d.injected_fault = KM_FAULT_DRIVER_FAULT;
    assert(km_driver_set_pwm_duty(&d.base, 100) == KM_ERR_FAULTED);
    assert(!d.base.enabled && d.base.pwm_duty_permille == 0);
}
static void test_current_samples_and_limits(void)
{
    km_current_regulator_t r;
    uint16_t duty;
    assert(km_current_regulator_init(&r, 900, 800, 600, 200) == KM_ERR_INVALID_PARAM);
    assert(km_current_regulator_init(&r, 500, 800, 600, 200) == KM_OK);
    assert(km_current_regulator_update_sample(&r, 0, true, 0, 0, &duty) == KM_OK);
    uint16_t first = duty;
    for (uint32_t now = 1; now <= 200; ++now) {
        assert(km_current_regulator_update_sample(&r, 0, true, 0, now, &duty) == KM_OK);
        assert(duty == first && r.integral_term == 0);
    }
    assert(km_current_regulator_update_sample(&r, 0, true, 0, 201, &duty) == KM_ERR_FAULTED && duty == 0);
    assert(km_current_regulator_update(&r, 0, true, 202, &duty) == KM_ERR_FAULTED && duty == 0);
    km_current_regulator_reset(&r);
    assert(km_current_regulator_update(&r, 1201, true, 0, &duty) == KM_ERR_FAULTED && duty == 0);
    km_current_regulator_reset(&r);
    assert(km_current_regulator_update(&r, 801, true, 0, &duty) == KM_OK && duty == 0);
    assert(km_current_regulator_update(&r, 801, true, 100, &duty) == KM_ERR_FAULTED && duty == 0);
    km_current_regulator_reset(&r); r.base_duty_permille = 0;
    assert(km_current_regulator_update(&r, 0, true, 0, &duty) == KM_OK && duty == 0);
    assert(km_current_regulator_update(&r, 1300, true, 1, &duty) == KM_ERR_FAULTED && duty == 0);
    km_current_regulator_t fast, slow;
    assert(km_current_regulator_init(&fast, 500, 800, 600, 200) == KM_OK);
    slow = fast;
    assert(km_current_regulator_update(&fast, 400, true, UINT32_MAX - 99u, &duty) == KM_OK);
    assert(km_current_regulator_update(&slow, 400, true, UINT32_MAX - 99u, &duty) == KM_OK);
    for (uint32_t i = 1; i <= 100; ++i)
        assert(km_current_regulator_update(&fast, 400, true, UINT32_MAX - 99u + i, &duty) == KM_OK);
    assert(km_current_regulator_update(&slow, 400, true, 0, &duty) == KM_OK);
    assert(slow.integral_term == fast.integral_term && slow.integral_term == 500);
}
static void test_follower_feedback_loss(void)
{
    km_follower_t f;
    assert(km_follower_init(&f, 1000, 500, 2000, 500, 300, 800) == KM_OK);
    assert(km_follower_arm(&f, KM_DIR_FORWARD, KM_FOLLOWER_DRIVE_STEP_VELOCITY, 0) == KM_OK);
    km_follower_inputs_t in = {0}; in.buffer_valid = true; in.buffer_position_permille = 200;
    assert(km_follower_update(&f, &in, 0).motor_enabled); /* Absent optional bay/hub sensing. */
    in.buffer_valid = false;
    km_follower_command_t command = km_follower_update(&f, &in, 1);
    assert(!command.motor_enabled && command.target_velocity_steps_s == 0);
    assert(!km_follower_get_command(&f).motor_enabled);
    command = km_follower_update(&f, &in, 51);
    assert(command.fault == KM_FAULT_BUFFER_SIGNAL);
    km_follower_disarm(&f);
    assert(km_follower_arm(&f, KM_DIR_FORWARD, KM_FOLLOWER_DRIVE_STEP_VELOCITY, 52) == KM_ERR_FAULTED);
    assert(km_follower_init(&f, 1000, 500, 2000, 500, 300, 800) == KM_OK);
    assert(km_follower_arm(&f, KM_DIR_FORWARD, KM_FOLLOWER_DRIVE_STEP_VELOCITY, 0) == KM_OK);
    in.buffer_valid = true; in.has_encoder = true; in.encoder_valid = false;
    assert(km_follower_update(&f, &in, 0).fault == KM_FAULT_SENSOR_FAILURE);
    assert(km_follower_init(&f, 1000, 500, 2000, 500, 300, 800) == KM_OK);
    assert(km_follower_set_encoder_detection(&f, 1000) == KM_OK);
    assert(km_follower_arm(&f, KM_DIR_FORWARD, KM_FOLLOWER_DRIVE_STEP_VELOCITY, 0) == KM_OK);
    in.encoder_valid = true; in.encoder_clicks = INT32_MAX; in.gear_position_steps = 0;
    assert(km_follower_update(&f, &in, 0).motor_enabled);
    in.encoder_clicks = INT32_MIN + 1; /* Wraps by +2 (not a bogus huge jump): a real click, resets the baseline. */
    in.gear_position_steps = 500; /* Under one detection length past that reset: no fault yet. */
    assert(km_follower_update(&f, &in, 750).motor_enabled);
    in.gear_position_steps = 1500; /* A full detection length past the last new click. */
    assert(km_follower_update(&f, &in, 1501).fault == KM_FAULT_STALLED); /* No current capability: no imaginary boost. */
}

static void test_proportional_local_following(void)
{
    for (int reverse = 0; reverse < 2; ++reverse) {
        km_follower_t f;
        assert(km_follower_init(&f, 1000, 1, 4000, 500, 300, 800) == KM_OK);
        assert(km_follower_set_feedback(&f, true, 8000, 10) == KM_OK);
        assert(km_follower_arm(&f, reverse ? KM_DIR_REVERSE : KM_DIR_FORWARD,
                               KM_FOLLOWER_DRIVE_STEP_VELOCITY, 0) == KM_OK);
        km_follower_inputs_t in = {0}; in.buffer_valid = true;
        in.buffer_position_permille = reverse ? 700 : 300;
        km_follower_command_t cmd = km_follower_update(&f, &in, 0);
        assert(cmd.motor_enabled && cmd.target_velocity_steps_s == 1520);
        in.buffer_position_permille = reverse ? 600 : 400;
        cmd = km_follower_update(&f, &in, 1);
        assert(cmd.motor_enabled && cmd.target_velocity_steps_s == 720);
        in.buffer_position_permille = reverse ? 505 : 495;
        cmd = km_follower_update(&f, &in, 2);
        assert(!cmd.motor_enabled && cmd.target_velocity_steps_s == 0);
        in.buffer_position_permille = reverse ? 1000 : 0;
        cmd = km_follower_update(&f, &in, 3);
        assert(cmd.motor_enabled && cmd.target_velocity_steps_s == 3920);
        assert(km_follower_set_feedback(&f, false, 0, 0) == KM_ERR_BUSY);
    }
}

static void test_legacy_wire_rejects_identity_truncation(void)
{
    km_event_t event = {0}; event.type = KM_EVENT_LOAD_COMPLETE; event.req_id = 1000;
    km_wire_action_status_t wire = km_protocol_wire_encode_event(&event);
    assert(!wire.valid && !wire.has_gen && wire.code != KM_WIRE_OP_CODE_SUCCESS);
    event.req_id = 42;
    wire = km_protocol_wire_encode_event(&event);
    assert(wire.valid && wire.has_gen && wire.gen == 42 && wire.code == KM_WIRE_OP_CODE_SUCCESS);
    km_status_snapshot_t status = {0}; status.op_state = KM_OP_STATE_LOADING;
    status.active_req_id = 42;
    wire = km_protocol_wire_encode_snapshot(&status);
    assert(wire.valid && !wire.has_gen && wire.code == KM_WIRE_OP_CODE_ERROR_BUSY);
}

static void test_maximum_supported_step_rate(void)
{
    km_trajectory_t t; mock_driver_t d;
    mock_driver_init(&d, 2, 5, false);
    d.base.caps.max_velocity_steps_s = 200000;
    assert(km_driver_set_enable(&d.base, true) == KM_OK);
    assert(km_trajectory_init(&t, 200000, 2000000, 2000000) == KM_OK);
    km_trajectory_bind_driver(&t, &d.base);
    assert(km_trajectory_set_target_velocity(&t, 200000, 0) == KM_OK);
    km_fake_clock_t clock; km_fake_clock_init(&clock, 0);
    assert(km_fake_clock_run_trajectory(&clock, &t, 200000) == KM_OK);
    /* Jerk-limited S-curve at the default jerk (20 x 2e6 = 4e7 steps/s^3):
     * 150 ms and exactly 15000 steps to reach 200000 steps/s, then cruise:
     * 25000 steps in 200 ms (a trapezoid would give 30000). Step timing and
     * spacing at this rate are step_segments.c's own contract (tests/
     * test_step_segments.c, tests/test_trajectory_window.c case 9). */
    assert(t.current_position_steps >= 24999 && t.current_position_steps <= 25001);
    assert(km_trajectory_cancel(&t, clock.now_us) == KM_OK);
    /* A jerk-limited stop from 200000 steps/s takes v/D + D/J = 150 ms. */
    assert(km_fake_clock_run_trajectory(&clock, &t, 360000) == KM_OK);
    assert(!km_trajectory_is_moving(&t));
    assert(t.current_position_steps >= 39999 && t.current_position_steps <= 40000);
    printf("PASS: test_maximum_supported_step_rate\n");
}

int main(void)
{
    test_large_endpoints_and_active_limits();
    test_driver_readiness();
    test_current_samples_and_limits(); test_follower_feedback_loss();
    test_proportional_local_following();
    test_legacy_wire_rejects_identity_truncation();
    test_maximum_supported_step_rate();
    puts("Motion, driver, feedback and current safety contracts passed.");
    return 0;
}
