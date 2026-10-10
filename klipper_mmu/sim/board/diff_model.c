// SPDX-License-Identifier: GPL-3.0-or-later
// DiffA/DiffB forward model for the sim board: the raw counts a calibrated
// frontend reports at a buffer position, the inverse of
// km_buffer_update_diff_adc (piecewise linear between calibration points,
// clamped to the ends, channels swapped for KM_DIFF_CH_B_LOW_A_HIGH).
// Drafted by a local model (best of 3, checked against the decoder).
#include "klipper_mmu/buffer_sensor.h"

int km_sim_diff_raw(const km_diff_adc_config_t *config, int32_t position_permille,
                    uint16_t *raw_a, uint16_t *raw_b)
{
    uint8_t n = config->point_count, i = 0;
    if (n < 2)
        return -1;
    int32_t pos = position_permille < 0 ? 0 : position_permille > 1000 ? 1000 : position_permille;
    while (i + 2 < n && pos > config->points[i + 1].position_permille)
        i++;
    const km_diff_cal_point_t *p0 = &config->points[i], *p1 = &config->points[i + 1];
    int32_t range = p1->position_permille - p0->position_permille;
    int32_t off = pos - p0->position_permille;
    int32_t a = p0->ch_a_raw + ((p1->ch_a_raw - p0->ch_a_raw) * off + range / 2) / range;
    int32_t b = p0->ch_b_raw + ((p1->ch_b_raw - p0->ch_b_raw) * off + range / 2) / range;
    int swap = config->channel_map == KM_DIFF_CH_B_LOW_A_HIGH;
    *raw_a = (uint16_t)(swap ? b : a);
    *raw_b = (uint16_t)(swap ? a : b);
    return 0;
}
