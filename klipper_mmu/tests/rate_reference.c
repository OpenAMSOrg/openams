// SPDX-License-Identifier: GPL-3.0-or-later
#include "rate_reference.h"
#include <math.h>
#include <stdlib.h>

static inline double step_clamp(double v, double min, double max) {
    return v < min ? min : (v > max ? max : v);
}

static inline int step_sign(double v) {
    return v > 0 ? 1 : (v < 0 ? -1 : 0);
}

static void step_apply_jerk(struct ref_state *s, double j, double dt) {
    s->pos += s->rate * dt + s->accel * dt * dt / 2.0 + j * dt * dt * dt / 6.0;
    s->rate += s->accel * dt + j * dt * dt / 2.0;
    s->accel += j * dt;
}

void ref_step(struct ref_state *s, const struct ref_limits *l, double target_rate, double dt) {
    double vmax = l->vmax, amax = l->amax, dmax = l->dmax, jmax = l->jmax;
    double target = step_clamp(target_rate, -vmax, vmax);
    double dv = target - s->rate;

    if (dv == 0.0) {
        double j = step_clamp(-s->accel / dt, -jmax, jmax);
        s->accel += j * dt;
        s->pos += s->rate * dt;
        s->t += dt;
        return;
    }

    int dir = dv > 0 ? 1 : (dv < 0 ? -1 : -step_sign(s->accel));
    int growing = (s->rate == 0.0) || (s->rate > 0.0 && dir > 0) || (s->rate < 0.0 && dir < 0);
    double limit = growing ? amax : dmax;
    double desired = dir * limit;

    if (s->accel * dir > 0.0 && (s->accel * s->accel) / (2.0 * jmax) >= fabs(dv)) {
        desired = 0.0;
    }

    double j = (desired - s->accel) / dt;
    j = step_clamp(j, -jmax, jmax);

    double old_rate = s->rate;
    step_apply_jerk(s, j, dt);

    if ((target - old_rate) * (target - s->rate) < 0.0) {
        s->rate = target;
    }
    s->t += dt;
}
#include "rate_reference.h"
#include <math.h>
#include <stdlib.h>

static double stop_calc_dist(struct ref_state *s, const struct ref_limits *l) {
    double dt = 1e-4;
    int i = 0;
    while (i < 50000000 && (s->rate > 0.0 || s->accel > 0.0)) {
        ref_step(s, l, 0.0, dt);
        i++;
    }
    return s->pos;
}

double ref_stop_distance(double rate, double accel, const struct ref_limits *l) {
    struct ref_state state = {0.0, 0.0, rate, accel};
    return stop_calc_dist(&state, l);
}