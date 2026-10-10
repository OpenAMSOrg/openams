// SPDX-License-Identifier: GPL-3.0-or-later
#include "adc.h"
#include "autoconf.h"
#include "clock.h"
#include "shim_klipper.h"
#include "board/irq.h"
#include "board/misc.h"
#include "klipper_mmu/adc_pair.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <assert.h>
#include <setjmp.h>
#include <unistd.h>
#include <sys/wait.h>

static km_adc_pair_config_t default_config(void)
{
    return (km_adc_pair_config_t){
        .max_raw = 4095u,
        .conversion_timeout_us = 1000u,
        .max_pair_span_us = 2500u,
        .max_sample_age_us = 5000u,
    };
}

static void init_and_ready_clock(uint32_t freq_hz)
{
    shim_reset(freq_hz, 1u, 1u, 4u);
    shim_set_ticks(10000u);
    shim_run_init();
    shim_set_board_ready(true);
    shim_run_task(); /* Advances clock adapter to READY */
    assert(km_clock_get_state() == KM_CLOCK_STATE_READY);
}

/* Helper to advance ticks to earliest timer and dispatch exactly one timer */
static void dispatch_next_timer(void)
{
    struct timer *t = shim_find_earliest_timer();
    assert(t != NULL);
    if (timer_is_before(test_shim_ticks, t->waketime)) {
        shim_set_ticks(t->waketime);
    }
    (void)shim_dispatch_timer(t);
}

/* 1. Validation before GPIO setup */
static void test_validation_before_gpio_setup(void)
{
    init_and_ready_clock(520000000u);

    /* NULL config */
    km_result_t res = km_adc_start(KM_ADC_PIN_DIFF_A, KM_ADC_PIN_DIFF_B, NULL, 100000u);
    assert(res == KM_ERR_INVALID_PARAM);
    assert(!shim_adc_setup_a_called && !shim_adc_setup_b_called);

    /* max_raw == 0 */
    km_adc_pair_config_t cfg = default_config();
    cfg.max_raw = 0;
    res = km_adc_start(KM_ADC_PIN_DIFF_A, KM_ADC_PIN_DIFF_B, &cfg, 100000u);
    assert(res == KM_ERR_INVALID_PARAM);
    assert(!shim_adc_setup_a_called && !shim_adc_setup_b_called);

    /* conversion_timeout_us == 0 */
    cfg = default_config();
    cfg.conversion_timeout_us = 0;
    res = km_adc_start(KM_ADC_PIN_DIFF_A, KM_ADC_PIN_DIFF_B, &cfg, 100000u);
    assert(res == KM_ERR_INVALID_PARAM);
    assert(!shim_adc_setup_a_called && !shim_adc_setup_b_called);

    /* conversion_timeout_us >= 0x80000000u */
    cfg = default_config();
    cfg.conversion_timeout_us = 0x80000000u;
    res = km_adc_start(KM_ADC_PIN_DIFF_A, KM_ADC_PIN_DIFF_B, &cfg, 100000u);
    assert(res == KM_ERR_INVALID_PARAM);
    assert(!shim_adc_setup_a_called && !shim_adc_setup_b_called);

    /* max_pair_span_us == 0 */
    cfg = default_config();
    cfg.max_pair_span_us = 0;
    res = km_adc_start(KM_ADC_PIN_DIFF_A, KM_ADC_PIN_DIFF_B, &cfg, 100000u);
    assert(res == KM_ERR_INVALID_PARAM);
    assert(!shim_adc_setup_a_called && !shim_adc_setup_b_called);

    /* max_sample_age_us < max_pair_span_us */
    cfg = default_config();
    cfg.max_sample_age_us = cfg.max_pair_span_us - 1u;
    res = km_adc_start(KM_ADC_PIN_DIFF_A, KM_ADC_PIN_DIFF_B, &cfg, 100000u);
    assert(res == KM_ERR_INVALID_PARAM);
    assert(!shim_adc_setup_a_called && !shim_adc_setup_b_called);

    /* Invalid pin A or B */
    cfg = default_config();
    res = km_adc_start(10u, KM_ADC_PIN_DIFF_B, &cfg, 100000u);
    assert(res == KM_ERR_INVALID_PARAM);
    assert(!shim_adc_setup_a_called && !shim_adc_setup_b_called);

    res = km_adc_start(KM_ADC_PIN_DIFF_A, 20u, &cfg, 100000u);
    assert(res == KM_ERR_INVALID_PARAM);
    assert(!shim_adc_setup_a_called && !shim_adc_setup_b_called);

    /* Start called with interrupts disabled */
    irq_disable();
    res = km_adc_start(KM_ADC_PIN_DIFF_A, KM_ADC_PIN_DIFF_B, &cfg, 100000u);
    assert(res == KM_ERR_INVALID_PARAM);
    assert(!shim_adc_setup_a_called && !shim_adc_setup_b_called);
    irq_enable();

    assert(shim_adc_irq_state_violations == 0);
}

/* 2. Deferred clock readiness */
static void test_deferred_clock_readiness(void)
{
    shim_reset(520000000u, 1u, 1u, 4u);
    shim_set_ticks(10000u);
    shim_run_init();
    assert(km_clock_get_state() == KM_CLOCK_STATE_UNAVAILABLE);

    km_adc_pair_config_t cfg = default_config();
    km_result_t res = km_adc_start(KM_ADC_PIN_DIFF_A, KM_ADC_PIN_DIFF_B, &cfg, 100000u);
    assert(res == KM_ERR_UNAVAILABLE);
    assert(!shim_adc_setup_a_called && !shim_adc_setup_b_called);

    shim_set_board_ready(true);
    shim_run_task();
    assert(km_clock_get_state() == KM_CLOCK_STATE_READY);

    res = km_adc_start(KM_ADC_PIN_DIFF_A, KM_ADC_PIN_DIFF_B, &cfg, 100000u);
    assert(res == KM_OK);
    assert(shim_adc_setup_a_called && shim_adc_setup_b_called);
    assert(km_adc_get_state() == KM_ADC_STATE_RUNNING);

    km_adc_stop();
}

/* 3. PC4/PC5 routing locks the first valid ordered pair until reset */
static void test_routing_and_ordering(void)
{
    init_and_ready_clock(520000000u);

    km_adc_pair_config_t cfg = default_config();
    km_result_t res = km_adc_start(KM_ADC_PIN_DIFF_B, KM_ADC_PIN_DIFF_A, &cfg, 100000u);
    assert(res == KM_OK);
    assert(shim_adc_setup_a_called && shim_adc_setup_b_called);
    assert(!shim_adc_setup_irq_disabled_violation);

    km_adc_stop();
    shim_adc_setup_a_called = false;
    shim_adc_setup_b_called = false;

    res = km_adc_start(KM_ADC_PIN_DIFF_A, KM_ADC_PIN_DIFF_B, &cfg, 100000u);
    assert(res == KM_ERR_BUSY);
    assert(!shim_adc_setup_a_called && !shim_adc_setup_b_called);
    assert(km_adc_get_state() == KM_ADC_STATE_STOPPED);

    res = km_adc_start(KM_ADC_PIN_DIFF_B, KM_ADC_PIN_DIFF_A, &cfg, 100000u);
    assert(res == KM_OK);
    assert(!shim_adc_setup_a_called && !shim_adc_setup_b_called);
    assert(!shim_adc_setup_irq_disabled_violation);

    km_adc_stop();
}

/* 4. Literal retry rounding and deadline assertions at 520MHz */
static void test_retry_rounding_520mhz(void)
{
    init_and_ready_clock(520000000u);

    km_adc_pair_config_t cfg = default_config();
    assert(km_adc_start(KM_ADC_PIN_DIFF_A, KM_ADC_PIN_DIFF_B, &cfg, 100000u) == KM_OK);

    /* 1 tick at 520MHz: ceil must produce exactly 1 us */
    shim_adc_delay_ticks[0] = timer_from_us(100u);
    shim_adc_sample_return_ticks[0] = 1u;

    dispatch_next_timer(); /* Poll A: busy, returns 1 tick */
    struct timer *earliest = shim_find_earliest_timer();
    assert(earliest != NULL);
    /* At 1 us start, 1 us retry produces next wake at 2 us (10000 + 2 * 520 = 11040 ticks) */
    assert(earliest->waketime == 10000u + timer_from_us(2u));

    /* 10400 ticks at 520MHz: exactly 20.0 us -> ceil must produce 20 us */
    shim_adc_sample_return_ticks[0] = 10400u;
    dispatch_next_timer(); /* Dispatches at 2 us */
    earliest = shim_find_earliest_timer();
    assert(earliest != NULL);
    /* 2 us + 20 us retry = 22 us (10000 + 22 * 520 = 21440 ticks) */
    assert(earliest->waketime == 10000u + timer_from_us(22u));

    /* 10401 ticks at 520MHz: 20.0019 us -> ceil must produce exactly 21 us */
    shim_adc_sample_return_ticks[0] = 10401u;
    dispatch_next_timer(); /* Dispatches at 22 us */
    earliest = shim_find_earliest_timer();
    assert(earliest != NULL);
    /* 22 us + 21 us retry = 43 us (10000 + 43 * 520 = 32360 ticks) */
    assert(earliest->waketime == 10000u + timer_from_us(43u));

    km_adc_stop();
}

/* 5. Fractional-MHz clocks are rejected before hardware setup */
static void test_fractional_mhz_clock_rejected(void)
{
    init_and_ready_clock(12288000u);
    int timers_before = shim_active_timer_count();
    assert(timers_before >= 1);

    km_adc_pair_config_t cfg = default_config();
    assert(km_adc_start(KM_ADC_PIN_DIFF_A, KM_ADC_PIN_DIFF_B, &cfg, 100000u)
           == KM_ERR_INVALID_PARAM);
    assert(km_adc_get_state() == KM_ADC_STATE_STOPPED);
    assert(!shim_adc_setup_a_called && !shim_adc_setup_b_called);
    assert(shim_active_timer_count() == timers_before);
    assert(!test_shim_irq_is_disabled());

    km_adc_pair_sample_t sample;
    memset(&sample, 0, sizeof(sample));
    sample.is_valid = true;
    assert(km_adc_get_sample(&sample) == KM_ERR_UNAVAILABLE);
    assert(!sample.is_valid);
}

/* 5b. Retry rounding at integer 12 MHz (portable ceil math, accepted clock) */
static void test_retry_rounding_12mhz(void)
{
    init_and_ready_clock(12000000u);

    km_adc_pair_config_t cfg = default_config();
    assert(km_adc_start(KM_ADC_PIN_DIFF_A, KM_ADC_PIN_DIFF_B, &cfg, 100000u) == KM_OK);

    /* 12 ticks at 12 MHz is exactly 1 us */
    shim_adc_delay_ticks[0] = 100000u;
    shim_adc_sample_return_ticks[0] = 12u;
    dispatch_next_timer();
    struct timer *earliest = shim_find_earliest_timer();
    assert(earliest != NULL);
    assert(test_shim_ticks == 10000u + timer_from_us(1u));
    assert(earliest->waketime == 10000u + timer_from_us(2u));

    /* 13 ticks at 12 MHz is 1.083 us -> ceil 2 us; now is 2 us so wake is 4 us */
    shim_adc_sample_return_ticks[0] = 13u;
    dispatch_next_timer();
    earliest = shim_find_earliest_timer();
    assert(earliest != NULL);
    assert(test_shim_ticks == 10000u + timer_from_us(2u));
    assert(earliest->waketime == 10000u + timer_from_us(4u));

    /* 247 ticks at 12 MHz is 20.583 us -> ceil 21 us; 4 us + 21 us = 25 us */
    shim_adc_sample_return_ticks[0] = 247u;
    dispatch_next_timer();
    earliest = shim_find_earliest_timer();
    assert(earliest != NULL);
    assert(test_shim_ticks == 10000u + timer_from_us(4u));
    assert(earliest->waketime == 10000u + timer_from_us(25u));

    km_adc_stop();
}

/* 6. Authoritative Reproduction Case 0: Channel A read callback overrun */
static void test_reproduction_case0_channel_a_read_overrun(void)
{
    init_and_ready_clock(520000000u);

    /* A admitted 1us, ready 81us, read takes 30us returning at 111us (>101us channel deadline) */
    km_adc_pair_config_t cfg = {4095u, 100u, 500u, 1000u};
    assert(km_adc_start(KM_ADC_PIN_DIFF_A, KM_ADC_PIN_DIFF_B, &cfg, 100000u) == KM_OK);

    shim_adc_delay_ticks[0] = timer_from_us(80u);
    shim_adc_sample_return_ticks[0] = timer_from_us(80u);
    shim_adc_read_cost_ticks[0] = timer_from_us(30u);

    dispatch_next_timer(); /* Poll A: busy at 1us, retry in 80us */
    dispatch_next_timer(); /* Poll A: ready at 81us, read takes 30us -> returns at 111us */

    km_adc_pair_sample_t sample;
    km_result_t res = km_adc_get_sample(&sample);
    assert(res == KM_OK);
    assert(!sample.is_valid);
    assert(sample.result == KM_ERR_TIMEOUT);
    /* Failed callback: raw value is absent, bit 0 cleared */
    assert(sample.raw_a == 0u);
    assert(sample.present_mask == 0u);

    km_adc_stop();
}

/* 7. Authoritative Reproduction Case 1: Delayed first Channel B poll */
static void test_reproduction_case1_delayed_first_b_poll(void)
{
    init_and_ready_clock(520000000u);

    /* A completed 1us, B deadline 101us. First B poll delayed to 100us, read takes 3us -> 103us */
    km_adc_pair_config_t cfg = {4095u, 100u, 500u, 1000u};
    assert(km_adc_start(KM_ADC_PIN_DIFF_A, KM_ADC_PIN_DIFF_B, &cfg, 100000u) == KM_OK);

    dispatch_next_timer(); /* A completes at 1us */

    /* Delay first B poll in scheduler queue until 100us */
    shim_advance_ticks(timer_from_us(99u));
    shim_adc_read_cost_ticks[1] = timer_from_us(3u);

    dispatch_next_timer(); /* B poll at 100us, read takes 3us -> completes at 103us */

    km_adc_pair_sample_t sample;
    km_result_t res = km_adc_get_sample(&sample);
    assert(res == KM_OK);
    assert(!sample.is_valid);
    assert(sample.result == KM_ERR_TIMEOUT);
    /* A's already accepted raw data and timestamps are retained */
    assert(sample.raw_a == 2048u);
    assert(sample.present_mask == 1u); /* bit 0 = A present, B absent */
    assert(sample.raw_b == 0u);
    assert(sample.a_attempt_us == 1u && sample.a_complete_us == 1u);

    km_adc_stop();
}

/* 8. Authoritative Reproduction Case 2: Channel B read duration at local timeout */
static void test_reproduction_case2_b_callback_local_timeout(void)
{
    init_and_ready_clock(520000000u);

    /* A completed 1us, B deadline 101us. B starts 2us, read takes 100us -> returns 102us */
    km_adc_pair_config_t cfg = {4095u, 100u, 500u, 1000u};
    assert(km_adc_start(KM_ADC_PIN_DIFF_A, KM_ADC_PIN_DIFF_B, &cfg, 100000u) == KM_OK);

    dispatch_next_timer(); /* A completes at 1us */

    shim_adc_read_cost_ticks[1] = timer_from_us(100u);
    dispatch_next_timer(); /* B starts at 2us, read takes 100us -> returns at 102us */

    km_adc_pair_sample_t sample;
    km_result_t res = km_adc_get_sample(&sample);
    assert(res == KM_OK);
    assert(!sample.is_valid);
    assert(sample.result == KM_ERR_TIMEOUT);

    km_adc_stop();
}

/* 9. Authoritative Reproduction Case 3: Pair span deadline equality */
static void test_reproduction_case3_pair_span_deadline_equality(void)
{
    init_and_ready_clock(520000000u);

    /* Pair starts 1us, max_pair_span 100us (span deadline 101us). B returns at 101us */
    km_adc_pair_config_t cfg = {4095u, 1000u, 100u, 1000u};
    assert(km_adc_start(KM_ADC_PIN_DIFF_A, KM_ADC_PIN_DIFF_B, &cfg, 100000u) == KM_OK);

    dispatch_next_timer(); /* A completes at 1us */

    shim_adc_read_cost_ticks[1] = timer_from_us(99u);
    dispatch_next_timer(); /* B starts at 2us, read takes 99us -> returns at 101us */

    km_adc_pair_sample_t sample;
    km_result_t res = km_adc_get_sample(&sample);
    assert(res == KM_OK);
    assert(!sample.is_valid);
    assert(sample.result == KM_ERR_TIMEOUT);

    km_adc_stop();
}

/* 10. Channel A deadline boundary: just-before (returns at 100us < 101us) */
static void test_channel_a_boundary_just_before(void)
{
    init_and_ready_clock(520000000u);
    km_adc_pair_config_t cfg = {4095u, 100u, 500u, 1000u};
    assert(km_adc_start(KM_ADC_PIN_DIFF_A, KM_ADC_PIN_DIFF_B, &cfg, 100000u) == KM_OK);

    shim_adc_delay_ticks[0] = timer_from_us(20u);
    shim_adc_sample_return_ticks[0] = timer_from_us(20u);
    shim_adc_read_cost_ticks[0] = timer_from_us(79u); /* 21us + 79us = 100us < 101us */

    dispatch_next_timer(); /* Poll A: busy, retry in 20us */
    dispatch_next_timer(); /* Poll A at 21us: ready, read finishes at 100us */
    dispatch_next_timer(); /* Poll B: completes within deadline */

    km_adc_pair_sample_t sample;
    assert(km_adc_get_sample(&sample) == KM_OK);
    assert(sample.is_valid);
    assert(sample.result == KM_OK);
    km_adc_stop();
}

/* 11. Channel A deadline boundary: equal (returns at 101us == 101us) */
static void test_channel_a_boundary_equal(void)
{
    init_and_ready_clock(520000000u);
    km_adc_pair_config_t cfg = {4095u, 100u, 500u, 1000u};
    assert(km_adc_start(KM_ADC_PIN_DIFF_A, KM_ADC_PIN_DIFF_B, &cfg, 100000u) == KM_OK);

    shim_adc_delay_ticks[0] = timer_from_us(20u);
    shim_adc_sample_return_ticks[0] = timer_from_us(20u);
    shim_adc_read_cost_ticks[0] = timer_from_us(80u); /* 21us + 80us = 101us == 101us */

    dispatch_next_timer();
    dispatch_next_timer();

    km_adc_pair_sample_t sample;
    assert(km_adc_get_sample(&sample) == KM_OK);
    assert(!sample.is_valid);
    assert(sample.result == KM_ERR_TIMEOUT);
    km_adc_stop();
}

/* 12. Channel A deadline boundary: after (returns at 102us > 101us) */
static void test_channel_a_boundary_after(void)
{
    init_and_ready_clock(520000000u);
    km_adc_pair_config_t cfg = {4095u, 100u, 500u, 1000u};
    assert(km_adc_start(KM_ADC_PIN_DIFF_A, KM_ADC_PIN_DIFF_B, &cfg, 100000u) == KM_OK);

    shim_adc_delay_ticks[0] = timer_from_us(20u);
    shim_adc_sample_return_ticks[0] = timer_from_us(20u);
    shim_adc_read_cost_ticks[0] = timer_from_us(81u); /* 21us + 81us = 102us > 101us */

    dispatch_next_timer();
    dispatch_next_timer();

    km_adc_pair_sample_t sample;
    assert(km_adc_get_sample(&sample) == KM_OK);
    assert(!sample.is_valid);
    assert(sample.result == KM_ERR_TIMEOUT);
    km_adc_stop();
}

/* 13. Channel B deadline boundary: just-before (returns at 100us < 101us) */
static void test_channel_b_boundary_just_before(void)
{
    init_and_ready_clock(520000000u);
    km_adc_pair_config_t cfg = {4095u, 100u, 500u, 1000u};
    assert(km_adc_start(KM_ADC_PIN_DIFF_A, KM_ADC_PIN_DIFF_B, &cfg, 100000u) == KM_OK);

    dispatch_next_timer(); /* A completes at 1us */
    shim_adc_read_cost_ticks[1] = timer_from_us(98u); /* 2us + 98us = 100us < 101us */
    dispatch_next_timer(); /* B completes at 100us */

    km_adc_pair_sample_t sample;
    assert(km_adc_get_sample(&sample) == KM_OK);
    assert(sample.is_valid);
    assert(sample.result == KM_OK);
    km_adc_stop();
}

/* 14. Channel B deadline boundary: equal (returns at 101us == 101us) */
static void test_channel_b_boundary_equal(void)
{
    init_and_ready_clock(520000000u);
    km_adc_pair_config_t cfg = {4095u, 100u, 500u, 1000u};
    assert(km_adc_start(KM_ADC_PIN_DIFF_A, KM_ADC_PIN_DIFF_B, &cfg, 100000u) == KM_OK);

    dispatch_next_timer(); /* A completes at 1us */
    shim_adc_read_cost_ticks[1] = timer_from_us(99u); /* 2us + 99us = 101us == 101us */
    dispatch_next_timer();

    km_adc_pair_sample_t sample;
    assert(km_adc_get_sample(&sample) == KM_OK);
    assert(!sample.is_valid);
    assert(sample.result == KM_ERR_TIMEOUT);
    km_adc_stop();
}

/* 15. Channel B deadline boundary: after (returns at 102us > 101us) */
static void test_channel_b_boundary_after(void)
{
    init_and_ready_clock(520000000u);
    km_adc_pair_config_t cfg = {4095u, 100u, 500u, 1000u};
    assert(km_adc_start(KM_ADC_PIN_DIFF_A, KM_ADC_PIN_DIFF_B, &cfg, 100000u) == KM_OK);

    dispatch_next_timer(); /* A completes at 1us */
    shim_adc_read_cost_ticks[1] = timer_from_us(100u); /* 2us + 100us = 102us > 101us */
    dispatch_next_timer();

    km_adc_pair_sample_t sample;
    assert(km_adc_get_sample(&sample) == KM_OK);
    assert(!sample.is_valid);
    assert(sample.result == KM_ERR_TIMEOUT);
    km_adc_stop();
}

/* 16. Pair span deadline boundary: just-before (completes at 200us < 201us) */
static void test_pair_span_boundary_just_before(void)
{
    init_and_ready_clock(520000000u);
    km_adc_pair_config_t cfg = {4095u, 500u, 200u, 1000u};
    assert(km_adc_start(KM_ADC_PIN_DIFF_A, KM_ADC_PIN_DIFF_B, &cfg, 100000u) == KM_OK);

    shim_adc_read_cost_ticks[0] = timer_from_us(49u); /* A completes at 50us */
    dispatch_next_timer();
    shim_adc_read_cost_ticks[1] = timer_from_us(149u); /* B starts 51us + 149us = 200us < 201us */
    dispatch_next_timer();

    km_adc_pair_sample_t sample;
    assert(km_adc_get_sample(&sample) == KM_OK);
    assert(sample.is_valid);
    assert(sample.result == KM_OK);
    km_adc_stop();
}

/* 17. Pair span deadline boundary: equal (completes at 201us == 201us) */
static void test_pair_span_boundary_equal(void)
{
    init_and_ready_clock(520000000u);
    km_adc_pair_config_t cfg = {4095u, 500u, 200u, 1000u};
    assert(km_adc_start(KM_ADC_PIN_DIFF_A, KM_ADC_PIN_DIFF_B, &cfg, 100000u) == KM_OK);

    shim_adc_read_cost_ticks[0] = timer_from_us(49u); /* A completes at 50us */
    dispatch_next_timer();
    shim_adc_read_cost_ticks[1] = timer_from_us(150u); /* B starts 51us + 150us = 201us == 201us */
    dispatch_next_timer();

    km_adc_pair_sample_t sample;
    assert(km_adc_get_sample(&sample) == KM_OK);
    assert(!sample.is_valid);
    assert(sample.result == KM_ERR_TIMEOUT);
    km_adc_stop();
}

/* 18. Pair span deadline boundary: after (completes at 202us > 201us) */
static void test_pair_span_boundary_after(void)
{
    init_and_ready_clock(520000000u);
    km_adc_pair_config_t cfg = {4095u, 500u, 200u, 1000u};
    assert(km_adc_start(KM_ADC_PIN_DIFF_A, KM_ADC_PIN_DIFF_B, &cfg, 100000u) == KM_OK);

    shim_adc_read_cost_ticks[0] = timer_from_us(49u); /* A completes at 50us */
    dispatch_next_timer();
    shim_adc_read_cost_ticks[1] = timer_from_us(151u); /* B starts 51us + 151us = 202us > 201us */
    dispatch_next_timer();

    km_adc_pair_sample_t sample;
    assert(km_adc_get_sample(&sample) == KM_OK);
    assert(!sample.is_valid);
    assert(sample.result == KM_ERR_TIMEOUT);
    km_adc_stop();
}

/* 13. Canceled hardware conversion on busy overrun for Channel A */
static void test_canceled_conversion_on_busy_overrun_channel_a(void)
{
    init_and_ready_clock(520000000u);

    km_adc_pair_config_t cfg = {4095u, 100u, 500u, 1000u};
    assert(km_adc_start(KM_ADC_PIN_DIFF_A, KM_ADC_PIN_DIFF_B, &cfg, 100000u) == KM_OK);

    /* Channel A busy return, but sample callback cost overruns 100us channel deadline */
    shim_adc_delay_ticks[0] = timer_from_us(500u);
    shim_adc_sample_cost_ticks[0] = timer_from_us(120u); /* 1us + 120us = 121us > 101us */

    dispatch_next_timer();

    /* Hardware conversion must be explicitly cancelled! */
    assert(shim_adc_cancel_count[0] == 1);
    assert(shim_adc_converting[0] == false);
    assert(shim_adc_cancel_count[1] == 0);

    km_adc_pair_sample_t sample;
    assert(km_adc_get_sample(&sample) == KM_OK);
    assert(!sample.is_valid);
    assert(sample.result == KM_ERR_TIMEOUT);

    km_adc_stop();
}

/* 14. Canceled hardware conversion on busy overrun for Channel B */
static void test_canceled_conversion_on_busy_overrun_channel_b(void)
{
    init_and_ready_clock(520000000u);

    km_adc_pair_config_t cfg = {4095u, 100u, 500u, 1000u};
    assert(km_adc_start(KM_ADC_PIN_DIFF_A, KM_ADC_PIN_DIFF_B, &cfg, 100000u) == KM_OK);

    shim_adc_raw_value[0] = 1800u;
    dispatch_next_timer(); /* A completes at 1us with 1800 */

    /* Channel B busy return, but sample callback cost overruns 100us budget */
    shim_adc_delay_ticks[1] = timer_from_us(500u);
    shim_adc_sample_cost_ticks[1] = timer_from_us(120u); /* 2us + 120us = 122us > 101us */

    dispatch_next_timer();

    /* Only attempted Channel B must be cancelled */
    assert(shim_adc_cancel_count[1] == 1);
    assert(shim_adc_converting[1] == false);
    assert(shim_adc_cancel_count[0] == 0);

    km_adc_pair_sample_t sample;
    assert(km_adc_get_sample(&sample) == KM_OK);
    assert(!sample.is_valid);
    assert(sample.result == KM_ERR_TIMEOUT);
    assert(sample.raw_a == 1800u);
    assert(sample.present_mask == 1u);
    assert(sample.raw_b == 0u);

    km_adc_stop();
}

/* 15. Counter wrap across raw tick and microsecond wrap */
static void test_counter_wrap(void)
{
    shim_reset(520000000u, 1u, 1u, 4u);
    shim_set_ticks(0xFFFFFF00u);
    shim_run_init();
    shim_set_board_ready(true);
    shim_run_task();

    km_adc_pair_config_t cfg = default_config();
    assert(km_adc_start(KM_ADC_PIN_DIFF_A, KM_ADC_PIN_DIFF_B, &cfg, 100000u) == KM_OK);

    shim_adc_raw_value[0] = 1200u;
    shim_adc_raw_value[1] = 2400u;

    dispatch_next_timer(); /* A completes */
    dispatch_next_timer(); /* B completes */

    km_adc_pair_sample_t s;
    assert(km_adc_get_sample(&s) == KM_OK);
    assert(s.is_valid);
    assert(s.raw_a == 1200u && s.raw_b == 2400u);

    km_adc_stop();
}

/* 16. Stale sample and no falsely valid sample after acquisition failure */
static void test_stale_cached_sample_and_failed_cycle_update(void)
{
    init_and_ready_clock(520000000u);

    km_adc_pair_config_t cfg = default_config();
    cfg.conversion_timeout_us = 500u;
    cfg.max_pair_span_us = 1000u;
    cfg.max_sample_age_us = 2000u;
    assert(km_adc_start(KM_ADC_PIN_DIFF_A, KM_ADC_PIN_DIFF_B, &cfg, 100000u) == KM_OK);

    shim_adc_raw_value[0] = 1500u;
    shim_adc_raw_value[1] = 2500u;

    dispatch_next_timer(); /* A completes */
    dispatch_next_timer(); /* B completes */

    km_adc_pair_sample_t s;
    assert(km_adc_get_sample(&s) == KM_OK);
    assert(s.is_valid);
    assert(s.raw_a == 1500u && s.raw_b == 2500u);
    uint32_t original_attempt_a = s.a_attempt_us;

    /* Advance time past max_sample_age_us */
    shim_advance_ticks(timer_from_us(3000u));
    assert(km_adc_get_sample(&s) == KM_OK);
    assert(!s.is_valid); /* Stale! */
    assert(s.a_attempt_us == original_attempt_a);

    /* Run next cycle which overruns and fails with timeout */
    shim_adc_read_cost_ticks[0] = timer_from_us(600u); /* Overruns 500us timeout */
    /* The next pair begins after the new inter-pair delay. An in-progress
     * conversion may require one more callback before its read overruns. */
    for (unsigned i = 0; i < 3u; ++i) {
        dispatch_next_timer();
        assert(km_adc_get_sample(&s) == KM_OK);
        if (s.result == KM_ERR_TIMEOUT && s.a_attempt_us != original_attempt_a)
            break;
    }

    /* Cached sample MUST NOT be falsely valid! */
    assert(km_adc_get_sample(&s) == KM_OK);
    assert(!s.is_valid);
    assert(s.result == KM_ERR_TIMEOUT);

    km_adc_stop();
}

/* 17. Raw invalidity exceeding max_raw */
static void test_raw_invalidity(void)
{
    init_and_ready_clock(520000000u);

    km_adc_pair_config_t cfg = default_config();
    cfg.max_raw = 4095u;
    assert(km_adc_start(KM_ADC_PIN_DIFF_A, KM_ADC_PIN_DIFF_B, &cfg, 100000u) == KM_OK);

    /* Return out-of-range value 4096 on Channel A */
    shim_adc_raw_value[0] = 4096u;
    dispatch_next_timer();

    km_adc_pair_sample_t s;
    assert(km_adc_get_sample(&s) == KM_OK);
    assert(!s.is_valid);
    assert(s.result == KM_ERR_INVALID_PARAM);

    /* Channel B out of range */
    shim_adc_raw_value[0] = 2000u;
    shim_adc_raw_value[1] = 4096u;
    dispatch_next_timer(); /* A completes with 2000 */
    dispatch_next_timer(); /* B completes with 4096 -> fails */

    assert(km_adc_get_sample(&s) == KM_OK);
    assert(!s.is_valid);
    assert(s.result == KM_ERR_INVALID_PARAM);

    km_adc_stop();
}

/* 18. Start/stop and restart policy */
static void test_start_stop_restart_policy(void)
{
    init_and_ready_clock(520000000u);

    km_adc_pair_config_t cfg = default_config();
    assert(km_adc_start(KM_ADC_PIN_DIFF_A, KM_ADC_PIN_DIFF_B, &cfg, 100000u) == KM_OK);
    assert(km_adc_get_state() == KM_ADC_STATE_RUNNING);

    /* Duplicate start rejected */
    assert(km_adc_start(KM_ADC_PIN_DIFF_A, KM_ADC_PIN_DIFF_B, &cfg, 100000u) == KM_ERR_BUSY);

    /* Normal stop */
    assert(km_adc_stop() == KM_OK);
    assert(km_adc_get_state() == KM_ADC_STATE_STOPPED);

    km_adc_pair_sample_t s;
    assert(km_adc_get_sample(&s) == KM_ERR_UNAVAILABLE);

    /* Restart after normal stop is permitted */
    assert(km_adc_start(KM_ADC_PIN_DIFF_A, KM_ADC_PIN_DIFF_B, &cfg, 100000u) == KM_OK);
    assert(km_adc_get_state() == KM_ADC_STATE_RUNNING);

    km_adc_stop();
}

/* 19. Shutdown during partial setup */
static void test_shutdown_partial_setup(void)
{
    init_and_ready_clock(520000000u);

    shim_adc_setup_fail_pin = KM_ADC_PIN_DIFF_B;
    km_adc_pair_config_t cfg = default_config();

    test_shim_use_longjmp = true;
    if (setjmp(test_shim_shutdown_jump) == 0) {
        (void)km_adc_start(KM_ADC_PIN_DIFF_A, KM_ADC_PIN_DIFF_B, &cfg, 100000u);
    }
    test_shim_use_longjmp = false;

    assert(shim_adc_setup_a_called == true);
    assert(km_adc_get_state() == KM_ADC_STATE_SHUTDOWN);
}

/* 20. Shutdown during active acquisition cancels active channel */
static void test_shutdown_during_acquisition(void)
{
    init_and_ready_clock(520000000u);
    km_adc_pair_config_t cfg = default_config();
    assert(km_adc_start(KM_ADC_PIN_DIFF_A, KM_ADC_PIN_DIFF_B, &cfg, 100000u) == KM_OK);

    shim_adc_delay_ticks[0] = timer_from_us(100u);
    dispatch_next_timer();
    assert(shim_adc_converting[0] == true);

    shutdown("Emergency stop during ADC sample");
    assert(km_adc_get_state() == KM_ADC_STATE_SHUTDOWN);
    assert(shim_adc_converting[0] == false);
    assert(shim_adc_cancel_count[0] == 1);
    assert(shim_adc_cancel_count[1] == 0);
}

/* 21. Completion clock nonlocal shutdown */
static void test_completion_clock_nonlocal_shutdown(void)
{
    init_and_ready_clock(520000000u);

    km_adc_pair_config_t cfg = default_config();
    assert(km_adc_start(KM_ADC_PIN_DIFF_A, KM_ADC_PIN_DIFF_B, &cfg, 100000u) == KM_OK);

    /* Fault during a busy sample callback, after the physical conversion starts. */
    shim_adc_delay_ticks[0] = timer_from_us(100u);
    shim_adc_sample_cost_ticks[0] = 0x80000000u;
    test_shim_use_longjmp = true;
    if (setjmp(test_shim_shutdown_jump) == 0) {
        dispatch_next_timer();
        assert(false); /* Completion observation must not return normally. */
    }
    test_shim_use_longjmp = false;

    assert(test_shim_is_shutdown);
    assert(shim_adc_sample_count[0] == 1 && shim_adc_read_count[0] == 0);
    assert(shim_adc_cancel_count[0] == 1 && shim_adc_cancel_count[1] == 0);
    assert(!shim_adc_converting[0] && !shim_adc_converting[1]);
    assert(shim_active_timer_count() == 0);
    assert(km_adc_get_state() == KM_ADC_STATE_SHUTDOWN);
    sched_clear_shutdown();
    assert(km_adc_start(KM_ADC_PIN_DIFF_A, KM_ADC_PIN_DIFF_B, &cfg, 100000u) == KM_ERR_FAULTED);
}

/* 22. No automatic resume after clear_shutdown */
static void test_no_automatic_resume(void)
{
    init_and_ready_clock(520000000u);

    km_adc_pair_config_t cfg = default_config();
    assert(km_adc_start(KM_ADC_PIN_DIFF_A, KM_ADC_PIN_DIFF_B, &cfg, 100000u) == KM_OK);

    shutdown("Test shutdown");
    assert(km_adc_get_state() == KM_ADC_STATE_SHUTDOWN);

    sched_clear_shutdown();
    assert(km_adc_get_state() == KM_ADC_STATE_SHUTDOWN);
    assert(km_adc_start(KM_ADC_PIN_DIFF_A, KM_ADC_PIN_DIFF_B, &cfg, 100000u) == KM_ERR_FAULTED);
}

/* 23. Promised IRQ states for setup and callbacks */
static void test_promised_irq_states(void)
{
    init_and_ready_clock(520000000u);

    km_adc_pair_config_t cfg = default_config();
    assert(km_adc_start(KM_ADC_PIN_DIFF_A, KM_ADC_PIN_DIFF_B, &cfg, 100000u) == KM_OK);

    dispatch_next_timer(); /* A completes */
    dispatch_next_timer(); /* B completes */

    km_adc_stop();

    /* Assert no IRQ state violations occurred */
    assert(shim_adc_irq_state_violations == 0);
    assert(!shim_adc_setup_irq_disabled_violation);
    assert(!shim_adc_callback_irq_enabled_violation);
}

/* 24. Nested IRQ preservation across queries */
static void test_nested_irq_preservation(void)
{
    init_and_ready_clock(520000000u);

    km_adc_pair_config_t cfg = default_config();
    assert(km_adc_start(KM_ADC_PIN_DIFF_A, KM_ADC_PIN_DIFF_B, &cfg, 100000u) == KM_OK);

    irq_enable();
    km_adc_pair_sample_t s;
    (void)km_adc_get_sample(&s);
    assert(!test_shim_irq_is_disabled());

    irq_disable();
    (void)km_adc_get_sample(&s);
    assert(test_shim_irq_is_disabled());
    irq_enable();

    irq_disable();
    km_adc_stop();
    assert(test_shim_irq_is_disabled());
    irq_enable();
}

/* 25. No read before ready and no double consume */
static void test_no_read_before_ready_and_no_double_consume(void)
{
    init_and_ready_clock(520000000u);

    km_adc_pair_config_t cfg = default_config();
    assert(km_adc_start(KM_ADC_PIN_DIFF_A, KM_ADC_PIN_DIFF_B, &cfg, 100000u) == KM_OK);

    dispatch_next_timer(); /* A completes */
    dispatch_next_timer(); /* B completes */

    assert(shim_adc_read_count[0] == 1);
    assert(shim_adc_read_count[1] == 1);
    assert(!shim_adc_read_before_ready_error);
    assert(!shim_adc_double_read_error);
    assert(!shim_adc_b_started_before_a_consumed);
    assert(!shim_adc_cancel_inactive_error);

    km_adc_stop();
}

static void test_completed_pair_waits_before_next_acquisition(void)
{
    init_and_ready_clock(520000000u);
    km_adc_pair_config_t cfg = default_config();
    assert(km_adc_start(KM_ADC_PIN_DIFF_A, KM_ADC_PIN_DIFF_B, &cfg, 100000u) == KM_OK);

    dispatch_next_timer(); /* A completes. */
    dispatch_next_timer(); /* B completes and the first pair is retained. */
    assert(shim_adc_sample_count[0] == 1 && shim_adc_sample_count[1] == 1);
    km_adc_pair_sample_t sample;
    assert(km_adc_get_sample(&sample) == KM_OK && sample.is_valid);

    uint32_t completed_tick = timer_read_time();
    for (unsigned i = 0; i < 4u && shim_adc_sample_count[0] == 1; ++i) {
        struct timer *next = shim_find_earliest_timer();
        assert(next != NULL);
        uint32_t due = next->waketime;
        dispatch_next_timer();
        if (shim_adc_sample_count[0] == 2)
            assert((uint32_t)(due - completed_tick)
                   >= timer_from_us(CONFIG_KLIPPER_MMU_ADC_SAMPLE_PERIOD_US - 1u));
    }
    assert(shim_adc_sample_count[0] == 2 && shim_adc_sample_count[1] == 1);
    km_adc_stop();
}

static void run_isolated(void (*test_fn)(void), const char *name)
{
    printf("Running %-58s ... ", name);
    fflush(stdout);

    pid_t pid = fork();
    if (pid < 0) {
        perror("fork");
        exit(1);
    }
    if (pid == 0) {
        test_fn();
        exit(0);
    }

    int status = 0;
    waitpid(pid, &status, 0);
    if (WIFEXITED(status) && WEXITSTATUS(status) == 0) {
        printf("PASS\n");
    } else if (WIFSIGNALED(status)) {
        printf("FAIL (signal %d)\n", WTERMSIG(status));
        exit(1);
    } else {
        printf("FAIL (exit code %d)\n", WEXITSTATUS(status));
        exit(1);
    }
}

/* Advance the public hardware clock through real observations, without changing
 * private clock/pair state. Only the maintenance timer exists between trials. */
static void advance_idle_clock_to(uint64_t target_us)
{
    km_core_timepoint_t now;
    assert(km_clock_get_time(&now) == KM_OK);
    uint64_t elapsed = (uint64_t)now.ms * 1000u
        + (uint32_t)(now.us - now.ms * 1000u);
    assert(shim_active_timer_count() == 1 && target_us >= elapsed);
    struct timer *maintenance = shim_find_earliest_timer();
    while (elapsed < target_us) {
        uint64_t delta = target_us - elapsed;
        if (delta > 1000000u)
            delta = 1000000u;
        shim_advance_ticks((uint32_t)(delta * 520u));
        assert(shim_dispatch_timer(maintenance) == SF_RESCHEDULE);
        elapsed += delta;
    }
    assert(km_clock_get_time(&now) == KM_OK);
    assert(now.us == (uint32_t)target_us);
}

/* Run actual ADC callbacks just before/equal/after a deadline across us wrap. */
static void test_us_wrap_boundary_deadlines(void)
{
    init_and_ready_clock(520000000u);
    km_adc_pair_config_t cfg = {4095u, 100u, 500u, 1000u};
    for (uint32_t trial = 0; trial < 3u; ++trial) {
        advance_idle_clock_to(((uint64_t)(trial + 1u) << 32) - 16u);
        shim_adc_read_cost_ticks[1] = timer_from_us(98u + trial);
        assert(km_adc_start(KM_ADC_PIN_DIFF_A, KM_ADC_PIN_DIFF_B, &cfg, 100000u) == KM_OK);
        dispatch_next_timer(); /* A observed at fffffff1; B deadline wraps to 85. */
        dispatch_next_timer(); /* B read returns at 84, 85, or 86. */
        km_core_timepoint_t now;
        assert(km_clock_get_time(&now) == KM_OK && now.us == 84u + trial);
        km_adc_pair_sample_t sample;
        assert(km_adc_get_sample(&sample) == KM_OK);
        assert(sample.is_valid == (trial == 0u));
        assert(sample.result == (trial == 0u ? KM_OK : KM_ERR_TIMEOUT));
        assert(sample.pair_start_us == 0xfffffff1u);
        assert(sample.a_attempt_us == 0xfffffff1u);
        assert(sample.b_attempt_us == 0xfffffff2u);
        assert(sample.present_mask == (trial == 0u ? 3u : 1u));
        assert(km_adc_stop() == KM_OK);
    }
}

int main(void)
{
    printf("======================================================================\n");
    printf("  Klipper MMU STM32H723 Paired-ADC Acquisition Acceptance Tests       \n");
    printf("======================================================================\n");

    run_isolated(test_validation_before_gpio_setup, "validation_before_gpio_setup");
    run_isolated(test_deferred_clock_readiness, "deferred_clock_readiness");
    run_isolated(test_routing_and_ordering, "routing_and_ordering");
    run_isolated(test_retry_rounding_520mhz, "retry_rounding_520mhz");
    run_isolated(test_fractional_mhz_clock_rejected, "fractional_mhz_clock_rejected");
    run_isolated(test_retry_rounding_12mhz, "retry_rounding_12mhz");
    run_isolated(test_reproduction_case0_channel_a_read_overrun, "reproduction_case0_channel_a_read_overrun");
    run_isolated(test_reproduction_case1_delayed_first_b_poll, "reproduction_case1_delayed_first_b_poll");
    run_isolated(test_reproduction_case2_b_callback_local_timeout, "reproduction_case2_b_callback_local_timeout");
    run_isolated(test_reproduction_case3_pair_span_deadline_equality, "reproduction_case3_pair_span_deadline_equality");
    run_isolated(test_channel_a_boundary_just_before, "channel_a_boundary_just_before");
    run_isolated(test_channel_a_boundary_equal, "channel_a_boundary_equal");
    run_isolated(test_channel_a_boundary_after, "channel_a_boundary_after");
    run_isolated(test_channel_b_boundary_just_before, "channel_b_boundary_just_before");
    run_isolated(test_channel_b_boundary_equal, "channel_b_boundary_equal");
    run_isolated(test_channel_b_boundary_after, "channel_b_boundary_after");
    run_isolated(test_pair_span_boundary_just_before, "pair_span_boundary_just_before");
    run_isolated(test_pair_span_boundary_equal, "pair_span_boundary_equal");
    run_isolated(test_pair_span_boundary_after, "pair_span_boundary_after");
    run_isolated(test_canceled_conversion_on_busy_overrun_channel_a, "canceled_conversion_on_busy_overrun_channel_a");
    run_isolated(test_canceled_conversion_on_busy_overrun_channel_b, "canceled_conversion_on_busy_overrun_channel_b");
    run_isolated(test_counter_wrap, "counter_wrap");
    run_isolated(test_us_wrap_boundary_deadlines, "us_wrap_boundary_deadlines");
    run_isolated(test_stale_cached_sample_and_failed_cycle_update, "stale_cached_sample_and_failed_cycle_update");
    run_isolated(test_raw_invalidity, "raw_invalidity");
    run_isolated(test_start_stop_restart_policy, "start_stop_restart_policy");
    run_isolated(test_shutdown_partial_setup, "shutdown_partial_setup");
    run_isolated(test_shutdown_during_acquisition, "shutdown_during_acquisition");
    run_isolated(test_completion_clock_nonlocal_shutdown, "completion_clock_nonlocal_shutdown");
    run_isolated(test_no_automatic_resume, "no_automatic_resume");
    run_isolated(test_promised_irq_states, "promised_irq_states");
    run_isolated(test_nested_irq_preservation, "nested_irq_preservation");
    run_isolated(test_no_read_before_ready_and_no_double_consume, "no_read_before_ready_and_no_double_consume");
    run_isolated(test_completed_pair_waits_before_next_acquisition, "completed_pair_waits_before_next_acquisition");

    printf("======================================================================\n");
    printf("  All 34 acceptance tests passed successfully!\n");
    printf("======================================================================\n");
    return 0;
}
