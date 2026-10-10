// SPDX-License-Identifier: GPL-3.0-or-later
/* Independent reference tests for km_step_segments_generate().
 *
 * Everything below is derived only from the contract in
 * include/klipper_mmu/step_segments.h plus the fixed-point formats documented
 * in include/klipper_mmu/rate.h.  src/step_segments.c is deliberately never
 * consulted: the reference evaluates the tick's cubic path in double
 * precision, splits it at every zero of v(t), applies the exact-reach rule per
 * monotone piece with the driver microstep as state, expands the emitted
 * segments with stepper.c's semantics (time += interval; interval += add) and
 * compares step by step.
 */

#include <math.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "klipper_mmu/step_segments.h"

#define Q32_SCALE 4294967296.0
#define Q48_SCALE 281474976710656.0
#define KM_REF_PI 3.14159265358979323846

#define REF_MAX_STEPS 8192
#define FAIL_PRINT_MAX 2000

static int g_failures;
static int g_printed;
static double g_max_err_us;
static int g_max_segments;
static int g_mnotes;
static int g_deferrals;

static void failf(const char *fmt, ...)
{
    g_failures++;
    if (g_printed < FAIL_PRINT_MAX) {
        va_list ap;
        va_start(ap, fmt);
        fputs("FAIL: ", stdout);
        vprintf(fmt, ap);
        fputc('\n', stdout);
        va_end(ap);
        g_printed++;
    }
}

/* ------------------------------------------------------------------ */
/* Fixed-point state interpretation (rate.h formats)                   */
/* ------------------------------------------------------------------ */

static double phase_of(const km_rate_t *s)
{
    return (double)s->phase_whole + (double)s->phase_frac / Q32_SCALE;
}

static double rate_of(const km_rate_t *s)
{
    return (double)s->rate / Q32_SCALE; /* steps/us */
}

static double accel_of(const km_rate_t *s)
{
    return (double)s->accel / Q48_SCALE; /* steps/us^2 */
}

static void build_state(km_rate_t *s, double phase, double rate, double accel)
{
    memset(s, 0, sizeof(*s));
    double fl = floor(phase);
    int64_t whole = (int64_t)fl;
    int64_t frac = llround((phase - fl) * Q32_SCALE);
    if (frac >= (int64_t)Q32_SCALE) {
        whole += 1;
        frac -= (int64_t)Q32_SCALE;
    }
    if (frac < 0) {
        frac = 0;
    }
    s->phase_whole = whole;
    s->phase_frac = (uint32_t)frac;
    s->rate = (int64_t)llround(rate * Q32_SCALE);
    s->accel = (int64_t)llround(accel * Q48_SCALE);
}

/* ------------------------------------------------------------------ */
/* Double-precision reference                                          */
/* ------------------------------------------------------------------ */

typedef struct {
    double t_us;
    int dir;
} ref_step_t;

typedef struct {
    double p0, v0, a0, c, T;
} cubic_t;

static double cubic_p(const cubic_t *q, double t)
{
    return q->p0 + q->v0 * t + 0.5 * q->a0 * t * t + q->c * t * t * t;
}

static double cubic_v(const cubic_t *q, double t)
{
    return q->v0 + q->a0 * t + 3.0 * q->c * t * t;
}

/* First t in [ta,tb] where the monotone path reaches tgt, to well under the
 * required 1e-6 us.  Safeguarded Newton with a bisection fallback. */
static double solve_cross(const cubic_t *q, double tgt, double ta, double tb, int up)
{
    double lo = ta;
    double hi = tb;
    double t = 0.5 * (ta + tb);
    for (int i = 0; i < 60; i++) {
        double f = cubic_p(q, t) - tgt;
        if (up ? (f >= 0.0) : (f <= 0.0)) {
            hi = t;
        } else {
            lo = t;
        }
        if (hi - lo < 1e-9 || fabs(f) < 1e-11) {
            break;
        }
        double df = cubic_v(q, t);
        double tn;
        if (fabs(df) > 1e-30) {
            tn = t - f / df;
        } else {
            tn = 0.5 * (lo + hi);
        }
        if (!(tn > lo && tn < hi)) {
            tn = 0.5 * (lo + hi);
        }
        t = tn;
    }
    return t;
}

static void ref_tick(const km_rate_t *start, const km_rate_t *end, double T,
                     int64_t *M, ref_step_t *out, int *n_out)
{
    int n = 0;
    cubic_t q;
    /* Relative to the tick's starting microstep: a double cannot hold an
     * absolute phase near 1e12 to the sub-step precision timing needs. */
    int64_t base = start->phase_whole;
    q.p0 = (double)start->phase_frac / Q32_SCALE;
    q.v0 = rate_of(start);
    q.a0 = accel_of(start);
    q.T = T;
    double p1 = (double)(end->phase_whole - base) + (double)end->phase_frac / Q32_SCALE;
    q.c = (p1 - q.p0 - q.v0 * T - 0.5 * q.a0 * T * T) / (T * T * T);

    /* Zeros of v(t) = 3c t^2 + a0 t + v0 inside (0, T). */
    double A = 3.0 * q.c;
    double B = q.a0;
    double C = q.v0;
    double roots[2];
    int nr = 0;
    if (fabs(A) < 1e-18) {
        if (fabs(B) > 1e-18) {
            double r = -C / B;
            if (r > 0.0 && r < T) {
                roots[nr++] = r;
            }
        }
    } else {
        double disc = B * B - 4.0 * A * C;
        if (disc >= 0.0) {
            double sq = sqrt(disc);
            double r1 = (-B - sq) / (2.0 * A);
            double r2 = (-B + sq) / (2.0 * A);
            if (r1 > 0.0 && r1 < T) {
                roots[nr++] = r1;
            }
            if (r2 > 0.0 && r2 < T && fabs(r2 - r1) > 1e-9) {
                roots[nr++] = r2;
            }
            if (nr == 2 && roots[0] > roots[1]) {
                double tmp = roots[0];
                roots[0] = roots[1];
                roots[1] = tmp;
            }
        }
    }

    double bp[4];
    int nb = 0;
    bp[nb++] = 0.0;
    for (int i = 0; i < nr; i++) {
        bp[nb++] = roots[i];
    }
    bp[nb++] = T;

    for (int i = 0; i + 1 < nb; i++) {
        double ta = bp[i];
        double tb = bp[i + 1];
        if (tb - ta < 1e-12) {
            continue;
        }
        double pa = cubic_p(&q, ta);
        double pb = cubic_p(&q, tb);
        int up;
        if (pb > pa) {
            up = 1;
        } else if (pb < pa) {
            up = 0;
        } else {
            continue;
        }
        if (up) {
            for (;;) {
                double tgt = (double)(*M - base + 1);
                if (tgt > pb + 1e-9) {
                    break;
                }
                double t = solve_cross(&q, tgt, ta, tb, 1);
                if (n >= REF_MAX_STEPS) {
                    break;
                }
                out[n].t_us = t;
                out[n].dir = 1;
                n++;
                (*M)++;
            }
        } else {
            for (;;) {
                double tgt = (double)(*M - base - 1);
                if (tgt < pb - 1e-9) {
                    break;
                }
                double t = solve_cross(&q, tgt, ta, tb, 0);
                if (n >= REF_MAX_STEPS) {
                    break;
                }
                out[n].t_us = t;
                out[n].dir = -1;
                n++;
                (*M)--;
            }
        }
    }
    *n_out = n;
}

/* ------------------------------------------------------------------ */
/* Per-tick check                                                      */
/* ------------------------------------------------------------------ */

typedef struct {
    const char *tag;
    uint32_t tick_us;
    uint32_t tpu;
    uint32_t min_interval;
    int64_t tick_len_ticks;
    int64_t M_ref;
    int64_t M_impl;
    int64_t M0;
    int restart;
    int64_t sum_impl;
    uint32_t A_in;
    int64_t tick_index;
    int64_t total_ref;
    int64_t total_impl;
    double last_err_us;
    int m_printed;
    int pending;            /* a step was deferred to this tick by the last */
    int pending_dir;
    double pending_ref_us;  /* its reference crossing, absolute us */
    int max_seg_tick;
} ctx_t;

static void ctx_init(ctx_t *c, const char *tag, uint32_t tick_us, uint32_t tpu,
                     uint32_t min_interval, int64_t M0)
{
    memset(c, 0, sizeof(*c));
    c->tag = tag;
    c->tick_us = tick_us;
    c->tpu = tpu;
    c->min_interval = min_interval;
    c->tick_len_ticks = (int64_t)tick_us * (int64_t)tpu;
    c->M_ref = M0;
    c->M_impl = M0;
    c->M0 = M0;
}

static void run_tick(ctx_t *c, const km_rate_t *start, const km_rate_t *end)
{
    static ref_step_t ref[REF_MAX_STEPS];
    int nref = 0;
    int64_t Mref = c->M_ref;
    ref_tick(start, end, (double)c->tick_us, &Mref, ref, &nref);

    int64_t tick_start = c->tick_index * c->tick_len_ticks;
    int64_t M_before = c->M_impl;
    uint32_t A_before = c->A_in;
    km_step_segment_t segs[KM_STEP_SEGMENTS_MAX];
    uint8_t cnt = 0;
    int rc = km_step_segments_generate(start, end, c->tick_us, c->tpu,
                                       c->min_interval, c->restart ? true : false,
                                       &c->M_impl, &c->A_in,
                                       segs, &cnt, KM_STEP_SEGMENTS_MAX);
    if (rc != KM_STEP_SEGMENTS_OK) {
        failf("tick %lld [%s]: unexpected rc=%d", (long long)c->tick_index, c->tag, rc);
        c->M_impl = M_before;
        c->A_in = A_before;
        c->M_ref = Mref;
        c->tick_index++;
        return;
    }

    int seg_used = (int)cnt;
    if (seg_used > KM_STEP_SEGMENTS_MAX) {
        failf("tick %lld: count %u > max %d", (long long)c->tick_index,
              (unsigned)cnt, KM_STEP_SEGMENTS_MAX);
        seg_used = KM_STEP_SEGMENTS_MAX;
    }
    if (seg_used > g_max_segments) {
        g_max_segments = seg_used;
    }
    if (seg_used > c->max_seg_tick) {
        c->max_seg_tick = seg_used;
    }

    double bound = (double)KM_STEP_SEGMENTS_MAX_ERROR_US + 1.0 / (double)c->tpu + 1e-6;
    /* Without a restart the first interval is measured from the anchor, which
     * sits A_before ticks before the start of this tick.  With a restart the
     * stepper takes the tick start as its start clock, so the first interval
     * is measured from there and only the spacing check carries the anchor. */
    int64_t t_rel = c->restart ? 0 : -(int64_t)A_before;
    int idx = 0;              /* this tick's reference steps emitted */
    int emitted = 0;          /* steps emitted in this tick */
    int pend = c->pending;    /* a step the previous tick left for this one */
    int pend_seen = 0;
    for (int s = 0; s < seg_used; s++) {
        if (segs[s].dir > 1) {
            failf("tick %lld seg %d: bad dir %u", (long long)c->tick_index, s,
                  (unsigned)segs[s].dir);
        }
        int64_t iv = (int64_t)segs[s].interval;
        for (int k = 0; k < (int)segs[s].count; k++) {
            t_rel += iv;
            /* On a restart the first step's real spacing also includes the
             * time carried from the previous step. */
            int64_t space = iv;
            if (c->restart && emitted == 0) {
                space += (int64_t)A_before;
            }
            if (space < (int64_t)c->min_interval) {
                failf("tick %lld: interval %lld < min %u",
                      (long long)c->tick_index, (long long)space, c->min_interval);
            }
            if (emitted == 0 && pend) {
                /* The step the previous tick deferred: its crossing was at
                 * the tick end, so its time is measured across the boundary. */
                if ((int)segs[s].dir != (c->pending_dir > 0 ? 1 : 0)) {
                    failf("tick %lld: deferred step dir %u ref %d",
                          (long long)c->tick_index, (unsigned)segs[s].dir,
                          c->pending_dir);
                }
                double em_abs = (double)(tick_start + t_rel) / (double)c->tpu;
                double err = fabs(em_abs - c->pending_ref_us);
                if (err > g_max_err_us) {
                    g_max_err_us = err;
                }
                c->last_err_us = err;
                if (err > (double)KM_STEP_SEGMENTS_MAX_ERROR_US + 1e-6) {
                    failf("tick %lld: deferred step err %.4f us > %d (ref %.6f em %.6f)",
                          (long long)c->tick_index, err, KM_STEP_SEGMENTS_MAX_ERROR_US,
                          c->pending_ref_us, em_abs);
                }
                pend_seen = 1;
            } else if (idx < nref) {
                int want = ref[idx].dir > 0 ? 1 : 0;
                if ((int)segs[s].dir != want) {
                    failf("tick %lld step %d: dir %u ref %d",
                          (long long)c->tick_index, idx, (unsigned)segs[s].dir,
                          ref[idx].dir);
                }
                double ref_abs = (double)c->tick_index * (double)c->tick_us + ref[idx].t_us;
                double em_abs = (double)(tick_start + t_rel) / (double)c->tpu;
                double err = fabs(em_abs - ref_abs);
                if (err > g_max_err_us) {
                    g_max_err_us = err;
                }
                c->last_err_us = err;
                if (err > bound) {
                    failf("tick %lld step %d: time err %.4f us > %.4f (ref %.6f em %.6f)",
                          (long long)c->tick_index, idx, err, bound, ref_abs, em_abs);
                }
                idx++;
            } else {
                failf("tick %lld: extra emitted step %d", (long long)c->tick_index, emitted);
            }
            emitted++;
            c->sum_impl += segs[s].dir ? 1 : -1;
            iv += (int64_t)segs[s].add;
        }
    }
    if (pend && !pend_seen) {
        failf("tick %lld: the step deferred by the previous tick was not emitted",
              (long long)c->tick_index);
    }
    /* A deferral is accepted only when it is exactly one step short, that step
     * is the tick's last reference step, its reference crossing lies within
     * the half-error-budget bound of the tick end, and (checked when the next
     * tick runs) that step is the next tick's first emission. */
    int new_defer = 0;
    if (idx == nref) {
        /* complete */
    } else if (idx == nref - 1 && nref >= 1 && (!pend || pend_seen)) {
        double ref_ticks = ref[nref - 1].t_us * (double)c->tpu;
        int64_t err_bound_ticks = ((int64_t)KM_STEP_SEGMENTS_MAX_ERROR_US * c->tpu - 1) / 2;
        if (err_bound_ticks < 1) {
            err_bound_ticks = 1;
        }
        if ((double)c->tick_len_ticks - ref_ticks > (double)err_bound_ticks + 1e-6) {
            failf("tick %lld: deferred step is %.4f ticks before the tick end (bound %lld)",
                  (long long)c->tick_index,
                  (double)c->tick_len_ticks - ref_ticks, (long long)err_bound_ticks);
        } else {
            new_defer = 1;
            g_deferrals++;
        }
    } else {
        failf("tick %lld: %d reference steps emitted, %d expected",
              (long long)c->tick_index, idx, nref);
    }

    c->total_ref += nref;
    c->total_impl += emitted;

    /* The header makes M the driver's microstep: a positive step is due when
     * the path reaches M + 1, a negative step when it reaches M - 1, and M
     * then moves by one.  So M must equal the start microstep plus the signed
     * number of steps actually emitted, and no step may be taken to a
     * microstep the path never reached within this tick. */
    int64_t want_M = c->M0 + c->sum_impl;
    if (c->M_impl != want_M) {
        failf("tick %lld [%s]: driver_microstep %lld but %lld steps emitted from %lld",
              (long long)c->tick_index, c->tag, (long long)c->M_impl,
              (long long)c->sum_impl, (long long)c->M0);
    }
    /* The new anchor is the last step of this tick (or the carried one when
     * there was none), so the expectation is tick length minus the last
     * step's offset from the tick start.  Without a step and without a
     * restart t_rel is negative, and with a restart it stays at the tick
     * start, both giving the carried anchor plus the tick. */
    int64_t expect = c->tick_len_ticks - t_rel;
    if (c->restart && emitted == 0) {
        expect += (int64_t)A_before;
    }
    if (expect < 0) {
        failf("tick %lld [%s]: last step is %lld ticks past the tick end",
              (long long)c->tick_index, c->tag, (long long)-expect);
    } else {
        int64_t got = (int64_t)c->A_in;
        int64_t adiff = got - expect;
        if (adiff < 0) {
            adiff = -adiff;
        }
        if (adiff > 1) {
            failf("tick %lld [%s]: anchor %lld expected %lld",
                  (long long)c->tick_index, c->tag, (long long)got, (long long)expect);
        }
    }
    /* A deferral leaves the driver one step behind the path, to be caught up
     * by the next tick. */
    int64_t M_expect = Mref - (new_defer ? (ref[nref - 1].dir > 0 ? 1 : -1) : 0);
    if (c->M_impl != M_expect) {
        failf("tick %lld [%s]: driver_microstep %lld ref %lld%s (%d steps emitted, %d expected)",
              (long long)c->tick_index, c->tag, (long long)c->M_impl, (long long)Mref,
              new_defer ? " one deferred" : "", emitted, nref);
        if (g_mnotes < 2) {
            g_mnotes++;
            printf("NOTE: [%s] tick %lld driver_microstep impl %lld ref %lld\n",
                   c->tag, (long long)c->tick_index, (long long)c->M_impl, (long long)Mref);
            printf("      start p=%.9f v=%.9f a=%.9f M_before=%lld\n",
                   phase_of(start), rate_of(start), accel_of(start), (long long)M_before);
            printf("      end   p=%.9f v=%.9f a=%.9f\n",
                   phase_of(end), rate_of(end), accel_of(end));
            printf("      A_in=%u tick_len=%lld ref_steps=%d emitted=%d\n",
                   (unsigned)A_before, (long long)c->tick_len_ticks, nref, emitted);
            for (int s = 0; s < seg_used; s++) {
                printf("      seg[%d] interval=%u count=%u add=%d dir=%u\n", s,
                       (unsigned)segs[s].interval, (unsigned)segs[s].count,
                       (int)segs[s].add, (unsigned)segs[s].dir);
            }
            for (int i = 0; i < nref && i < 8; i++) {
                printf("      ref[%d] t=%.6f dir=%d\n", i, ref[i].t_us, ref[i].dir);
            }
            printf("      impl first steps t:");
            int64_t tt = -(int64_t)A_before;
            int shown = 0;
            for (int s = 0; s < seg_used && shown < 8; s++) {
                int64_t iv = (int64_t)segs[s].interval;
                for (int k = 0; k < (int)segs[s].count && shown < 8; k++) {
                    tt += iv;
                    printf(" %.6f", (double)tt / (double)c->tpu);
                    iv += (int64_t)segs[s].add;
                    shown++;
                }
            }
            printf("\n");
            fflush(stdout);
        }
    }
    if (new_defer) {
        c->pending = 1;
        c->pending_dir = ref[nref - 1].dir;
        c->pending_ref_us = (double)c->tick_index * (double)c->tick_us + ref[nref - 1].t_us;
    } else {
        c->pending = 0;
    }
    c->M_ref = Mref;
    c->tick_index++;
}

/* ------------------------------------------------------------------ */
/* Scenarios                                                           */
/* ------------------------------------------------------------------ */

typedef struct {
    uint32_t vmax, amax, dmax, jmax;
    const char *name;
} limits_t;

/* A move this long takes at most dist/vmax + the ramps + the slow landing
 * crawl; the guard is that, with generous slack.  A tick budget (rather than a
 * step budget) keeps a misbehaving implementation from running forever. */
static int64_t guard_ticks(double dist, const limits_t *L, uint32_t tick_us)
{
    double t_s = fabs(dist) / (double)L->vmax + 3.0 * (double)L->vmax / (double)L->amax + 0.5;
    return (int64_t)(t_s * 1e6 / (double)tick_us) + 4000;
}

static void scenario_line(const char *name, const char *lim, uint32_t tick_us,
                          uint32_t tpu, int64_t ref, int64_t impl, int fails_before)
{
    printf("%-14s %-6s tick=%4u tpu=%3u steps ref=%7lld impl=%7lld %s\n",
           name, lim, tick_us, tpu, (long long)ref, (long long)impl,
           g_failures == fails_before ? "ok" : "FAIL");
    fflush(stdout);
}

static void drive_active(ctx_t *c, km_rate_t *kr, int64_t guard)
{
    while (kr->target_phase_active && guard-- > 0) {
        km_rate_t st = *kr;
        km_rate_advance(kr, c->tick_us);
        run_tick(c, &st, kr);
    }
}

/* Drive a move that starts from an idle stepper: the caller marks its first
 * tick a restart and lets the carried anchor bound the first spacing. */
static void drive_active_restart(ctx_t *c, km_rate_t *kr, int64_t guard)
{
    int first = 1;
    while (kr->target_phase_active && guard-- > 0) {
        km_rate_t st = *kr;
        km_rate_advance(kr, c->tick_us);
        c->restart = first;
        run_tick(c, &st, kr);
        first = 0;
    }
}

static void scenario_position(const limits_t *L, uint32_t tick_us, uint32_t tpu)
{
    static const int64_t moves[] = {1, 2, 3, 10, 100, 1000, 10000, 200000};
    int fails_before = g_failures;
    int64_t ref = 0, impl = 0;
    size_t nmoves = sizeof(moves) / sizeof(moves[0]);
    for (size_t mi = 0; mi < nmoves; mi++) {
        for (int sgn = 0; sgn < 2; sgn++) {
            int64_t N = moves[mi];
            int64_t target = sgn ? -N : N;
            ctx_t c;
            ctx_init(&c, "position", tick_us, tpu, tpu, 0);
            km_rate_t kr;
            km_rate_init(&kr);
            km_rate_set_limits(&kr, L->vmax, L->amax, L->dmax, L->jmax);
            km_rate_set_target_rate(&kr, (int32_t)L->vmax);
            km_rate_set_target_phase(&kr, target);
            drive_active(&c, &kr, guard_ticks((double)target, L, tick_us));
            if (kr.target_phase_active) {
                failf("position N=%lld sign=%d: never landed (ticks=%lld)",
                      (long long)N, sgn, (long long)c.tick_index);
            }
            if (kr.phase_whole != target || kr.rate != 0) {
                failf("position N=%lld sign=%d: final phase %lld rate %lld",
                      (long long)N, sgn, (long long)kr.phase_whole,
                      (long long)kr.rate);
            }
            ref += c.total_ref;
            impl += c.total_impl;
        }
    }
    scenario_line("position", L->name, tick_us, tpu, ref, impl, fails_before);
}

static void scenario_rate(const limits_t *L, uint32_t tick_us, uint32_t tpu)
{
    int fails_before = g_failures;
    ctx_t c;
    ctx_init(&c, "rate-reversal", tick_us, tpu, tpu, 0);
    km_rate_t kr;
    km_rate_init(&kr);
    km_rate_set_limits(&kr, L->vmax, L->amax, L->dmax, L->jmax);
    km_rate_set_target_rate(&kr, (int32_t)L->vmax);
    for (int i = 0; i < 10; i++) {
        km_rate_t st = kr;
        km_rate_advance(&kr, tick_us);
        run_tick(&c, &st, &kr);
    }
    if (kr.rate <= 0) {
        failf("rate: not ramping up before retarget");
    }
    int64_t vq = (int64_t)llround((double)L->vmax * Q32_SCALE);
    int64_t stop_at = -(vq - vq / 100);
    km_rate_set_target_rate(&kr, -((int32_t)L->vmax));
    int64_t guard = guard_ticks(0.0, L, tick_us);
    while (kr.rate > stop_at && guard-- > 0) {
        km_rate_t st = kr;
        km_rate_advance(&kr, tick_us);
        run_tick(&c, &st, &kr);
    }
    km_rate_set_target_rate(&kr, 0);
    guard = guard_ticks(0.0, L, tick_us);
    while (!(kr.rate == 0 && kr.accel == 0) && guard-- > 0) {
        km_rate_t st = kr;
        km_rate_advance(&kr, tick_us);
        run_tick(&c, &st, &kr);
    }
    scenario_line("rate-reversal", L->name, tick_us, tpu, c.total_ref, c.total_impl,
                  fails_before);
}

static void retarget_one(const limits_t *L, uint32_t tick_us, uint32_t tpu,
                         int64_t N, int64_t newN, const char *tag,
                         int64_t *ref_out, int64_t *impl_out)
{
    ctx_t c;
    ctx_init(&c, tag, tick_us, tpu, tpu, 0);
    km_rate_t kr;
    km_rate_init(&kr);
    km_rate_set_limits(&kr, L->vmax, L->amax, L->dmax, L->jmax);
    km_rate_set_target_rate(&kr, (int32_t)L->vmax);
    int64_t start_rc = km_rate_set_target_phase(&kr, N);
    if (start_rc != 0) {
        failf("retarget %s: start move to %lld refused", tag, (long long)N);
    }
    int64_t vq = (int64_t)llround((double)L->vmax * Q32_SCALE);
    /* Advance a quarter of the way: the move is running and has not started
     * braking, so the retarget lands mid-move (a short move may never reach
     * vmax, so the rate is not the trigger). */
    int64_t fail_before = g_failures;
    int64_t guard = guard_ticks((double)(newN > N ? newN : N), L, tick_us);
    while (kr.target_phase_active && kr.phase_whole < N / 4 && guard-- > 0) {
        km_rate_t st = kr;
        km_rate_advance(&kr, tick_us);
        run_tick(&c, &st, &kr);
    }
    if (g_failures == fail_before) {
        if (!kr.target_phase_active) {
            failf("retarget %s: move landed before the retarget", tag);
        } else if (kr.rate <= 0 || kr.rate > vq) {
            failf("retarget %s: not mid-ramp at the retarget (rate %lld of %lld)",
                  tag, (long long)kr.rate, (long long)vq);
        }
    }
    int64_t moved_before = kr.phase_whole;
    /* The core refuses a target it can no longer stop for, and only that. */
    int64_t stop = km_rate_stop_distance_q32(&kr) >> 32;
    int64_t left = newN > moved_before ? newN - moved_before : moved_before - newN;
    int reachable = left > stop + 1;
    int rc = km_rate_set_target_phase(&kr, newN);
    if (reachable && rc != 0) {
        failf("retarget %s: reachable target refused at phase %lld", tag,
              (long long)moved_before);
    } else if (!reachable && rc == 0) {
        failf("retarget %s: target inside the stopping distance accepted", tag);
    }
    int64_t target = (rc == 0) ? newN : N;
    guard = guard_ticks((double)(newN > N ? newN : N) + 1000.0, L, tick_us);
    while (kr.target_phase_active && guard-- > 0) {
        km_rate_t st = kr;
        km_rate_advance(&kr, tick_us);
        run_tick(&c, &st, &kr);
    }
    if (kr.target_phase_active) {
        failf("retarget %s: never landed (ticks %lld)", tag, (long long)c.tick_index);
    }
    if (kr.phase_whole != target || kr.rate != 0) {
        failf("retarget %s: final microstep %lld rate %lld expected %lld",
              tag, (long long)kr.phase_whole, (long long)kr.rate, (long long)target);
    }
    *ref_out += c.total_ref;
    *impl_out += c.total_impl;
}

static void scenario_retarget(const limits_t *L, uint32_t tick_us, uint32_t tpu)
{
    int fails_before = g_failures;
    int64_t ref = 0, impl = 0;
    retarget_one(L, tick_us, tpu, 4000, 2500, "retarget-nearer", &ref, &impl);
    retarget_one(L, tick_us, tpu, 4000, 8000, "retarget-farther", &ref, &impl);
    scenario_line("retarget", L->name, tick_us, tpu, ref, impl, fails_before);
}

static void scenario_dither(uint32_t tick_us, uint32_t tpu)
{
    int fails_before = g_failures;
    ctx_t c;
    ctx_init(&c, "dither", tick_us, tpu, tpu, 5);
    double w = 2.0 * KM_REF_PI / 4000.0;
    for (int k = 0; k < 16; k++) {
        double t0 = (double)k * (double)tick_us;
        double t1 = (double)(k + 1) * (double)tick_us;
        km_rate_t s, e;
        build_state(&s, 5.0 + 0.3 * sin(w * t0), 0.3 * w * cos(w * t0),
                    -0.3 * w * w * sin(w * t0));
        build_state(&e, 5.0 + 0.3 * sin(w * t1), 0.3 * w * cos(w * t1),
                    -0.3 * w * w * sin(w * t1));
        run_tick(&c, &s, &e);
    }
    if (c.total_ref != 0) {
        failf("dither: reference produced %lld steps", (long long)c.total_ref);
    }
    if (c.total_impl != 0) {
        failf("dither: implementation produced %lld steps", (long long)c.total_impl);
    }
    scenario_line("dither", "-", tick_us, tpu, c.total_ref, c.total_impl, fails_before);
}

static void scenario_two_zero(uint32_t tick_us, uint32_t tpu)
{
    int fails_before = g_failures;
    ctx_t c;
    ctx_init(&c, "two-zero-v", tick_us, tpu, tpu, 0);
    double T = (double)tick_us;
    double r1 = T / 10.0;
    double r2 = 9.0 * T / 10.0;
    double v0 = 0.05;
    double K = v0 / (r1 * r2);
    double a0 = -K * (r1 + r2);
    double cc = K / 3.0;
    double p1 = v0 * T + 0.5 * a0 * T * T + cc * T * T * T;
    double v1 = v0 + a0 * T + 3.0 * cc * T * T;
    double a1 = a0 + 6.0 * cc * T;
    km_rate_t s, e;
    build_state(&s, 0.0, v0, a0);
    build_state(&e, p1, v1, a1);
    run_tick(&c, &s, &e);
    scenario_line("two-zero-v", "-", tick_us, tpu, c.total_ref, c.total_impl,
                  fails_before);
}

static void scenario_errors(uint32_t tick_us, uint32_t tpu)
{
    int fails_before = g_failures;

    /* A tick whose intervals must fall below the driver minimum. */
    {
        km_rate_t s, e;
        build_state(&s, 0.0, 0.2, 0.0);
        build_state(&e, 0.2 * (double)tick_us, 0.2, 0.0);
        int64_t M = 0;
        uint32_t A = 7;
        km_step_segment_t segs[KM_STEP_SEGMENTS_MAX];
        uint8_t cnt = 0;
        uint32_t min_interval = tick_us * tpu;
        int rc = km_step_segments_generate(&s, &e, tick_us, tpu, min_interval,
                                           false, &M, &A, segs, &cnt,
                                           KM_STEP_SEGMENTS_MAX);
        if (rc != KM_STEP_SEGMENTS_E_MIN_INTERVAL) {
            failf("min-interval: rc=%d expected %d", rc, KM_STEP_SEGMENTS_E_MIN_INTERVAL);
        }
        if (M != 0 || A != 7 || cnt != 0) {
            failf("min-interval: outputs changed M=%lld A=%u cnt=%u",
                  (long long)M, (unsigned)A, (unsigned)cnt);
        }
    }

    /* A first interval above UINT32_MAX. */
    {
        km_rate_t s, e;
        build_state(&s, 0.0, 0.01, 0.0);
        build_state(&e, 0.01 * (double)tick_us, 0.01, 0.0);
        int64_t M = 0;
        uint32_t A = 0xFFFFF000u;
        km_step_segment_t segs[KM_STEP_SEGMENTS_MAX];
        uint8_t cnt = 0;
        int rc = km_step_segments_generate(&s, &e, tick_us, tpu, 1u,
                                           false, &M, &A, segs, &cnt,
                                           KM_STEP_SEGMENTS_MAX);
        if (rc != KM_STEP_SEGMENTS_E_RANGE) {
            failf("range: rc=%d expected %d", rc, KM_STEP_SEGMENTS_E_RANGE);
        }
        if (M != 0 || A != 0xFFFFF000u || cnt != 0) {
            failf("range: outputs changed M=%lld A=%u cnt=%u",
                  (long long)M, (unsigned)A, (unsigned)cnt);
        }
    }

    scenario_line("errors", "-", tick_us, tpu, 0, 0, fails_before);
}

static void scenario_long(const limits_t *L, uint32_t tick_us, uint32_t tpu)
{
    int fails_before = g_failures;
    ctx_t c;
    ctx_init(&c, "constant-run", tick_us, tpu, tpu, 0);
    km_rate_t kr;
    km_rate_init(&kr);
    km_rate_set_limits(&kr, L->vmax, L->amax, L->dmax, L->jmax);
    km_rate_set_target_rate(&kr, (int32_t)L->vmax);
    for (int64_t i = 0; i < 100000; i++) {
        km_rate_t st = kr;
        km_rate_advance(&kr, tick_us);
        run_tick(&c, &st, &kr);
    }
    double bound = (double)KM_STEP_SEGMENTS_MAX_ERROR_US + 1.0 / (double)tpu + 1e-6;
    if (c.last_err_us > bound) {
        failf("long-run drift: last step err %.4f us > %.4f", c.last_err_us, bound);
    }
    scenario_line("constant-run", L->name, tick_us, tpu, c.total_ref, c.total_impl,
                  fails_before);
}

/* A tick's absolute phase in Q32 overflows int64 once |phase_whole| reaches
 * 2^31, which the rate core's +-1e12 microstep range allows.  Run position
 * and rate moves starting at +-(2^40) and just inside the core limits; the
 * generator must produce the same steps as the double reference, which is
 * built from the absolute phase. */
static void scenario_overflow(const limits_t *L, uint32_t tick_us, uint32_t tpu,
                              int64_t base)
{
    int fails_before = g_failures;
    int64_t ref = 0, impl = 0;
    static const int64_t moves[] = {1, 1000, 200000};

    for (size_t mi = 0; mi < sizeof(moves) / sizeof(moves[0]); mi++) {
        for (int sgn = 0; sgn < 2; sgn++) {
            int64_t N = moves[mi];
            int64_t target = base + (sgn ? -N : N);
            ctx_t c;
            ctx_init(&c, "overflow-pos", tick_us, tpu, tpu, base);
            km_rate_t kr;
            km_rate_init(&kr);
            km_rate_set_limits(&kr, L->vmax, L->amax, L->dmax, L->jmax);
            km_rate_set_target_rate(&kr, (int32_t)L->vmax);
            kr.phase_whole = base;
            if (km_rate_set_target_phase(&kr, target) != 0) {
                failf("overflow-pos base=%lld N=%lld: target refused",
                      (long long)base, (long long)N);
                continue;
            }
            drive_active(&c, &kr, guard_ticks((double)N, L, tick_us));
            if (kr.target_phase_active) {
                failf("overflow-pos base=%lld N=%lld sign=%d: never landed",
                      (long long)base, (long long)N, sgn);
            }
            if (kr.phase_whole != target || kr.rate != 0) {
                failf("overflow-pos base=%lld N=%lld sign=%d: final %lld rate %lld",
                      (long long)base, (long long)N, sgn,
                      (long long)kr.phase_whole, (long long)kr.rate);
            }
            ref += c.total_ref;
            impl += c.total_impl;
        }
    }

    {
        ctx_t c;
        ctx_init(&c, "overflow-rate", tick_us, tpu, tpu, base);
        km_rate_t kr;
        km_rate_init(&kr);
        km_rate_set_limits(&kr, L->vmax, L->amax, L->dmax, L->jmax);
        km_rate_set_target_rate(&kr, (int32_t)L->vmax);
        kr.phase_whole = base;
        for (int i = 0; i < 10; i++) {
            km_rate_t st = kr;
            km_rate_advance(&kr, tick_us);
            run_tick(&c, &st, &kr);
        }
        int64_t vq = (int64_t)llround((double)L->vmax * Q32_SCALE);
        int64_t stop_at = -(vq - vq / 100);
        km_rate_set_target_rate(&kr, -((int32_t)L->vmax));
        int64_t guard = guard_ticks(0.0, L, tick_us);
        while (kr.rate > stop_at && guard-- > 0) {
            km_rate_t st = kr;
            km_rate_advance(&kr, tick_us);
            run_tick(&c, &st, &kr);
        }
        km_rate_set_target_rate(&kr, 0);
        guard = guard_ticks(0.0, L, tick_us);
        while (!(kr.rate == 0 && kr.accel == 0) && guard-- > 0) {
            km_rate_t st = kr;
            km_rate_advance(&kr, tick_us);
            run_tick(&c, &st, &kr);
        }
        ref += c.total_ref;
        impl += c.total_impl;
    }

    char name[32];
    snprintf(name, sizeof(name), "overflow%+lld", (long long)base);
    scenario_line(name, L->name, tick_us, tpu, ref, impl, fails_before);
}

/* The relative-Q32 base must be invisible to non-overflowing positions: the
 * same move run at phase 0 and shifted by +2^40 records identical segments,
 * with only the driver microstep moved by the shift and the anchor unchanged. */
static void scenario_relative_identity(void)
{
    int fails_before = g_failures;
    const uint32_t tick_us = 1000, tpu = 64;
    const int nticks = 8;
    const int64_t shift = 1099511627776LL; /* 2^40 */

    km_rate_t a, b;
    km_rate_init(&a);
    km_rate_init(&b);
    km_rate_set_limits(&a, 4000, 20000, 20000, 400000);
    km_rate_set_limits(&b, 4000, 20000, 20000, 400000);
    km_rate_set_target_rate(&a, 4000);
    km_rate_set_target_rate(&b, 4000);
    b.phase_whole = shift;

    int64_t mA = 0, mB = shift;   /* driver microstep, run 0 and run shift */
    uint32_t aA = 0, aB = 0;      /* carried anchor */
    km_step_segment_t seg0[KM_STEP_SEGMENTS_MAX], seg1[KM_STEP_SEGMENTS_MAX];
    uint8_t cnt0 = 0, cnt1 = 0;

    for (int i = 0; i < nticks; i++) {
        km_rate_t sa = a, sb = b;
        km_rate_advance(&a, tick_us);
        km_rate_advance(&b, tick_us);
        int rc0 = km_step_segments_generate(&sa, &a, tick_us, tpu, tpu, false,
                                            &mA, &aA, seg0, &cnt0, KM_STEP_SEGMENTS_MAX);
        int rc1 = km_step_segments_generate(&sb, &b, tick_us, tpu, tpu, false,
                                            &mB, &aB, seg1, &cnt1, KM_STEP_SEGMENTS_MAX);
        if (rc0 != KM_STEP_SEGMENTS_OK || rc1 != KM_STEP_SEGMENTS_OK) {
            failf("relative-identity tick %d: rc %d / %d", i, rc0, rc1);
            return;
        }
        if (cnt0 != cnt1) {
            failf("relative-identity tick %d: %u vs %u segments",
                  i, (unsigned)cnt0, (unsigned)cnt1);
            continue;
        }
        for (int s = 0; s < (int)cnt0; s++) {
            if (seg0[s].interval != seg1[s].interval ||
                seg0[s].count != seg1[s].count ||
                seg0[s].add != seg1[s].add ||
                seg0[s].dir != seg1[s].dir) {
                failf("relative-identity tick %d seg %d differs", i, s);
            }
        }
        if (aA != aB) {
            failf("relative-identity tick %d: anchor %u vs %u",
                  i, (unsigned)aA, (unsigned)aB);
        }
    }
    if (mB - mA != shift) {
        failf("relative-identity: driver offset %lld expected %lld",
              (long long)(mB - mA), (long long)shift);
    }
    scenario_line("relative-id", "gentle", tick_us, tpu, 0, 0, fails_before);
}

static void scenario_idle_restart(const limits_t *L, uint32_t tick_us, uint32_t tpu)
{
    int fails_before = g_failures;
    ctx_t c;
    ctx_init(&c, "idle-restart", tick_us, tpu, tpu, 0);
    km_rate_t kr;
    km_rate_init(&kr);
    km_rate_set_limits(&kr, L->vmax, L->amax, L->dmax, L->jmax);
    km_rate_set_target_rate(&kr, (int32_t)L->vmax);
    km_rate_set_target_phase(&kr, 300);
    drive_active(&c, &kr, guard_ticks(600.0, L, tick_us));
    if (kr.phase_whole != 300 || kr.rate != 0) {
        failf("idle-restart: first move ended phase %lld rate %lld",
              (long long)kr.phase_whole, (long long)kr.rate);
    }
    /* The stepper went idle: the caller keeps the carried anchor and marks
     * this move's first tick a restart. */
    km_rate_set_target_rate(&kr, (int32_t)L->vmax);
    km_rate_set_target_phase(&kr, 600);
    drive_active_restart(&c, &kr, guard_ticks(600.0, L, tick_us));
    if (kr.phase_whole != 600 || kr.rate != 0) {
        failf("idle-restart: second move ended phase %lld rate %lld",
              (long long)kr.phase_whole, (long long)kr.rate);
    }
    scenario_line("idle-restart", L->name, tick_us, tpu, c.total_ref, c.total_impl,
                  fails_before);
}

/* The rate limit: a 5 us minimum interval at 200000 steps/s (exactly
 * 1/min_interval), the limits test_safety_contracts drives (max velocity
 * 200000, accel 2000000, its default jerk 20 x amax), ramped to the limit and
 * cruised for well over 200 ticks.  every_restart marks each tick a restart
 * with the carried anchor, the way a harness that drains every tick does.
 *
 * At this rate the tick's cubic can bring a crossing a tick inside the
 * minimum, and a delayed crossing can be left for the next tick; run_tick
 * accepts exactly that. */
static void scenario_rate_limit(const limits_t *L, uint32_t tick_us, uint32_t tpu,
                                int every_restart)
{
    int fails_before = g_failures;
    int64_t def_before = g_deferrals;
    const int32_t cruise = 200000;
    const int64_t cruise_q = ((int64_t)cruise * 67108864LL + 7812LL) / 15625LL;
    ctx_t c;
    ctx_init(&c, every_restart ? "limit-restart" : "limit-chained", tick_us, tpu,
             5u * tpu, 0);
    km_rate_t kr;
    km_rate_init(&kr);
    km_rate_set_limits(&kr, L->vmax, L->amax, L->dmax, L->jmax);
    km_rate_set_target_rate(&kr, cruise);
    int64_t guard = 100000;
    while ((kr.rate != cruise_q || kr.accel != 0) && guard-- > 0) {
        km_rate_t st = kr;
        km_rate_advance(&kr, tick_us);
        c.restart = every_restart;
        run_tick(&c, &st, &kr);
    }
    if (guard <= 0 || kr.rate != cruise_q || kr.accel != 0) {
        failf("limit: never settled at %d steps/s (rate %lld accel %lld)",
              cruise, (long long)kr.rate, (long long)kr.accel);
    }
    for (int i = 0; i < 220; i++) {
        km_rate_t st = kr;
        km_rate_advance(&kr, tick_us);
        c.restart = every_restart;
        run_tick(&c, &st, &kr);
    }
    printf("%-14s %-6s tick=%4u tpu=%3u min=%5u max_seg=%2d deferrals=%lld\n",
           every_restart ? "limit-restart" : "limit-chained", L->name, tick_us, tpu,
           5u * tpu, c.max_seg_tick, (long long)(g_deferrals - def_before));
    scenario_line(every_restart ? "limit-restart" : "limit-chained", L->name,
                  tick_us, tpu, c.total_ref, c.total_impl, fails_before);
}

/* 2% above the cruise interval (5.1 us at 200000 steps/s): the path is
 * genuinely too fast, so every step needs a delay the fit cannot absorb and
 * the tick must be refused with E_MIN_INTERVAL -- not scaled into a
 * whole-tick fit (OK) and not run out of segments (E_RANGE).
 *
 * The ramp runs at the 5 us minimum of scenario_rate_limit, because while the
 * spacing is still tightening the path is not yet too fast: only the cruise
 * ticks, which are the ones whose spacing is 2% under the minimum, are handed
 * the wider minimum.  The delay each of those needs grows by the width of the
 * gap once per step, so the first cruise tick or one shortly after it is the
 * one refused, once the accumulated delay passes the half-error-budget. */
static void scenario_rate_limit_excess(const limits_t *L, uint32_t tick_us, uint32_t tpu)
{
    int fails_before = g_failures;
    const int32_t cruise = 200000;
    const int64_t cruise_q = ((int64_t)cruise * 67108864LL + 7812LL) / 15625LL;
    uint32_t min_ramp = 5u * tpu;
    uint32_t min_iv = (5u * tpu * 102u) / 100u;
    km_rate_t kr;
    km_rate_init(&kr);
    km_rate_set_limits(&kr, L->vmax, L->amax, L->dmax, L->jmax);
    km_rate_set_target_rate(&kr, cruise);
    int64_t M = 0;
    uint32_t A = 0;
    int64_t guard = 100000;
    int at_cruise = 0, refusals = 0, cruise_ticks = 0;
    while (guard-- > 0) {
        km_rate_t st = kr;
        km_rate_advance(&kr, tick_us);
        int cruise_tick = (kr.rate == cruise_q && kr.accel == 0);
        km_step_segment_t segs[KM_STEP_SEGMENTS_MAX];
        uint8_t cnt = 0;
        int rc = km_step_segments_generate(&st, &kr, tick_us, tpu,
                                           cruise_tick ? min_iv : min_ramp, false,
                                           &M, &A, segs, &cnt, KM_STEP_SEGMENTS_MAX);
        if (rc == KM_STEP_SEGMENTS_E_RANGE) {
            failf("excess: E_RANGE at rate %lld (%d cruise ticks, %d refusals)",
                  (long long)kr.rate, cruise_ticks, refusals);
            break;
        }
        if (!cruise_tick) {
            if (rc != KM_STEP_SEGMENTS_OK) {
                failf("excess: rc=%d at rate %lld", rc, (long long)kr.rate);
                break;
            }
            continue;
        }
        at_cruise = 1;
        cruise_ticks++;
        if (rc == KM_STEP_SEGMENTS_E_MIN_INTERVAL) {
            refusals++;
            break;
        }
        if (rc != KM_STEP_SEGMENTS_OK) {
            failf("excess: cruise tick rc=%d", rc);
            break;
        }
        if (cruise_ticks > 250) {
            break;
        }
    }
    if (!at_cruise) {
        failf("excess: never reached %d steps/s", cruise);
    }
    if (refusals == 0) {
        failf("excess: the 2%%-wider minimum never refused a cruise tick");
    }
    printf("%-14s %-6s tick=%4u tpu=%3u min=%5u refusal=%d cruise_ticks=%d\n",
           "rate-excess", L->name, tick_us, tpu, min_iv, refusals, cruise_ticks);
    scenario_line("rate-excess", L->name, tick_us, tpu, 0, 0, fails_before);
}

/* The recorded rate-limit tick that first showed the whole-tick fit's residual
 * shortening its last interval below the minimum: a restart with 276 ticks
 * carried since the last step, minimum spacing 320 ticks (5 us).  Only the
 * start phase, rate and accel and the end phase enter a tick, so build_state
 * gets the cubic's own end rate and accel alongside the recorded phases. */
static void scenario_captured_restart(void)
{
    int fails_before = g_failures;
    const uint32_t tick_us = 1000, tpu = 64;
    double T = (double)tick_us;
    double p0 = 13204.0 + 3694760616.0 / Q32_SCALE;
    double p1 = 13403.0 + 1776360712.0 / Q32_SCALE;
    double v0 = 852035631.0 / Q32_SCALE;
    double a0 = 101330994.0 / Q48_SCALE;
    double cc = (p1 - p0 - v0 * T - 0.5 * a0 * T * T) / (T * T * T);
    double v1 = v0 + a0 * T + 3.0 * cc * T * T;
    double a1 = a0 + 6.0 * cc * T;
    km_rate_t s, e;
    build_state(&s, p0, v0, a0);
    build_state(&e, p1, v1, a1);
    ctx_t c;
    ctx_init(&c, "captured", tick_us, tpu, 320u, 13204);
    c.restart = 1;
    c.A_in = 276;
    run_tick(&c, &s, &e);
    scenario_line("captured", "limit", tick_us, tpu, c.total_ref, c.total_impl,
                  fails_before);
}

/* The restart branch of the anchor contract:
 * (a) the crossing falls 1 us after the tick start with a 1 ms carried anchor
 *     (accepted, first interval ~1 us);
 * (b) anchor + first interval is below the minimum (E_MIN_INTERVAL);
 * (c) a long idle saturates the anchor at 0x7fffffff instead of wrapping;
 * (d) a low-speed run, fewer than one step per tick with the stepper idle at
 *     every tick start, every step inside the error bound and no fault. */
static void scenario_restart_cases(void)
{
    int fails_before = g_failures;

    /* (a) A restart that would look like a sub-minimum first interval if the
     * anchor were reset: p(t) = t, so the crossing lands 64 ticks (1 us at
     * 64 ticks/us) after the tick start, while the real spacing is the 1 ms
     * carried anchor plus those 64 ticks. */
    {
        const uint32_t tpu = 64;
        km_rate_t s, e;
        build_state(&s, 0.0, 1.0, 0.0);
        build_state(&e, 1.0, 1.0, 0.0);
        int64_t M = 0;
        uint32_t A = 1000u * tpu;
        km_step_segment_t segs[KM_STEP_SEGMENTS_MAX];
        uint8_t cnt = 0;
        int rc = km_step_segments_generate(&s, &e, 1u, tpu, 500u, true,
                                           &M, &A, segs, &cnt, KM_STEP_SEGMENTS_MAX);
        if (rc != KM_STEP_SEGMENTS_OK) {
            failf("restart-1us: rc=%d expected ok", rc);
        } else if (cnt != 1) {
            failf("restart-1us: %u segments expected 1", (unsigned)cnt);
        } else if (segs[0].interval < 63u || segs[0].interval > 65u) {
            failf("restart-1us: first interval %u expected ~64 ticks (1 us)",
                  (unsigned)segs[0].interval);
        } else if (M != 1) {
            failf("restart-1us: driver_microstep %lld expected 1", (long long)M);
        }
    }

    /* (b) Same geometry, but the minimum interval exceeds the real spacing
     * (1 ms anchor + 1 us), so the tick must fault without touching its
     * outputs. */
    {
        const uint32_t tpu = 64;
        km_rate_t s, e;
        build_state(&s, 0.0, 1.0, 0.0);
        build_state(&e, 1.0, 1.0, 0.0);
        int64_t M = 0;
        uint32_t A = 1000u * tpu;
        km_step_segment_t segs[KM_STEP_SEGMENTS_MAX];
        uint8_t cnt = 0;
        int rc = km_step_segments_generate(&s, &e, 1u, tpu, 100000u, true,
                                           &M, &A, segs, &cnt, KM_STEP_SEGMENTS_MAX);
        if (rc != KM_STEP_SEGMENTS_E_MIN_INTERVAL) {
            failf("restart-min: rc=%d expected %d", rc, KM_STEP_SEGMENTS_E_MIN_INTERVAL);
        }
        if (M != 0 || A != 1000u * tpu || cnt != 0) {
            failf("restart-min: outputs changed M=%lld A=%u cnt=%u",
                  (long long)M, (unsigned)A, (unsigned)cnt);
        }
    }

    /* (c) Idle ticks with no step: the anchor carries, and once it would pass
     * 0x7fffffff it saturates there rather than wrapping. */
    {
        const uint32_t tick_us = 1000, tpu = 64;
        km_rate_t s;
        build_state(&s, 5.0, 0.0, 0.0);
        int64_t M = 5;
        uint32_t A = 0;
        int reached = 0;
        for (int i = 0; i < 40000; i++) {
            int64_t Mi = M;
            uint32_t Ai = A;
            km_step_segment_t segs[KM_STEP_SEGMENTS_MAX];
            uint8_t cnt = 0;
            int rc = km_step_segments_generate(&s, &s, tick_us, tpu, 1u, true,
                                               &Mi, &Ai, segs, &cnt, KM_STEP_SEGMENTS_MAX);
            if (rc != KM_STEP_SEGMENTS_OK || cnt != 0 || Mi != M) {
                failf("restart-idle: tick %d rc=%d cnt=%u M=%lld", i, rc,
                      (unsigned)cnt, (long long)Mi);
                break;
            }
            if (Ai < A) {
                failf("restart-idle: anchor wrapped %u -> %u", (unsigned)A, (unsigned)Ai);
                break;
            }
            A = Ai;
            if (A == 0x7fffffffu) {
                reached = 1;
                break;
            }
        }
        if (!reached) {
            failf("restart-idle: anchor never saturated (last %u)", (unsigned)A);
        } else {
            for (int i = 0; i < 3; i++) {
                int64_t Mi = M;
                uint32_t Ai = A;
                km_step_segment_t segs[KM_STEP_SEGMENTS_MAX];
                uint8_t cnt = 0;
                int rc = km_step_segments_generate(&s, &s, tick_us, tpu, 1u, true,
                                                   &Mi, &Ai, segs, &cnt, KM_STEP_SEGMENTS_MAX);
                if (rc != KM_STEP_SEGMENTS_OK || Ai != 0x7fffffffu) {
                    failf("restart-idle: saturation unstable rc=%d A=%u", rc, (unsigned)Ai);
                    break;
                }
            }
        }
    }

    /* (d) One step every 5 ticks (0.2 per tick), the stepper idle at every
     * tick start: the anchor carries across the empty ticks and the first
     * interval of each stepping tick is measured from that tick's start. */
    {
        const uint32_t tick_us = 250, tpu = 64;
        ctx_t c;
        ctx_init(&c, "restart-low", tick_us, tpu, 1u, 0);
        c.restart = 1;
        double v = 0.2 / (double)tick_us;
        for (int i = 0; i < 2000; i++) {
            km_rate_t s, e;
            build_state(&s, 0.2 * (double)i, v, 0.0);
            build_state(&e, 0.2 * (double)(i + 1), v, 0.0);
            run_tick(&c, &s, &e);
        }
        double bound = (double)KM_STEP_SEGMENTS_MAX_ERROR_US + 1.0 / (double)tpu + 1e-6;
        if (c.total_ref == 0) {
            failf("restart-low: reference produced no steps");
        }
        if (c.total_impl != c.total_ref) {
            failf("restart-low: %lld steps emitted, %lld expected",
                  (long long)c.total_impl, (long long)c.total_ref);
        }
        if (c.last_err_us > bound) {
            failf("restart-low: last step err %.4f us > %.4f", c.last_err_us, bound);
        }
    }

    printf("%-14s %-6s %s\n", "restart", "-",
           g_failures == fails_before ? "ok" : "FAIL");
    fflush(stdout);
}

int main(void)
{
    printf("== step_segments independent reference tests ==\n");
    const limits_t limits[] = {
        {4000u, 20000u, 20000u, 400000u, "gentle"},
        {200000u, 5000000u, 5000000u, 100000000u, "harsh"},
    };
    const uint32_t tick_us_list[] = {250u, 500u, 1000u};
    const uint32_t tpu_list[] = {64u, 520u};

    for (size_t ti = 0; ti < sizeof(tick_us_list) / sizeof(tick_us_list[0]); ti++) {
        for (size_t pi = 0; pi < sizeof(tpu_list) / sizeof(tpu_list[0]); pi++) {
            uint32_t tick_us = tick_us_list[ti];
            uint32_t tpu = tpu_list[pi];
            scenario_position(&limits[0], tick_us, tpu);
            scenario_position(&limits[1], tick_us, tpu);
            scenario_rate(&limits[0], tick_us, tpu);
            scenario_rate(&limits[1], tick_us, tpu);
            scenario_retarget(&limits[0], tick_us, tpu);
            scenario_retarget(&limits[1], tick_us, tpu);
            scenario_dither(tick_us, tpu);
            scenario_two_zero(tick_us, tpu);
            scenario_errors(tick_us, tpu);
            scenario_long(&limits[0], tick_us, tpu);
            scenario_idle_restart(&limits[0], tick_us, tpu);
        }
    }

    scenario_relative_identity();
    {
        static const int64_t bases[] = {
            1099511627776LL, -1099511627776LL,   /* +-2^40 */
            999990000000LL, -999990000000LL,     /* margin inside +-1e12 */
        };
        for (size_t bi = 0; bi < sizeof(bases) / sizeof(bases[0]); bi++) {
            scenario_overflow(&limits[0], 1000u, 64u, bases[bi]);
            scenario_overflow(&limits[0], 250u, 520u, bases[bi]);
        }
    }

    scenario_restart_cases();

    {
        /* Max velocity 200000, accel 2000000 and the default jerk 20 x amax,
         * as tests/test_safety_contracts.c drives them. */
        static const limits_t limit_limits = {200000u, 2000000u, 2000000u, 40000000u,
                                              "limit"};
        /* The 5 us minimum is the cruise spacing itself, so the tick has to be
         * a whole number of those 5 us steps.  At 520 ticks/us a 1 ms tick is
         * 200 steps and the fit's integer intervals cannot all clear the
         * minimum -- every step becomes its own count-1 segment, over the 56
         * the header allows -- so that rate runs 200 us ticks (40 steps,
         * measured max 40 segments). */
        static const struct { uint32_t tpu, tick_us; } rate_cfg[] = {
            {64u, 1000u}, {520u, 200u},
        };
        for (size_t i = 0; i < sizeof(rate_cfg) / sizeof(rate_cfg[0]); i++) {
            scenario_rate_limit(&limit_limits, rate_cfg[i].tick_us, rate_cfg[i].tpu, 0);
            scenario_rate_limit(&limit_limits, rate_cfg[i].tick_us, rate_cfg[i].tpu, 1);
            scenario_rate_limit_excess(&limit_limits, rate_cfg[i].tick_us,
                                       rate_cfg[i].tpu);
        }
    }
    scenario_captured_restart();

    printf("\n---------------------------------------------\n");
    printf("failures: %d (first %d printed)\n", g_failures, FAIL_PRINT_MAX);
    printf("max segments per tick: %d (KM_STEP_SEGMENTS_MAX %d)\n",
           g_max_segments, KM_STEP_SEGMENTS_MAX);
    printf("max step-time error: %.4f us (bound %d)\n",
           g_max_err_us, KM_STEP_SEGMENTS_MAX_ERROR_US);
    printf("deferrals checked: %d\n", g_deferrals);
    if (g_failures == 0) {
        printf("RESULT: PASS\n");
    } else {
        printf("RESULT: FAIL (%d checks)\n", g_failures);
    }
    return g_failures ? 1 : 0;
}
