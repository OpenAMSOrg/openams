// SPDX-License-Identifier: GPL-3.0-or-later
#include "klipper_mmu/step_dir_driver.h"

km_result_t km_step_dir_config_validate(const km_step_dir_config_t *cfg)
{
    if (!cfg || !cfg->pulse_high_us || !cfg->pulse_low_us
        || !cfg->maximum_velocity_steps_s
        || cfg->maximum_velocity_steps_s > KM_MOTION_MAX_VELOCITY_STEPS_S)
        return KM_ERR_INVALID_PARAM;
    uint32_t cycle = (uint32_t)cfg->pulse_high_us + cfg->pulse_low_us;
    return cfg->maximum_velocity_steps_s <= 1000000u / cycle
        ? KM_OK : KM_ERR_INVALID_PARAM;
}
