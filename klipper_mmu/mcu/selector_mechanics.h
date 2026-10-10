// SPDX-License-Identifier: GPL-3.0-or-later
/* Selector mechanics: turns Happy Hare's millimetre/degree selector settings
 * into the control core's microsteps and microsecond servo pulses, once, at
 * build time -- the selector-stepper and selector-servo analogue of
 * mcu/feeder_mechanics.h. Every macro here is guarded by whether this build
 * has a selector stepper (CONFIG_KLIPPER_MMU_SELECTOR_STEPPER) or a selector
 * servo (CONFIG_KLIPPER_MMU_SERVO); a device with neither compiles this
 * header to nothing. All conversions are exact integer arithmetic.
 */
#ifndef KLIPPER_MMU_SELECTOR_MECHANICS_H
#define KLIPPER_MMU_SELECTOR_MECHANICS_H

#include <stdint.h>
#include "klipper_mmu/types.h" /* KM_MOTION_MAX_* */

#if CONFIG_KLIPPER_MMU_SELECTOR_STEPPER

/* A saved header from before the selector menu cannot supply this; either
 * way the Happy Hare reference 16 applies (config/base/mmu_hardware.cfg:417). */
#if defined(CONFIG_KLIPPER_MMU_SELECTOR_MICROSTEPS) && CONFIG_KLIPPER_MMU_SELECTOR_MICROSTEPS
#define KM_SELECTOR_MICROSTEPS CONFIG_KLIPPER_MMU_SELECTOR_MICROSTEPS
#else
#define KM_SELECTOR_MICROSTEPS 16
#endif

#if !defined(CONFIG_KLIPPER_MMU_SELECTOR_ROTATION_DISTANCE_UM) \
    || CONFIG_KLIPPER_MMU_SELECTOR_ROTATION_DISTANCE_UM < 1 \
    || !defined(CONFIG_KLIPPER_MMU_SELECTOR_FULL_STEPS) \
    || CONFIG_KLIPPER_MMU_SELECTOR_FULL_STEPS < 1 \
    || !defined(CONFIG_KLIPPER_MMU_SELECTOR_GEAR_RATIO_A) \
    || CONFIG_KLIPPER_MMU_SELECTOR_GEAR_RATIO_A < 1 \
    || !defined(CONFIG_KLIPPER_MMU_SELECTOR_GEAR_RATIO_B) \
    || CONFIG_KLIPPER_MMU_SELECTOR_GEAR_RATIO_B < 1 \
    || KM_SELECTOR_MICROSTEPS < 1
#error "klipper_mmu: regenerate config with tools/install.py (selector mechanics need nonzero rotation distance, full steps, gear ratio and microsteps)"
#endif

/* Steps per metre of selector travel; same derivation as
 * KM_FEEDER_STEPS_PER_M in feeder_mechanics.h. */
#define KM_SELECTOR_STEPS_PER_M \
    ((int64_t)((int64_t)(CONFIG_KLIPPER_MMU_SELECTOR_FULL_STEPS) \
               * (KM_SELECTOR_MICROSTEPS) \
               * (CONFIG_KLIPPER_MMU_SELECTOR_GEAR_RATIO_A) * 1000000LL) \
     / ((int64_t)(CONFIG_KLIPPER_MMU_SELECTOR_GEAR_RATIO_B) \
        * (CONFIG_KLIPPER_MMU_SELECTOR_ROTATION_DISTANCE_UM)))

/* Millimetres to microsteps, rounded to the nearest microstep. */
#define KM_SELECTOR_MM_TO_STEPS(mm) \
    ((int32_t)(((int64_t)(mm) * KM_SELECTOR_STEPS_PER_M + 500LL) / 1000LL))

/* Micrometres (the CAD/geometry menu's unit) to microsteps. */
#define KM_SELECTOR_UM_TO_STEPS(um) \
    ((int32_t)(((int64_t)(um) * KM_SELECTOR_STEPS_PER_M + 500000LL) / 1000000LL))

_Static_assert(KM_SELECTOR_STEPS_PER_M > 0,
               "klipper_mmu: selector step scale must be positive");
_Static_assert(KM_SELECTOR_MM_TO_STEPS(CONFIG_KLIPPER_MMU_SELECTOR_MOVE_SPEED_MM_S) > 0
               && (uint32_t)KM_SELECTOR_MM_TO_STEPS(CONFIG_KLIPPER_MMU_SELECTOR_MOVE_SPEED_MM_S)
                  <= KM_MOTION_MAX_VELOCITY_STEPS_S,
               "klipper_mmu: selector move speed is outside what the control core accepts");
_Static_assert(KM_SELECTOR_MM_TO_STEPS(CONFIG_KLIPPER_MMU_SELECTOR_HOMING_SPEED_MM_S) > 0
               && (uint32_t)KM_SELECTOR_MM_TO_STEPS(CONFIG_KLIPPER_MMU_SELECTOR_HOMING_SPEED_MM_S)
                  <= KM_MOTION_MAX_VELOCITY_STEPS_S,
               "klipper_mmu: selector homing speed is outside what the control core accepts");
_Static_assert(KM_SELECTOR_MM_TO_STEPS(CONFIG_KLIPPER_MMU_SELECTOR_ACCEL_MM_S2) > 0
               && (uint32_t)KM_SELECTOR_MM_TO_STEPS(CONFIG_KLIPPER_MMU_SELECTOR_ACCEL_MM_S2)
                  <= KM_MOTION_MAX_ACCEL_STEPS_S2,
               "klipper_mmu: selector acceleration is outside what the control core accepts");

/* Whole-axis maximum travel bound: Happy Hare's _get_max_selector_movement
 * for the unknown/whole-axis case (mmu_linear_selector.py:336-358,
 * mmu_rotary_selector.py:321-329): gate0 + (lanes-1)*width, plus ERCF 1.1's
 * every-third-gate bearing-block term, plus the LINEAR-only last-gate offset
 * (0 for ROTARY), plus the selector tolerance. */
#define KM_SELECTOR_MAX_TRAVEL_UM(lanes) \
    ((int64_t)(CONFIG_KLIPPER_MMU_SELECTOR_GATE0_POS_UM) \
     + (int64_t)((lanes) - 1) * (CONFIG_KLIPPER_MMU_SELECTOR_GATE_WIDTH_UM) \
     + (int64_t)(((lanes) - 1) / 3) * (CONFIG_KLIPPER_MMU_SELECTOR_BLOCK_WIDTH_UM) \
     + (CONFIG_KLIPPER_MMU_SELECTOR_LAST_GATE_OFFSET_UM) \
     + (CONFIG_KLIPPER_MMU_SELECTOR_TOLERANCE_UM))

#endif /* CONFIG_KLIPPER_MMU_SELECTOR_STEPPER */

#if CONFIG_KLIPPER_MMU_SERVO

#if !defined(CONFIG_KLIPPER_MMU_SELECTOR_SERVO_MAX_ANGLE_DEG) \
    || CONFIG_KLIPPER_MMU_SELECTOR_SERVO_MAX_ANGLE_DEG < 1 \
    || !defined(CONFIG_KLIPPER_MMU_SELECTOR_SERVO_MIN_PULSE_US) \
    || !defined(CONFIG_KLIPPER_MMU_SELECTOR_SERVO_MAX_PULSE_US) \
    || CONFIG_KLIPPER_MMU_SELECTOR_SERVO_MAX_PULSE_US < 1 \
    || CONFIG_KLIPPER_MMU_SELECTOR_SERVO_MAX_PULSE_US <= CONFIG_KLIPPER_MMU_SELECTOR_SERVO_MIN_PULSE_US \
    || CONFIG_KLIPPER_MMU_SELECTOR_SERVO_MAX_PULSE_US > 65535
#error "klipper_mmu: regenerate config with tools/install.py (selector servo needs a nonzero angle range and a max pulse width above min, both fitting microseconds)"
#endif

/* Degrees to microseconds, linear across the servo's hardware angle range
 * (Happy Hare's PARAM_SERVO_MAXIMUM_ANGLE + min/max pulse width). */
#define KM_SELECTOR_SERVO_US(deg) \
    ((uint16_t)(CONFIG_KLIPPER_MMU_SELECTOR_SERVO_MIN_PULSE_US \
     + ((int64_t)(deg) \
        * (CONFIG_KLIPPER_MMU_SELECTOR_SERVO_MAX_PULSE_US - CONFIG_KLIPPER_MMU_SELECTOR_SERVO_MIN_PULSE_US) \
        + CONFIG_KLIPPER_MMU_SELECTOR_SERVO_MAX_ANGLE_DEG / 2) \
       / CONFIG_KLIPPER_MMU_SELECTOR_SERVO_MAX_ANGLE_DEG))

#endif /* CONFIG_KLIPPER_MMU_SERVO */

#endif /* KLIPPER_MMU_SELECTOR_MECHANICS_H */
