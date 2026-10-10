// SPDX-License-Identifier: GPL-3.0-or-later
#include "shim_tmc.h"
#include "board/irq.h"
#include "board/misc.h"
#include "board/gpio.h"
#include "sched.h"
#include "command.h"
#include "klipper_mmu/tmc2209.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <assert.h>

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

static virtual_tmc_t virtual_chips[SHIM_MAX_PINS];

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

#define MAX_REGISTRATIONS 32
static shim_func_void_t init_funcs[MAX_REGISTRATIONS];
static int init_count = 0;
static shim_func_void_t task_funcs[MAX_REGISTRATIONS];
static int task_count = 0;
static shim_func_void_t shutdown_funcs[MAX_REGISTRATIONS];
static int shutdown_count = 0;

void shim_register_init(shim_func_void_t f)
{
    if (init_count < MAX_REGISTRATIONS) {
        init_funcs[init_count++] = f;
    }
}

void shim_register_task(shim_func_void_t f)
{
    if (task_count < MAX_REGISTRATIONS) {
        task_funcs[task_count++] = f;
    }
}

void shim_register_shutdown(shim_func_void_t f)
{
    if (shutdown_count < MAX_REGISTRATIONS) {
        shutdown_funcs[shutdown_count++] = f;
    }
}

/* Eight independent channels may need two timers each plus the common clock. */
#define MAX_TIMERS 32
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

/* Strict scheduler wrapper assertions */
#define STRICT_MAX_TIMERS 32
static struct timer *strict_active_timers[STRICT_MAX_TIMERS];
static int strict_num_active_timers = 0;
static struct timer *strict_executing_timer = NULL;

extern void __real_sched_add_timer(struct timer *t);
extern void __real_sched_del_timer(struct timer *t);

void __wrap_sched_add_timer(struct timer *t)
{
    assert(test_shim_irq_is_disabled());
    assert(t != strict_executing_timer);
    assert(!timer_is_before(t->waketime, timer_read_time() + 1u));
    for (int i = 0; i < strict_num_active_timers; i++) {
        assert(strict_active_timers[i] != t);
    }
    assert(strict_num_active_timers < STRICT_MAX_TIMERS);
    strict_active_timers[strict_num_active_timers++] = t;
    __real_sched_add_timer(t);
}

void __wrap_sched_del_timer(struct timer *del)
{
    assert(test_shim_irq_is_disabled());
    assert(del != strict_executing_timer);
    for (int i = 0; i < strict_num_active_timers; i++) {
        if (strict_active_timers[i] == del) {
            for (int j = i; j < strict_num_active_timers - 1; j++) {
                strict_active_timers[j] = strict_active_timers[j + 1];
            }
            strict_num_active_timers--;
            break;
        }
    }
    __real_sched_del_timer(del);
}

/* Virtual TMC frame processing */
static void chip_process_frame(virtual_tmc_t *f, uint32_t now_ticks)
{
    f->byte_count = 0;
    if (f->frame[0] != 0x05 || f->frame[1] != f->address) {
        return; /* Not addressed to this slave */
    }
    uint8_t expected_crc = km_tmc2209_crc(f->frame, (uint8_t)(f->expected_bytes - 1u));
    if (expected_crc != f->frame[f->expected_bytes - 1u]) {
        return; /* Bad CRC */
    }
    f->valid_frames++;
    uint8_t reg = (uint8_t)(f->frame[2] & 0x7Fu);
    if (f->expected_bytes == 8u) {
        /* Write frame */
        uint32_t val = ((uint32_t)f->frame[3] << 24) |
                       ((uint32_t)f->frame[4] << 16) |
                       ((uint32_t)f->frame[5] << 8)  |
                       ((uint32_t)f->frame[6]);
        if (reg == 1u) {
            f->registers[reg] &= ~val; /* clear-on-write */
        } else {
            f->registers[reg] = val;
        }
        /* Increment IFCNT */
        f->registers[2] = (f->registers[2] + 1u) & 0xFFu;
    } else {
        /* Read frame */
        if (f->no_reply) {
            return; /* Simulate silent chip */
        }
        if (f->no_reply_count) {
            --f->no_reply_count;
            return;
        }
        uint32_t val = f->registers[reg];
        f->reply[0] = 0x05;
        f->reply[1] = 0xFF; /* Master address */
        f->reply[2] = reg;
        for (unsigned i = 0; i < 4; ++i) {
            f->reply[3 + i] = (uint8_t)(val >> (24u - i * 8u));
        }
        f->reply[7] = km_tmc2209_crc(f->reply, 7);
        if (f->corrupt_reply_crc) {
            f->reply[7] ^= 0x01u;
        }
        if (f->corrupt_reply_count) {
            --f->corrupt_reply_count;
            f->reply[7] ^= 0x01u;
        }
        unsigned senddelay = (f->registers[3] >> 8) & 0x0Fu;
        unsigned delay_bits = (senddelay | 1u) * 8u;
        f->reply_start = now_ticks + f->bit_ticks / 2u + delay_bits * f->bit_ticks;
        f->reply_release = f->reply_start + 84u * f->bit_ticks;
        f->reply_pending = true;
    }
}

static void chip_step_to(virtual_tmc_t *f, uint32_t target_tick)
{
    if (!f->attached) return;

    while (f->chip_receiving && !timer_is_before(target_tick, f->chip_sample)) {
        bool high = !f->master_driving || f->master_level;
        if (f->chip_bit < 8u) {
            if (high) {
                f->chip_byte |= (uint8_t)(1u << f->chip_bit);
            }
            f->chip_bit++;
            f->chip_sample += f->bit_ticks;
        } else {
            /* 8 data bits sampled; check stop bit */
            f->chip_receiving = false;
            if (!high) {
                /* Framing error: stop level must be high */
                f->byte_count = 0;
                break;
            }
            f->frame[f->byte_count++] = f->chip_byte;
            if (f->byte_count == 3u) {
                f->expected_bytes = (f->frame[2] & 0x80u) ? 8u : 4u;
            }
            if (f->byte_count >= 4u && f->byte_count == f->expected_bytes) {
                chip_process_frame(f, f->chip_sample);
            }
        }
    }

    if (f->reply_pending && !timer_is_before(target_tick, f->reply_release)) {
        f->reply_pending = false;
    }
}

static void chip_on_master_edge(uint32_t pin, bool level, uint32_t now_ticks)
{
    if (pin >= SHIM_MAX_PINS) return;
    virtual_tmc_t *f = &virtual_chips[pin];
    if (!f->attached) return;

    if (f->edge_count < 64u) {
        f->edge_history[f->edge_count] = now_ticks;
        f->edge_levels[f->edge_count] = level;
        f->edge_count++;
    }

    chip_step_to(f, now_ticks);

    bool falling = (!f->master_driving || f->master_level) && !level;
    f->master_driving = true;
    f->master_level = level;

    if (falling && !f->chip_receiving) {
        if (now_ticks - f->byte_started > 63u * f->bit_ticks) {
            f->byte_count = 0;
        }
        f->byte_started = now_ticks;
        f->chip_receiving = true;
        f->chip_bit = 0;
        f->chip_byte = 0;
        f->chip_sample = now_ticks + f->bit_ticks + f->bit_ticks / 2u;
    }
}

void shim_attach_virtual_tmc(uint32_t pin, uint8_t address, uint32_t clock_hz)
{
    if (pin >= SHIM_MAX_PINS) return;
    virtual_tmc_t *f = &virtual_chips[pin];
    memset(f, 0, sizeof(*f));
    f->attached = true;
    f->address = address;
    f->bit_ticks = clock_hz / 40000u;
    f->master_level = true;
    /* Initialize default TMC2209 register values */
    f->registers[1] = 1u;          /* GSTAT: reset=1 */
    f->registers[2] = 250u;        /* IFCNT */
    f->registers[6] = 0x21000000u; /* IOIN: TMC2209 version 0x21 */
}

virtual_tmc_t *shim_get_virtual_tmc(uint32_t pin)
{
    if (pin >= SHIM_MAX_PINS) return NULL;
    return &virtual_chips[pin];
}

uint_fast8_t shim_dispatch_timer(struct timer *t)
{
    if (!t || !t->func)
        return SF_DONE;
    strict_executing_timer = t;
    uint_fast8_t res = t->func(t);
    strict_executing_timer = NULL;
    if (res == SF_DONE) {
        bool prev_irq = test_shim_irq_disabled;
        test_shim_irq_disabled = true;
        __wrap_sched_del_timer(t);
        test_shim_irq_disabled = prev_irq;
    } else if (res == SF_RESCHEDULE) {
        assert(!timer_is_before(t->waketime, timer_read_time() + 1u));
    }
    return res;
}

uint_fast8_t shim_dispatch_earliest_timer(void)
{
    struct timer *t = shim_find_earliest_timer();
    if (!t)
        return SF_DONE;
    if (timer_is_before(test_shim_ticks, t->waketime)) {
        uint32_t target_tick = t->waketime;
        for (uint32_t i = 0; i < SHIM_MAX_PINS; ++i) {
            if (virtual_chips[i].attached) {
                chip_step_to(&virtual_chips[i], target_tick);
            }
        }
        test_shim_ticks = target_tick;
    }
    return shim_dispatch_timer(t);
}

void sched_wake_task(struct task_wake *w)
{
    if (w) w->wake = 1u;
}

uint8_t sched_check_wake(struct task_wake *w)
{
    if (!w || !w->wake) return 0u;
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
    strict_num_active_timers = 0;
    shim_run_shutdown();
    irq_enable();
    if (test_shim_use_longjmp)
        longjmp(test_shim_shutdown_jump, 1);
}

_Noreturn void shutdown(const char *msg)
{
    do_shutdown(msg);
    abort();
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

void irq_wait(void) {}
void irq_poll(void) {}

bool test_shim_irq_is_disabled(void)
{
    return test_shim_irq_disabled;
}

struct gpio_out gpio_out_setup(uint32_t pin, uint32_t val)
{
    struct gpio_out g = { .pin = pin };
    gpio_out_reset(g, val);
    return g;
}

void gpio_out_reset(struct gpio_out g, uint32_t val)
{
    uint32_t pin = g.pin;
    chip_on_master_edge(pin, val != 0u, test_shim_ticks);
}

void gpio_out_write(struct gpio_out g, uint32_t val)
{
    uint32_t pin = g.pin;
    chip_on_master_edge(pin, val != 0u, test_shim_ticks);
}

void gpio_out_toggle_noirq(struct gpio_out g)
{
    uint32_t pin = g.pin;
    bool current = false;
    if (pin < SHIM_MAX_PINS && virtual_chips[pin].attached) {
        current = virtual_chips[pin].master_level;
    }
    gpio_out_write(g, !current);
}

void gpio_out_toggle(struct gpio_out g)
{
    irqstatus_t flag = irq_save();
    gpio_out_toggle_noirq(g);
    irq_restore(flag);
}

struct gpio_in gpio_in_setup(uint32_t pin, int32_t pull_up)
{
    struct gpio_in g = { .pin = pin };
    gpio_in_reset(g, pull_up);
    return g;
}

void gpio_in_reset(struct gpio_in g, int32_t pull_up)
{
    (void)pull_up;
    uint32_t pin = g.pin;
    if (pin < SHIM_MAX_PINS) {
        virtual_tmc_t *f = &virtual_chips[pin];
        if (f->attached) {
            chip_step_to(f, test_shim_ticks);
            f->master_driving = false;
            f->master_level = true;
        }
    }
}

uint8_t gpio_in_read(struct gpio_in g)
{
    uint32_t pin = g.pin;
    if (pin >= SHIM_MAX_PINS) return 1u;
    virtual_tmc_t *f = &virtual_chips[pin];
    if (!f->attached) return 1u;

    if (f->stuck_low) return 0u;
    if (f->master_driving) return f->master_level ? 1u : 0u;

    uint32_t now = test_shim_ticks;
    if (!f->reply_pending || timer_is_before(now, f->reply_start)) {
        return 1u; /* Line pulled high */
    }

    uint32_t bit = (now - f->reply_start) / f->bit_ticks;
    if (bit >= 80u) {
        return 1u;
    }
    unsigned offset = (unsigned)(bit % 10u);
    if (offset == 0u) return 0u; /* Start bit */
    if (offset == 9u) return 1u; /* Stop bit */
    uint8_t byte_idx = (uint8_t)(bit / 10u);
    return (uint8_t)(((unsigned)f->reply[byte_idx] >> (offset - 1u)) & 1u);
}

void shim_reset(uint32_t clock_hz)
{
    test_clock_freq = clock_hz;
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
    strict_num_active_timers = 0;
    strict_executing_timer = NULL;

    for (uint32_t i = 0; i < SHIM_MAX_PINS; ++i) {
        memset(&virtual_chips[i], 0, sizeof(virtual_chips[i]));
    }
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
