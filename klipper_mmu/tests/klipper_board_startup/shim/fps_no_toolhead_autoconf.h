/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Exact synthetic menu selection used only by the composed FPS fixture. */
#include "diff_autoconf.h"
#undef CONFIG_KLIPPER_MMU_FPS_ARRIVAL
#define CONFIG_KLIPPER_MMU_FPS_ARRIVAL 1
#undef CONFIG_KLIPPER_MMU_FPS_ARRIVAL_PERMILLE
#define CONFIG_KLIPPER_MMU_FPS_ARRIVAL_PERMILLE 700
#undef CONFIG_KLIPPER_MMU_FPS_ARRIVAL_SAMPLES
#define CONFIG_KLIPPER_MMU_FPS_ARRIVAL_SAMPLES 3
#undef CONFIG_KLIPPER_MMU_FPS_ARRIVAL_HOLD_MS
#define CONFIG_KLIPPER_MMU_FPS_ARRIVAL_HOLD_MS 2
#undef CONFIG_KLIPPER_MMU_FPS_ARRIVAL_MIN_MM
#define CONFIG_KLIPPER_MMU_FPS_ARRIVAL_MIN_MM 1
#undef CONFIG_KLIPPER_MMU_FPS_ARRIVAL_MAX_MM
#define CONFIG_KLIPPER_MMU_FPS_ARRIVAL_MAX_MM 4
/* The base image's real BoxTurtle 50 mm window does not fit under half this
 * fixture's 3 mm outer spread; narrow it to fit. Buffer travel stays the
 * base image's real 15 mm -- the calibrated LOAD's build-time stop-distance
 * proof already holds at that value with the real cruise/crawl/braking. */
#undef CONFIG_KLIPPER_MMU_LOAD_WINDOW_MM
#define CONFIG_KLIPPER_MMU_LOAD_WINDOW_MM 1
#undef CONFIG_KLIPPER_MMU_TOOLHEAD_SENSOR
#define CONFIG_KLIPPER_MMU_TOOLHEAD_SENSOR 0
