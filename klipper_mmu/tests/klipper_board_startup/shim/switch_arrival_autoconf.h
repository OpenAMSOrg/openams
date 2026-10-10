/* SPDX-License-Identifier: GPL-3.0-or-later */
/* Toolhead-switch arrival on SW6/PE7 over a saved FPS-arrival BoxTurtle
 * header, for switch-driven fixtures. Removing the FPS symbols selects the
 * sources' fallbacks: FPS arrival off, toolhead switch fitted. */
#undef CONFIG_KLIPPER_MMU_FPS_ARRIVAL
#undef CONFIG_KLIPPER_MMU_FPS_ARRIVAL_PERMILLE
#undef CONFIG_KLIPPER_MMU_FPS_ARRIVAL_SAMPLES
#undef CONFIG_KLIPPER_MMU_FPS_ARRIVAL_HOLD_MS
#undef CONFIG_KLIPPER_MMU_FPS_ARRIVAL_MIN_MM
#undef CONFIG_KLIPPER_MMU_FPS_ARRIVAL_MAX_MM
/* The calibrated LOAD profile's menu values are also FPS-arrival-only; leave
 * them defined and mcu/config.c's own "#ifndef FPS_ARRIVAL" fallback block
 * (which redefines all of these as 0) collides with the base image's real
 * values still in scope here. */
#undef CONFIG_KLIPPER_MMU_LOAD_CRUISE_SPEED_MM_S
#undef CONFIG_KLIPPER_MMU_LOAD_CRAWL_SPEED_MM_S
#undef CONFIG_KLIPPER_MMU_LOAD_WINDOW_MM
#undef CONFIG_KLIPPER_MMU_BUFFER_TRAVEL_MM
#undef CONFIG_KLIPPER_MMU_TOOLHEAD_SENSOR
#undef CONFIG_KLIPPER_MMU_PINS_TOOLHEAD
#define CONFIG_KLIPPER_MMU_PINS_TOOLHEAD "PE7"
#undef CONFIG_KLIPPER_MMU_TOOLHEAD_ACTIVE_HIGH
#define CONFIG_KLIPPER_MMU_TOOLHEAD_ACTIVE_HIGH 0
#undef CONFIG_KLIPPER_MMU_TOOLHEAD_PULL
#define CONFIG_KLIPPER_MMU_TOOLHEAD_PULL 1
