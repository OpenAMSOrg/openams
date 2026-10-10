// SPDX-License-Identifier: GPL-3.0-or-later
#include <stdio.h>
#include <stdlib.h>
#include <math.h>
#include <assert.h>
#include "klipper_mmu/rate.h"
#include "rate_reference.h"

static double get_real_phase(km_rate_t *kr) {
    return (double)kr->phase_whole + (double)kr->phase_frac / 4294967296.0;
}
static double get_real_v(km_rate_t *kr) {
    return (double)kr->rate * 1e6 / 4294967296.0;
}
static double get_real_a(km_rate_t *kr) {
    return (double)kr->accel * 1e12 / 281474976710656.0;
}

static double closed_form_stop_distance(double v, double a_val, struct ref_limits *lims) {
    if (v < 0) return 0.0;
    double D = lims->dmax;
    double J = lims->jmax;
    double a_mag = a_val < 0 ? -a_val : a_val;
    double a = a_val < 0 ? fmin(a_mag, D) : a_mag;
    
    if (a_val < 0 && v < a * a / (2 * J)) {
        return v * a / J;
    }
    
    double vp = v + a * a / (2 * J);
    double SV;
    if (vp >= D * D / J) {
        SV = vp * vp / (2 * D) + vp * D / (2 * J);
    } else {
        SV = vp * sqrt(vp / J);
    }
    
    double term_va = v * a / J;
    double a3_term = (a * a * a) / (3 * J * J);
    
    double S = SV;
    if (a_val >= 0) {
        S += term_va + a3_term;
    } else {
        S -= (term_va + a3_term);
        if (S < 0) S = 0.0;
    }
    return S;
}

int main(void) {
    printf("Running Rate Motion Core Crosscheck Test...\n");
    
    struct ref_limits lims = {18000.0, 10000.0, 15000.0, 200000.0};
    km_rate_t kr;
    km_rate_init(&kr);
    km_rate_set_limits(&kr, 18000, 10000, 15000, 200000);
    
    km_rate_set_target_rate(&kr, 18000);
    
    struct ref_state ref = {0.0, 0.0, 0.0, 0.0};
    
    double dt = 0.001; // 1ms
    
    // Test 1: Advance both and compare state
    for (int i = 0; i < 5000; i++) {
        km_rate_advance(&kr, 1000U);
        ref_step(&ref, &lims, 18000.0, dt);
        
        double p_err = fabs(get_real_phase(&kr) - ref.pos);
        double v_err = fabs(get_real_v(&kr) - ref.rate);
        double a_err = fabs(get_real_a(&kr) - ref.accel);
        
        if (p_err > 1.0 || v_err > 1e-4 * lims.vmax || a_err > 1e-4 * fmax(lims.amax, lims.dmax)) {
            printf("State mismatch at i=%d: \n", i);
            printf("  p=%.9f/%.9f err=%.9f\n", get_real_phase(&kr), ref.pos, p_err);
            printf("  v=%.6f/%.6f err=%.6f\n", get_real_v(&kr), ref.rate, v_err);
            printf("  a=%.6f/%.6f err=%.6f\n", get_real_a(&kr), ref.accel, a_err);
            return 1;
        }
    }
    
    // Test 2: Compare stopping distance on grid
    int32_t vs[] = {0, 1, 100, 1000, 1125, 5000, 18000};
    int32_t as[] = {-15000, -7500, -1000, 0, 1000, 5000, 10000};
    for (int i=0; i<7; i++) {
        for (int j=0; j<7; j++) {
            double v_val = vs[i];
            double a_val = as[j];
            double J = lims.jmax;
            if (a_val < 0 && v_val < (a_val * a_val) / (2 * J)) continue;
            if (v_val == 0) continue;
            
            km_rate_t krt;
            km_rate_init(&krt);
            km_rate_set_limits(&krt, 18000, 10000, 15000, 200000);
            km_rate_set_target_rate(&krt, 18000);
            
            // Core's own conversions
            if (v_val >= 0) krt.rate = (int64_t)(v_val * 67108864.0 + 7812.0) / 15625;
            else krt.rate = (int64_t)(v_val * 67108864.0 - 7812.0) / 15625;
            
            int32_t abs_a = (int32_t)(a_val < 0 ? -a_val : a_val);
            int64_t a_q48 = (int64_t)((abs_a * 68719476736.0 + 122070312.0) / 244140625.0);
            krt.accel = a_val < 0 ? -a_q48 : a_q48;
            
            double ref_stop = ref_stop_distance(v_val, a_val, &lims);
            int64_t core_stop_q32 = km_rate_stop_distance_q32(&krt);
            double int_stop = (double)core_stop_q32 / 4294967296.0;
            double closed_stop = closed_form_stop_distance(v_val, a_val, &lims);
            
            double tol_core = fmax(0.01, 1e-6 * closed_stop);
            if (fabs(int_stop - closed_stop) > tol_core) {
                printf("Stop distance core mismatch: v=%f a=%f core=%.6f closed=%.6f\n", v_val, a_val, int_stop, closed_stop);
                return 1;
            }
            
            double D = lims.dmax;
            double a_mag = a_val < 0 ? -a_val : a_val;
            double m = a_val < 0 ? fmin(a_mag, D) : a_mag;
            double vp = v_val + (m * m) / (2 * J);
            double tol_ref = vp * 1e-4 + 0.01;
            
            if (fabs(ref_stop - closed_stop) > tol_ref) {
                printf("Stop distance ref mismatch: v=%f a=%f ref=%.6f closed=%.6f tol=%.6f\n", v_val, a_val, ref_stop, closed_stop, tol_ref);
                return 1;
            }
            
            int64_t dist_whole = core_stop_q32 >> 32;
            if ((core_stop_q32 & 0xFFFFFFFFLL) != 0) dist_whole++;
            
            if (dist_whole > 0) {
                if (km_rate_set_target_phase(&krt, dist_whole - 1) != -1) {
                    printf("Failed to refuse target inside stopping distance!\n");
                    return 1;
                }
            }
            
            if (km_rate_set_target_phase(&krt, dist_whole + 1) != 0) {
                printf("Failed to accept target outside stopping distance!\n");
                return 1;
            }
        }
    }
    
    // Test 3: Edge solver against reference trajectory
    km_rate_init(&kr);
    km_rate_set_limits(&kr, 18000, 10000, 15000, 200000);
    km_rate_set_target_rate(&kr, 18000);
    
    double t = 0;
    int first_edge = 1;
    
    while (kr.rate != 0 || kr.target_rate_internal != 0) {
        int32_t dt_us = km_rate_next_edge_us(&kr);
        if (dt_us < 0) {
            km_rate_advance(&kr, 1000U);
            t += 0.001;
            if (get_real_v(&kr) >= 18000.0 - 0.001) {
                km_rate_set_target_rate(&kr, 0);
            }
            continue;
        }
        
        int64_t old_whole = kr.phase_whole;
        
        // Setup ref from identical state
        struct ref_state rs;
        rs.t = t;
        rs.pos = get_real_phase(&kr);
        rs.rate = get_real_v(&kr);
        rs.accel = get_real_a(&kr);
        
        double target_v = kr.target_rate_internal == 0 ? 0.0 : 18000.0;
        
        // Skip check if lookahead is a tie
        double J = lims.jmax;
        double a2_2J = (rs.accel * rs.accel) / (2 * J);
        double tie_val = fabs(a2_2J - fabs(target_v - rs.rate));
        
        ref_step(&rs, &lims, target_v, dt_us * 1e-6);
        
        km_rate_advance(&kr, (uint32_t)dt_us);
        t += dt_us * 1e-6;
        
        if (tie_val > 1e-3) {
            double err = fabs(get_real_phase(&kr) - rs.pos);
            if (err > 1e-6) {
                printf("Edge traj mismatch: dt=%d pos=%.9f ref=%.9f err=%.9f\n", dt_us, get_real_phase(&kr), rs.pos, err);
                return 1;
            }
        }
        
        if (kr.phase_whole != old_whole) {
            if (first_edge) {
                first_edge = 0;
                double exact = pow(6.0 / lims.jmax, 1.0/3.0);
                if (fabs(t - exact) > 1e-6) {
                    printf("First edge mismatch: t=%.6f exact=%.6f\n", t, exact);
                    return 1;
                }
            }
            
            // landing error
            int64_t dist_to_boundary = kr.phase_frac;
            if (dist_to_boundary > 2147483648LL) {
                dist_to_boundary = 4294967296LL - dist_to_boundary;
            }
            int64_t abs_rate = kr.rate > 0 ? kr.rate : -kr.rate;
            int64_t max_err = abs_rate + 1;
            if (dist_to_boundary > max_err) {
                printf("Landing error exceeded! err=%lld max=%lld\n", (long long)dist_to_boundary, (long long)max_err);
                return 1;
            }
            kr.phase_frac = 0;
        }
        
        if (get_real_v(&kr) >= 18000.0 - 0.001) {
            km_rate_set_target_rate(&kr, 0);
        }
    }
    
    printf("Crosscheck passed!\n");
    return 0;
}
