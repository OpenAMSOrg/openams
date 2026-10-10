// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KLIPPER_MMU_STEP_SEGMENTS_H
#define KLIPPER_MMU_STEP_SEGMENTS_H

#include <stdint.h>
#include "klipper_mmu/rate.h"

/* One rate-core tick as step segments for Klipper's stepper.c.
 *
 * Path: inside a tick of T us the phase follows the cubic through the start
 * phase, rate and acceleration that ends exactly at the end phase:
 *   p(t) = p0 + v0 t + a0 t^2 / 2 + c t^3,  c chosen so that p(T) = p1.
 *
 * Steps (exact reach, as in km_trajectory): the driver microstep M is state.
 * A positive step is due when the path reaches M + 1, a negative step when it
 * reaches M - 1; M then moves by one. A reversal inside a microstep emits
 * nothing. Counts are exact; each step is timed at the first instant the path
 * reaches its microstep, within KM_STEP_SEGMENTS_MAX_ERROR_US.
 *
 * Minimum spacing: no emitted interval is below min_interval_ticks. A step
 * the path brings inside it (at the rate limit the tick's cubic can, by a
 * tick or so) is delayed to it, by at most half the error budget; a step so
 * delayed past the tick end is left for the next tick (M stays one behind,
 * and the next tick finds its crossing already reached). A path that needs
 * more delay than that is too fast: KM_STEP_SEGMENTS_E_MIN_INTERVAL.
 *
 * Segments use stepper.c semantics: the first step is interval after the
 * anchor; before each later step interval += add, then next_step_time +=
 * interval (steps at I, I + (I + A), I + (I + A) + (I + 2A), ...). dir is 1 for increasing phase, 0 for decreasing, and
 * never changes inside a segment.
 *
 * Timing anchor: *anchor_ticks is the time in timer ticks from the last step
 * (the anchor) to the start of this tick, carried across ticks without steps
 * and saturating at 0x7fffffff (long enough ago that no spacing rule
 * applies). restart false: the stepper is still busy and the first interval
 * is measured from the anchor. restart true: the stepper went idle and will
 * take the tick start as its start clock, so the first interval is measured
 * from the tick start; the minimum interval still applies to the real
 * spacing, anchor + first interval (at low speed a tick without a step lets
 * the stepper drain, and the next step may fall just after a tick start).
 * On return it is the time from the new anchor (the last step of this tick,
 * or the old one when there was none) to the end of the tick.
 *
 * Integer only: no floating point and no 64-bit division (Cortex-M0+).
 */

/* Seven window-pieces per tick at most (four windows, split by up to two
 * zeros of v). One of 1 or 2 steps becomes that many segments; a longer one
 * is fitted with one constant int16 add and, when the fit misses, bisected
 * and refitted at most three levels deep: at most 8 segments per piece, 56
 * per tick, so a legitimate tick never runs out of room. The reference suite
 * measures at most 18. */
#define KM_STEP_SEGMENTS_MAX 56
#define KM_STEP_SEGMENTS_WINDOW_US 250
#define KM_STEP_SEGMENTS_MAX_ERROR_US 5    /* step time vs the path */
/* A turn (v = 0 inside the tick) must pass a microstep by this much, in Q32
 * microsteps (2^13 / 2^32, about 2e-6 microstep), to reach it: below the
 * phase evaluation's own rounding, so the step decision does not depend on
 * it. */
#define KM_STEP_SEGMENTS_TURN_GUARD (1 << 13)

enum {
    KM_STEP_SEGMENTS_OK = 0,
    KM_STEP_SEGMENTS_E_MIN_INTERVAL = -1,  /* the path is faster than the
                                              minimum interval allows */
    KM_STEP_SEGMENTS_E_RANGE = -2,         /* interval > UINT32_MAX, or more
                                              than max_segments segments */
};

typedef struct {
    uint32_t interval;
    uint16_t count;
    int16_t add;
    uint8_t dir;
} km_step_segment_t;

/* On an error nothing is emitted and *driver_microstep and *anchor_ticks are
 * unchanged; the caller treats it as a motion fault. */
int km_step_segments_generate(const km_rate_t *start, const km_rate_t *end,
                              uint32_t tick_us, uint32_t ticks_per_us,
                              uint32_t min_interval_ticks, bool restart,
                              int64_t *driver_microstep, uint32_t *anchor_ticks,
                              km_step_segment_t *segments, uint8_t *count,
                              uint8_t max_segments);

#endif
