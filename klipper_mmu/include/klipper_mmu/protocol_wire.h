// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KLIPPER_MMU_PROTOCOL_WIRE_H
#define KLIPPER_MMU_PROTOCOL_WIRE_H

#include "klipper_mmu/types.h"
#include "klipper_mmu/event_queue.h"
#include "klipper_mmu/follower.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Upstream OpenAMS wire action constants */
#define KM_WIRE_STATUS_LOADING           0u
#define KM_WIRE_STATUS_UNLOADING         1u
#define KM_WIRE_STATUS_FORWARD_FOLLOWING 2u
#define KM_WIRE_STATUS_REVERSE_FOLLOWING 3u
#define KM_WIRE_STATUS_COASTING          4u
#define KM_WIRE_STATUS_STOPPED           5u
#define KM_WIRE_STATUS_CALIBRATING       6u
#define KM_WIRE_STATUS_ERROR             7u

/* Upstream OpenAMS wire result code constants */
#define KM_WIRE_OP_CODE_SUCCESS                0u
#define KM_WIRE_OP_CODE_ERROR_UNSPECIFIED      1u
#define KM_WIRE_OP_CODE_ERROR_BUSY             2u
#define KM_WIRE_OP_CODE_SPOOL_ALREADY_IN_BAY   3u
#define KM_WIRE_OP_CODE_NO_SPOOL_IN_BAY        4u
#define KM_WIRE_OP_CODE_ERROR_KLIPPER_CALL     5u
#define KM_WIRE_OP_CODE_CANCEL                 6u
#define KM_WIRE_OP_CODE_TIMEOUT                7u

/**
 * Standard wire message structure corresponding to oams_action_status / oams_action_status2.
 */
typedef struct {
    bool valid; /* False when a legacy 8-bit wire message cannot represent the event. */
    uint8_t action;
    uint8_t code;
    uint32_t value;
    uint8_t gen;
    bool has_gen;
} km_wire_action_status_t;

/**
 * Full-fidelity internal status snapshot.
 * Independent of transport and serialization.
 */
typedef struct {
    km_op_state_t op_state;
    km_motion_stage_t stage;
    km_operation_type_t active_op;
    km_client_id_t active_client;
    uint32_t active_session;
    uint32_t active_req_id;
    uint8_t active_lane;
    bool lane_active;
    bool terminal_pending;
    uint8_t path_owner_lane; /* Meaningful only while lane_active. */
    bool path_occupied;
    uint8_t path_occupant_lane; /* Meaningful only while path_occupied. */
    int32_t current_velocity_steps_s;
    int64_t current_position_steps;
    bool position_uncertain; /* Pulse accounting, not an absolute homing claim. */
    int32_t buffer_position_permille;
    bool buffer_valid;
    uint16_t commanded_current_ma;
    uint16_t measured_current_ma; /* Rewinder measurement, never feeder setpoint. */
    uint32_t measured_current_sample_ms;
    bool measured_current_available;
    km_fault_t system_fault;
    km_fault_t motion_fault;
    km_follower_state_t follower_state; /* Armed includes idle between motor pulses. */
    km_direction_t follower_direction; /* Meaningful only while armed. */
    km_fault_t follower_fault;
    bool host_connected;
    bool motor_ready; /* Global admission readiness, not per-lane qualification. */
    bool control_time_valid;
    uint32_t control_time_ms;
    uint32_t control_time_us; /* Last accepted core time, not the query's time. */
    uint32_t client_session[KM_MAX_CLIENTS];
    uint32_t client_request[KM_MAX_CLIENTS]; /* Highest admitted identity per client. */
    uint8_t queued_events;
    uint32_t head_event_seq; /* Zero when empty; reading does not acknowledge. */
    bool pending_fault_event; /* Unsolicited fault waiting for queue space. */
    bool selector_homed;      /* Selector machines only. */
    uint8_t selector_gate;    /* Selected gate; meaningful only while homed. */
} km_status_snapshot_t;

/**
 * Encode a terminal event into wire representation.
 * Terminal events carry the operation generation and terminal result code.
 */
km_wire_action_status_t km_protocol_wire_encode_event(const km_event_t *event);

/**
 * Encode a status/telemetry snapshot into wire representation.
 * In-progress operations report busy state rather than terminal success completions.
 */
km_wire_action_status_t km_protocol_wire_encode_snapshot(const km_status_snapshot_t *snapshot);

/**
 * Convert admission result and fault to OpenAMS wire code.
 */
uint8_t km_protocol_result_code(km_admission_result_t adm, km_fault_t fault);

#ifdef __cplusplus
}
#endif

#endif /* KLIPPER_MMU_PROTOCOL_WIRE_H */
