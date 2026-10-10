// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KLIPPER_MMU_SWITCH_CONFIG_H
#define KLIPPER_MMU_SWITCH_CONFIG_H

#include "switch_gpio.h"

/* Startup factory for the saved pin map and electrical switch settings.
 * Serialized setup calls only: fixed pin scratch avoids a large MCU stack copy.
 * Uses the shared km_config_pins_view(), so complete pin assignments and its
 * conflict checks must pass, including DiffA/B remapping. No GPIO or controller
 * initialization.
 * Empty/unset electrical choices return UNAVAILABLE; malformed lists return
 * INVALID_PARAM. Pin errors are propagated. Output is unchanged on failure;
 * unused bay/analog-buffer entries have pin UINT16_MAX. */
KM_API km_result_t km_config_switches(km_switch_gpio_config_t *out);

#endif
