// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KLIPPER_MMU_SELECTOR_HOME_H
#define KLIPPER_MMU_SELECTOR_HOME_H
#include "klipper_mmu/trajectory.h"
typedef struct
{
    uint32_t speed_steps_s, acceleration_steps_s2, deceleration_steps_s2, max_travel_steps,
        deadline_us, max_switch_age_us;
    km_direction_t home_direction;
} km_selector_home_config_t;
typedef struct
{
    bool asserted;
    uint32_t sampled_at_us;
    bool valid;
} km_selector_home_switch_t;
typedef enum
{
    KM_SELECTOR_HOME_IDLE,
    KM_SELECTOR_HOME_RUNNING,
    KM_SELECTOR_HOME_HOMED,
    KM_SELECTOR_HOME_FAILED
} km_selector_home_state_t;
typedef struct
{
    km_trajectory_t *trajectory;
    km_driver_t *driver;
    km_selector_home_config_t config;
    uint32_t started_at_us, last_sample_us;
    int64_t starting_position;
    /* The trajectory's position, in this call's own frame, at the instant the
     * switch was found -- captured before recovering it to 0, so a caller
     * that also tracks this same continuous frame from further back (e.g. a
     * pressed-switch backoff before this call even started) can still
     * recover the true net travel across both. Meaningful only once state
     * is HOMED. */
    int64_t found_position;
    km_selector_home_state_t state;
    km_result_t result;
    bool position_known;
} km_selector_home_t;
/* Caller exclusively owns/interlocks the selector, verifies switch and direction,
 * and services every trajectory deadline. No profile or board integration exists.
 * Driver must be initialized, fault-free, and disabled at entry. A fresh inactive
 * switch at entry, at least one completed trajectory pulse, and a newer asserted
 * switch sample are required to establish zero; early assertion fails with
 * unknown position.
 * Zero-initialize the state before first use; FAILED remains latched until
 * deliberate reset. */
KM_API km_result_t km_selector_home_start(km_selector_home_t *, km_trajectory_t *, km_driver_t *,
                                          const km_selector_home_config_t *,
                                          km_selector_home_switch_t, uint32_t);
KM_API km_result_t km_selector_home_service(km_selector_home_t *, km_selector_home_switch_t,
                                            uint32_t);
KM_API km_result_t km_selector_home_cancel(km_selector_home_t *);
#endif
