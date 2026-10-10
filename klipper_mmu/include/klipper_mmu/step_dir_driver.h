// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KLIPPER_MMU_STEP_DIR_DRIVER_H
#define KLIPPER_MMU_STEP_DIR_DRIVER_H
#include "klipper_mmu/driver.h"

typedef struct {
    bool enable_active_high;
    bool step_active_high;
    bool forward_high;
    uint16_t pulse_high_us;
    uint16_t pulse_low_us;
    uint32_t maximum_velocity_steps_s;
} km_step_dir_config_t;

/* Pure validation shared by saved configuration and physical setup. */
KM_API km_result_t km_step_dir_config_validate(const km_step_dir_config_t *config);
#endif
