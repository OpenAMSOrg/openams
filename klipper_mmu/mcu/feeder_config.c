// SPDX-License-Identifier: GPL-3.0-or-later
#include "feeder_config.h"
#include "autoconf.h"
#include "feeder_mechanics.h"
#include "pin_config.h"
#include <string.h>

/* The driver ceiling is a menu millimetre value like the feed speeds; the
 * core's velocity ceiling is in microsteps, so check the converted value.
 * The pulse-budget ceiling is a wiring-dependent runtime check, not this. */
#if CONFIG_KLIPPER_MMU_PIN_CONFIG
_Static_assert(KM_FEEDER_MM_TO_STEPS(CONFIG_KLIPPER_MMU_STEP_MAX_SPEED_MM_S) > 0
               && KM_FEEDER_MM_TO_STEPS(CONFIG_KLIPPER_MMU_STEP_MAX_SPEED_MM_S)
                  <= KM_MOTION_MAX_VELOCITY_STEPS_S,
               "klipper_mmu: feeder driver ceiling is outside what the control core accepts");
#endif

km_result_t km_config_feeders(km_step_dir_config_t out[KM_MAX_LANES])
{
    if (!out) return KM_ERR_INVALID_PARAM;
#if !CONFIG_KLIPPER_MMU_PIN_CONFIG
    return KM_ERR_UNAVAILABLE;
#else
    const unsigned lanes = KM_GEAR_DRIVERS;
    const uint32_t step_max_steps_s =
        KM_FEEDER_MM_TO_STEPS(CONFIG_KLIPPER_MMU_STEP_MAX_SPEED_MM_S);
    if (!lanes || lanes > KM_MAX_LANES) return KM_ERR_INVALID_PARAM;
    const char *level[] = {CONFIG_KLIPPER_MMU_FEEDER_ENABLE_HIGH,
                          CONFIG_KLIPPER_MMU_FEEDER_STEP_HIGH,
                          CONFIG_KLIPPER_MMU_FEEDER_FORWARD_HIGH};
    km_step_dir_config_t candidate[KM_MAX_LANES] = {0};
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
        candidate[lane] = (km_step_dir_config_t){
            .enable_active_high = value[0], .step_active_high = value[1],
            .forward_high = value[2],
            .pulse_high_us = CONFIG_KLIPPER_MMU_STEP_HIGH_US,
            .pulse_low_us = CONFIG_KLIPPER_MMU_STEP_LOW_US,
            .maximum_velocity_steps_s = step_max_steps_s
        };
        if (km_step_dir_config_validate(&candidate[lane]) != KM_OK)
            return KM_ERR_INVALID_PARAM;
    }
    if (KM_FEEDER_MM_TO_STEPS(CONFIG_KLIPPER_MMU_FEED_SPEED_MM_S)
            > step_max_steps_s
        || KM_FEEDER_MM_TO_STEPS(CONFIG_KLIPPER_MMU_RETRACT_SPEED_MM_S)
            > step_max_steps_s)
        return KM_ERR_CONFLICT;
    memcpy(out, candidate, sizeof(candidate));
    return KM_OK;
#endif
}
