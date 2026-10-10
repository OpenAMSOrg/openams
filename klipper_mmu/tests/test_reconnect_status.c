// SPDX-License-Identifier: GPL-3.0-or-later
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "klipper_mmu/operation_core.h"
#include "mock_driver.h"
#include "fake_clock.h"

typedef struct {
    km_operation_core_t core;
    mock_driver_t feeder[4];
    km_fake_clock_t clock;
    uint32_t ms;
} fixture_t;

static void sample(fixture_t *f, bool hub, bool arrival)
{
    for (uint8_t lane = 0; lane < 4; ++lane)
        km_core_set_sensors_at(&f->core, lane, true, hub, arrival, true, f->ms);
    assert(km_buffer_update_switches(&f->core.buffer_sensor, false, false, f->ms) == KM_OK);
}

static void setup(fixture_t *f)
{
    memset(f, 0, sizeof(*f));
    km_device_profile_t profile;
    km_profile_init_nightowl(&profile);
    assert(km_core_init(&f->core, &profile) == KM_OK);
    for (uint8_t i = 0; i < 4; ++i) {
        mock_driver_init(&f->feeder[i], 2, 10, false);
        assert(km_core_bind_lane_driver(&f->core, i, &f->feeder[i].base, NULL) == KM_OK);
    }
    km_core_host_comm_ready(&f->core, 0);
    km_fake_clock_init(&f->clock, 0);
    sample(f, false, false);
}

static void advance(fixture_t *f, uint32_t duration, bool hub, bool arrival)
{
    for (uint32_t i = 0; i < duration; ++i) {
        ++f->ms;
        assert(km_fake_clock_run_trajectory(&f->clock, &f->core.trajectory, f->ms * 1000u) == KM_OK);
        sample(f, hub, arrival);
        km_core_update(&f->core, f->ms, f->ms * 1000u);
    }
}

static km_status_snapshot_t snapshot(fixture_t *f)
{
    km_operation_core_t before;
    memcpy(&before, &f->core, sizeof(before));
    km_status_snapshot_t s = km_core_get_snapshot(&f->core);
    assert(memcmp(&before, &f->core, sizeof(before)) == 0);
    return s;
}

static void lifecycle(void)
{
    fixture_t f; setup(&f);
    km_status_snapshot_t s = snapshot(&f);
    assert(!s.lane_active && !s.path_occupied && !s.terminal_pending);
    assert(s.motor_ready && s.host_connected && !s.position_uncertain);
    assert(!s.control_time_valid && !s.queued_events && !s.head_event_seq);
    for (unsigned i = 0; i < KM_MAX_CLIENTS; ++i)
        assert(!s.client_session[i] && !s.client_request[i]);

    const uint32_t session = UINT32_MAX - 1u, request = UINT32_MAX - 1u;
    assert(km_core_request_load(&f.core, KM_CLIENT_HOST, session, request, 2,
                               (km_core_timepoint_t){0, 0}) == KM_REQ_ACCEPTED);
    s = snapshot(&f);
    assert(s.active_op == KM_OP_TYPE_LOAD && s.active_client == KM_CLIENT_HOST);
    assert(s.active_session == session && s.active_req_id == request && s.active_lane == 2);
    assert(s.lane_active && s.path_owner_lane == 2 && s.terminal_pending);
    assert(s.client_session[KM_CLIENT_HOST] == session && s.client_request[KM_CLIENT_HOST] == request);
    assert(s.control_time_valid && !s.control_time_us && !s.head_event_seq);

    advance(&f, 10, false, false);
    advance(&f, 1, true, false);
    advance(&f, 10, true, true);
    s = snapshot(&f);
    assert(s.stage == KM_STAGE_FOLLOW && s.op_state == KM_OP_STATE_FOLLOWING);
    assert(s.lane_active && s.path_occupied && s.path_occupant_lane == 2);
    assert(s.active_op == KM_OP_TYPE_LOAD && !s.terminal_pending);
    assert(s.queued_events == 1 && s.head_event_seq != 0);
    km_event_t e;
    assert(km_event_queue_peek(&f.core.event_queue, &e) == KM_OK);
    assert(e.seq == s.head_event_seq && e.type == KM_EVENT_LOAD_COMPLETE);
    assert(e.session_id == session && e.req_id == request && e.lane_idx == 2);
    km_request_identity_t id = {KM_CLIENT_HOST, session, request, KM_OP_TYPE_LOAD, 2};
    assert(km_core_find_terminal(&f.core, &id, &e) == KM_OK);
    assert(km_event_queue_ack(&f.core.event_queue, s.head_event_seq) == KM_OK);
    s = snapshot(&f);
    assert(!s.queued_events && !s.head_event_seq && s.lane_active);
    assert(km_core_find_terminal(&f.core, &id, &e) == KM_OK); /* Acknowledgement retains history. */
    assert(km_core_request_load(&f.core, KM_CLIENT_HOST, session, request, 2,
                               (km_core_timepoint_t){f.ms, f.ms * 1000u}) == KM_REQ_ALREADY_COMPLETED);

    assert(km_core_request_follow(&f.core, KM_CLIENT_HOST, session, UINT32_MAX,
                                 true, KM_DIR_REVERSE, (km_core_timepoint_t){f.ms, f.ms * 1000u}) == KM_REQ_ACCEPTED);
    s = snapshot(&f);
    assert(s.active_op == KM_OP_TYPE_FOLLOW && s.terminal_pending);
    assert(s.active_req_id == UINT32_MAX && s.client_request[KM_CLIENT_HOST] == UINT32_MAX);
    assert(km_core_request_cancel5(&f.core, KM_CLIENT_HOST, session, UINT32_MAX,
                                  (km_core_timepoint_t){f.ms, f.ms * 1000u}) == KM_REQ_ACCEPTED);
    s = snapshot(&f);
    assert(s.stage == KM_STAGE_BRAKING && s.lane_active && s.terminal_pending);
    advance(&f, 100, true, true);
    s = snapshot(&f);
    assert(!s.lane_active && !s.terminal_pending && s.path_occupied && s.path_occupant_lane == 2);
    assert(s.active_op == KM_OP_TYPE_NONE && s.active_client == KM_CLIENT_NONE);
    assert(s.client_request[KM_CLIENT_HOST] == UINT32_MAX);
    assert(s.control_time_ms == f.ms && s.control_time_us == f.ms * 1000u);
    assert(!f.feeder[2].base.enabled);
}

static void reconnect_and_recovery(void)
{
    fixture_t f; setup(&f);
    assert(km_core_request_load(&f.core, KM_CLIENT_HOST, 1000, 9000, 1,
                               (km_core_timepoint_t){0, 0}) == KM_REQ_ACCEPTED);
    advance(&f, 25, true, true);
    km_core_host_comm_lost(&f.core, f.ms);
    km_status_snapshot_t s = snapshot(&f);
    assert(!s.host_connected && s.system_fault == KM_FAULT_HOST_COMM_LOST);
    assert(s.position_uncertain && s.path_occupied && s.path_occupant_lane == 1);
    assert(!f.feeder[1].base.enabled);
    const uint32_t events = s.queued_events, head = s.head_event_seq;
    km_core_host_comm_ready(&f.core, f.ms);
    s = snapshot(&f);
    assert(s.host_connected && s.position_uncertain && s.system_fault == KM_FAULT_HOST_COMM_LOST);
    assert(s.queued_events == events && s.head_event_seq == head);
    assert(s.client_session[KM_CLIENT_HOST] == 1000 && s.client_request[KM_CLIENT_HOST] == 9000);
    assert(!f.feeder[1].base.enabled);
    const int64_t reconciled = -999999999999LL;
    assert(km_core_recover(&f.core, reconciled, true, 1,
                          (km_core_timepoint_t){f.ms, f.ms * 1000u + 731u}) == KM_OK);
    s = snapshot(&f);
    assert(!s.position_uncertain && s.current_position_steps == reconciled);
    assert(!s.lane_active && s.path_occupied && s.path_occupant_lane == 1);
    assert(!s.active_req_id && !s.active_session && s.active_op == KM_OP_TYPE_NONE);
    assert(s.client_session[KM_CLIENT_HOST] == 1000 && s.client_request[KM_CLIENT_HOST] == 9000);
    assert(s.control_time_us == f.ms * 1000u + 731u && s.control_time_ms == f.ms);
    assert(s.queued_events == events && s.head_event_seq == head && !f.feeder[1].base.enabled);
    s.client_session[KM_CLIENT_HOST] = 1; /* Returned counters are values, not writable core aliases. */
    assert(snapshot(&f).client_session[KM_CLIENT_HOST] == 1000);
}

static void pending_fault(void)
{
    fixture_t f; setup(&f);
    km_event_t event = {0};
    event.type = KM_EVENT_STOPPED;
    for (unsigned i = 0; i < KM_EVENT_QUEUE_CAPACITY; ++i)
        assert(km_event_queue_push(&f.core.event_queue, &event) == KM_OK);
    km_core_explicit_stop(&f.core, KM_FAULT_EMERGENCY_STOP, 0);
    km_status_snapshot_t s = snapshot(&f);
    assert(s.pending_fault_event && s.queued_events == KM_EVENT_QUEUE_CAPACITY);
    assert(s.head_event_seq == 1 && s.system_fault == KM_FAULT_EMERGENCY_STOP);
    assert(km_event_queue_ack(&f.core.event_queue, s.head_event_seq) == KM_OK);
    km_core_update(&f.core, 0, 0);
    s = snapshot(&f);
    assert(!s.pending_fault_event && s.queued_events == KM_EVENT_QUEUE_CAPACITY);
    assert(s.head_event_seq == 2);
}

int main(void)
{
    lifecycle(); reconnect_and_recovery(); pending_fault();
    puts("read-only reconciliation: ownership, terminal retention, full identities, reconnect, recovery and event pressure passed");
    return 0;
}
