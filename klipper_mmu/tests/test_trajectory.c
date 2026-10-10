// SPDX-License-Identifier: GPL-3.0-or-later
#include <stdio.h>
#include <assert.h>
#include <stdlib.h>
#include <limits.h>
#include <math.h>
#include <string.h>
#include "klipper_mmu/trajectory.h"
#include "mock_driver.h"
#include "fake_clock.h"

/* Jerk-limited ramps need virtual time: the default jerk reaches a 1e6 steps/s2
 * acceleration limit in 50 ms, so a 1000 steps/s target is reached after 10 ms. */
#define KM_TRAJECTORY_TEST_RAMP_US 20000u

static void test_trajectory_ramp_and_steady(void)
{
    km_trajectory_t traj;
    km_trajectory_init(&traj, 5000, 4000, 4000); 

    uint32_t now_us = 1000000;
    km_trajectory_set_target_velocity(&traj, 2000, now_us);

    for (int i = 0; i < 55; i++) {
        now_us += 10000;
        km_trajectory_update(&traj, now_us);
    }

    assert(km_trajectory_get_velocity(&traj) == 2000);

    int64_t pos_before = km_trajectory_get_position(&traj);
    for (int i = 0; i < 50; i++) {
        now_us += 10000;
        km_trajectory_update(&traj, now_us);
    }
    int64_t steady_steps = km_trajectory_get_position(&traj) - pos_before;
    assert(steady_steps >= 995 && steady_steps <= 1005);
    printf("PASS: test_trajectory_ramp_and_steady\n");
}

static void test_trajectory_zero_crossing_reversal(void)
{
    km_trajectory_t traj;
    km_trajectory_init(&traj, 5000, 4000, 4000);

    uint32_t now_us = 1000000;
    km_trajectory_set_target_velocity(&traj, 2000, now_us);

    for (int i = 0; i < 55; i++) {
        now_us += 10000;
        km_trajectory_update(&traj, now_us);
    }
    assert(km_trajectory_get_velocity(&traj) == 2000);

    km_trajectory_set_target_velocity(&traj, -2000, now_us);

    bool reached_zero = false;
    bool reached_negative = false;
    int32_t prev_v = 2000;

    for (int i = 0; i < 120; i++) {
        now_us += 10000;
        km_trajectory_update(&traj, now_us);
        int32_t v = km_trajectory_get_velocity(&traj);
        /* The reversal crosses zero between samples; the old integer ramp happened
         * to land on zero exactly at this sample period. */
        if (prev_v > 0 && v <= 0) reached_zero = true;
        if (v < 0) reached_negative = true;
        prev_v = v;
    }

    assert(reached_zero);
    assert(reached_negative);
    assert(km_trajectory_get_velocity(&traj) == -2000);
    printf("PASS: test_trajectory_zero_crossing_reversal\n");
}

static void test_trajectory_substep_accumulation_low_speed(void)
{
    km_trajectory_t traj;
    km_trajectory_init(&traj, 1000, 50000, 50000);
    km_trajectory_set_jerk(&traj, 100000000);

    uint32_t now_us = 100000;
    km_trajectory_set_target_velocity(&traj, 3, now_us);

    for (int i = 0; i <= 100; i++) {
        now_us += 10000;
        km_trajectory_update(&traj, now_us);
    }

    assert(km_trajectory_get_position(&traj) == 3);
    printf("PASS: test_trajectory_substep_accumulation_low_speed\n");
}

static void test_trajectory_extreme_inputs_and_limit_changes(void)
{
    km_trajectory_t t;
    mock_driver_t d;
    mock_driver_init(&d, 2, 4, false);
    km_driver_set_enable(&d.base, true);

    km_trajectory_init(&t, 1000, 1000, 1000);
    km_result_t res = km_trajectory_set_target_velocity(&t, INT32_MIN, 0);
    assert(res == KM_OK);
    assert(t.target_velocity_steps_s == -1000);

    km_trajectory_init(&t, 1000, 1000000, 1000000);
    km_trajectory_bind_driver(&t, &d.base);
    /* Driver-bound trajectories advance on their step deadlines. */
    km_fake_clock_t clock;
    km_fake_clock_init(&clock, 0);
    assert(km_trajectory_set_target_velocity(&t, 1000, clock.now_us) == KM_OK);
    assert(km_fake_clock_run_trajectory(&clock, &t, KM_TRAJECTORY_TEST_RAMP_US) == KM_OK);
    assert(km_trajectory_get_velocity(&t) == 1000);

    assert(km_trajectory_set_limits(&t, 100, 1000000, 1000000) == KM_OK);
    assert(km_fake_clock_run_trajectory(&clock, &t, 2 * KM_TRAJECTORY_TEST_RAMP_US) == KM_OK);
    assert(km_trajectory_get_velocity(&t) == 100);

    printf("PASS: test_trajectory_extreme_inputs_and_limit_changes\n");
}

static void test_trajectory_fake_clock_execution(void)
{
    km_trajectory_t traj;
    mock_driver_t mock;
    mock_driver_init(&mock, 2, 20, false);
    km_driver_set_enable(&mock.base, true);

    km_trajectory_init(&traj, 1000, 1000000, 1000000);
    km_trajectory_set_jerk(&traj, 100000000);
    km_trajectory_bind_driver(&traj, &mock.base);

    km_fake_clock_t clock;
    km_fake_clock_init(&clock, 10000);

    km_trajectory_set_target_velocity(&traj, 1000, clock.now_us);
    km_fake_clock_run_trajectory(&clock, &traj, 20000);

    /* Jerk-limited ramp: accel is reached after t1 = sqrt(2*v/a) = 4.47 ms, covering
     * a*t1^3/6 = 1.49 microsteps; cruising the remaining microseconds at 1 kHz covers
     * the rest, for 7 whole microsteps in a 20 ms pulse train. Stepping runs through
     * the shared reference executor now, not the driver's own edge callback, so the
     * count is read off the reference; pulse width/spacing are step_segments.c's own
     * contract (tests/test_step_segments.c, tests/test_trajectory_window.c case 1). */
    assert(km_ref_position(&clock.ref) == 7);
    assert(km_trajectory_get_position(&traj) == 7);

    printf("PASS: test_trajectory_fake_clock_execution\n");
}

static void test_trajectory_cancellation_and_emergency_stop(void)
{
    km_trajectory_t traj;
    mock_driver_t mock;
    mock_driver_init(&mock, 2, 10, false);
    km_driver_set_enable(&mock.base, true);

    km_trajectory_init(&traj, 5000, 4000, 4000);
    km_trajectory_bind_driver(&traj, &mock.base);

    uint32_t now_us = 100000;
    km_fake_clock_t clock;
    km_fake_clock_init(&clock, now_us);
    km_trajectory_set_target_velocity(&traj, 2000, now_us);

    for (int i = 0; i < 55; i++) {
        now_us += 10000;
        km_fake_clock_run_trajectory(&clock, &traj, now_us);
        km_trajectory_update(&traj, now_us);
    }
    assert(km_trajectory_get_velocity(&traj) == 2000);

    km_trajectory_cancel(&traj, now_us);
    for (int i = 0; i < 65; i++) {
        now_us += 10000;
        km_fake_clock_run_trajectory(&clock, &traj, now_us);
        km_trajectory_update(&traj, now_us);
    }
    assert(km_trajectory_get_velocity(&traj) == 0);
    assert(!km_trajectory_is_moving(&traj));

    km_trajectory_set_target_velocity(&traj, 3000, now_us);
    for (int i = 0; i < 20; i++) {
        now_us += 10000;
        km_fake_clock_run_trajectory(&clock, &traj, now_us);
        km_trajectory_update(&traj, now_us);
    }
    assert(km_trajectory_get_velocity(&traj) > 0);

    km_trajectory_emergency_stop(&traj);
    assert(km_trajectory_get_velocity(&traj) == 0);
    assert(!km_trajectory_is_moving(&traj));
    assert(mock.shutdown_called);
    printf("PASS: test_trajectory_cancellation_and_emergency_stop\n");
}

static void test_trajectory_time_wrap(void)
{
    km_trajectory_t traj;
    mock_driver_t mock;
    mock_driver_init(&mock, 2, 10, false);
    km_driver_set_enable(&mock.base, true);

    km_trajectory_init(&traj, 2000, 4000, 4000);
    km_trajectory_bind_driver(&traj, &mock.base);

    uint32_t now_us = UINT32_MAX - 50000u;
    km_trajectory_set_target_velocity(&traj, 1000, now_us);

    /* A driver-bound trajectory advances on its step deadlines, so run it past the
     * 32-bit wrap through the deadline arithmetic rather than the analytic model. */
    km_fake_clock_t clock;
    km_fake_clock_init(&clock, now_us);
    assert(km_fake_clock_run_trajectory(&clock, &traj, now_us + 350000u) == KM_OK);

    assert(km_trajectory_get_velocity(&traj) == 1000);
    printf("PASS: test_trajectory_time_wrap\n");
}

static void test_motion_diagnostics_ramp_analytic_reference(void)
{
    km_trajectory_t t;
    mock_driver_t m;
    mock_driver_init(&m, 2, 10, false);
    assert(km_driver_init(&m.base, NULL) == KM_OK);
    assert(km_driver_set_enable(&m.base, true) == KM_OK);
    assert(km_trajectory_init(&t, 1000, 1000, 1000) == KM_OK);
    km_trajectory_bind_driver(&t, &m.base);
    assert(km_trajectory_set_target_velocity(&t, 1000, 0) == KM_OK);

    km_fake_clock_t clock;
    km_fake_clock_init(&clock, 0);
    assert(km_fake_clock_run_trajectory(&clock, &t, 500000u) == KM_OK);

    /* Position tracks the reference's completed steps exactly; step timing
     * and spacing are step_segments.c's own contract, already covered by
     * tests/test_step_segments.c and tests/test_trajectory_window.c case 1. */
    assert(km_trajectory_get_position(&t) == km_ref_position(&clock.ref));
    printf("PASS: test_motion_diagnostics_ramp_analytic_reference\n");
}

static void test_motion_diagnostics_1us_updates(void)
{
    km_trajectory_t t;
    assert(km_trajectory_init(&t, 1000, 1000, 1000) == KM_OK);
    assert(km_trajectory_set_target_velocity(&t, 1000, 0) == KM_OK);

    /* v/a = 1 s of linear ramp, plus the jerk limit's 50 ms to reach full accel;
     * per-microsecond truncation in the integrator adds a little, so run past it. */
    for (uint32_t i = 1; i <= 1200000; ++i) {
        km_trajectory_update(&t, i);
    }

    assert(km_trajectory_get_velocity(&t) == 1000);
    printf("PASS: test_motion_diagnostics_1us_updates\n");
}

static void test_motion_diagnostics_large_ramp_overflow_free(void)
{
    km_trajectory_t t;
    assert(km_trajectory_init(&t, 10000, 10000, 10000) == KM_OK);
    assert(km_trajectory_set_target_velocity(&t, 10000, 0) == KM_OK);

    /* The 1 s linear ramp plus the 50 ms the jerk limit needs to reach full accel. */
    km_trajectory_update(&t, 1100000);
    assert(km_trajectory_get_velocity(&t) == 10000);
    printf("PASS: test_motion_diagnostics_large_ramp_overflow_free\n");
}

static void test_motion_diagnostics_missing_driver_ops(void)
{
    const km_driver_ops_t empty_ops = {0};
    km_driver_t empty = { .ops = &empty_ops, .enabled = true, .initialized = true };

    assert(km_driver_set_direction(&empty, KM_DIR_FORWARD, 1000) == KM_ERR_NOT_SUPPORTED);
    printf("PASS: test_motion_diagnostics_missing_driver_ops\n");
}

static void test_motion_driver_fault_and_uncertainty(void)
{
    km_trajectory_t t;
    mock_driver_t m;
    mock_driver_init(&m, 2, 10, false);
    assert(km_driver_set_enable(&m.base, true) == KM_OK);
    assert(km_trajectory_init(&t, 1000, 1000, 1000) == KM_OK);
    km_trajectory_bind_driver(&t, &m.base);
    assert(km_trajectory_set_target_velocity(&t, 1000, 0) == KM_OK);

    km_fake_clock_t clock;
    km_fake_clock_init(&clock, 0);
    /* The jerk-limited ramp only reaches its first microstep after a while. */
    assert(km_fake_clock_run_trajectory(&clock, &t, 10000u) == KM_OK);
    assert(km_trajectory_is_moving(&t));

    /* A driver fault noticed between ticks stops the move the same way a
     * failed edge call used to, through km_trajectory_service. */
    m.injected_fault = KM_FAULT_DRIVER_FAULT;
    assert(km_fake_clock_run_trajectory(&clock, &t, 11000u) == KM_OK);
    assert(km_trajectory_is_position_uncertain(&t));
    assert(!km_trajectory_is_moving(&t));
    printf("PASS: test_motion_driver_fault_and_uncertainty\n");
}

static void test_motion_numeric_contracts_validation(void)
{
    km_trajectory_t t;
    assert(km_trajectory_init(&t, UINT32_MAX, 1000, 1000) == KM_ERR_INVALID_PARAM);
    assert(km_trajectory_init(&t, 1000, UINT32_MAX, 1000) == KM_ERR_INVALID_PARAM);
    assert(km_trajectory_init(&t, 1000, 1000, UINT32_MAX) == KM_ERR_INVALID_PARAM);
    assert(km_trajectory_init(&t, 0, 1000, 1000) == KM_ERR_INVALID_PARAM);

    assert(km_trajectory_init(&t, 1000, 1000, 1000) == KM_OK);
    assert(km_trajectory_set_limits(&t, UINT32_MAX, 1000, 1000) == KM_ERR_INVALID_PARAM);

    assert(km_trajectory_set_target_position(&t, INT64_MIN, 1000, 0) == KM_ERR_INVALID_PARAM);
    assert(km_trajectory_set_target_position(&t, INT64_MAX, 1000, 0) == KM_ERR_INVALID_PARAM);

    assert(km_trajectory_set_target_velocity(&t, INT32_MIN, 0) == KM_OK);
    assert(t.target_velocity_steps_s == -1000);
    printf("PASS: test_motion_numeric_contracts_validation\n");
}

static void test_trajectory_stationary_point_settlement(void)
{
    km_trajectory_t traj;
    mock_driver_t m;
    mock_driver_init(&m, 2, 10, false);
    km_driver_set_enable(&m.base, true);
    km_trajectory_init(&traj, 1000, 1000, 1000);
    km_trajectory_bind_driver(&traj, &m.base);
    
    km_trajectory_set_target_position(&traj, 100, 1000, 0);
    
    km_fake_clock_t clock;
    km_fake_clock_init(&clock, 0);
    km_fake_clock_run_trajectory(&clock, &traj, 1000000);
    
    assert(!km_trajectory_is_moving(&traj));
    assert(km_trajectory_get_position(&traj) == 100);
    printf("PASS: test_trajectory_stationary_point_settlement\n");
}

static void test_trajectory_driver_speed_limit_enforcement(void)
{
    km_trajectory_t traj;
    mock_driver_t m;
    mock_driver_init(&m, 2, 10, false);
    km_driver_set_enable(&m.base, true);
    memset(&traj, 0, sizeof(traj));
    assert(km_trajectory_init(&traj, 20000, 1000, 1000) == KM_OK);
    km_trajectory_bind_driver(&traj, &m.base);
    
    m.base.caps.max_velocity_steps_s = 100;
    km_trajectory_set_target_velocity(&traj, 1000, 0);
    assert(traj.target_velocity_steps_s <= 100);
    printf("PASS: test_trajectory_driver_speed_limit_enforcement\n");
}

static void test_trajectory_negative_substep_cancellation(void)
{
    km_trajectory_t traj;
    mock_driver_t m;
    mock_driver_init(&m, 2, 10, false);
    km_driver_set_enable(&m.base, true);
    km_trajectory_init(&traj, 1000, 1000, 1000);
    km_trajectory_bind_driver(&traj, &m.base);

    km_trajectory_set_target_velocity(&traj, -1000, 0);
    km_fake_clock_t clock;
    km_fake_clock_init(&clock, 0);
    assert(km_fake_clock_run_trajectory(&clock, &traj, 5000) == KM_OK);
    assert(km_trajectory_cancel(&traj, 5000) == KM_OK);

    assert(km_fake_clock_run_trajectory(&clock, &traj, 500000) == KM_OK);
    assert(!km_trajectory_is_moving(&traj));
    assert(llabs((long long)km_ref_position(&clock.ref)) <= 5);
    printf("PASS: test_trajectory_negative_substep_cancellation\n");
}

/* test_edge_accounting_and_faults (the per-edge pulse/fault accounting this
 * tested: t.pulse_edge_count, t.has_deadline, a late km_trajectory_service
 * skipping a microstep boundary) tested the removed per-edge service itself;
 * both fields it asserted on are gone. The tick API's equivalents are the
 * B2 judge's window-fault and resync-after-stop cases
 * (tests/test_trajectory_window.c cases 4, 8, 11), so it is deleted rather
 * than ported. */

static void test_repeated_target_is_harmless_write(void)
{
    km_trajectory_t t;
    mock_driver_t m;
    mock_driver_init(&m, 2, 10, false);
    assert(km_driver_set_enable(&m.base, true) == KM_OK);
    assert(km_trajectory_init(&t, 18000, 10000, 15000) == KM_OK);
    km_trajectory_bind_driver(&t, &m.base);
    assert(km_trajectory_set_target_velocity(&t, 9000, 0) == KM_OK);
    km_fake_clock_t clock;
    km_fake_clock_init(&clock, 0);
    assert(km_fake_clock_run_trajectory(&clock, &t, 300000) == KM_OK);

    /* A repeated identical target at the model's own update time is a harmless
     * write: the planned motion keeps its state, while the command sequence
     * advances for the controller. (The rest of the original test covered a
     * stale command past the missed-deadline lateness bound -- the removed
     * per-edge service's own concept, whose tick-API equivalent is the
     * underrun case, tests/test_trajectory_window.c case 11.) */
    uint32_t now_us = t.last_update_us;
    km_trajectory_t before = t;
    uint32_t seq = t.command_seq;
    assert(km_trajectory_set_target_velocity(&t, 9000, now_us) == KM_OK);
    assert(t.command_seq == seq + 1);
    assert(t.rate.phase_whole == before.rate.phase_whole);
    assert(t.rate.phase_frac == before.rate.phase_frac);
    assert(t.rate.rate == before.rate.rate);
    assert(t.rate.accel == before.rate.accel);
    assert(t.rate.target_rate_internal == before.rate.target_rate_internal);
    assert(t.current_velocity_steps_s == before.current_velocity_steps_s);
    assert(t.current_position_steps == before.current_position_steps);
    assert(t.model_position_steps == before.model_position_steps);

    assert(km_trajectory_set_target_velocity(&t, 20000, now_us) == KM_OK);
    assert(t.target_velocity_steps_s == 18000);
    printf("PASS: test_repeated_target_is_harmless_write\n");
}

static void test_trajectory_publish_and_collect(void)
{
    km_trajectory_t plan, edge;
    mock_driver_t m;
    mock_driver_init(&m, 2, 10, false);
    assert(km_driver_set_enable(&m.base, true) == KM_OK);
    
    assert(km_trajectory_init(&plan, 18000, 20000, 20000) == KM_OK);
    km_trajectory_bind_driver(&plan, &m.base);
    
    assert(km_trajectory_set_target_velocity(&plan, 9000, 0) == KM_OK);
    
    edge = plan;
    km_fake_clock_t clock;
    km_fake_clock_init(&clock, 0);
    assert(km_fake_clock_run_trajectory(&clock, &edge, 100000) == KM_OK);
    
    /* The controller's order: collect the edge output, then command the plan. */
    km_trajectory_collect(&plan, &edge);
    assert(km_trajectory_set_target_velocity(&plan, 3000, 100000) == KM_OK);
    assert(plan.command_seq != edge.command_seq);
    
    int64_t live_position = edge.model_position_steps;
    bool published = km_trajectory_publish(&edge, &plan, 100000);
    assert(published);
    assert(edge.command_seq == plan.command_seq);
    assert(edge.target_velocity_steps_s == 3000);
    /* The edge keeps its own (newer) state and serves the command at once. */
    assert(edge.model_position_steps == live_position);
    assert(!km_trajectory_publish(&edge, &plan, 100000)); /* nothing new */
    
    km_trajectory_collect(&plan, &edge);
    assert(plan.current_position_steps == edge.current_position_steps);
    /* Output still due is collected as PENDING, which keeps the plan moving. */

    printf("PASS: test_trajectory_publish_and_collect\n");
}

/* Regression: km_trajectory_collect never mirrored has_target_position from
 * the edge back to the plan (only rate/position/activity fields), so once a
 * move actually arrives (km_trajectory_observe, edge-only, clears it there),
 * the plan's own copy -- set once when the command was issued and never
 * refreshed on this side -- stayed true forever. km_trajectory_set_limits_at
 * (src/operation_core.c resume_operation_motion, called for every fresh
 * selection to restore the gear's own motion limits) reads that stale flag
 * to decide whether to re-arm rate.target_rate_internal to the velocity
 * limit, and its own accept_command then recomputes is_active from that
 * freshly nonzero rate -- making an already-arrived, fully idle trajectory
 * look like it is still moving right when the very next
 * km_trajectory_bind_driver (selector -> gear) checks, which then
 * emergency-stops it and reports a misleading driver fault instead of
 * rebinding cleanly: the actual ercf_2_0 whole-stack sim failure right
 * after PRELOAD's selector reaches gate 0 and the servo engages. */
static void test_trajectory_collect_clears_stale_has_target_position(void)
{
    km_trajectory_t plan, edge;
    mock_driver_t m;
    mock_driver_init(&m, 2, 10, false);
    assert(km_driver_set_enable(&m.base, true) == KM_OK);

    assert(km_trajectory_init(&edge, 16000, 96000, 96000) == KM_OK);
    km_trajectory_bind_driver(&edge, &m.base);
    assert(km_trajectory_set_target_position(&edge, 320, 16000, 0) == KM_OK);
    assert(edge.has_target_position);

    km_fake_clock_t clock;
    km_fake_clock_init(&clock, 0);
    assert(km_fake_clock_run_trajectory(&clock, &edge, 2000000) == KM_OK);
    /* Arrived (not just timed out): confirms the move actually settles. */
    assert(!km_trajectory_is_moving(&edge));
    assert(!edge.has_target_position);
    assert(edge.model_position_steps == 320);

    /* The plan still carries the command's own original has_target_position
     * (set true when it was first issued and, before this fix, never
     * refreshed by collect). */
    km_trajectory_init(&plan, 16000, 96000, 96000);
    km_trajectory_bind_driver(&plan, &m.base);
    plan.has_target_position = true;

    km_trajectory_collect(&plan, &edge);
    assert(!plan.has_target_position);

    /* The controller's own next step after every fresh selection
     * (resume_operation_motion): restore the shared trajectory's limits.
     * Before this fix, the stale has_target_position made this re-arm a
     * live rate target on an already-arrived trajectory. */
    assert(km_trajectory_set_limits_at(&plan, 18000, 20000, 20000, 2000000) == KM_OK);
    assert(!km_trajectory_is_moving(&plan));

    printf("PASS: test_trajectory_collect_clears_stale_has_target_position\n");
}

/* Regression: km_trajectory_recover declares a new absolute baseline (e.g.
 * "0 at the home switch", src/selector_home.c's found()), but the driver's
 * own step accumulator (mcu/stepdir_gpio.c's s->position, stood in for here
 * by the fake clock's reference stepper) is a net delta since that driver's
 * first activation and is never rebased to match -- the very next
 * km_trajectory_observe call reports completed_steps in that unrebased
 * frame. Before this fix, current_position_steps silently reverted to the
 * driver's raw, un-rebased count: permanently offset from
 * target_position_steps (always stated in the model's current, rebased
 * frame) by however many net steps happened before the rebase. A move
 * issued after a recover() then never satisfied completed_steps ==
 * target_position_steps, and (with the is_active fix above requiring that
 * match) km_trajectory_is_moving() never went false -- the actual ercf_2_0
 * sim failure: stuck in SELECT_MOVE/SELECT_HOME forever after a genuine
 * home-then-gate-move sequence, engage_feeder never reached. */
static void test_trajectory_recover_rebase_survives_driver_accumulator(void)
{
    km_trajectory_t traj;
    mock_driver_t m;
    mock_driver_init(&m, 2, 10, false);
    assert(km_driver_set_enable(&m.base, true) == KM_OK);

    assert(km_trajectory_init(&traj, 16000, 96000, 96000) == KM_OK);
    km_trajectory_bind_driver(&traj, &m.base);
    assert(km_trajectory_set_target_position(&traj, 320, 16000, 0) == KM_OK);

    km_fake_clock_t clock;
    km_fake_clock_init(&clock, 0);
    assert(km_fake_clock_run_trajectory(&clock, &traj, 2000000) == KM_OK);
    assert(!km_trajectory_is_moving(&traj));
    /* The driver's own accumulator (the fake clock's reference stepper,
     * standing in for mcu/stepdir_gpio.c's s->position) now reads 320 net
     * steps since activation, and never resets on its own. */

    /* selector_home.c's found(): declare "0" as the new baseline (the home
     * switch) on the SAME driver, without ever touching its own step
     * count. */
    km_driver_shutdown(&m.base);
    assert(km_trajectory_recover(&traj, 0, 2000000) == KM_OK);
    assert(km_driver_set_enable(&m.base, true) == KM_OK);

    /* The next move, in the rebased frame: the same fake clock keeps
     * counting its reference stepper from where it left off, exactly like
     * real hardware never resetting. */
    assert(km_trajectory_set_target_position(&traj, 320, 16000, 2000000) == KM_OK);
    assert(km_fake_clock_run_trajectory(&clock, &traj, 4000000) == KM_OK);

    assert(!km_trajectory_is_moving(&traj));
    assert(!traj.has_target_position);
    assert(traj.current_position_steps == 320);

    printf("PASS: test_trajectory_recover_rebase_survives_driver_accumulator\n");
}

/* Regression: km_trajectory_recover cannot compute position_offset itself
 * when it follows km_trajectory_bind_driver switching to a DIFFERENT
 * physical driver (src/operation_core.c select_bind_stepper: selector <->
 * gear) -- current_position_steps at that moment is still the *previous*
 * driver's own last reported position, with no relation at all to the new
 * driver's own raw count. Computing a delta directly in recover (an
 * earlier version of this fix) produced a huge, wrong offset from that
 * stale value, permanently preventing completed_steps from ever equalling
 * target_position_steps on the new driver -- the actual ercf_2_0 sim
 * failure selecting a second gate after the gear had already fed on the
 * first one (PRELOAD BAY=3 after BAY=0: "the lane made no progress"). */
static void test_trajectory_recover_after_driver_switch_seeds_fresh_offset(void)
{
    km_trajectory_t traj;
    mock_driver_t gear, selector;
    mock_driver_init(&gear, 2, 10, false);
    mock_driver_init(&selector, 2, 10, false);
    assert(km_driver_set_enable(&gear.base, true) == KM_OK);

    assert(km_trajectory_init(&traj, 16000, 96000, 96000) == KM_OK);
    km_trajectory_bind_driver(&traj, &gear.base);
    assert(km_trajectory_set_target_position(&traj, 5000, 16000, 0) == KM_OK);

    km_fake_clock_t gear_clock;
    km_fake_clock_init(&gear_clock, 0);
    assert(km_fake_clock_run_trajectory(&gear_clock, &traj, 4000000) == KM_OK);
    assert(!km_trajectory_is_moving(&traj));
    assert(traj.current_position_steps == 5000);
    /* current_position_steps holds the gear's own 5000, entirely unrelated
     * to the selector's own hardware, about to be bound next. */

    km_driver_shutdown(&gear.base);
    km_trajectory_bind_driver(&traj, &selector.base);
    /* select_bind_stepper's own order: shutdown (recover needs it
     * disabled), recover, enable. */
    assert(km_trajectory_recover(&traj, 320, 4000000) == KM_OK);
    assert(km_driver_set_enable(&selector.base, true) == KM_OK);

    assert(km_trajectory_set_target_position(&traj, 640, 16000, 4000000) == KM_OK);
    km_fake_clock_t selector_clock;
    km_fake_clock_init(&selector_clock, 4000000);
    assert(km_fake_clock_run_trajectory(&selector_clock, &traj, 8000000) == KM_OK);

    assert(!km_trajectory_is_moving(&traj));
    assert(!traj.has_target_position);
    assert(traj.current_position_steps == 640);

    printf("PASS: test_trajectory_recover_after_driver_switch_seeds_fresh_offset\n");
}

/* Regression: a selector machine rebinds the shared trajectory's driver
 * mid-operation (gear <-> selector stepper, km_trajectory_bind_driver, once
 * idle, then a fresh command -- src/operation_core.c's select_bind_stepper
 * followed by enter_select_move/enter_select_home). mcu/controller.c's edge
 * copy only picks up a new command through km_trajectory_publish whenever
 * its own bookkeeping thinks the edge is still "moving" from an earlier
 * exchange (its idle raw-copy path is skipped then), so publish() itself
 * must carry the new driver and its direction sense over. A single fixed
 * feeder driver never rebinds mid-operation, so this had never mattered. */
static void test_trajectory_publish_adopts_rebound_driver(void)
{
    km_trajectory_t plan, edge;
    mock_driver_t gear, selector;
    mock_driver_init(&gear, 2, 10, false);
    mock_driver_init(&selector, 2, 10, false);
    assert(km_driver_set_enable(&gear.base, true) == KM_OK);
    assert(km_driver_set_enable(&selector.base, true) == KM_OK);

    assert(km_trajectory_init(&plan, 18000, 20000, 20000) == KM_OK);
    km_trajectory_bind_driver(&plan, &gear.base); /* Idle: a clean bind. */
    edge = plan;
    assert(edge.driver == &gear.base && !km_trajectory_is_moving(&edge));

    /* The core switches to the selector stepper while idle (rebind_driver's
     * own emergency-stop guard only fires while actually moving), then
     * issues its own move, which bumps command_seq like any command. */
    km_trajectory_bind_driver(&plan, &selector.base);
    plan.invert_direction = true;
    assert(km_trajectory_set_target_position(&plan, 5000, 4000, 100000) == KM_OK);
    assert(plan.command_seq != edge.command_seq);

    assert(km_trajectory_publish(&edge, &plan, 100000));
    assert(edge.driver == &selector.base);
    assert(edge.invert_direction == true);

    printf("PASS: test_trajectory_publish_adopts_rebound_driver\n");
}

/* Regression: the same rebind, but the edge still carries the GEAR's own
 * leftover phase/velocity from real motion (mcu/controller.c never resets
 * edge.rate on a rebind; only km_trajectory_collect/publish touch it), the
 * way it does whenever the exchange lands on the replan path instead of the
 * idle raw copy (test_trajectory_publish_adopts_rebound_driver above covers
 * only the from-idle case). Before this fix, km_trajectory_publish kept the
 * edge's old phase/rate underneath the selector's new target, so
 * km_rate_set_target_phase saw the gear's leftover motion direction and
 * refused the target as "behind the stopping distance", emergency-stopping
 * the whole operation (KM_FAULT_CONTROL_TIMING) instead of ever stepping the
 * selector -- the actual ercf_2_0 whole-stack sim failure this covers. */
static void test_trajectory_publish_replaces_stale_edge_phase_on_rebind(void)
{
    km_trajectory_t plan, edge;
    mock_driver_t gear, selector;
    mock_driver_init(&gear, 2, 10, false);
    mock_driver_init(&selector, 2, 10, false);
    assert(km_driver_set_enable(&gear.base, true) == KM_OK);
    assert(km_driver_set_enable(&selector.base, true) == KM_OK);

    assert(km_trajectory_init(&plan, 18000, 20000, 20000) == KM_OK);
    km_trajectory_bind_driver(&plan, &gear.base);
    assert(km_trajectory_set_target_velocity(&plan, 9000, 0) == KM_OK);
    edge = plan;

    /* Real gear motion, mid-ramp: the edge now has its own nonzero phase,
     * rate and (still accelerating) accel, exactly like mcu/controller.c's
     * timer ISR leaves it mid-move. */
    km_fake_clock_t clock;
    km_fake_clock_init(&clock, 0);
    assert(km_fake_clock_run_trajectory(&clock, &edge, 50000) == KM_OK);
    assert(edge.rate.accel != 0);
    km_trajectory_collect(&plan, &edge);

    /* The gear has since settled (whatever precedes select_bind_stepper in
     * src/operation_core.c ensures this before it ever runs), but the timer
     * ISR's edge copy -- ticking a period behind the task-context exchange
     * that will run this command -- has not caught up yet: its rate/accel
     * are still the gear's. */
    plan.is_active = false;
    plan.current_velocity_steps_s = 0;
    plan.stepper_active = false;

    /* select_bind_stepper's own order: rebind (plan reads idle, so this
     * succeeds cleanly), then recover onto the new axis's own baseline
     * before issuing its move. */
    km_trajectory_bind_driver(&plan, &selector.base);
    assert(plan.driver == &selector.base);
    km_driver_shutdown(&selector.base); /* km_trajectory_recover requires it disabled. */
    assert(km_trajectory_recover(&plan, 0, 100000) == KM_OK);
    assert(km_driver_set_enable(&selector.base, true) == KM_OK);
    assert(km_trajectory_set_target_position(&plan, 5000, 4000, 100000) == KM_OK);
    assert(plan.command_seq != edge.command_seq);

    /* The edge is still "moving" by its own (gear-era) bookkeeping, so this
     * is the replan path, not the idle raw copy -- the one that dropped the
     * driver (fixed above) and, until this fix, kept the stale rate/accel
     * underneath the selector's new target, so km_rate_set_target_phase saw
     * the gear's leftover motion direction (from accel alone, even with
     * rate back at 0: src/rate.c's motion_dir) and refused it as behind the
     * (unrelated) stopping distance, emergency-stopping the edge instead of
     * ever stepping the selector -- the actual ercf_2_0 sim failure. */
    assert(km_trajectory_is_moving(&edge));
    assert(km_trajectory_publish(&edge, &plan, 100000));
    assert(!edge.faulted);
    assert(edge.driver == &selector.base);
    assert(edge.rate.rate == plan.rate.rate);
    assert(edge.rate.accel == plan.rate.accel);
    assert(edge.rate.phase_whole == plan.rate.phase_whole);
    assert(edge.has_target_position && edge.target_position_steps == 5000);

    printf("PASS: test_trajectory_publish_replaces_stale_edge_phase_on_rebind\n");
}

/* Regression: the same stale-edge-phase hazard, but with the driver
 * unchanged throughout -- src/selector_home.c's found() calls
 * km_trajectory_emergency_stop(plan) then km_trajectory_recover(plan, ...)
 * back to back, directly on the task-side plan, both inside one
 * km_core_update() call and never through mcu/controller.c's exchange()
 * (the only place a plan fault is normally propagated to the edge). The
 * timer ISR's edge copy is still executing the abandoned homing search --
 * same driver, real nonzero rate/accel -- when the very next command (the
 * move to the found gate) publishes. Before this fix, "driver unchanged"
 * kept that stale search motion under the new target and
 * km_rate_set_target_phase refused it as behind the (unrelated) stopping
 * distance, emergency-stopping the edge and, via km_trajectory_collect,
 * the plan too -- reported by mcu/controller.c as the generic
 * KM_FAULT_CONTROL_TIMING instead of ever reaching the gate: the actual
 * ercf_2_0 whole-stack sim failure (PRELOAD's homing-to-gate-0 move). */
static void test_trajectory_publish_replaces_stale_edge_phase_on_same_driver_recover(void)
{
    km_trajectory_t plan, edge;
    mock_driver_t selector;
    mock_driver_init(&selector, 2, 10, false);
    assert(km_driver_set_enable(&selector.base, true) == KM_OK);

    assert(km_trajectory_init(&plan, 18000, 20000, 20000) == KM_OK);
    km_trajectory_bind_driver(&plan, &selector.base);
    assert(km_trajectory_set_target_velocity(&plan, -9000, 0) == KM_OK);
    edge = plan;

    /* Real homing-search motion, mid-ramp: the edge now has its own
     * nonzero phase, rate and accel, exactly like mcu/controller.c's timer
     * ISR leaves it mid-search. */
    km_fake_clock_t clock;
    km_fake_clock_init(&clock, 0);
    assert(km_fake_clock_run_trajectory(&clock, &edge, 50000) == KM_OK);
    assert(edge.rate.rate != 0);
    km_trajectory_collect(&plan, &edge);

    /* selector_home.c's found(): emergency_stop then recover, both on plan
     * alone, driver never rebound. The edge (a tick behind) has not caught
     * up to either call. */
    km_trajectory_emergency_stop(&plan);
    assert(km_trajectory_recover(&plan, 0, 100000) == KM_OK);
    assert(plan.driver == &selector.base);
    assert(km_driver_set_enable(&selector.base, true) == KM_OK);
    assert(km_trajectory_set_target_position(&plan, 320, 4000, 100000) == KM_OK);
    assert(plan.command_seq != edge.command_seq);

    assert(km_trajectory_is_moving(&edge));
    assert(km_trajectory_publish(&edge, &plan, 100000));
    assert(!edge.faulted);
    assert(edge.driver == &selector.base);
    assert(edge.rate.rate == plan.rate.rate);
    assert(edge.rate.accel == plan.rate.accel);
    assert(edge.rate.phase_whole == plan.rate.phase_whole);
    assert(edge.has_target_position && edge.target_position_steps == 320);

    printf("PASS: test_trajectory_publish_replaces_stale_edge_phase_on_same_driver_recover\n");
}

/* An ordinary command from rest on the same driver keeps the edge's own
 * live state: the edge may have completed steps since the plan last
 * collected (a BoxTurtle lane swap hung when this was overwritten). */
static void test_trajectory_publish_keeps_edge_state_for_a_command_from_rest(void)
{
    km_trajectory_t plan, edge;
    mock_driver_t feeder;
    mock_driver_init(&feeder, 2, 10, false);
    assert(km_driver_set_enable(&feeder.base, true) == KM_OK);
    assert(km_trajectory_init(&plan, 18000, 20000, 20000) == KM_OK);
    km_trajectory_bind_driver(&plan, &feeder.base);
    edge = plan;
    km_trajectory_observe(&edge, 5, false);
    assert(plan.rate.rate == 0 && plan.rate.accel == 0);
    assert(km_trajectory_set_target_velocity(&plan, 9000, 0) == KM_OK);
    assert(km_trajectory_publish(&edge, &plan, 0));
    assert(!edge.faulted && edge.current_position_steps == 5);
    printf("PASS: test_trajectory_publish_keeps_edge_state_for_a_command_from_rest\n");
}

/* A ROTARY gate with a reversed gear (invert_direction) flips DIR at the
 * edge, so the driver's raw count runs backwards while the move goes
 * forward: observed positions and deltas must keep the model's sign. */
static void test_trajectory_observe_maps_inverted_direction(void)
{
    km_trajectory_t edge;
    mock_driver_t gear;
    mock_driver_init(&gear, 2, 10, false);
    assert(km_trajectory_init(&edge, 18000, 20000, 20000) == KM_OK);
    km_trajectory_bind_driver(&edge, &gear.base);
    assert(km_trajectory_recover(&edge, 100, 0) == KM_OK);
    edge.invert_direction = true; /* Set at gear bind, after any recover. */
    km_trajectory_observe(&edge, -40, true);    /* First raw count seeds 100. */
    assert(edge.current_position_steps == 100);
    km_trajectory_observe(&edge, -140, true);   /* 100 raw steps backwards... */
    assert(edge.current_position_steps == 200); /* ...are 100 steps forward. */
    km_trajectory_resync(&edge, -150, false);
    assert(edge.current_position_steps == 150 && edge.model_position_steps == 150);
    printf("PASS: test_trajectory_observe_maps_inverted_direction\n");
}

/* A lane scale of 1:1 (here written 7/7, which GCD-reduces to 1/1) is a no-op:
 * nothing in the trajectory state changes. */
static void test_trajectory_lane_scale_identity(void)
{
    km_trajectory_t t, before;
    assert(km_trajectory_init(&t, 16000, 96000, 96000) == KM_OK);
    before = t;
    assert(km_trajectory_set_lane_scale(&t, 7, 7) == KM_OK);
    assert(t.scale_num == 1 && t.scale_den == 1);
    assert(memcmp(&t, &before, sizeof(t)) == 0);
    printf("PASS: test_trajectory_lane_scale_identity\n");
}

/* The scale multiplies the physical step count the driver sees while the
 * reference position it was commanded to stays put. */
static void test_trajectory_lane_scale_move(void)
{
    km_trajectory_t traj;
    mock_driver_t m;
    mock_driver_init(&m, 2, 10, false);
    assert(km_driver_set_enable(&m.base, true) == KM_OK);
    assert(km_trajectory_init(&traj, 16000, 96000, 96000) == KM_OK);
    assert(km_trajectory_set_lane_scale(&traj, 11, 10) == KM_OK);
    km_trajectory_bind_driver(&traj, &m.base);
    assert(km_trajectory_set_target_position(&traj, 1000, 16000, 0) == KM_OK);
    km_fake_clock_t clock;
    km_fake_clock_init(&clock, 0);
    assert(km_fake_clock_run_trajectory(&clock, &traj, 2000000) == KM_OK);
    assert(!km_trajectory_is_moving(&traj));
    assert(km_ref_position(&clock.ref) == 1100);
    assert(km_trajectory_get_position(&traj) == 1000);
    assert(traj.current_position_steps == 1100);
    printf("PASS: test_trajectory_lane_scale_move\n");
}

/* Limits and velocities are stated in reference units, so the scale shows up
 * in the physical struct fields and cancels back out of the reference getters. */
static void test_trajectory_lane_scale_velocity_and_limits(void)
{
    km_trajectory_t t;
    assert(km_trajectory_init(&t, 10000, 20000, 20000) == KM_OK);
    assert(km_trajectory_set_lane_scale(&t, 11, 10) == KM_OK);
    assert(t.max_velocity_steps_s == 11000);
    assert(t.acceleration_steps_s2 == 22000);

    uint32_t now_us = 1000000;
    assert(km_trajectory_set_target_velocity(&t, 1000, now_us) == KM_OK);
    for (int i = 0; i < 55; i++) {
        now_us += 10000;
        km_trajectory_update(&t, now_us);
    }
    assert(t.current_velocity_steps_s == 1100);
    assert(km_trajectory_get_velocity(&t) == 1000);

    assert(km_trajectory_set_jerk(&t, 100000) == KM_OK);
    assert(t.jerk_steps_s3 == 110000);
    printf("PASS: test_trajectory_lane_scale_velocity_and_limits\n");
}

/* Changing the scale rebases the stored physical position, so the reported
 * reference position is unchanged in both directions. */
static void test_trajectory_lane_scale_keeps_reference_position(void)
{
    km_trajectory_t traj;
    mock_driver_t m;
    mock_driver_init(&m, 2, 10, false);
    assert(km_driver_set_enable(&m.base, true) == KM_OK);
    assert(km_trajectory_init(&traj, 16000, 96000, 96000) == KM_OK);
    km_trajectory_bind_driver(&traj, &m.base);
    assert(km_trajectory_set_target_position(&traj, 500, 16000, 0) == KM_OK);
    km_fake_clock_t clock;
    km_fake_clock_init(&clock, 0);
    assert(km_fake_clock_run_trajectory(&clock, &traj, 2000000) == KM_OK);
    assert(!km_trajectory_is_moving(&traj));
    assert(km_trajectory_get_position(&traj) == 500);

    assert(km_trajectory_set_lane_scale(&traj, 11, 10) == KM_OK);
    assert(km_trajectory_get_position(&traj) == 500);
    assert(km_trajectory_set_lane_scale(&traj, 1, 1) == KM_OK);
    assert(km_trajectory_get_position(&traj) == 500);
    printf("PASS: test_trajectory_lane_scale_keeps_reference_position\n");
}

/* A live move cannot be rescaled underneath its own ramp. */
static void test_trajectory_lane_scale_refused_while_moving(void)
{
    km_trajectory_t t;
    assert(km_trajectory_init(&t, 10000, 20000, 20000) == KM_OK);
    uint32_t now_us = 1000000;
    assert(km_trajectory_set_target_velocity(&t, 1000, now_us) == KM_OK);
    now_us += 10000;
    km_trajectory_update(&t, now_us);
    assert(km_trajectory_is_moving(&t));
    assert(km_trajectory_set_lane_scale(&t, 11, 10) == KM_ERR_BUSY);
    assert(t.scale_num == 1);
    printf("PASS: test_trajectory_lane_scale_refused_while_moving\n");
}

/* A rebased limit outside the supported range, or a zero ratio, is rejected
 * without disturbing the stored scale. */
static void test_trajectory_lane_scale_refuses_out_of_range(void)
{
    km_trajectory_t t;
    assert(km_trajectory_init(&t, KM_MOTION_MAX_VELOCITY_STEPS_S, 1000, 1000) == KM_OK);
    assert(km_trajectory_set_lane_scale(&t, 2, 1) == KM_ERR_INVALID_PARAM);
    assert(t.scale_num == 1 && t.scale_den == 1);
    assert(km_trajectory_set_lane_scale(&t, 0, 1) == KM_ERR_INVALID_PARAM);
    printf("PASS: test_trajectory_lane_scale_refuses_out_of_range\n");
}

int main(void)
{
    printf("--- Running Trajectory Generator Tests ---\n");
    test_trajectory_observe_maps_inverted_direction();
    test_trajectory_publish_keeps_edge_state_for_a_command_from_rest();
    test_trajectory_ramp_and_steady();
    test_trajectory_zero_crossing_reversal();
    test_trajectory_substep_accumulation_low_speed();
    test_trajectory_extreme_inputs_and_limit_changes();
    test_trajectory_fake_clock_execution();
    test_trajectory_cancellation_and_emergency_stop();
    test_trajectory_time_wrap();
    test_motion_diagnostics_ramp_analytic_reference();
    test_motion_diagnostics_1us_updates();
    test_motion_diagnostics_large_ramp_overflow_free();
    test_motion_diagnostics_missing_driver_ops();
    test_motion_driver_fault_and_uncertainty();
    test_motion_numeric_contracts_validation();
    test_trajectory_stationary_point_settlement();
    test_trajectory_driver_speed_limit_enforcement();
    test_trajectory_negative_substep_cancellation();
    test_repeated_target_is_harmless_write();
    test_trajectory_publish_and_collect();
    test_trajectory_collect_clears_stale_has_target_position();
    test_trajectory_recover_rebase_survives_driver_accumulator();
    test_trajectory_recover_after_driver_switch_seeds_fresh_offset();
    test_trajectory_publish_adopts_rebound_driver();
    test_trajectory_publish_replaces_stale_edge_phase_on_rebind();
    test_trajectory_publish_replaces_stale_edge_phase_on_same_driver_recover();
    test_trajectory_lane_scale_identity();
    test_trajectory_lane_scale_move();
    test_trajectory_lane_scale_velocity_and_limits();
    test_trajectory_lane_scale_keeps_reference_position();
    test_trajectory_lane_scale_refused_while_moving();
    test_trajectory_lane_scale_refuses_out_of_range();
    printf("All Trajectory Tests Passed.\n\n");
    return 0;
}
