// SPDX-License-Identifier: GPL-3.0-or-later
// Native tests for the WANT_STEPPER_LOCAL stepper extension.
//
// The patched upstream src/stepper.c is compiled unmodified against the shims
// (a test-controlled clock, a scheduler whose timers the test fires by hand,
// GPIO edge logging, a counted shared move pool, an oid table and a shutdown
// hook).  Everything the local API can observe is asserted through the public
// local API and the recorded pin/timer activity.
//
// Scheduling notes for the pinned upstream stepper_event_full(): a step asserts
// the pin, then a separate event unsteps it after step_pulse_ticks.  The next
// assert is pushed back to the unstep time plus step_pulse_ticks whenever the
// ideal next step time is closer than that.  STEP_INTERVAL is therefore chosen
// comfortably above two pulses so the ideal schedule is never clamped, and a
// step's time is exactly start + interval + k*(interval + add) for the k-th
// step after the first.  The both-edge path (invert_step < 0, SF_SINGLE_SCHED)
// emits one toggle per step with the same step times.

#include <setjmp.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "autoconf.h"
#include "basecmd.h"
#include "board/gpio.h"
#include "board/irq.h"
#include "board/misc.h"
#include "command.h"
#include "sched.h"
#include "stepper.h"
#include "trsync.h"

#define STEP_PULSE timer_from_us(1)   // 520 ticks: well under half the interval
#define STEP_INTERVAL 2000u
#define STEP_ADD 7u

// ------------------------------------------------------------------
// Test-controlled clock
// ------------------------------------------------------------------

static uint32_t test_clock;

uint32_t
timer_read_time(void)
{
    return test_clock;
}

uint32_t
timer_from_us(uint32_t us)
{
    return us * (CONFIG_CLOCK_FREQ / 1000000U);
}

bool
timer_is_before(uint32_t time1, uint32_t time2)
{
    return (int32_t)(time1 - time2) < 0;
}

// ------------------------------------------------------------------
// Interrupts (recorded nesting only; nothing is preempted in the test)
// ------------------------------------------------------------------

static int irq_depth;

void
irq_disable(void)
{
    irq_depth++;
}

void
irq_enable(void)
{
    if (irq_depth)
        irq_depth--;
}

irqstatus_t
irq_save(void)
{
    irq_disable();
    return (irqstatus_t)irq_depth;
}

void
irq_restore(irqstatus_t flag)
{
    (void)flag;
    irq_enable();
}

// ------------------------------------------------------------------
// GPIO edge log
// ------------------------------------------------------------------

#define MAX_GPIO_EVENTS 4096
#define MAX_GPIO_PINS 32

struct gpio_event {
    uint32_t pin;
    uint32_t level;
    uint32_t time;
};

static struct gpio_event gpio_events[MAX_GPIO_EVENTS];
static int gpio_event_count;
static uint32_t gpio_levels[MAX_GPIO_PINS];

static void
gpio_record(uint32_t pin, uint32_t level)
{
    if (gpio_event_count < MAX_GPIO_EVENTS) {
        gpio_events[gpio_event_count].pin = pin;
        gpio_events[gpio_event_count].level = level;
        gpio_events[gpio_event_count].time = test_clock;
        gpio_event_count++;
    }
}

struct gpio_out
gpio_out_setup(uint32_t pin, uint32_t val)
{
    if (pin < MAX_GPIO_PINS)
        gpio_levels[pin] = val ? 1 : 0;
    struct gpio_out g = { pin };
    return g;
}

void
gpio_out_write(struct gpio_out g, uint32_t val)
{
    if (g.pin < MAX_GPIO_PINS)
        gpio_levels[g.pin] = val ? 1 : 0;
    gpio_record(g.pin, val ? 1 : 0);
}

void
gpio_out_reset(struct gpio_out g, uint32_t val)
{
    gpio_out_write(g, val);
}

static void
gpio_flip(struct gpio_out g)
{
    uint32_t level = 1;
    if (g.pin < MAX_GPIO_PINS) {
        level = gpio_levels[g.pin] ? 0 : 1;
        gpio_levels[g.pin] = level;
    }
    gpio_record(g.pin, level);
}

void
gpio_out_toggle_noirq(struct gpio_out g)
{
    gpio_flip(g);
}

void
gpio_out_toggle(struct gpio_out g)
{
    gpio_flip(g);
}

// ------------------------------------------------------------------
// Hand-fired scheduler
// ------------------------------------------------------------------

static struct timer *pending_timers;

void
sched_add_timer(struct timer *t)
{
    for (struct timer *p = pending_timers; p; p = p->next)
        if (p == t)
            return;
    t->next = pending_timers;
    pending_timers = t;
}

void
sched_del_timer(struct timer *del)
{
    struct timer **pp = &pending_timers;
    while (*pp) {
        if (*pp == del) {
            *pp = del->next;
            del->next = NULL;
            return;
        }
        pp = &(*pp)->next;
    }
}

static int
pending_count(void)
{
    int n = 0;
    for (struct timer *p = pending_timers; p; p = p->next)
        n++;
    return n;
}

static int
fire_next(void)
{
    struct timer *best = NULL;
    for (struct timer *p = pending_timers; p; p = p->next)
        if (!best || timer_is_before(p->waketime, best->waketime))
            best = p;
    if (!best)
        return 0;
    sched_del_timer(best);
    test_clock = best->waketime;
    uint_fast8_t res = best->func(best);
    if (res == SF_RESCHEDULE)
        sched_add_timer(best);
    return 1;
}

static void
fire_all(void)
{
    long guard = 200000;
    while (fire_next() && --guard > 0)
        ;
}

// ------------------------------------------------------------------
// Step edge helpers (invert_step 0 emits an assert then an unstep per step;
// the both-edge path emits a single toggle per step)
// ------------------------------------------------------------------

static int
step_edge_count(uint32_t pin, int single)
{
    int n = 0;
    for (int i = 0; i < gpio_event_count; i++) {
        struct gpio_event *e = &gpio_events[i];
        if (e->pin != pin)
            continue;
        if (!single && e->level != 1)
            continue;
        n++;
    }
    return n;
}

static int
step_edges(uint32_t pin, int single, uint32_t *out, int max)
{
    int n = 0;
    for (int i = 0; i < gpio_event_count && n < max; i++) {
        struct gpio_event *e = &gpio_events[i];
        if (e->pin != pin)
            continue;
        if (!single && e->level != 1)
            continue;
        out[n++] = e->time;
    }
    return n;
}

static void
fire_edges(uint32_t pin, int single, int want)
{
    long guard = 200000;
    while (step_edge_count(pin, single) < want && fire_next() && --guard > 0)
        ;
}

static int
gpio_pin_events(uint32_t pin)
{
    int n = 0;
    for (int i = 0; i < gpio_event_count; i++)
        if (gpio_events[i].pin == pin)
            n++;
    return n;
}

static uint32_t
gpio_first_time(uint32_t pin)
{
    for (int i = 0; i < gpio_event_count; i++)
        if (gpio_events[i].pin == pin)
            return gpio_events[i].time;
    return 0;
}

// ------------------------------------------------------------------
// Shutdown hook
// ------------------------------------------------------------------

static jmp_buf shutdown_jmp;
static char shutdown_message[128];
static int shutdown_count;

_Noreturn void
shutdown(const char *msg)
{
    snprintf(shutdown_message, sizeof shutdown_message, "%s", msg);
    shutdown_count++;
    longjmp(shutdown_jmp, 1);
}

void
try_shutdown(const char *msg)
{
    shutdown(msg);
}

// ------------------------------------------------------------------
// sendf capture
// ------------------------------------------------------------------

static char last_sendf[128];

void
test_sendf(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(last_sendf, sizeof last_sendf, fmt, ap);
    va_end(ap);
}

// ------------------------------------------------------------------
// Allocators
// ------------------------------------------------------------------

static unsigned char chunk_store[65536] __attribute__((aligned(16)));
static size_t chunk_used;

// Klipper's move_queue_setup shuts down once the config is finalized, and
// the shared move pool then holds the free memory; a local stepper is set up
// after that, so setup_local sets this flag around it.
static int config_finalized;

void *
alloc_chunk(size_t size)
{
    if (config_finalized) {
        fprintf(stderr, "alloc_chunk after finalize: the shared pool holds the memory\n");
        abort();
    }
    size_t aligned = (size + 15) & ~(size_t)15;
    if (chunk_used + aligned > sizeof chunk_store)
        return NULL;
    void *p = chunk_store + chunk_used;
    chunk_used += aligned;
    memset(p, 0, aligned);
    return p;
}

// ------------------------------------------------------------------
// oid table
// ------------------------------------------------------------------

#define OID_SLOTS 8
#define OID_SLOT_BYTES 192

struct oid_entry {
    uint8_t oid;
    oid_type type;
    void *ptr;
};

static struct oid_entry oid_table[OID_SLOTS];
static unsigned char oid_store[OID_SLOTS][OID_SLOT_BYTES]
    __attribute__((aligned(16)));

void *
oid_alloc(uint8_t oid, oid_type type, size_t size)
{
    for (int i = 0; i < OID_SLOTS; i++) {
        if (oid_table[i].type)
            continue;
        if (size > OID_SLOT_BYTES)
            return NULL;
        oid_table[i].oid = oid;
        oid_table[i].type = type;
        oid_table[i].ptr = oid_store[i];
        return oid_store[i];
    }
    return NULL;
}

void *
oid_lookup(uint8_t oid, oid_type type)
{
    for (int i = 0; i < OID_SLOTS; i++)
        if (oid_table[i].type == type && oid_table[i].oid == oid)
            return oid_table[i].ptr;
    return NULL;
}

void *
test_oid_foreach(uint8_t index, oid_type type)
{
    if (index < OID_SLOTS && oid_table[index].type == type)
        return oid_table[index].ptr;
    return NULL;
}

// ------------------------------------------------------------------
// Shared move pool (only host steppers may use this)
// ------------------------------------------------------------------

#define SHARED_MOVE_SLOTS 8

union shared_slot {
    struct move_node node;
    unsigned char bytes[64];
};

static union shared_slot shared_storage[SHARED_MOVE_SLOTS];
static struct move_node *shared_free;
static int test_move_alloc_count;
static int test_move_free_count;

static void
shared_pool_reset(void)
{
    shared_free = NULL;
    for (int i = SHARED_MOVE_SLOTS - 1; i >= 0; i--) {
        shared_storage[i].node.next = shared_free;
        shared_free = &shared_storage[i].node;
    }
}

void
move_queue_setup(struct move_queue_head *mq, uint8_t size)
{
    (void)size;
    if (config_finalized) {
        fprintf(stderr, "move_queue_setup after finalize shuts Klipper down\n");
        abort();
    }
    mq->head = mq->tail = NULL;
}

void
move_queue_push(struct move_node *m, struct move_queue_head *mq)
{
    m->next = NULL;
    if (mq->tail)
        mq->tail->next = m;
    else
        mq->head = m;
    mq->tail = m;
}

struct move_node *
move_queue_pop(struct move_queue_head *mq)
{
    struct move_node *m = mq->head;
    if (m) {
        mq->head = m->next;
        if (!mq->head)
            mq->tail = NULL;
        m->next = NULL;
    }
    return m;
}

void *
move_alloc(void)
{
    struct move_node *m = shared_free;
    if (!m)
        return NULL;
    shared_free = m->next;
    test_move_alloc_count++;
    return m;
}

void
move_free(void *p)
{
    struct move_node *m = p;
    m->next = shared_free;
    shared_free = m;
    test_move_free_count++;
}

// ------------------------------------------------------------------
// trsync stubs
// ------------------------------------------------------------------

void
trsync_add_signal(struct trsync *ts, struct trsync_signal *tss,
                  trsync_signal_func func)
{
    (void)ts;
    (void)tss;
    (void)func;
}

struct trsync *
trsync_oid_lookup(uint8_t oid)
{
    (void)oid;
    return NULL;
}

// ------------------------------------------------------------------
// Direct command declarations (the test drives the handlers itself)
// ------------------------------------------------------------------

void command_config_stepper(uint32_t *args);
void command_queue_step(uint32_t *args);
void command_reset_step_clock(uint32_t *args);
void command_stepper_get_position(uint32_t *args);
void stepper_local_shutdown(void);

// ------------------------------------------------------------------
// Harness
// ------------------------------------------------------------------

static const char *cur_fail;
static char fail_detail[256];
static int cases_run;
static int cases_failed;

static void
failf(const char *fmt, ...)
{
    if (cur_fail)
        return;
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(fail_detail, sizeof fail_detail, fmt, ap);
    va_end(ap);
    cur_fail = fail_detail;
}

#define CHECK(cond, ...) \
    do { if (!(cond)) failf(__VA_ARGS__); } while (0)

// Stale steppers from earlier cases stay allocated in the bump chunk.  Every
// case leaves its steppers idle, so resetting the world never zeroes their
// memory and never makes them write to a pin.  chunk_used stays monotonic.
static void
reset_world(void)
{
    gpio_event_count = 0;
    memset(gpio_levels, 0, sizeof gpio_levels);
    pending_timers = NULL;
    test_clock = 0;
    irq_depth = 0;
    test_move_alloc_count = 0;
    test_move_free_count = 0;
    shared_pool_reset();
    memset(oid_table, 0, sizeof oid_table);
    shutdown_count = 0;
    shutdown_message[0] = '\0';
}

static void
run_case(const char *name, void (*fn)(void))
{
    cur_fail = NULL;
    fail_detail[0] = '\0';
    reset_world();
    if (setjmp(shutdown_jmp)) {
        failf("unexpected shutdown: %s", shutdown_message);
    } else {
        fn();
    }
    cases_run++;
    if (cur_fail) {
        cases_failed++;
        printf("FAIL %s: %s\n", name, cur_fail);
    } else {
        printf("ok   %s\n", name);
    }
}

static struct stepper *
setup_local(uint32_t step_pin, uint32_t dir_pin, int_fast8_t invert_step,
            uint8_t max_moves)
{
    config_finalized = 1;
    struct stepper *s = stepper_local_setup(gpio_out_setup(step_pin, invert_step > 0),
                                            gpio_out_setup(dir_pin, 0), invert_step,
                                            STEP_PULSE, max_moves);
    config_finalized = 0;
    return s;
}

// Case 1: one move lands every step at the growing-delta schedule, on both the
// fully scheduled (invert_step 0) and both-edge (invert_step -1) paths.
static void
case_timing_full(void)
{
    const uint32_t I = STEP_INTERVAL, A = STEP_ADD;
    uint32_t start = test_clock + timer_from_us(200);
    struct stepper *s = setup_local(1, 2, 0, 4);
    CHECK(stepper_local_queue(s, start, I, 5, A, 0) == 0, "queue refused");
    fire_edges(1, 0, 5);
    uint32_t t[8];
    int n = step_edges(1, 0, t, 8);
    CHECK(n == 5, "expected 5 step edges, got %d", n);
    uint32_t want = start, delta = I;
    for (int k = 0; k < 5; k++) {
        want += delta;
        if (k < n)
            CHECK(t[k] == want, "step %d at %u want %u", k + 1, t[k], want);
        delta += A;
    }
    fire_all();   // the final step's unstep event completes the move
    CHECK(!stepper_local_active(s), "still active after 5 steps");
    CHECK(stepper_local_position(s) == -5, "position %d want -5",
          (int)stepper_local_position(s));
    int lows = 0;
    for (int i = 0; i < gpio_event_count; i++)
        if (gpio_events[i].pin == 1 && gpio_events[i].level == 0)
            lows++;
    CHECK(lows == 5, "expected 5 unstep edges, got %d", lows);

    s = setup_local(3, 4, -1, 4);
    start = test_clock + timer_from_us(200);
    CHECK(stepper_local_queue(s, start, I, 5, A, 0) == 0,
          "both-edge queue refused");
    fire_edges(3, 1, 5);
    n = step_edges(3, 1, t, 8);
    CHECK(n == 5, "both-edge: expected 5 step edges, got %d", n);
    want = start;
    delta = I;
    for (int k = 0; k < 5; k++) {
        want += delta;
        if (k < n)
            CHECK(t[k] == want, "both-edge step %d at %u want %u",
                  k + 1, t[k], want);
        delta += A;
    }
    CHECK(gpio_pin_events(3) == 5, "both-edge emitted %d toggles want 5",
          gpio_pin_events(3));
    CHECK(!stepper_local_active(s), "both-edge still active");
    CHECK(stepper_local_position(s) == -5, "both-edge position %d want -5",
          (int)stepper_local_position(s));
}

// Case 2: a move queued while active continues from the last step, using its
// own interval and add.
static void
case_back_to_back(void)
{
    const uint32_t I = STEP_INTERVAL, A = STEP_ADD;
    uint32_t start = test_clock + timer_from_us(200);
    struct stepper *s = setup_local(5, 6, 0, 4);
    CHECK(stepper_local_queue(s, start, I, 3, A, 0) == 0, "first queue refused");
    CHECK(stepper_local_active(s), "not active after first queue");
    CHECK(stepper_local_queue(s, start, I, 3, A, 0) == 0,
          "second queue refused");
    fire_all();
    uint32_t t[8];
    int n = step_edges(5, 0, t, 8);
    CHECK(n == 6, "expected 6 step edges, got %d", n);
    if (n == 6) {
        uint32_t want = start, delta = I;
        for (int k = 0; k < 3; k++) {
            want += delta;
            CHECK(t[k] == want, "move A step %d at %u want %u",
                  k + 1, t[k], want);
            delta += A;
        }
        CHECK(t[3] == t[2] + I, "move B first step %u want %u",
              t[3], (unsigned)(t[2] + I));
        delta = I + A;
        CHECK(t[4] == t[3] + delta, "move B step 2 at %u want %u",
              t[4], (unsigned)(t[3] + delta));
        delta += A;
        CHECK(t[5] == t[4] + delta, "move B step 3 at %u want %u",
              t[5], (unsigned)(t[4] + delta));
    }
    CHECK(!stepper_local_active(s), "still active");
}

// Case 3: a direction change between moves toggles DIR at queue time, before
// the first step of the new move; a same-direction move does not touch DIR.
static void
case_dir_change(void)
{
    const uint32_t I = STEP_INTERVAL;
    uint32_t start = test_clock + timer_from_us(200);
    struct stepper *s = setup_local(7, 8, 0, 4);
    CHECK(stepper_local_queue(s, start, I, 2, 0, 0) == 0, "dir 0 queue refused");
    fire_all();
    CHECK(gpio_pin_events(8) == 0, "first move toggled DIR");

    uint32_t queued_at = test_clock;
    uint32_t start2 = test_clock + timer_from_us(200);
    CHECK(stepper_local_queue(s, start2, I, 2, 0, 1) == 0,
          "dir 1 queue refused");
    CHECK(gpio_pin_events(8) == 1, "expected one DIR toggle, got %d",
          gpio_pin_events(8));
    uint32_t dir_time = gpio_first_time(8);
    CHECK(dir_time == queued_at, "DIR toggle at %u want %u",
          dir_time, queued_at);
    gpio_event_count = 0;
    fire_edges(7, 0, 2);
    uint32_t t[4];
    int n = step_edges(7, 0, t, 4);
    CHECK(n == 2, "expected 2 step edges, got %d", n);
    if (n == 2) {
        CHECK(t[0] == start2 + I, "first new step at %u want %u",
              t[0], (unsigned)(start2 + I));
        CHECK(t[1] == start2 + 2 * I, "second new step at %u want %u",
              t[1], (unsigned)(start2 + 2 * I));
        CHECK(dir_time < t[0], "DIR not toggled before the first step");
    }

    uint32_t start3 = test_clock + timer_from_us(200);
    CHECK(stepper_local_queue(s, start3, I, 2, 0, 1) == 0,
          "same-direction queue refused");
    CHECK(gpio_pin_events(8) == 0, "same-direction move toggled DIR");
    fire_all();
}

// Case 4: refusals return nonzero, queue nothing and never shutdown.
static void
case_refusals(void)
{
    const uint32_t I = STEP_INTERVAL;
    uint32_t start = test_clock + timer_from_us(200);
    struct stepper *s = setup_local(9, 10, 0, 2);
    CHECK(stepper_local_room(s) == 2, "room of a new stepper is not max_moves");
    CHECK(stepper_local_queue(s, start, I, 0, 0, 0) != 0,
          "zero count accepted");
    CHECK(!stepper_local_active(s), "zero count left active");
    CHECK(pending_count() == 0, "zero count scheduled a timer");
    CHECK(gpio_pin_events(9) == 0, "zero count pulsed the step pin");

    uint32_t early = test_clock + timer_from_us(STEPPER_LOCAL_MIN_LEAD_US) - 1;
    CHECK(stepper_local_queue(s, early, I, 1, 0, 0) != 0,
          "short-lead start accepted");
    CHECK(!stepper_local_active(s), "short lead left active");
    CHECK(pending_count() == 0, "short lead scheduled a timer");
    CHECK(gpio_pin_events(9) == 0, "short lead pulsed the step pin");

    CHECK(stepper_local_queue(s, start, I, 2, 0, 0) == 0, "accepted 1/3");
    CHECK(stepper_local_queue(s, start, I, 2, 0, 0) == 0, "accepted 2/3");
    CHECK(stepper_local_queue(s, start, I, 2, 0, 0) == 0, "accepted 3/3");
    CHECK(stepper_local_room(s) == 0, "room not 0 with a full queue");
    CHECK(stepper_local_queue(s, start, I, 1, 0, 0) != 0,
          "full private pool accepted a move");
    CHECK(stepper_local_active(s), "refused queue disturbed active move");
    CHECK(test_move_alloc_count == 0 && test_move_free_count == 0,
          "refusals touched the shared move pool");
    fire_all();
    CHECK(!stepper_local_active(s), "still active after draining");
    CHECK(stepper_local_room(s) == 2, "room not restored after draining");

    uint32_t restart = test_clock + timer_from_us(200);
    CHECK(stepper_local_queue(s, restart, I, 1, 0, 0) == 0,
          "private pool not restored after drain");
    fire_all();
    CHECK(!stepper_local_active(s), "still active at end");
}

// Case 5: stop unsteps, empties the queue, restores the pool and makes the
// next queue wait for a fresh start clock.
static void
case_stop_restart(void)
{
    const uint32_t I = STEP_INTERVAL;
    uint32_t start = test_clock + timer_from_us(200);
    struct stepper *s = setup_local(11, 12, 0, 2);
    CHECK(stepper_local_queue(s, start, I, 4, 0, 0) == 0, "queue 1 refused");
    CHECK(stepper_local_queue(s, start, I, 4, 0, 0) == 0, "queue 2 refused");
    fire_edges(11, 0, 1);
    CHECK(stepper_local_active(s), "stopped too early");
    CHECK(gpio_levels[11] == 1, "not mid-step before stop");
    stepper_local_stop(s);
    CHECK(!stepper_local_active(s), "stop left the stepper active");
    CHECK(pending_count() == 0, "stop left a timer queued");
    CHECK(gpio_levels[11] == 0, "stop left the step pin asserted");

    CHECK(stepper_local_queue(s, start, I, 2, 0, 0) != 0,
          "stale start clock accepted after stop");
    uint32_t fresh = test_clock + timer_from_us(200);
    gpio_event_count = 0;
    CHECK(stepper_local_queue(s, fresh, I, 2, 0, 0) == 0,
          "queue refused after stop");
    CHECK(stepper_local_queue(s, fresh, I, 2, 0, 0) == 0,
          "private pool not restored after stop");
    fire_edges(11, 0, 1);
    uint32_t t[4];
    int n = step_edges(11, 0, t, 4);
    CHECK(n == 1 && t[0] == fresh + I, "restart step %u want %u",
          n ? t[0] : 0, (unsigned)(fresh + I));
    fire_all();
    CHECK(!stepper_local_active(s), "still active at end");
}

// Case 6: the shutdown hook stops every local stepper.
static void
case_shutdown_hook(void)
{
    const uint32_t I = STEP_INTERVAL;
    uint32_t start = test_clock + timer_from_us(200);
    struct stepper *s1 = setup_local(13, 14, 0, 2);
    struct stepper *s2 = setup_local(15, 16, 0, 2);
    CHECK(stepper_local_queue(s1, start, I, 4, 0, 0) == 0, "queue 1 refused");
    CHECK(stepper_local_queue(s2, start, I, 4, 0, 0) == 0, "queue 2 refused");
    fire_edges(13, 0, 1);
    fire_edges(15, 0, 1);
    CHECK(stepper_local_active(s1) && stepper_local_active(s2),
          "steppers not active before shutdown");
    stepper_local_shutdown();
    CHECK(shutdown_count == 0, "shutdown hook called shutdown");
    CHECK(!stepper_local_active(s1), "shutdown left stepper 1 active");
    CHECK(!stepper_local_active(s2), "shutdown left stepper 2 active");
    CHECK(pending_count() == 0, "shutdown left timers queued");
    CHECK(gpio_levels[13] == 0 && gpio_levels[15] == 0,
          "shutdown left a step pin asserted");
}

// Case 7: stepper_local_position counts completed steps in both directions.
static void
case_position(void)
{
    const uint32_t I = STEP_INTERVAL;
    struct stepper *s = setup_local(19, 20, 0, 4);
    CHECK(stepper_local_position(s) == 0, "fresh position %d",
          (int)stepper_local_position(s));
    uint32_t start = test_clock + timer_from_us(200);
    CHECK(stepper_local_queue(s, start, I, 10, 0, 0) == 0,
          "forward queue refused");
    fire_all();
    CHECK(stepper_local_position(s) == -10, "position after 10 back %d",
          (int)stepper_local_position(s));

    gpio_event_count = 0;
    uint32_t start2 = test_clock + timer_from_us(200);
    CHECK(stepper_local_queue(s, start2, I, 4, 0, 1) == 0,
          "reverse queue refused");
    fire_edges(19, 0, 3);
    CHECK(stepper_local_position(s) == -7, "mid-move position %d want -7",
          (int)stepper_local_position(s));
    fire_all();
    CHECK(stepper_local_position(s) == -6, "position after 4 forward %d",
          (int)stepper_local_position(s));

    uint32_t start3 = test_clock + timer_from_us(200);
    CHECK(stepper_local_queue(s, start3, I, 6, 0, 0) == 0,
          "second reverse queue refused");
    fire_all();
    CHECK(stepper_local_position(s) == -12, "final position %d want -12",
          (int)stepper_local_position(s));
}

// Case 8: local steppers never allocate from the shared move pool.
static void
case_shared_pool_untouched(void)
{
    uint32_t start = test_clock + timer_from_us(200);
    struct stepper *s = setup_local(21, 22, -1, 2);
    CHECK(stepper_local_queue(s, start, STEP_INTERVAL, 5, 3, 0) == 0,
          "both-edge queue refused");
    fire_all();
    CHECK(!stepper_local_active(s), "still active");
    CHECK(test_move_alloc_count == 0,
          "local stepper called move_alloc %d times", test_move_alloc_count);
    CHECK(test_move_free_count == 0,
          "local stepper called move_free %d times", test_move_free_count);
}

// Case 9: a host stepper still takes the shared pool and keeps working while a
// local stepper runs.
static void
case_host_stepper(void)
{
    const uint32_t I = STEP_INTERVAL;
    struct stepper *loc = setup_local(23, 24, 0, 2);
    uint32_t lstart = test_clock + timer_from_us(200);
    CHECK(stepper_local_queue(loc, lstart, I, 4, 0, 0) == 0,
          "local queue refused");
    CHECK(test_move_alloc_count == 0, "local queue used the shared pool");

    uint32_t cargs[5] = { 0, 17, 18, 0, STEP_PULSE };
    command_config_stepper(cargs);
    uint32_t rargs[2] = { 0, test_clock + timer_from_us(200) };
    command_reset_step_clock(rargs);
    uint32_t qargs[4] = { 0, I, 3, 0 };
    command_queue_step(qargs);
    CHECK(test_move_alloc_count == 1, "host queue allocated %d moves",
          test_move_alloc_count);
    CHECK(test_move_free_count == 1, "host load released %d moves",
          test_move_free_count);

    gpio_event_count = 0;
    fire_all();
    uint32_t t[4];
    int n = step_edges(17, 0, t, 4);
    CHECK(n == 3, "host emitted %d step edges want 3", n);
    CHECK(!stepper_local_active(loc), "host stepper disturbed the local one");
    CHECK(gpio_levels[23] == 0, "local step pin left asserted");
    CHECK(test_move_alloc_count == 1 && test_move_free_count == 1,
          "unexpected further shared pool traffic");

    uint32_t pargs[1] = { 0 };
    command_stepper_get_position(pargs);
    CHECK(strstr(last_sendf, "stepper_position") != NULL,
          "position report was '%s'", last_sendf);
}

int
main(void)
{
    run_case("timing_full", case_timing_full);
    run_case("back_to_back", case_back_to_back);
    run_case("dir_change", case_dir_change);
    run_case("refusals", case_refusals);
    run_case("stop_restart", case_stop_restart);
    run_case("shutdown_hook", case_shutdown_hook);
    run_case("position", case_position);
    run_case("shared_pool_untouched", case_shared_pool_untouched);
    run_case("host_stepper", case_host_stepper);
    printf("summary: %d/%d cases passed\n", cases_run - cases_failed, cases_run);
    return cases_failed ? 1 : 0;
}
