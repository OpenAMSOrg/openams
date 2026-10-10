// SPDX-License-Identifier: GPL-3.0-or-later
#include "adc_config.h"
#include "autoconf.h"

km_result_t km_config_adc_pair(km_adc_pair_config_t *out)
{
    if (!out) return KM_ERR_INVALID_PARAM;
#if !CONFIG_KLIPPER_MMU_PIN_CONFIG || !CONFIG_KLIPPER_MMU_DIFF_BUFFER || !CONFIG_WANT_ADC
    return KM_ERR_UNAVAILABLE;
#else
    if (!CONFIG_KLIPPER_MMU_ADC_CONVERSION_TIMEOUT_US
        || !CONFIG_KLIPPER_MMU_ADC_PAIR_SPAN_US
        || !CONFIG_KLIPPER_MMU_ADC_SAMPLE_AGE_US)
        return KM_ERR_UNAVAILABLE;
    const km_adc_pair_config_t candidate = {
        .max_raw = 4095,  /* Upstream H723 gpio_adc_read() returns 12-bit raw. */
        .conversion_timeout_us = CONFIG_KLIPPER_MMU_ADC_CONVERSION_TIMEOUT_US,
        .max_pair_span_us = CONFIG_KLIPPER_MMU_ADC_PAIR_SPAN_US,
        .max_sample_age_us = CONFIG_KLIPPER_MMU_ADC_SAMPLE_AGE_US
    };
    if (km_adc_pair_config_validate(&candidate) != KM_OK)
        return KM_ERR_INVALID_PARAM;
    if (candidate.max_sample_age_us > (uint32_t)CONFIG_KLIPPER_MMU_SENSOR_AGE * 1000u)
        return KM_ERR_CONFLICT;
    if ((uint64_t)CONFIG_KLIPPER_MMU_ADC_SAMPLE_PERIOD_US
        + candidate.max_pair_span_us >= candidate.max_sample_age_us)
        return KM_ERR_CONFLICT;
    *out = candidate;
    return KM_OK;
#endif
}
