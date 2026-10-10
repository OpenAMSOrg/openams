// SPDX-License-Identifier: GPL-3.0-or-later
// Fake autoconf.h for a native, standalone build of sim/board/plant.c's
// INDEXED selector mode (see run.py), shaped like the BTT ViViD sim image
// tests/linux_mcu/devices.py builds: one gear, an INDEXED selector axis (no
// grip servo, no gate position table), 4 lanes, a real physical gate order.
// Every numeric value here mirrors a Kconfig default mcu/Kconfig.selector
// documents for KLIPPER_MMU_DEVICE_BTT_VIVID (cited there with its Happy
// Hare source); this header exists only so this test can compile plant.c
// without a full Kconfig/Klipper build, not as a second source of truth.
#ifndef SHIM_AUTOCONF_H
#define SHIM_AUTOCONF_H

#define CONFIG_CLOCK_FREQ 520000000

// Feeder mechanics (the one shared gear driver): reference BoxTurtle-style
// values, same as shim_selector/autoconf.h -- the gear's own mechanics are
// not what this test is about.
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
#define CONFIG_KLIPPER_MMU_SELECTOR_STEPPER 1
#define CONFIG_KLIPPER_MMU_SERVO 0
#define CONFIG_KLIPPER_MMU_SELECTOR_KIND_LINEAR 0
#define CONFIG_KLIPPER_MMU_SELECTOR_KIND_ROTARY 0
#define CONFIG_KLIPPER_MMU_SELECTOR_KIND_SERVO 0
#define CONFIG_KLIPPER_MMU_SELECTOR_KIND_INDEXED 1

// Selector stepper axis mechanics (mcu/Kconfig.selector's BTT ViViD
// overrides: 360 mm rotation distance, 25:10 gear ratio -- one selector
// revolution sweeps the whole 4-gate, 90 mm-pitch drum once).
#define CONFIG_KLIPPER_MMU_SELECTOR_ROTATION_DISTANCE_UM 360000
#define CONFIG_KLIPPER_MMU_SELECTOR_FULL_STEPS 200
#define CONFIG_KLIPPER_MMU_SELECTOR_GEAR_RATIO_A 25
#define CONFIG_KLIPPER_MMU_SELECTOR_GEAR_RATIO_B 10
#define CONFIG_KLIPPER_MMU_SELECTOR_MICROSTEPS 16
#define CONFIG_KLIPPER_MMU_SELECTOR_MOVE_SPEED_MM_S 200
#define CONFIG_KLIPPER_MMU_SELECTOR_HOMING_SPEED_MM_S 60
#define CONFIG_KLIPPER_MMU_SELECTOR_ACCEL_MM_S2 1200

// INDEXED's own geometry (mcu/Kconfig.selector, "Indexed selector geometry"
// menu): BTT ViViD's real gate order and switch width, and its whole-axis
// travel bound (720 mm = 4 gates * 90 mm CAD pitch * 2 possible rotations,
// docs/devices/vivid.json's "gate_positions" decision) -- sim/board/
// plant.c's own selector_load_profile() rebuilds the 90 mm pitch from this
// bound and the lane count, not a second copy of the CAD number.
#define CONFIG_KLIPPER_MMU_SELECTOR_GATE_ORDER "0,3,1,2"
#define CONFIG_KLIPPER_MMU_SELECTOR_ENDSTOP_WIDTH_MM 10
#define CONFIG_KLIPPER_MMU_SELECTOR_INDEXED_MAX_TRAVEL_MM 720

#endif
