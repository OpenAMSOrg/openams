// SPDX-License-Identifier: GPL-3.0-or-later
#include "shim_klipper.h"
#include "board/irq.h"
#include <stdio.h>
#include <stdarg.h>
#include <string.h>
#include <stdlib.h>
#include <assert.h>

uint32_t test_clock_freq = 520000000u;
uint32_t test_device_id = 1u;
uint32_t test_board_id = 1u;
uint32_t test_lane_count = 4u;
uint32_t test_protocol_version = 1u;

uint32_t test_shim_ticks = 0u;
bool test_shim_board_ready = false;
bool test_shim_timer_armed_before_ready = false;
bool test_shim_sendf_called_in_lock = false;
bool test_shim_is_shutdown = false;
int test_shim_shutdown_count = 0;
char test_shim_shutdown_msg[128] = { 0 };
char test_shim_last_sendf[256] = { 0 };
int test_shim_sendf_count = 0;
int test_shim_timer_add_count = 0;
struct timer *test_shim_active_timer = NULL;
bool test_shim_use_longjmp = false;
jmp_buf test_shim_shutdown_jump;

static bool shim_irq_disabled = false;

irqstatus_t irq_save(void)
{
    irqstatus_t prev = shim_irq_disabled ? 1ul : 0ul;
    shim_irq_disabled = true;
    return prev;
}

void irq_restore(irqstatus_t flag)
{
    shim_irq_disabled = (flag != 0ul);
}

void irq_disable(void)
{
    shim_irq_disabled = true;
}

void irq_enable(void)
{
    shim_irq_disabled = false;
}

void irq_wait(void)
{
}

void irq_poll(void)
{
}

bool test_shim_irq_is_disabled(void)
{
    return shim_irq_disabled;
}

uint32_t timer_read_time(void)
{
    return test_shim_ticks;
}

uint32_t timer_from_us(uint32_t us)
{
    return (uint32_t)(((uint64_t)us * test_clock_freq + 999999ULL) / 1000000ULL);
}

uint8_t timer_is_before(uint32_t time1, uint32_t time2)
{
    return (uint8_t)((int32_t)(time1 - time2) < 0 ? 1u : 0u);
}

void sched_add_timer(struct timer *t)
{
    if (!test_shim_board_ready) {
        test_shim_timer_armed_before_ready = true;
    }
    test_shim_timer_add_count++;
    test_shim_active_timer = t;
}

void sched_del_timer(struct timer *del)
{
    if (test_shim_active_timer == del) {
        test_shim_active_timer = NULL;
    }
}

void sched_wake_task(struct task_wake *w)
{
    if (w) {
        w->wake = 1u;
    }
}

uint8_t sched_check_wake(struct task_wake *w)
{
    if (!w || !w->wake) {
        return 0u;
    }
    w->wake = 0u;
    return 1u;
}

uint8_t sched_is_shutdown(void)
{
    return test_shim_is_shutdown ? 1u : 0u;
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
    test_shim_active_timer = NULL;
    shim_run_shutdown();
    irq_enable();
    if (test_shim_use_longjmp)
        longjmp(test_shim_shutdown_jump, 1);
}

void shutdown(const char *msg)
{
    do_shutdown(msg);
}

void try_shutdown(const char *msg)
{
    if (!test_shim_is_shutdown) {
        do_shutdown(msg);
    }
}

void command_sendf(const char *fmt, ...)
{
    if (shim_irq_disabled) {
        test_shim_sendf_called_in_lock = true;
    }
    va_list args;
    va_start(args, fmt);
    vsnprintf(test_shim_last_sendf, sizeof(test_shim_last_sendf), fmt, args);
    va_end(args);
    test_shim_sendf_count++;
}

/* mcu/commands.c now registers more than 16 commands once every optional
 * block (TMC UART driver restart, selector gate/servo-test) is compiled in;
 * this silently drops registrations past the limit (shim_register_command),
 * so headroom matters more than the exact number. */
#define MAX_REGS 32
static shim_func_void_t init_funcs[MAX_REGS];
static int init_count = 0;
static shim_func_void_t task_funcs[MAX_REGS];
static int task_count = 0;
static shim_func_void_t shutdown_funcs[MAX_REGS];
static int shutdown_count = 0;

struct reg_cmd {
    command_func_t func;
    uint32_t flags;
    char msg[64];
};
static struct reg_cmd commands[MAX_REGS];
static int cmd_count = 0;

void shim_register_init(shim_func_void_t f)
{
    if (init_count < MAX_REGS) {
        init_funcs[init_count++] = f;
    }
}

void shim_register_task(shim_func_void_t f)
{
    if (task_count < MAX_REGS) {
        task_funcs[task_count++] = f;
    }
}

void shim_register_shutdown(shim_func_void_t f)
{
    if (shutdown_count < MAX_REGS) {
        shutdown_funcs[shutdown_count++] = f;
    }
}

void shim_register_command(command_func_t func, uint32_t flags, const char *msg)
{
    if (cmd_count < MAX_REGS) {
        commands[cmd_count].func = func;
        commands[cmd_count].flags = flags;
        strncpy(commands[cmd_count].msg, msg, sizeof(commands[cmd_count].msg) - 1u);
        commands[cmd_count].msg[sizeof(commands[cmd_count].msg) - 1u] = '\0';
        cmd_count++;
    }
}

void shim_register_constant(const char *name, uint32_t val)
{
    (void)name;
    (void)val;
}

void shim_run_init(void)
{
    for (int i = 0; i < init_count; i++) {
        init_funcs[i]();
    }
}

void shim_run_task(void)
{
    for (int i = 0; i < task_count; i++) {
        task_funcs[i]();
    }
}

void shim_run_shutdown(void)
{
    for (int i = 0; i < shutdown_count; i++) {
        shutdown_funcs[i]();
    }
}

command_func_t shim_find_command(const char *name)
{
    for (int i = 0; i < cmd_count; i++) {
        if (strncmp(commands[i].msg, name, strlen(name)) == 0) {
            return commands[i].func;
        }
    }
    return NULL;
}

uint_fast8_t shim_dispatch_timer(void)
{
    if (!test_shim_active_timer || !test_shim_active_timer->func) {
        return SF_DONE;
    }
    struct timer *t = test_shim_active_timer;
    irqstatus_t prev = irq_save();
    uint_fast8_t res = t->func(t);
    irq_restore(prev);
    if (res == SF_DONE) {
        test_shim_active_timer = NULL;
    }
    return res;
}

void shim_reset(uint32_t clock_hz, uint32_t device_id, uint32_t board_id, uint32_t lane_count)
{
    test_clock_freq = clock_hz;
    test_device_id = device_id;
    test_board_id = board_id;
    test_lane_count = lane_count;
    test_protocol_version = 1u;

    test_shim_ticks = 0u;
    test_shim_board_ready = false;
    test_shim_timer_armed_before_ready = false;
    test_shim_sendf_called_in_lock = false;
    test_shim_is_shutdown = false;
    test_shim_shutdown_count = 0;
    test_shim_shutdown_msg[0] = '\0';
    test_shim_last_sendf[0] = '\0';
    test_shim_sendf_count = 0;
    test_shim_timer_add_count = 0;
    test_shim_active_timer = NULL;
    test_shim_use_longjmp = false;
    shim_irq_disabled = false;
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
