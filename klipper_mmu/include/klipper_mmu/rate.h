// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KLIPPER_MMU_RATE_H
#define KLIPPER_MMU_RATE_H

#include <stdint.h>
#include <stdbool.h>

// Fixed-point formats:
// time: dt in microseconds (us)
// phase: whole microsteps (int64_t) and fraction (uint32_t, where 2^32 is 1 microstep)
// rate: Q32 (steps/us * 2^32)
// accel: Q48 (steps/us^2 * 2^48)
// jerk: Q64 (steps/us^3 * 2^64)
//
// Limits fit within ranges without overflow for dt up to 1,000,000 us (1s):
// vmax: 200,000 steps/s -> 8.5e8 in Q32
// amax: 2,000,000 steps/s^2 -> 5.6e8 in Q48
// jmax: 100,000,000 steps/s^3 -> 1.84e9 in Q64
//
// All integrations use carefully scaled 64-bit integer arithmetic to prevent overflow.

typedef struct {
    uint32_t mant;
    int shift;
} km_reciprocal_t;

typedef struct {
    int64_t phase_whole;
    uint32_t phase_frac;
    
    int64_t rate; // Q32
    int64_t accel; // Q48
    
    int64_t target_rate_internal;
    int64_t target_phase;
    bool target_phase_active;
    bool braking;
    
    int64_t vmax_internal;
    int64_t amax_internal;
    int64_t dmax_internal;
    int64_t jmax_internal;
    
    int64_t inv_J_61;
    km_reciprocal_t inv_J;
    km_reciprocal_t inv_2J;
    km_reciprocal_t inv_2D;
    uint64_t D_sq_over_J;
} km_rate_t;

void km_rate_init(km_rate_t *r);
void km_rate_set_limits(km_rate_t *r, uint32_t vmax, uint32_t amax, uint32_t dmax, uint32_t jmax);
void km_rate_set_target_rate(km_rate_t *r, int32_t target_rate);
int km_rate_set_target_phase(km_rate_t *r, int64_t target_phase);
void km_rate_stop(km_rate_t *r, bool emergency);
void km_rate_advance(km_rate_t *r, uint32_t dt_us);
/* Updates are at most this far apart while moving: at low speed an edge
 * interval can outlast a whole jerk phase (the first microstep from rest
 * takes (6/jmax)^(1/3), 31 ms at 200000 steps/s^3). */
#define KM_RATE_MAX_UPDATE_US 1000
/* Microseconds to the next update: the next microstep boundary, or
 * KM_RATE_MAX_UPDATE_US if sooner. After km_rate_advance, a step is due only
 * if the phase crossed a boundary. -1 when stopped with nothing to do. */
int32_t km_rate_next_edge_us(km_rate_t *r);
int64_t km_rate_stop_distance_q32(const km_rate_t *r);

#endif
