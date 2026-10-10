// SPDX-License-Identifier: GPL-3.0-or-later
#include <stdio.h>
#include <stdlib.h>
#include <math.h>
#include <assert.h>
#include "klipper_mmu/rate.h"

static double get_real_v(km_rate_t *kr) {
    return (double)kr->rate * 1e6 / 4294967296.0;
}
static double get_real_a(km_rate_t *kr) {
    return (double)kr->accel * 1e12 / 281474976710656.0;
}

static int64_t steps_per_s3_to_q64(uint32_t j) {
    uint64_t whole = (uint64_t)j * 18ULL;
    uint64_t frac = ((uint64_t)j * 4467440737ULL + 5000000000ULL) / 10000000000ULL;
    return (int64_t)(whole + frac);
}

struct limit_case {
    uint32_t vmax, amax, dmax, jmax;
};

static void check_state(km_rate_t *kr, struct limit_case *l, int32_t dt, int64_t old_a) {
    int64_t delta_a = kr->accel - old_a;
    int64_t abs_da = delta_a < 0 ? -delta_a : delta_a;
    int64_t allowed_da = (steps_per_s3_to_q64(l->jmax) * dt) >> 16;
    if (abs_da > allowed_da + 1) {
        printf("Jerk limit exceeded! %lld > %lld\n", (long long)abs_da, (long long)allowed_da);
        exit(1);
    }
    
    double a = get_real_a(kr);
    double max_accel = fmax(l->amax, l->dmax);
    if (fabs(a) > max_accel + 0.1) {
        printf("Accel limit exceeded! a=%.2f\n", a);
        exit(1);
    }
    
    double v = get_real_v(kr);
    if (fabs(v) > l->vmax + 0.01) {
        printf("Rate limit exceeded! v=%.6f\n", v);
        exit(1);
    }
}

static void run_rate_test(struct limit_case *l) {
    km_rate_t kr;
    km_rate_init(&kr);
    km_rate_set_limits(&kr, l->vmax, l->amax, l->dmax, l->jmax);
    
    int32_t targets[] = { (int32_t)l->vmax, 0, -(int32_t)l->vmax, 0 };
    for (int t = 0; t < 4; t++) {
        km_rate_set_target_rate(&kr, targets[t]);
        int steps = 0;
        while (steps < 2000000) {
            int64_t old_a = kr.accel;
            km_rate_advance(&kr, 1000U); // 1ms
            
            check_state(&kr, l, 1000U, old_a);
            
            if (kr.rate == kr.target_rate_internal && kr.accel == 0) break;
            steps++;
        }
        if (kr.rate != kr.target_rate_internal || kr.accel != 0) {
            printf("Failed to reach target rate %d exactly! v=%.6f a=%.6f\n", targets[t], get_real_v(&kr), get_real_a(&kr));
            exit(1);
        }
    }
    
    // Retarget mid-ramp
    km_rate_init(&kr);
    km_rate_set_limits(&kr, l->vmax, l->amax, l->dmax, l->jmax);
    km_rate_set_target_rate(&kr, (int32_t)l->vmax);
    
    int steps = 0;
    while (get_real_v(&kr) < l->vmax / 2.0 && steps < 100000) {
        int64_t old_a = kr.accel;
        km_rate_advance(&kr, 1000U);
        check_state(&kr, l, 1000U, old_a);
        steps++;
    }
    
    // Reverse target mid-ramp
    km_rate_set_target_rate(&kr, -(int32_t)l->vmax);
    while (steps < 200000) {
        int64_t old_a = kr.accel;
        km_rate_advance(&kr, 1000U);
        check_state(&kr, l, 1000U, old_a);
        if (kr.rate == kr.target_rate_internal && kr.accel == 0) break;
        steps++;
    }
}

static void test_move_run(struct limit_case *l, int64_t target_dist, int edge_clocked) {
    km_rate_t kr;
    km_rate_init(&kr);
    km_rate_set_limits(&kr, l->vmax, l->amax, l->dmax, l->jmax);
    km_rate_set_target_rate(&kr, (int32_t)l->vmax);
    
    int dir = target_dist > 0 ? 1 : -1;
    km_rate_set_target_phase(&kr, target_dist);
    
    int steps = 0;
    int64_t max_p = 0, min_p = 0;
    
    int64_t last_rate = kr.rate;
    int64_t total_edges = 0;
    int64_t since_edge = 0; /* us since the previous edge: the pulse spacing */
    
    while (kr.target_phase_active && steps < 5000000) {
        int64_t old_a = kr.accel;
        int32_t dt = 1000;
        if (edge_clocked) {
            dt = km_rate_next_edge_us(&kr);
            if (dt < 0) dt = 1000;
        }
        
        last_rate = kr.rate;
        /* The microstep the driver is at: the floor moving up, the ceiling
         * moving down (leaving k downward is not an edge; reaching k-1 is). */
        int64_t old_edge = kr.phase_whole + (dir < 0 && kr.phase_frac != 0);
        
        km_rate_advance(&kr, (uint32_t)dt);
        
        since_edge += dt;
        if (edge_clocked) {
            int64_t edge = kr.phase_whole + (dir < 0 && kr.phase_frac != 0);
            int64_t delta = edge - old_edge;
            if (delta != 0) {
                total_edges += (delta > 0 ? delta : -delta);
                
                int64_t dist_to_boundary = kr.phase_frac;
                if (dist_to_boundary > 2147483648LL) {
                    dist_to_boundary = 4294967296LL - dist_to_boundary;
                }
                
                int64_t abs_rate = kr.rate < 0 ? -kr.rate : kr.rate;
                /* Edge times are whole us, plus a few Q32 units of rounding. */
                int64_t max_err = abs_rate + 256;
                
                if (dist_to_boundary > max_err) {
                    printf("Landing error exceeded! err=%lld max=%lld\n", (long long)dist_to_boundary, (long long)max_err);
                    exit(1);
                }
                
                kr.phase_whole = edge;
                kr.phase_frac = 0;
                if (since_edge < 2) {
                    printf("Edge interval below 2 us! %lld\n", (long long)since_edge);
                    exit(1);
                }
                since_edge = 0;
            }
        }
        
        int landed = !kr.target_phase_active;
        if (!landed) {
            check_state(&kr, l, dt, old_a);
        }
        
        if (kr.rate != 0) {
            if ((dir > 0 && kr.rate < 0) || (dir < 0 && kr.rate > 0)) {
                printf("Rate sign reversal in position move! rate=%.6f dir=%d\n", get_real_v(&kr), dir);
                exit(1);
            }
        }
        
        if (kr.phase_whole > max_p) max_p = kr.phase_whole;
        if (kr.phase_whole < min_p) min_p = kr.phase_whole;
        steps++;
    }
    
    if (kr.target_phase_active) {
        printf("Move to %lld did not finish!\n", (long long)target_dist);
        exit(1);
    }
    
    double last_v_real = (double)(last_rate < 0 ? -last_rate : last_rate) / 4294967296.0 * 1e6;
    if (last_v_real > 10.01) {
        printf("Final update rate too high! %.6f > 10\n", last_v_real);
        exit(1);
    }
    
    /* Landed means at rest: the speed cap must not become a new rate target. */
    for (int i = 0; i < 100; i++) km_rate_advance(&kr, 1000U);
    if (kr.phase_whole != target_dist || kr.phase_frac != 0 || kr.rate != 0 || kr.accel != 0) {
        printf("Move failed to stop exactly! p=%lld.%u v=%lld a=%lld\n", 
               (long long)kr.phase_whole, kr.phase_frac, (long long)kr.rate, (long long)kr.accel);
        exit(1);
    }
    if (dir > 0 && max_p > target_dist) {
        printf("Move overshot! target=%lld max=%lld\n", (long long)target_dist, (long long)max_p);
        exit(1);
    }
    if (dir < 0 && min_p < target_dist) {
        printf("Move overshot! target=%lld min=%lld\n", (long long)target_dist, (long long)min_p);
        exit(1);
    }
    if (edge_clocked && total_edges != (target_dist > 0 ? target_dist : -target_dist)) {
        printf("Edge count mismatch! edges=%lld target=%lld\n", (long long)total_edges, (long long)target_dist);
        exit(1);
    }
}

static void run_position_test(struct limit_case *l) {
    int64_t tgts[] = {1, 10, 100, 1000, 10000, 200000};
    for(int i=0; i<6; i++) {
        test_move_run(l, tgts[i], 0);
        test_move_run(l, -tgts[i], 0);
        test_move_run(l, tgts[i], 1);
        test_move_run(l, -tgts[i], 1);
    }
    
    // Retarget longer
    km_rate_t kr;
    km_rate_init(&kr);
    km_rate_set_limits(&kr, l->vmax, l->amax, l->dmax, l->jmax);
    km_rate_set_target_rate(&kr, (int32_t)l->vmax);
    km_rate_set_target_phase(&kr, 5000);
    
    for (int i=0; i<100; i++) {
        int64_t old_a = kr.accel;
        km_rate_advance(&kr, 1000U);
        check_state(&kr, l, 1000U, old_a);
    }
    km_rate_set_target_phase(&kr, 10000); // Longer
    
    int steps = 0;
    while(kr.target_phase_active && steps < 5000000) {
        int64_t old_a = kr.accel;
        km_rate_advance(&kr, 1000U);
        check_state(&kr, l, 1000U, old_a);
        steps++;
    }
    if (kr.phase_whole != 10000 || kr.rate != 0) {
        printf("Failed retarget longer!\n"); exit(1);
    }
    
    // Retarget inside stopping distance
    km_rate_init(&kr);
    km_rate_set_limits(&kr, l->vmax, l->amax, l->dmax, l->jmax);
    km_rate_set_target_rate(&kr, (int32_t)l->vmax);
    km_rate_set_target_phase(&kr, 20000);
    
    steps = 0;
    int refused = 0;
    while(kr.target_phase_active && steps < 5000000) {
        if (kr.phase_whole > 19500 && !refused) {
            if (km_rate_set_target_phase(&kr, 19510) == -1) {
                refused = 1; // Expected to refuse
            }
        }
        int64_t old_a = kr.accel;
        km_rate_advance(&kr, 1000U);
        check_state(&kr, l, 1000U, old_a);
        steps++;
    }
    if (!refused) {
        printf("Failed to refuse retarget inside stopping distance!\n"); exit(1);
    }
    if (kr.phase_whole != 20000 || kr.rate != 0) {
        printf("Failed to stop at original target after refuse!\n"); exit(1);
    }
}

/* Regression: once rate and accel have both settled to exactly zero exactly
 * on the target phase (phase_whole == target_phase, phase_frac == 0 -- the
 * plant's own ground truth confirms a short move like a selector's homing
 * or gate seek does land exactly, since it counts whole microsteps),
 * get_move_direction (above) reports dir=0 (the remaining distance is
 * already 0), and km_rate_advance's old arrival check required dir != 0 to
 * ever clear target_phase_active. That made the clear self-defeating right
 * at the one moment it was true: target_phase_active (and the
 * km_trajectory_is_moving()/is_active it drives, src/trajectory.c
 * refresh_mirrors) latched stuck true forever, because every later tick
 * also finds dir == 0 (rate/accel are already at rest) with nothing left
 * to move. mcu/controller.c's exchange() and src/operation_core.c's
 * engage_feeder then see the trajectory as still moving on the very next
 * driver bind (selector -> gear, once PRELOAD's selection settles) and
 * emergency-stop it, reporting KM_FAULT_DRIVER_FAULT -- the actual
 * ercf_2_0 whole-stack sim failure right after PRELOAD's selector reaches
 * gate 0 and the servo engages. */
static void test_rate_arrival_clears_exactly_on_target(void) {
    km_rate_t kr;
    km_rate_init(&kr);
    km_rate_set_limits(&kr, 16000, 96000, 96000, 200000);
    kr.target_phase_active = true;
    kr.target_phase = 320;
    kr.phase_whole = 320;
    kr.phase_frac = 0;
    kr.rate = 0;
    kr.accel = 0;
    kr.target_rate_internal = 0;

    km_rate_advance(&kr, 1000U);

    if (kr.target_phase_active) {
        printf("Failed: target_phase_active stuck true exactly at rest on target!\n");
        exit(1);
    }
    if (kr.phase_whole != 320 || kr.rate != 0 || kr.accel != 0) {
        printf("Failed: arrival snap moved off the already-reached target!\n");
        exit(1);
    }
}

int main(void) {
    printf("Starting native rate tests...\n");

    struct limit_case limits[] = {
        {18000, 10000, 15000, 200000},       // BoxTurtle
        {200000, 2000000, 2000000, 100000000}, // Extremes
        {18000, 10000, 15000, 100000}          // Small jmax
    };
    
    for (size_t i = 0; i < sizeof(limits)/sizeof(limits[0]); i++) {
        printf("Testing limits set %zu...\n", i);
        run_rate_test(&limits[i]);
        run_position_test(&limits[i]);
    }
    test_rate_arrival_clears_exactly_on_target();

    printf("All tests passed!\n");
    return 0;
}
