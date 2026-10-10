// SPDX-License-Identifier: GPL-3.0-or-later
// Fake autoconf.h for a native, standalone build of sim/board/plant.c in
// its *original* four-lane, no-selector mode (see run.py): every
// CONFIG_KLIPPER_MMU_SELECTOR_* symbol is left undefined, exactly like a
// saved BoxTurtle/NightOwl/EMU header from before the selector add-on --
// plant.c's own #ifndef guards default those to 0. Only the feeder
// mechanics plant.c always needs (regardless of selector mode) are here.
#ifndef SHIM_AUTOCONF_H
#define SHIM_AUTOCONF_H

#define CONFIG_CLOCK_FREQ 520000000

// Four lanes, the original shape this binary proves is unchanged now that
// KM_SIM_PLANT_LANES reads this symbol on every image, not just a selector
// one (see plant.c's own header comment).
#define CONFIG_KLIPPER_MMU_LANE_COUNT 4
// A BoxTurtle/NightOwl-style device has a per-lane BAY switch (unlike
// shim_no_bay/, Angry Beaver/3MS/QuattroBox's shape): keep the grip point
// at the real BAY threshold rather than falling through plant.c's own
// no-BAY-sensor default of 0 for an undefined symbol.
#define CONFIG_KLIPPER_MMU_BAY_SENSORS 1

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

#endif
