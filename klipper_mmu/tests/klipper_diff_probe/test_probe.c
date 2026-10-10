// SPDX-License-Identifier: GPL-3.0-or-later
#include <assert.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include "adc.h"
#include "adc_config.h"
#include "pin_config.h"
#include "command.h"
#include "clock.h"

void command_km_diff_probe_start(uint32_t *args);
void command_km_diff_probe_query(uint32_t *args);

static char reply[256];
static int shutdown_flag;
static int reserve_result;
static uint32_t reserved[2];
static unsigned reserve_count;
static unsigned starts;
static km_result_t config_result = KM_OK;
static km_result_t start_result = KM_OK;
static enum km_clock_state clock_state = KM_CLOCK_STATE_READY;
static km_result_t sample_result = KM_ERR_UNAVAILABLE;
static enum km_adc_state adc_state = KM_ADC_STATE_STOPPED;
static km_adc_pair_sample_t sample;
static uint32_t observed = 77;

int sched_is_shutdown(void) { return shutdown_flag; }
enum km_clock_state km_clock_get_state(void) { return clock_state; }
void sendf(const char *fmt, ...) {
    va_list ap;
    char encoded[256];
    const char *state_field = strstr(fmt, "%c");
    if (state_field) {
        size_t prefix = (size_t)(state_field - fmt);
        memcpy(encoded, fmt, prefix);
        strcpy(encoded + prefix, "%u");
        strcat(encoded, state_field + 2);
        fmt = encoded;
    }
    va_start(ap, fmt);
    vsnprintf(reply, sizeof(reply), fmt, ap);
    va_end(ap);
}
int gpio_local_reserve(const uint32_t *pins, uint32_t count) {
    assert(count == 2u);
    reserved[0] = pins[0]; reserved[1] = pins[1]; reserve_count++;
    return reserve_result;
}
static km_pin_config_t configured_pins;
km_result_t km_config_pins_view(const km_pin_config_t **out, km_resource_diagnostic_t *diag) {
    (void)diag;
    if (!out) return KM_ERR_INVALID_PARAM;
    if (config_result != KM_OK) return config_result;
    memset(&configured_pins, 0xff, sizeof(configured_pins));
    configured_pins.pin[KM_PIN_BUFFER_A][0] = 36u;
    configured_pins.pin[KM_PIN_BUFFER_B][0] = 37u;
    *out = &configured_pins;
    return KM_OK;
}
km_result_t km_config_pins(km_pin_config_t *out, km_resource_diagnostic_t *diag) {
    (void)diag;
    if (!out) return KM_ERR_INVALID_PARAM;
    const km_pin_config_t *view;
    km_result_t result = km_config_pins_view(&view, NULL);
    if (result == KM_OK) *out = *view;
    return result;
}
km_result_t km_config_adc_pair(km_adc_pair_config_t *out) {
    *out = (km_adc_pair_config_t){4095u, 10000u, 5000u, 20000u};
    return config_result;
}
km_result_t km_adc_start(uint32_t a, uint32_t b, const km_adc_pair_config_t *cfg, uint32_t timeout) {
    assert(a == 36u && b == 37u && cfg->max_raw == 4095u && timeout == 1000000u);
    starts++;
    if (start_result == KM_OK) adc_state = KM_ADC_STATE_RUNNING;
    return start_result;
}
km_result_t km_adc_get_sample_at(km_adc_pair_sample_t *out, km_core_timepoint_t *time) {
    if (sample_result != KM_OK) return sample_result;
    *out = sample;
    time->us = observed;
    return KM_OK;
}
enum km_adc_state km_adc_get_state(void) { return adc_state; }

static void query(unsigned challenge) {
    uint32_t args[] = {challenge};
    command_km_diff_probe_query(args);
}
static void start(void) {
    uint32_t args[] = {0};
    command_km_diff_probe_start(args);
}
static void assert_reply(const char *expected) { assert(strcmp(reply, expected) == 0); }

int main(int argc, char **argv) {
    assert(argc == 2);
    if (!strcmp(argv[1], "idle")) {
        query(7u);
        assert_reply("km_diff_probe_state challenge=7 state=0 result=-3 a_raw=0 b_raw=0 a_attempt_us=0 b_ready_us=0 observed_us=0");
    } else if (!strcmp(argv[1], "fresh")) {
        start();
        sample = (km_adc_pair_sample_t){.is_valid=true, .result=KM_OK, .raw_a=1234u,
            .raw_b=2345u, .a_attempt_us=11u, .b_complete_us=15u};
        sample_result = KM_OK;
        query(42u);
        assert_reply("km_diff_probe_state challenge=42 state=2 result=0 a_raw=1234 b_raw=2345 a_attempt_us=11 b_ready_us=15 observed_us=77");
    } else if (!strcmp(argv[1], "invalid")) {
        start();
        sample = (km_adc_pair_sample_t){.is_valid=false, .result=KM_OK, .raw_a=1234u, .raw_b=2345u};
        sample_result = KM_OK;
        query(1u);
        assert_reply("km_diff_probe_state challenge=1 state=1 result=0 a_raw=0 b_raw=0 a_attempt_us=0 b_ready_us=0 observed_us=77");
    } else if (!strcmp(argv[1], "fault")) {
        start(); sample_result = KM_ERR_FAULTED; adc_state = KM_ADC_STATE_FAULTED;
        query(2u);
        assert_reply("km_diff_probe_state challenge=2 state=3 result=-4 a_raw=0 b_raw=0 a_attempt_us=0 b_ready_us=0 observed_us=0");
    } else if (!strcmp(argv[1], "duplicate")) {
        start(); start();
        assert(starts == 1u && reserve_count == 1u);
        assert(reserved[0] == 36u && reserved[1] == 37u);
        query(3u);
        assert_reply("km_diff_probe_state challenge=3 state=1 result=-3 a_raw=0 b_raw=0 a_attempt_us=0 b_ready_us=0 observed_us=0");
    } else if (!strcmp(argv[1], "no_mutation")) {
        km_pin_config_t before;
        memset(&before, 0x5a, sizeof(before));
        km_pin_config_t after = before;
        config_result = KM_ERR_UNAVAILABLE;
        assert(km_config_pins(&after, 0) == KM_ERR_UNAVAILABLE);
        assert(memcmp(&before, &after, sizeof(before)) == 0);
        start(); query(9u);
        assert_reply("km_diff_probe_state challenge=9 state=3 result=-3 a_raw=0 b_raw=0 a_attempt_us=0 b_ready_us=0 observed_us=0");
        assert(reserve_count == 0u && starts == 0u);
    } else if (!strcmp(argv[1], "clock_retry")) {
        clock_state = KM_CLOCK_STATE_UNAVAILABLE;
        start();
        query(4u);
        assert_reply("km_diff_probe_state challenge=4 state=0 result=-3 a_raw=0 b_raw=0 a_attempt_us=0 b_ready_us=0 observed_us=0");
        assert(reserve_count == 0u && starts == 0u);
        clock_state = KM_CLOCK_STATE_READY;
        query(5u);
        assert_reply("km_diff_probe_state challenge=5 state=1 result=-3 a_raw=0 b_raw=0 a_attempt_us=0 b_ready_us=0 observed_us=0");
        start();
        assert(reserve_count == 1u && starts == 1u);
    } else {
        assert(0 && "unknown case");
    }
    return 0;
}
