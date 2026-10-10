// SPDX-License-Identifier: GPL-3.0-or-later
#include "klipper_mmu/tmc_uart_config.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include <stdint.h>
#include <limits.h>

static km_tmc_uart_config_t valid_config(uint32_t clock_hz)
{
    km_tmc_uart_config_t cfg;
    memset(&cfg, 0, sizeof(cfg));
    cfg.clock_hz = clock_hz;
    cfg.tmc.address = 0;
    cfg.tmc.sense_resistor_milliohms = 110;
    cfg.tmc.maximum_current_ma = 1500;
    cfg.tmc.run_current_ma = 500;
    cfg.tmc.hold_current_ma = 200;
    cfg.tmc.microsteps = 16;
    cfg.tmc.interpolate = true;
    cfg.tmc.stealthchop = true;
    cfg.tmc.transaction_timeout_us = 10000;
    cfg.tmc.diagnostic_interval_us = 100000;
    return cfg;
}

static void test_null_and_immutability(void)
{
    assert(km_tmc_uart_config_validate(NULL) == KM_ERR_INVALID_PARAM);

    km_tmc_uart_config_t cfg = valid_config(20000000u);
    km_tmc_uart_config_t copy = cfg;
    assert(km_tmc_uart_config_validate(&cfg) == KM_OK);
    assert(memcmp(&cfg, &copy, sizeof(cfg)) == 0);

    // Also verify when rejected that config is unmodified (no side effects)
    cfg.clock_hz = 12288000u;
    copy = cfg;
    assert(km_tmc_uart_config_validate(&cfg) == KM_ERR_INVALID_PARAM);
    assert(memcmp(&cfg, &copy, sizeof(cfg)) == 0);
    printf("PASS null_and_immutability\n");
}

static void test_clock_frequencies(void)
{
    // Valid frequencies: 1 MHz, 2 MHz, 20 MHz, 520 MHz
    const uint32_t valid_clocks[] = {1000000u, 2000000u, 20000000u, 520000000u};
    for (size_t i = 0; i < sizeof(valid_clocks) / sizeof(valid_clocks[0]); ++i) {
        km_tmc_uart_config_t cfg = valid_config(valid_clocks[i]);
        assert(km_tmc_uart_config_validate(&cfg) == KM_OK);
    }

    // Invalid non-integral or non-40kHz-divisible clocks
    const uint32_t invalid_clocks[] = {
        0u,
        39999u,
        40000u,     // divisible by 40000 but not integral MHz
        500000u,    // not integral MHz
        1000001u,   // not integral MHz, not divisible by 40000
        1020000u,   // not integral MHz
        10500000u,  // not integral MHz
        12288000u,  // 12.288 MHz audio crystal: not integral MHz
    };
    for (size_t i = 0; i < sizeof(invalid_clocks) / sizeof(invalid_clocks[0]); ++i) {
        km_tmc_uart_config_t cfg = valid_config(invalid_clocks[i]);
        assert(km_tmc_uart_config_validate(&cfg) == KM_ERR_INVALID_PARAM);
    }
    printf("PASS clock_frequencies\n");
}

static void test_minimum_264_bit_transaction(void)
{
    // Minimum transaction is 264 bit times. At 40000 baud, 1 bit = 25 us.
    // 264 * 25 us = 6600 us.
    // At each clock: transaction = timeout_us * (clock_hz / 1000000)
    //                264 * baud = 264 * (clock_hz / 40000)
    // 6600 us is the exact boundary across all valid clocks.
    const uint32_t clocks[] = {1000000u, 2000000u, 20000000u, 520000000u};
    for (size_t i = 0; i < sizeof(clocks) / sizeof(clocks[0]); ++i) {
        km_tmc_uart_config_t cfg = valid_config(clocks[i]);

        // Exact boundary: 6600 us must be valid
        cfg.tmc.transaction_timeout_us = 6600u;
        cfg.tmc.diagnostic_interval_us = 6600u;
        assert(km_tmc_uart_config_validate(&cfg) == KM_OK);

        // One microsecond below boundary: 6599 us must be rejected
        cfg.tmc.transaction_timeout_us = 6599u;
        assert(km_tmc_uart_config_validate(&cfg) == KM_ERR_INVALID_PARAM);
    }
    printf("PASS minimum_264_bit_transaction\n");
}

static void test_diag_plus_2timeout_stale_bound(void)
{
    // At 2 GHz (per_us = 2000) the stale bound
    // (diag_us + 2 * attempts * (timeout_us + 256 * 25 us)) * per_us
    // must stay below INT32_MAX (2147483647). With a 10 ms timeout and three
    // attempts the retry budget is 6 * 16400 = 98400 us.
    km_tmc_uart_config_t cfg = valid_config(2000000000u);
    cfg.tmc.transaction_timeout_us = 10000u;
    assert(km_tmc_uart_diagnostic_budget_us(10000u) == 98400u);
    cfg.tmc.diagnostic_interval_us = 975341u; // (975341 + 98400) * 2000 < INT32_MAX
    assert(km_tmc_uart_config_validate(&cfg) == KM_OK);
    cfg.tmc.diagnostic_interval_us = 975342u; // (975342 + 98400) * 2000 >= INT32_MAX
    assert(km_tmc_uart_config_validate(&cfg) == KM_ERR_INVALID_PARAM);

    // A longer timeout grows the retry budget: 6 * (30000 + 6400) = 218400 us.
    cfg.tmc.diagnostic_interval_us = 900000u;
    assert(km_tmc_uart_config_validate(&cfg) == KM_OK);
    cfg.tmc.transaction_timeout_us = 30000u;
    assert(km_tmc_uart_config_validate(&cfg) == KM_ERR_INVALID_PARAM);

    printf("PASS diag_plus_2timeout_stale_bound\n");
}

static void test_overflow_protection(void)
{
    // Test that extreme uint32_t values widen properly before additions and
    // multiplications and do not wrap or pass validation.
    km_tmc_uart_config_t cfg = valid_config(20000000u);

    // UINT32_MAX timeout
    cfg.tmc.transaction_timeout_us = UINT32_MAX;
    assert(km_tmc_uart_config_validate(&cfg) == KM_ERR_INVALID_PARAM);

    // UINT32_MAX diagnostic interval
    cfg = valid_config(20000000u);
    cfg.tmc.diagnostic_interval_us = UINT32_MAX;
    assert(km_tmc_uart_config_validate(&cfg) == KM_ERR_INVALID_PARAM);

    // Both UINT32_MAX (would overflow 32-bit addition: UINT32_MAX + 2*UINT32_MAX)
    cfg.tmc.transaction_timeout_us = UINT32_MAX;
    cfg.tmc.diagnostic_interval_us = UINT32_MAX;
    assert(km_tmc_uart_config_validate(&cfg) == KM_ERR_INVALID_PARAM);

    // Diagnostic interval near 2^31
    cfg = valid_config(20000000u);
    cfg.tmc.diagnostic_interval_us = 0x80000000u;
    assert(km_tmc_uart_config_validate(&cfg) == KM_ERR_INVALID_PARAM);

    printf("PASS overflow_protection\n");
}

static void test_tmc2209_register_rejections(void)
{
    // Invalid address (> 3)
    km_tmc_uart_config_t cfg = valid_config(20000000u);
    cfg.tmc.address = 4;
    assert(km_tmc_uart_config_validate(&cfg) == KM_ERR_INVALID_PARAM);

    // Sense resistor = 0
    cfg = valid_config(20000000u);
    cfg.tmc.sense_resistor_milliohms = 0;
    assert(km_tmc_uart_config_validate(&cfg) == KM_ERR_INVALID_PARAM);

    // Hold current > run current
    cfg = valid_config(20000000u);
    cfg.tmc.hold_current_ma = (uint16_t)(cfg.tmc.run_current_ma + 1u);
    assert(km_tmc_uart_config_validate(&cfg) == KM_ERR_INVALID_PARAM);

    // Run current > max current
    cfg = valid_config(20000000u);
    cfg.tmc.run_current_ma = (uint16_t)(cfg.tmc.maximum_current_ma + 1u);
    assert(km_tmc_uart_config_validate(&cfg) == KM_ERR_INVALID_PARAM);

    // Microsteps not power of 2
    cfg = valid_config(20000000u);
    cfg.tmc.microsteps = 3;
    assert(km_tmc_uart_config_validate(&cfg) == KM_ERR_INVALID_PARAM);

    printf("PASS tmc2209_register_rejections\n");
}

int main(void)
{
    test_null_and_immutability();
    test_clock_frequencies();
    test_minimum_264_bit_transaction();
    test_diag_plus_2timeout_stale_bound();
    test_overflow_protection();
    test_tmc2209_register_rejections();
    printf("ALL 6 TEST SUITES PASSED\n");
    return 0;
}
