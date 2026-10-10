// SPDX-License-Identifier: GPL-3.0-or-later
#include "shim_klipper.h"
#include "board/irq.h"
#include "board/misc.h"
#include "board/gpio.h"
#include "sched.h"
#include "command.h"
#include "basecmd.h"
#include "trsync.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

uint32_t test_clock_freq = 520000000u;
uint32_t test_device_id = 1u;
uint32_t test_board_id = 1u;
uint32_t test_lane_count = 4u;
uint32_t test_protocol_version = 1u;

uint32_t test_shim_ticks = 1000u;
bool test_shim_board_ready = false;
bool test_shim_is_shutdown = false;
int test_shim_shutdown_count = 0;
char test_shim_shutdown_msg[128] = {0};
bool test_shim_use_longjmp = false;
jmp_buf test_shim_shutdown_jump;
uint32_t test_shim_clock_reads_in_shutdown = 0u;
bool test_shim_in_shutdown = false;

static bool test_shim_irq_disabled = false;

uint32_t shim_gpio_setup_count[SHIM_MAX_PINS];
uint32_t shim_gpio_write_count[SHIM_MAX_PINS];
uint32_t shim_gpio_current_level[SHIM_MAX_PINS];
uint32_t shim_gpio_last_write_tick[SHIM_MAX_PINS];
bool shim_gpio_is_setup[SHIM_MAX_PINS];
uint32_t shim_gpio_fail_pin = 0xFFFFFFFFu;

uint32_t shim_gpio_write_delay_ticks = 0u;
uint32_t shim_gpio_write_delay_pin = 0xFFFFFFFFu;
uint32_t shim_gpio_write_clock_jump_ticks = 0u;
uint32_t shim_gpio_write_clock_jump_pin = 0xFFFFFFFFu;

shim_gpio_event_t shim_gpio_events[SHIM_GPIO_MAX_EVENTS];
uint32_t shim_gpio_events_count = 0u;

void shim_gpio_clear_events(void)
{
    shim_gpio_events_count = 0u;
    memset(shim_gpio_events, 0, sizeof(shim_gpio_events));
}

#define MAX_REGISTRATIONS 32
static shim_func_void_t init_funcs[MAX_REGISTRATIONS];
static int init_count = 0;
static shim_func_void_t task_funcs[MAX_REGISTRATIONS];
static int task_count = 0;
static shim_func_void_t shutdown_funcs[MAX_REGISTRATIONS];
static int shutdown_count = 0;

void shim_register_init(shim_func_void_t f)
{
    if (init_count < MAX_REGISTRATIONS)
        init_funcs[init_count++] = f;
}

void shim_register_task(shim_func_void_t f)
{
    if (task_count < MAX_REGISTRATIONS)
        task_funcs[task_count++] = f;
}

void shim_register_shutdown(shim_func_void_t f)
{
    if (shutdown_count < MAX_REGISTRATIONS)
        shutdown_funcs[shutdown_count++] = f;
}

void shim_register_command(command_func_t func, uint32_t flags, const char *msg)
{
    (void)func; (void)flags; (void)msg;
}

void shim_register_constant(const char *name, uint32_t val)
{
    (void)name; (void)val;
}

void command_sendf(const char *fmt, ...)
{
    (void)fmt;
}

#define MAX_TIMERS 16
static struct timer *active_timers[MAX_TIMERS];
static int num_active_timers = 0;

void sched_add_timer(struct timer *t)
{
    for (int i = 0; i < num_active_timers; i++) {
        if (active_timers[i] == t)
            return;
    }
    if (num_active_timers < MAX_TIMERS)
        active_timers[num_active_timers++] = t;
}

void sched_del_timer(struct timer *del)
{
    for (int i = 0; i < num_active_timers; i++) {
        if (active_timers[i] == del) {
            for (int j = i; j < num_active_timers - 1; j++)
                active_timers[j] = active_timers[j + 1];
            num_active_timers--;
            return;
        }
    }
}

int shim_active_timer_count(void)
{
    return num_active_timers;
}

struct timer *shim_find_earliest_timer(void)
{
    if (num_active_timers == 0)
        return NULL;
    struct timer *earliest = active_timers[0];
    for (int i = 1; i < num_active_timers; i++) {
        if (timer_is_before(active_timers[i]->waketime, earliest->waketime))
            earliest = active_timers[i];
    }
    return earliest;
}

uint_fast8_t shim_dispatch_timer(struct timer *t)
{
    if (!t || !t->func)
        return SF_DONE;
    uint_fast8_t res = t->func(t);
    if (res == SF_DONE)
        sched_del_timer(t);
    return res;
}

uint_fast8_t shim_dispatch_earliest_timer(void)
{
    struct timer *t = shim_find_earliest_timer();
    if (!t)
        return SF_DONE;
    if (timer_is_before(test_shim_ticks, t->waketime))
        test_shim_ticks = t->waketime;
    return shim_dispatch_timer(t);
}

void sched_wake_task(struct task_wake *w)
{
    if (w)
        w->wake = 1u;
}

uint8_t sched_check_wake(struct task_wake *w)
{
    if (!w || !w->wake)
        return 0u;
    w->wake = 0u;
    return 1u;
}

uint8_t sched_is_shutdown(void)
{
    return test_shim_is_shutdown ? 1u : 0u;
}

void sched_clear_shutdown(void)
{
    test_shim_is_shutdown = false;
}

static void do_shutdown(const char *msg)
{
    irq_disable();
    test_shim_is_shutdown = true;
    test_shim_shutdown_count++;
    if (msg) {
        strncpy(test_shim_shutdown_msg, msg, sizeof(test_shim_shutdown_msg) - 1u);
        test_shim_shutdown_msg[sizeof(test_shim_shutdown_msg) - 1u] = '\0';
    }
    num_active_timers = 0;
    shim_run_shutdown();
    irq_enable();
    if (test_shim_use_longjmp)
        longjmp(test_shim_shutdown_jump, 1);
}

_Noreturn void shutdown(const char *msg)
{
    do_shutdown(msg);
    abort(); /* A production shutdown never returns to the interrupted caller. */
}

void try_shutdown(const char *msg)
{
    if (!test_shim_is_shutdown)
        shutdown(msg);
}

uint32_t timer_read_time(void)
{
    if (test_shim_in_shutdown) {
        test_shim_clock_reads_in_shutdown++;
    }
    return test_shim_ticks;
}

uint32_t timer_from_us(uint32_t us)
{
    uint64_t prod = (uint64_t)us * (uint64_t)test_clock_freq + 999999ULL;
    return (uint32_t)(prod / 1000000ULL);
}

bool timer_is_before(uint32_t time1, uint32_t time2)
{
    return (int32_t)(time1 - time2) < 0;
}

irqstatus_t irq_save(void)
{
    irqstatus_t flag = test_shim_irq_disabled ? 1ul : 0ul;
    test_shim_irq_disabled = true;
    return flag;
}

void irq_restore(irqstatus_t flag)
{
    test_shim_irq_disabled = (flag != 0ul);
}

void irq_disable(void)
{
    test_shim_irq_disabled = true;
}

void irq_enable(void)
{
    test_shim_irq_disabled = false;
}

void irq_wait(void)
{
}

void irq_poll(void)
{
}

bool test_shim_irq_is_disabled(void)
{
    return test_shim_irq_disabled;
}

struct gpio_out gpio_out_setup(uint32_t pin, uint32_t val)
{
    if (pin == shim_gpio_fail_pin) {
        shutdown("Not a valid pin");
    }
    if (pin < SHIM_MAX_PINS) {
        shim_gpio_setup_count[pin]++;
        shim_gpio_current_level[pin] = val ? 1u : 0u;
        shim_gpio_is_setup[pin] = true;
        shim_gpio_last_write_tick[pin] = test_shim_ticks;
        if (shim_gpio_events_count < SHIM_GPIO_MAX_EVENTS) {
            shim_gpio_events[shim_gpio_events_count++] = (shim_gpio_event_t){
                .pin = pin,
                .val = val ? 1u : 0u,
                .tick = test_shim_ticks,
                .is_setup = true
            };
        }
    }
    return (struct gpio_out){ .pin = pin };
}

void gpio_out_write(struct gpio_out g, uint32_t val)
{
    uint32_t pin = g.pin;
    if (pin < SHIM_MAX_PINS) {
        shim_gpio_write_count[pin]++;
        shim_gpio_current_level[pin] = val ? 1u : 0u;
        shim_gpio_last_write_tick[pin] = test_shim_ticks;
        if (shim_gpio_events_count < SHIM_GPIO_MAX_EVENTS) {
            shim_gpio_events[shim_gpio_events_count++] = (shim_gpio_event_t){
                .pin = pin,
                .val = val ? 1u : 0u,
                .tick = test_shim_ticks,
                .is_setup = false
            };
        }
    }
    if (shim_gpio_write_delay_ticks > 0u) {
        if (shim_gpio_write_delay_pin == 0xFFFFFFFFu || shim_gpio_write_delay_pin == pin) {
            test_shim_ticks += shim_gpio_write_delay_ticks;
        }
    }
    if (shim_gpio_write_clock_jump_ticks > 0u) {
        if (shim_gpio_write_clock_jump_pin == 0xFFFFFFFFu || shim_gpio_write_clock_jump_pin == pin) {
            test_shim_ticks += shim_gpio_write_clock_jump_ticks;
            shim_gpio_write_clock_jump_ticks = 0u;
        }
    }
}

void shim_reset(uint32_t clock_hz, uint32_t device_id, uint32_t board_id, uint32_t lane_count)
{
    test_clock_freq = clock_hz;
    test_device_id = device_id;
    test_board_id = board_id;
    test_lane_count = lane_count;
    test_protocol_version = 1u;
    test_shim_ticks = 1000u;
    test_shim_board_ready = false;
    test_shim_is_shutdown = false;
    test_shim_shutdown_count = 0;
    test_shim_shutdown_msg[0] = '\0';
    test_shim_use_longjmp = false;
    test_shim_clock_reads_in_shutdown = 0u;
    test_shim_in_shutdown = false;
    test_shim_irq_disabled = false;
    num_active_timers = 0;

    memset(shim_gpio_setup_count, 0, sizeof(shim_gpio_setup_count));
    memset(shim_gpio_write_count, 0, sizeof(shim_gpio_write_count));
    memset(shim_gpio_current_level, 0, sizeof(shim_gpio_current_level));
    memset(shim_gpio_last_write_tick, 0, sizeof(shim_gpio_last_write_tick));
    memset(shim_gpio_is_setup, 0, sizeof(shim_gpio_is_setup));
    shim_gpio_fail_pin = 0xFFFFFFFFu;
    shim_gpio_write_delay_ticks = 0u;
    shim_gpio_write_delay_pin = 0xFFFFFFFFu;
    shim_gpio_write_clock_jump_ticks = 0u;
    shim_gpio_write_clock_jump_pin = 0xFFFFFFFFu;
    shim_gpio_clear_events();
}

void shim_set_board_ready(bool ready)
{
    test_shim_board_ready = ready;
}

void shim_set_ticks(uint32_t ticks)
{
    test_shim_ticks = ticks;
}

void shim_advance_ticks(uint32_t delta)
{
    test_shim_ticks += delta;
}

void shim_run_init(void)
{
    for (int i = 0; i < init_count; i++) {
        if (init_funcs[i])
            init_funcs[i]();
    }
}

void shim_run_task(void)
{
    for (int i = 0; i < task_count; i++) {
        if (task_funcs[i])
            task_funcs[i]();
    }
}

void shim_run_shutdown(void)
{
    test_shim_in_shutdown = true;
    for (int i = 0; i < shutdown_count; i++) {
        if (shutdown_funcs[i])
            shutdown_funcs[i]();
    }
    test_shim_in_shutdown = false;
}


// Mocks for stepper.c command handling
void *move_alloc(void) { return malloc(32); }
void move_queue_setup(struct move_queue_head *mq, int move_size) { (void)move_size; mq->head = mq->tail = NULL; }
void move_queue_push(struct move_node *m, struct move_queue_head *mq) {
    m->next = NULL;
    if (mq->tail)
        mq->tail->next = m;
    else
        mq->head = m;
    mq->tail = m;
}
struct move_node *move_queue_pop(struct move_queue_head *mq) {
    struct move_node *m = mq->head;
    if (m) {
        mq->head = m->next;
        if (!mq->head)
            mq->tail = NULL;
        m->next = NULL;
    }
    return m;
}
int move_queue_empty(struct move_queue_head *mq) {
    return mq->head == NULL;
}
void move_queue_clear(struct move_queue_head *mq) {
    mq->head = mq->tail = NULL;
}
void move_free(void *m) { free(m); }

void *oid_alloc(uint8_t oid, oid_type type, size_t size) { (void)oid; (void)type; return malloc(size); }
void *oid_lookup(uint8_t oid, oid_type type) { (void)oid; (void)type; return NULL; }
void *test_oid_foreach(uint8_t index, oid_type type) { (void)index; (void)type; return NULL; }
/* Real Klipper's alloc_chunk() zeroes memory (comment in
 * upstream/drivers-local.patch's stepper_local_setup()); stepper_local's
 * struct stepper_local relies on that for its zero-initialized flags/count. */
void *alloc_chunk(size_t size) { return calloc(1, size); }

struct trsync *trsync_oid_lookup(uint8_t oid) { (void)oid; return NULL; }
void trsync_add_signal(struct trsync *ts, struct trsync_signal *tss,
                       trsync_signal_func func) { (void)ts; (void)tss; (void)func; }
/* Real stepper.c's fully-scheduled step function toggles rather than writes
 * (stepper_event_full() calls gpio_out_toggle_noirq(), not gpio_out_write()),
 * so this must flip the tracked level and record an event the same way
 * gpio_out_write() does, or a real local stepper's steps go untracked. */
void gpio_out_toggle_noirq(struct gpio_out g)
{
    uint32_t pin = g.pin;
    if (pin < SHIM_MAX_PINS)
        gpio_out_write(g, shim_gpio_current_level[pin] ? 0u : 1u);
}

