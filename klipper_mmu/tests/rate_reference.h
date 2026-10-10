// SPDX-License-Identifier: GPL-3.0-or-later
/* Independent double-precision reference of the klipper-mmu rate state
 * (docs/motion-core.md), used only by native tests. rate_reference.c was
 * written from these declarations by a local model, one function
 * at a time, best of 3 against a self-test of the rules below. */
#ifndef KM_RATE_REFERENCE_H
#define KM_RATE_REFERENCE_H

struct ref_limits { double vmax, amax, dmax, jmax; };   /* steps/s, /s^2, /s^2, /s^3 */
struct ref_state { double t, pos, rate, accel; };        /* s, steps, steps/s, steps/s^2 */

/* Advance by dt seconds toward target_rate (clamped to +-vmax): move accel toward the
 * desired acceleration by at most jmax*dt, then integrate pos and rate exactly for the
 * constant-jerk interval. Desired acceleration is +-amax toward target_rate when |rate|
 * grows, +-dmax when |rate| falls, and 0 once the remaining rate change is covered by
 * accel^2/(2*jmax) (so the rate arrives with zero acceleration). */
void ref_step(struct ref_state *s, const struct ref_limits *l, double target_rate, double dt);

/* Distance travelled until rest if braking starts now with jerk-limited deceleration:
 * accel moves to -dmax at jmax, holds, then returns to 0 at jmax; if the rate runs out
 * first, the peak deceleration is lower (triangular). rate >= 0, accel any sign. */
double ref_stop_distance(double rate, double accel, const struct ref_limits *l);

#endif
