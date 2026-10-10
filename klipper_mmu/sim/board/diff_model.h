// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KM_SIM_DIFF_MODEL_H
#define KM_SIM_DIFF_MODEL_H
#include "klipper_mmu/buffer_sensor.h"

/* Raw ADC counts a DiffA/DiffB frontend with this calibration reports at a
 * buffer position: the inverse of km_buffer_update_diff_adc. Returns 0, or
 * -1 when the config has fewer than 2 points. */
int km_sim_diff_raw(const km_diff_adc_config_t *config, int32_t position_permille,
                    uint16_t *raw_a, uint16_t *raw_b);

#endif
