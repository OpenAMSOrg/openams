// SPDX-License-Identifier: GPL-3.0-or-later
// Fake autoconf.h for a native, standalone build of sim/board/plant.c
// shaped like Angry Beaver/3MS/QuattroBox 1.0/1.1/2 (see run.py and
// test_plant_no_bay.c): no per-lane BAY switch at all
// (CONFIG_KLIPPER_MMU_BAY_SENSORS left unset, its real Kconfig default for
// these devices, mcu/Kconfig.control) on top of shim_default's own
// four-lane, no-selector feeder mechanics.
#ifndef SHIM_AUTOCONF_H
#define SHIM_AUTOCONF_H

#define CONFIG_CLOCK_FREQ 520000000

// Reference BoxTurtle-style feeder mechanics (mcu/feeder_mechanics.h):
// 200 full steps, 16 microsteps, 50:10 gear, 22 mm rotation distance --
// KM_FEEDER_STEPS_PER_M works out to 727272, the real BoxTurtle value
// cited in plant.c's own header comment.
#define CONFIG_KLIPPER_MMU_FEEDER_ROTATION_DISTANCE_UM 22000
#define CONFIG_KLIPPER_MMU_FEEDER_FULL_STEPS 200
#define CONFIG_KLIPPER_MMU_FEEDER_GEAR_RATIO_A 50
#define CONFIG_KLIPPER_MMU_FEEDER_GEAR_RATIO_B 10
#define CONFIG_KLIPPER_MMU_TMC_MICROSTEPS 16

// feeder_mechanics.h's own unconditional _Static_assert lines need these
// regardless of selector mode; FPS_ARRIVAL/DIFF_BUFFER stay unset (0) so
// the LOAD-window/buffer-gain checks that need more settings stay skipped.
#define CONFIG_KLIPPER_MMU_FEED_SPEED_MM_S 26
#define CONFIG_KLIPPER_MMU_RETRACT_SPEED_MM_S 26
#define CONFIG_KLIPPER_MMU_ACCELERATION_MM_S2 500
#define CONFIG_KLIPPER_MMU_DECELERATION_MM_S2 500
#define CONFIG_KLIPPER_MMU_JERK_MM_S3 0
#define CONFIG_KLIPPER_MMU_LANE_COUNT 4

// No CONFIG_KLIPPER_MMU_BAY_SENSORS define here on purpose: this is exactly
// what a real Angry Beaver/3MS/QuattroBox autoconf.h looks like (the
// symbol is simply absent), and the C preprocessor already treats an
// undefined macro as 0 in #if -- the same convention every other boolean
// Kconfig symbol in this codebase relies on (see mcu/switch_config.c).

#endif
