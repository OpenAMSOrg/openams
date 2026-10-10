// SPDX-License-Identifier: GPL-3.0-or-later
#include <time.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include "autoconf.h"
#include "board/io.h"
#include "board/irq.h"
#include "board/misc.h"
#include "command.h"
#include "internal.h"
#include "sched.h"

static struct {
    uint32_t last_read_time;
    time_t start_sec;
    uint32_t must_wake_timers;
    uint32_t next_wake_counter;
    struct timespec next_wake;
    timer_t t_alarm;
    sigset_t ss_alarm, ss_sleep;
} TimerInfo;

static uint32_t current_timer_waketime;
static int in_timer_callback = 0;

static inline uint32_t timespec_to_time(struct timespec ts) {
    uint64_t diff = ts.tv_sec - TimerInfo.start_sec;
    return diff * CONFIG_CLOCK_FREQ + ((uint64_t)ts.tv_nsec * (CONFIG_CLOCK_FREQ / 1000000)) / 1000;
}

static inline struct timespec timespec_from_time(uint32_t time) {
    int32_t counter_diff = time - TimerInfo.next_wake_counter;
    struct timespec ts;
    ts.tv_sec = TimerInfo.next_wake.tv_sec;
    int64_t nsec_diff = (int64_t)counter_diff * 1000000000LL / CONFIG_CLOCK_FREQ;
    ts.tv_nsec = TimerInfo.next_wake.tv_nsec + nsec_diff;
    while (ts.tv_nsec >= 1000000000LL) {
        ts.tv_sec++;
        ts.tv_nsec -= 1000000000LL;
    }
    while (ts.tv_nsec < 0) {
        ts.tv_sec--;
        ts.tv_nsec += 1000000000LL;
    }
    return ts;
}

static struct timespec timespec_read(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts;
}

DECL_CONSTANT("CLOCK_FREQ", CONFIG_CLOCK_FREQ);

int timer_check_periodic(uint32_t *ts) {
    uint32_t lrt = TimerInfo.last_read_time;
    if (timer_is_before(lrt, *ts)) return 0;
    *ts = lrt + timer_from_us(2000000);
    return 1;
}

uint32_t timer_from_us(uint32_t us) {
    return us * (CONFIG_CLOCK_FREQ / 1000000);
}

uint8_t timer_is_before(uint32_t time1, uint32_t time2) {
    return (int32_t)(time1 - time2) < 0;
}

// The real (wall-anchored) clock, always, with the last_read_time
// bookkeeping timer_check_periodic() depends on. Used directly by the
// dispatcher's own lag checks, which must never see virtual time even
// though they run between callback invocations (in_timer_callback is
// already false there, so timer_read_time() would give the same answer;
// this spells out the "always real" contract instead of relying on that).
static uint32_t real_time_now(void) {
    uint32_t t = timespec_to_time(timespec_read());
    TimerInfo.last_read_time = t;
    return t;
}

// timer_dispatch() below runs every timer whose nominal waketime is not yet
// meaningfully ahead of the real clock (TIMER_MIN_TRY_TICKS), in one tight
// loop, without re-reading the real clock between them: on a host under any
// load, a batch of closely spaced timers can finish real-world faster than
// their nominal spacing implied, and two DIFFERENT timers' own nominal
// waketimes are not required to be mutually ordered the way one timer's own
// reschedule chain is. Either way, a caller of timer_read_time() can end up
// observing a value a handful of microseconds *behind* one this function
// already returned to an earlier caller -- a real, if tiny, backward step
// for anything treating timer_read_time() as one clock
// (km_mcu_clock_observe(), src/mcu_clock.c, latches exactly that shape of
// jump as "klipper_mmu: clock fault"). high_water_mark makes every value
// this function returns monotonic non-decreasing against every value it has
// ever returned before, in either context, per the platform contract real
// hardware's timer_read_time() already gives for free. The dispatcher's own
// lag checks below read real_time_now() directly, bypassing this clamp, so
// a genuinely slow host is still reported as a stall, not hidden by it.
static uint32_t high_water_mark;

static uint32_t clamp_monotonic(uint32_t candidate) {
    if (timer_is_before(candidate, high_water_mark))
        return high_water_mark;
    high_water_mark = candidate;
    return candidate;
}

// Test-only: track the worst gaps between successive timer dispatches, in
// both real (wall) and virtual (scheduled waketime) time. sched_timer_dispatch()
// (Klipper's own generic scheduler, not board code) doesn't expose which
// timer it just ran, so this tracks the dispatch loop's own cadence instead
// -- one entry per call, whichever timer that happens to be -- and reports
// the scheduled-time delta ("virtual_gap_us") alongside each worst wall gap:
// a value near 1000us is almost certainly klipper-mmu's KM_CONTROLLER_PERIOD_US
// controller tick (mcu/controller.h), since nothing else in this profile is
// periodic at ~1kHz; a value near a few tens of us is almost certainly a
// feeder STEP pulse. Cheap (one clock_gettime() and a handful of integer
// comparisons per dispatch, no allocation, no per-event output) so it does
// not itself perturb the timing it measures. Dumped to stderr (captured into
// the MCU log by tests/linux_mcu/*.py) at exit or SIGUSR2 -- a different
// signal from sim/board/plant.c's SIGUSR1 so the two test-only diagnostics
// never fight over one handler.
#define KM_SIM_GAP_TRACK_COUNT 5
typedef struct {
    uint64_t wall_gap_ns;
    uint32_t virtual_gap_ticks;
    uint32_t waketime;
    struct timespec at;
} km_sim_gap_t;
static km_sim_gap_t km_sim_worst_gaps[KM_SIM_GAP_TRACK_COUNT];
static struct timespec km_sim_last_dispatch_wall;
static uint32_t km_sim_last_dispatch_waketime;
static int km_sim_have_last_dispatch;
static uint64_t km_sim_dispatch_count;

static void km_sim_note_dispatch_gap(uint32_t waketime) {
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    km_sim_dispatch_count++;
    if (km_sim_have_last_dispatch) {
        uint64_t wall_gap_ns =
            (uint64_t)(now.tv_sec - km_sim_last_dispatch_wall.tv_sec) * 1000000000ULL
            + (int64_t)(now.tv_nsec - km_sim_last_dispatch_wall.tv_nsec);
        if (wall_gap_ns > km_sim_worst_gaps[KM_SIM_GAP_TRACK_COUNT - 1].wall_gap_ns) {
            int i = KM_SIM_GAP_TRACK_COUNT - 1;
            km_sim_worst_gaps[i] = (km_sim_gap_t){
                wall_gap_ns, waketime - km_sim_last_dispatch_waketime, waketime, now};
            while (i > 0 && km_sim_worst_gaps[i].wall_gap_ns
                            > km_sim_worst_gaps[i - 1].wall_gap_ns) {
                km_sim_gap_t tmp = km_sim_worst_gaps[i];
                km_sim_worst_gaps[i] = km_sim_worst_gaps[i - 1];
                km_sim_worst_gaps[i - 1] = tmp;
                i--;
            }
        }
    }
    km_sim_last_dispatch_wall = now;
    km_sim_last_dispatch_waketime = waketime;
    km_sim_have_last_dispatch = 1;
}

static void km_sim_dump_gaps(void) {
    const char *path = getenv("KM_SIM_TIMER_GAPS_OUT");
    if (!path || !*path) return;
    FILE *f = fopen(path, "w");
    if (!f) return;
    fprintf(f, "dispatch_count=%llu\n",
           (unsigned long long)km_sim_dispatch_count);
    for (int i = 0; i < KM_SIM_GAP_TRACK_COUNT; i++) {
        km_sim_gap_t *g = &km_sim_worst_gaps[i];
        if (g->wall_gap_ns == 0) break;
        fprintf(f,
               "rank=%d wall_gap_us=%llu virtual_gap_us=%u waketime=%u"
               " at_sec=%lld at_nsec=%ld\n",
               i, (unsigned long long)(g->wall_gap_ns / 1000u),
               g->virtual_gap_ticks / (CONFIG_CLOCK_FREQ / 1000000u),
               g->waketime, (long long)g->at.tv_sec, g->at.tv_nsec);
    }
    fclose(f);
}

static void km_sim_gap_sigusr2(int signo) {
    (void)signo;
    km_sim_dump_gaps();
}

void km_sim_gap_diag_init(void) {
    const char *path = getenv("KM_SIM_TIMER_GAPS_OUT");
    if (!path || !*path) return;
    atexit(km_sim_dump_gaps);
    struct sigaction sa;
    __builtin_memset(&sa, 0, sizeof(sa));
    sa.sa_handler = km_sim_gap_sigusr2;
    sigaction(SIGUSR2, &sa, NULL);
}
DECL_INIT(km_sim_gap_diag_init);

uint32_t timer_read_time(void) {
    if (in_timer_callback)
        return clamp_monotonic(current_timer_waketime);
    return clamp_monotonic(real_time_now());
}

void timer_kick(void) {
    struct itimerspec it = { .it_interval = {0, 0}, .it_value = {0, 1} };
    timer_settime(TimerInfo.t_alarm, TIMER_ABSTIME, &it, NULL);
}

#define TIMER_IDLE_REPEAT_COUNT 100
#define TIMER_REPEAT_COUNT 20
#define TIMER_MIN_TRY_TICKS timer_from_us(2)

static void timer_dispatch(void) {
    uint32_t repeat_count = TIMER_REPEAT_COUNT, next;
    current_timer_waketime = TimerInfo.next_wake_counter;

    for (;;) {
        km_sim_note_dispatch_gap(current_timer_waketime);
        in_timer_callback = 1;
        next = sched_timer_dispatch();
        in_timer_callback = 0;
        
        current_timer_waketime = next;

        repeat_count--;
        uint32_t lrt = TimerInfo.last_read_time;
        if (!timer_is_before(lrt, next) && repeat_count)
            continue;

        uint32_t now = real_time_now();
        int32_t diff = next - now;
        if (diff > (int32_t)TIMER_MIN_TRY_TICKS)
            break;

        if (unlikely(!repeat_count)) {
            if (diff < (int32_t)(-timer_from_us(100000)))
                try_shutdown("Rescheduled timer in the past");
            if (sched_check_set_tasks_busy())
                return;
            repeat_count = TIMER_IDLE_REPEAT_COUNT;
        }

        while (unlikely(diff > 0)) {
            diff = next - real_time_now();
        }
    }

    struct itimerspec it;
    it.it_interval = (struct timespec){0, 0};
    TimerInfo.next_wake = it.it_value = timespec_from_time(next);
    TimerInfo.next_wake_counter = next;
    TimerInfo.must_wake_timers = 0;
    timer_settime(TimerInfo.t_alarm, TIMER_ABSTIME, &it, NULL);
}

static void timer_signal(int signal) {
    TimerInfo.must_wake_timers = 1;
}

void timer_init(void) {
    sigemptyset(&TimerInfo.ss_alarm);
    sigaddset(&TimerInfo.ss_alarm, SIGALRM);
    sigprocmask(0, NULL, &TimerInfo.ss_sleep);
    sigdelset(&TimerInfo.ss_sleep, SIGALRM);
    
    struct timespec curtime = timespec_read();
    /* Anchor at the current second, not one second ahead: timespec_to_time()
     * computes (ts.tv_sec - start_sec) into an unsigned 64-bit diff, and a
     * "+1" here made that diff negative (huge once unsigned) for the whole
     * first second of process life. That produced a bogus multi-billion-tick
     * value at boot, then an enormous apparent backward jump once real time
     * caught up to start_sec a second later -- exactly the "ambiguous
     * backward sample" km_mcu_clock_observe() (src/mcu_clock.c) latches as
     * "klipper_mmu: clock fault". Phase 1 never queried the clock this early
     * or this often; phase 2's diagnostic_startup does, which is what
     * surfaced it. See docs/plan/linux-mcu-integration.md phase 2. */
    TimerInfo.start_sec = curtime.tv_sec;
    TimerInfo.next_wake = curtime;
    TimerInfo.next_wake_counter = timespec_to_time(curtime);
    
    timer_create(CLOCK_MONOTONIC, NULL, &TimerInfo.t_alarm);
    struct sigaction act = {.sa_handler = timer_signal, .sa_flags = SA_RESTART};
    sigaction(SIGALRM, &act, NULL);
    timer_kick();
}
DECL_INIT(timer_init);

void timer_disable_signals(void) {
    sigprocmask(SIG_BLOCK, &TimerInfo.ss_alarm, NULL);
}

void timer_enable_signals(void) {
    sigprocmask(SIG_UNBLOCK, &TimerInfo.ss_alarm, NULL);
}

void irq_disable(void) {}
void irq_enable(void) {}
irqstatus_t irq_save(void) { return 0; }
void irq_restore(irqstatus_t flag) {}

void irq_wait(void) {
    if (!readl(&TimerInfo.must_wake_timers)) {
        timer_disable_signals();
        if (!TimerInfo.must_wake_timers)
            console_sleep(&TimerInfo.ss_sleep);
        timer_enable_signals();
    }
    irq_poll();
}

void irq_poll(void) {
    if (readl(&TimerInfo.must_wake_timers))
        timer_dispatch();
}
