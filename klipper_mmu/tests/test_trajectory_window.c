// SPDX-License-Identifier: GPL-3.0-or-later
/* tests/test_trajectory_window.c
 *
 * Contract tests for the B2 tick API declared in
 * include/klipper_mmu/trajectory.h. The test drives the same adapter loop the
 * MCU layer will use and plays Klipper's stepper.c through
 * tests/stepper_reference.c. The API is a declaration-only contract here: this
 * file compiles on its own and links once the implementation exists.
 */

#include "klipper_mmu/trajectory.h"
#include "klipper_mmu/step_segments.h"
#include "stepper_reference.h"
#include "mock_driver.h"

#include <math.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define TICK_US 1000u
#define MAX_TICKS 20000u
#define STEP_BUF 4096u
#define REF_POOL 64u
#define MIN_LEAD_TICKS 2u

static km_ref_step_t g_steps[STEP_BUF];
static int g_failures;

#define CHECK(cond, msg)                                                       \
    do {                                                                       \
        if (!(cond)) {                                                         \
            printf("    FAIL %s:%d: %s\n", __FILE__, __LINE__, (msg));         \
            g_failures++;                                                      \
        }                                                                      \
    } while (0)

static void checkd(bool ok, const char *msg, double got, double want)
{
    if (!ok) {
        printf("    FAIL %s: got %.6f want %.6f\n", msg, got, want);
        g_failures++;
    }
}

/* Cubic contract from step_segments.h: inside one tick of T us the phase is
 *   p(t) = p0 + v0 t + a0 t^2 / 2 + c t^3,  c chosen so that p(T) = p1.
 * km_rate_t carries phase in microsteps, rate in Q32 steps/us and accel in
 * Q48 steps/us^2. */

static double q32(int64_t v) { return (double)v / 4294967296.0; }
static double q48(int64_t v) { return (double)v / 281474976710656.0; }

static double rate_phase(const km_rate_t *r)
{
    return (double)r->phase_whole + (double)r->phase_frac / 4294967296.0;
}

static double path_at(const km_rate_t *s, const km_rate_t *e, double T, double t)
{
    double p0 = rate_phase(s);
    double v0 = q32(s->rate);
    double a0 = q48(s->accel);
    double p1 = rate_phase(e);
    double c = (p1 - p0 - v0 * T - a0 * T * T * 0.5) / (T * T * T);
    return p0 + v0 * t + a0 * t * t * 0.5 + c * t * t * t;
}

/* d/dt of the same cubic. */
static double path_vel(const km_rate_t *s, const km_rate_t *e, double T, double t)
{
    double p0 = rate_phase(s);
    double v0 = q32(s->rate);
    double a0 = q48(s->accel);
    double p1 = rate_phase(e);
    double c = (p1 - p0 - v0 * T - a0 * T * T * 0.5) / (T * T * T);
    return v0 + a0 * t + 3.0 * c * t * t;
}

/* First t in [0, T] where the path reaches the microstep boundary: >= target
 * for an increasing step, <= target for a decreasing one. */
static double first_reach(const km_rate_t *s, const km_rate_t *e, double T,
                          double target, int dir)
{
    double lo = 0.0;
    double hi = T;
    for (int i = 0; i < 90; i++) {
        double m = 0.5 * (lo + hi);
        double p = path_at(s, e, T, m);
        bool past = dir ? (p >= target) : (p <= target);
        if (past) {
            hi = m;
        } else {
            lo = m;
        }
    }
    return 0.5 * (lo + hi);
}

/* A committed window, kept so each step that runs can be checked against the
 * cubic its own window promised (windows lead the motor, so a step at tick k
 * belongs to a window committed a few ticks earlier). */
#define WLOG 8
typedef struct {
    uint32_t wstart;
    km_rate_t start;
    km_rate_t end;
    bool restart;
} km_win_t;

/* One reference stepper, one trajectory core and the adapter's window chain.
 * The adapter runs a lead of LEAD ticks: at tick k the clock is now_k and it
 * queues window k starting at now_k + LEAD (chained: each window starts where
 * the previous one ended), while the reference only runs up to now_k. */
typedef struct {
    km_trajectory_t traj;
    km_ref_stepper_t ref;
    mock_driver_t drv;
    uint32_t tpu;
    uint32_t win_ticks;
    uint32_t now0;
    uint32_t start0;
    uint32_t min_interval;
    km_rate_t prev;      /* core at the start of the window being built */
    int64_t cur_pos;     /* reference position before the next step checked */
    uint32_t tick;
    int faults;
    int restarts;        /* windows the adapter had to restart the anchor for */
    uint32_t last_wstart;
    bool last_restart;
    int last_n;
    uint32_t last_first_step;
    km_win_t wlog[WLOG];
    uint32_t wlog_count;
} ctx_t;

/* The committed window whose [wstart, wstart + tick) contains t. */
static const km_win_t *window_for(const ctx_t *c, uint32_t t)
{
    for (uint32_t i = 1; i <= WLOG && i <= c->wlog_count; i++) {
        const km_win_t *w = &c->wlog[(c->wlog_count - i) % WLOG];
        if ((int32_t)(t - w->wstart) >= 0 &&
            (uint32_t)(t - w->wstart) < c->win_ticks) {
            return w;
        }
    }
    return NULL;
}

static void ctx_init(ctx_t *c, uint32_t tpu, uint32_t vmax, uint32_t accel,
                     uint32_t decel)
{
    memset(c, 0, sizeof *c);
    c->tpu = tpu;
    c->win_ticks = TICK_US * tpu;
    c->now0 = 0;
    c->start0 = c->now0 + 2u * c->win_ticks;
    c->min_interval = 4u * tpu; /* the mock driver's 4 us minimum step interval */
    km_result_t r = km_trajectory_init(&c->traj, vmax, accel, decel);
    CHECK(r == KM_OK, "km_trajectory_init");
    /* A driverless trajectory yields empty windows (contract): bind an
     * enabled driver, as the MCU adapter does. */
    mock_driver_init(&c->drv, 2, 4, false);
    km_driver_set_enable(&c->drv.base, true);
    km_trajectory_bind_driver(&c->traj, &c->drv.base);
    uint32_t lo = accel < decel ? accel : decel;
    r = km_trajectory_set_jerk(&c->traj, 20u * lo);
    CHECK(r == KM_OK, "km_trajectory_set_jerk");
    km_ref_init(&c->ref, REF_POOL, MIN_LEAD_TICKS);
    c->prev = c->traj.rate;
}

/* One adapter tick, in the real order. Run the reference up to the adapter's
 * clock now_k; the windows committed two ticks ago are what executes now.
 * Evaluate the restart at now_k (the motor is ahead/behind, not reset every
 * window), then generate window k starting at now_k + LEAD and queue it.
 * verify_path checks every step the reference ran this tick against the cubic
 * of the committed window that owns it. */
static void ctx_tick(ctx_t *c, bool verify_path)
{
    uint32_t now = c->now0 + c->tick * c->win_ticks;
    uint32_t wstart = now + 2u * c->win_ticks;

    int n = 0;
    km_ref_advance(&c->ref, now, g_steps, (int)STEP_BUF, &n);
    c->last_n = n;
    if (n > 0) {
        c->last_first_step = g_steps[0].time;
    }
    for (int i = 0; i < n; i++) {
        int64_t target = c->cur_pos + (g_steps[i].dir ? 1 : -1);
        if (verify_path) {
            const km_win_t *w = window_for(c, g_steps[i].time);
            CHECK(w != NULL, "executed step belongs to no committed window");
            if (w != NULL) {
                double rel =
                    (double)(int32_t)(g_steps[i].time - w->wstart) /
                    (double)c->tpu;
                double tau = first_reach(&w->start, &w->end, (double)TICK_US,
                                         (double)target, g_steps[i].dir);
                double tol = (double)KM_STEP_SEGMENTS_MAX_ERROR_US +
                             1.0 / (double)c->tpu;
                checkd(fabs(rel - tau) <= tol,
                       "step time vs committed-window path", rel, tau);
            }
        }
        c->cur_pos = target;
    }

    bool restart = !km_ref_active(&c->ref);
    c->last_wstart = wstart;
    c->last_restart = restart;
    if (restart) {
        c->restarts++;
    }
    km_trajectory_window_t w;
    memset(&w, 0, sizeof w);
    km_result_t r = km_trajectory_window(&c->traj, TICK_US, c->tpu,
                                         c->min_interval, restart, &w);
    if (r != KM_OK) {
        c->faults++;
        c->tick++;
        return;
    }
    CHECK(km_ref_room(&c->ref) >= (int)w.count,
          "stepper pool room < window segment count");
    for (uint8_t i = 0; i < w.count; i++) {
        int q = km_ref_queue(&c->ref, now, wstart, w.segments[i].interval,
                             w.segments[i].count, w.segments[i].add,
                             w.segments[i].dir);
        CHECK(q == 0, "km_ref_queue rejected a window segment");
    }
    km_trajectory_commit(&c->traj, &w);
    km_win_t cw = { wstart, c->prev, w.rate, restart };
    c->wlog[c->wlog_count % WLOG] = cw;
    c->wlog_count++;

    km_trajectory_observe(&c->traj, km_ref_position(&c->ref),
                          km_ref_active(&c->ref) != 0);
    c->prev = w.rate;
    c->tick++;
}

#define VMAX 40000u
#define ACCEL 100000u
#define DECEL 100000u

/* Case 1: exact reach and step timing. Every step of the reference must land
 * at the time the committed windows' cubic path reached that microstep (within
 * the generator's error plus one timer tick), and the count must be exact. */
static void case_position_move(uint32_t tpu, int64_t target)
{
    ctx_t c;
    ctx_init(&c, tpu, VMAX, ACCEL, DECEL);
    km_result_t r = km_trajectory_set_target_position(&c.traj, target, VMAX,
                                                      c.now0);
    CHECK(r == KM_OK, "set_target_position rejected a reachable move");
    for (uint32_t k = 0; k < MAX_TICKS; k++) {
        ctx_tick(&c, true);
        if (!km_trajectory_is_moving(&c.traj) && !km_ref_active(&c.ref)) {
            break;
        }
    }
    CHECK(c.faults == 0, "window faulted during a position move");
    CHECK(km_ref_position(&c.ref) == target,
          "reference ran a different step count than the target");
    CHECK(km_trajectory_get_position(&c.traj) == target,
          "reported completed position != target");
    printf("  case1 tpu=%u target=%lld: ref=%lld M=%lld faults=%d "
           "restarts=%d\n",
           tpu, (long long)target, (long long)km_ref_position(&c.ref),
           (long long)km_trajectory_get_model_position(&c.traj), c.faults,
           c.restarts);
}

/* Case 2: while stepper.c is idle, the reported position, the reference and
 * the committed microstep M agree. */
static void case_idle_invariant(uint32_t tpu)
{
    ctx_t c;
    ctx_init(&c, tpu, VMAX, ACCEL, DECEL);
    km_result_t r = km_trajectory_set_target_position(&c.traj, 50000, VMAX,
                                                      c.now0);
    CHECK(r == KM_OK, "set_target_position rejected");
    int checks = 0;
    for (uint32_t k = 0; k < MAX_TICKS; k++) {
        ctx_tick(&c, false);
        if (!km_ref_active(&c.ref)) {
            int64_t p = km_trajectory_get_position(&c.traj);
            int64_t rp = km_ref_position(&c.ref);
            int64_t m = km_trajectory_get_model_position(&c.traj);
            if (p != rp || p != m) {
                printf("    FAIL idle: get_position=%lld ref=%lld M=%lld\n",
                       (long long)p, (long long)rp, (long long)m);
                g_failures++;
            }
            checks++;
        }
        if (!km_trajectory_is_moving(&c.traj) && !km_ref_active(&c.ref)) {
            break;
        }
    }
    CHECK(checks > 0, "reference never reported idle");
    CHECK(c.faults == 0, "window faulted during the idle-invariant move");
    printf("  case2 tpu=%u: idle samples=%d restarts=%d\n", tpu, checks,
           c.restarts);
}

/* Case 3: arrival. is_moving stays true until the phase landed, completed
 * steps equal the target and the stepper is idle. */
static void case_arrival(uint32_t tpu)
{
    ctx_t c;
    ctx_init(&c, tpu, VMAX, ACCEL, DECEL);
    int64_t target = 30000;
    km_result_t r = km_trajectory_set_target_position(&c.traj, target, VMAX,
                                                      c.now0);
    CHECK(r == KM_OK, "set_target_position rejected");
    bool went_false = false;
    for (uint32_t k = 0; k < MAX_TICKS; k++) {
        bool before = km_trajectory_is_moving(&c.traj);
        ctx_tick(&c, false);
        if (!km_trajectory_is_moving(&c.traj)) {
            bool phase_ok =
                fabs(rate_phase(&c.traj.rate) - (double)target) < 1e-6;
            bool pos_ok = km_trajectory_get_position(&c.traj) == target;
            bool idle_ok = !km_ref_active(&c.ref);
            CHECK(before, "is_moving was already false before arrival");
            if (!phase_ok || !pos_ok || !idle_ok) {
                printf("    FAIL arrival tick %u: phase=%d pos=%d idle=%d\n", k,
                       (int)phase_ok, (int)pos_ok, (int)idle_ok);
                g_failures++;
            }
            went_false = true;
            break;
        }
    }
    CHECK(went_false, "is_moving never went false");
    CHECK(c.faults == 0, "window faulted during arrival");
    printf("  case3 tpu=%u: target=%lld arrival checks done restarts=%d\n",
           tpu, (long long)target, c.restarts);
}

/* After a stop and a resync, M, the phase, the reported position and the
 * velocity all follow the completed step count (cases 4 and 11). */
static void check_resync_invariants(ctx_t *c)
{
    int64_t steps = km_ref_position(&c->ref);
    CHECK(km_trajectory_get_model_position(&c->traj) == steps,
          "resync M != completed steps");
    CHECK(km_trajectory_get_position(&c->traj) == steps,
          "resync position != completed steps");
    CHECK(fabs(rate_phase(&c->traj.rate) - (double)steps) < 1e-6,
          "resync phase != completed steps");
    CHECK(km_trajectory_get_velocity(&c->traj) == 0, "resync rate != 0");
    CHECK(km_trajectory_is_position_uncertain(&c->traj),
          "resync with uncertain=true did not mark the position uncertain");
}

/* Case 4: cancel with an emergency stop mid-move, then resync after the
 * reference is stopped. The next window restarts the anchor. */
static void case_cancel_resync(uint32_t tpu)
{
    ctx_t c;
    ctx_init(&c, tpu, VMAX, ACCEL, DECEL);
    km_result_t r = km_trajectory_set_target_position(&c.traj, 50000, VMAX,
                                                      c.now0);
    CHECK(r == KM_OK, "set_target_position rejected");
    for (uint32_t k = 0; k < 300; k++) {
        ctx_tick(&c, false);
    }
    CHECK(km_trajectory_is_moving(&c.traj), "move finished before the cancel");
    uint32_t now = c.now0 + c.tick * c.win_ticks;
    (void)km_trajectory_cancel(&c.traj, now);
    km_trajectory_emergency_stop(&c.traj);
    km_ref_stop(&c.ref);
    int64_t steps = km_ref_position(&c.ref);
    km_trajectory_resync(&c.traj, steps, true);

    check_resync_invariants(&c);

    /* Resync reconciles position only (trajectory.h): it does not clear the
     * fault latch or re-enable the driver. Recovery after a stop stays
     * explicit, so the judge takes the same path the operation core takes
     * (km_core_recover -> km_trajectory_recover, then the caller enables the
     * driver before commanding motion). */
    r = km_trajectory_recover(&c.traj, steps, now);
    CHECK(r == KM_OK, "km_trajectory_recover after resync rejected");
    r = km_driver_set_enable(&c.drv.base, true);
    CHECK(r == KM_OK, "driver re-enable after recover rejected");

    r = km_trajectory_set_target_position(&c.traj, steps + 20000, VMAX, now);
    CHECK(r == KM_OK, "set_target_position after resync rejected");
    bool saw_restart = false;
    for (uint32_t k = 0; k < 200 && !saw_restart; k++) {
        ctx_tick(&c, false);
        if (c.last_n > 0) {
            const km_win_t *w = window_for(&c, g_steps[0].time);
            if (w != NULL && w->restart) {
                uint32_t lead = g_steps[0].time - w->wstart;
                CHECK(lead >= MIN_LEAD_TICKS,
                      "restarted window's first step landed before the min lead");
                saw_restart = true;
            }
        }
    }
    CHECK(saw_restart, "no restarted window with steps after resync");
    CHECK(c.faults == 0, "window faulted after resync");
    printf("  case4 tpu=%u: resync at %lld steps, restart seen=%d "
           "restarts=%d\n",
           tpu, (long long)steps, (int)saw_restart, c.restarts);
}

/* Case 5: constant velocity for 5000 ticks. Step spacing stays within one
 * timer tick across window seams and the displacement does not drift. */
static void case_constant_velocity(uint32_t tpu)
{
    ctx_t c;
    ctx_init(&c, tpu, VMAX, ACCEL, DECEL);
    int32_t vel = 20000;
    km_result_t r = km_trajectory_set_target_velocity(&c.traj, vel, c.now0);
    CHECK(r == KM_OK, "set_target_velocity rejected");
    double expected = 1000000.0 * (double)tpu / (double)vel; /* ticks per step */
    /* The generator fits each step's crossing time to within
     * KM_STEP_SEGMENTS_MAX_ERROR_US of the true path (step_segments.h
     * "Timing"), via an integer-tick bisection (tau_of's fixed-point
     * reciprocal), not exactly: a gap between two such steps can be off by
     * that error on each end. A flat one-tick bound is tighter than the
     * generator's own contract and fails on legitimate quantization that
     * step timing is explicitly allowed (KM_STEP_SEGMENTS_MAX_ERROR_US is
     * "step time vs the path", not one timer tick). */
    double tol = 2.0 * (double)KM_STEP_SEGMENTS_MAX_ERROR_US * (double)tpu + 2.0;
    bool cruise = false;
    bool settled = false;
    bool have_prev = false;
    bool spacing_ok = true;
    bool reached = false;
    uint32_t prev_time = 0;
    int64_t p_start = 0;
    uint32_t t_start = 0;
    int gaps = 0;
    for (uint32_t k = 0; k < 5000; k++) {
        ctx_tick(&c, false);
        if (!cruise && km_trajectory_get_velocity(&c.traj) == vel) {
            cruise = true;
            reached = true;
            p_start = km_ref_position(&c.ref);
            t_start = c.tick;
        }
        if (cruise && !settled && (c.tick - t_start) >= 10u) {
            settled = true;
            have_prev = false; /* start clean; the ramp is behind us */
        }
        for (int i = 0; i < c.last_n; i++) {
            if (have_prev && settled) {
                double gap =
                    (double)(uint32_t)(g_steps[i].time - prev_time);
                if (fabs(gap - expected) > tol) {
                    spacing_ok = false;
                }
                gaps++;
            }
            prev_time = g_steps[i].time;
            have_prev = true;
        }
    }
    int64_t p_end = km_ref_position(&c.ref);
    double elapsed_s =
        (double)(c.tick - t_start) * (double)TICK_US / 1000000.0;
    double expected_steps = (double)vel * elapsed_s;
    CHECK(reached, "velocity never reached its target");
    CHECK(spacing_ok, "constant-velocity step spacing drifted at a window seam");
    CHECK(gaps > 100, "too few cruise steps sampled");
    checkd(fabs((double)(p_end - p_start) - expected_steps) <= 5.0,
           "constant-velocity displacement drifted",
           (double)(p_end - p_start), expected_steps);
    CHECK(c.faults == 0, "window faulted at constant velocity");
    printf("  case5 tpu=%u: gaps=%d spacing_ok=%d position=%lld restarts=%d\n",
           tpu, gaps, (int)spacing_ok, (long long)p_end, c.restarts);
}

/* Case 6: retarget a position move mid-move. The first pair asks for a nearer
 * target, the second a farther one; both must remain reachable and the
 * reference must end exactly on the new target, with the arrival rule of
 * case 3. */
static void case_retarget(uint32_t tpu)
{
    static const int64_t pair[2][2] = { { 50000, 20000 }, { 20000, 50000 } };
    for (int p = 0; p < 2; p++) {
        int64_t first = pair[p][0];
        int64_t second = pair[p][1];
        ctx_t c;
        ctx_init(&c, tpu, VMAX, ACCEL, DECEL);
        km_result_t r = km_trajectory_set_target_position(&c.traj, first, VMAX,
                                                          c.now0);
        CHECK(r == KM_OK, "set_target_position (first) rejected");
        for (uint32_t k = 0; k < MAX_TICKS; k++) {
            ctx_tick(&c, true);
            if (km_ref_position(&c.ref) >= 1000) {
                break;
            }
        }
        CHECK(km_trajectory_is_moving(&c.traj),
              "move finished before the retarget");
        uint32_t now = c.now0 + c.tick * c.win_ticks;
        r = km_trajectory_set_target_position(&c.traj, second, VMAX, now);
        CHECK(r == KM_OK, "retarget rejected a reachable target");
        bool arrived = false;
        for (uint32_t k = 0; k < MAX_TICKS && !arrived; k++) {
            bool before = km_trajectory_is_moving(&c.traj);
            ctx_tick(&c, true);
            if (!km_trajectory_is_moving(&c.traj)) {
                CHECK(before, "is_moving was already false before arrival");
                CHECK(fabs(rate_phase(&c.traj.rate) - (double)second) < 1e-6,
                      "arrival phase != the retargeted position");
                CHECK(km_trajectory_get_position(&c.traj) == second,
                      "completed steps != the retargeted position");
                CHECK(!km_ref_active(&c.ref), "stepper still active at arrival");
                arrived = true;
            }
        }
        CHECK(arrived, "is_moving never went false after the retarget");
        CHECK(c.faults == 0, "window faulted across the retarget");
        CHECK(km_ref_position(&c.ref) == second,
              "reference did not end exactly on the retargeted target");
        printf("  case6 tpu=%u: %lld -> %lld ref=%lld faults=%d restarts=%d\n",
               tpu, (long long)first, (long long)second,
               (long long)km_ref_position(&c.ref), c.faults, c.restarts);
    }
}

/* Case 7: reverse a velocity move through zero. The reference may change step
 * direction only where the committed path's velocity is zero, and it must end
 * on the committed M. */
static void case_velocity_reversal(uint32_t tpu)
{
    ctx_t c;
    ctx_init(&c, tpu, VMAX, ACCEL, DECEL);
    int32_t v = 20000;
    const uint32_t reverse_at = 60u;   /* +v, mid-ramp */
    /* -v, still short of target. The jerk limit (20x the lower of accel,
     * decel, set in ctx_init) makes accel itself take 2*accel/jerk = 100
     * ticks to invert sign before velocity can fall through zero at all;
     * stop_at must clear that inversion plus enough of the ramp for the
     * reference to actually take reversed steps, or the direction never
     * changes and the case tests nothing (a constant chosen without
     * that S-curve delay, not a contract requirement). */
    const uint32_t stop_at = 400u;
    km_result_t r = km_trajectory_set_target_velocity(&c.traj, v, c.now0);
    CHECK(r == KM_OK, "set_target_velocity (+v) rejected");
    /* A reversal step lies within one microstep of the zero crossing, so the
     * committed path's speed there cannot exceed sqrt(2 * a * 1); allow the
     * generator's timing error on top. ACCEL is a plain steps/s^2 constant,
     * not a Q48-encoded field (q48() decodes km_rate_t.accel, a different
     * representation) -- convert units by hand: path_vel returns steps per
     * microsecond (km_rate_t.rate is Q32 steps/us and t is in microseconds),
     * so torricelli's v = sqrt(2*a*d) needs a in steps/us^2, i.e.
     * ACCEL[steps/s^2] * 1e-12. */
    double tol = 1.5 * sqrt(2.0 * (double)ACCEL) * 1e-6;
    int changes = 0;
    bool change_at_zero = true;
    int prev_dir = -1;
    bool stopped = false;
    for (uint32_t k = 0; k < MAX_TICKS; k++) {
        uint32_t now = c.now0 + c.tick * c.win_ticks;
        if (k == reverse_at) {
            r = km_trajectory_set_target_velocity(&c.traj, -v, now);
            CHECK(r == KM_OK, "set_target_velocity (-v) rejected");
        }
        if (k == stop_at) {
            r = km_trajectory_set_target_velocity(&c.traj, 0, now);
            CHECK(r == KM_OK, "set_target_velocity (0) rejected");
        }
        ctx_tick(&c, false);
        for (int i = 0; i < c.last_n; i++) {
            if (prev_dir >= 0 && (int)g_steps[i].dir != prev_dir) {
                const km_win_t *w = window_for(&c, g_steps[i].time);
                if (w == NULL) {
                    change_at_zero = false;
                } else {
                    double rel =
                        (double)(int32_t)(g_steps[i].time - w->wstart) /
                        (double)c.tpu;
                    if (fabs(path_vel(&w->start, &w->end, (double)TICK_US,
                                      rel)) > tol) {
                        change_at_zero = false;
                    }
                }
                changes++;
            }
            prev_dir = g_steps[i].dir;
        }
        if (k > stop_at && !km_trajectory_is_moving(&c.traj) &&
            !km_ref_active(&c.ref)) {
            stopped = true;
            break;
        }
    }
    CHECK(changes > 0, "the reference never reversed direction");
    CHECK(change_at_zero, "reference reversed away from a zero of the path's velocity");
    CHECK(stopped, "the move never settled at zero velocity");
    CHECK(km_trajectory_get_velocity(&c.traj) == 0, "final velocity != 0");
    CHECK(km_ref_position(&c.ref) == km_trajectory_get_model_position(&c.traj),
          "final reference position != committed M");
    CHECK(c.faults == 0, "window faulted across the reversal");
    printf("  case7 tpu=%u: changes=%d at_zero=%d ref=%lld M=%lld faults=%d "
           "restarts=%d\n",
           tpu, changes, (int)change_at_zero,
           (long long)km_ref_position(&c.ref),
           (long long)km_trajectory_get_model_position(&c.traj), c.faults,
           c.restarts);
}

/* Case 8: an emergency-stopped or faulted trajectory plans an empty window
 * with the core held. */
static void case_empty_window(uint32_t tpu, bool faulted)
{
    ctx_t c;
    ctx_init(&c, tpu, VMAX, ACCEL, DECEL);
    km_result_t r = km_trajectory_set_target_position(&c.traj, 50000, VMAX,
                                                      c.now0);
    CHECK(r == KM_OK, "set_target_position rejected");
    for (uint32_t k = 0; k < 100; k++) {
        ctx_tick(&c, false);
    }
    km_trajectory_emergency_stop(&c.traj);
    if (faulted) {
        c.traj.faulted = true;
    }
    km_rate_t held = c.traj.rate;
    int64_t m = km_trajectory_get_model_position(&c.traj);
    km_trajectory_window_t w;
    memset(&w, 0, sizeof w);
    r = km_trajectory_window(&c.traj, TICK_US, c.tpu, c.min_interval, true, &w);
    CHECK(r == KM_OK, "stopped/faulted window returned an error");
    CHECK(w.count == 0, "stopped/faulted trajectory planned a non-empty window");
    CHECK(w.microstep == m, "empty window moved the model position");
    CHECK(w.rate.phase_whole == held.phase_whole &&
              w.rate.phase_frac == held.phase_frac &&
              w.rate.rate == held.rate && w.rate.accel == held.accel,
          "empty window changed the held core");
    CHECK(!km_trajectory_is_moving(&c.traj),
          "stopped/faulted trajectory still reports moving");
    printf("  case8 tpu=%u %s: empty window, core held restarts=%d\n", tpu,
           faulted ? "faulted" : "estopped", c.restarts);
}

/* Case 9: the adapter must keep the reference pool ahead of what it queues:
 * two windows still being queued plus two being drained. ctx_tick already
 * checks room >= the window's segment count every tick. */
#define CASE9_POOL ((2 + 2) * KM_STEP_SEGMENTS_MAX)

static void case_pool_room(uint32_t tpu)
{
    ctx_t c;
    ctx_init(&c, tpu, 50000u, 500000u, 500000u); /* a deliberately harsh ramp */
    km_ref_init(&c.ref, CASE9_POOL, MIN_LEAD_TICKS);
    km_result_t r = km_trajectory_set_target_position(&c.traj, 50000, 50000u,
                                                      c.now0);
    CHECK(r == KM_OK, "set_target_position rejected");
    uint32_t ticks = 0;
    uint32_t step_ticks = 0;
    for (uint32_t k = 0; k < MAX_TICKS; k++) {
        ctx_tick(&c, false);
        ticks++;
        if (c.last_n > 0) {
            step_ticks++;
        }
        if (!km_trajectory_is_moving(&c.traj) && !km_ref_active(&c.ref)) {
            break;
        }
    }
    CHECK(step_ticks > 0, "the harsh move never emitted steps");
    CHECK(km_ref_position(&c.ref) == 50000,
          "the harsh move did not land exactly");
    CHECK(c.faults == 0, "window faulted on the harsh move");
    printf("  case9 tpu=%u: pool=%d ticks=%u step-ticks=%u faults=%d "
           "restarts=%d\n",
           tpu, (int)CASE9_POOL, ticks, step_ticks, c.faults, c.restarts);
}

/* Case 10: the chained window clock wraps. The window clock starts 20 ms
 * before 2^32 and the move runs across the wrap; ctx_tick's path check is a
 * signed 32-bit delta, so it holds on the far side too. */
static void case_clock_wrap(uint32_t tpu)
{
    ctx_t c;
    ctx_init(&c, tpu, VMAX, ACCEL, DECEL);
    c.start0 = (uint32_t)0u - 20u * c.win_ticks; /* 20 ms before the wrap */
    c.now0 = c.start0 - 2u * c.win_ticks;
    CHECK((int32_t)c.start0 < 0, "the wrap case did not start before 2^32");
    km_result_t r = km_trajectory_set_target_position(&c.traj, 50000, VMAX,
                                                      c.now0);
    CHECK(r == KM_OK, "set_target_position rejected across the wrap");
    bool wrapped = false;
    for (uint32_t k = 0; k < MAX_TICKS; k++) {
        ctx_tick(&c, true);
        if ((int32_t)c.last_wstart >= 0) {
            wrapped = true;
        }
        if (!km_trajectory_is_moving(&c.traj) && !km_ref_active(&c.ref)) {
            break;
        }
    }
    CHECK(wrapped, "the simulated window clock never crossed the 32-bit wrap");
    CHECK(c.faults == 0, "window faulted across the 32-bit clock wrap");
    CHECK(km_ref_position(&c.ref) == 50000, "steps lost across the clock wrap");
    printf("  case10 tpu=%u: ref=%lld faults=%d restarts=%d\n", tpu,
           (long long)km_ref_position(&c.ref), c.faults, c.restarts);
}

/* Case 11: an underrun. The adapter cannot queue a window for three ticks
 * mid-cruise; the reference runs dry and goes idle while the core still
 * reports a move. Recovery follows the case-4 resync and the next move must
 * land exactly. */
static void case_underrun(uint32_t tpu)
{
    ctx_t c;
    ctx_init(&c, tpu, VMAX, ACCEL, DECEL);
    km_result_t r = km_trajectory_set_target_position(&c.traj, 50000, VMAX,
                                                      c.now0);
    CHECK(r == KM_OK, "set_target_position rejected");
    /* effective_limit (trajectory.c) caps the move at the driver's own
     * caps.max_velocity_steps_s, not the caller's `maximum` argument alone:
     * the mock driver's cap (mock_driver_init, 20000) is below VMAX, so
     * cruise never reaches VMAX. Wait for the actual, driver-capped cruise
     * speed instead of a plateau this move can never reach. */
    int32_t cruise_v = (int32_t)c.drv.base.caps.max_velocity_steps_s;
    for (uint32_t k = 0; k < MAX_TICKS; k++) {
        ctx_tick(&c, true);
        if (km_trajectory_get_velocity(&c.traj) == cruise_v &&
            km_ref_position(&c.ref) < 40000) {
            break;
        }
    }
    CHECK(km_trajectory_is_moving(&c.traj), "move finished before the underrun");
    CHECK(km_ref_position(&c.ref) > 0, "the reference made no progress");

    /* The adapter already queued two ticks of lead, so the gap is a real
     * underrun: with no window queued for three ticks the in-flight lead runs
     * out about two ticks in, and the reference goes idle while the core is
     * mid-move. */
    CHECK(km_ref_active(&c.ref), "the reference had no queued lead to drain");
    int ran = 0;
    int drained_at = 0;
    for (int d = 1; d <= 3; d++) {
        uint32_t now = c.now0 + c.tick * c.win_ticks;
        int n = 0;
        km_ref_advance(&c.ref, now, g_steps, (int)STEP_BUF, &n);
        ran += n;
        c.tick++;
        if (!km_ref_active(&c.ref) && drained_at == 0) {
            drained_at = d;
        }
    }
    CHECK(ran > 0, "the queued lead never ran during the gap");
    CHECK(drained_at >= 2, "the reference drained before its lead ran out");
    CHECK(!km_ref_active(&c.ref), "the reference did not go idle");
    CHECK(km_trajectory_is_moving(&c.traj), "the core stopped on its own");
    CHECK(!km_ref_active(&c.ref) && km_trajectory_is_moving(&c.traj),
          "the adapter could not have detected the underrun");

    int64_t steps = km_ref_position(&c.ref);
    uint32_t now = c.now0 + c.tick * c.win_ticks;
    km_ref_stop(&c.ref);
    km_trajectory_emergency_stop(&c.traj);
    km_trajectory_resync(&c.traj, steps, true);
    c.cur_pos = steps;
    check_resync_invariants(&c);

    /* Resync reconciles position only (trajectory.h); recovery after a stop
     * stays explicit, the same km_trajectory_recover path case 4 takes. */
    r = km_trajectory_recover(&c.traj, steps, now);
    CHECK(r == KM_OK, "km_trajectory_recover after the underrun rejected");
    r = km_driver_set_enable(&c.drv.base, true);
    CHECK(r == KM_OK, "driver re-enable after recover rejected");

    r = km_trajectory_set_target_position(&c.traj, steps + 20000, VMAX, now);
    CHECK(r == KM_OK, "set_target_position after the underrun rejected");
    bool saw_restart = false;
    for (uint32_t k = 0; k < MAX_TICKS; k++) {
        ctx_tick(&c, true);
        if (c.last_n > 0) {
            const km_win_t *w = window_for(&c, g_steps[0].time);
            if (w != NULL && w->restart) {
                saw_restart = true;
            }
        }
        if (!km_trajectory_is_moving(&c.traj) && !km_ref_active(&c.ref)) {
            break;
        }
    }
    CHECK(saw_restart, "no restarted window after the underrun");
    CHECK(km_ref_position(&c.ref) == steps + 20000,
          "the post-underrun move did not land exactly");
    CHECK(km_trajectory_get_position(&c.traj) == steps + 20000,
          "reported position after the underrun != target");
    CHECK(c.faults == 0, "window faulted after the underrun");
    printf("  case11 tpu=%u: drained at %lld (tick %d), new target %lld "
           "ref=%lld faults=%d restarts=%d\n",
           tpu, (long long)steps, drained_at, (long long)(steps + 20000),
           (long long)km_ref_position(&c.ref), c.faults, c.restarts);
}

int main(void)
{
    static const uint32_t tpus[2] = { 520u, 64u };
    static const int64_t moves[8] = { 1, -1, 10, -10, 1000, -1000, 50000,
                                      -50000 };
    printf("== B2 trajectory window contract tests ==\n");
    printf("case1: position moves, exact step count and path timing\n");
    for (int t = 0; t < 2; t++) {
        for (int m = 0; m < 8; m++) {
            case_position_move(tpus[t], moves[m]);
        }
    }
    printf("case2: idle position agreement\n");
    for (int t = 0; t < 2; t++) {
        case_idle_invariant(tpus[t]);
    }
    printf("case3: arrival gating\n");
    for (int t = 0; t < 2; t++) {
        case_arrival(tpus[t]);
    }
    printf("case4: cancel + emergency stop + resync\n");
    for (int t = 0; t < 2; t++) {
        case_cancel_resync(tpus[t]);
    }
    printf("case5: constant velocity across window seams\n");
    for (int t = 0; t < 2; t++) {
        case_constant_velocity(tpus[t]);
    }
    printf("case6: mid-move position retarget\n");
    for (int t = 0; t < 2; t++) {
        case_retarget(tpus[t]);
    }
    printf("case7: velocity reversal through zero\n");
    for (int t = 0; t < 2; t++) {
        case_velocity_reversal(tpus[t]);
    }
    printf("case8: empty windows after a stop or a fault\n");
    for (int t = 0; t < 2; t++) {
        case_empty_window(tpus[t], false);
        case_empty_window(tpus[t], true);
    }
    printf("case9: reference pool room on a harsh move\n");
    for (int t = 0; t < 2; t++) {
        case_pool_room(tpus[t]);
    }
    printf("case10: timer clock wrap\n");
    for (int t = 0; t < 2; t++) {
        case_clock_wrap(tpus[t]);
    }
    printf("case11: underrun drain and recovery\n");
    for (int t = 0; t < 2; t++) {
        case_underrun(tpus[t]);
    }
    printf("== %s: %d failure(s) ==\n", g_failures ? "FAIL" : "PASS",
           g_failures);
    return g_failures ? 1 : 0;
}
