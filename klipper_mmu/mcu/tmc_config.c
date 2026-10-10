// SPDX-License-Identifier: GPL-3.0-or-later
#include "tmc_config.h"
#include "autoconf.h"
#include "pin_config.h"
#include <string.h>

/* Saved headers from before the selector-UART option exist have none of these. */
#ifndef CONFIG_KLIPPER_MMU_SELECTOR_STEPPER
#define CONFIG_KLIPPER_MMU_SELECTOR_STEPPER 0
#endif
#ifndef CONFIG_KLIPPER_MMU_SELECTOR_UART_ADDRESS
#define CONFIG_KLIPPER_MMU_SELECTOR_UART_ADDRESS 0
#endif
#ifndef CONFIG_KLIPPER_MMU_SELECTOR_UART_PULLUP
#define CONFIG_KLIPPER_MMU_SELECTOR_UART_PULLUP 0
#endif
#ifndef CONFIG_KLIPPER_MMU_SELECTOR_RUN_CURRENT
#define CONFIG_KLIPPER_MMU_SELECTOR_RUN_CURRENT 0
#endif
#ifndef CONFIG_KLIPPER_MMU_SELECTOR_HOLD_CURRENT
#define CONFIG_KLIPPER_MMU_SELECTOR_HOLD_CURRENT 0
#endif
#ifndef CONFIG_KLIPPER_MMU_SELECTOR_MICROSTEPS
#define CONFIG_KLIPPER_MMU_SELECTOR_MICROSTEPS 16
#endif

km_result_t km_config_tmc(km_tmc_uart_config_t out[KM_MAX_LANES])
{
    if (!out) return KM_ERR_INVALID_PARAM;
#if !CONFIG_KLIPPER_MMU_PIN_CONFIG
    return KM_ERR_UNAVAILABLE;
#else
    if (!CONFIG_KLIPPER_MMU_TMC_RSENSE || !CONFIG_KLIPPER_MMU_TMC_CURRENT_CEILING)
        return KM_ERR_UNAVAILABLE;
#ifndef CONFIG_KLIPPER_MMU_TMC_UART_PULLUP
    return KM_ERR_UNAVAILABLE;
#else
    const char *pullups = CONFIG_KLIPPER_MMU_TMC_UART_PULLUP;
    if (!pullups || pullups[0] == '\0')
        return KM_ERR_UNAVAILABLE;
    const unsigned lanes = KM_GEAR_DRIVERS;
    if (!lanes || lanes > KM_MAX_LANES) return KM_ERR_INVALID_PARAM;
    const char *addresses = CONFIG_KLIPPER_MMU_TMC_ADDRESSES;
    const char *address = addresses;
    const char *pullup = pullups;
    bool lane_pullup[KM_MAX_LANES] = {false};
    uint8_t lane_address[KM_MAX_LANES] = {0};

    /* Only address and receiver pull-up differ by lane. Keep one candidate on the
     * MCU stack, validate every lane, then publish all outputs together. */
    km_tmc_uart_config_t candidate = {
        .clock_hz = CONFIG_CLOCK_FREQ,
        .tmc = {
            .sense_resistor_milliohms = CONFIG_KLIPPER_MMU_TMC_RSENSE,
            .maximum_current_ma = CONFIG_KLIPPER_MMU_TMC_CURRENT_CEILING,
            .run_current_ma = CONFIG_KLIPPER_MMU_RUN_CURRENT,
            .hold_current_ma = CONFIG_KLIPPER_MMU_HOLD_CURRENT,
            .microsteps = CONFIG_KLIPPER_MMU_TMC_MICROSTEPS,
#if CONFIG_KLIPPER_MMU_TMC_INTERPOLATE
            .interpolate = true,
#endif
#if CONFIG_KLIPPER_MMU_TMC_STEALTHCHOP
            .stealthchop = true,
#endif
            .transaction_timeout_us = CONFIG_KLIPPER_MMU_TMC_TIMEOUT_US,
            .diagnostic_interval_us = CONFIG_KLIPPER_MMU_TMC_DIAGNOSTIC_US
        }
    };
    for (unsigned lane = 0; lane < lanes; ++lane) {
        if (address[0] < '0' || address[0] > '3'
            || (lane + 1u == lanes ? address[1] != '\0' : address[1] != ','))
            return KM_ERR_INVALID_PARAM;
        if ((pullup[0] != '0' && pullup[0] != '1')
            || (lane + 1u == lanes ? pullup[1] != '\0' : pullup[1] != ','))
            return KM_ERR_INVALID_PARAM;
        lane_address[lane] = (uint8_t)(address[0] - '0');
        lane_pullup[lane] = (pullup[0] == '1');
        candidate.tmc.address = lane_address[lane];
        candidate.uart_pull_up = lane_pullup[lane];
        if (km_tmc_uart_config_validate(&candidate) != KM_OK)
            return KM_ERR_INVALID_PARAM;
        if (lane + 1u < lanes) {
            address += 2;
            pullup += 2;
        }
    }
    memset(out, 0, KM_MAX_LANES * sizeof(*out));
    for (unsigned lane = 0; lane < lanes; ++lane) {
        candidate.tmc.address = lane_address[lane];
        candidate.uart_pull_up = lane_pullup[lane];
        out[lane] = candidate;
    }
    return KM_OK;
#endif
#endif
}

km_result_t km_config_selector_tmc(km_tmc_uart_config_t *out)
{
    if (!out) return KM_ERR_INVALID_PARAM;
#if !CONFIG_KLIPPER_MMU_PIN_CONFIG || !CONFIG_KLIPPER_MMU_SELECTOR_STEPPER
    return KM_ERR_UNAVAILABLE;
#else
    if (!CONFIG_KLIPPER_MMU_TMC_RSENSE || !CONFIG_KLIPPER_MMU_TMC_CURRENT_CEILING)
        return KM_ERR_UNAVAILABLE;
    const km_tmc_uart_config_t candidate = {
        .clock_hz = CONFIG_CLOCK_FREQ,
        .uart_pull_up = CONFIG_KLIPPER_MMU_SELECTOR_UART_PULLUP ? true : false,
        .tmc = {
            .address = CONFIG_KLIPPER_MMU_SELECTOR_UART_ADDRESS,
            .sense_resistor_milliohms = CONFIG_KLIPPER_MMU_TMC_RSENSE,
            .maximum_current_ma = CONFIG_KLIPPER_MMU_TMC_CURRENT_CEILING,
            .run_current_ma = CONFIG_KLIPPER_MMU_SELECTOR_RUN_CURRENT,
            .hold_current_ma = CONFIG_KLIPPER_MMU_SELECTOR_HOLD_CURRENT,
            .microsteps = CONFIG_KLIPPER_MMU_SELECTOR_MICROSTEPS,
#if CONFIG_KLIPPER_MMU_TMC_INTERPOLATE
            .interpolate = true,
#endif
#if CONFIG_KLIPPER_MMU_TMC_STEALTHCHOP
            .stealthchop = true,
#endif
            .transaction_timeout_us = CONFIG_KLIPPER_MMU_TMC_TIMEOUT_US,
            .diagnostic_interval_us = CONFIG_KLIPPER_MMU_TMC_DIAGNOSTIC_US
        }
    };
    if (km_tmc_uart_config_validate(&candidate) != KM_OK) return KM_ERR_INVALID_PARAM;
    *out = candidate;
    return KM_OK;
#endif
}
