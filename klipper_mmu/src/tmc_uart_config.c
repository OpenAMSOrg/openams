// SPDX-License-Identifier: GPL-3.0-or-later
#include "klipper_mmu/tmc_uart_config.h"
#include <stdint.h>
#include <limits.h>

KM_API km_result_t km_tmc_uart_config_validate(const km_tmc_uart_config_t *config)
{
    if (!config) return KM_ERR_INVALID_PARAM;
    if (km_tmc2209_config_validate(&config->tmc) != KM_OK)
        return KM_ERR_INVALID_PARAM;
    if (!config->clock_hz || (config->clock_hz % 1000000u) != 0u || (config->clock_hz % 40000u) != 0u)
        return KM_ERR_INVALID_PARAM;

    uint64_t baud = (uint64_t)config->clock_hz / 40000ULL;
    uint64_t per_us = (uint64_t)config->clock_hz / 1000000ULL;
    uint64_t transaction = (uint64_t)config->tmc.transaction_timeout_us * per_us;
    uint64_t stale = ((uint64_t)config->tmc.diagnostic_interval_us +
        km_tmc_uart_diagnostic_budget_us(config->tmc.transaction_timeout_us)) * per_us;
    uint64_t quiet_guard = (uint64_t)KM_TMC_UART_ERROR_QUIET_BITS * baud + 200ULL * per_us;

    if (baud < 16ULL || baud > (uint64_t)UINT32_MAX ||
        quiet_guard >= (uint64_t)INT32_MAX ||
        transaction >= (uint64_t)INT32_MAX ||
        stale >= (uint64_t)INT32_MAX ||
        transaction < 264ULL * baud)
        return KM_ERR_INVALID_PARAM;

    return KM_OK;
}
