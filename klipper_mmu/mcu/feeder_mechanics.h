// SPDX-License-Identifier: GPL-3.0-or-later
/* Feeder mechanics: the one place this package turns millimetres into
 * feeder microsteps.
 *
 * The menu asks for filament travel and speeds the way Klipper asks for a
 * stepper: rotation distance, gear ratio, full steps and microsteps. The
 * common control core in src/ and include/ stays in microsteps, and so does
 * every exported constant, so the conversion happens here once, at build
 * time, and nowhere else:
 *
 *     steps_per_m = full_steps * microsteps * A * 1000000
 *                   / (B * rotation_distance_um)
 *
 * The widest intermediate is 1000 * 256 * 100000 * 1000000 = 2.56e16, which
 * fits an int64_t with room to spare. The division truncates, as integer
 * arithmetic must; a millimetre value is then rounded to the nearest
 * microstep, which is finer than any feeder can be told apart.
 */
#ifndef KLIPPER_MMU_FEEDER_MECHANICS_H
#define KLIPPER_MMU_FEEDER_MECHANICS_H

#include <stdint.h>
#include "klipper_mmu/types.h" /* KM_MOTION_MAX_* */

/* The microsteps entry lives under KLIPPER_MMU_PIN_CONFIG. A MACH_SIMU header
 * omits it and Klipper's autoconf defines it as 0 when pin config is off;
 * either way the reference 16 applies. */
#if defined(CONFIG_KLIPPER_MMU_TMC_MICROSTEPS) && CONFIG_KLIPPER_MMU_TMC_MICROSTEPS
#define KM_FEEDER_MICROSTEPS CONFIG_KLIPPER_MMU_TMC_MICROSTEPS
#else
#define KM_FEEDER_MICROSTEPS 16
#endif

/* A saved header from before the millimetre menu cannot supply these, and a
 * zero would both divide by zero and silently rescale every move: refuse the
 * build and say what to do about it. */
#if !defined(CONFIG_KLIPPER_MMU_FEEDER_ROTATION_DISTANCE_UM) \
    || CONFIG_KLIPPER_MMU_FEEDER_ROTATION_DISTANCE_UM < 1 \
    || !defined(CONFIG_KLIPPER_MMU_FEEDER_FULL_STEPS) \
    || CONFIG_KLIPPER_MMU_FEEDER_FULL_STEPS < 1 \
    || !defined(CONFIG_KLIPPER_MMU_FEEDER_GEAR_RATIO_A) \
    || CONFIG_KLIPPER_MMU_FEEDER_GEAR_RATIO_A < 1 \
    || !defined(CONFIG_KLIPPER_MMU_FEEDER_GEAR_RATIO_B) \
    || CONFIG_KLIPPER_MMU_FEEDER_GEAR_RATIO_B < 1 \
    || KM_FEEDER_MICROSTEPS < 1
#error "klipper_mmu: regenerate config with tools/install.py (feeder mechanics need nonzero rotation distance, full steps, gear ratio and microsteps)"
#endif

#define KM_FEEDER_STEPS_PER_M \
    ((int64_t)((int64_t)(CONFIG_KLIPPER_MMU_FEEDER_FULL_STEPS) \
               * (KM_FEEDER_MICROSTEPS) \
               * (CONFIG_KLIPPER_MMU_FEEDER_GEAR_RATIO_A) * 1000000LL) \
     / ((int64_t)(CONFIG_KLIPPER_MMU_FEEDER_GEAR_RATIO_B) \
        * (CONFIG_KLIPPER_MMU_FEEDER_ROTATION_DISTANCE_UM)))

/* Millimetres to microsteps, rounded to the nearest microstep. A distance, a
 * speed, an acceleration and a jerk all scale identically. Micrometres/s per
 * permille also lands correctly on the core's microsteps/s x1000 gain,
 * because the micrometre and the x1000 cancel. */
#define KM_FEEDER_MM_TO_STEPS(mm) \
    ((uint32_t)(((int64_t)(mm) * KM_FEEDER_STEPS_PER_M + 500LL) / 1000LL))

/* The menu ranges end at these core ceilings under the reference mechanics,
 * but the mechanics are editable, so check the converted values here instead
 * of trusting the menu. */
_Static_assert(KM_FEEDER_STEPS_PER_M > 0,
               "klipper_mmu: feeder step scale must be positive");
_Static_assert(KM_FEEDER_MM_TO_STEPS(CONFIG_KLIPPER_MMU_FEED_SPEED_MM_S) > 0
               && KM_FEEDER_MM_TO_STEPS(CONFIG_KLIPPER_MMU_FEED_SPEED_MM_S)
                  <= KM_MOTION_MAX_VELOCITY_STEPS_S,
               "klipper_mmu: feed speed is outside what the control core accepts");
_Static_assert(KM_FEEDER_MM_TO_STEPS(CONFIG_KLIPPER_MMU_RETRACT_SPEED_MM_S) > 0
               && KM_FEEDER_MM_TO_STEPS(CONFIG_KLIPPER_MMU_RETRACT_SPEED_MM_S)
                  <= KM_MOTION_MAX_VELOCITY_STEPS_S,
               "klipper_mmu: retract speed is outside what the control core accepts");
_Static_assert(KM_FEEDER_MM_TO_STEPS(CONFIG_KLIPPER_MMU_ACCELERATION_MM_S2) > 0
               && KM_FEEDER_MM_TO_STEPS(CONFIG_KLIPPER_MMU_ACCELERATION_MM_S2)
                  <= KM_MOTION_MAX_ACCEL_STEPS_S2,
               "klipper_mmu: acceleration is outside what the control core accepts");
_Static_assert(KM_FEEDER_MM_TO_STEPS(CONFIG_KLIPPER_MMU_DECELERATION_MM_S2) > 0
               && KM_FEEDER_MM_TO_STEPS(CONFIG_KLIPPER_MMU_DECELERATION_MM_S2)
                  <= KM_MOTION_MAX_ACCEL_STEPS_S2,
               "klipper_mmu: deceleration is outside what the control core accepts");
_Static_assert(KM_FEEDER_MM_TO_STEPS(CONFIG_KLIPPER_MMU_JERK_MM_S3)
               <= KM_MOTION_MAX_JERK_STEPS_S3,
               "klipper_mmu: jerk is outside what the control core accepts");

/* The gain applies only with the DiffA/DiffB buffer; Klipper's autoconf
 * still defines the disabled entry, as 0. */
#if CONFIG_KLIPPER_MMU_DIFF_BUFFER
_Static_assert(KM_FEEDER_MM_TO_STEPS(CONFIG_KLIPPER_MMU_BUFFER_GAIN_UM_S) > 0
               && KM_FEEDER_MM_TO_STEPS(CONFIG_KLIPPER_MMU_BUFFER_GAIN_UM_S)
                  <= KM_MOTION_MAX_VELOCITY_STEPS_S * 1000u,
               "klipper_mmu: buffer gain is outside what the control core accepts");
#endif

/* The calibrated LOAD: the menu values that drive it are checked here, in
 * the same units the core uses, so a wiring mistake is a build error rather
 * than a buffer filled on the machine.
 *
 * The arrival leg matters most. The core declares arrival on a buffer sample,
 * keeps going through the hold and the samples that confirm it and then
 * brakes, so the follower is still moving for hold + samples x 5 ms + three
 * ticks after the filament actually arrived. That distance plus the braking
 * ramp has to fit in the travel the follower has left above the arrival
 * threshold, or a LOAD that arrives on time still drives the buffer full. */
#if CONFIG_KLIPPER_MMU_FPS_ARRIVAL
/* From the last buffer sample that still counts as arrival to the stop: the
 * hold, one 5 ms sample period per confirming sample, and three ticks of
 * rounding and retargeting. */
#define KM_FPS_ARRIVAL_LATENCY_MS \
    (CONFIG_KLIPPER_MMU_FPS_ARRIVAL_HOLD_MS \
     + 5 * CONFIG_KLIPPER_MMU_FPS_ARRIVAL_SAMPLES + 3)
/* Half of the buffer travel left past the arrival threshold. */
#define KM_FPS_ARRIVAL_HALF_STEPS \
    ((uint64_t)(1000 - CONFIG_KLIPPER_MMU_FPS_ARRIVAL_PERMILLE) \
     * KM_FEEDER_MM_TO_STEPS(CONFIG_KLIPPER_MMU_BUFFER_TRAVEL_MM) / 2000)
/* Travel through the latency at mm_s, plus the braking ramp 1.5 v^2 / (2 a)
 * = 3 v^2 / (4 a). Integer division truncates; that is one microstep of
 * optimism on a whole ramp. */
#define KM_FPS_ARRIVAL_STOP_STEPS(mm_s) \
    ((uint64_t)KM_FEEDER_MM_TO_STEPS(mm_s) * KM_FPS_ARRIVAL_LATENCY_MS / 1000 \
     + 3 * (uint64_t)KM_FEEDER_MM_TO_STEPS(mm_s) \
       * KM_FEEDER_MM_TO_STEPS(mm_s) \
       / (4 * KM_FEEDER_MM_TO_STEPS(CONFIG_KLIPPER_MMU_DECELERATION_MM_S2)))

_Static_assert(KM_FEEDER_MM_TO_STEPS(CONFIG_KLIPPER_MMU_LOAD_CRUISE_SPEED_MM_S) > 0
               && KM_FEEDER_MM_TO_STEPS(CONFIG_KLIPPER_MMU_LOAD_CRUISE_SPEED_MM_S)
                  <= KM_MOTION_MAX_VELOCITY_STEPS_S,
               "klipper_mmu: LOAD cruise speed is outside what the control core accepts");
_Static_assert(KM_FEEDER_MM_TO_STEPS(CONFIG_KLIPPER_MMU_LOAD_CRAWL_SPEED_MM_S) > 0
               && KM_FEEDER_MM_TO_STEPS(CONFIG_KLIPPER_MMU_LOAD_CRAWL_SPEED_MM_S)
                  < KM_FEEDER_MM_TO_STEPS(CONFIG_KLIPPER_MMU_LOAD_CRUISE_SPEED_MM_S),
               "klipper_mmu: LOAD crawl speed must be below the cruise speed");
/* The driver ceiling lives under pin config; without it there is none. */
#if CONFIG_KLIPPER_MMU_STEP_MAX_SPEED_MM_S
_Static_assert(KM_FEEDER_MM_TO_STEPS(CONFIG_KLIPPER_MMU_STEP_MAX_SPEED_MM_S)
               >= KM_FEEDER_MM_TO_STEPS(CONFIG_KLIPPER_MMU_LOAD_CRUISE_SPEED_MM_S),
               "klipper_mmu: the driver ceiling must admit the LOAD cruise speed");
#endif
/* Outer travel (FPS_ARRIVAL_MIN/MAX_MM), the window and the buffer travel are
 * all "0 means unset" for a custom device, exactly like the outer travel
 * bounds above: km_profile_validate rejects that combination at runtime.
 * Klipper's autoconf still defines each disabled entry, as 0, so the same
 * unset case must not fail the build first. Only a device that actually
 * configured every one of them gets the compile-time proof. */
#if CONFIG_KLIPPER_MMU_FPS_ARRIVAL_MIN_MM && CONFIG_KLIPPER_MMU_FPS_ARRIVAL_MAX_MM \
    && CONFIG_KLIPPER_MMU_LOAD_WINDOW_MM && CONFIG_KLIPPER_MMU_BUFFER_TRAVEL_MM
_Static_assert(KM_FEEDER_MM_TO_STEPS(CONFIG_KLIPPER_MMU_FPS_ARRIVAL_MAX_MM)
                  > KM_FEEDER_MM_TO_STEPS(CONFIG_KLIPPER_MMU_FPS_ARRIVAL_MIN_MM)
               && 2 * KM_FEEDER_MM_TO_STEPS(CONFIG_KLIPPER_MMU_LOAD_WINDOW_MM)
                  < KM_FEEDER_MM_TO_STEPS(CONFIG_KLIPPER_MMU_FPS_ARRIVAL_MAX_MM)
                    - KM_FEEDER_MM_TO_STEPS(CONFIG_KLIPPER_MMU_FPS_ARRIVAL_MIN_MM),
               "klipper_mmu: the LOAD arrival window must be narrower than half the outer travel spread");
_Static_assert(KM_FPS_ARRIVAL_STOP_STEPS(CONFIG_KLIPPER_MMU_LOAD_CRAWL_SPEED_MM_S)
                  <= KM_FPS_ARRIVAL_HALF_STEPS
               && KM_FPS_ARRIVAL_STOP_STEPS(CONFIG_KLIPPER_MMU_FEED_SPEED_MM_S)
                  <= KM_FPS_ARRIVAL_HALF_STEPS,
               "klipper_mmu: the LOAD approach cannot stop inside the buffer travel above the arrival threshold");
#endif
#endif

#endif /* KLIPPER_MMU_FEEDER_MECHANICS_H */
