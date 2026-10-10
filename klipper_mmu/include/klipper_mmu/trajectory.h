// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KLIPPER_MMU_TRAJECTORY_H
#define KLIPPER_MMU_TRAJECTORY_H

#include "klipper_mmu/types.h"
#include "klipper_mmu/driver.h"
#include "klipper_mmu/rate.h"
#include "klipper_mmu/step_segments.h"

#ifdef __cplusplus
extern "C" {
#endif



/**
 * Continuous local trajectory generator.
 * Maintains local velocity, acceleration, and signed displacement without
 * host-planned distance chunks. Employs next-edge deadlines and an explicit
 * bounded timer service for real hardware and fake monotonic clocks.
 */
typedef struct {
    km_rate_t rate;

    km_driver_t *driver;
    /* Flip every emitted step segment's physical dir bit, leaving the
     * position/rate model (and every caller computing from it) in the same
     * logical, lane-relative sense (klipper_mmu/selector.h: a ROTARY gate
     * whose gear_direction is REVERSE). False for everything else. */
    bool invert_direction;

    /* Per-lane rotation-distance scale, an integer ratio reduced to lowest
     * terms: physical_steps = reference_steps * scale_num / scale_den.
     * "Reference" is the unit's own KLIPPER_MMU_FEEDER_ROTATION_DISTANCE_UM
     * mechanics (mcu/feeder_mechanics.h), what every profile distance and
     * speed is already expressed in; "physical" is what this struct's rate
     * model, limits, jerk, stop-distance math, window generation and
     * observe/resync actually run on, to match the active lane's real
     * rotation distance. km_trajectory_set_lane_scale is the only writer;
     * km_trajectory_init resets both to 1 (identity). Every other public
     * function converts at this boundary: 1:1 (the default, and every build
     * that never calls set_lane_scale) takes a fast path with no rounding
     * at all, so today's arithmetic is unchanged bit-for-bit. */
    uint32_t scale_num;
    uint32_t scale_den;

    /* Output state */
    int64_t current_position_steps;
    /* km_trajectory_observe's completed_steps is the driver's own raw
     * step count (mcu/stepdir_gpio.c's s->position), a net accumulator
     * since that driver's first activation -- it knows nothing about
     * km_trajectory_recover's declared baseline. Added to every raw
     * completed_steps report so current_position_steps stays in the
     * same absolute frame recover just established (e.g. "0 at the home
     * switch"), even though the driver's own counter never gets rebased.
     * recover cannot compute this offset itself: it may run right after
     * km_trajectory_bind_driver switched to a different physical driver
     * (select_bind_stepper), whose own raw count has no relation at all to
     * current_position_steps's last value (the *previous* driver's). So
     * recover only marks position_offset_pending; km_trajectory_observe
     * seeds the real offset from the first raw count it actually sees
     * afterward (same driver or not), against the position recover
     * declared, then clears the flag. resync clears it outright (a fault
     * trusts the driver's raw count as ground truth again, no declared
     * baseline to reconcile). */
    int64_t position_offset;
    bool position_offset_pending;
    bool position_uncertain;
    bool faulted;
    bool stepper_active;
    uint32_t tick_seq;
    uint32_t anchor_ticks;
    /* Time base */
    uint32_t last_update_us;
    bool clock_valid;

    /* Limits */
    uint32_t max_velocity_steps_s;
    uint32_t acceleration_steps_s2;
    uint32_t deceleration_steps_s2;
    uint32_t jerk_steps_s3;

    /* Mirrors for callers */
    int32_t current_velocity_steps_s;
    int64_t model_position_steps;
    int32_t target_velocity_steps_s;
    bool has_target_position;
    int64_t target_position_steps;
    uint32_t move_velocity_limit;
    bool is_active;

    /* Controller sequence */
    uint32_t command_seq;
    uint32_t reset_seq; /* Bumped by km_trajectory_recover; see km_trajectory_publish. */
} km_trajectory_t;

/**
 * Initialize trajectory generator. Limits (here and in set_limits) are rejected
 * when max_vel would take 2^29 us or longer to reach or stop, the integer STEP
 * image's span.
 */
KM_API km_result_t km_trajectory_init(km_trajectory_t *traj,
                               uint32_t max_vel,
                               uint32_t accel,
                               uint32_t decel);

KM_API km_result_t km_trajectory_set_jerk(km_trajectory_t *t, uint32_t jerk_steps_s3);

/**
 * Bind an abstract driver to receive step events.
 */
KM_API void km_trajectory_bind_driver(km_trajectory_t *traj, km_driver_t *driver);

/**
 * Set this trajectory's lane scale (see km_trajectory_t's own comment):
 * physical_steps = reference_steps * scale_num / scale_den. Both must be
 * nonzero; the ratio is reduced to lowest terms before it is stored, so a
 * caller need not reduce it itself and scale_num == scale_den always takes
 * the identity fast path regardless of the values passed. Refused while
 * moving (KM_ERR_BUSY), so a change never reinterprets motion already under
 * way. Rebases every physical field this trajectory already carries (position,
 * velocity/acceleration/deceleration/jerk limits) from the old scale to the
 * new one, so get_position() reports the same reference distance immediately
 * before and after the call -- callers need not resend limits or position
 * afterward. KM_ERR_INVALID_PARAM if a rebased limit would fall outside the
 * motion core's own range.
 */
KM_API km_result_t km_trajectory_set_lane_scale(km_trajectory_t *traj,
                                                uint32_t scale_num,
                                                uint32_t scale_den);

/**
 * Update limits at the last integrated timestamp. Active callers should use
 * the timestamped variant to include travel since the previous service.
 */
KM_API km_result_t km_trajectory_set_limits(km_trajectory_t *traj,
                                     uint32_t max_vel,
                                     uint32_t accel,
                                     uint32_t decel);

KM_API km_result_t km_trajectory_set_limits_at(km_trajectory_t *traj,
                                        uint32_t max_vel, uint32_t accel,
                                        uint32_t decel, uint32_t now_us);

/**
 * Retarget velocity continuously without restarting the ramp from zero.
 * Supports smooth direction reversals with zero-crossing.
 * Extreme inputs (including INT32_MIN) are clamped safely with no signed overflow.
 * After the same checks, an unchanged clamped target at the model's own update
 * time (FOLLOW's control tick) keeps the existing plan instead of rebuilding it.
 */
KM_API km_result_t km_trajectory_set_target_velocity(km_trajectory_t *traj,
                                              int32_t target_vel,
                                              uint32_t now_us);

/**
 * Set an absolute position with local acceleration/cruise/braking phases.
 * Rejects a target inside stopping distance or opposite to current velocity;
 * the caller may first request a graceful stop. Rejection preserves the plan.
 * Commands also reject a phase 2^29 us or more from zero speed, which only
 * braking from above a lowered limit can produce.
 */
KM_API km_result_t km_trajectory_set_target_position(km_trajectory_t *traj,
                                              int64_t target_pos,
                                              uint32_t max_vel,
                                              uint32_t now_us);

/**
 * Gracefully cancel motion using the configured deceleration rate.
 */
KM_API km_result_t km_trajectory_cancel(km_trajectory_t *traj, uint32_t now_us);

/**
 * Immediate stop (e.g. emergency or limit trip), clearing velocity, deadlines,
 * and calling driver shutdown.
 */
KM_API void km_trajectory_emergency_stop(km_trajectory_t *traj);

/* Deliberate recovery after the caller establishes position. Driver must be
 * disabled and free of a reported hardware fault. Does not enable outputs. */
KM_API km_result_t km_trajectory_recover(km_trajectory_t *traj, int64_t position,
                                 uint32_t now_us);

/**
 * Advance continuous trajectory motion model to now_us (periodic control and
 * commands; STEP edges do not call it). Driverless models output this position.
 * Accounts for actual ramps, exact displacement, signed fractions, and limit changes.
 * @return Number of whole steps calculated in this model step.
 */
KM_API uint32_t km_trajectory_update(km_trajectory_t *traj, uint32_t now_us);

/**
 * Task/interrupt split (MCU controller). An interrupt services a separate edge
 * copy that owns the output: position, fault, stepper activity. The task plans
 * in its own copy. The caller masks interrupts around publish and collect,
 * which are integer only and now carry phase, M, completed steps, stepper
 * activity and a tick sequence number.
 */
/*
 * B2 tick API (contract; docs/plan/b2-stepper-segments.md "The controller").
 * The rate core runs on a fixed tick; each tick becomes one window of step
 * segments that the MCU adapter queues into Klipper's stepper.c (natively, a
 * reference executor plays stepper.c). It replaces km_trajectory_service and
 * the edge deadline. All integer, allocation-free, safe in the timer callback.
 *
 * Positions, and who uses which:
 *   rate phase (traj->rate)         the core; runs a lead ahead of the motor.
 *   model_position_steps (M)        the driver microstep after the last
 *                                   queued window (exact-reach rule).
 *   current_position_steps          completed steps, from km_trajectory_observe;
 *                                   what km_trajectory_get_position reports.
 * Arrival (km_trajectory_is_moving false, target cleared) needs all three: the
 * phase landed on the target, completed steps equal the target, and the
 * stepper is idle. The follower's control law reads the core (velocity);
 * distance accounting and anything tied to a sensor sample read completed
 * steps.
 */
typedef struct {
    km_rate_t rate;           /* the core at the end of the window */
    int64_t microstep;        /* M after the window */
    uint32_t anchor_ticks;    /* generator anchor after the window */
    uint8_t count;            /* segments in the window, 0..KM_STEP_SEGMENTS_MAX */
    km_step_segment_t segments[KM_STEP_SEGMENTS_MAX];
} km_trajectory_window_t;

/**
 * Compute the next window: advance a copy of the core by tick_us (targets as
 * published before this call apply to this window) and turn it into segments
 * with km_step_segments_generate, starting from the committed M and anchor.
 * traj is not modified. anchor_restart true means the stepper is idle and the
 * caller will give this window's start clock to stepper.c: the first interval
 * is measured from the window start, while the committed anchor (the time
 * since the last step) still bounds the spacing (the generator's restart). A faulted, emergency-stopped or driverless trajectory yields an
 * empty window with the core held. Errors: the generator's (a fault: the
 * caller stops, resyncs and latches it).
 */
KM_API km_result_t km_trajectory_window(const km_trajectory_t *traj, uint32_t tick_us,
                                        uint32_t ticks_per_us,
                                        uint32_t min_interval_ticks,
                                        bool anchor_restart,
                                        km_trajectory_window_t *out);

/** Adopt a window whose segments were all queued (all or nothing). */
KM_API void km_trajectory_commit(km_trajectory_t *traj, const km_trajectory_window_t *w);

/**
 * Report the motor's progress: completed steps (stepper_local_position,
 * unwrapped to 64 bits by the caller) and whether stepper.c is still active.
 * Drives current_position_steps and arrival.
 */
KM_API void km_trajectory_observe(km_trajectory_t *traj, int64_t completed_steps,
                                  bool stepper_active);

/**
 * After any stop (fault, lease, underrun, cancel with an emergency stop):
 * M, the phase and current_position_steps become completed_steps, fraction,
 * rate and acceleration 0, targets cleared, anchor invalid (the next window
 * restarts). uncertain marks the position mechanically uncertain (the count
 * itself is exact).
 */
KM_API void km_trajectory_resync(km_trajectory_t *traj, int64_t completed_steps,
                                 bool uncertain);

KM_API bool km_trajectory_publish(km_trajectory_t *edge, const km_trajectory_t *plan,
                                  uint32_t now);
KM_API void km_trajectory_collect(km_trajectory_t *plan, const km_trajectory_t *edge);

/* Boundary getters: convert this trajectory's internal physical position/
 * velocity back to the reference domain (km_trajectory_t's scale comment),
 * the exact inverse of what km_trajectory_set_target_position/
 * set_target_velocity/recover apply on the way in. 1:1 (scale_num ==
 * scale_den) returns the stored value unchanged, same as before this scale
 * existed. */
KM_API int64_t km_trajectory_get_position(const km_trajectory_t *traj);
KM_API int64_t km_trajectory_get_model_position(const km_trajectory_t *traj);
KM_API int32_t km_trajectory_get_velocity(const km_trajectory_t *traj);

/* Stopping distance from the current rate, in reference steps (the boundary
 * conversion of km_rate_stop_distance_q32(&traj->rate) >> 32, this
 * trajectory's own physical Q32 result). */
KM_API int64_t km_trajectory_get_stop_distance(const km_trajectory_t *traj);

static inline bool km_trajectory_is_position_uncertain(const km_trajectory_t *traj)
{
    return traj ? traj->position_uncertain : false;
}

static inline bool km_trajectory_is_moving(const km_trajectory_t *traj)
{
    return traj ? (traj->is_active || traj->current_velocity_steps_s != 0 || traj->stepper_active) : false;
}

#ifdef __cplusplus
}
#endif

#endif /* KLIPPER_MMU_TRAJECTORY_H */
