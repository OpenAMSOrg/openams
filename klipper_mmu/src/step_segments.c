// SPDX-License-Identifier: GPL-3.0-or-later
#include "klipper_mmu/step_segments.h"

/* Cost analysis, worst case per core tick.
 *
 * Splits: at most two zeros of v(t) inside the tick, so at most 3 pieces,
 * plus window edges every KM_STEP_SEGMENTS_WINDOW_US.  Both sets are
 * boundaries of the same walk, so there are at most
 *   (windows inside the tick) + (piece edges) + 1
 * = 4 + 2 + 1 = 7 window-pieces.  A window-piece of 1 or 2 steps is emitted
 * as that many count-1 segments.  A longer one is fitted with one constant
 * int16 add and, where the fit misses around three quarters of the way along
 * (which is where the residual of a centred interval turn peaks), bisected at
 * that step and refitted, at most KM_STEP_SEGMENTS_SPLIT_DEPTH levels deep, so
 * at most 7 runs and 8 leaves per window-piece; every step becomes at most
 * one segment, and only the pieces that need the deepest split pay per step.
 * Measured maximum over the reference suite: 26 segments per tick.
 *
 * Evaluations of the phase polynomial: every step-crossing bisection runs
 * over whole timer ticks inside one window, so at most
 *   ceil(log2(KM_STEP_SEGMENTS_WINDOW_US * ticks_per_us)) + 1
 * evaluations per root.  Each run costs at most 3 roots (first, last and the
 * probe/split step), and the 3 bisections for the zeros and the vertex of v(t)
 * run over the whole tick.  Measured maximum: 739 evaluations per tick.
 */

typedef struct {
    int64_t p0;      /* start phase, Q32 microsteps, relative to the tick base */
    int64_t Vq;      /* v0 * T, Q32 */
    int64_t Aq;      /* a0 * T^2 / 2, Q32 */
    int64_t Jq;      /* c * T^3, Q32 */
    uint32_t T;      /* timer ticks in the tick */
    uint32_t inv_T;  /* 2^32 / T, for tau in Q30 */
    int dir;         /* +1 or -1, the direction of the current piece */
    int64_t M;       /* last reached microstep */
    int64_t last;    /* time of the previous step, ticks from tick start */
    int64_t carry;   /* time since the last step at the tick start */
    int rest_first;  /* the first step is measured from the tick start */
    int stepped;     /* a step was emitted in this tick */
    int deferred;    /* a step was left for the next tick */
    int64_t min_iv;  /* minimum interval, ticks */
    int64_t err_bound; /* accepted progression error, ticks */
    km_step_segment_t *seg;
    uint32_t nseg;
    uint32_t max_seg;
} seg_ctx_t;

/* How many times a progression that misses its target may be bisected before
 * each remaining step becomes its own count-1 segment. */
#define KM_STEP_SEGMENTS_SPLIT_DEPTH 3

static uint32_t tau_of(const seg_ctx_t *c, uint32_t t)
{
    if (t >= c->T) {
        return 1u << 30;
    }
    uint32_t x = (uint32_t)(((uint64_t)t * c->inv_T + 2u) >> 2);
    return x > (1u << 30) ? (1u << 30) : x;
}

/* Phase at timer tick t, Q32 microsteps. */
static int64_t p_at(const seg_ctx_t *c, uint32_t t)
{
    int64_t x = tau_of(c, t);
    int64_t v = c->Jq >> 10;
    v = ((v * x) >> 30) + (c->Aq >> 10);
    v = ((v * x) >> 30) + (c->Vq >> 10);
    return c->p0 + ((v * x) >> 20);
}

/* Sign of v(t) = p'(t), up to a positive scale. */
static int vsign_at(const seg_ctx_t *c, uint32_t t)
{
    int64_t x = tau_of(c, t);
    int64_t v = (3 * c->Jq) >> 10;
    v = ((v * x) >> 30) + ((2 * c->Aq) >> 10);
    v = ((v * x) >> 30) + (c->Vq >> 10);
    return (v > 0) - (v < 0);
}

/* Sign of v'(t) = 2 (Aq + 3 Jq tau), up to a positive scale. */
static int gsign_at(const seg_ctx_t *c, uint32_t t)
{
    int64_t x = tau_of(c, t);
    int64_t g = (c->Aq >> 10) + ((((3 * c->Jq) >> 10) * x) >> 30);
    return (g > 0) - (g < 0);
}

static int64_t floor_q32(int64_t x)
{
    return x >> 32;
}

static int64_t ceil_q32(int64_t x)
{
    return -((-x) >> 32);
}

/* First tick in (lo, hi] at which the phase reaches the microstep level in
 * the motion direction.  lo (which may be before the tick) must not have
 * reached it, hi must have. */
static uint32_t find_cross(const seg_ctx_t *c, int64_t level, int64_t lo, uint32_t hi)
{
    int64_t target = level * 4294967296LL - c->p0;
    uint32_t l = (lo < 0 || (uint32_t)lo > hi) ? 0 : (uint32_t)lo;
    while (hi - l > 1) {
        uint32_t mid = l + (hi - l) / 2;
        int64_t dp = p_at(c, mid) - c->p0;
        int reached = (c->dir > 0) ? (dp >= target) : (dp <= target);
        if (reached) {
            hi = mid;
        } else {
            l = mid;
        }
    }
    return hi;
}

static int emit_one(seg_ctx_t *c, uint32_t t)
{
    int64_t iv = (int64_t)t - c->last;
    /* A restarting step is measured from the tick start, but the spacing that
     * must clear the minimum is the real one, anchor + first interval. */
    int64_t space = c->rest_first ? iv + c->carry : iv;
    /* At the rate limit the tick's cubic can bring a crossing a tick or so
     * inside the minimum spacing: delay that step to the minimum, within the
     * error bound and the tick. A path that is really too fast needs more
     * delay with every step and is refused here. */
    if (c->deferred) {
        return KM_STEP_SEGMENTS_OK;
    }
    if (space < c->min_iv) {
        int64_t late = c->min_iv - space;
        if (late > c->err_bound) {
            return KM_STEP_SEGMENTS_E_MIN_INTERVAL;
        }
        if ((int64_t)t + late > (int64_t)c->T) {
            /* Past the tick end: the step, and every later one, goes to the
             * next tick, which finds its crossing already reached. */
            c->deferred = 1;
            return KM_STEP_SEGMENTS_OK;
        }
        iv += late;
        t += (uint32_t)late;
    }
    if (iv > 0xFFFFFFFFLL || c->nseg >= c->max_seg) {
        return KM_STEP_SEGMENTS_E_RANGE;
    }
    c->seg[c->nseg].interval = (uint32_t)iv;
    c->seg[c->nseg].count = 1;
    c->seg[c->nseg].add = 0;
    c->seg[c->nseg].dir = (uint8_t)(c->dir > 0);
    c->nseg++;
    c->last = t;
    c->M += c->dir;
    c->rest_first = 0;
    c->stepped = 1;
    return KM_STEP_SEGMENTS_OK;
}

/* Emit n steps in the current direction; the crossing of the last one is at
 * or before hi.  On entry c->last is the previous step (the anchor).
 *
 * A window-piece is fitted with one constant int16 add only when every
 * interval the fit emits stays at or above c->min_iv and the fitted step n-1
 * lands within c->err_bound of its real crossing; a restart's first step is
 * emitted alone first, because its interval is measured from the tick start
 * and not from a step.  Near a zero of v(t) the interval sequence turns, a
 * single add cannot follow it, the fit misses near its end and the run is
 * bisected at the worst step so the halves fit.  A run that still misses
 * after KM_STEP_SEGMENTS_SPLIT_DEPTH bisections is emitted one step per
 * segment. */
static int emit_run(seg_ctx_t *c, int64_t n, uint32_t hi, int depth)
{
    if (c->deferred) {
        return KM_STEP_SEGMENTS_OK;
    }
    if (c->rest_first) {
        /* The first interval is measured from the tick start, not from the
         * previous step, so a constant-add fit cannot follow it.  emit_one
         * applies the carried anchor to the minimum-spacing check. */
        uint32_t t = find_cross(c, c->M + c->dir, c->last, hi);
        int rc = emit_one(c, t);
        if (rc) {
            return rc;
        }
        n--;
        if (n == 0 || c->deferred) {
            return KM_STEP_SEGMENTS_OK;
        }
    }

    if (n <= 2 || depth >= KM_STEP_SEGMENTS_SPLIT_DEPTH) {
        for (int64_t i = 0; i < n; i++) {
            uint32_t t = find_cross(c, c->M + c->dir, c->last, hi);
            int rc = emit_one(c, t);
            if (rc) {
                return rc;
            }
        }
        return KM_STEP_SEGMENTS_OK;
    }

    uint32_t t1 = find_cross(c, c->M + c->dir, c->last, hi);
    uint32_t tN = find_cross(c, c->M + c->dir * n, t1, hi);
    int64_t I1 = (int64_t)t1 - c->last;
    int64_t S = (int64_t)tN - c->last;
    int64_t denom = n * (n - 1) / 2;
    int64_t diff = S - n * I1;
    /* Split at the half, not at three quarters: the residual that makes a fit
     * miss grows with the run's length, so each level must halve the length
     * (and the drift) to reach a fitting run inside the depth limit. */
    int64_t m = n / 2;   /* 1 <= m < n for n >= 3, so recursion shrinks */

    if (n > 65536 || diff > 32767 * denom || diff < -32768 * denom) {
        /* No int16 add fits; bisect and retry. */
        uint32_t tm = find_cross(c, c->M + c->dir * m, t1, tN);
        int rc = emit_run(c, m, tm, depth + 1);
        if (rc) {
            return rc;
        }
        return emit_run(c, n - m, hi, depth + 1);
    }

    /* Nearest integer add = diff / denom, without 64-bit division. */
    int64_t lo = -32768, up = 32767;
    while (lo < up) {
        int64_t mid = lo + (up - lo) / 2;
        if (mid * denom < diff) {
            lo = mid + 1;
        } else {
            up = mid;
        }
    }
    int64_t add = lo;
    if (lo > -32768 && diff - (lo - 1) * denom <= lo * denom - diff) {
        add = lo - 1;
    }

    /* The first interval is the floor of the run's average after the add, not
     * the first crossing's own interval: the residual is then 0..n-1 ticks and
     * only lengthens the final interval. Pinned at the first crossing, the
     * residual takes either sign, and a negative one shortens the final
     * interval below the minimum at the rate limit. 32-bit division only. */
    int64_t base = S - add * denom;
    int64_t resid = -1;
    if (base >= 0 && base <= 0xFFFFFFFFLL) {
        I1 = (int64_t)((uint32_t)base / (uint32_t)n);
        resid = base - n * I1;
    }
    int64_t final_iv = I1 + (n - 1) * add + resid;
    int64_t t1_err = (int64_t)t1 - (c->last + I1);
    if (t1_err < 0) {
        t1_err = -t1_err;
    }

    /* The fit is accepted only if the first step, the middle probe and the
     * step before the pinned end all land within err_bound of their real
     * crossings and every interval the fit emits stays at or above the
     * minimum: I1 (with the carried time while a restart's first step is
     * pending), the smallest constant-add interval I1 + (n - 2)*add when
     * add < 0, and final_iv. */
    uint32_t tm = find_cross(c, c->M + c->dir * m, t1, tN);
    int64_t tm_fit = c->last + m * I1 + add * (m * (m - 1) / 2);
    int64_t tm_err = (int64_t)tm - tm_fit;
    if (tm_err < 0) {
        tm_err = -tm_err;
    }
    uint32_t tprev = find_cross(c, c->M + c->dir * (n - 1), t1, tN);
    int64_t tprev_fit = c->last + (n - 1) * I1 + add * ((n - 1) * (n - 2) / 2);
    int64_t tprev_err = (int64_t)tprev - tprev_fit;
    if (tprev_err < 0) {
        tprev_err = -tprev_err;
    }
    int64_t first_space = c->rest_first ? I1 + c->carry : I1;
    if (resid < 0 || t1_err > c->err_bound ||
        tm_err > c->err_bound || tprev_err > c->err_bound ||
        first_space < c->min_iv ||
        (add < 0 && I1 + (n - 2) * add < c->min_iv) ||
        final_iv < c->min_iv) {
        int rc = emit_run(c, m, tm, depth + 1);
        if (rc) {
            return rc;
        }
        return emit_run(c, n - m, hi, depth + 1);
    }

    if (I1 > 0xFFFFFFFFLL || final_iv > 0xFFFFFFFFLL) {
        return KM_STEP_SEGMENTS_E_RANGE;
    }

    if (c->nseg + (resid ? 2 : 1) > c->max_seg) {
        return KM_STEP_SEGMENTS_E_RANGE;
    }
    uint8_t sd = (uint8_t)(c->dir > 0);
    if (resid) {
        c->seg[c->nseg++] = (km_step_segment_t){(uint32_t)I1, (uint16_t)(n - 1), (int16_t)add, sd};
        c->seg[c->nseg++] = (km_step_segment_t){(uint32_t)final_iv, 1, 0, sd};
    } else {
        c->seg[c->nseg++] = (km_step_segment_t){(uint32_t)I1, (uint16_t)n, (int16_t)add, sd};
    }
    c->last = tN;
    c->M += c->dir * n;
    c->rest_first = 0;
    c->stepped = 1;
    return KM_STEP_SEGMENTS_OK;
}

/* Time from the last step (this tick's, or the carried one) to the end of the
 * tick.  It saturates at 0x7fffffff: a long idle neither wraps the anchor nor
 * makes the caller see a short time since the last step. */
static uint32_t anchor_out(const seg_ctx_t *c)
{
    int64_t since = c->stepped ? (int64_t)c->T - c->last
                               : c->carry + (int64_t)c->T;
    if (since > 0x7fffffffLL) {
        since = 0x7fffffffLL;
    }
    return (uint32_t)since;
}

int km_step_segments_generate(
    const km_rate_t *start,
    const km_rate_t *end,
    uint32_t tick_us,
    uint32_t ticks_per_us,
    uint32_t min_interval_ticks,
    bool restart,
    int64_t *driver_microstep,
    uint32_t *anchor_ticks,
    km_step_segment_t *segments,
    uint8_t *num_segments,
    uint8_t max_segments
) {
    if (ticks_per_us != 0 && tick_us > 0xFFFFFFFFu / ticks_per_us) {
        return KM_STEP_SEGMENTS_E_RANGE;
    }
    uint32_t T = tick_us * ticks_per_us;

    seg_ctx_t c;
    /* Work relative to the tick's starting whole microstep.  The absolute
     * phase can reach the rate core's +-1e12 microstep limits, where its Q32
     * form would overflow int64; a tick moves at most a few thousand steps, so
     * the relative values fit comfortably. */
    int64_t base = start->phase_whole;
    c.p0 = (int64_t)start->phase_frac;
    int64_t p1 = (end->phase_whole - base) * 4294967296LL + (int64_t)end->phase_frac;
    c.Vq = start->rate * (int64_t)tick_us;
    c.Aq = ((start->accel * (int64_t)tick_us) >> 17) * (int64_t)tick_us;
    c.Jq = p1 - c.p0 - c.Vq - c.Aq;
    c.T = T;
    c.inv_T = T ? 0xFFFFFFFFu / T : 0;
    c.dir = 1;
    c.M = *driver_microstep - base;
    c.carry = (int64_t)(*anchor_ticks);
    /* On a restart the stepper takes the tick start as its start clock, so the
     * first interval is measured from there; the anchor only bounds the real
     * spacing through carry. */
    c.last = restart ? 0 : -(int64_t)(*anchor_ticks);
    c.rest_first = restart ? 1 : 0;
    c.stepped = 0;
    c.deferred = 0;
    c.min_iv = (int64_t)min_interval_ticks;
    c.err_bound = ((int64_t)KM_STEP_SEGMENTS_MAX_ERROR_US * ticks_per_us - 1) / 2;
    if (c.err_bound < 1) {
        c.err_bound = 1;
    }
    c.seg = segments;
    c.nseg = 0;
    c.max_seg = max_segments;

    if (T == 0) {
        *driver_microstep = c.M + base;
        *anchor_ticks = anchor_out(&c);
        *num_segments = 0;
        return KM_STEP_SEGMENTS_OK;
    }

    /* Piece edges: 0, the zeros of v(t) in (0, T), and T. */
    uint32_t pieces[4];
    int np = 0;
    pieces[np++] = 0;
    {
        uint32_t crit[3];
        int nc = 0;
        crit[nc++] = 0;
        if (c.Jq != 0) {
            int g0 = gsign_at(&c, 0);
            int gt = gsign_at(&c, T);
            if (g0 != 0 && gt != 0 && g0 != gt) {
                uint32_t lo = 0, hi = T;
                while (hi - lo > 1) {
                    uint32_t mid = lo + (hi - lo) / 2;
                    if (gsign_at(&c, mid) == g0) {
                        lo = mid;
                    } else {
                        hi = mid;
                    }
                }
                if (hi > 0 && hi < T) {
                    crit[nc++] = hi;
                }
            }
        }
        crit[nc++] = T;
        int sp = vsign_at(&c, crit[0]);
        for (int i = 1; i < nc; i++) {
            int sc = vsign_at(&c, crit[i]);
            if (sp != 0 && sc != 0 && sp != sc) {
                uint32_t lo = crit[i - 1], hi = crit[i];
                while (hi - lo > 1) {
                    uint32_t mid = lo + (hi - lo) / 2;
                    if (vsign_at(&c, mid) == sp) {
                        lo = mid;
                    } else {
                        hi = mid;
                    }
                }
                if (hi > pieces[np - 1] && hi < T) {
                    pieces[np++] = hi;
                }
            }
            sp = sc;
        }
    }
    pieces[np++] = T;

    uint32_t W = KM_STEP_SEGMENTS_WINDOW_US * ticks_per_us;
    int64_t p_lo = c.p0;
    for (int k = 0; k + 1 < np; k++) {
        uint32_t a = pieces[k];
        uint32_t b = pieces[k + 1];
        if (b <= a) {
            continue;
        }
        int64_t p_hi = (b == T) ? p1 : p_at(&c, b);
        int dir = (p_hi > p_lo) ? 1 : ((p_hi < p_lo) ? -1 : 0);
        if (dir != 0) {
            c.dir = dir;
            /* At a zero of v(t) the piece ends on an extreme, where the
             * evaluated phase is only good to a few thousand LSB: require
             * the extreme to clear a level by the guard before counting it. */
            int64_t guard = (k < np - 2) ? KM_STEP_SEGMENTS_TURN_GUARD : 0;
            int64_t remaining = (dir > 0) ? floor_q32(p_hi - guard) - c.M
                                          : c.M - ceil_q32(p_hi + guard);
            if (remaining < 0) {
                remaining = 0;
            }
            uint32_t cur = a;
            while (remaining > 0 && cur < b) {
                uint32_t nxt = b;
                if (W > 0) {
                    uint32_t m = cur / W * W + W;
                    if (m > cur && m < b) {
                        nxt = m;
                    }
                }
                int64_t p_nxt = (nxt == b) ? p_hi : p_at(&c, nxt);
                int64_t avail = (dir > 0) ? floor_q32(p_nxt) - c.M
                                          : c.M - ceil_q32(p_nxt);
                if (avail < 0) {
                    avail = 0;
                }
                if (avail > remaining) {
                    avail = remaining;
                }
                if (avail > 0) {
                    int rc = emit_run(&c, avail, nxt, 0);
                    if (rc) {
                        return rc;
                    }
                    remaining -= avail;
                }
                cur = nxt;
            }
        }
        p_lo = p_hi;
    }

    *driver_microstep = c.M + base;
    *anchor_ticks = anchor_out(&c);
    *num_segments = (uint8_t)c.nseg;
    return KM_STEP_SEGMENTS_OK;
}
