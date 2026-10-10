// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef MMU_SIM_FEEDBACK_H
#define MMU_SIM_FEEDBACK_H
#include "plant.h"
#include "klipper_mmu/buffer_sensor.h"

/* Generate ideal physical channel readings from an explicit calibration curve.
 * No default curve, noise, acquisition time or validity is manufactured here.
 * Both outputs require separate caller storage; failure leaves them unchanged. */
bool mmu_sim_diff_sample(const mmu_sim_plant_t *plant,
                         const km_diff_adc_config_t *calibration,
                         uint16_t *raw_a, uint16_t *raw_b);
#endif
