// SPDX-License-Identifier: GPL-3.0-or-later
#include "feedback.h"
#include <math.h>

bool mmu_sim_diff_sample(const mmu_sim_plant_t *plant,
                         const km_diff_adc_config_t *calibration,
                         uint16_t *raw_a, uint16_t *raw_b)
{
    if (!plant || !calibration || !raw_a || !raw_b || raw_a == raw_b
        || !isfinite(plant->material_mm)
        || !isfinite(plant->parameters.buffer_capacity_mm)
        || plant->parameters.buffer_capacity_mm <= 0) return false;
    if (km_diff_adc_config_validate(calibration) != KM_OK) return false;
    double position = mmu_sim_buffer_mm(plant) / plant->parameters.buffer_capacity_mm * 1000;
    if (!isfinite(position) || position < 0 || position > 1000) return false;
    for (unsigned i = 1; i < calibration->point_count; ++i) {
        const km_diff_cal_point_t *low = &calibration->points[i - 1];
        const km_diff_cal_point_t *high = &calibration->points[i];
        if (position > high->position_permille) continue;
        double fraction = (position - low->position_permille)
            / (high->position_permille - low->position_permille);
        uint16_t a = (uint16_t)lround(low->ch_a_raw + fraction * (high->ch_a_raw - low->ch_a_raw));
        uint16_t b = (uint16_t)lround(low->ch_b_raw + fraction * (high->ch_b_raw - low->ch_b_raw));
        if (calibration->channel_map == KM_DIFF_CH_B_LOW_A_HIGH) {
            *raw_a = b; *raw_b = a;
        } else {
            *raw_a = a; *raw_b = b;
        }
        return true;
    }
    return false;
}
