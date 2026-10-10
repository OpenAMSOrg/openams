// SPDX-License-Identifier: GPL-3.0-or-later
/* Independent synthetic H723 register/timing model. No analog hardware proof.
 * Initialized ordinary paths and explicit timer-observation-driven ADC transitions.
 * Calibration and ADRDY change only in timer_read_time. W1C is applied on the next
 * observation; volatile writes are not trapped. No enable-bit write-failure model.
 * CONFIG_WANT_GPIO_LOCAL is off. Shared-GPIO composition is not covered.
 * Assertions here are outside the product catch paths.
 */
#include "internal.h"
#include "gpio.h"
#include "board/irq.h"
#include "board/misc.h"
#include "sched.h"
#include "adc_local.h"
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

ADC_TypeDef test_adcs[3];
ADC_Common_TypeDef test_common[2];
static uint32_t ticks, increment, irq_delay, reads, budget_ticks;
static unsigned locked, stopped, io, muxes, mode, calibration_reads, ready_reads;
static unsigned regulator_seen, calibration_seen, enable_seen;
static uint32_t mux_delay;
enum { COLD = 1, CAL_STUCK, READY_STUCK, SHUT_CAL, SHUT_EXACT };

uint32_t timer_from_us(uint32_t us) { return us * (CONFIG_CLOCK_FREQ / 1000000u); }
irqstatus_t irq_save(void)
{
    unsigned old = locked;
    if (!old)
        ticks += irq_delay;
    locked = 1;
    return old;
}
void irq_restore(irqstatus_t old) { locked = old; }
uint8_t sched_is_shutdown(void) { return (uint8_t)stopped; }
uint32_t timer_read_time(void)
{
    ++reads;
    ticks += increment;
    if (mode == SHUT_EXACT && !locked && reads >= 3) {
        ticks = budget_ticks;
        stopped = 1;
    }
    if (mode >= COLD && mode <= SHUT_CAL) {
        ADC_TypeDef *adc = ADC1;
        if (adc->CR & ADC_CR_ADVREGEN)
            regulator_seen = 1;
        if (adc->CR & ADC_CR_ADCAL) {
            calibration_seen = 1;
            ++calibration_reads;
            if (mode == SHUT_CAL)
                stopped = 1;
            if (mode != CAL_STUCK && mode != SHUT_CAL && calibration_reads >= 3)
                adc->CR &= ~ADC_CR_ADCAL;
        }
        /* Model the W1C ready clear on the next explicit time observation before
         * enable, then a readiness delay. Register writes themselves are not trapped. */
        if (!(adc->CR & ADC_CR_ADEN))
            adc->ISR &= ~ADC_ISR_ADRDY;
        else {
            enable_seen = 1;
            ++ready_reads;
            if (mode != READY_STUCK && ready_reads >= 3)
                adc->ISR |= ADC_ISR_ADRDY;
            else
                adc->ISR &= ~ADC_ISR_ADRDY;
        }
    }
    return ticks;
}
int is_enabled_pclock(uint32_t bit) { (void)bit; ++io; return 1; }
void enable_pclock(uint32_t bit) { (void)bit; ++io; }
void gpio_peripheral(uint32_t pin, uint32_t mode_bits, int32_t value)
{
    (void)pin;
    (void)mode_bits;
    (void)value;
    ++io;
    ++muxes;
    ticks += mux_delay;
}
void register_shutdown(void (*fn)(void)) { (void)fn; }
_Noreturn void shutdown(const char *msg)
{
    fprintf(stderr, "unexpected shutdown %s\n", msg);
    abort();
}
static void unchanged(struct gpio_adc *left, struct gpio_adc *right,
                      const struct gpio_adc *left0, const struct gpio_adc *right0)
{
    assert(!memcmp(left, left0, sizeof(*left)) && !memcmp(right, right0, sizeof(*right)));
}
int main(int argc, char **argv)
{
    assert(argc == 2);
    const char *name = argv[1];
    for (unsigned i = 0; i < 3; i++)
        test_adcs[i].CR = ADC_CR_ADEN;
    struct gpio_adc out_a, out_b;
    memset(&out_a, 0x55, sizeof(out_a));
    memset(&out_b, 0x66, sizeof(out_b));
    struct gpio_adc saved_a = out_a, saved_b = out_b;
    uint32_t timeout = 100;
    int want = 0;
    budget_ticks = timer_from_us(timeout);
    if (!strcmp(name, "legacy")) {
        fprintf(stderr, "initialized legacy trace\n");
        assert(gpio_adc_setup_local_pair(36, 37, &out_a, &out_b) == 0);
        assert(reads == 0);
        unsigned before = io;
        assert(gpio_adc_setup_local_pair(36, 37, &out_a, &out_b) == 0);
        assert(reads == 0 && io == before);
        return 0;
    }
    if (!strcmp(name, "ordinary")) {
        fprintf(stderr, "initialized ordinary trace\n");
        out_a = gpio_adc_setup(36);
        assert(out_a.adc == ADC1 && out_a.chan == 4 && reads == 0);
        return 0;
    }
    if (!strcmp(name, "zero")) {
        timeout = 0;
        want = GPIO_ADC_LOCAL_ERR_INVALID;
    } else if (!strcmp(name, "overflow")) {
        timeout = UINT32_MAX;
        want = GPIO_ADC_LOCAL_ERR_INVALID;
    } else if (!strcmp(name, "half-bound")) {
        timeout = (uint32_t)(((uint64_t)1 << 31) + (CONFIG_CLOCK_FREQ / 1000000u) - 1)
            / (CONFIG_CLOCK_FREQ / 1000000u);
        want = GPIO_ADC_LOCAL_ERR_INVALID;
    } else if (!strcmp(name, "irq-disabled")) {
        locked = 1;
        want = GPIO_ADC_LOCAL_ERR_INVALID;
    } else if (!strcmp(name, "ordinary-owned")) {
        fprintf(stderr, "initialized ordinary trace\n");
        (void)gpio_adc_setup(36);
        want = GPIO_ADC_LOCAL_ERR_BUSY;
    } else if (!strcmp(name, "expired-replay")) {
        assert(gpio_adc_setup_local_pair_timeout(36, 37, 100, &out_a, &out_b) == 0);
        out_a = saved_a;
        out_b = saved_b;
        irq_delay = timer_from_us(2);
        timeout = 1;
        want = GPIO_ADC_LOCAL_ERR_TIMEOUT;
    } else if (!strcmp(name, "shutdown-exact")) {
        mode = SHUT_EXACT;
        want = GPIO_ADC_LOCAL_ERR_SHUTDOWN;
    } else if (!strcmp(name, "mux-deadline")) {
        mux_delay = budget_ticks;
        want = GPIO_ADC_LOCAL_ERR_TIMEOUT;
    } else if (!strcmp(name, "control")) {
    } else if (!strcmp(name, "max-valid")) {
        uint32_t mhz = CONFIG_CLOCK_FREQ / 1000000u;
        assert(mhz && CONFIG_CLOCK_FREQ % 1000000u == 0);
        timeout = (uint32_t)(((1ull << 31) - 1ull) / mhz);
        assert((uint64_t)timeout * mhz < (1ull << 31));
        assert(((uint64_t)timeout + 1ull) * mhz >= (1ull << 31));
        fprintf(stderr, "initialized max-valid trace\n");
        want = 0;
    } else if (!strcmp(name, "nonintegral")) {
        assert(CONFIG_CLOCK_FREQ % 1000000u);
        timeout = 100;
        want = GPIO_ADC_LOCAL_ERR_INVALID;
    } else {
        ADC1->CR = 0;
        increment = timer_from_us(1);
        mode = COLD;
        if (!strcmp(name, "regulator-timeout")) {
            timeout = 12;
            want = GPIO_ADC_LOCAL_ERR_TIMEOUT;
        } else if (!strcmp(name, "calibration-timeout")) {
            mode = CAL_STUCK;
            want = GPIO_ADC_LOCAL_ERR_TIMEOUT;
        } else if (!strcmp(name, "ready-timeout")) {
            mode = READY_STUCK;
            want = GPIO_ADC_LOCAL_ERR_TIMEOUT;
        } else if (!strcmp(name, "shutdown-calibration")) {
            mode = SHUT_CAL;
            want = GPIO_ADC_LOCAL_ERR_SHUTDOWN;
        } else if (!strcmp(name, "wrap"))
            ticks = UINT32_MAX - timer_from_us(10);
        else
            assert(!strcmp(name, "cold"));
    }
    uint32_t cr_before[3] = {test_adcs[0].CR, test_adcs[1].CR, test_adcs[2].CR};
    unsigned before_io = io;
    int got = gpio_adc_setup_local_pair_timeout(36, 37, timeout, &out_a, &out_b);
    fprintf(stderr, "case=%s result=%d expected=%d reads=%u io=%u mux=%u reg=%u cal=%u enable=%u\n",
            name, got, want, reads, io, muxes, regulator_seen, calibration_seen, enable_seen);
    assert(got == want);
    if (want)
        unchanged(&out_a, &out_b, &saved_a, &saved_b);
    else
        assert(out_a.adc == ADC1 && out_a.chan == 4 && out_b.adc == ADC1 && out_b.chan == 8);
    if (!strcmp(name, "calibration-timeout"))
        assert(calibration_seen && !enable_seen && !muxes);
    if (!strcmp(name, "ready-timeout"))
        assert(calibration_seen && enable_seen && !muxes);
    if (!strcmp(name, "regulator-timeout"))
        assert(regulator_seen && !calibration_seen && !muxes);
    if (!strcmp(name, "cold") || !strcmp(name, "wrap"))
        assert(regulator_seen && calibration_seen && enable_seen && muxes == 2);
    if (!strcmp(name, "zero") || !strcmp(name, "overflow") || !strcmp(name, "half-bound"))
        assert(!reads && !io);
    if (!strcmp(name, "irq-disabled"))
        assert(locked && io == before_io);
    if (!strcmp(name, "ordinary-owned"))
        assert(io == before_io);
    if (!strcmp(name, "mux-deadline"))
        assert(muxes == 1);
    if (want == GPIO_ADC_LOCAL_ERR_TIMEOUT && strcmp(name, "expired-replay")) {
        unsigned prior = io;
        increment = 0;
        mux_delay = 0;
        assert(gpio_adc_setup_local_pair_timeout(36, 37, 1000, &out_a, &out_b) == GPIO_ADC_LOCAL_ERR_BUSY);
        assert(io == prior);
        unchanged(&out_a, &out_b, &saved_a, &saved_b);
    }
    if (!strcmp(name, "nonintegral")) {
        assert(!reads && !io && !muxes);
        assert(test_adcs[0].CR == cr_before[0] && test_adcs[1].CR == cr_before[1]
               && test_adcs[2].CR == cr_before[2]);
        fprintf(stderr, "initialized ordinary trace\n");
        struct gpio_adc ordinary = gpio_adc_setup(36);
        assert(ordinary.adc == ADC1 && ordinary.chan == 4);
        fprintf(stderr, "initialized legacy trace\n");
        struct gpio_adc legacy_a, legacy_b;
        memset(&legacy_a, 0x11, sizeof legacy_a);
        memset(&legacy_b, 0x22, sizeof legacy_b);
        struct gpio_adc legacy_a0 = legacy_a, legacy_b0 = legacy_b;
        unsigned io_at_legacy = io;
        assert(gpio_adc_setup_local_pair(36, 37, &legacy_a, &legacy_b) == GPIO_ADC_LOCAL_ERR_BUSY);
        assert(io == io_at_legacy);
        unchanged(&legacy_a, &legacy_b, &legacy_a0, &legacy_b0);
    }
    return 0;
}
