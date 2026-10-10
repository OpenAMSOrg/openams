// SPDX-License-Identifier: GPL-3.0-or-later
/* Native behavioral model for the shared STM32 hard_pwm paired-output API.
 * Each named case runs in a new process: driver static claims are deliberately
 * never reset by the test.  A UG copies preload CCRs to active CCRs; selecting
 * AF therefore checks the active compare and the already driven GPIO level.
 */
#include "autoconf.h"
#include "gpio.h"
#include "pwm.h"
#include "internal.h"
#include "board/irq.h"
#include <assert.h>
#include <setjmp.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

TIM_TypeDef pair_test_timers[18];
struct timer_model {
    unsigned clocked, reads, writes, ug, reserved;
    unsigned stale_uif, missing_uif;
    uint32_t preload[4], active[4];
};
static struct timer_model tm[18];
static unsigned pin_level[256], pin_was_gpio[256], pin_was_af[256];
static unsigned mux_count, irq_disabled, scheduler_shutdown, force_off_count, pclock_queries;
static unsigned external_enable = 1;
static int callback_action;
static unsigned clock_hook_count;
static jmp_buf shutdown_jmp;
static const char *shutdown_reason;

static unsigned timer_index(const void *address)
{
    uintptr_t p = (uintptr_t)address;
    for (unsigned i = 0; i < 18; ++i) {
        uintptr_t base = (uintptr_t)&pair_test_timers[i];
        if (p >= base && p < base + sizeof(pair_test_timers[i]))
            return i;
    }
    return 99;
}

static unsigned channel_index(const void *address, unsigned ti)
{
    TIM_TypeDef *t = &pair_test_timers[ti];
    if (address == (const void *)&t->CCR1) return 0;
    if (address == (const void *)&t->CCR2) return 1;
    if (address == (const void *)&t->CCR3) return 2;
    if (address == (const void *)&t->CCR4) return 3;
    return 99;
}

static void count_access(unsigned ti, const void *address, int write)
{
    if (ti >= 18) return;
    if (write) tm[ti].writes++; else tm[ti].reads++;
    assert(tm[ti].clocked && "pair driver touched an unclocked timer");
    if (ti == 12 && address != (const void *)&pair_test_timers[ti].CR1
        && address != (const void *)&pair_test_timers[ti].CR2
        && address != (const void *)&pair_test_timers[ti].SMCR
        && address != (const void *)&pair_test_timers[ti].DIER
        && address != (const void *)&pair_test_timers[ti].SR
        && address != (const void *)&pair_test_timers[ti].EGR
        && address != (const void *)&pair_test_timers[ti].CCMR1
        && address != (const void *)&pair_test_timers[ti].CCER
        && address != (const void *)&pair_test_timers[ti].CNT
        && address != (const void *)&pair_test_timers[ti].PSC
        && address != (const void *)&pair_test_timers[ti].ARR
        && address != (const void *)&pair_test_timers[ti].CCR1
        && address != (const void *)&pair_test_timers[ti].CCR2)
        tm[ti].reserved++;
}

uint32_t get_pclock_frequency(uint32_t base)
{
    unsigned ti = timer_index((const void *)(uintptr_t)base);
    /* Period calculation is a clock-tree query, not a timer-register read. */
    assert(ti < 18);
    pclock_queries++;
    return 130000000;
}
int is_enabled_pclock(uint32_t base)
{
    unsigned ti = timer_index((const void *)(uintptr_t)base);
    assert(ti < 18);
    return (int)tm[ti].clocked;
}
void enable_pclock(uint32_t base)
{
    unsigned ti = timer_index((const void *)(uintptr_t)base);
    assert(ti < 18);
    tm[ti].clocked = 1;
    if (callback_action == 3 && !clock_hook_count++)
        gpio_pwm_pair_shutdown();
}

struct gpio_out gpio_out_setup(uint32_t pin, uint32_t level)
{
    assert(pin < 256);
    pin_was_gpio[pin] = 1;
    pin_level[pin] = level;
    return (struct gpio_out){0};
}

/* At AF handoff a zero PWM-mode-1 compare is inactive.  Active-low channels
 * invert that electrical level, hence inactive GPIO is high for active-low. */
void gpio_peripheral(uint32_t pin, uint32_t function, int pull_up)
{
    (void)function;
    assert(!pull_up && pin < 256);
    /* Ordinary upstream setup intentionally routes directly to AF. */
    if (!pin_was_gpio[pin]) { pin_was_af[pin] = 1; return; }
    unsigned ti = pin == 0 || pin == 1 || pin == 2 || pin == 3 ? 2
        : pin == 20 || pin == 21 ? 3 : pin == 22 || pin == 23 ? 4
        : pin == 30 || pin == 31 ? 12 : 5;
    unsigned channel = pin == 0 || pin == 20 || pin == 22 || pin == 30 ? 0
        : pin == 1 || pin == 21 || pin == 23 || pin == 31 ? 1
        : pin == 122 ? 0 : pin == 123 ? 1
        : 2;
    unsigned active_low = (pair_test_timers[ti].CCER
        & (TIM_CCER_CC1P << (4 * channel))) != 0;
    assert(tm[ti].active[channel] == 0);
    assert(pin_level[pin] == active_low);
    unsigned shift = (channel & 1) * 8;
    uint32_t ccmr = channel < 2 ? pair_test_timers[ti].CCMR1 : pair_test_timers[ti].CCMR2;
    assert(((ccmr >> shift) & 0x10070U) == 0x60U);
    assert(pair_test_timers[ti].CCER & (TIM_CCER_CC1E << (4 * channel)));
    pin_was_af[pin] = 1;
    mux_count++;
    if (callback_action == 2 && mux_count == 1)
        gpio_pwm_pair_shutdown();
}

void writel(void *address, uint32_t value)
{
    unsigned ti = timer_index(address);
    if (ti >= 18) { *(volatile uint32_t *)address = value; return; }
    count_access(ti, address, 1);
    TIM_TypeDef *t = &pair_test_timers[ti];
    unsigned ch = channel_index(address, ti);
    if (ch < 4) { tm[ti].preload[ch] = value; *(volatile uint32_t *)address = value; return; }
    if (address == (void *)&t->SR) {
        if (!tm[ti].stale_uif) t->SR = value;
        return;
    }
    if (address == (void *)&t->EGR && (value & TIM_EGR_UG)) {
        t->EGR = value; tm[ti].ug++;
        if (!tm[ti].missing_uif) {
            for (unsigned i = 0; i < 4; ++i) tm[ti].active[i] = tm[ti].preload[i];
            t->SR |= TIM_SR_UIF;
        }
        return;
    }
    *(volatile uint32_t *)address = value;
}
uint32_t readl(const void *address)
{
    unsigned ti = timer_index(address);
    if (ti < 18) count_access(ti, address, 0);
    return *(volatile const uint32_t *)address;
}
uint8_t sched_is_shutdown(void) { return (uint8_t)scheduler_shutdown; }
irqstatus_t irq_save(void) { irqstatus_t old = irq_disabled; irq_disabled = 1; return old; }
void irq_restore(irqstatus_t old) { irq_disabled = (unsigned)old; }
_Noreturn void shutdown(const char *message) { shutdown_reason = message; longjmp(shutdown_jmp, 1); }

static void raw_force_off(void *unused)
{
    (void)unused;
    force_off_count++;
    external_enable = 0;
    if (callback_action == 1)
        gpio_pwm_pair_shutdown();
}

static struct gpio_pwm_pair_config config(uint32_t forward, uint32_t reverse)
{
    return (struct gpio_pwm_pair_config){
        .forward_pin = forward, .reverse_pin = reverse, .cycle_time = 5200,
        .raw_force_off = raw_force_off,
    };
}
static struct gpio_pwm_pair *setup(uint32_t forward, uint32_t reverse)
{
    struct gpio_pwm_pair *pair = NULL;
    struct gpio_pwm_pair_config cfg = config(forward, reverse);
    assert(gpio_pwm_pair_setup(&cfg, &pair) == GPIO_PWM_PAIR_OK && pair);
    return pair;
}
static void check_applied_at_most(const struct gpio_pwm_pair *pair,
                                  uint32_t request, int forward)
{
    struct gpio_pwm_pair_info info;
    assert(gpio_pwm_pair_get_info(pair, &info) == GPIO_PWM_PAIR_OK);
    assert((forward ? info.confirmed_forward_val : info.confirmed_reverse_val) <= request);
}

static void case_setup_and_polarity(void)
{
    pair_test_timers[2].CCMR1 = 0x01000000U;
    pair_test_timers[2].CCMR2 = 0x00010000U;
    struct gpio_pwm_pair *pair = setup(0, 2);
    assert(pin_was_af[0] && pin_was_af[2] && tm[2].active[0] == 0 && tm[2].active[2] == 0);
    assert(pair_test_timers[2].CR1 & TIM_CR1_CEN);
    (void)pair;
}
static void case_active_low(void)
{
    struct gpio_pwm_pair_config cfg = config(20, 21);
    cfg.forward_active_low = 1; cfg.reverse_active_low = 1;
    struct gpio_pwm_pair *pair = NULL;
    assert(gpio_pwm_pair_setup(&cfg, &pair) == GPIO_PWM_PAIR_OK);
    assert(pin_level[20] == 1 && pin_level[21] == 1 && pin_was_af[20] && pin_was_af[21]);
    assert(pair_test_timers[3].CCER & TIM_CCER_CC1P);
    assert(pair_test_timers[3].CCER & TIM_CCER_CC2P);
}
static void case_mixed_polarity(void)
{
    struct gpio_pwm_pair_config same = config(0, 2);
    same.reverse_active_low = 1;
    struct gpio_pwm_pair *pair = NULL;
    assert(gpio_pwm_pair_setup(&same, &pair) == GPIO_PWM_PAIR_OK);
    assert(!(pair_test_timers[2].CCER & TIM_CCER_CC1P));
    assert(pair_test_timers[2].CCER & TIM_CCER_CC3P);
#if CONFIG_HARD_PWM_LOCAL_MAX_PAIRS > 1
    struct gpio_pwm_pair_config split = config(22, 122);
    split.forward_active_low = 1;
    struct gpio_pwm_pair *other = NULL;
    assert(gpio_pwm_pair_setup(&split, &other) == GPIO_PWM_PAIR_OK && other);
    assert(pair_test_timers[4].CCER & TIM_CCER_CC1P);
    assert(!(pair_test_timers[5].CCER & TIM_CCER_CC1P));
#endif
}
static void case_ack_failures(void)
{
    struct gpio_pwm_pair *pair = (void *)1;
    struct gpio_pwm_pair_config cfg = config(0, 2);
    tm[2].stale_uif = 1;
    pair_test_timers[2].SR = TIM_SR_UIF;
    assert(gpio_pwm_pair_setup(&cfg, &pair) == GPIO_PWM_PAIR_ERR_HARDWARE && pair == (void *)1);
    assert(!pin_was_af[0] && !pin_was_af[2] && force_off_count > 0);
}
static void case_missing_ack(void)
{
    struct gpio_pwm_pair *pair = NULL;
    struct gpio_pwm_pair_config cfg = config(0, 2);
    tm[2].missing_uif = 1;
    assert(gpio_pwm_pair_setup(&cfg, &pair) == GPIO_PWM_PAIR_ERR_HARDWARE && !pair);
    assert(!pin_was_af[0] && !pin_was_af[2]);
    assert(gpio_pwm_pair_setup(&cfg, &pair) == GPIO_PWM_PAIR_ERR_BUSY);
}
static void case_second_timer_ack_failure_claim(void)
{
    struct gpio_pwm_pair *pair = (void *)1;
    struct gpio_pwm_pair_config cfg = config(0, 20);
    tm[3].missing_uif = 1;
    assert(gpio_pwm_pair_setup(&cfg, &pair) == GPIO_PWM_PAIR_ERR_HARDWARE && pair == (void *)1);
    assert(!pin_was_af[0] && !pin_was_af[20]);
    tm[3].missing_uif = 0;
    assert(gpio_pwm_pair_setup(&cfg, &pair) == GPIO_PWM_PAIR_ERR_BUSY);
}
static void case_reversal_and_quantization(void)
{
    struct gpio_pwm_pair *pair = setup(0, 20);
    assert(gpio_pwm_pair_update(pair, 10000, 0) == GPIO_PWM_PAIR_OK);
    check_applied_at_most(pair, 10000, 1);
    assert(gpio_pwm_pair_update(pair, 0, 9000) == GPIO_PWM_PAIR_ERR_INVALID);
    assert(tm[2].active[0] && !tm[3].active[0]);
    assert(gpio_pwm_pair_update(pair, 0, 0) == GPIO_PWM_PAIR_OK);
    assert(!tm[2].active[0] && !tm[3].active[0]);
    assert(gpio_pwm_pair_update(pair, 0, 9000) == GPIO_PWM_PAIR_OK);
    check_applied_at_most(pair, 9000, 0);
}
static void case_endpoints_and_irq(void)
{
    struct gpio_pwm_pair *pair = setup(0, 2);
    irq_disabled = 1;
    assert(gpio_pwm_pair_update(pair, 32768, 0) == GPIO_PWM_PAIR_OK && irq_disabled == 1);
    assert(tm[2].active[0] == pair_test_timers[2].ARR + 1);
    assert(gpio_pwm_pair_update(pair, 0, 0) == GPIO_PWM_PAIR_OK && irq_disabled == 1);
    assert(tm[2].active[0] == 0 && tm[2].active[2] == 0);
}
static void case_tim12_and_ownership(void)
{
    struct gpio_pwm_pair *pair = setup(30, 31);
    assert(!tm[12].reserved);
    assert(gpio_pwm_pair_update(pair, 16384, 0) == GPIO_PWM_PAIR_OK);
    assert(!tm[12].reserved);
}
static void case_capacity_and_isolation(void)
{
    static const uint32_t pins[][2] = {{0, 1}, {20, 21}, {22, 23}, {122, 123}, {30, 31}};
    struct gpio_pwm_pair *pairs[5] = {0};
    unsigned want = CONFIG_HARD_PWM_LOCAL_MAX_PAIRS < 5 ? CONFIG_HARD_PWM_LOCAL_MAX_PAIRS : 5;
    for (unsigned i = 0; i < want; ++i) pairs[i] = setup(pins[i][0], pins[i][1]);
    unsigned extra = want < 5 ? want : 0;
    struct gpio_pwm_pair *next = NULL;
    struct gpio_pwm_pair_config cfg = config(pins[extra][0], pins[extra][1]);
    assert(gpio_pwm_pair_setup(&cfg, &next) == GPIO_PWM_PAIR_ERR_BUSY && !next);
    assert(gpio_pwm_pair_update(pairs[0], 16384, 0) == GPIO_PWM_PAIR_OK);
    for (unsigned i = 1; i < want; ++i) {
        struct gpio_pwm_pair_info info;
        assert(gpio_pwm_pair_get_info(pairs[i], &info) == GPIO_PWM_PAIR_OK);
        assert(!info.confirmed_forward_counts && !info.confirmed_reverse_counts);
    }
}
static void case_ordinary_conflict(void)
{
    (void)setup(0, 2);
    TIM_TypeDef before = pair_test_timers[2];
    if (!setjmp(shutdown_jmp)) {
        (void)gpio_pwm_setup(1, 5200, 1);
        assert(!"ordinary PWM accepted a local timer");
    }
    assert(shutdown_reason && !memcmp(&before, &pair_test_timers[2], sizeof(before)));
}
static void case_ordinary_first_conflict(void)
{
    (void)gpio_pwm_setup(0, 5200, 1);
    TIM_TypeDef before = pair_test_timers[2];
    struct gpio_pwm_pair *pair = NULL;
    struct gpio_pwm_pair_config cfg = config(0, 2);
    assert(gpio_pwm_pair_setup(&cfg, &pair) == GPIO_PWM_PAIR_ERR_BUSY && !pair);
    assert(!memcmp(&before, &pair_test_timers[2], sizeof(before)));
}
static void case_parameters_and_pointer(void)
{
    struct gpio_pwm_pair *pair = NULL;
    TIM_TypeDef before[18]; memcpy(before, pair_test_timers, sizeof(before));
    struct gpio_pwm_pair_config cfg = config(0, 0);
    assert(gpio_pwm_pair_setup(&cfg, &pair) == GPIO_PWM_PAIR_ERR_INVALID);
    cfg = config(0x100, 2);
    assert(gpio_pwm_pair_setup(&cfg, &pair) == GPIO_PWM_PAIR_ERR_INVALID);
    cfg = config(0, 2); cfg.forward_active_low = 2;
    assert(gpio_pwm_pair_setup(&cfg, &pair) == GPIO_PWM_PAIR_ERR_INVALID);
    cfg = config(0, 2); cfg.cycle_time = 0;
    assert(gpio_pwm_pair_setup(&cfg, &pair) == GPIO_PWM_PAIR_ERR_INVALID);
    assert(!memcmp(before, pair_test_timers, sizeof(before)));
    cfg = config(0, 5); /* same TIM2 channel alias */
    assert(gpio_pwm_pair_setup(&cfg, &pair) == GPIO_PWM_PAIR_ERR_INVALID);
    cfg = config(8, 2); /* unsupported TIM1 */
    assert(gpio_pwm_pair_setup(&cfg, &pair) == GPIO_PWM_PAIR_ERR_NOT_SUPPORTED);
    cfg = config(0, 2); cfg.raw_force_off = NULL;
    assert(gpio_pwm_pair_setup(&cfg, &pair) == GPIO_PWM_PAIR_ERR_INVALID);
    int unrelated = 0;
    assert(gpio_pwm_pair_update(NULL, 0, 0) == GPIO_PWM_PAIR_ERR_INVALID);
    assert(gpio_pwm_pair_update((struct gpio_pwm_pair *)&unrelated, 0, 0) == GPIO_PWM_PAIR_ERR_INVALID);
    pair = setup(0, 2);
    assert(gpio_pwm_pair_update(pair, 32769, 0) == GPIO_PWM_PAIR_ERR_INVALID);
    assert(gpio_pwm_pair_update(pair, 1, 1) == GPIO_PWM_PAIR_ERR_INVALID);
    assert(gpio_pwm_pair_update((struct gpio_pwm_pair *)((char *)pair + 1), 0, 0)
           == GPIO_PWM_PAIR_ERR_INVALID);
}
static void case_update_fault_latches(void)
{
    struct gpio_pwm_pair *pair = setup(0, 2);
    tm[2].missing_uif = 1;
    assert(gpio_pwm_pair_update(pair, 5000, 0) == GPIO_PWM_PAIR_ERR_HARDWARE);
    assert(!gpio_pwm_pair_is_ready(pair) && force_off_count >= 2);
    tm[2].missing_uif = 0;
    assert(gpio_pwm_pair_update(pair, 1, 0) == GPIO_PWM_PAIR_ERR_SHUTDOWN);
}
static void case_subunit_reversal(void)
{
    struct gpio_pwm_pair_config cfg = config(0, 20);
    cfg.cycle_time = 131070; /* fixture pclock divisor 2 -> 65535 ticks */
    struct gpio_pwm_pair *pair = NULL;
    assert(gpio_pwm_pair_setup(&cfg, &pair) == GPIO_PWM_PAIR_OK);
    assert(gpio_pwm_pair_update(pair, 2, 0) == GPIO_PWM_PAIR_OK);
    assert(tm[2].active[0] == 3 && tm[3].active[0] == 0);
    assert(gpio_pwm_pair_update(pair, 1, 0) == GPIO_PWM_PAIR_OK);
    /* The public normalized value floors to zero, but one timer tick is live. */
    assert(tm[2].active[0] == 1 && tm[3].active[0] == 0);
    assert(gpio_pwm_pair_update(pair, 0, 2) == GPIO_PWM_PAIR_ERR_INVALID);
    assert(tm[2].active[0] == 1 && tm[3].active[0] == 0);
    assert(gpio_pwm_pair_update(pair, 0, 0) == GPIO_PWM_PAIR_OK);
    assert(tm[2].active[0] == 0);
    assert(gpio_pwm_pair_update(pair, 0, 2) == GPIO_PWM_PAIR_OK);
    assert(tm[3].active[0] == 3);
}
static void case_raw_and_shutdown(void)
{
    struct gpio_pwm_pair *pair = setup(0, 2);
    assert(gpio_pwm_pair_update(pair, 1000, 0) == GPIO_PWM_PAIR_OK);
    gpio_pwm_pair_raw_disable(pair);
    assert(!gpio_pwm_pair_is_ready(pair));
    assert(!external_enable && tm[2].preload[0] == 0 && tm[2].preload[2] == 0);
    assert(!(pair_test_timers[2].CCER & (TIM_CCER_CC1E | TIM_CCER_CC3E)));
    unsigned reads = tm[2].reads;
    unsigned clocks = pclock_queries;
    scheduler_shutdown = 0;
    assert(gpio_pwm_pair_update(pair, 0, 0) == GPIO_PWM_PAIR_ERR_SHUTDOWN);
    assert(tm[2].reads == reads);
    assert(pclock_queries == clocks);
}
static void case_shutdown_callback_stages(void)
{
    struct gpio_pwm_pair *pair = (void *)1;
    struct gpio_pwm_pair_config cfg = config(0, 2);
    callback_action = 1;
    assert(gpio_pwm_pair_setup(&cfg, &pair) == GPIO_PWM_PAIR_ERR_SHUTDOWN && pair == (void *)1);
    assert(!tm[2].clocked && !(pair_test_timers[2].CR1 & TIM_CR1_CEN));
}
static void case_shutdown_during_af(void)
{
    struct gpio_pwm_pair *pair = (void *)1;
    struct gpio_pwm_pair_config cfg = config(0, 2);
    callback_action = 2;
    assert(gpio_pwm_pair_setup(&cfg, &pair) == GPIO_PWM_PAIR_ERR_SHUTDOWN && pair == (void *)1);
    assert(mux_count == 1 && pin_was_af[0] && !pin_was_af[2]);
    assert(!(pair_test_timers[2].CR1 & TIM_CR1_CEN));
}
static void case_shutdown_during_clock(void)
{
    struct gpio_pwm_pair *pair = (void *)1;
    struct gpio_pwm_pair_config cfg = config(0, 2);
    callback_action = 3;
    assert(gpio_pwm_pair_setup(&cfg, &pair) == GPIO_PWM_PAIR_ERR_SHUTDOWN && pair == (void *)1);
    assert(clock_hook_count == 1 && tm[2].clocked);
    assert(!pin_was_af[0] && !pin_was_af[2] && !(pair_test_timers[2].CR1 & TIM_CR1_CEN));
}

int main(int argc, char **argv)
{
    assert(argc == 2);
    if (!strcmp(argv[1], "setup")) case_setup_and_polarity();
    else if (!strcmp(argv[1], "active_low")) case_active_low();
    else if (!strcmp(argv[1], "mixed")) case_mixed_polarity();
    else if (!strcmp(argv[1], "stale_ack")) case_ack_failures();
    else if (!strcmp(argv[1], "missing_ack")) case_missing_ack();
    else if (!strcmp(argv[1], "second_ack")) case_second_timer_ack_failure_claim();
    else if (!strcmp(argv[1], "reversal")) case_reversal_and_quantization();
    else if (!strcmp(argv[1], "endpoints")) case_endpoints_and_irq();
    else if (!strcmp(argv[1], "tim12")) case_tim12_and_ownership();
    else if (!strcmp(argv[1], "capacity")) case_capacity_and_isolation();
    else if (!strcmp(argv[1], "ordinary")) case_ordinary_conflict();
    else if (!strcmp(argv[1], "ordinary_first")) case_ordinary_first_conflict();
    else if (!strcmp(argv[1], "parameters")) case_parameters_and_pointer();
    else if (!strcmp(argv[1], "update_fault")) case_update_fault_latches();
    else if (!strcmp(argv[1], "subunit")) case_subunit_reversal();
    else if (!strcmp(argv[1], "raw")) case_raw_and_shutdown();
    else if (!strcmp(argv[1], "shutdown_callback")) case_shutdown_callback_stages();
    else if (!strcmp(argv[1], "shutdown_af")) case_shutdown_during_af();
    else if (!strcmp(argv[1], "shutdown_clock")) case_shutdown_during_clock();
    else return 2;
    puts("pass");
    return 0;
}
