// SPDX-License-Identifier: GPL-3.0-or-later
#include "rewind_config.h"
#include "autoconf.h"
#include <string.h>

km_result_t km_config_rewind(km_pwm_bridge_config_t *out)
{
    if (!out) return KM_ERR_INVALID_PARAM;
#if !CONFIG_KLIPPER_MMU_PIN_CONFIG || !CONFIG_KLIPPER_MMU_POWERED_REWIND
    return KM_ERR_UNAVAILABLE;
#else
    if (!CONFIG_KLIPPER_MMU_REWIND_BRIDGE_CEILING
        || !CONFIG_KLIPPER_MMU_REWIND_DEADTIME_US
        || !CONFIG_KLIPPER_MMU_REWIND_LATENESS_US)
        return KM_ERR_UNAVAILABLE;
    const km_pwm_bridge_config_t candidate = {
        .maximum_duty_permille = CONFIG_KLIPPER_MMU_REWIND_BRIDGE_CEILING,
        .reversal_deadtime_us = CONFIG_KLIPPER_MMU_REWIND_DEADTIME_US,
        .maximum_call_lateness_us = CONFIG_KLIPPER_MMU_REWIND_LATENESS_US
    };
    if (km_pwm_bridge_config_validate(&candidate) != KM_OK)
        return KM_ERR_INVALID_PARAM;
    if (CONFIG_KLIPPER_MMU_REWIND_DUTY > CONFIG_KLIPPER_MMU_REWIND_BRIDGE_CEILING)
        return KM_ERR_CONFLICT;
    *out = candidate;
    return KM_OK;
#endif
}

km_result_t km_config_rewind_outputs(km_rewind_output_config_t out[KM_MAX_LANES])
{
    if (!out) return KM_ERR_INVALID_PARAM;
#if !CONFIG_KLIPPER_MMU_PIN_CONFIG || !CONFIG_KLIPPER_MMU_POWERED_REWIND
    return KM_ERR_UNAVAILABLE;
#else
    if (!CONFIG_KLIPPER_MMU_REWIND_CYCLE_US
        || !CONFIG_KLIPPER_MMU_REWIND_ENABLE_HIGH[0]
        || !CONFIG_KLIPPER_MMU_REWIND_FORWARD_HIGH[0]
        || !CONFIG_KLIPPER_MMU_REWIND_REVERSE_HIGH[0])
        return KM_ERR_UNAVAILABLE;
    const unsigned lanes = CONFIG_KLIPPER_MMU_LANE_COUNT;
    if (!lanes || lanes > KM_MAX_LANES
        || CONFIG_KLIPPER_MMU_REWIND_CYCLE_US < 1
        || CONFIG_KLIPPER_MMU_REWIND_CYCLE_US > 1000000)
        return KM_ERR_INVALID_PARAM;
    const char *level[] = {CONFIG_KLIPPER_MMU_REWIND_ENABLE_HIGH,
                          CONFIG_KLIPPER_MMU_REWIND_FORWARD_HIGH,
                          CONFIG_KLIPPER_MMU_REWIND_REVERSE_HIGH};
    km_rewind_output_config_t candidate[KM_MAX_LANES] = {0};
    for (unsigned lane = 0; lane < lanes; ++lane) {
        bool value[3];
        for (unsigned i = 0; i < 3u; ++i) {
            const char *s = level[i];
            if ((s[0] != '0' && s[0] != '1')
                || (lane + 1u == lanes ? s[1] != '\0' : s[1] != ','))
                return KM_ERR_INVALID_PARAM;
            value[i] = s[0] == '1';
            if (lane + 1u < lanes) level[i] += 2;
        }
        candidate[lane] = (km_rewind_output_config_t){
            .cycle_us = CONFIG_KLIPPER_MMU_REWIND_CYCLE_US,
            .enable_active_high = value[0],
            .forward_active_high = value[1],
            .reverse_active_high = value[2],
        };
    }
    memcpy(out, candidate, sizeof(candidate));
    return KM_OK;
#endif
}
