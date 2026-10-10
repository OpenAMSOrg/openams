// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KLIPPER_MMU_BUILD_CONFIG_H
#define KLIPPER_MMU_BUILD_CONFIG_H
#include "klipper_mmu/device_profile.h"

/* Resolve the saved build choices into the existing common control profile.
 * Optional analog calibration is copied and validated; NULL is required in
 * switched mode and returns UNAVAILABLE in analog mode. No synthetic defaults.
 * Output is untouched on error. This does not initialize a core or hardware,
 * reserve pins, establish an instance, or make configured capabilities usable.
 * The board layer must verify those preconditions before controller attachment. */
KM_API km_result_t km_config_profile(km_device_profile_t *out,
                                     const km_diff_adc_config_t *calibration);
/* FPS arrival builds: config_km_fps_steps oid=%c steps=%u restores the
 * calibrated HUB-to-arrival distance km_config_profile uses (0 means
 * uncalibrated), or shuts down if it is outside the outer travel bounds. */
void command_config_km_fps_steps(uint32_t *args);
#endif
