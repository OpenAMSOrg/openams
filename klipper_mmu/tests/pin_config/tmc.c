// SPDX-License-Identifier: GPL-3.0-or-later
#include "tmc_config.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

int main(int argc, char **argv)
{
    km_tmc_uart_config_t out[KM_MAX_LANES], before[KM_MAX_LANES];
    memset(out, 0xa5, sizeof(out));
    memcpy(before, out, sizeof(out));
    km_result_t result = km_config_tmc(argc > 1 && !strcmp(argv[1], "null") ? NULL : out);
    if (result != KM_OK) assert(!memcmp(out, before, sizeof(out)));
    assert(km_tmc2209_config_validate(NULL) == KM_ERR_INVALID_PARAM);
    assert(km_tmc_uart_config_validate(NULL) == KM_ERR_INVALID_PARAM);
    printf("{\"result\":%d,\"lanes\":[", (int)result);
    if (result == KM_OK) {
        for (unsigned i = 0; i < KM_MAX_LANES; ++i) {
            km_tmc2209_config_t *c = &out[i].tmc;
            if (out[i].clock_hz) {
                assert(km_tmc_uart_config_validate(&out[i]) == KM_OK);
            }
            printf("%s[%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u]", i ? "," : "",
                   c->address, c->sense_resistor_milliohms, c->maximum_current_ma,
                   c->run_current_ma, c->hold_current_ma, c->microsteps,
                   (unsigned)c->interpolate, (unsigned)c->stealthchop,
                   c->transaction_timeout_us, c->diagnostic_interval_us, out[i].clock_hz,
                   (unsigned)out[i].uart_pull_up);
        }
    }
    printf("],\"selector\":");
    km_tmc_uart_config_t selector, selector_before;
    memset(&selector, 0xa5, sizeof(selector));
    memcpy(&selector_before, &selector, sizeof(selector));
    km_result_t selector_result = km_config_selector_tmc(&selector);
    if (selector_result != KM_OK) assert(!memcmp(&selector, &selector_before, sizeof(selector)));
    if (selector_result == KM_OK) {
        km_tmc2209_config_t *c = &selector.tmc;
        assert(km_tmc_uart_config_validate(&selector) == KM_OK);
        printf("{\"result\":%d,\"lane\":[%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u,%u]}",
               (int)selector_result, c->address, c->sense_resistor_milliohms,
               c->maximum_current_ma, c->run_current_ma, c->hold_current_ma,
               c->microsteps, (unsigned)c->interpolate, (unsigned)c->stealthchop,
               c->transaction_timeout_us, c->diagnostic_interval_us,
               selector.clock_hz, (unsigned)selector.uart_pull_up);
    } else {
        printf("{\"result\":%d,\"lane\":null}", (int)selector_result);
    }
    assert(km_config_selector_tmc(NULL) == KM_ERR_INVALID_PARAM);
    printf(",\"clock_validation\":[");
    if (result == KM_OK) {
        // Values that bypass Kconfig still receive the same controller check.
        km_tmc_uart_config_t invalid = out[0];
        invalid.tmc.hold_current_ma = (uint16_t)(invalid.tmc.run_current_ma + 1u);
        assert(km_tmc2209_config_validate(&invalid.tmc) == KM_ERR_INVALID_PARAM);
        assert(km_tmc_uart_config_validate(&invalid) == KM_ERR_INVALID_PARAM);
        const uint32_t frequencies[] = {0u, 1000000u, 12288000u, 2000000u, 20000000u, 520000000u};
        for (unsigned i = 0; i < sizeof(frequencies) / sizeof(frequencies[0]); ++i) {
            km_tmc_uart_config_t c = out[0];
            c.clock_hz = frequencies[i];
            km_tmc_uart_config_t saved = c;
            km_result_t valid = km_tmc_uart_config_validate(&c);
            assert(!memcmp(&c, &saved, sizeof(c)));
            printf("%s%d", i ? "," : "", (int)valid);
        }
    }
    puts("]}");
    return 0;
}
