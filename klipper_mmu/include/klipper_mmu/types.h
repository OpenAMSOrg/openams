// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KLIPPER_MMU_TYPES_H
#define KLIPPER_MMU_TYPES_H

#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Integration/verification builds may retain public ABI entry points under
 * Klipper's whole-program LTO. Ordinary native builds need no compiler attribute. */
#if defined(KM_RETAIN_API) && defined(__GNUC__)
#define KM_API __attribute__((externally_visible))
#else
#define KM_API
#endif

/* Standard bounds and capacity */
/* Lane capacity. mcu/Makefile sets 8 for Klipper images of up to 8 lanes, so
 * they keep the 8-lane RAM layout (32 KB parts such as the G431 need it). */
#ifndef KM_MAX_LANES
#define KM_MAX_LANES              12u
#endif
#define KM_LANE_UNKNOWN           UINT8_MAX /* Occupied path, source not established. */
#define KM_MAX_PATHS              1u /* One explicit bounded shared path per core */
#define KM_MAX_CLIENTS            4u
#define KM_MAX_CAL_POINTS         8u
#define KM_EVENT_QUEUE_CAPACITY   16u

/* Integer range definitions */
#define KM_PERMILLE_MIN           0
#define KM_PERMILLE_MAX           1000
#define KM_PERMILLE_CENTER        500

/* Time constant and wrap horizon */
#define KM_INVALID_TIME           UINT32_MAX
#define KM_TIME_WRAP_HORIZON_US   0x7FFFFFFFu

/* Supported motion numeric contracts */
#define KM_MOTION_MAX_VELOCITY_STEPS_S 200000u
#define KM_MOTION_MAX_ACCEL_STEPS_S2   2000000u
#define KM_MOTION_MAX_JERK_STEPS_S3    100000000u
#define KM_MOTION_MAX_POSITION_STEPS   1000000000000LL
#define KM_MOTION_MIN_POSITION_STEPS  (-1000000000000LL)

/**
 * Return elapsed time in ticks/ms/us with correct unsigned 32-bit modular wrap.
 */
static inline uint32_t km_time_elapsed(uint32_t now, uint32_t then)
{
    return now - then;
}

/**
 * Check if timestamp 'a' is chronologically before timestamp 'b' within the wrap horizon.
 */
static inline bool km_time_before(uint32_t a, uint32_t b)
{
    return (int32_t)(a - b) < 0;
}

/**
 * Check if timestamp 'a' is chronologically after timestamp 'b' within the wrap horizon.
 */
static inline bool km_time_after(uint32_t a, uint32_t b)
{
    return (int32_t)(a - b) > 0;
}

/**
 * Check if timestamp 'a' is chronologically before or equal to 'b' within the wrap horizon.
 */
static inline bool km_time_before_or_eq(uint32_t a, uint32_t b)
{
    return (int32_t)(a - b) <= 0;
}

/**
 * Check if timestamp 'a' is chronologically after or equal to 'b' within the wrap horizon.
 */
static inline bool km_time_after_or_eq(uint32_t a, uint32_t b)
{
    return (int32_t)(a - b) >= 0;
}

/**
 * Known client categories for the shared arbiter.
 */
typedef enum {
    KM_CLIENT_NONE     = 0,
    KM_CLIENT_HOST     = 1, /* Klipper host OpenAMS adapter */
    KM_CLIENT_LOCAL_UI = 2, /* Local device screen / buttons */
    KM_CLIENT_INTERNAL = 3  /* Internal autonomous sequencer */
} km_client_id_t;

/**
 * Operation types for request identity and arbiter tracking.
 */
typedef enum {
    KM_OP_TYPE_NONE      = 0,
    KM_OP_TYPE_LOAD      = 1,
    KM_OP_TYPE_UNLOAD    = 2,
    KM_OP_TYPE_FOLLOW    = 3,
    KM_OP_TYPE_CALIBRATE = 4,
    KM_OP_TYPE_CANCEL    = 5,
    KM_OP_TYPE_PRELOAD   = 6,
    KM_OP_TYPE_EJECT     = 7,
    KM_OP_TYPE_HOME      = 8  /* Home the selector (klipper_mmu/selector.h). */
} km_operation_type_t;

/**
 * Full request identity across load/unload/follow/calibrate/cancel.
 */
typedef struct {
    km_client_id_t client;
    uint32_t session_id;
    uint32_t req_id;
    km_operation_type_t op_type;
    uint8_t lane_idx;
} km_request_identity_t;

/**
 * Return codes for internal function results.
 */
typedef enum {
    KM_OK                   =  0,
    KM_ERR_INVALID_PARAM    = -1,
    KM_ERR_BUSY             = -2,
    KM_ERR_UNAVAILABLE      = -3,
    KM_ERR_FAULTED          = -4,
    KM_ERR_CONFLICT         = -5,
    KM_ERR_STALE            = -6,
    KM_ERR_TIMEOUT          = -7,
    KM_ERR_NOT_SUPPORTED    = -8,
    KM_ERR_QUEUE_FULL       = -9,
    KM_ERR_QUEUE_EMPTY      = -10
} km_result_t;

/**
 * Admission result for requested operations.
 */
typedef enum {
    KM_REQ_ACCEPTED            = 0,
    KM_REQ_ALREADY_IN_PROGRESS = 1, /* Idempotent duplicate accepted */
    KM_REQ_BUSY                = 2, /* Busy with another op or client */
    KM_REQ_INVALID_LANE        = 3, /* Lane index out of range */
    KM_REQ_NO_SPOOL            = 4, /* No filament present at lane */
    KM_REQ_PATH_OCCUPIED       = 5, /* Shared path owned or occupied by another lane */
    KM_REQ_FAULTED             = 6, /* Subsystem or driver in fault */
    KM_REQ_UNAVAILABLE         = 7, /* Hardware or capability not ready */
    KM_REQ_REJECTED_STALE      = 8, /* Generation/ID/session mismatch on cancel */
    KM_REQ_REJECTED_CONFLICT   = 9, /* Client conflict */
    KM_REQ_ALREADY_COMPLETED   = 10 /* Retained terminal; query by full identity */
} km_admission_result_t;

/**
 * System and motion fault codes.
 */
typedef enum {
    KM_FAULT_NONE              = 0,
    KM_FAULT_HOST_COMM_LOST    = 1,
    KM_FAULT_DRIVER_OVERCURRENT= 2,
    KM_FAULT_DRIVER_FAULT      = 3,
    KM_FAULT_NO_PROGRESS       = 4,
    KM_FAULT_JAM               = 5,
    KM_FAULT_BUFFER_SIGNAL     = 6,
    KM_FAULT_FILAMENT_LOST     = 7,
    KM_FAULT_STARVED           = 8,
    KM_FAULT_STALLED           = 9,
    KM_FAULT_EMERGENCY_STOP    = 10,
    KM_FAULT_SENSOR_FAILURE    = 11,
    KM_FAULT_CONTROL_TIMING    = 12,
    KM_FAULT_SELECTOR          = 13 /* Selector homing failed or position lost. */
} km_fault_t;

/**
 * Top-level operation state of the MMU.
 */
typedef enum {
    KM_OP_STATE_IDLE        = 0,
    KM_OP_STATE_LOADING     = 1,
    KM_OP_STATE_LOADED      = 2,
    KM_OP_STATE_UNLOADING   = 3,
    KM_OP_STATE_FOLLOWING   = 4,
    KM_OP_STATE_CALIBRATING = 5,
    KM_OP_STATE_FAULT       = 6,
    KM_OP_STATE_PRELOADING  = 7,
    KM_OP_STATE_PRELOADED   = 8,
    KM_OP_STATE_EJECTING    = 9
} km_op_state_t;

/**
 * Internal execution stages for continuous motion.
 */
typedef enum {
    KM_STAGE_STOPPED               = 0,
    KM_STAGE_LOAD_FEED_TO_HUB      = 1,
    KM_STAGE_LOAD_FEED_TO_BUFFER   = 2,
    KM_STAGE_FOLLOW                = 3,
    KM_STAGE_UNLOAD_RETRACT_TO_HUB = 4,
    KM_STAGE_UNLOAD_REWIND_TO_SPOOL= 5,
    KM_STAGE_UNLOAD_STOP_SETTLE    = 6,
    KM_STAGE_CALIBRATE_PROBE       = 7,
    KM_STAGE_CALIBRATE_RETRACT     = 8,
    KM_STAGE_BRAKING               = 9,
    KM_STAGE_UNLOAD_RETRACT_TO_LOAD= 10,
    KM_STAGE_EJECT_TO_BAY          = 11,
    KM_STAGE_LOAD_RETRY_RETRACT    = 12, /* LOAD: back off before feeding again. */
    KM_STAGE_SELECT_HOME           = 13, /* Selector: establish zero at its endstop. */
    KM_STAGE_SELECT_MOVE           = 14, /* Selector: move to the requested gate. */
    KM_STAGE_SERVO_DWELL           = 15  /* Servo: wait for it to reach position. */
} km_motion_stage_t;

/**
 * Follower direction.
 */
typedef enum {
    KM_DIR_REVERSE = 0,
    KM_DIR_FORWARD = 1
} km_direction_t;

#ifdef __cplusplus
}
#endif

#endif /* KLIPPER_MMU_TYPES_H */
