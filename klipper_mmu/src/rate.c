// SPDX-License-Identifier: GPL-3.0-or-later
#include "klipper_mmu/rate.h"
#define ROUND_SHIFT_16(x) (((x) >= 0) ? (((x) + 32768LL) >> 16) : (((x) + 32767LL) >> 16))
#define ROUND_SHIFT_17(x) (((x) >= 0) ? (((x) + 65536LL) >> 17) : (((x) + 65535LL) >> 17))
#define ROUND_DIV_6(x) (((x) >= 0) ? (((x) + 3LL) / 6LL) : (((x) - 3LL) / 6LL))
#define ROUND_DIV_2(x) (((x) >= 0) ? (((x) + 1LL) / 2LL) : (((x) - 1LL) / 2LL))

static int64_t steps_per_s_to_q32(int32_t v) {
    if (v >= 0) return ((int64_t)v * 67108864LL + 7812LL) / 15625LL;
    else return ((int64_t)v * 67108864LL - 7812LL) / 15625LL;
}

static int64_t steps_per_s2_to_q48(uint32_t a) {
    return (int64_t)(((uint64_t)a * 68719476736ULL + 122070312ULL) / 244140625ULL);
}

static int64_t steps_per_s3_to_q64(uint32_t j) {
    uint64_t whole = (uint64_t)j * 18ULL;
    uint64_t frac = ((uint64_t)j * 4467440737ULL + 5000000000ULL) / 10000000000ULL;
    return (int64_t)(whole + frac);
}

static uint64_t isqrt64(uint64_t n) {
    uint64_t res = 0;
    uint64_t bit = 1ULL << 62; // The second-to-top bit is set
    
    while (bit > n) bit >>= 2;
    
    while (bit != 0) {
        if (n >= res + bit) {
            n -= res + bit;
            res = (res >> 1) + bit;
        } else {
            res >>= 1;
        }
        bit >>= 2;
    }
    return res;
}

// Iteration bounded to 22 steps (max result 2^22 - 1 ~ 4.19M),
// sufficient since dt is clamped to 1,000,000 us.
static uint32_t fast_div(uint64_t num, uint64_t den) {
    if (den == 0) return 1000000;
    uint32_t res = 0;
    for (int i = 21; i >= 0; i--) {
        if ((num >> i) >= den) {
            res |= (1U << i);
            num -= (den << i);
        }
    }
    return res;
}

/* Stopping distances saturate here (Q32 steps, about 1e9 steps): overestimating
 * only brakes earlier, while a wrapped value would never brake. */
#define KM_RATE_S_CAP ((uint64_t)1 << 62)

static km_reciprocal_t compute_recip(uint64_t val) {
    km_reciprocal_t r = {0, 0};
    if (val == 0) return r;
    int msb = 63 - __builtin_clzll(val);
    uint64_t norm = (msb >= 31) ? (val >> (msb - 31)) : (val << (31 - msb));
    r.mant = (uint32_t)((1ULL << 62) / norm); 
    r.shift = 31 + msb; 
    return r;
}

/* Derivation: Computes (val * mant) / 2^(shift - extra_left_shift).
   val is shifted right to at most 31 bits to ensure val * mant fits in 64 bits. */
static inline uint64_t mul_inv_shifted(uint64_t val, km_reciprocal_t recip, int extra_left_shift) {
    if (val == 0 || recip.mant == 0) return 0;
    int msb = 63 - __builtin_clzll(val);
    int v_shift = msb >= 31 ? msb - 30 : 0;
    uint64_t reduced = val >> v_shift;
    int final_shift = recip.shift - v_shift - extra_left_shift;
    if (final_shift < 0) {
        uint64_t p = reduced * recip.mant;
        return (p >> (62 + final_shift)) ? KM_RATE_S_CAP : p << (-final_shift);
    }
    return (reduced * recip.mant) >> final_shift;
}

static int64_t compute_stopping_distance(int64_t v_mag, int64_t a_mag, const km_rate_t *r) {
    if (v_mag < 0) return 0; // Not going towards target
    
    uint64_t v = (uint64_t)v_mag;
    uint64_t a = (uint64_t)((a_mag < 0) ? -a_mag : a_mag);
    uint64_t D = (uint64_t)r->dmax_internal;
    
    // a^2 / (2J) : a is Q48, 2J is Q64. (a * a) is Q96. Q96/Q64 = Q32.
    // mul_inv_shifted gives exact integer division.
    uint64_t a_sq_over_2J = 0;
    if (a_mag >= 0) {
        uint64_t a_sq = a * a;
        a_sq_over_2J = mul_inv_shifted(a_sq, r->inv_2J, 0);
    } else {
        uint64_t m = (a < D) ? a : D;
        uint64_t m_sq = m * m;
        uint64_t m_sq_over_2J = mul_inv_shifted(m_sq, r->inv_2J, 0);
        
        if (v < m_sq_over_2J) {
            // Upper bound S = v * m / J
            uint64_t v_m = v * m; // Q80
            // Q80 / Q64 = Q16. Need Q32, so << 16.
            uint64_t bound = mul_inv_shifted(v_m, r->inv_J, 16);
            return (int64_t)(bound < KM_RATE_S_CAP ? bound : KM_RATE_S_CAP);
        }
        a_sq_over_2J = m_sq_over_2J;
        a = m; // Use m for the rest of the a < 0 branch
    }
    
    uint64_t v_p = v + a_sq_over_2J;
    if (v_p >> 31) return (int64_t)KM_RATE_S_CAP; /* v_p^2 must fit 64 bits */
    
    uint64_t S_V = 0;
    if (v_p >= r->D_sq_over_J) {
        // v_p^2 / 2D
        uint64_t v_p_sq = v_p * v_p; // Q64
        // Q64 / Q48 = Q16. Need << 16.
        uint64_t term1 = mul_inv_shifted(v_p_sq, r->inv_2D, 16);
        
        // v_p * D / 2J
        uint64_t vp_D = v_p * D; // Q80
        // Q80 / Q64 = Q16. Need << 16.
        uint64_t term2 = mul_inv_shifted(vp_D, r->inv_2J, 16);
        
        S_V = term1 + term2;
    } else {
        // v_p * sqrt(v_p / J)
        // time_sq_us2 = v_p / J in us^2 (Q0)
        // v_p is Q32. J is Q64. v_p/J is Q(-32). Need << 32.
        uint64_t time_sq_us2 = mul_inv_shifted(v_p, r->inv_J, 32);
        uint64_t time_us = isqrt64(time_sq_us2); // us
        // v_p (Q32 steps/us) * time_us (us) = Q32 steps
        S_V = v_p * time_us;
    }
    
    // v * a / J
    uint64_t v_a = v * a; // Q80
    // Q80 / Q64 = Q16. Need << 16.
    uint64_t term_v_a = mul_inv_shifted(v_a, r->inv_J, 16);
    
    // a^3 / (3J^2)
    // t_us = a / J in us (Q0). a is Q48, J is Q64. Q(-16). Need << 16.
    uint64_t t_us = mul_inv_shifted(a, r->inv_J, 16);
    if (t_us >> 24) return (int64_t)KM_RATE_S_CAP; /* over 16 s to remove a */
    uint64_t t_sq = t_us * t_us; // us^2
    uint64_t a3_term = 0;
    int a_msb = a == 0 ? 0 : 63 - __builtin_clzll(a);
    int t_sq_msb = t_sq == 0 ? 0 : 63 - __builtin_clzll(t_sq);
    if (a_msb + t_sq_msb < 63) {
        a3_term = ((a * t_sq) / 3) >> 16;
    } else {
        a3_term = (a * (t_sq >> 16)) / 3;
    }
    
    /* Each term is at most KM_RATE_S_CAP, so the sums fit; rounding must not
     * wrap the a < 0 difference below zero. */
    uint64_t S = S_V + term_v_a + a3_term;
    if (a_mag < 0) S = S_V > term_v_a + a3_term ? S_V - term_v_a - a3_term : 0;
    return (int64_t)(S < KM_RATE_S_CAP ? S : KM_RATE_S_CAP);
}

/* The direction the state is moving in: the rate's sign, else the accel's. */
static int64_t motion_dir(const km_rate_t *r) {
    int64_t m = r->rate != 0 ? r->rate : r->accel;
    return m > 0 ? 1 : (m < 0 ? -1 : 0);
}

/* Signed Q32 distance from the phase to a whole-microstep target. */
static int64_t distance_to(const km_rate_t *r, int64_t target) {
    int64_t wl = target - r->phase_whole;
    if (wl > 300000000LL) wl = 300000000LL;
    if (wl < -300000000LL) wl = -300000000LL;
    return (wl * ((int64_t)1 << 32)) - (int64_t)r->phase_frac;
}

int64_t km_rate_stop_distance_q32(const km_rate_t *r) {
    int64_t dir = motion_dir(r);
    return dir ? compute_stopping_distance(r->rate * dir, r->accel * dir, r) : 0;
}

void km_rate_init(km_rate_t *r) {
    r->phase_whole = 0;
    r->phase_frac = 0;
    r->rate = 0;
    r->accel = 0;
    r->target_rate_internal = 0;
    r->target_phase_active = false;
    r->braking = false;
    km_rate_set_limits(r, 200000, 2000000, 2000000, 100000000);
}

void km_rate_set_limits(km_rate_t *r, uint32_t vmax, uint32_t amax, uint32_t dmax, uint32_t jmax) {
    r->vmax_internal = steps_per_s_to_q32((int32_t)vmax);
    r->amax_internal = steps_per_s2_to_q48(amax);
    r->dmax_internal = steps_per_s2_to_q48(dmax);
    r->jmax_internal = steps_per_s3_to_q64(jmax);
    
    r->inv_J = compute_recip((uint64_t)r->jmax_internal);
    r->inv_2J = compute_recip(2ULL * (uint64_t)r->jmax_internal);
    r->inv_2D = compute_recip(2ULL * (uint64_t)r->dmax_internal);
    
    uint64_t D_sq = (uint64_t)r->dmax_internal * (uint64_t)r->dmax_internal;
    r->D_sq_over_J = mul_inv_shifted(D_sq, r->inv_J, 0);
    
    r->inv_J_61 = (int64_t)((1ULL << 61) / (uint64_t)r->jmax_internal);
}

void km_rate_set_target_rate(km_rate_t *r, int32_t target_rate) {
    int64_t t = steps_per_s_to_q32(target_rate);
    if (t > r->vmax_internal) t = r->vmax_internal;
    if (t < -r->vmax_internal) t = -r->vmax_internal;
    r->target_rate_internal = t;
    r->braking = false;
}

int km_rate_set_target_phase(km_rate_t *r, int64_t target_phase) {
    /* Refused inside the stopping distance (or behind the motion). */
    int64_t dir = motion_dir(r);
    if (dir && distance_to(r, target_phase) * dir < km_rate_stop_distance_q32(r)) return -1;
    r->target_phase = target_phase;
    r->target_phase_active = true;
    r->braking = false;
    return 0;
}

void km_rate_stop(km_rate_t *r, bool emergency) {
    if (emergency) {
        r->rate = 0;
        r->accel = 0;
    }
    r->target_rate_internal = 0;
    r->target_phase_active = false;
    r->braking = false;
}

static int64_t get_policy_target_rate(const km_rate_t *r, int64_t dir, int64_t dist_mag) {
    if (r->braking) return 0;
    if (dist_mag <= ((int64_t)1 << 32)) return steps_per_s_to_q32(10) * dir;
    int64_t target = r->target_rate_internal;
    return (target < 0 ? -target : target) * dir;
}

static int64_t compute_delta_accel(int64_t v, int64_t a, int64_t target_v, int64_t dt, const km_rate_t *r) {
    int64_t dv = target_v - v;
    int64_t desired_a = dv > 0 ? (v >= 0 ? r->amax_internal : r->dmax_internal) : 
                       (dv < 0 ? (v <= 0 ? -r->amax_internal : -r->dmax_internal) : 0);
    if (dv != 0 && a != 0 && ((dv > 0 && a > 0) || (dv < 0 && a < 0))) {
        if (a * a >= 2 * r->jmax_internal * (dv < 0 ? -dv : dv)) desired_a = 0;
    }
    int64_t max_d = (r->jmax_internal * dt) >> 16;
    int64_t da = desired_a - a;
    return da > max_d ? max_d : (da < -max_d ? -max_d : da);
}

static void integrate_state(int64_t v, int64_t a, int64_t da, int64_t target_v, int64_t dt,
                            int64_t *nv, int64_t *na, int64_t *dp, const km_rate_t *r) {
    bool hold = (target_v == v);
    *dp = v * dt + (hold ? 0 : ROUND_SHIFT_17(a * dt) * dt + ROUND_SHIFT_16(ROUND_DIV_6(da * dt)) * dt);
    *na = a + da;
    int64_t v_new = v + ROUND_SHIFT_16(a * dt + ROUND_DIV_2(da * dt));
    int64_t dv = target_v - v;
    if (hold || (dv > 0 && v_new >= target_v) || (dv < 0 && v_new <= target_v)) v_new = target_v;
    *nv = v_new > r->vmax_internal ? r->vmax_internal : (v_new < -r->vmax_internal ? -r->vmax_internal : v_new);
}

static int64_t get_move_direction(const km_rate_t *r, int64_t *dist) {
    if (r->target_phase_active) {
        *dist = distance_to(r, r->target_phase);
        return *dist > 0 ? 1 : (*dist < 0 ? -1 : 0);
    }
    *dist = 0;
    if (motion_dir(r)) return motion_dir(r);
    return r->target_rate_internal > 0 ? 1 : (r->target_rate_internal < 0 ? -1 : 0);
}

void km_rate_advance(km_rate_t *r, uint32_t dt_us) {
    int64_t dt = (int64_t)dt_us;
    if (dt == 0) return;
    
    int64_t v = r->rate, a = r->accel, dist = 0;
    int64_t dir = get_move_direction(r, &dist);
    int64_t target_v = r->target_rate_internal;
    
    if (r->target_phase_active && dir != 0) {
        int64_t dist_mag = dist * dir;
        if (r->braking && v == 0 && a == 0) r->braking = false;
        target_v = get_policy_target_rate(r, dir, dist_mag);
        
        if (!r->braking && dist_mag > ((int64_t)1 << 32)) {
            int64_t test_da = compute_delta_accel(v, a, target_v, dt, r), nv, na, dp;
            integrate_state(v, a, test_da, target_v, dt, &nv, &na, &dp, r);
            if (dist_mag - dp * dir <= compute_stopping_distance(nv * dir, na * dir, r)) {
                r->braking = true;
                target_v = 0;
            }
        }
    }
    
    int64_t nv, na, dp;
    integrate_state(v, a, compute_delta_accel(v, a, target_v, dt, r), target_v, dt, &nv, &na, &dp, r);
    
    if (r->target_phase_active && dir != 0) {
        if ((nv > 0 && dir < 0) || (nv < 0 && dir > 0)) nv = 0;
        if ((dp > 0 && dir < 0) || (dp < 0 && dir > 0)) dp = 0;
    }
    
    int64_t delta_whole = dp >> 32;
    uint32_t old_frac = r->phase_frac;
    r->phase_frac += (uint32_t)((uint64_t)dp & 0xFFFFFFFFULL);
    if (r->phase_frac < old_frac) delta_whole += 1;
    r->phase_whole += delta_whole;
    r->rate = nv;
    r->accel = na;
    
    /* dir (get_move_direction, above) is the sign of the distance to the
     * target phase at entry to this call, and is only ever computed while
     * target_phase_active. It reads 0 exactly when that distance was
     * already 0 -- i.e., already sitting exactly on the target -- not just
     * "not moving". Requiring dir != 0 here to declare arrival is
     * self-defeating in exactly that case: a short, precise move (a
     * selector homing to its gate, say) that lands its last microstep
     * exactly on the target phase the same tick its ramp reaches rest
     * never sees dir != 0 again, so target_phase_active (and the
     * km_trajectory_is_moving()/is_active it drives) latches true forever.
     * The next driver bind then reads the trajectory as still moving and
     * emergency-stops it, reporting a misleading driver fault. Arrival
     * with zero remaining distance needs no direction to confirm. */
    if (r->target_phase_active
        && (dir == 0 || distance_to(r, r->target_phase) * dir <= 0)) {
        r->phase_whole = r->target_phase;
        r->phase_frac = 0;
        r->rate = 0;
        r->accel = 0;
        r->target_phase_active = false;
        r->target_rate_internal = 0; /* at rest: the speed cap is not a new target */
        r->braking = false;
    }
}

int32_t km_rate_next_edge_us(km_rate_t *r) {
    if (r->rate == 0 && r->accel == 0 && r->target_rate_internal == 0 && !r->target_phase_active) return -1;
    
    int64_t v = r->rate, a = r->accel, dist = 0;
    int64_t dir = get_move_direction(r, &dist);
    if (dir == 0) return -1;
    
    int64_t target_v = r->target_phase_active ? get_policy_target_rate(r, dir, dist * dir) : r->target_rate_internal;
    int64_t dist_Q32 = dir > 0 ? 4294967296LL - (int64_t)r->phase_frac : 
                       ((int64_t)r->phase_frac == 0 ? 4294967296LL : (int64_t)r->phase_frac);
    
    if (v == 0 && a == 0) {
        uint64_t t3 = ( ((uint64_t)dist_Q32 * 6ULL) * (uint64_t)(r->inv_J_61 >> 11) ) >> 18;
        uint64_t low = 0, high = 2000000;
        while (low < high) {
            uint64_t mid = low + (high - low) / 2;
            if (mid * mid * mid < t3) low = mid + 1;
            else high = mid;
        }
        return low < KM_RATE_MAX_UPDATE_US ? (int32_t)low : KM_RATE_MAX_UPDATE_US;
    }
    
    int64_t v_mag = v * dir;
    if (v_mag <= 0) v_mag = 1;
    
    int64_t dt = (int64_t)fast_div((uint64_t)dist_Q32, (uint64_t)v_mag);
    if (dt > KM_RATE_MAX_UPDATE_US) dt = KM_RATE_MAX_UPDATE_US; /* Newton starts within one update */
    if (dt == 0) dt = 1;
    
    /* Newton on the travel to the boundary; at low speed with a large
     * acceleration one step is not within 1 us, so repeat (bounded). */
    for (int i = 0; i < 3; i++) {
        int64_t nv, na, dp;
        integrate_state(v, a, compute_delta_accel(v, a, target_v, dt, r), target_v, dt, &nv, &na, &dp, r);
        int64_t f_t = dp * dir - dist_Q32;
        int64_t f_prime = nv * dir;
        if (f_prime <= 0 || (f_t < 0 && dt == KM_RATE_MAX_UPDATE_US)) break;
        int64_t step = (int64_t)fast_div((uint64_t)(f_t < 0 ? -f_t : f_t), (uint64_t)f_prime);
        if (step == 0) break;
        dt -= f_t > 0 ? step : -step;
        if (dt < 1) dt = 1;
        if (dt > KM_RATE_MAX_UPDATE_US) dt = KM_RATE_MAX_UPDATE_US;
    }
    return (int32_t)dt;
}
