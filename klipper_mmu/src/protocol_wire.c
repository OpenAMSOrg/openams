// SPDX-License-Identifier: GPL-3.0-or-later
#include "klipper_mmu/protocol_wire.h"

uint8_t km_protocol_result_code(km_admission_result_t adm, km_fault_t fault)
{
    if (fault == KM_FAULT_NO_PROGRESS || fault == KM_FAULT_STALLED) {
        return KM_WIRE_OP_CODE_TIMEOUT;
    }
    if (fault == KM_FAULT_EMERGENCY_STOP || fault == KM_FAULT_HOST_COMM_LOST) {
        return KM_WIRE_OP_CODE_ERROR_KLIPPER_CALL;
    }
    if (fault != KM_FAULT_NONE) {
        return KM_WIRE_OP_CODE_ERROR_UNSPECIFIED;
    }

    switch (adm) {
    case KM_REQ_ACCEPTED:
    case KM_REQ_ALREADY_IN_PROGRESS:
    case KM_REQ_ALREADY_COMPLETED:
        return KM_WIRE_OP_CODE_SUCCESS;
    case KM_REQ_BUSY:
    case KM_REQ_REJECTED_CONFLICT:
        return KM_WIRE_OP_CODE_ERROR_BUSY;
    case KM_REQ_PATH_OCCUPIED:
        return KM_WIRE_OP_CODE_SPOOL_ALREADY_IN_BAY;
    case KM_REQ_NO_SPOOL:
    case KM_REQ_INVALID_LANE:
        return KM_WIRE_OP_CODE_NO_SPOOL_IN_BAY;
    case KM_REQ_FAULTED:
    case KM_REQ_UNAVAILABLE:
        return KM_WIRE_OP_CODE_ERROR_UNSPECIFIED;
    case KM_REQ_REJECTED_STALE:
        return KM_WIRE_OP_CODE_CANCEL;
    }
    return KM_WIRE_OP_CODE_ERROR_UNSPECIFIED;
}

km_wire_action_status_t km_protocol_wire_encode_event(const km_event_t *event)
{
    km_wire_action_status_t wire = {0};
    wire.action = KM_WIRE_STATUS_STOPPED;
    wire.code = KM_WIRE_OP_CODE_SUCCESS;
    wire.value = 0;
    wire.gen = 0;
    wire.has_gen = false;

    if (!event) {
        return wire;
    }

    /* The legacy OpenAMS wire format has no preload action. Do not make a
     * hub-only terminal look like a successful LOAD to an old host. */
    if (event->op_type == KM_OP_TYPE_PRELOAD || event->op_type == KM_OP_TYPE_EJECT) return wire;

    /* Legacy 8-bit generations need a host-maintained mapping. Never alias
     * full internal identities by truncating them. */
    if (event->req_id > UINT8_MAX || event->req_id == 0) {
        wire.action = KM_WIRE_STATUS_ERROR;
        wire.code = KM_WIRE_OP_CODE_ERROR_UNSPECIFIED;
        return wire;
    }
    wire.valid = true;
    wire.gen = (uint8_t)event->req_id;
    wire.has_gen = true;
    wire.value = event->value;

    switch (event->type) {
    case KM_EVENT_LOAD_COMPLETE:
        wire.action = KM_WIRE_STATUS_LOADING;
        wire.code = KM_WIRE_OP_CODE_SUCCESS;
        break;
    case KM_EVENT_LOAD_CANCELLED:
        wire.action = KM_WIRE_STATUS_LOADING;
        wire.code = KM_WIRE_OP_CODE_CANCEL;
        break;
    case KM_EVENT_LOAD_FAILED:
        wire.action = KM_WIRE_STATUS_LOADING;
        wire.code = (event->result_code != 0) ? event->result_code : KM_WIRE_OP_CODE_ERROR_UNSPECIFIED;
        break;
    case KM_EVENT_UNLOAD_COMPLETE:
        wire.action = KM_WIRE_STATUS_UNLOADING;
        wire.code = KM_WIRE_OP_CODE_SUCCESS;
        break;
    case KM_EVENT_UNLOAD_CANCELLED:
        wire.action = KM_WIRE_STATUS_UNLOADING;
        wire.code = KM_WIRE_OP_CODE_CANCEL;
        break;
    case KM_EVENT_UNLOAD_FAILED:
        wire.action = KM_WIRE_STATUS_UNLOADING;
        wire.code = (event->result_code != 0) ? event->result_code : KM_WIRE_OP_CODE_ERROR_UNSPECIFIED;
        break;
    case KM_EVENT_FOLLOW_FAULT:
        wire.action = KM_WIRE_STATUS_ERROR;
        wire.code = KM_WIRE_OP_CODE_ERROR_UNSPECIFIED;
        break;
    case KM_EVENT_PTFE_CAL_COMPLETE:
        wire.action = KM_WIRE_STATUS_CALIBRATING;
        wire.code = KM_WIRE_OP_CODE_SUCCESS;
        /* Legacy PTFE values are encoder lengths; never alias a step estimate. */
        wire.valid = !(event->flags & KM_EVENT_FLAG_STEP_ESTIMATE);
        break;
    case KM_EVENT_PTFE_CAL_CANCELLED:
        wire.action = KM_WIRE_STATUS_CALIBRATING;
        wire.code = KM_WIRE_OP_CODE_CANCEL;
        break;
    case KM_EVENT_PTFE_CAL_FAILED:
        wire.action = KM_WIRE_STATUS_CALIBRATING;
        wire.code = (event->result_code != 0) ? event->result_code : KM_WIRE_OP_CODE_ERROR_UNSPECIFIED;
        break;
    case KM_EVENT_STOPPED:
        wire.action = KM_WIRE_STATUS_STOPPED;
        wire.code = event->result_code;
        break;
    case KM_EVENT_FAULT_ENTERED:
        wire.action = KM_WIRE_STATUS_ERROR;
        wire.code = KM_WIRE_OP_CODE_ERROR_UNSPECIFIED;
        break;
    case KM_EVENT_PRELOAD_COMPLETE:
    case KM_EVENT_PRELOAD_CANCELLED:
    case KM_EVENT_PRELOAD_FAILED:
    case KM_EVENT_EJECT_COMPLETE:
    case KM_EVENT_EJECT_CANCELLED:
    case KM_EVENT_EJECT_FAILED:
    case KM_EVENT_HOME_COMPLETE:
    case KM_EVENT_HOME_FAILED:
        /* No legacy OpenAMS wire action exists for selector homing. */
        wire.valid = false;
        break;
    case KM_EVENT_NONE:
        break;
    }
    return wire;
}

km_wire_action_status_t km_protocol_wire_encode_snapshot(const km_status_snapshot_t *s)
{
    km_wire_action_status_t wire = {0};
    wire.action = KM_WIRE_STATUS_STOPPED;
    wire.code = KM_WIRE_OP_CODE_SUCCESS;
    wire.value = 0;
    wire.gen = s && s->active_req_id <= UINT8_MAX ? (uint8_t)s->active_req_id : 0;
    wire.has_gen = false; /* Replaceable telemetry snapshot does not claim generation-tagged terminal event */

    if (!s) {
        return wire;
    }

    wire.valid = true;
    if (s->system_fault != KM_FAULT_NONE || s->motion_fault != KM_FAULT_NONE) {
        wire.action = KM_WIRE_STATUS_ERROR;
        wire.code = km_protocol_result_code(KM_REQ_FAULTED, s->motion_fault != KM_FAULT_NONE ? s->motion_fault : s->system_fault);
        wire.value = (uint32_t)(s->motion_fault != KM_FAULT_NONE ? s->motion_fault : s->system_fault);
        return wire;
    }

    switch (s->op_state) {
    case KM_OP_STATE_PRELOADING:
    case KM_OP_STATE_PRELOADED:
    case KM_OP_STATE_EJECTING:
        wire.valid = false;
        break;
    case KM_OP_STATE_LOADING:
        /* Critical: in-progress loading must NOT be encoded as completion-class success */
        wire.action = KM_WIRE_STATUS_LOADING;
        wire.code = KM_WIRE_OP_CODE_ERROR_BUSY;
        break;
    case KM_OP_STATE_UNLOADING:
        /* Critical: in-progress unloading must NOT be encoded as completion-class success */
        wire.action = KM_WIRE_STATUS_UNLOADING;
        wire.code = KM_WIRE_OP_CODE_ERROR_BUSY;
        break;
    case KM_OP_STATE_FOLLOWING:
        wire.action = (s->current_velocity_steps_s >= 0) ? KM_WIRE_STATUS_FORWARD_FOLLOWING
                                                         : KM_WIRE_STATUS_REVERSE_FOLLOWING;
        wire.code = KM_WIRE_OP_CODE_SUCCESS;
        break;
    case KM_OP_STATE_CALIBRATING:
        wire.action = KM_WIRE_STATUS_CALIBRATING;
        wire.code = KM_WIRE_OP_CODE_ERROR_BUSY;
        break;
    case KM_OP_STATE_LOADED:
    case KM_OP_STATE_IDLE:
        wire.action = KM_WIRE_STATUS_STOPPED;
        wire.code = KM_WIRE_OP_CODE_SUCCESS;
        break;
    case KM_OP_STATE_FAULT:
        wire.action = KM_WIRE_STATUS_ERROR;
        wire.code = KM_WIRE_OP_CODE_ERROR_UNSPECIFIED;
        break;
    }

    return wire;
}
