// SPDX-License-Identifier: GPL-3.0-or-later
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "klipper_mmu/operation_core.h"
#include "mock_driver.h"
#include "fake_clock.h"

typedef struct {
    km_operation_core_t core;
    mock_driver_t feeders[4];
    km_fake_clock_t clock;
    uint32_t ms;
} fixture_t;

static km_core_timepoint_t now(const fixture_t *f)
{
    return (km_core_timepoint_t){f->ms, f->ms * 1000u};
}

static void samples(fixture_t *f, bool occupied)
{
    for (uint8_t i = 0; i < 4; ++i)
        km_core_set_sensors_at(&f->core, i, true, occupied, occupied, true, f->ms);
    km_core_set_encoder_at(&f->core, 0, true, f->ms);
    assert(km_buffer_update_switches(&f->core.buffer_sensor, false, false, f->ms) == KM_OK);
}

static void setup(fixture_t *f)
{
    memset(f, 0, sizeof(*f));
    km_device_profile_t profile;
    km_profile_init_nightowl(&profile);
    profile.has_encoder = true;
    profile.encoder_detection_length_steps = 1000; /* km_profile_validate requires > 0. */
    assert(km_core_init(&f->core, &profile) == KM_OK);
    for (uint8_t i = 0; i < 4; ++i) {
        mock_driver_init(&f->feeders[i], 2, 10, false);
        assert(km_core_bind_lane_driver(&f->core, i, &f->feeders[i].base, NULL) == KM_OK);
    }
    km_core_host_comm_ready(&f->core, 0);
    km_fake_clock_init(&f->clock, 0);
    samples(f, false);
}

static void advance(fixture_t *f, uint32_t ms, bool occupied)
{
    for (uint32_t i = 0; i < ms; ++i) {
        ++f->ms;
        assert(km_fake_clock_run_trajectory(&f->clock, &f->core.trajectory, f->ms * 1000u) == KM_OK);
        samples(f, occupied);
        km_core_update(&f->core, f->ms, f->ms * 1000u);
    }
}

static km_admission_result_t start(fixture_t *f, km_request_identity_t request)
{
    return km_core_request_start(&f->core, &request, KM_DIR_FORWARD, now(f));
}

static void rejected_unchanged(fixture_t *f, km_request_identity_t request,
                                bool cancel, km_admission_result_t expected)
{
    fixture_t before;
    memcpy(&before, f, sizeof(before));
    km_admission_result_t result = cancel
        ? km_core_request_cancel_target(&f->core, &request, now(f)) : start(f, request);
    assert(result == expected);
    /* Includes reservations/counters, trajectory and every driver output. */
    assert(memcmp(&before, f, sizeof(before)) == 0);
}

static void full_cancel_identity(void)
{
    fixture_t f; setup(&f);
    km_request_identity_t id = {KM_CLIENT_HOST, UINT32_MAX - 1u, UINT32_MAX, KM_OP_TYPE_LOAD, 2};
    assert(start(&f, id) == KM_REQ_ACCEPTED);
    advance(&f, 10, false);
    km_request_identity_t wrong = id;
    wrong.op_type = KM_OP_TYPE_UNLOAD;
    rejected_unchanged(&f, wrong, true, KM_REQ_REJECTED_STALE);
    wrong = id; wrong.lane_idx = 3;
    rejected_unchanged(&f, wrong, true, KM_REQ_REJECTED_STALE);
    wrong = id; --wrong.session_id;
    rejected_unchanged(&f, wrong, true, KM_REQ_REJECTED_STALE);
    wrong = id; wrong.req_id = 255;
    rejected_unchanged(&f, wrong, true, KM_REQ_REJECTED_STALE);
    wrong = id; wrong.client = KM_CLIENT_LOCAL_UI;
    rejected_unchanged(&f, wrong, true, KM_REQ_REJECTED_CONFLICT);
    assert(km_core_request_cancel_target(&f.core, &id, now(&f)) == KM_REQ_ACCEPTED);
    assert(f.core.stage == KM_STAGE_BRAKING && f.core.shared_path_owned);
    assert(km_core_request_cancel_target(&f.core, &id, now(&f)) == KM_REQ_ALREADY_IN_PROGRESS);
    advance(&f, 40, false);
    km_event_t event;
    assert(km_core_find_terminal(&f.core, &id, &event) == KM_OK);
    assert(event.type == KM_EVENT_LOAD_CANCELLED && event.req_id == UINT32_MAX);
    rejected_unchanged(&f, id, true, KM_REQ_REJECTED_STALE);
}

static void explicit_lane_and_history(void)
{
    fixture_t f; setup(&f);
    km_request_identity_t load = {KM_CLIENT_HOST, 1000, 9000, KM_OP_TYPE_LOAD, 2};
    assert(start(&f, load) == KM_REQ_ACCEPTED);
    advance(&f, 20, true);
    assert(f.core.stage == KM_STAGE_FOLLOW && !f.core.terminal_pending);
    km_request_identity_t follow = {KM_CLIENT_HOST, 1000, 9001, KM_OP_TYPE_FOLLOW, 3};
    rejected_unchanged(&f, follow, false, KM_REQ_PATH_OCCUPIED);
    km_request_identity_t unload = {KM_CLIENT_HOST, 1000, 9002, KM_OP_TYPE_UNLOAD, 3};
    rejected_unchanged(&f, unload, false, KM_REQ_PATH_OCCUPIED);
    follow.lane_idx = 2;
    assert(km_core_request_start(&f.core, &follow, KM_DIR_REVERSE, now(&f)) == KM_REQ_ACCEPTED);
    assert(km_core_request_start(&f.core, &follow, KM_DIR_REVERSE, now(&f)) == KM_REQ_ALREADY_IN_PROGRESS);
    rejected_unchanged(&f, follow, false, KM_REQ_REJECTED_CONFLICT); /* Changed direction. */
    unload.lane_idx = 2;
    assert(start(&f, unload) == KM_REQ_ACCEPTED);
    advance(&f, 80, false);
    assert(!f.core.shared_path_owned && !f.core.path_occupied);
    km_event_t event;
    assert(km_core_find_terminal(&f.core, &unload, &event) == KM_OK);
    assert(event.type == KM_EVENT_UNLOAD_COMPLETE && event.lane_idx == 2);
    km_request_identity_t next = {KM_CLIENT_HOST, 1000, 9003, KM_OP_TYPE_LOAD, 1};
    assert(start(&f, next) == KM_REQ_ACCEPTED);
    assert(f.core.active_lane == 1);
    /* Replay the OLD bay's terminal while another bay owns the path. No inferred lane. */
    rejected_unchanged(&f, unload, false, KM_REQ_ALREADY_COMPLETED);
    rejected_unchanged(&f, follow, false, KM_REQ_ALREADY_COMPLETED);
    unload.lane_idx = 1;
    rejected_unchanged(&f, unload, false, KM_REQ_REJECTED_STALE);
}

static void automatic_follow_uses_original_load_target(void)
{
    fixture_t f; setup(&f);
    km_request_identity_t load = {KM_CLIENT_HOST, 1000, 9000, KM_OP_TYPE_LOAD, 3};
    assert(start(&f, load) == KM_REQ_ACCEPTED);
    advance(&f, 20, true);
    assert(!f.core.terminal_pending && f.core.shared_path_owned);
    km_request_identity_t wrong = load; wrong.op_type = KM_OP_TYPE_FOLLOW;
    rejected_unchanged(&f, wrong, true, KM_REQ_REJECTED_STALE);
    assert(km_core_request_cancel_target(&f.core, &load, now(&f)) == KM_REQ_ACCEPTED);
    advance(&f, 50, true);
    assert(!f.core.shared_path_owned && f.core.path_occupied);
    assert(!f.feeders[3].base.enabled);
    km_event_t event;
    assert(km_event_queue_peek(&f.core.event_queue, &event) == KM_OK);
    assert(event.type == KM_EVENT_LOAD_COMPLETE);
    assert(km_event_queue_ack(&f.core.event_queue, event.seq) == KM_OK);
    assert(km_event_queue_peek(&f.core.event_queue, &event) == KM_OK);
    assert(event.type == KM_EVENT_STOPPED && event.op_type == KM_OP_TYPE_LOAD);
}

static void nightowl_preload(void)
{
    fixture_t f; setup(&f);
    km_request_identity_t preload = {KM_CLIENT_HOST, 1000, 9000, KM_OP_TYPE_PRELOAD, 3};
    assert(start(&f, preload) == KM_REQ_ACCEPTED);
    for (unsigned i = 0; i < 10; ++i) {
        ++f.core.encoder_clicks;
        advance(&f, 1, false);
    }
    f.core.filament_present_hub = true;
    for (unsigned i = 0; i < 5; ++i) {
        ++f.ms;
        assert(km_fake_clock_run_trajectory(&f.clock, &f.core.trajectory, f.ms * 1000u) == KM_OK);
        for (uint8_t lane = 0; lane < 4; ++lane)
            km_core_set_sensors_at(&f.core, lane, true, true, false, true, f.ms);
        km_core_set_encoder_at(&f.core, f.core.encoder_clicks, true, f.ms);
        assert(km_buffer_update_switches(&f.core.buffer_sensor, false, false, f.ms) == KM_OK);
        km_core_update(&f.core, f.ms, f.ms * 1000u);
    }
    /* No per-lane LOAD switch, a real HUB: PRELOAD retracts until the HUB
     * clears rather than settling the instant it trips. */
    assert(f.core.stage == KM_STAGE_UNLOAD_RETRACT_TO_HUB);
    for (unsigned i = 0; i < 40; ++i) {
        ++f.ms;
        assert(km_fake_clock_run_trajectory(&f.clock, &f.core.trajectory, f.ms * 1000u) == KM_OK);
        for (uint8_t lane = 0; lane < 4; ++lane)
            km_core_set_sensors_at(&f.core, lane, true, false, false, true, f.ms);
        km_core_set_encoder_at(&f.core, f.core.encoder_clicks, true, f.ms);
        assert(km_buffer_update_switches(&f.core.buffer_sensor, false, false, f.ms) == KM_OK);
        km_core_update(&f.core, f.ms, f.ms * 1000u);
    }
    km_event_t event;
    assert(km_core_find_terminal(&f.core, &preload, &event) == KM_OK);
    assert(event.type == KM_EVENT_PRELOAD_COMPLETE && f.core.path_occupant_lane == 3);
}

static void calibration_and_invalid_requests(void)
{
    fixture_t f; setup(&f);
    km_request_identity_t id = {KM_CLIENT_HOST, 1000, 9000, KM_OP_TYPE_CALIBRATE, 3};
    km_request_identity_t wrong = id; wrong.op_type = KM_OP_TYPE_CANCEL;
    rejected_unchanged(&f, wrong, false, KM_REQ_UNAVAILABLE);
    wrong = id; wrong.lane_idx = 7;
    rejected_unchanged(&f, wrong, false, KM_REQ_INVALID_LANE);
    wrong = id; wrong.session_id = 0;
    rejected_unchanged(&f, wrong, false, KM_REQ_REJECTED_STALE);
    assert(km_core_request_start(NULL, &id, KM_DIR_FORWARD, now(&f)) == KM_REQ_REJECTED_STALE);
    assert(km_core_request_start(&f.core, NULL, KM_DIR_FORWARD, now(&f)) == KM_REQ_UNAVAILABLE);
    assert(km_core_request_cancel_target(NULL, &id, now(&f)) == KM_REQ_REJECTED_STALE);
    assert(km_core_request_cancel_target(&f.core, NULL, now(&f)) == KM_REQ_REJECTED_STALE);
    assert(start(&f, id) == KM_REQ_ACCEPTED);
    assert(f.core.active_lane == 3 && f.core.stage == KM_STAGE_CALIBRATE_PROBE);
    assert(km_core_request_start(&f.core, &id, KM_DIR_FORWARD,
                                (km_core_timepoint_t){0, 1000}) == KM_REQ_REJECTED_STALE);
    assert(km_core_request_cancel_target(&f.core, &id,
                                        (km_core_timepoint_t){0, 1000}) == KM_REQ_REJECTED_STALE);
    assert(km_core_request_cancel_target(&f.core, &id, now(&f)) == KM_REQ_ACCEPTED);
    advance(&f, 50, false);
    km_event_t event;
    assert(km_core_find_terminal(&f.core, &id, &event) == KM_OK);
    assert(event.type == KM_EVENT_PTFE_CAL_CANCELLED);
}

int main(void)
{
    nightowl_preload();
    full_cancel_identity();
    explicit_lane_and_history();
    automatic_follow_uses_original_load_target();
    calibration_and_invalid_requests();
    puts("Full-identity dispatch: explicit lanes, history, exact cancellation and shared lifecycle passed");
    return 0;
}
