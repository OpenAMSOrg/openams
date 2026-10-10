// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KLIPPER_MMU_TMC_UART_CONFIG_H
#define KLIPPER_MMU_TMC_UART_CONFIG_H

#include "klipper_mmu/types.h"
#include "klipper_mmu/tmc2209.h"
#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
    km_tmc2209_config_t tmc;
    uint32_t clock_hz;
    /* Saved internal MCU receiver pull-up enable for single-wire TMC UART.
     * True enables MCU internal pull-up; false leaves it disabled. */
    bool uart_pull_up;
} km_tmc_uart_config_t;

/* Line idle after a failed or cancelled transfer, so the chip resynchronizes
 * before a retry. One bit is 25 us at the exact 40000 baud. */
#define KM_TMC_UART_ERROR_QUIET_BITS 256u
#define KM_TMC_UART_BIT_US 25u

/* Stale bound after next_diagnostic_us for the GSTAT/DRV_STATUS pair: every
 * permitted attempt of both reads, each followed by the post-error guard. */
static inline uint64_t km_tmc_uart_diagnostic_budget_us(uint32_t transaction_timeout_us)
{
    return 2ULL * KM_TMC2209_MAX_ATTEMPTS * ((uint64_t)transaction_timeout_us
        + (uint64_t)KM_TMC_UART_ERROR_QUIET_BITS * KM_TMC_UART_BIT_US);
}

/* Pure validation for the shared Klipper transport: exact 40000 baud, integral
 * MHz clock, minimum transaction/quiet guards, and a fixed diagnostic stale
 * bound of diagnostic_interval_us + km_tmc_uart_diagnostic_budget_us(). All
 * converted deadlines must stay below the raw tick half range. Never modifies
 * config, claims pins, initializes a chip, or treats current settings as
 * measurements. */
KM_API km_result_t km_tmc_uart_config_validate(const km_tmc_uart_config_t *config);

#ifdef __cplusplus
}
#endif

#endif // KLIPPER_MMU_TMC_UART_CONFIG_H
