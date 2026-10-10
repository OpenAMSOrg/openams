// SPDX-License-Identifier: GPL-3.0-or-later
// Fake autoconf.h for a native, standalone build of sim/board/plant.c's
// selector mode (see run.py), shaped like an ERCF 2.0 sim image: LINEAR
// selector kind, a grip servo, 8 lanes. Every numeric value here mirrors a
// Kconfig default mcu/Kconfig.selector documents for
// KLIPPER_MMU_DEVICE_ERCF_2_0 (cited there with its Happy Hare source);
// this header exists only so this test can compile plant.c without a full
// Kconfig/Klipper build, not as a second source of truth for those values.
#ifndef SHIM_AUTOCONF_H
#define SHIM_AUTOCONF_H

#define CONFIG_CLOCK_FREQ 520000000

// Feeder mechanics (the one shared gear driver): 200 full steps, 16
// microsteps, 80:20 gear (ERCF 2.0's real gear_ratio), 22.732 mm rotation
// distance -- close to the dossier's 22.7316868 mm, not required to be
// exact (sim geometry, not a hardware measurement).
#define CONFIG_KLIPPER_MMU_FEEDER_ROTATION_DISTANCE_UM 22732
#define CONFIG_KLIPPER_MMU_FEEDER_FULL_STEPS 200
#define CONFIG_KLIPPER_MMU_FEEDER_GEAR_RATIO_A 4
#define CONFIG_KLIPPER_MMU_FEEDER_GEAR_RATIO_B 1
#define CONFIG_KLIPPER_MMU_TMC_MICROSTEPS 16

// feeder_mechanics.h's own unconditional _Static_assert lines need these
// regardless of selector mode; FPS_ARRIVAL/DIFF_BUFFER stay unset (0) so
// the LOAD-window/buffer-gain checks that need more settings stay skipped.
#define CONFIG_KLIPPER_MMU_FEED_SPEED_MM_S 26
#define CONFIG_KLIPPER_MMU_RETRACT_SPEED_MM_S 26
#define CONFIG_KLIPPER_MMU_ACCELERATION_MM_S2 500
#define CONFIG_KLIPPER_MMU_DECELERATION_MM_S2 500
#define CONFIG_KLIPPER_MMU_JERK_MM_S3 0

#define CONFIG_KLIPPER_MMU_LANE_COUNT 8
#define CONFIG_KLIPPER_MMU_SELECTOR_STEPPER 1
#define CONFIG_KLIPPER_MMU_SERVO 1
#define CONFIG_KLIPPER_MMU_SELECTOR_KIND_LINEAR 1
#define CONFIG_KLIPPER_MMU_SELECTOR_KIND_ROTARY 0
#define CONFIG_KLIPPER_MMU_SELECTOR_KIND_SERVO 0

// Selector stepper axis mechanics (mcu/Kconfig.selector's generic
// defaults: 40 mm rotation distance, 1:1 gear, 200 full steps, 16
// microsteps -- none of ERCF 1.1/2.0/3.0 override these).
#define CONFIG_KLIPPER_MMU_SELECTOR_ROTATION_DISTANCE_UM 40000
#define CONFIG_KLIPPER_MMU_SELECTOR_FULL_STEPS 200
#define CONFIG_KLIPPER_MMU_SELECTOR_GEAR_RATIO_A 1
#define CONFIG_KLIPPER_MMU_SELECTOR_GEAR_RATIO_B 1
#define CONFIG_KLIPPER_MMU_SELECTOR_MICROSTEPS 16
#define CONFIG_KLIPPER_MMU_SELECTOR_MOVE_SPEED_MM_S 200
#define CONFIG_KLIPPER_MMU_SELECTOR_HOMING_SPEED_MM_S 60
#define CONFIG_KLIPPER_MMU_SELECTOR_ACCEL_MM_S2 1200
#define CONFIG_KLIPPER_MMU_SELECTOR_ENDSTOP_ACTIVE_HIGH 1

// ERCF 2.0's own gate list (mcu/Kconfig.selector
// KLIPPER_MMU_SELECTOR_GATE_POSITIONS_MM default for
// KLIPPER_MMU_DEVICE_ERCF_2_0): gate0_pos 4.0 mm, gate_width 23.0 mm.
#define CONFIG_KLIPPER_MMU_SELECTOR_GATE_POSITIONS_MM \
    "4.0,27.0,50.0,73.0,96.0,119.0,142.0,165.0"

// Selector servo (mcu/Kconfig.selector generic defaults) and ERCF 2.0's own
// engage/release/move angles (servo_down_angle 100, servo_up_angle 30,
// servo_move_angle 50).
#define CONFIG_KLIPPER_MMU_SELECTOR_SERVO_MIN_PULSE_US 1000
#define CONFIG_KLIPPER_MMU_SELECTOR_SERVO_MAX_PULSE_US 2000
#define CONFIG_KLIPPER_MMU_SELECTOR_SERVO_MAX_ANGLE_DEG 180
#define CONFIG_KLIPPER_MMU_SELECTOR_SERVO_ENGAGE_ANGLE_DEG 100
#define CONFIG_KLIPPER_MMU_SELECTOR_SERVO_RELEASE_ANGLE_DEG 30
#define CONFIG_KLIPPER_MMU_SELECTOR_SERVO_MOVE_ANGLE_DEG 50

// ERCF 2.0 defaults its shared-path encoder on (mcu/Kconfig.control).
#define CONFIG_KLIPPER_MMU_ENCODER 1

#endif
