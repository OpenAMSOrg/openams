// SPDX-License-Identifier: GPL-3.0-or-later
#include "messages.h"
#include <assert.h>
#include <inttypes.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>

/* Capture the actual emitter arguments. Python then encodes, frames and parses
 * them using the pinned upstream Klipper codec, rather than a second codec. */
void capture_sendf(const char *format, ...);
void capture_sendf(const char *format, ...)
{
    printf("{\"format\":\"%s\",\"values\":[", format);
    va_list args;
    va_start(args, format);
    const char *p = format;
    int count = 0;
    while ((p = strchr(p, '%'))) {
        if (count++)
            putchar(',');
        if (p[1] == 'i')
            printf("%" PRId32, va_arg(args, int32_t));
        else
            printf("%" PRIu32, va_arg(args, uint32_t));
        ++p;
    }
    va_end(args);
    puts("]}");
}

int main(void)
{
    const uint64_t instance = UINT64_MAX;
    km_request_identity_t id = {KM_CLIENT_HOST, UINT32_MAX, 0xfffffffeu,
                               KM_OP_TYPE_LOAD, 7};
    assert(km_send_admission(instance, &id, KM_REQ_ACCEPTED, KM_FAULT_NONE) == KM_OK);
    assert(km_send_admission(instance, &id, KM_REQ_ALREADY_COMPLETED, KM_FAULT_NONE) == KM_OK);
    id.lane_idx = 255;
    assert(km_send_admission(instance, &id, KM_REQ_INVALID_LANE, KM_FAULT_NONE) == KM_OK);
    km_event_t event = {.seq = UINT32_MAX, .timestamp_ms = 0x80000000u,
        .client_id = KM_CLIENT_HOST, .session_id = UINT32_MAX,
        .req_id = 0xfffffffeu, .lane_idx = 7, .op_type = KM_OP_TYPE_LOAD,
        .type = KM_EVENT_LOAD_COMPLETE, .result_code = 0, .value = UINT32_MAX,
        .flags = KM_EVENT_FLAG_OVERFLOW};
    assert(km_send_event(instance, &event) == KM_OK);
    event.client_id = KM_CLIENT_NONE; event.op_type = KM_OP_TYPE_NONE;
    event.session_id = 0; event.req_id = 0; event.type = KM_EVENT_FAULT_ENTERED;
    assert(km_send_event(instance, &event) == KM_OK);
    km_status_snapshot_t s = {
        .op_state = KM_OP_STATE_FOLLOWING, .stage = KM_STAGE_FOLLOW,
        .active_op = KM_OP_TYPE_LOAD, .active_client = KM_CLIENT_HOST,
        .active_session = UINT32_MAX, .active_req_id = 0xfffffffeu, .active_lane = 7,
        .lane_active = true, .terminal_pending = false, .path_owner_lane = 7,
        .path_occupied = true, .path_occupant_lane = 6,
        .current_velocity_steps_s = INT32_MIN, .current_position_steps = INT64_MIN,
        .position_uncertain = true, .buffer_position_permille = INT32_MAX,
        .buffer_valid = false, .commanded_current_ma = UINT16_MAX,
        .measured_current_ma = 1234, .measured_current_sample_ms = 0x12345678,
        .measured_current_available = false, .system_fault = KM_FAULT_SENSOR_FAILURE,
        .motion_fault = KM_FAULT_STALLED, .follower_state = KM_FOLLOWER_IDLE,
        .follower_direction = KM_DIR_FORWARD, .follower_fault = KM_FAULT_NONE,
        .host_connected = false, .motor_ready = true,
        .control_time_valid = true, .control_time_ms = 0x89abcdef,
        .control_time_us = 0xfedcba98,
        .client_session = {0, UINT32_MAX, 0x80000000u, 0x100},
        .client_request = {0, 0xfffffffeu, 0x7fffffffu, 0x101},
        .queued_events = 16, .head_event_seq = UINT32_MAX, .pending_fault_event = true,
    };
    const int64_t positions[] = {INT64_MIN, INT64_MAX, -1, 0, 1,
                                -4294967296LL, 4294967296LL, -1000000000000LL};
    for (uint32_t i = 0; i < sizeof(positions) / sizeof(positions[0]); ++i) {
        s.current_position_steps = positions[i];
        s.current_velocity_steps_s = i % 2 ? INT32_MAX : INT32_MIN;
        s.buffer_position_permille = i % 2 ? INT32_MIN : INT32_MAX;
        for (enum km_status_part part = KM_STATUS_IDENTITY; part <= KM_STATUS_REQUESTS; ++part)
            assert(km_send_snapshot_part(instance, UINT32_MAX - i, &s, 0x10203040, 0x50607080, part) == KM_OK);
    }
    /* Distinct literal patterns catch swapped flag bits and false fabrication. */
    s.lane_active = false; s.terminal_pending = true; s.path_occupied = false;
    s.position_uncertain = false; s.buffer_valid = true; s.measured_current_available = true;
    s.host_connected = true; s.motor_ready = false; s.control_time_valid = false;
    s.pending_fault_event = false;
    s.follower_state = KM_FOLLOWER_DISARMED;
    s.follower_direction = KM_DIR_REVERSE;
    for (enum km_status_part part = KM_STATUS_IDENTITY; part <= KM_STATUS_REQUESTS; ++part)
        assert(km_send_snapshot_part(instance, 123, &s, 0, 0, part) == KM_OK);
    for (uint32_t bit = 0; bit < 10; ++bit) {
        s.lane_active = bit == 0; s.terminal_pending = bit == 1;
        s.path_occupied = bit == 2; s.position_uncertain = bit == 3;
        s.buffer_valid = bit == 4; s.measured_current_available = bit == 5;
        s.host_connected = bit == 6; s.motor_ready = bit == 7;
        s.control_time_valid = bit == 8; s.pending_fault_event = bit == 9;
        for (enum km_status_part part = KM_STATUS_IDENTITY; part <= KM_STATUS_REQUESTS; ++part)
            assert(km_send_snapshot_part(instance, 200 + bit, &s, 0, 0, part) == KM_OK);
    }
    s.follower_state = KM_FOLLOWER_FAULT;
    s.follower_fault = KM_FAULT_SENSOR_FAILURE;
    for (enum km_status_part part = KM_STATUS_IDENTITY; part <= KM_STATUS_REQUESTS; ++part)
        assert(km_send_snapshot_part(instance, 124, &s, 0, 0, part) == KM_OK);
    assert(km_send_admission(0, &id, KM_REQ_ACCEPTED, KM_FAULT_NONE) == KM_ERR_INVALID_PARAM);
    assert(km_send_admission(instance, NULL, KM_REQ_ACCEPTED, KM_FAULT_NONE) == KM_ERR_INVALID_PARAM);
    assert(km_send_event(0, &event) == KM_ERR_INVALID_PARAM);
    assert(km_send_event(instance, NULL) == KM_ERR_INVALID_PARAM);
    event.seq = 0;
    assert(km_send_event(instance, &event) == KM_ERR_INVALID_PARAM);
    assert(km_send_snapshot_part(0, 1, &s, 0, 0, KM_STATUS_IDENTITY) == KM_ERR_INVALID_PARAM);
    assert(km_send_snapshot_part(instance, 0, &s, 0, 0, KM_STATUS_IDENTITY) == KM_ERR_INVALID_PARAM);
    assert(km_send_snapshot_part(instance, 1, NULL, 0, 0, KM_STATUS_IDENTITY) == KM_ERR_INVALID_PARAM);
    /* KM_STATUS_SELECTOR = 5 is now a valid part (selector.h); 6 is invalid. */
    assert(km_send_snapshot_part(instance, 1, &s, 0, 0, (enum km_status_part)6) == KM_ERR_INVALID_PARAM);
    assert(km_send_snapshot_part(instance, 1, &s, 0, 0, (enum km_status_part)-1) == KM_ERR_INVALID_PARAM);
    /* 12-lane snapshot: lanes 8-11 BAY/LOAD configured/present/valid/fresh and
     * sample times, plus HUB/toolhead unaffected by the wider masks. */
    km_sensor_snapshot_t wide = {.lane_count = 12, .observed_ms = 0x22334455u};
    wide.bays[0] = (km_sensor_sample_t){true, true, true, true, 1};
    wide.bays[8] = (km_sensor_sample_t){true, true, true, false, 0x11111111u};
    wide.bays[9] = (km_sensor_sample_t){true, false, false, false, 0};
    wide.bays[10] = (km_sensor_sample_t){true, true, false, false, 0x22222222u};
    wide.bays[11] = (km_sensor_sample_t){false, false, false, false, 0};
    wide.hub = (km_sensor_sample_t){true, true, true, true, 99};
    wide.toolhead = (km_sensor_sample_t){true, false, false, false, 0};
    wide.loads[8] = (km_sensor_sample_t){true, true, true, true, 0x33333333u};
    wide.loads[9] = (km_sensor_sample_t){true, false, true, false, 0};
    wide.loads[10] = (km_sensor_sample_t){false, false, false, false, 0};
    wide.loads[11] = (km_sensor_sample_t){true, true, false, false, 0x44444444u};
    for (enum km_sensor_part part = KM_SENSOR_FLAGS; part <= KM_SENSOR_LOADS2; ++part)
        assert(km_send_sensor_part(instance, 55, &wide, part) == KM_OK);

    km_sensor_snapshot_t sensors = {.lane_count = 8, .observed_ms = 0x10203040u};
    sensors.bays[0] = (km_sensor_sample_t){true, true, true, true, UINT32_MAX};
    sensors.bays[1] = (km_sensor_sample_t){true, false, false, false, 0};
    sensors.bays[7] = (km_sensor_sample_t){true, true, true, false, 0x80000000u};
    sensors.hub = (km_sensor_sample_t){true, true, true, true, 23};
    sensors.toolhead = (km_sensor_sample_t){false, false, false, false, 0};
    sensors.loads[0] = (km_sensor_sample_t){true, true, true, true, UINT32_MAX};
    sensors.loads[2] = (km_sensor_sample_t){true, false, true, false, 0x80000000u};
    sensors.loads[7] = (km_sensor_sample_t){true, true, false, false, 0x12345678u};
    assert(km_send_sensor_part(instance, UINT32_MAX, &sensors, KM_SENSOR_LOADS0) == KM_OK);
    assert(km_send_sensor_part(instance, UINT32_MAX, &sensors, KM_SENSOR_LOADS1) == KM_OK);
    for (enum km_sensor_part part = KM_SENSOR_FLAGS; part <= KM_SENSOR_BAYS1; ++part)
        assert(km_send_sensor_part(instance, UINT32_MAX, &sensors, part) == KM_OK);
    assert(km_send_sensor_part(0, UINT32_MAX, &sensors, KM_SENSOR_FLAGS) == KM_ERR_INVALID_PARAM);
    assert(km_send_sensor_part(instance, 0, &sensors, KM_SENSOR_FLAGS) == KM_ERR_INVALID_PARAM);
    assert(km_send_sensor_part(instance, UINT32_MAX, NULL, KM_SENSOR_FLAGS) == KM_ERR_INVALID_PARAM);
    assert(km_send_sensor_part(instance, UINT32_MAX, &sensors, (enum km_sensor_part)7) == KM_ERR_INVALID_PARAM);
    assert(km_send_sensor_part(instance, UINT32_MAX, &sensors, (enum km_sensor_part)-1) == KM_ERR_INVALID_PARAM);
    return 0;
}
