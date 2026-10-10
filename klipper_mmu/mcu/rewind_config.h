// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KLIPPER_MMU_REWIND_CONFIG_H
#define KLIPPER_MMU_REWIND_CONFIG_H
#include "klipper_mmu/pwm_bridge_driver.h"

/* Pure saved limits shared by all configured rewind lanes. Failure preserves
 * output. Disabled pin menu, passive rewind, or unset limits return UNAVAILABLE.
 * No pins, PWM hardware, readiness or current measurement are established. */
KM_API km_result_t km_config_rewind(km_pwm_bridge_config_t *out);

typedef struct {
    uint32_t cycle_us; /* Requested period; the board must validate realizability. */
    bool enable_active_high;
    bool forward_active_high;
    bool reverse_active_high;
} km_rewind_output_config_t;

/* Pure saved electrical choices, one per lane. A shared period keeps channels
 * on a common timer compatible. All settings require explicit configuration.
 * Validates every lane before writing out; unused lanes are zero. No pin/timer
 * claim, PWM setup, applied-output guarantee or hardware qualification. */
KM_API km_result_t km_config_rewind_outputs(km_rewind_output_config_t out[KM_MAX_LANES]);
#endif
