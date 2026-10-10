// SPDX-License-Identifier: GPL-3.0-or-later
#include "adc.h"
#include "clock.h"
#include "autoconf.h"
#include "board/irq.h"
#include "board/misc.h"
#include "board/gpio.h"
#if defined(CONFIG_WANT_ADC_LOCAL) && CONFIG_WANT_ADC_LOCAL
#include "stm32/adc_local.h"
#endif
#include "command.h"
#include "sched.h"

/* Non-Diff builds retain the callable ADC API without its Kconfig submenu. */
#ifndef CONFIG_KLIPPER_MMU_ADC_SAMPLE_PERIOD_US
#define CONFIG_KLIPPER_MMU_ADC_SAMPLE_PERIOD_US 1000u
#endif

enum active_channel_t {
    ACTIVE_CHAN_NONE = 0,
    ACTIVE_CHAN_A    = 1,
    ACTIVE_CHAN_B    = 2,
};

static km_adc_pair_t pair;
static enum km_adc_state reader_state = KM_ADC_STATE_STOPPED;
static enum active_channel_t active_channel = ACTIVE_CHAN_NONE;
static struct timer adc_timer;

static struct gpio_adc gpio_adc_a;
static struct gpio_adc gpio_adc_b;
static bool setup_a_done = false;
static bool setup_b_done = false;
#if defined(CONFIG_WANT_ADC_LOCAL) && CONFIG_WANT_ADC_LOCAL
static uint32_t owned_pin_a;
static uint32_t owned_pin_b;
static bool owned_pins;
#endif

static km_result_t ticks_to_retry_us(uint32_t ticks, uint32_t *out_retry_us)
{
    if (ticks == 0u)
        return KM_ERR_INVALID_PARAM;
    uint64_t prod = (uint64_t)ticks * 1000000ULL;
    uint64_t freq = (uint64_t)CONFIG_CLOCK_FREQ;
    if (freq == 0ULL)
        return KM_ERR_INVALID_PARAM;
    /* 64-bit ceil conversion: never round early or produce zero */
    uint64_t us_64 = (prod + freq - 1ULL) / freq;
    if (us_64 >= 0x80000000ULL)
        return KM_ERR_INVALID_PARAM;
    *out_retry_us = (uint32_t)us_64;
    return KM_OK;
}

static void reader_hal_cancel(void *context)
{
    (void)context;
    /* Cancel only the active channel attempted by this reader */
    if (active_channel == ACTIVE_CHAN_A) {
        if (setup_a_done)
            gpio_adc_cancel_sample(gpio_adc_a);
        active_channel = ACTIVE_CHAN_NONE;
    } else if (active_channel == ACTIVE_CHAN_B) {
        if (setup_b_done)
            gpio_adc_cancel_sample(gpio_adc_b);
        active_channel = ACTIVE_CHAN_NONE;
    }
}

static km_result_t check_deadline(void)
{
    km_core_timepoint_t now;
    if (km_clock_get_time(&now) != KM_OK) {
        reader_state = KM_ADC_STATE_FAULTED;
        reader_hal_cancel(NULL);
        return KM_ERR_FAULTED;
    }
    if (km_time_after_or_eq(now.us, pair.channel_deadline_us) ||
        km_time_after_or_eq(now.us, pair.span_deadline_us)) {
        return KM_ERR_TIMEOUT;
    }
    return KM_OK;
}

static km_result_t reader_hal_poll(void *context, uint8_t channel, uint16_t *raw, uint32_t *retry_us)
{
    (void)context;
    if (channel > 1u || !raw || !retry_us)
        return KM_ERR_INVALID_PARAM;

    struct gpio_adc pin = (channel == 0u) ? gpio_adc_a : gpio_adc_b;
    enum active_channel_t this_chan = (channel == 0u) ? ACTIVE_CHAN_A : ACTIVE_CHAN_B;
    enum active_channel_t other_chan = (channel == 0u) ? ACTIVE_CHAN_B : ACTIVE_CHAN_A;

    if (active_channel == other_chan)
        return KM_ERR_CONFLICT;

    active_channel = this_chan;
    uint32_t ticks = gpio_adc_sample(pin);

    km_result_t chk = check_deadline();
    if (chk != KM_OK)
        return chk;

    if (ticks != 0u)
        return ticks_to_retry_us(ticks, retry_us) == KM_OK ? KM_ERR_BUSY : KM_ERR_INVALID_PARAM;

    /* Sample ready: read and clear active channel */
    *raw = gpio_adc_read(pin);
    active_channel = ACTIVE_CHAN_NONE;

    return check_deadline();
}

static const km_adc_pair_hal_t reader_hal = {
    .poll = reader_hal_poll,
    .cancel = reader_hal_cancel,
};

static uint_fast8_t adc_timer_event(struct timer *t)
{
    if (sched_is_shutdown() || reader_state != KM_ADC_STATE_RUNNING)
        return SF_DONE;

    km_core_timepoint_t now;
    if (km_clock_get_time(&now) != KM_OK) {
        reader_state = KM_ADC_STATE_FAULTED;
        reader_hal_cancel(NULL);
        return SF_DONE;
    }

    if (pair.state == KM_ADC_PAIR_STATE_IDLE) {
        if (km_adc_pair_begin(&pair, now.us) != KM_OK) {
            reader_state = KM_ADC_STATE_FAULTED;
            reader_hal_cancel(NULL);
            return SF_DONE;
        }
    }

    km_result_t s_res = km_adc_pair_service(&pair, now.us);
    if (s_res != KM_OK) {
        reader_state = KM_ADC_STATE_FAULTED;
        reader_hal_cancel(NULL);
        return SF_DONE;
    }

    /* Enforce deadline on completed sample if final service stepped past deadline */
    if (pair.state == KM_ADC_PAIR_STATE_IDLE && pair.current_sample.is_valid) {
        if (check_deadline() != KM_OK) {
            pair.current_sample.is_valid = false;
            pair.current_sample.result = KM_ERR_TIMEOUT;
        }
    }

    uint32_t deadline_us = 0u;
    if (pair.state != KM_ADC_PAIR_STATE_IDLE) {
        if (km_adc_pair_next_deadline(&pair, &deadline_us) != KM_OK) {
            reader_state = KM_ADC_STATE_FAULTED;
            reader_hal_cancel(NULL);
            return SF_DONE;
        }
    } else {
        /* A completed pair need not spin at the conversion retry cadence.
         * Keep the last sample available until the next bounded acquisition. */
        deadline_us = now.us + CONFIG_KLIPPER_MMU_ADC_SAMPLE_PERIOD_US;
    }

    uint32_t target_ticks = 0u;
    bool is_due = false;
    if (km_clock_deadline_ticks(deadline_us, &target_ticks, &is_due) != KM_OK) {
        reader_state = KM_ADC_STATE_FAULTED;
        reader_hal_cancel(NULL);
        return SF_DONE;
    }

    uint32_t cur = timer_read_time();
    uint32_t min_delay = timer_from_us(1u);
    if (min_delay == 0u)
        min_delay = 1u;
    if (is_due || !timer_is_before(cur, target_ticks) || timer_is_before(target_ticks, cur + min_delay)) {
        target_ticks = cur + min_delay;
    }

    t->waketime = target_ticks;
    return SF_RESCHEDULE;
}

KM_API km_result_t km_adc_start(uint32_t pin_a, uint32_t pin_b,
                                const km_adc_pair_config_t *config,
                                uint32_t timeout_us)
{
    irqstatus_t flag = irq_save();
    if (flag != 0ul) {
        irq_restore(flag);
        return KM_ERR_INVALID_PARAM;
    }
    if (sched_is_shutdown()) {
        reader_state = KM_ADC_STATE_SHUTDOWN;
        irq_restore(flag);
        return KM_ERR_FAULTED;
    }
    if (reader_state == KM_ADC_STATE_RUNNING) {
        irq_restore(flag);
        return KM_ERR_BUSY;
    }
    if (reader_state == KM_ADC_STATE_FAULTED || reader_state == KM_ADC_STATE_SHUTDOWN) {
        irq_restore(flag);
        return KM_ERR_FAULTED;
    }
    enum km_clock_state clk_st = km_clock_get_state();
    if (clk_st == KM_CLOCK_STATE_UNAVAILABLE) {
        irq_restore(flag);
        return KM_ERR_UNAVAILABLE;
    }
    if (clk_st != KM_CLOCK_STATE_READY) {
        irq_restore(flag);
        return KM_ERR_FAULTED;
    }
    if (!config) {
        irq_restore(flag);
        return KM_ERR_INVALID_PARAM;
    }
    km_adc_pair_config_t validated_cfg = *config;
    if (km_adc_pair_config_validate(&validated_cfg) != KM_OK) {
        irq_restore(flag);
        return KM_ERR_INVALID_PARAM;
    }
    if (CONFIG_KLIPPER_MMU_ADC_SAMPLE_PERIOD_US < 1000u
        || CONFIG_KLIPPER_MMU_ADC_SAMPLE_PERIOD_US > 1000000u) {
        irq_restore(flag);
        return KM_ERR_INVALID_PARAM;
    }
    if (timeout_us == 0u || timeout_us >= (1u << 31)) {
        irq_restore(flag);
        return KM_ERR_INVALID_PARAM;
    }
    irq_restore(flag);

    uint32_t budget_start_us = 0u;
#if !defined(CONFIG_WANT_ADC_LOCAL) || !CONFIG_WANT_ADC_LOCAL
    (void)pin_a;
    (void)pin_b;
    return KM_ERR_UNAVAILABLE;
#else
    if (owned_pins && (pin_a != owned_pin_a || pin_b != owned_pin_b))
        return KM_ERR_BUSY;

    /* Driver deadline is raw ticks and accepts only an integral MHz clock. */
    if ((uint64_t)CONFIG_CLOCK_FREQ < 1000000ULL
        || ((uint64_t)CONFIG_CLOCK_FREQ % 1000000ULL) != 0ULL) {
        return KM_ERR_INVALID_PARAM;
    }
    uint32_t ticks_per_us = (uint32_t)((uint64_t)CONFIG_CLOCK_FREQ / 1000000ULL);
    uint32_t max_driver_us = (uint32_t)(0x7fffffffULL / ticks_per_us);
    if (max_driver_us == 0u)
        return KM_ERR_INVALID_PARAM;

    km_core_timepoint_t budget_mark;
    if (km_clock_get_time(&budget_mark) != KM_OK) {
        reader_state = KM_ADC_STATE_FAULTED;
        return KM_ERR_FAULTED;
    }
    budget_start_us = budget_mark.us;

    uint32_t setup_now_us = 0u;
    if (km_clock_get_completion_us(&setup_now_us) != KM_OK) {
        reader_state = KM_ADC_STATE_FAULTED;
        return KM_ERR_FAULTED;
    }
    uint32_t spent_us = (uint32_t)(setup_now_us - budget_start_us);
    if (spent_us >= timeout_us)
        return KM_ERR_TIMEOUT;
    uint32_t driver_timeout_us = timeout_us - spent_us;
    if (driver_timeout_us > max_driver_us)
        driver_timeout_us = max_driver_us;

    struct gpio_adc fresh_a;
    struct gpio_adc fresh_b;
    int setup_rc = gpio_adc_setup_local_pair_timeout(pin_a, pin_b, driver_timeout_us,
                                                    &fresh_a, &fresh_b);
    if (setup_rc != GPIO_ADC_LOCAL_OK) {
        if (setup_rc == GPIO_ADC_LOCAL_ERR_BUSY)
            return KM_ERR_BUSY;
        if (setup_rc == GPIO_ADC_LOCAL_ERR_SHUTDOWN) {
            reader_state = KM_ADC_STATE_SHUTDOWN;
            return KM_ERR_FAULTED;
        }
        if (setup_rc == GPIO_ADC_LOCAL_ERR_TIMEOUT) {
            reader_state = KM_ADC_STATE_FAULTED;
            return KM_ERR_TIMEOUT;
        }
        return KM_ERR_INVALID_PARAM;
    }

    gpio_adc_a = fresh_a;
    gpio_adc_b = fresh_b;
    setup_a_done = true;
    setup_b_done = true;
    owned_pin_a = pin_a;
    owned_pin_b = pin_b;
    owned_pins = true;

    if (sched_is_shutdown()) {
        reader_state = KM_ADC_STATE_SHUTDOWN;
        return KM_ERR_FAULTED;
    }
#endif
    flag = irq_save();
    if (sched_is_shutdown() || km_clock_get_state() != KM_CLOCK_STATE_READY) {
        reader_state = KM_ADC_STATE_SHUTDOWN;
        irq_restore(flag);
        return KM_ERR_FAULTED;
    }
    km_core_timepoint_t now;
    if (km_clock_get_time(&now) != KM_OK) {
        reader_state = KM_ADC_STATE_FAULTED;
        irq_restore(flag);
        return KM_ERR_FAULTED;
    }
    if (km_adc_pair_init(&pair, &validated_cfg, &reader_hal, NULL, now.us) != KM_OK) {
        reader_state = KM_ADC_STATE_FAULTED;
        irq_restore(flag);
        return KM_ERR_FAULTED;
    }
    /* Ceiling completion time: fractional residue must not admit an expired start. */
    uint32_t done_us = 0u;
    if (km_clock_get_completion_us(&done_us) != KM_OK) {
        pair.current_sample.is_valid = false;
        pair.has_completed_sample = false;
        reader_state = KM_ADC_STATE_FAULTED;
        irq_restore(flag);
        return KM_ERR_FAULTED;
    }
    if ((uint32_t)(done_us - budget_start_us) >= timeout_us) {
        pair.current_sample.is_valid = false;
        pair.has_completed_sample = false;
        pair.state = KM_ADC_PAIR_STATE_IDLE;
        irq_restore(flag);
        return KM_ERR_TIMEOUT;
    }
    active_channel = ACTIVE_CHAN_NONE;
    adc_timer.func = adc_timer_event;
    uint32_t min_delay = timer_from_us(1u);
    if (min_delay == 0u)
        min_delay = 1u;
    adc_timer.waketime = timer_read_time() + min_delay;
    sched_add_timer(&adc_timer);
    reader_state = KM_ADC_STATE_RUNNING;
    irq_restore(flag);
    return KM_OK;
}

KM_API km_result_t km_adc_stop(void)
{
    irqstatus_t flag = irq_save();
    sched_del_timer(&adc_timer);
    reader_hal_cancel(NULL);
    pair.current_sample.is_valid = false;
    pair.has_completed_sample = false;
    pair.state = KM_ADC_PAIR_STATE_IDLE;
    if (reader_state == KM_ADC_STATE_RUNNING) {
        reader_state = KM_ADC_STATE_STOPPED;
    }
    irq_restore(flag);
    return KM_OK;
}

KM_API km_result_t km_adc_get_sample(km_adc_pair_sample_t *out_sample)
{
    km_core_timepoint_t query_time;
    return km_adc_get_sample_at(out_sample, &query_time);
}

KM_API km_result_t km_adc_get_sample_at(km_adc_pair_sample_t *out_sample,
                                        km_core_timepoint_t *query_time)
{
    if (!out_sample || !query_time)
        return KM_ERR_INVALID_PARAM;

    irqstatus_t flag = irq_save();
    if (reader_state == KM_ADC_STATE_FAULTED || reader_state == KM_ADC_STATE_SHUTDOWN ||
        sched_is_shutdown()) {
        out_sample->is_valid = false;
        irq_restore(flag);
        return KM_ERR_FAULTED;
    }
    if (reader_state == KM_ADC_STATE_STOPPED) {
        out_sample->is_valid = false;
        irq_restore(flag);
        return KM_ERR_UNAVAILABLE;
    }

    km_core_timepoint_t now;
    if (km_clock_get_time(&now) != KM_OK) {
        reader_state = KM_ADC_STATE_FAULTED;
        out_sample->is_valid = false;
        irq_restore(flag);
        return KM_ERR_FAULTED;
    }

    km_result_t res = km_adc_pair_get_sample(&pair, now.us, out_sample);
    if (res == KM_OK) *query_time = now;
    irq_restore(flag);
    return res;
}

enum km_adc_state km_adc_get_state(void)
{
    irqstatus_t flag = irq_save();
    enum km_adc_state st = reader_state;
    if (sched_is_shutdown() && st != KM_ADC_STATE_FAULTED)
        st = KM_ADC_STATE_SHUTDOWN;
    irq_restore(flag);
    return st;
}

void km_adc_shutdown(void)
{
    irqstatus_t flag = irq_save();
    sched_del_timer(&adc_timer);
    reader_hal_cancel(NULL);
    pair.current_sample.is_valid = false;
    pair.has_completed_sample = false;
    pair.state = KM_ADC_PAIR_STATE_FAULT;
    if (reader_state != KM_ADC_STATE_FAULTED)
        reader_state = KM_ADC_STATE_SHUTDOWN;
    irq_restore(flag);
}
DECL_SHUTDOWN(km_adc_shutdown);
