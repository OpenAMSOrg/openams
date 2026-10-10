// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KLIPPER_MMU_ADC_CONFIG_H
#define KLIPPER_MMU_ADC_CONFIG_H
#include "klipper_mmu/adc_pair.h"

/* Pure saved acquisition limits for the H723 DiffA/DiffB reader. Failure
 * preserves output. Unset limits or disabled analog acquisition: UNAVAILABLE.
 * Does not claim ADC1, configure pins, calibrate the sensor or start sampling. */
KM_API km_result_t km_config_adc_pair(km_adc_pair_config_t *out);
#endif
