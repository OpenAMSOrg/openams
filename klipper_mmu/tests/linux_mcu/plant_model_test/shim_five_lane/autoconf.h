// SPDX-License-Identifier: GPL-3.0-or-later
// Fake autoconf.h for a native, standalone build of sim/board/plant.c in
// non-selector mode with more than four lanes (EMU's real shape, see
// run.py): every CONFIG_KLIPPER_MMU_SELECTOR_* symbol is left undefined,
// same as shim_default/, so plant.c's own #ifndef guards default those to
// 0 and the per-lane feeder pin tables (not the shared-gear ones) apply.
// Only CONFIG_KLIPPER_MMU_LANE_COUNT differs from shim_default/.
#ifndef SHIM_AUTOCONF_H
#define SHIM_AUTOCONF_H

#define CONFIG_CLOCK_FREQ 520000000

// Five lanes: EMU's real lane count (docs/devices/emu.json), the shape
// that extended step_pins/dir_pins/enable_pins/bay_pins/load_pins past
// their original four real entries.
#define CONFIG_KLIPPER_MMU_LANE_COUNT 5

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
