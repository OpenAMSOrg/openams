// SPDX-License-Identifier: GPL-3.0-or-later
#include "shim_klipper.h"
#include "board/irq.h"
#include "board/gpio.h"
#include <stdio.h>
#include <stdarg.h>
#include <string.h>
#include <stdlib.h>
#include <assert.h>
#include "stm32/adc_local.h"

uint32_t test_clock_freq = 520000000u;
uint32_t test_device_id = 1u;
uint32_t test_board_id = 1u;
uint32_t test_lane_count = 4u;
uint32_t test_protocol_version = 1u;

uint32_t test_shim_ticks = 0u;
bool test_shim_board_ready = false;
bool test_shim_timer_armed_before_ready = false;
bool test_shim_is_shutdown = false;
int test_shim_shutdown_count = 0;
char test_shim_shutdown_msg[128] = { 0 };
bool test_shim_use_longjmp = false;
jmp_buf test_shim_shutdown_jump;

uint32_t shim_adc_setup_fail_pin = 0u;
bool shim_adc_setup_a_called = false;
bool shim_adc_setup_b_called = false;
uint32_t shim_adc_delay_ticks[2] = { 0u, 0u };
uint32_t shim_adc_sample_return_ticks[2] = { 10400u, 10400u }; /* 20us at 520MHz */
uint16_t shim_adc_raw_value[2] = { 2048u, 2048u };
bool shim_adc_converting[2] = { false, false };
uint32_t shim_adc_ready_at_ticks[2] = { 0u, 0u };
int shim_adc_sample_count[2] = { 0, 0 };
int shim_adc_read_count[2] = { 0, 0 };
int shim_adc_cancel_count[2] = { 0, 0 };
bool shim_adc_double_read_error = false;
bool shim_adc_read_before_ready_error = false;
bool shim_adc_b_started_before_a_consumed = false;
bool shim_adc_cancel_inactive_error = false;

uint32_t shim_adc_sample_cost_ticks[2] = { 0u, 0u };
uint32_t shim_adc_read_cost_ticks[2] = { 0u, 0u };
int shim_adc_irq_state_violations = 0;
bool shim_adc_setup_irq_disabled_violation = false;
bool shim_adc_callback_irq_enabled_violation = false;

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
    return us * (test_clock_freq / 1000000u);
}

uint8_t timer_is_before(uint32_t time1, uint32_t time2)
{
    return (uint8_t)((int32_t)(time1 - time2) < 0 ? 1u : 0u);
}

#define MAX_TIMERS 16
static struct timer *active_timers[MAX_TIMERS];
static int num_active_timers = 0;

void sched_add_timer(struct timer *t)
{
    if (!t) return;
    if (!test_shim_board_ready) {
        test_shim_timer_armed_before_ready = true;
    }
    if (timer_is_before(t->waketime, test_shim_ticks)) {
        try_shutdown("Timer too close");
    }
    /* Check if already present */
    for (int i = 0; i < num_active_timers; i++) {
        if (active_timers[i] == t)
            return;
    }
    if (num_active_timers < MAX_TIMERS) {
        active_timers[num_active_timers++] = t;
    }
}

void sched_del_timer(struct timer *del)
{
    if (!del) return;
    for (int i = 0; i < num_active_timers; i++) {
        if (active_timers[i] == del) {
            for (int j = i; j < num_active_timers - 1; j++) {
                active_timers[j] = active_timers[j + 1];
            }
            num_active_timers--;
            active_timers[num_active_timers] = NULL;
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
        if (timer_is_before(active_timers[i]->waketime, earliest->waketime)) {
            earliest = active_timers[i];
        }
    }
    return earliest;
}

uint_fast8_t shim_dispatch_timer(struct timer *t)
{
    if (!t || !t->func) return SF_DONE;
    irqstatus_t prev = irq_save();
    uint_fast8_t res = t->func(t);
    irq_restore(prev);
    if (res == SF_DONE) {
        sched_del_timer(t);
    }
    return res;
}

uint_fast8_t shim_dispatch_earliest_timer(void)
{
    struct timer *earliest = shim_find_earliest_timer();
    if (!earliest)
        return SF_DONE;
    return shim_dispatch_timer(earliest);
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
    if (!test_shim_is_shutdown)
        do_shutdown(msg);
}

void command_sendf(const char *fmt, ...)
{
    (void)fmt;
}

#define MAX_REGS 16
static shim_func_void_t init_funcs[MAX_REGS];
static int init_count = 0;
static shim_func_void_t task_funcs[MAX_REGS];
static int task_count = 0;
static shim_func_void_t shutdown_funcs[MAX_REGS];
static int shutdown_count = 0;

void shim_register_init(shim_func_void_t f)
{
    if (init_count < MAX_REGS) init_funcs[init_count++] = f;
}

void shim_register_task(shim_func_void_t f)
{
    if (task_count < MAX_REGS) task_funcs[task_count++] = f;
}

void shim_register_shutdown(shim_func_void_t f)
{
    if (shutdown_count < MAX_REGS) shutdown_funcs[shutdown_count++] = f;
}

void shim_register_command(command_func_t func, uint32_t flags, const char *msg)
{
    (void)func; (void)flags; (void)msg;
}

void shim_register_constant(const char *name, uint32_t val)
{
    (void)name; (void)val;
}

void shim_run_init(void)
{
    for (int i = 0; i < init_count; i++) init_funcs[i]();
}

void shim_run_task(void)
{
    for (int i = 0; i < task_count; i++) task_funcs[i]();
}

void shim_run_shutdown(void)
{
    for (int i = 0; i < shutdown_count; i++) shutdown_funcs[i]();
}

/* Preset timing-suite pins: PC4 / PC5. Ownership is mock-only. */
static bool shim_adc_pair_owned = false;
static uint32_t shim_adc_owned_pin_a = 0u;
static uint32_t shim_adc_owned_pin_b = 0u;
static struct gpio_adc shim_adc_owned_a;
static struct gpio_adc shim_adc_owned_b;

static int shim_adc_init_owned_pin(uint32_t pin, struct gpio_adc *out)
{
    if (shim_adc_setup_fail_pin != 0u && pin == shim_adc_setup_fail_pin) {
        shutdown("ADC setup failed");
        return GPIO_ADC_LOCAL_ERR_SHUTDOWN;
    }
    if (pin == 36u) { /* PC4 -> ADC1 channel 4 */
        shim_adc_setup_a_called = true;
        *out = (struct gpio_adc){ .adc = (void*)1, .chan = 4u };
        return GPIO_ADC_LOCAL_OK;
    }
    if (pin == 37u) { /* PC5 -> ADC1 channel 8 */
        shim_adc_setup_b_called = true;
        *out = (struct gpio_adc){ .adc = (void*)1, .chan = 8u };
        return GPIO_ADC_LOCAL_OK;
    }
    shutdown("Not a valid ADC pin");
    return GPIO_ADC_LOCAL_ERR_INVALID;
}

struct gpio_adc gpio_adc_setup(uint32_t pin)
{
    (void)pin;
    abort();
}

int gpio_adc_setup_local_pair(uint32_t pin_a, uint32_t pin_b,
                              struct gpio_adc *out_a, struct gpio_adc *out_b)
{
    if (!out_a || !out_b)
        return GPIO_ADC_LOCAL_ERR_INVALID;
    if (sched_is_shutdown())
        return GPIO_ADC_LOCAL_ERR_SHUTDOWN;

    /* Validate the ordered pair before any pin initialization. */
    if (!((pin_a == 36u && pin_b == 37u) || (pin_a == 37u && pin_b == 36u)))
        return GPIO_ADC_LOCAL_ERR_INVALID;

    if (shim_adc_pair_owned) {
        if (pin_a == shim_adc_owned_pin_a && pin_b == shim_adc_owned_pin_b) {
            *out_a = shim_adc_owned_a;
            *out_b = shim_adc_owned_b;
            return GPIO_ADC_LOCAL_OK;
        }
        return GPIO_ADC_LOCAL_ERR_BUSY;
    }

    if (shim_irq_disabled) {
        shim_adc_irq_state_violations++;
        shim_adc_setup_irq_disabled_violation = true;
        return GPIO_ADC_LOCAL_ERR_INVALID;
    }

    struct gpio_adc fresh_a = { .adc = NULL, .chan = 0u };
    struct gpio_adc fresh_b = { .adc = NULL, .chan = 0u };
    int rc_a = shim_adc_init_owned_pin(pin_a, &fresh_a);
    if (rc_a != GPIO_ADC_LOCAL_OK)
        return rc_a;
    int rc_b = shim_adc_init_owned_pin(pin_b, &fresh_b);
    if (rc_b != GPIO_ADC_LOCAL_OK) {
        /* Partial setup is not published. Diagnostic called-flags stay. */
        fresh_a.adc = NULL;
        fresh_a.chan = 0u;
        return rc_b;
    }

    shim_adc_owned_a = fresh_a;
    shim_adc_owned_b = fresh_b;
    shim_adc_owned_pin_a = pin_a;
    shim_adc_owned_pin_b = pin_b;
    shim_adc_pair_owned = true;
    *out_a = fresh_a;
    *out_b = fresh_b;
    return GPIO_ADC_LOCAL_OK;
}

/* Immediate mock, not hardware deadline proof. Ticks stay unchanged. */
int gpio_adc_setup_local_pair_timeout(uint32_t pin_a, uint32_t pin_b, uint32_t timeout_us, struct gpio_adc *out_a, struct gpio_adc *out_b)
{
    uint32_t mhz = test_clock_freq / 1000000u;
    if (timeout_us == 0u || mhz == 0u || (test_clock_freq % 1000000u) != 0u)
        return GPIO_ADC_LOCAL_ERR_INVALID;
    uint64_t tick_len = (uint64_t)timeout_us * (uint64_t)mhz;
    if (tick_len >= (1ull << 31))
        return GPIO_ADC_LOCAL_ERR_INVALID;
    return gpio_adc_setup_local_pair(pin_a, pin_b, out_a, out_b);
}

uint32_t gpio_adc_sample(struct gpio_adc g)
{
    if (!shim_irq_disabled) {
        shim_adc_irq_state_violations++;
        shim_adc_callback_irq_enabled_violation = true;
    }
    uint32_t idx = (g.chan == 4u) ? 0u : 1u;
    if (idx == 1u && shim_adc_converting[0]) {
        shim_adc_b_started_before_a_consumed = true;
    }
    shim_adc_sample_count[idx]++;
    if (!shim_adc_converting[idx]) {
        shim_adc_converting[idx] = true;
        shim_adc_ready_at_ticks[idx] = test_shim_ticks + shim_adc_delay_ticks[idx];
    }
    shim_advance_ticks(shim_adc_sample_cost_ticks[idx]);
    if ((int32_t)(test_shim_ticks - shim_adc_ready_at_ticks[idx]) >= 0) {
        return 0u; /* Ready */
    }
    return shim_adc_sample_return_ticks[idx];
}

uint16_t gpio_adc_read(struct gpio_adc g)
{
    if (!shim_irq_disabled) {
        shim_adc_irq_state_violations++;
        shim_adc_callback_irq_enabled_violation = true;
    }
    uint32_t idx = (g.chan == 4u) ? 0u : 1u;
    if (!shim_adc_converting[idx] || (int32_t)(test_shim_ticks - shim_adc_ready_at_ticks[idx]) < 0) {
        shim_adc_read_before_ready_error = true;
    }
    if (shim_adc_read_count[idx] > 0 && !shim_adc_converting[idx]) {
        shim_adc_double_read_error = true;
    }
    shim_adc_converting[idx] = false; /* Consumed */
    shim_adc_read_count[idx]++;
    shim_advance_ticks(shim_adc_read_cost_ticks[idx]);
    return shim_adc_raw_value[idx];
}

void gpio_adc_cancel_sample(struct gpio_adc g)
{
    if (!shim_irq_disabled) {
        shim_adc_irq_state_violations++;
        shim_adc_callback_irq_enabled_violation = true;
    }
    uint32_t idx = (g.chan == 4u) ? 0u : 1u;
    if (!shim_adc_converting[idx]) {
        shim_adc_cancel_inactive_error = true;
    }
    shim_adc_converting[idx] = false;
    shim_adc_cancel_count[idx]++;
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
    test_shim_is_shutdown = false;
    test_shim_shutdown_count = 0;
    test_shim_shutdown_msg[0] = '\0';
    test_shim_use_longjmp = false;
    shim_irq_disabled = false;
    num_active_timers = 0;

    shim_adc_setup_fail_pin = 0u;
    shim_adc_setup_a_called = false;
    shim_adc_setup_b_called = false;
    shim_adc_pair_owned = false;
    shim_adc_owned_pin_a = 0u;
    shim_adc_owned_pin_b = 0u;
    shim_adc_owned_a = (struct gpio_adc){ .adc = NULL, .chan = 0u };
    shim_adc_owned_b = (struct gpio_adc){ .adc = NULL, .chan = 0u };
    shim_adc_delay_ticks[0] = 0u;
    shim_adc_delay_ticks[1] = 0u;
    shim_adc_sample_return_ticks[0] = 10400u;
    shim_adc_sample_return_ticks[1] = 10400u;
    shim_adc_raw_value[0] = 2048u;
    shim_adc_raw_value[1] = 2048u;
    shim_adc_converting[0] = false;
    shim_adc_converting[1] = false;
    shim_adc_ready_at_ticks[0] = 0u;
    shim_adc_ready_at_ticks[1] = 0u;
    shim_adc_sample_count[0] = 0;
    shim_adc_sample_count[1] = 0;
    shim_adc_read_count[0] = 0;
    shim_adc_read_count[1] = 0;
    shim_adc_cancel_count[0] = 0;
    shim_adc_cancel_count[1] = 0;
    shim_adc_double_read_error = false;
    shim_adc_read_before_ready_error = false;
    shim_adc_b_started_before_a_consumed = false;
    shim_adc_cancel_inactive_error = false;

    shim_adc_sample_cost_ticks[0] = 0u;
    shim_adc_sample_cost_ticks[1] = 0u;
    shim_adc_read_cost_ticks[0] = 0u;
    shim_adc_read_cost_ticks[1] = 0u;
    shim_adc_irq_state_violations = 0;
    shim_adc_setup_irq_disabled_violation = false;
    shim_adc_callback_irq_enabled_violation = false;
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
