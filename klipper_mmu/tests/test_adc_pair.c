// SPDX-License-Identifier: GPL-3.0-or-later
// Adapted from the iteration 14 converter fixture and acceptance cases.
#include "klipper_mmu/adc_pair.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

typedef struct {
    km_adc_pair_t pair;
    uint32_t now, ready_at, delay[2], retry;
    uint16_t raw[2];
    km_result_t error[2];
    bool active, never_ready;
    uint8_t channel, expected;
    unsigned polls, cancels;
    uint32_t times[256];
    uint8_t channels[256];
} converter_t;

static km_result_t poll(void *ctx, uint8_t channel, uint16_t *raw, uint32_t *retry)
{
    converter_t *f = ctx;
    assert(channel < 2 && f->polls < 256);
    f->times[f->polls] = f->now; f->channels[f->polls++] = channel;
    if (!f->active) {
        assert(channel == f->expected);
        f->active = true; f->channel = channel;
        f->ready_at = f->now + f->delay[channel];
    }
    assert(channel == f->channel);
    if (f->error[channel] != KM_OK) return f->error[channel];
    if (f->never_ready || (int32_t)(f->now - f->ready_at) < 0) {
        *retry = f->retry;
        return KM_ERR_BUSY;
    }
    *raw = f->raw[channel]; f->active = false;
    f->expected = channel == 0 ? 1 : 0;
    return KM_OK;
}
static void cancel(void *ctx)
{
    converter_t *f = ctx; f->active = false; f->expected = 0; ++f->cancels;
}
static const km_adc_pair_hal_t hal = {poll, cancel};
static km_adc_pair_config_t config(void)
{
    km_adc_pair_config_t cfg = {4095, 1000, 1500, 2000};
    return cfg;
}
static void setup(converter_t *f, uint32_t now)
{
    memset(f, 0, sizeof(*f)); f->now = now; f->retry = 17;
    f->raw[0] = 100; f->raw[1] = 3000;
    km_adc_pair_config_t cfg = config();
    assert(km_adc_pair_init(&f->pair, &cfg, &hal, f, now) == KM_OK);
}
static void begin(converter_t *f, uint32_t now)
{
    f->now = now; unsigned before = f->polls;
    assert(km_adc_pair_begin(&f->pair, now) == KM_OK);
    assert(f->polls - before <= 1);
}
static void service(converter_t *f, uint32_t now)
{
    f->now = now; unsigned before = f->polls;
    km_result_t result = km_adc_pair_service(&f->pair, now);
    assert(result == KM_OK || result == KM_ERR_TIMEOUT || result == KM_ERR_INVALID_PARAM
           || result == KM_ERR_FAULTED);
    assert(f->polls - before <= 1);
}
static km_adc_pair_sample_t sample(converter_t *f)
{
    km_adc_pair_sample_t value;
    assert(km_adc_pair_get_sample(&f->pair, f->now, &value) == KM_OK);
    return value;
}
static void finish(converter_t *f)
{
    unsigned calls = 0, same_time = 0;
    uint32_t deadline;
    while (km_adc_pair_next_deadline(&f->pair, &deadline) == KM_OK) {
        assert(++calls < 200 && (int32_t)(deadline - f->now) >= 0);
        same_time = deadline == f->now ? same_time + 1u : 0;
        assert(same_time < 4);
        service(f, deadline);
    }
}
static void good_pair(converter_t *f, uint32_t now)
{
    begin(f, now); finish(f);
    km_adc_pair_sample_t s = sample(f);
    assert(s.is_valid && s.present_mask == 3);
}

static void check_config(const km_adc_pair_config_t *cfg, km_result_t expected)
{
    converter_t f;
    setup(&f, 100);
    f.active = true;
    assert(km_adc_pair_config_validate(cfg) == expected);
    assert(f.active && !f.polls && !f.cancels);
    assert(km_adc_pair_init(&f.pair, cfg, &hal, &f, 100) == expected);
    assert(!f.polls && f.cancels == (unsigned)(expected != KM_OK));
    assert(f.pair.state == (expected == KM_OK ? KM_ADC_PAIR_STATE_IDLE : KM_ADC_PAIR_STATE_UNINIT));
    if (expected != KM_OK) assert(!f.active);
}

int main(void)
{
    setbuf(stdout, NULL);
    converter_t f;

    check_config(NULL, KM_ERR_INVALID_PARAM);
    const km_adc_pair_config_t cases[] = {
        {0, 1, 1, 1}, {1, 1, 1, 1}, {UINT16_MAX, 1, 1, 1},
        {4095, 0, 1, 1}, {4095, 0x80000000u, 1, 1}, {4095, UINT32_MAX, 1, 1},
        {4095, 0x7fffffffu, 1, 1}, /* Total pair deadline may be tighter. */
        {4095, 1, 0, 1}, {4095, 1, 0x80000000u, 0x80000000u},
        {4095, 1, UINT32_MAX, UINT32_MAX}, {4095, 1, 0x7fffffffu, 0x7fffffffu},
        {4095, 1, 1, 0}, {4095, 1, 1, 0x80000000u}, {4095, 1, 1, UINT32_MAX},
        {4095, 1, 2, 1}
    };
    for (unsigned i = 0; i < sizeof(cases) / sizeof(cases[0]); ++i)
        check_config(&cases[i], i == 1 || i == 2 || i == 6 || i == 10 ? KM_OK : KM_ERR_INVALID_PARAM);

    /* Initialization still supports configuration and HAL stored in the pair. */
    setup(&f, 100);
    assert(km_adc_pair_init(&f.pair, &f.pair.config, &f.pair.hal, &f, 200) == KM_OK);
    good_pair(&f, 200);

    // endpoints
    setup(&f, 100);
    f.raw[0] = 0; f.raw[1] = 4095; f.delay[0] = 30; f.delay[1] = 40;
    good_pair(&f, 100);
    km_adc_pair_sample_t s = sample(&f);
    assert(s.raw_a == 0 && s.raw_b == 4095);
    assert(s.a_complete_us <= s.b_attempt_us && s.b_attempt_us <= s.b_complete_us);

    // invalid_max
    setup(&f, 100);
    km_adc_pair_config_t cfg = config(); cfg.max_raw = 0;
    unsigned old = f.cancels;
    assert(km_adc_pair_init(&f.pair, &cfg, &hal, &f, 100) == KM_ERR_INVALID_PARAM);
    assert(f.cancels == old + 1);
    assert(km_adc_pair_service(&f.pair, 100) == KM_ERR_INVALID_PARAM);

    // null_init
    setup(&f, 100);
    cfg = config(); old = f.cancels;
    assert(km_adc_pair_init(NULL, &cfg, &hal, &f, 100) == KM_ERR_INVALID_PARAM);
    assert(f.cancels == old + 1);

    // retry_half
    setup(&f, 100);
    f.never_ready = true; f.retry = 0x80000000u;
    begin(&f, 100); service(&f, 100);
    s = sample(&f);
    assert(!s.is_valid && s.result == KM_ERR_INVALID_PARAM && !f.active);
    uint32_t deadline;
    assert(km_adc_pair_next_deadline(&f.pair, &deadline) == KM_ERR_UNAVAILABLE);

    // retry_max
    setup(&f, 100);
    f.never_ready = true; f.retry = 0xFFFFFFFFu;
    begin(&f, 100); service(&f, 100);
    s = sample(&f);
    assert(!s.is_valid && s.result == KM_ERR_INVALID_PARAM && !f.active);
    assert(km_adc_pair_next_deadline(&f.pair, &deadline) == KM_ERR_UNAVAILABLE);

    // attempt_times
    setup(&f, 100);
    begin(&f, 100); service(&f, 130); service(&f, 170); finish(&f);
    s = sample(&f);
    assert(f.polls == 2);
    assert(s.a_attempt_us == f.times[0] && s.b_attempt_us == f.times[1]);

    // fresh_metadata
    setup(&f, 100);
    good_pair(&f, 100);
    f.error[0] = KM_ERR_FAULTED;
    begin(&f, 300); service(&f, 300); finish(&f);
    s = sample(&f);
    assert(!s.is_valid && s.present_mask == 0);
    assert(s.a_complete_us == 0 && s.b_attempt_us == 0 && s.b_complete_us == 0);

    // range_evidence
    setup(&f, 100);
    f.raw[1] = 5000;
    begin(&f, 100); finish(&f);
    s = sample(&f);
    assert(!s.is_valid && s.result == KM_ERR_INVALID_PARAM);
    assert(s.present_mask == 3 && s.raw_b == 5000 && s.raw_a == 100);

    // no_resurrection
    setup(&f, 100);
    good_pair(&f, 100);
    for (unsigned i = 0; i < 4; ++i) service(&f, f.now + 0x40000000u);
    s = sample(&f);
    assert(!s.is_valid);

    // busy_deadline
    setup(&f, 100);
    f.never_ready = true;
    begin(&f, 100);
    f.now = 200;
    assert(km_adc_pair_begin(&f.pair, f.now) == KM_ERR_BUSY);
    assert(km_adc_pair_next_deadline(&f.pair, &deadline) == KM_OK);
    assert((int32_t)(deadline - f.now) >= 0);
    finish(&f);
    s = sample(&f);
    assert(!s.is_valid && s.result == KM_ERR_TIMEOUT);

    // watchdog
    setup(&f, 100);
    f.never_ready = true; begin(&f, 100); service(&f, 100);
    f.never_ready = false; unsigned count = f.polls;
    service(&f, 1100);
    s = sample(&f);
    assert(f.polls == count && !s.is_valid && s.result == KM_ERR_TIMEOUT);

    // span
    setup(&f, 100);
    f.delay[0] = 800; f.delay[1] = 800; f.retry = 100;
    begin(&f, 100); finish(&f);
    s = sample(&f);
    assert(f.now == 1600 && !s.is_valid && s.result == KM_ERR_TIMEOUT);
    assert(s.present_mask == 1 && !f.active);

    // query_purity
    setup(&f, 100);
    good_pair(&f, 100);
    km_adc_pair_t saved = f.pair;
    unsigned polls = f.polls, cancels = f.cancels;
    assert(km_adc_pair_get_sample(&f.pair, 2200, &s) == KM_OK && !s.is_valid);
    assert(km_adc_pair_get_sample(&f.pair, 99, &s) == KM_OK && !s.is_valid);
    assert(km_adc_pair_get_sample(&f.pair, 100, &s) == KM_OK && s.is_valid);
    assert(memcmp(&saved, &f.pair, sizeof(saved)) == 0);
    assert(f.polls == polls && f.cancels == cancels);

    // old_while_busy
    setup(&f, 100);
    good_pair(&f, 100);
    f.never_ready = true;
    begin(&f, 2000); service(&f, 2000);
    assert(km_adc_pair_get_sample(&f.pair, 2000, &s) == KM_OK && s.is_valid);
    assert(km_adc_pair_get_sample(&f.pair, 2101, &s) == KM_OK && !s.is_valid);

    // reset_b
    setup(&f, 100);
    f.delay[1] = 100;
    begin(&f, 100); service(&f, 100); service(&f, 100);
    assert(f.active && f.channel == 1);
    assert(km_adc_pair_reset(&f.pair, 105) == KM_OK && !f.active);
    assert(km_adc_pair_get_sample(&f.pair, 105, &s) == KM_ERR_UNAVAILABLE && !s.is_valid);
    f.raw[0] = 20; f.raw[1] = 2000; f.delay[1] = 0;
    good_pair(&f, 105); s = sample(&f);
    assert(s.raw_a == 20 && s.raw_b == 2000);

    // wrap_fault
    setup(&f, UINT32_MAX - 20u);
    f.delay[0] = 10; f.delay[1] = 10;
    good_pair(&f, UINT32_MAX - 20u);
    uint32_t now = f.now;
    assert(km_adc_pair_service(&f.pair, now - 1u) == KM_ERR_FAULTED);
    assert(km_adc_pair_begin(&f.pair, now) == KM_ERR_FAULTED);
    assert(km_adc_pair_reset(&f.pair, now) == KM_OK);
    good_pair(&f, now);

    printf("All tests passed.\n");
    return 0;
}
