// SPDX-License-Identifier: GPL-3.0-or-later
#include "messages.h"
#include "command.h"

km_result_t
km_send_admission(uint64_t instance, const km_request_identity_t *id,
                  km_admission_result_t result, km_fault_t fault)
{
    if (!instance || !id)
        return KM_ERR_INVALID_PARAM;
    /* Rejected identities may contain zero IDs or an invalid lane. Echo them
     * unchanged so the host can match the rejection to the attempted request. */
    sendf("km_admission instance_hi=%u instance_lo=%u client=%c session=%u request=%u operation=%c lane=%c admission=%c fault=%c",
          (uint32_t)(instance >> 32), (uint32_t)instance, (uint32_t)id->client,
          id->session_id, id->req_id, (uint32_t)id->op_type, (uint32_t)id->lane_idx,
          (uint32_t)result, (uint32_t)fault);
    return KM_OK;
}

km_result_t
km_send_event(uint64_t instance, const km_event_t *event)
{
    if (!instance || !event || !event->seq)
        return KM_ERR_INVALID_PARAM;
    sendf("km_event instance_hi=%u instance_lo=%u seq=%u timestamp_ms=%u client=%c session=%u request=%u operation=%c lane=%c event_type=%c result_code=%c value=%u flags=%c",
          (uint32_t)(instance >> 32), (uint32_t)instance, event->seq,
          event->timestamp_ms, (uint32_t)event->client_id, event->session_id,
          event->req_id, (uint32_t)event->op_type, (uint32_t)event->lane_idx,
          (uint32_t)event->type, (uint32_t)event->result_code, event->value,
          (uint32_t)event->flags);
    return KM_OK;
}

km_result_t
km_send_snapshot_part(uint64_t instance, uint32_t query,
                      const km_status_snapshot_t *s,
                      uint32_t acquired_ms, uint32_t acquired_us,
                      enum km_status_part part)
{
    if (!instance || !query || !s || part > KM_STATUS_SELECTOR)
        return KM_ERR_INVALID_PARAM;
    _Static_assert(KM_MAX_CLIENTS == 4, "Update wire snapshot client fields");
    uint32_t hi = (uint32_t)(instance >> 32), lo = (uint32_t)instance;
    switch (part) {
    case KM_STATUS_IDENTITY: {
        uint32_t flags = (uint32_t)s->lane_active
            | ((uint32_t)s->terminal_pending << 1)
            | ((uint32_t)s->path_occupied << 2)
            | ((uint32_t)s->position_uncertain << 3)
            | ((uint32_t)s->buffer_valid << 4)
            | ((uint32_t)s->measured_current_available << 5)
            | ((uint32_t)s->host_connected << 6)
            | ((uint32_t)s->motor_ready << 7)
            | ((uint32_t)s->control_time_valid << 8)
            | ((uint32_t)s->pending_fault_event << 9);
        sendf("km_status_identity instance_hi=%u instance_lo=%u query=%u state=%c stage=%c operation=%c client=%c session=%u request=%u lane=%c owner_lane=%c occupant_lane=%c flags=%hu queued_events=%c head_event_seq=%u",
              hi, lo, query, (uint32_t)s->op_state, (uint32_t)s->stage,
              (uint32_t)s->active_op, (uint32_t)s->active_client, s->active_session,
              s->active_req_id, (uint32_t)s->active_lane, (uint32_t)s->path_owner_lane,
              (uint32_t)s->path_occupant_lane, flags, (uint32_t)s->queued_events,
              s->head_event_seq);
        break;
    }
    case KM_STATUS_MOTION: {
        uint64_t position = (uint64_t)s->current_position_steps;
        sendf("km_status_motion instance_hi=%u instance_lo=%u query=%u velocity_bits=%u position_hi=%u position_lo=%u buffer_position_bits=%u system_fault=%c motion_fault=%c follower_state=%c follower_direction=%c follower_fault=%c",
              hi, lo, query, (uint32_t)s->current_velocity_steps_s, (uint32_t)(position >> 32),
              (uint32_t)position, (uint32_t)s->buffer_position_permille,
              (uint32_t)s->system_fault, (uint32_t)s->motion_fault,
              (uint32_t)s->follower_state, (uint32_t)s->follower_direction,
              (uint32_t)s->follower_fault);
        break;
    }
    case KM_STATUS_TIME:
        sendf("km_status_time instance_hi=%u instance_lo=%u query=%u commanded_current_ma=%hu measured_current_ma=%hu measured_current_sample_ms=%u control_ms=%u control_us=%u acquired_ms=%u acquired_us=%u",
              hi, lo, query, (uint32_t)s->commanded_current_ma,
              (uint32_t)s->measured_current_ma, s->measured_current_sample_ms,
              s->control_time_ms, s->control_time_us, acquired_ms, acquired_us);
        break;
    case KM_STATUS_SESSIONS:
        sendf("km_status_sessions instance_hi=%u instance_lo=%u query=%u client0=%u client1=%u client2=%u client3=%u",
              hi, lo, query, s->client_session[0], s->client_session[1],
              s->client_session[2], s->client_session[3]);
        break;
    case KM_STATUS_REQUESTS:
        sendf("km_status_requests instance_hi=%u instance_lo=%u query=%u client0=%u client1=%u client2=%u client3=%u",
              hi, lo, query, s->client_request[0], s->client_request[1],
              s->client_request[2], s->client_request[3]);
        break;
    case KM_STATUS_SELECTOR:
        sendf("km_status_selector instance_hi=%u instance_lo=%u query=%u homed=%c gate=%c",
              hi, lo, query, (uint32_t)s->selector_homed, (uint32_t)s->selector_gate);
        break;
    }
    return KM_OK;
}

km_result_t
km_send_sensor_part(uint64_t instance, uint32_t query,
                    const km_sensor_snapshot_t *s, enum km_sensor_part part)
{
    if (!instance || !query || !s || part > KM_SENSOR_LOADS2
        || !s->lane_count || s->lane_count > KM_MAX_LANES)
        return KM_ERR_INVALID_PARAM;
    /* Lanes 0-7 keep bits 0-7 of every %hu mask, HUB bit 8 and toolhead bit 9,
     * exactly as before. Lanes 8-11 (KM_SENSOR_WIRE_LANES) add bits 10-13 of
     * those same masks and two new parts sent only for lane_count > 8; an
     * 8-lane image leaves bits 10-13 clear and never sends BAYS2/LOADS2, so
     * its wire bytes are unchanged. */
    _Static_assert(KM_SENSOR_WIRE_LANES == 12, "Update sensor wire lane fields");
    uint32_t hi = (uint32_t)(instance >> 32), lo = (uint32_t)instance;
    if (part == KM_SENSOR_FLAGS) {
        uint32_t configured = 0, present = 0, valid = 0, fresh = 0;
        for (uint32_t i = 0; i < KM_SENSOR_WIRE_LANES + 2u; ++i) {
            /* i is the destination bit: 0-7 bay lanes 0-7, 8 HUB, 9 toolhead,
             * 10-13 bay lanes 8-11 (bays[i - 2] once i is past HUB/toolhead). */
            const km_sensor_sample_t *sample = i < 8u ? &s->bays[i]
                : i == 8u ? &s->hub : i == 9u ? &s->toolhead : &s->bays[i - 2u];
            configured |= (uint32_t)sample->configured << i;
            present |= (uint32_t)sample->present << i;
            valid |= (uint32_t)sample->valid << i;
            fresh |= (uint32_t)sample->fresh << i;
        }
        sendf("km_sensor_flags instance_hi=%u instance_lo=%u query=%u lane_count=%c observed_ms=%u configured=%hu present=%hu valid=%hu fresh=%hu",
              hi, lo, query, (uint32_t)s->lane_count, s->observed_ms,
              configured, present, valid, fresh);
    } else if (part == KM_SENSOR_BAYS0) {
        sendf("km_sensor_bays0 instance_hi=%u instance_lo=%u query=%u sample0=%u sample1=%u sample2=%u sample3=%u",
              hi, lo, query, s->bays[0].sample_ms, s->bays[1].sample_ms,
              s->bays[2].sample_ms, s->bays[3].sample_ms);
    } else if (part == KM_SENSOR_BAYS1) {
        sendf("km_sensor_bays1 instance_hi=%u instance_lo=%u query=%u sample4=%u sample5=%u sample6=%u sample7=%u hub_sample_ms=%u toolhead_sample_ms=%u",
              hi, lo, query, s->bays[4].sample_ms, s->bays[5].sample_ms,
              s->bays[6].sample_ms, s->bays[7].sample_ms,
              s->hub.sample_ms, s->toolhead.sample_ms);
    } else if (part == KM_SENSOR_BAYS2) {
        sendf("km_sensor_bays2 instance_hi=%u instance_lo=%u query=%u sample8=%u sample9=%u sample10=%u sample11=%u",
              hi, lo, query, s->bays[8].sample_ms, s->bays[9].sample_ms,
              s->bays[10].sample_ms, s->bays[11].sample_ms);
    } else if (part == KM_SENSOR_LOADS0) {
        uint32_t configured = 0, present = 0, valid = 0, fresh = 0;
        for (uint32_t i = 0; i < 8u; ++i) {
            const km_sensor_sample_t *sample = &s->loads[i];
            configured |= (uint32_t)sample->configured << i;
            present |= (uint32_t)sample->present << i;
            valid |= (uint32_t)sample->valid << i;
            fresh |= (uint32_t)sample->fresh << i;
        }
        sendf("km_sensor_loads0 instance_hi=%u instance_lo=%u query=%u configured=%c present=%c valid=%c fresh=%c sample0=%u sample1=%u sample2=%u sample3=%u",
              hi, lo, query, configured, present, valid, fresh,
              s->loads[0].sample_ms, s->loads[1].sample_ms,
              s->loads[2].sample_ms, s->loads[3].sample_ms);
    } else if (part == KM_SENSOR_LOADS1) {
        sendf("km_sensor_loads1 instance_hi=%u instance_lo=%u query=%u sample4=%u sample5=%u sample6=%u sample7=%u",
              hi, lo, query, s->loads[4].sample_ms, s->loads[5].sample_ms,
              s->loads[6].sample_ms, s->loads[7].sample_ms);
    } else {
        uint32_t configured = 0, present = 0, valid = 0, fresh = 0;
        for (uint32_t i = 0; i < 4u; ++i) {
            const km_sensor_sample_t *sample = &s->loads[8u + i];
            configured |= (uint32_t)sample->configured << i;
            present |= (uint32_t)sample->present << i;
            valid |= (uint32_t)sample->valid << i;
            fresh |= (uint32_t)sample->fresh << i;
        }
        sendf("km_sensor_loads2 instance_hi=%u instance_lo=%u query=%u configured=%c present=%c valid=%c fresh=%c sample8=%u sample9=%u sample10=%u sample11=%u",
              hi, lo, query, configured, present, valid, fresh,
              s->loads[8].sample_ms, s->loads[9].sample_ms,
              s->loads[10].sample_ms, s->loads[11].sample_ms);
    }
    return KM_OK;
}

km_result_t
km_send_diag_part(uint64_t instance, uint32_t query,
                  const km_diag_snapshot_t *s, enum km_diag_part part)
{
    if (!instance || !query || !s || part > KM_DIAG_TMC1)
        return KM_ERR_INVALID_PARAM;
    /* This wire message's BAY/LOAD fields are unrolled for exactly 8 lanes.
     * A selector or other >8-lane profile still gets bounce/idle counters
     * for its first 8 lanes; the fixed wire format is unchanged rather than
     * grown here for the lanes beyond it. */
    _Static_assert(KM_MAX_LANES >= 8, "Update diag wire lane fields");
    uint32_t hi = (uint32_t)(instance >> 32), lo = (uint32_t)instance;
    switch (part) {
    case KM_DIAG_BAY_BOUNCE:
        sendf("km_diag_bay_bounce instance_hi=%u instance_lo=%u query=%u count0=%hu count1=%hu count2=%hu count3=%hu count4=%hu count5=%hu count6=%hu count7=%hu",
              hi, lo, query, s->bay_bounce[0], s->bay_bounce[1], s->bay_bounce[2],
              s->bay_bounce[3], s->bay_bounce[4], s->bay_bounce[5],
              s->bay_bounce[6], s->bay_bounce[7]);
        break;
    case KM_DIAG_BAY_IDLE:
        sendf("km_diag_bay_idle instance_hi=%u instance_lo=%u query=%u edges0=%hu edges1=%hu edges2=%hu edges3=%hu edges4=%hu edges5=%hu edges6=%hu edges7=%hu hub_edges=%hu hub_bounce=%hu toolhead_bounce=%hu selector_bounce=%hu",
              hi, lo, query, s->bay_idle_edges[0], s->bay_idle_edges[1],
              s->bay_idle_edges[2], s->bay_idle_edges[3], s->bay_idle_edges[4],
              s->bay_idle_edges[5], s->bay_idle_edges[6], s->bay_idle_edges[7],
              s->hub_idle_edges, s->hub_bounce, s->toolhead_bounce, s->selector_bounce);
        break;
    case KM_DIAG_LOAD_BOUNCE:
        sendf("km_diag_load_bounce instance_hi=%u instance_lo=%u query=%u count0=%hu count1=%hu count2=%hu count3=%hu count4=%hu count5=%hu count6=%hu count7=%hu",
              hi, lo, query, s->load_bounce[0], s->load_bounce[1], s->load_bounce[2],
              s->load_bounce[3], s->load_bounce[4], s->load_bounce[5],
              s->load_bounce[6], s->load_bounce[7]);
        break;
    case KM_DIAG_LOAD_IDLE:
        sendf("km_diag_load_idle instance_hi=%u instance_lo=%u query=%u edges0=%hu edges1=%hu edges2=%hu edges3=%hu edges4=%hu edges5=%hu edges6=%hu edges7=%hu",
              hi, lo, query, s->load_idle_edges[0], s->load_idle_edges[1],
              s->load_idle_edges[2], s->load_idle_edges[3], s->load_idle_edges[4],
              s->load_idle_edges[5], s->load_idle_edges[6], s->load_idle_edges[7]);
        break;
    case KM_DIAG_TMC0:
        sendf("km_diag_tmc0 instance_hi=%u instance_lo=%u query=%u gstat0=%c gstat1=%c gstat2=%c gstat3=%c drv_status0=%u drv_status1=%u drv_status2=%u drv_status3=%u",
              hi, lo, query, s->gstat[0], s->gstat[1], s->gstat[2], s->gstat[3],
              s->drv_status[0], s->drv_status[1], s->drv_status[2], s->drv_status[3]);
        break;
    default:
        sendf("km_diag_tmc1 instance_hi=%u instance_lo=%u query=%u gstat4=%c gstat5=%c gstat6=%c gstat7=%c drv_status4=%u drv_status5=%u drv_status6=%u drv_status7=%u",
              hi, lo, query, s->gstat[4], s->gstat[5], s->gstat[6], s->gstat[7],
              s->drv_status[4], s->drv_status[5], s->drv_status[6], s->drv_status[7]);
        break;
    }
    return KM_OK;
}
