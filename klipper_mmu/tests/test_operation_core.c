// SPDX-License-Identifier: GPL-3.0-or-later
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "klipper_mmu/operation_core.h"
#include "mock_driver.h"
#include "fake_clock.h"

typedef struct {
    km_operation_core_t core;
    mock_driver_t feeder[KM_MAX_LANES], rewinder[KM_MAX_LANES];
    km_fake_clock_t clock;
    uint32_t ms;
    bool publish_load;
} fixture_t;

static void sample(fixture_t *f)
{
    km_operation_core_t *c = &f->core;
    for (uint8_t lane = 0; lane < c->profile.lane_count; ++lane)
        km_core_set_sensors_at(c, lane, c->filament_present_bay[lane],
            c->filament_present_hub, c->filament_present_toolhead, true, f->ms);
    if (f->publish_load)
        for (uint8_t lane = 0; lane < c->profile.lane_count; ++lane)
            km_core_set_load_sensor_at(c, lane, c->filament_present_load[lane], true, f->ms);
    km_core_set_encoder_at(c, c->encoder_clicks, true, f->ms);
    assert(km_buffer_update_switches(&c->buffer_sensor, false, false, f->ms) == KM_OK);
}
static void setup(fixture_t *f, bool powered, bool encoder)
{
    memset(f, 0, sizeof(*f));
    km_device_profile_t p;
    if (powered) km_profile_init_boxturtle(&p);
    else km_profile_init_nightowl(&p);
    p.has_encoder = encoder;
    if (encoder) p.encoder_detection_length_steps = 1000; /* km_profile_validate requires > 0. */
    assert(km_core_init(&f->core, &p) == KM_OK);
    for (uint8_t i = 0; i < p.lane_count; ++i) {
        mock_driver_init(&f->feeder[i], 2, 10, false);
        mock_driver_init(&f->rewinder[i], 2, 10, false);
        assert(km_core_bind_lane_driver(&f->core, i, &f->feeder[i].base,
                                       powered ? &f->rewinder[i].base : NULL) == KM_OK);
        f->core.filament_present_bay[i] = true;
    }
    km_core_host_comm_ready(&f->core, 0);
    km_fake_clock_init(&f->clock, 0);
    sample(f);
}
static void advance(fixture_t *f, uint32_t duration)
{
    for (uint32_t i = 0; i < duration; ++i) {
        ++f->ms;
        assert(km_fake_clock_run_trajectory(&f->clock, &f->core.trajectory, f->ms * 1000u) == KM_OK);
        sample(f);
        km_core_update(&f->core, f->ms, f->ms * 1000u);
    }
}
static void advance_with_current(fixture_t *f, uint32_t duration, uint16_t ma)
{
    for (uint32_t i = 0; i < duration; ++i) {
        ++f->ms;
        assert(km_fake_clock_run_trajectory(&f->clock, &f->core.trajectory, f->ms * 1000u) == KM_OK);
        sample(f);
        km_core_set_rewind_current(&f->core, ma, true, f->ms);
        km_core_update(&f->core, f->ms, f->ms * 1000u);
    }
}
/* The rate core settles jerk-limited, so a stop the old trapezoidal planner
 * completed within a few milliseconds now takes far longer. Advance until an
 * event is queued (or the bound expires) instead of asserting the old planner's
 * exact settle time. */
static void advance_to_event(fixture_t *f, uint32_t limit_ms)
{
    for (uint32_t i = 0; i < limit_ms && km_event_queue_count(&f->core.event_queue) == 0; ++i)
        advance(f, 1);
}
static km_event_t event(fixture_t *f, km_event_type_t type)
{
    km_event_t e;
    assert(km_event_queue_pop(&f->core.event_queue, &e) == KM_OK);
    assert(e.type == type);
    return e;
}
static void load(fixture_t *f, uint32_t session, uint32_t req)
{
    km_core_timepoint_t t = {f->ms, f->ms * 1000u};
    assert(km_core_request_load(&f->core, KM_CLIENT_HOST, session, req, 0, t) == KM_REQ_ACCEPTED);
}
static void arrived(fixture_t *f)
{
    advance(f, 10);
    f->core.filament_present_hub = true;
    advance(f, 1);
    assert(f->core.stage == KM_STAGE_LOAD_FEED_TO_BUFFER);
    advance(f, 10);
    f->core.filament_present_toolhead = true;
    advance(f, 1);
    assert(f->core.stage == KM_STAGE_FOLLOW);
    assert(f->core.shared_path_owned);
}

static void test_identity_and_cancel(void)
{
    fixture_t f; setup(&f, true, false);
    km_core_timepoint_t t0 = {0, 0};
    assert(km_core_request_load(&f.core, KM_CLIENT_HOST, 300, 1000, 7, t0) == KM_REQ_INVALID_LANE);
    load(&f, 300, 1000);
    advance(&f, 20);
    km_core_timepoint_t t1 = {f.ms, f.ms * 1000u};
    assert(km_core_request_load(&f.core, KM_CLIENT_HOST, 300, 1000, 0, t1) == KM_REQ_ALREADY_IN_PROGRESS);
    assert(km_core_request_cancel(&f.core, KM_CLIENT_HOST, 0, 1000, t1) == KM_REQ_REJECTED_STALE);
    assert(km_core_request_cancel(&f.core, KM_CLIENT_HOST, 44, 1000, t1) == KM_REQ_REJECTED_STALE);
    assert(km_core_request_cancel(&f.core, KM_CLIENT_HOST, 300, 744, t1) == KM_REQ_REJECTED_STALE);
    assert(km_core_request_cancel(&f.core, KM_CLIENT_LOCAL_UI, 300, 1000, t1) == KM_REQ_REJECTED_CONFLICT);
    assert(km_core_request_cancel(&f.core, KM_CLIENT_HOST, 300, 1000, t1) == KM_REQ_ACCEPTED);
    assert(f.core.stage == KM_STAGE_BRAKING && f.core.shared_path_owned);
    assert(km_event_queue_count(&f.core.event_queue) == 0);
    assert(km_core_request_load(&f.core, KM_CLIENT_HOST, 300, 1001, 1, t1) == KM_REQ_BUSY);
    advance(&f, 200);
    assert(!f.core.shared_path_owned && !km_trajectory_is_moving(&f.core.trajectory));
    km_event_t e = event(&f, KM_EVENT_LOAD_CANCELLED);
    assert(e.session_id == 300 && e.req_id == 1000 && e.op_type == KM_OP_TYPE_LOAD);
    km_core_timepoint_t t2 = {f.ms, f.ms * 1000u};
    assert(km_core_request_load(&f.core, KM_CLIENT_HOST, 300, 1000, 0, t2) == KM_REQ_ALREADY_COMPLETED);
    km_request_identity_t r = {KM_CLIENT_HOST, 300, 1000, KM_OP_TYPE_LOAD, 0};
    assert(km_core_find_terminal(&f.core, &r, &e) == KM_OK && e.type == KM_EVENT_LOAD_CANCELLED);
    assert(km_core_request_ptfe_cal(&f.core, KM_CLIENT_HOST, 300, 1000, 0, t2) == KM_REQ_REJECTED_STALE);
    assert(km_core_request_load(&f.core, KM_CLIENT_HOST, 299, 1001, 0, t2) == KM_REQ_REJECTED_STALE);
}

static void test_load_follow_unload(bool powered)
{
    fixture_t f; setup(&f, powered, false);
    load(&f, 1, 1); arrived(&f);
    event(&f, KM_EVENT_LOAD_COMPLETE);
    assert(f.core.event_reservations == 1);
    advance(&f, 25);
    /* FOLLOW repeats an unchanged velocity every tick; on the rate core that
     * is a harmless write of the same target. */
    int32_t following_velocity = f.core.trajectory.target_velocity_steps_s;
    advance(&f, 5);
    assert(f.core.stage == KM_STAGE_FOLLOW
           && f.core.trajectory.target_velocity_steps_s == following_velocity);
    km_core_timepoint_t t1 = {f.ms, f.ms * 1000u};
    assert(km_core_request_follow(&f.core, KM_CLIENT_HOST, 1, 2, true, KM_DIR_REVERSE, t1) == KM_REQ_ACCEPTED);
    km_status_snapshot_t following = km_core_get_snapshot(&f.core);
    assert(following.follower_state == KM_FOLLOWER_IDLE);
    assert(following.follower_direction == KM_DIR_REVERSE);
    assert(following.follower_fault == KM_FAULT_NONE);
    assert(km_core_request_follow(&f.core, KM_CLIENT_HOST, 1, 2, true, KM_DIR_FORWARD, t1) == KM_REQ_REJECTED_CONFLICT);
    advance(&f, 10);
    km_core_timepoint_t t2 = {f.ms, f.ms * 1000u};
    assert(km_core_request_unload(&f.core, KM_CLIENT_HOST, 1, 3, t2) == KM_REQ_ACCEPTED);
    assert(km_core_get_snapshot(&f.core).follower_state == KM_FOLLOWER_DISARMED);
    event(&f, KM_EVENT_STOPPED); /* explicit following ended at handoff */
    /* The rewinder is engaged once the feeder's measured velocity reaches zero
     * (the design's "the retract starts once the feeder stops"). The rate core's
     * jerk-limited stop lets the feeder coast forward for a few milliseconds
     * after the retract target is set, where the old planner reached zero in the
     * same tick; wait for the engagement, bounded. */
    if (powered)
        for (unsigned i = 0; i < 40 && !f.rewinder[0].base.enabled; ++i)
            advance(&f, 1);
    assert(f.rewinder[0].base.enabled == powered);
    advance(&f, 20);
    f.core.filament_present_hub = f.core.filament_present_toolhead = false;
    advance(&f, 1);
    assert(f.core.stage == KM_STAGE_BRAKING && f.core.shared_path_owned);
    assert(!f.rewinder[0].base.enabled);
    /* Braking from the retract speed is jerk limited, so the settle takes far
     * longer than the old planner's few milliseconds. */
    advance(&f, 200);
    event(&f, KM_EVENT_UNLOAD_COMPLETE);
    assert(f.core.op_state == KM_OP_STATE_IDLE && !f.core.path_occupied);
    assert(f.core.filament_present_bay[0]); /* Unload does not eject the spool. */
    assert(!f.feeder[0].base.enabled && !f.core.event_reservations);
    assert(f.feeder[0].rising_edge_count == f.feeder[0].falling_edge_count);
}

static void test_unload_cancel_and_ownership(void)
{
    fixture_t f; setup(&f, true, false);
    load(&f, 1, 1); arrived(&f); event(&f, KM_EVENT_LOAD_COMPLETE); advance(&f, 30);
    km_core_timepoint_t t1 = {f.ms, f.ms * 1000u};
    assert(km_core_request_unload(&f.core, KM_CLIENT_HOST, 1, 2, t1) == KM_REQ_ACCEPTED);
    advance(&f, 20);
    km_core_timepoint_t t2 = {f.ms, f.ms * 1000u};
    assert(km_core_request_cancel(&f.core, KM_CLIENT_HOST, 1, 2, t2) == KM_REQ_ACCEPTED);
    /* The cancellation terminal follows the jerk-limited braking stop, which
     * takes far longer than the old planner's few milliseconds. */
    advance(&f, 200);
    event(&f, KM_EVENT_UNLOAD_CANCELLED);
    assert(f.core.path_occupied && f.core.op_state == KM_OP_STATE_LOADED);
    km_core_timepoint_t t3 = {f.ms, f.ms * 1000u};
    assert(km_core_request_load(&f.core, KM_CLIENT_HOST, 1, 3, 1, t3) == KM_REQ_PATH_OCCUPIED);
    /* An unrelated lane's sensor update cannot steal the shared path. */
    km_core_set_sensors_at(&f.core, 1, true, true, true, true, f.ms);
    assert(f.core.path_occupant_lane == 0);
}

static void test_unload_clearance_reasserted_while_braking(bool powered)
{
    fixture_t f; setup(&f, powered, false);
    load(&f, 1, 1); arrived(&f); event(&f, KM_EVENT_LOAD_COMPLETE);
    advance(&f, 30);
    km_core_timepoint_t t = {f.ms, f.ms * 1000u};
    assert(km_core_request_unload(&f.core, KM_CLIENT_HOST, 1, 2, t) == KM_REQ_ACCEPTED);
    /* See test_load_follow_unload: the rewinder engages once the feeder's
     * measured velocity reaches zero, which the jerk-limited rate core reaches
     * a little later than the old planner's same-tick stop. */
    if (powered)
        for (unsigned i = 0; i < 200 && !f.rewinder[0].base.enabled; ++i)
            advance(&f, 1);
    assert(f.rewinder[0].base.enabled == powered);
    advance(&f, 20);
    f.core.filament_present_hub = f.core.filament_present_toolhead = false;
    advance(&f, 1);
    assert(f.core.stage == KM_STAGE_BRAKING);
    f.core.filament_present_hub = true;
    /* The braking stop is jerk limited and must finish before the reasserted
     * clearance is judged, well beyond the old planner's few milliseconds. */
    advance(&f, 200);
    assert(f.core.op_state == KM_OP_STATE_FAULT);
    assert(f.core.system_fault == KM_FAULT_SENSOR_FAILURE);
    assert(f.core.path_occupied && f.core.path_occupant_lane == 0);
    assert(!f.feeder[0].base.enabled && !f.rewinder[0].base.enabled);
    event(&f, KM_EVENT_UNLOAD_FAILED);
}

static void start_unload_with_clearance(fixture_t *f)
{
    setup(f, true, false);
    f->core.profile.unload_hub_clear_steps = 100;
    load(f, 1, 1); arrived(f); event(f, KM_EVENT_LOAD_COMPLETE); advance(f, 30);
    km_core_timepoint_t t = {f->ms, f->ms * 1000u};
    assert(km_core_request_unload(&f->core, KM_CLIENT_HOST, 1, 2, t) == KM_REQ_ACCEPTED);
    advance(f, 20);
    f->core.filament_present_hub = f->core.filament_present_toolhead = false;
    advance(f, 1);
    assert(f->core.stage == KM_STAGE_UNLOAD_STOP_SETTLE);
}

static void test_unload_hub_clearance_success_and_reassertion(void)
{
    fixture_t f; start_unload_with_clearance(&f);
    advance(&f, 100);
    assert(f.core.stage == KM_STAGE_UNLOAD_REWIND_TO_SPOOL);
    int64_t target = f.core.trajectory.target_position_steps;
    int64_t remaining = km_trajectory_get_model_position(&f.core.trajectory) - target;
    assert(f.core.trajectory.has_target_position && remaining > 0 && remaining <= 100);
    assert(f.rewinder[0].base.enabled);
    advance(&f, 1000);
    event(&f, KM_EVENT_UNLOAD_COMPLETE);
    assert(!f.core.path_occupied && !f.rewinder[0].base.enabled);

    start_unload_with_clearance(&f);
    advance(&f, 100);
    assert(f.core.stage == KM_STAGE_UNLOAD_REWIND_TO_SPOOL);
    f.core.filament_present_hub = true;
    advance(&f, 1);
    assert(f.core.op_state == KM_OP_STATE_FAULT);
    assert(f.core.system_fault == KM_FAULT_SENSOR_FAILURE);
    assert(f.core.path_occupied && !f.rewinder[0].base.enabled);
    event(&f, KM_EVENT_UNLOAD_FAILED);
}

static void test_unload_hub_clearance_cancel_and_timeout(void)
{
    fixture_t f; start_unload_with_clearance(&f);
    advance(&f, 100);
    km_core_timepoint_t t = {f.ms, f.ms * 1000u};
    assert(km_core_request_cancel(&f.core, KM_CLIENT_HOST, 1, 2, t) == KM_REQ_ACCEPTED);
    advance(&f, 100);
    advance_to_event(&f, 600);
    assert(!f.rewinder[0].base.enabled && f.core.path_occupied);
    event(&f, KM_EVENT_UNLOAD_CANCELLED);

    start_unload_with_clearance(&f);
    for (unsigned i = 0; i < 100 && f.core.stage == KM_STAGE_UNLOAD_STOP_SETTLE; ++i)
        advance(&f, 1);
    assert(f.core.stage == KM_STAGE_UNLOAD_REWIND_TO_SPOOL);
    f.core.profile.unload_rewind_timeout_ms = 10;
    advance(&f, 10);
    assert(f.core.op_state == KM_OP_STATE_FAULT);
    assert(f.core.system_fault == KM_FAULT_NO_PROGRESS);
    assert(f.core.path_occupied && !f.rewinder[0].base.enabled);
    event(&f, KM_EVENT_UNLOAD_FAILED);
}

static void test_unload_clearance_keeps_current_regulation(void)
{
    fixture_t f; memset(&f, 0, sizeof(f));
    km_device_profile_t p; km_profile_init_boxturtle_enhanced(&p);
    p.has_proportional_buffer = false;
    p.has_diff_calibration = false;
    assert(km_core_init(&f.core, &p) == KM_OK);
    for (uint8_t i = 0; i < p.lane_count; ++i) {
        mock_driver_init(&f.feeder[i], 2, 10, false);
        mock_driver_init(&f.rewinder[i], 2, 10, true);
        assert(km_core_bind_lane_driver(&f.core, i, &f.feeder[i].base,
                                       &f.rewinder[i].base) == KM_OK);
        f.core.filament_present_bay[i] = true;
    }
    km_core_host_comm_ready(&f.core, 0);
    km_fake_clock_init(&f.clock, 0); sample(&f);
    f.core.profile.unload_hub_clear_steps = 10000;
    load(&f, 1, 1); arrived(&f); event(&f, KM_EVENT_LOAD_COMPLETE); advance(&f, 30);
    km_core_set_rewind_current(&f.core, 500, true, f.ms);
    km_core_timepoint_t t = {f.ms, f.ms * 1000u};
    assert(km_core_request_unload(&f.core, KM_CLIENT_HOST, 1, 2, t) == KM_REQ_ACCEPTED);
    advance_with_current(&f, 20, 500);
    f.core.filament_present_hub = f.core.filament_present_toolhead = false;
    advance_with_current(&f, 1, 500);
    assert(f.core.stage == KM_STAGE_UNLOAD_STOP_SETTLE);
    for (unsigned i = 0; i < 100 && f.core.stage == KM_STAGE_UNLOAD_STOP_SETTLE; ++i)
        advance_with_current(&f, 1, 500);
    assert(f.core.stage == KM_STAGE_UNLOAD_REWIND_TO_SPOOL);
    assert(f.rewinder[0].base.enabled && f.rewinder[0].base.pwm_duty_permille == 600);
    advance_with_current(&f, 1, 700);
    assert(f.rewinder[0].base.pwm_duty_permille == 560);

    km_core_set_rewind_current(&f.core, 0, false, f.ms);
    advance(&f, 1);
    assert(f.core.op_state == KM_OP_STATE_FAULT);
    assert(f.core.system_fault == KM_FAULT_SENSOR_FAILURE);
    assert(!f.rewinder[0].base.enabled && f.core.path_occupied);
    event(&f, KM_EVENT_UNLOAD_FAILED);
}

static void test_fault_queue_and_reconnect(void)
{
    fixture_t f; setup(&f, true, false);
    km_event_t filler = {0}; filler.type = KM_EVENT_STOPPED;
    for (unsigned i = 0; i < KM_EVENT_QUEUE_CAPACITY - 2u; ++i)
        assert(km_event_queue_push(&f.core.event_queue, &filler) == KM_OK);
    load(&f, 1, 1);
    assert(km_event_queue_push(&f.core.event_queue, &filler) == KM_ERR_QUEUE_FULL);
    advance(&f, 10);
    f.feeder[0].injected_fault = KM_FAULT_DRIVER_FAULT;
    for (unsigned i = 0; i < 100; ++i) km_core_update(&f.core, f.ms, f.ms * 1000u);
    assert(km_event_queue_count(&f.core.event_queue) == 15);
    assert(!f.feeder[0].base.enabled && !f.core.event_reservations);
    for (unsigned i = 0; i < 14; ++i) event(&f, KM_EVENT_STOPPED);
    event(&f, KM_EVENT_LOAD_FAILED);
    km_core_timepoint_t t1 = {f.ms, f.ms * 1000u};
    assert(km_core_recover(&f.core, 0, false, 0, t1) == KM_ERR_FAULTED);
    f.feeder[0].injected_fault = KM_FAULT_NONE;
    km_core_host_comm_lost(&f.core, f.ms);
    km_core_host_comm_ready(&f.core, f.ms);
    assert(f.core.op_state == KM_OP_STATE_FAULT && !f.feeder[0].base.enabled);
    assert(km_core_clear_fault(&f.core) == KM_ERR_UNAVAILABLE);
    assert(km_core_recover(&f.core, 0, false, 0, t1) == KM_OK);
    assert(!f.feeder[0].base.enabled);
    load(&f, 2, 1); /* New session only after reconciliation. */
}

static void test_full_queue_idle_fault(void)
{
    fixture_t f; setup(&f, false, false);
    km_event_t filler = {0}; filler.type = KM_EVENT_STOPPED;
    for (unsigned i = 0; i < KM_EVENT_QUEUE_CAPACITY; ++i)
        assert(km_event_queue_push(&f.core.event_queue, &filler) == KM_OK);
    km_core_timepoint_t t0 = {0, 0};
    assert(km_core_request_load(&f.core, KM_CLIENT_HOST, 1, 1, 0, t0) == KM_REQ_BUSY);
    km_core_explicit_stop(&f.core, KM_FAULT_EMERGENCY_STOP, 0);
    assert(f.core.pending_fault_event && km_event_queue_count(&f.core.event_queue) == 16);
    assert(km_core_recover(&f.core, 0, false, 0, t0) == KM_ERR_UNAVAILABLE);
    event(&f, KM_EVENT_STOPPED);
    km_core_update(&f.core, 0, 0);
    assert(!f.core.pending_fault_event && km_event_queue_count(&f.core.event_queue) == 16);
    for (unsigned i = 0; i < 15; ++i) event(&f, KM_EVENT_STOPPED);
    event(&f, KM_EVENT_FAULT_ENTERED);
}

static void test_sensor_driver_and_watchdog(void)
{
    fixture_t f; setup(&f, false, false);
    km_core_set_sensors_at(&f.core, 0, true, false, false, false, 0);
    km_core_timepoint_t t0 = {0, 0};
    assert(km_core_request_load(&f.core, KM_CLIENT_HOST, 1, 1, 0, t0) == KM_REQ_UNAVAILABLE);
    sample(&f);
    f.feeder[0].base.initialized = false;
    assert(km_core_request_load(&f.core, KM_CLIENT_HOST, 1, 1, 0, t0) == KM_REQ_UNAVAILABLE);
    f.feeder[0].base.initialized = true;
    f.core.profile.hub_feed_timeout_ms = 25;
    load(&f, 1, 1); advance(&f, 25); /* Three attempts end in one terminal. */
    for (unsigned i = 0; i < 1000 && f.core.op_state != KM_OP_STATE_FAULT; ++i) advance(&f, 1);
    assert(f.core.motion_fault == KM_FAULT_NO_PROGRESS && f.core.load_attempt_count == 3);
    km_event_t e = event(&f, KM_EVENT_LOAD_FAILED);
    assert(e.result_code == KM_WIRE_OP_CODE_TIMEOUT);
    setup(&f, false, true);
    load(&f, 1, 1); advance(&f, 5);
    km_core_set_encoder_at(&f.core, 0, false, f.ms);
    km_core_update(&f.core, f.ms, f.ms * 1000u);
    assert(f.core.motion_fault == KM_FAULT_SENSOR_FAILURE);
    event(&f, KM_EVENT_LOAD_FAILED);
    setup(&f, false, false); f.core.profile.host_watchdog_ms = 10;
    load(&f, 1, 1); advance(&f, 10);
    assert(f.core.motion_fault == KM_FAULT_HOST_COMM_LOST && !f.core.host_connected);
    event(&f, KM_EVENT_LOAD_FAILED);
    /* ADC/path freshness is independent of the host lease. */
    setup(&f, false, false); load(&f, 1, 1);
    assert(km_fake_clock_run_trajectory(&f.clock, &f.core.trajectory, 201000) == KM_OK);
    km_core_update(&f.core, 201, 201000);
    assert(f.core.motion_fault == KM_FAULT_SENSOR_FAILURE);
    event(&f, KM_EVENT_LOAD_FAILED);
}

static void test_presence_batch(void)
{
    fixture_t f;
    bool bays[KM_MAX_LANES] = {0};
    /* Bay presence, including exactly one occupied bay, cannot identify the
     * source of filament already in the shared path at startup. */
    for (unsigned mask = 0; mask < 16; ++mask) {
        setup(&f, false, false);
        for (unsigned i = 0; i < 4; ++i) bays[i] = (mask & (1u << i)) != 0;
        assert(km_core_set_presence_at(&f.core, bays, true, false, true, 0) == KM_OK);
        assert(f.core.path_occupied && f.core.path_occupant_lane == KM_LANE_UNKNOWN);
        km_status_snapshot_t status = km_core_get_snapshot(&f.core);
        assert(status.path_occupied && status.path_occupant_lane == KM_LANE_UNKNOWN);
        for (uint8_t i = 0; i < 4; ++i) {
            assert(f.core.filament_present_bay[i] == bays[i] && f.core.bay_valid[i]);
            assert(f.core.bay_sample_ms[i] == 0);
            assert(km_core_request_load(&f.core, KM_CLIENT_HOST, 1, (uint32_t)i + 1u,
                       i, (km_core_timepoint_t){0, 0}) == KM_REQ_PATH_OCCUPIED);
        }
        assert(km_core_set_presence_at(&f.core, bays, false, false, true, 1) == KM_OK);
        assert(f.core.path_occupied && f.core.path_occupant_lane == KM_LANE_UNKNOWN);
        assert(km_core_set_presence_at(&f.core, bays, true, true, false, 2) == KM_OK);
        assert(!f.core.bay_sensors_valid && !f.core.hub_sensor_valid && !f.core.toolhead_sensor_valid);
        assert(f.core.path_occupied && f.core.path_occupant_lane == KM_LANE_UNKNOWN);
    }
    setup(&f, false, false);
    memset(bays, 1, sizeof(bays));
    assert(km_core_request_load(&f.core, KM_CLIENT_HOST, 1, 1, 3,
                               (km_core_timepoint_t){0, 0}) == KM_REQ_ACCEPTED);
    assert(km_core_set_presence_at(&f.core, bays, false, true, true, UINT32_MAX) == KM_OK);
    assert(f.core.path_occupant_lane == 3 && f.core.path_sample_ms == UINT32_MAX);
    for (unsigned i = 0; i < 4; ++i) assert(f.core.bay_sample_ms[i] == UINT32_MAX);
    assert(km_core_set_presence_at(&f.core, bays, true, false, true, 0) == KM_OK);
    assert(f.core.path_sample_ms == 0 && f.core.path_occupant_lane == 3);

    setup(&f, false, false);
    assert(km_core_recover(&f.core, 0, true, 2, (km_core_timepoint_t){0, 0}) == KM_OK);
    assert(km_core_set_presence_at(&f.core, bays, true, true, true, 0) == KM_OK);
    assert(f.core.path_occupant_lane == 2); /* Preserve reconciled source. */
    km_operation_core_t before = f.core;
    assert(km_core_set_presence_at(NULL, bays, false, false, true, 0) == KM_ERR_INVALID_PARAM);
    assert(km_core_set_presence_at(&f.core, NULL, false, false, true, 0) == KM_ERR_INVALID_PARAM);
    assert(!memcmp(&before, &f.core, sizeof(before)));
    for (uint8_t lanes = 0; lanes <= KM_MAX_LANES + 1; lanes += KM_MAX_LANES + 1) {
        f.core.profile.lane_count = lanes;
        before = f.core;
        assert(km_core_set_presence_at(&f.core, bays, false, false, true, 0) == KM_ERR_INVALID_PARAM);
        assert(!memcmp(&before, &f.core, sizeof(before)));
    }
}

/* Diagnostic-only idle-edge counters (docs/diagnostics.md): accepted switch
 * level changes while that lane (or, for HUB, any lane) is not being moved
 * by this MMU. setup() already samples once (every bay true, HUB false); a
 * counted edge is a CHANGE from that, never the fixture's own resampling. */
static void test_idle_edges(void)
{
    fixture_t f;
    setup(&f, true, false);
    bool bays[KM_MAX_LANES];
    for (unsigned i = 0; i < f.core.profile.lane_count; ++i)
        bays[i] = f.core.filament_present_bay[i];
    bool hub = f.core.filament_present_hub;

    /* Re-asserting the current values is not a new edge. */
    assert(km_core_set_presence_at(&f.core, bays, hub, false, true, 1) == KM_OK);
    assert(f.core.bay_idle_edges[0] == 0 && f.core.hub_idle_edges == 0);

    /* At rest, a bay transition counts once. */
    bays[0] = !bays[0];
    assert(km_core_set_presence_at(&f.core, bays, hub, false, true, 2) == KM_OK);
    assert(f.core.bay_idle_edges[0] == 1);
    /* Repeating the same value is not a new edge. */
    assert(km_core_set_presence_at(&f.core, bays, hub, false, true, 3) == KM_OK);
    assert(f.core.bay_idle_edges[0] == 1);
    /* An invalid capture, then a different value on recovery, is not counted:
     * the previous sample was not itself valid. */
    assert(km_core_set_presence_at(&f.core, bays, hub, false, false, 4) == KM_OK);
    bays[0] = !bays[0];
    assert(km_core_set_presence_at(&f.core, bays, hub, false, true, 5) == KM_OK);
    assert(f.core.bay_idle_edges[0] == 1);
    /* HUB while idle also counts. */
    hub = !hub;
    assert(km_core_set_presence_at(&f.core, bays, hub, false, true, 6) == KM_OK);
    assert(f.core.hub_idle_edges == 1);

    /* A LOAD switch transition while idle also counts (first establish a
     * valid baseline, which is never itself an edge). */
    km_core_set_load_sensor_at(&f.core, 0, false, true, 7);
    assert(f.core.load_idle_edges[0] == 0);
    km_core_set_load_sensor_at(&f.core, 0, true, true, 8);
    assert(f.core.load_idle_edges[0] == 1);

    /* Saturates rather than wraps. */
    f.core.bay_idle_edges[2] = UINT16_MAX;
    bays[2] = !bays[2];
    assert(km_core_set_presence_at(&f.core, bays, hub, false, true, 9) == KM_OK);
    assert(f.core.bay_idle_edges[2] == UINT16_MAX);

    /* A fresh fixture (HUB never observed, so admission is not blocked by
     * the sticky occupancy above): BAY edges on a LOAD's own ACTIVE lane are
     * not counted while it feeds, but a different lane's still is, and HUB
     * is suppressed too, for any lane moving. */
    fixture_t g;
    setup(&g, true, false);
    bool gbays[KM_MAX_LANES];
    for (unsigned i = 0; i < g.core.profile.lane_count; ++i)
        gbays[i] = g.core.filament_present_bay[i];
    assert(gbays[0]);
    load(&g, 1, 1);
    advance(&g, 3);
    assert(km_trajectory_is_moving(&g.core.trajectory)
           && g.core.shared_path_owned && g.core.active_lane == 0);
    gbays[0] = !gbays[0]; /* Toggling the ACTIVE lane's own bay while it feeds. */
    assert(km_core_set_presence_at(&g.core, gbays, false, false, true, g.ms + 1) == KM_OK);
    assert(g.core.bay_idle_edges[0] == 0);
    gbays[1] = !gbays[1]; /* A DIFFERENT lane's bay flips while lane 0 feeds. */
    assert(km_core_set_presence_at(&g.core, gbays, false, false, true, g.ms + 2) == KM_OK);
    assert(g.core.bay_idle_edges[1] == 1);
    assert(km_core_set_presence_at(&g.core, gbays, true, false, true, g.ms + 3) == KM_OK);
    assert(g.core.hub_idle_edges == 0); /* HUB just appeared while lane 0 feeds. */
}

static void test_sensor_snapshot(void)
{
    fixture_t f;
    setup(&f, false, false);
    km_sensor_snapshot_t sensors;
    bool bays[KM_MAX_LANES] = {true, false, true, false};
    assert(km_core_set_presence_at(&f.core, bays, true, false, true, UINT32_MAX - 1u) == KM_OK);
    assert(km_core_get_sensor_snapshot(&f.core, 0, &sensors) == KM_OK);
    assert(sensors.lane_count == 4 && sensors.observed_ms == 0);
    for (unsigned i = 0; i < 4; ++i) {
        assert(sensors.bays[i].configured && sensors.bays[i].valid && sensors.bays[i].fresh);
        assert(sensors.bays[i].present == bays[i]);
        assert(sensors.bays[i].sample_ms == UINT32_MAX - 1u);
    }
    assert(sensors.hub.configured && sensors.hub.present && sensors.hub.fresh);
    assert(sensors.toolhead.configured && !sensors.toolhead.present && sensors.toolhead.fresh);
    assert(!sensors.bays[4].configured && !sensors.bays[4].valid);

    /* An expired or future reading retains its diagnostic value but cannot
     * attest physical absence or presence. */
    uint32_t expired = UINT32_MAX - 1u + f.core.profile.max_sensor_age_ms + 1u;
    assert(km_core_get_sensor_snapshot(&f.core, expired, &sensors) == KM_OK);
    assert(sensors.bays[1].valid && !sensors.bays[1].fresh && !sensors.bays[1].present);
    assert(sensors.hub.valid && !sensors.hub.fresh && sensors.hub.present);
    assert(km_core_get_sensor_snapshot(&f.core, UINT32_MAX - 2u, &sensors) == KM_OK);
    assert(!sensors.bays[0].fresh);

    assert(km_core_set_presence_at(&f.core, bays, true, false, false, 10) == KM_OK);
    assert(km_core_get_sensor_snapshot(&f.core, 10, &sensors) == KM_OK);
    assert(sensors.bays[0].present && !sensors.bays[0].valid && !sensors.bays[0].fresh);
    assert(!sensors.toolhead.present && !sensors.toolhead.valid && !sensors.toolhead.fresh);

    f.core.profile.has_hub_sensor = false;
    f.core.profile.has_bay_sensors = false;
    assert(km_core_get_sensor_snapshot(&f.core, 10, &sensors) == KM_OK);
    assert(!sensors.hub.configured && !sensors.hub.present && !sensors.hub.valid);
    assert(!sensors.bays[0].configured && !sensors.bays[0].present && !sensors.bays[0].valid);
    assert(sensors.toolhead.configured && !sensors.toolhead.valid);

    assert(km_core_get_sensor_snapshot(NULL, 0, &sensors) == KM_ERR_INVALID_PARAM);
    assert(km_core_get_sensor_snapshot(&f.core, 0, NULL) == KM_ERR_INVALID_PARAM);
}

static void test_lane_binding_and_calibration(void)
{
    fixture_t f; setup(&f, false, true);
    assert(km_core_bind_lane_driver(&f.core, 1, &f.feeder[0].base, NULL) == KM_ERR_CONFLICT);
    km_core_timepoint_t t0 = {0, 0};
    assert(km_core_request_ptfe_cal(&f.core, KM_CLIENT_HOST, 1, 1, 2, t0) == KM_REQ_ACCEPTED);
    assert(f.core.active_feeder == &f.feeder[2].base);
    assert(km_core_bind_lane_driver(&f.core, 2, &f.feeder[2].base, NULL) == KM_ERR_BUSY);
    /* The jerk-limited rate core needs longer than the old planner's instant
     * spin-up before the bound lane's first step appears. Stepping now runs
     * through the shared reference executor (tests/stepper_reference), not
     * the driver's own edge callback, so "did it step" reads the clock's
     * reference position; "only the bound lane moved" still reads the other
     * driver's own state, which the reference never touches. */
    for (unsigned i = 0; i < 200 && km_ref_position(&f.clock.ref) == 0; ++i)
        advance(&f, 1);
    assert(km_ref_position(&f.clock.ref) > 0 && !f.feeder[0].base.enabled
           && f.feeder[0].step_count == 0);
    f.core.encoder_clicks = 2000;
    f.core.filament_present_hub = f.core.filament_present_toolhead = true;
    advance(&f, 1);
    assert(f.core.calibrated_ptfe_value == 2000 && f.core.stage == KM_STAGE_CALIBRATE_RETRACT);
    advance(&f, 30);
    f.core.filament_present_hub = f.core.filament_present_toolhead = false;
    advance(&f, 1); advance_to_event(&f, 600);
    km_event_t e = event(&f, KM_EVENT_PTFE_CAL_COMPLETE);
    assert(e.value == 2000 && e.lane_idx == 2);
    /* An encoder result remains an unflagged measurement in clicks. */
    assert(e.flags == KM_EVENT_FLAG_NONE && !f.core.cal_step_estimate);
    km_wire_action_status_t wire = km_protocol_wire_encode_event(&e);
    assert(wire.valid && wire.action == KM_WIRE_STATUS_CALIBRATING && wire.value == 2000);
    /* Probe deadline is independent of encoder progress. */
    setup(&f, false, true); f.core.profile.ptfe_cal_probe_timeout_ms = 10;
    assert(km_core_request_ptfe_cal(&f.core, KM_CLIENT_HOST, 1, 1, 0, t0) == KM_REQ_ACCEPTED);
    for (unsigned i = 0; i < 10; ++i) { ++f.core.encoder_clicks; advance(&f, 1); }
    event(&f, KM_EVENT_PTFE_CAL_FAILED);
}

static void test_lane_driver_velocity_admission(void)
{
    km_device_profile_t p;
    km_operation_core_t core;
    mock_driver_t feeder;
    km_profile_init_nightowl(&p);
    mock_driver_init(&feeder, 2, 10, false);
    assert(km_core_init(&core, &p) == KM_OK);

    feeder.base.caps.max_velocity_steps_s = p.default_feed_velocity_steps_s - 1u;
    assert(km_core_bind_lane_driver(&core, 0, &feeder.base, NULL) == KM_ERR_UNAVAILABLE);
    assert(!core.motor_ready && core.feeder_drivers[0] == NULL);

    feeder.base.caps.max_velocity_steps_s = p.default_feed_velocity_steps_s;
    assert(km_core_bind_lane_driver(&core, 0, &feeder.base, NULL) == KM_OK);
    assert(core.motor_ready && core.feeder_drivers[0] == &feeder.base);

    /* Profiles may configure a faster retract than feed. */
    p.default_rewind_velocity_steps_s = p.default_feed_velocity_steps_s + 1u;
    assert(km_core_init(&core, &p) == KM_OK);
    assert(km_core_bind_lane_driver(&core, 0, &feeder.base, NULL) == KM_ERR_UNAVAILABLE);
    assert(!core.motor_ready && core.feeder_drivers[0] == NULL);
    feeder.base.caps.max_velocity_steps_s = p.default_rewind_velocity_steps_s;
    assert(km_core_bind_lane_driver(&core, 0, &feeder.base, NULL) == KM_OK);
    assert(core.motor_ready && core.feeder_drivers[0] == &feeder.base);
}

static void enable_load_switches(fixture_t *f)
{
    f->core.profile.has_load_sensors = true;
    f->publish_load = true;
    sample(f);
}

static void test_preload(void)
{
    fixture_t f; setup(&f, false, false);
    km_request_identity_t id = {KM_CLIENT_HOST, 3, 1, KM_OP_TYPE_PRELOAD, 2};
    km_core_timepoint_t t = {0, 0};
    assert(km_core_request_start(&f.core, &id, KM_DIR_FORWARD, t) == KM_REQ_ACCEPTED);
    assert(f.core.op_state == KM_OP_STATE_PRELOADING);
    advance(&f, 10);
    f.core.filament_present_hub = true;
    advance(&f, 1);
    /* No per-lane LOAD switch, a real HUB: PRELOAD retracts until the HUB
     * clears (parks behind it), same as a selector machine, rather than
     * settling the instant the HUB trips. */
    assert(f.core.stage == KM_STAGE_UNLOAD_RETRACT_TO_HUB);
    assert(f.core.path_occupied && f.core.path_occupant_lane == 2);
    advance(&f, 10);
    f.core.filament_present_hub = false;
    advance(&f, 1);
    assert(f.core.stage == KM_STAGE_BRAKING && f.core.path_occupied);
    advance_to_event(&f, 600);
    km_event_t e = event(&f, KM_EVENT_PRELOAD_COMPLETE);
    assert(e.op_type == KM_OP_TYPE_PRELOAD && e.lane_idx == 2 && e.result_code == KM_WIRE_OP_CODE_SUCCESS);
    assert(!km_protocol_wire_encode_event(&e).valid);
    km_status_snapshot_t snapshot = km_core_get_snapshot(&f.core);
    assert(!km_protocol_wire_encode_snapshot(&snapshot).valid);
    assert(f.core.op_state == KM_OP_STATE_PRELOADED && !f.core.shared_path_owned);
    assert(!f.feeder[2].base.enabled);
    assert(km_core_find_terminal(&f.core, &id, &e) == KM_OK);
    t = (km_core_timepoint_t){f.ms, f.ms * 1000u};
    assert(km_core_request_start(&f.core, &id, KM_DIR_FORWARD, t) == KM_REQ_ALREADY_COMPLETED);
    km_request_identity_t other = {KM_CLIENT_HOST, 3, 2, KM_OP_TYPE_PRELOAD, 1};
    assert(km_core_request_start(&f.core, &other, KM_DIR_FORWARD, t) == KM_REQ_PATH_OCCUPIED);
    km_request_identity_t load_after = {KM_CLIENT_HOST, 3, 2, KM_OP_TYPE_LOAD, 2};
    assert(km_core_request_start(&f.core, &load_after, KM_DIR_FORWARD, t) == KM_REQ_ACCEPTED);
    /* The rest was behind the HUB, not at a LOAD switch: LOAD re-feeds
     * through the HUB before continuing, unlike a LOAD-switch device's
     * resumption straight into the buffer stage. */
    assert(f.core.stage == KM_STAGE_LOAD_FEED_TO_HUB);
    advance(&f, 10);
    f.core.filament_present_hub = true;
    advance(&f, 1);
    assert(f.core.stage == KM_STAGE_LOAD_FEED_TO_BUFFER);
    advance(&f, 10);
    f.core.filament_present_toolhead = true;
    advance(&f, 1);
    event(&f, KM_EVENT_LOAD_COMPLETE);

    setup(&f, false, false);
    id = (km_request_identity_t){KM_CLIENT_HOST, 3, 1, KM_OP_TYPE_PRELOAD, 2};
    assert(km_core_request_start(&f.core, &id, KM_DIR_FORWARD, (km_core_timepoint_t){0, 0}) == KM_REQ_ACCEPTED);
    advance(&f, 10);
    f.core.filament_present_hub = true;
    advance(&f, 1);
    assert(f.core.stage == KM_STAGE_UNLOAD_RETRACT_TO_HUB);
    advance(&f, 10);
    f.core.filament_present_hub = false;
    advance_to_event(&f, 600);
    event(&f, KM_EVENT_PRELOAD_COMPLETE);
    t = (km_core_timepoint_t){f.ms, f.ms * 1000u};
    km_request_identity_t unload = {KM_CLIENT_HOST, 3, 2, KM_OP_TYPE_UNLOAD, 2};
    assert(km_core_request_start(&f.core, &unload, KM_DIR_FORWARD, t) == KM_REQ_ACCEPTED);
    /* The rest was already behind the HUB, so UNLOAD's own retract-to-HUB
     * finds it clear at once -- unlike the old on-HUB rest, no further
     * travel is needed before it can settle. */
    advance_to_event(&f, 600);
    event(&f, KM_EVENT_UNLOAD_COMPLETE);
    assert(!f.core.path_occupied);

    /* The lane's own PRELOAD rest does not block CALIBRATE; another lane's does. */
    setup(&f, false, true);
    id = (km_request_identity_t){KM_CLIENT_HOST, 3, 1, KM_OP_TYPE_PRELOAD, 2};
    assert(km_core_request_start(&f.core, &id, KM_DIR_FORWARD, (km_core_timepoint_t){0, 0}) == KM_REQ_ACCEPTED);
    advance(&f, 10);
    f.core.filament_present_hub = true;
    advance(&f, 1);
    assert(f.core.stage == KM_STAGE_UNLOAD_RETRACT_TO_HUB);
    advance(&f, 10);
    f.core.filament_present_hub = false;
    advance_to_event(&f, 600);
    event(&f, KM_EVENT_PRELOAD_COMPLETE);
    t = (km_core_timepoint_t){f.ms, f.ms * 1000u};
    assert(km_core_request_ptfe_cal(&f.core, KM_CLIENT_HOST, 3, 2, 1, t) == KM_REQ_PATH_OCCUPIED);
    assert(km_core_request_ptfe_cal(&f.core, KM_CLIENT_HOST, 3, 3, 2, t) == KM_REQ_ACCEPTED);
    assert(f.core.op_state == KM_OP_STATE_CALIBRATING);
    f.core.encoder_clicks = f.core.cal_start_clicks + 1500;
    f.core.filament_present_toolhead = true;
    advance(&f, 1);
    assert(f.core.calibrated_ptfe_value == 1500);
    advance(&f, 30);
    f.core.filament_present_hub = f.core.filament_present_toolhead = false;
    advance(&f, 1); advance_to_event(&f, 600);
    assert(event(&f, KM_EVENT_PTFE_CAL_COMPLETE).lane_idx == 2);

    setup(&f, false, false);
    id = (km_request_identity_t){KM_CLIENT_HOST, 3, 1, KM_OP_TYPE_PRELOAD, 0};
    assert(km_core_request_start(&f.core, &id, KM_DIR_FORWARD, (km_core_timepoint_t){0, 0}) == KM_REQ_ACCEPTED);
    advance(&f, 10);
    t = (km_core_timepoint_t){f.ms, f.ms * 1000u};
    assert(km_core_request_cancel_target(&f.core, &id, t) == KM_REQ_ACCEPTED);
    advance_to_event(&f, 600);
    event(&f, KM_EVENT_PRELOAD_CANCELLED);
    assert(!f.core.path_occupied);

    setup(&f, false, false);
    f.core.profile.hub_feed_timeout_ms = 10;
    assert(km_core_request_start(&f.core, &id, KM_DIR_FORWARD, (km_core_timepoint_t){0, 0}) == KM_REQ_ACCEPTED);
    advance(&f, 10);
    e = event(&f, KM_EVENT_PRELOAD_FAILED);
    assert(e.result_code == KM_WIRE_OP_CODE_TIMEOUT);

    /* The old parked-on-the-switch invariant (something disappearing during
     * braking faults the settle) is still what a LOAD-sensor device gets;
     * without one, PRELOAD now retracts off the HUB instead of settling on
     * it, so this fault needs a LOAD-sensor profile to reproduce. */
    setup(&f, false, false);
    enable_load_switches(&f);
    assert(km_core_request_start(&f.core, &id, KM_DIR_FORWARD, (km_core_timepoint_t){0, 0}) == KM_REQ_ACCEPTED);
    advance(&f, 10);
    f.core.filament_present_load[0] = true;
    advance(&f, 1);
    f.core.filament_present_load[0] = false;
    advance_to_event(&f, 600);
    event(&f, KM_EVENT_PRELOAD_FAILED);
    assert(f.core.op_state == KM_OP_STATE_FAULT && f.core.path_occupied);
}

static void test_cancel_preload_during_load_arrival(void)
{
    fixture_t f;
    setup(&f, false, false);
    enable_load_switches(&f);
    km_request_identity_t pre = {KM_CLIENT_HOST, 9, 1, KM_OP_TYPE_PRELOAD, 1};
    km_request_identity_t other = {KM_CLIENT_HOST, 9, 2, KM_OP_TYPE_PRELOAD, 2};
    km_core_timepoint_t t = {0, 0};
    assert(km_core_request_start(&f.core, &pre, KM_DIR_FORWARD, t) == KM_REQ_ACCEPTED);
    advance(&f, 10);
    t = (km_core_timepoint_t){f.ms, f.ms * 1000u};
    assert(km_core_request_cancel_target(&f.core, &pre, t) == KM_REQ_ACCEPTED);
    assert(f.core.stage == KM_STAGE_BRAKING && !f.core.path_occupied);
    f.core.filament_present_load[1] = true;
    advance(&f, 1);
    assert(f.core.path_occupied && f.core.path_occupant_lane == 1);
    advance_to_event(&f, 600);
    km_event_t cancelled = event(&f, KM_EVENT_PRELOAD_CANCELLED);
    assert(cancelled.result_code == KM_WIRE_OP_CODE_CANCEL);
    assert(!f.core.shared_path_owned && f.core.path_occupied
           && f.core.op_state == KM_OP_STATE_PRELOADED);
    t = (km_core_timepoint_t){f.ms, f.ms * 1000u};
    assert(km_core_request_start(&f.core, &other, KM_DIR_FORWARD, t) == KM_REQ_PATH_OCCUPIED);
    km_request_identity_t unload = {KM_CLIENT_HOST, 9, 2, KM_OP_TYPE_UNLOAD, 1};
    assert(km_core_request_start(&f.core, &unload, KM_DIR_REVERSE, t) == KM_REQ_ACCEPTED);
    assert(f.core.stage == KM_STAGE_UNLOAD_RETRACT_TO_LOAD && f.core.unload_from_load_preload);
    advance(&f, 10);
    assert(f.core.stage == KM_STAGE_UNLOAD_RETRACT_TO_LOAD && f.core.path_occupied);
    f.core.filament_present_load[1] = false;
    advance_to_event(&f, 600);
    event(&f, KM_EVENT_UNLOAD_COMPLETE);
    assert(!f.core.path_occupied);

    setup(&f, false, false);
    enable_load_switches(&f);
    assert(km_core_request_start(&f.core, &pre, KM_DIR_FORWARD,
                                 (km_core_timepoint_t){0, 0}) == KM_REQ_ACCEPTED);
    advance(&f, 10);
    t = (km_core_timepoint_t){f.ms, f.ms * 1000u};
    assert(km_core_request_cancel_target(&f.core, &pre, t) == KM_REQ_ACCEPTED);
    f.core.filament_present_load[1] = true;
    advance_to_event(&f, 600);
    event(&f, KM_EVENT_PRELOAD_CANCELLED);
    assert(f.core.op_state == KM_OP_STATE_PRELOADED);
    t = (km_core_timepoint_t){f.ms, f.ms * 1000u};
    km_request_identity_t load_id = {KM_CLIENT_HOST, 9, 2, KM_OP_TYPE_LOAD, 1};
    assert(km_core_request_start(&f.core, &load_id, KM_DIR_FORWARD, t) == KM_REQ_ACCEPTED);
    assert(f.core.stage == KM_STAGE_LOAD_FEED_TO_HUB);

    setup(&f, false, false);
    enable_load_switches(&f);
    assert(km_core_request_start(&f.core, &pre, KM_DIR_FORWARD,
                                 (km_core_timepoint_t){0, 0}) == KM_REQ_ACCEPTED);
    advance(&f, 10);
    t = (km_core_timepoint_t){f.ms, f.ms * 1000u};
    assert(km_core_request_cancel_target(&f.core, &pre, t) == KM_REQ_ACCEPTED);
    f.core.filament_present_load[1] = true;
    advance(&f, 1);
    f.core.filament_present_load[1] = false;
    advance_to_event(&f, 600);
    event(&f, KM_EVENT_PRELOAD_CANCELLED);
    assert(f.core.op_state == KM_OP_STATE_LOADED && f.core.path_occupied);
    f.core.filament_present_load[1] = true;
    sample(&f);
    t = (km_core_timepoint_t){f.ms, f.ms * 1000u};
    assert(km_core_request_start(&f.core, &unload, KM_DIR_REVERSE, t) == KM_REQ_ACCEPTED);
    assert(f.core.stage == KM_STAGE_UNLOAD_RETRACT_TO_LOAD);

    setup(&f, false, false);
    enable_load_switches(&f);
    assert(km_core_request_start(&f.core, &pre, KM_DIR_FORWARD,
                                 (km_core_timepoint_t){0, 0}) == KM_REQ_ACCEPTED);
    advance(&f, 10);
    t = (km_core_timepoint_t){f.ms, f.ms * 1000u};
    assert(km_core_request_cancel_target(&f.core, &pre, t) == KM_REQ_ACCEPTED);
    advance_to_event(&f, 600);
    event(&f, KM_EVENT_PRELOAD_CANCELLED);
    assert(!f.core.path_occupied && f.core.op_state == KM_OP_STATE_IDLE);
    t = (km_core_timepoint_t){f.ms, f.ms * 1000u};
    assert(km_core_request_start(&f.core, &other, KM_DIR_FORWARD, t) == KM_REQ_ACCEPTED);
}

static void test_lane_load_switch(void)
{
    fixture_t f;
    km_device_profile_t p;
    km_profile_init_boxturtle(&p);
    p.has_load_sensors = true;
    p.has_bay_sensors = false;
    assert(km_profile_validate(&p) == KM_ERR_INVALID_PARAM);

    setup(&f, false, false);
    enable_load_switches(&f);
    km_request_identity_t pre = {KM_CLIENT_HOST, 5, 1, KM_OP_TYPE_PRELOAD, 1};
    km_request_identity_t load_id = {KM_CLIENT_HOST, 5, 2, KM_OP_TYPE_LOAD, 1};
    km_core_timepoint_t t = {0, 0};
    km_sensor_snapshot_t sensors;
    assert(km_core_get_sensor_snapshot(&f.core, 0, &sensors) == KM_OK);
    assert(sensors.loads[1].configured && sensors.loads[1].valid && sensors.loads[1].fresh
           && !sensors.loads[1].present);
    assert(km_core_request_start(&f.core, &load_id, KM_DIR_FORWARD, t) == KM_REQ_UNAVAILABLE);
    km_core_set_load_sensor_at(&f.core, 1, false, false, 0);
    assert(km_core_request_start(&f.core, &pre, KM_DIR_FORWARD, t) == KM_REQ_UNAVAILABLE);
    sample(&f);
    f.core.filament_present_load[1] = true;
    sample(&f);
    assert(km_core_request_start(&f.core, &pre, KM_DIR_FORWARD, t) == KM_REQ_UNAVAILABLE);
    f.core.filament_present_load[1] = false;
    sample(&f);
    assert(km_core_request_start(&f.core, &pre, KM_DIR_FORWARD, t) == KM_REQ_ACCEPTED);
    advance(&f, 10);
    f.core.filament_present_load[1] = true;
    advance(&f, 1);
    assert(f.core.stage == KM_STAGE_BRAKING && f.core.path_occupied
           && f.core.path_occupant_lane == 1);
    advance_to_event(&f, 600);
    event(&f, KM_EVENT_PRELOAD_COMPLETE);
    assert(f.core.op_state == KM_OP_STATE_PRELOADED && f.core.path_occupied
           && !f.core.shared_path_owned);
    t = (km_core_timepoint_t){f.ms, f.ms * 1000u};
    km_request_identity_t other = {KM_CLIENT_HOST, 5, 2, KM_OP_TYPE_PRELOAD, 2};
    assert(km_core_request_start(&f.core, &other, KM_DIR_FORWARD, t) == KM_REQ_PATH_OCCUPIED);
    km_request_identity_t unload = {KM_CLIENT_HOST, 5, 2, KM_OP_TYPE_UNLOAD, 1};
    f.core.filament_present_load[1] = false;
    sample(&f);
    assert(km_core_request_start(&f.core, &unload, KM_DIR_REVERSE, t) == KM_REQ_UNAVAILABLE);
    f.core.filament_present_load[1] = true;
    sample(&f);
    assert(f.core.path_occupied && f.core.path_occupant_lane == 1);
    assert(km_core_request_start(&f.core, &load_id, KM_DIR_FORWARD, t) == KM_REQ_ACCEPTED);
    assert(f.core.stage == KM_STAGE_LOAD_FEED_TO_HUB);
    advance(&f, 10);
    f.core.filament_present_hub = true;
    advance(&f, 1);
    assert(f.core.stage == KM_STAGE_LOAD_FEED_TO_BUFFER);

    setup(&f, false, false); enable_load_switches(&f);
    assert(km_core_request_start(&f.core, &pre, KM_DIR_FORWARD,
                                 (km_core_timepoint_t){0, 0}) == KM_REQ_ACCEPTED);
    advance(&f, 10);
    f.core.filament_present_load[1] = true;
    advance_to_event(&f, 600);
    event(&f, KM_EVENT_PRELOAD_COMPLETE);
    t = (km_core_timepoint_t){f.ms, f.ms * 1000u};
    assert(km_core_request_start(&f.core, &unload, KM_DIR_REVERSE, t) == KM_REQ_ACCEPTED);
    assert(f.core.path_occupied && f.core.unload_from_load_preload
           && f.core.stage == KM_STAGE_UNLOAD_RETRACT_TO_LOAD);
    advance(&f, 10);
    f.core.filament_present_load[1] = false;
    advance(&f, 1);
    assert(f.core.stage == KM_STAGE_BRAKING && f.core.path_occupied);
    advance_to_event(&f, 600);
    event(&f, KM_EVENT_UNLOAD_COMPLETE);
    assert(!f.core.path_occupied && f.core.op_state == KM_OP_STATE_IDLE);

    setup(&f, false, false); enable_load_switches(&f);
    assert(km_core_request_start(&f.core, &pre, KM_DIR_FORWARD,
                                 (km_core_timepoint_t){0, 0}) == KM_REQ_ACCEPTED);
    advance(&f, 10);
    f.core.filament_present_load[1] = true;
    advance_to_event(&f, 600);
    event(&f, KM_EVENT_PRELOAD_COMPLETE);
    t = (km_core_timepoint_t){f.ms, f.ms * 1000u};
    assert(km_core_request_start(&f.core, &unload, KM_DIR_REVERSE, t) == KM_REQ_ACCEPTED);
    advance(&f, 10);
    f.core.filament_present_load[1] = false;
    advance(&f, 1);
    f.core.filament_present_load[1] = true;
    advance_to_event(&f, 600);
    event(&f, KM_EVENT_UNLOAD_FAILED);
    assert(f.core.system_fault == KM_FAULT_SENSOR_FAILURE && f.core.path_occupied);

    setup(&f, false, false); enable_load_switches(&f);
    assert(km_core_request_start(&f.core, &pre, KM_DIR_FORWARD,
                                 (km_core_timepoint_t){0, 0}) == KM_REQ_ACCEPTED);
    advance(&f, 10);
    f.core.filament_present_load[1] = true;
    advance_to_event(&f, 600);
    event(&f, KM_EVENT_PRELOAD_COMPLETE);
    t = (km_core_timepoint_t){f.ms, f.ms * 1000u};
    assert(km_core_request_start(&f.core, &unload, KM_DIR_REVERSE, t) == KM_REQ_ACCEPTED);
    f.core.filament_present_hub = true;
    advance(&f, 1);
    event(&f, KM_EVENT_UNLOAD_FAILED);
    assert(f.core.system_fault == KM_FAULT_SENSOR_FAILURE && f.core.path_occupied);

    setup(&f, false, false); enable_load_switches(&f);
    f.core.filament_present_load[1] = true;
    sample(&f);
    assert(km_core_request_start(&f.core, &load_id, KM_DIR_FORWARD, (km_core_timepoint_t){0, 0}) == KM_REQ_ACCEPTED);
    assert(f.core.stage == KM_STAGE_LOAD_FEED_TO_HUB);
    f.core.filament_present_load[1] = false;
    advance(&f, 1);
    event(&f, KM_EVENT_LOAD_FAILED);
    assert(f.core.system_fault == KM_FAULT_SENSOR_FAILURE && !f.feeder[1].base.enabled);

    setup(&f, false, false); enable_load_switches(&f);
    f.core.profile.hub_feed_timeout_ms = 10;
    assert(km_core_request_start(&f.core, &pre, KM_DIR_FORWARD, (km_core_timepoint_t){0, 0}) == KM_REQ_ACCEPTED);
    advance(&f, 10);
    assert(event(&f, KM_EVENT_PRELOAD_FAILED).result_code == KM_WIRE_OP_CODE_TIMEOUT);

    setup(&f, false, false); enable_load_switches(&f);
    assert(km_core_request_start(&f.core, &pre, KM_DIR_FORWARD, (km_core_timepoint_t){0, 0}) == KM_REQ_ACCEPTED);
    f.core.filament_present_bay[1] = false;
    advance(&f, 1);
    event(&f, KM_EVENT_PRELOAD_FAILED);
    assert(f.core.system_fault == KM_FAULT_SENSOR_FAILURE);

    setup(&f, false, false); enable_load_switches(&f);
    assert(km_core_request_start(&f.core, &pre, KM_DIR_FORWARD, (km_core_timepoint_t){0, 0}) == KM_REQ_ACCEPTED);
    f.publish_load = false;
    km_core_set_load_sensor_at(&f.core, 1, false, false, 1);
    advance(&f, 1);
    event(&f, KM_EVENT_PRELOAD_FAILED);
    assert(f.core.system_fault == KM_FAULT_SENSOR_FAILURE);

    setup(&f, false, false); enable_load_switches(&f);
    assert(km_core_request_start(&f.core, &pre, KM_DIR_FORWARD, (km_core_timepoint_t){0, 0}) == KM_REQ_ACCEPTED);
    f.publish_load = false;
    advance(&f, f.core.profile.max_sensor_age_ms + 1);
    event(&f, KM_EVENT_PRELOAD_FAILED);
    assert(f.core.system_fault == KM_FAULT_SENSOR_FAILURE);

    setup(&f, false, false); enable_load_switches(&f);
    assert(km_core_request_start(&f.core, &pre, KM_DIR_FORWARD, (km_core_timepoint_t){0, 0}) == KM_REQ_ACCEPTED);
    f.core.filament_present_load[1] = true;
    advance(&f, 1);
    f.core.filament_present_load[1] = false;
    advance_to_event(&f, 600);
    event(&f, KM_EVENT_PRELOAD_FAILED);
    assert(f.core.system_fault == KM_FAULT_SENSOR_FAILURE && f.core.path_occupied
           && f.core.path_occupant_lane == 1);

    setup(&f, false, false); enable_load_switches(&f);
    f.publish_load = false;
    f.ms = f.core.profile.max_sensor_age_ms + 1;
    t = (km_core_timepoint_t){f.ms, f.ms * 1000u};
    assert(km_core_request_start(&f.core, &pre, KM_DIR_FORWARD, t) == KM_REQ_UNAVAILABLE);
    assert(km_core_request_start(&f.core, &load_id, KM_DIR_FORWARD, t) == KM_REQ_UNAVAILABLE);
    assert(km_core_get_sensor_snapshot(&f.core, f.ms, &sensors) == KM_OK);
    assert(sensors.loads[1].valid && !sensors.loads[1].fresh);
}

static void setup_emu(fixture_t *f)
{
    memset(f, 0, sizeof(*f));
    km_device_profile_t p;
    km_profile_init_emu(&p);
    assert(km_core_init(&f->core, &p) == KM_OK);
    for (uint8_t lane = 0; lane < p.lane_count; ++lane) {
        mock_driver_init(&f->feeder[lane], 2, 10, false);
        assert(km_core_bind_lane_driver(&f->core, lane, &f->feeder[lane].base, NULL) == KM_OK);
        f->core.filament_present_bay[lane] = true;
    }
    f->publish_load = true;
    km_core_host_comm_ready(&f->core, 0);
    km_fake_clock_init(&f->clock, 0);
    sample(f);
}

static void test_emu_virtual_hub(void)
{
    fixture_t f;
    setup_emu(&f);
    km_sensor_snapshot_t sensors;
    assert(km_core_get_sensor_snapshot(&f.core, 0, &sensors) == KM_OK);
    assert(sensors.hub.configured && !sensors.hub.valid && !sensors.hub.fresh);
    f.publish_load = false;
    f.ms = f.core.profile.max_sensor_age_ms + 1;
    assert(km_core_request_preload(&f.core, KM_CLIENT_HOST, 1, 1, 0,
        (km_core_timepoint_t){f.ms, f.ms * 1000u}) == KM_REQ_UNAVAILABLE);
    assert(km_core_get_sensor_snapshot(&f.core, f.ms, &sensors) == KM_OK);
    assert(!sensors.hub.fresh && !sensors.loads[0].fresh);
    setup_emu(&f);
    assert(km_core_request_preload(&f.core, KM_CLIENT_HOST, 1, 1, 0,
        (km_core_timepoint_t){0, 0}) == KM_REQ_ACCEPTED);
    advance(&f, 10);
    f.core.filament_present_load[0] = true;
    advance(&f, 1);
    assert(f.core.stage == KM_STAGE_UNLOAD_RETRACT_TO_LOAD);
    assert(f.core.path_occupied && f.core.filament_present_hub);
    f.core.filament_present_load[0] = false;
    advance(&f, 1);
    assert(f.core.stage == KM_STAGE_BRAKING);
    advance_to_event(&f, 600);
    event(&f, KM_EVENT_PRELOAD_COMPLETE);
    assert(f.core.op_state == KM_OP_STATE_PRELOADED && f.core.path_occupied);
    assert(!f.core.filament_present_hub);
    assert(km_core_request_load(&f.core, KM_CLIENT_HOST, 1, 2, 0,
        (km_core_timepoint_t){f.ms, f.ms * 1000u}) == KM_REQ_ACCEPTED);
    assert(f.core.stage == KM_STAGE_LOAD_FEED_TO_HUB);
    f.core.filament_present_load[0] = true;
    advance(&f, 1);
    assert(f.core.stage == KM_STAGE_LOAD_FEED_TO_BUFFER);
    f.core.filament_present_toolhead = true;
    advance(&f, 1);
    event(&f, KM_EVENT_LOAD_COMPLETE);
    assert(f.core.stage == KM_STAGE_FOLLOW);
    assert(km_core_request_unload(&f.core, KM_CLIENT_HOST, 1, 3,
        (km_core_timepoint_t){f.ms, f.ms * 1000u}) == KM_REQ_ACCEPTED);
    f.core.filament_present_toolhead = false;
    f.core.filament_present_load[0] = false;
    advance(&f, 1);
    assert(f.core.stage == KM_STAGE_BRAKING);
    advance_to_event(&f, 600);
    event(&f, KM_EVENT_UNLOAD_COMPLETE);
    assert(!f.core.path_occupied);

    setup_emu(&f);
    assert(km_core_request_preload(&f.core, KM_CLIENT_HOST, 4, 1, 0,
        (km_core_timepoint_t){0, 0}) == KM_REQ_ACCEPTED);
    f.core.filament_present_load[0] = true;
    advance(&f, 1);
    f.core.filament_present_load[0] = false;
    advance_to_event(&f, 600);
    event(&f, KM_EVENT_PRELOAD_COMPLETE);
    assert(f.core.op_state == KM_OP_STATE_PRELOADED && f.core.path_occupied);
    uint32_t prior_steps = f.feeder[0].rising_edge_count;
    assert(km_core_request_unload(&f.core, KM_CLIENT_HOST, 4, 2,
        (km_core_timepoint_t){f.ms, f.ms * 1000u}) == KM_REQ_ACCEPTED);
    assert(f.core.stage == KM_STAGE_BRAKING);
    advance(&f, 1);
    event(&f, KM_EVENT_UNLOAD_COMPLETE);
    assert(f.core.op_state == KM_OP_STATE_IDLE && !f.core.path_occupied);
    assert(f.feeder[0].rising_edge_count == prior_steps);

    setup_emu(&f);
    f.core.profile.unload_retract_timeout_ms = 10;
    assert(km_core_request_preload(&f.core, KM_CLIENT_HOST, 2, 1, 0,
        (km_core_timepoint_t){0, 0}) == KM_REQ_ACCEPTED);
    f.core.filament_present_load[0] = true;
    advance(&f, 1);
    assert(f.core.stage == KM_STAGE_UNLOAD_RETRACT_TO_LOAD);
    advance(&f, 10);
    event(&f, KM_EVENT_PRELOAD_FAILED);
    assert(f.core.system_fault == KM_FAULT_NO_PROGRESS);
    assert(f.core.path_occupied);

    setup_emu(&f);
    assert(km_core_request_preload(&f.core, KM_CLIENT_HOST, 3, 1, 0,
        (km_core_timepoint_t){0, 0}) == KM_REQ_ACCEPTED);
    f.core.filament_present_load[0] = true;
    advance(&f, 1);
    f.core.filament_present_load[0] = false;
    advance(&f, 1);
    assert(f.core.stage == KM_STAGE_BRAKING);
    f.core.filament_present_load[0] = true;
    advance_to_event(&f, 600);
    event(&f, KM_EVENT_PRELOAD_FAILED);
    assert(f.core.system_fault == KM_FAULT_SENSOR_FAILURE);
    assert(f.core.path_occupied);
}

static void test_emu_multilane_projection(void)
{
    fixture_t f; setup_emu(&f);
    km_sensor_snapshot_t s;
    km_core_timepoint_t t = {0, 0};
    assert(km_core_request_preload(&f.core, KM_CLIENT_HOST, 8, 1, 3, t) == KM_REQ_ACCEPTED);
    assert(f.core.active_lane == 3 && f.core.hub_sensor_valid && !f.core.filament_present_hub);
    km_core_set_load_sensor_at(&f.core, 0, true, true, 0);
    assert(!f.core.filament_present_hub && !f.core.hub_sensor_valid
        && f.core.path_occupied && f.core.path_occupant_lane == KM_LANE_UNKNOWN);
    assert(km_core_get_sensor_snapshot(&f.core, 0, &s) == KM_OK);
    assert(s.loads[0].present && !s.hub.valid && !s.hub.fresh);
    advance(&f, 1);
    assert(f.core.op_state == KM_OP_STATE_FAULT && f.core.system_fault == KM_FAULT_SENSOR_FAILURE);
    event(&f, KM_EVENT_PRELOAD_FAILED);
    assert(km_core_request_load(&f.core, KM_CLIENT_HOST, 8, 2, 3,
        (km_core_timepoint_t){f.ms, f.ms * 1000u}) == KM_REQ_FAULTED);
    assert(km_core_recover(&f.core, 0, true, 3,
        (km_core_timepoint_t){f.ms, f.ms * 1000u}) == KM_ERR_UNAVAILABLE);

    setup_emu(&f);
    km_core_set_load_sensor_at(&f.core, 3, false, true, 0);
    f.ms = f.core.profile.max_sensor_age_ms + 1;
    for (uint8_t lane = 0; lane < f.core.profile.lane_count; ++lane)
        if (lane != 3) km_core_set_load_sensor_at(&f.core, lane, false, true, f.ms);
    assert(km_core_recover(&f.core, 0, true, 3,
        (km_core_timepoint_t){f.ms, f.ms * 1000u}) == KM_ERR_UNAVAILABLE);

    setup_emu(&f);
    km_core_set_load_sensor_at(&f.core, 4, false, false, 0);
    assert(km_core_request_preload(&f.core, KM_CLIENT_HOST, 9, 1, 3, t) == KM_REQ_UNAVAILABLE);
    assert(km_core_request_load(&f.core, KM_CLIENT_HOST, 9, 2, 3, t) == KM_REQ_UNAVAILABLE);
    km_core_set_load_sensor_at(&f.core, 4, false, true, 0);
    assert(km_core_request_preload(&f.core, KM_CLIENT_HOST, 9, 3, 3, t) == KM_REQ_ACCEPTED);
    f.core.filament_present_load[3] = true;
    advance(&f, 1);
    assert(f.core.path_occupied && f.core.path_occupant_lane == 3);
    assert(f.core.stage == KM_STAGE_UNLOAD_RETRACT_TO_LOAD);
    f.core.filament_present_load[3] = false;
    advance_to_event(&f, 600);
    event(&f, KM_EVENT_PRELOAD_COMPLETE);
    assert(f.core.op_state == KM_OP_STATE_PRELOADED && f.core.path_occupant_lane == 3);
    assert(km_core_request_load(&f.core, KM_CLIENT_HOST, 9, 4, 2,
        (km_core_timepoint_t){f.ms, f.ms * 1000u}) == KM_REQ_PATH_OCCUPIED);
    assert(km_core_request_load(&f.core, KM_CLIENT_HOST, 9, 5, 3,
        (km_core_timepoint_t){f.ms, f.ms * 1000u}) == KM_REQ_ACCEPTED);
    f.core.filament_present_load[3] = true;
    advance(&f, 1);
    assert(f.core.stage == KM_STAGE_LOAD_FEED_TO_BUFFER);
    f.core.filament_present_toolhead = true;
    advance(&f, 1);
    event(&f, KM_EVENT_LOAD_COMPLETE);
    assert(km_core_request_unload(&f.core, KM_CLIENT_HOST, 9, 6,
        (km_core_timepoint_t){f.ms, f.ms * 1000u}) == KM_REQ_ACCEPTED);
    f.core.filament_present_toolhead = false;
    f.core.filament_present_load[3] = false;
    advance_to_event(&f, 600);
    event(&f, KM_EVENT_UNLOAD_COMPLETE);
    assert(!f.core.path_occupied);
    assert(km_core_get_sensor_snapshot(&f.core, f.ms, &s) == KM_OK && !s.hub.valid);
}

static void test_preload_new_host_session(void)
{
    for (uint8_t resume_with_unload = 0; resume_with_unload < 2; ++resume_with_unload) {
        fixture_t f; setup(&f, false, false);
        km_request_identity_t preload = {KM_CLIENT_HOST, 3, 1, KM_OP_TYPE_PRELOAD, 2};
        assert(km_core_request_start(&f.core, &preload, KM_DIR_FORWARD,
            (km_core_timepoint_t){0, 0}) == KM_REQ_ACCEPTED);
        advance(&f, 10);
        f.core.filament_present_hub = true;
        advance(&f, 1);
        assert(f.core.stage == KM_STAGE_UNLOAD_RETRACT_TO_HUB);
        advance(&f, 10);
        f.core.filament_present_hub = false;
        advance_to_event(&f, 600);
        km_event_t terminal = event(&f, KM_EVENT_PRELOAD_COMPLETE);
        assert(terminal.session_id == 3 && terminal.req_id == 1);
        assert(f.core.op_state == KM_OP_STATE_PRELOADED && f.core.path_occupied);

        km_core_timepoint_t t = {f.ms, f.ms * 1000u};
        km_request_identity_t other_lane = {KM_CLIENT_HOST, 4, 1, KM_OP_TYPE_LOAD, 1};
        assert(km_core_request_start(&f.core, &other_lane, KM_DIR_FORWARD, t)
            == KM_REQ_PATH_OCCUPIED);
        km_request_identity_t same_lane = {KM_CLIENT_HOST, 4, 1,
            resume_with_unload ? KM_OP_TYPE_UNLOAD : KM_OP_TYPE_LOAD, 2};
        assert(km_core_request_start(&f.core, &same_lane, KM_DIR_FORWARD, t)
            == KM_REQ_ACCEPTED);
    }
}

static void test_eject_lifecycle(void)
{
    fixture_t f; setup(&f, false, false);
    load(&f, 20, 1); arrived(&f); event(&f, KM_EVENT_LOAD_COMPLETE);
    advance(&f, 30);
    assert(km_core_request_unload(&f.core, KM_CLIENT_HOST, 20, 2,
        (km_core_timepoint_t){f.ms, f.ms * 1000u}) == KM_REQ_ACCEPTED);
    advance(&f, 20);
    f.core.filament_present_hub = f.core.filament_present_toolhead = false;
    advance_to_event(&f, 600);
    event(&f, KM_EVENT_UNLOAD_COMPLETE);
    assert(f.core.op_state == KM_OP_STATE_IDLE && f.core.filament_present_bay[0]);
    km_request_identity_t eject = {KM_CLIENT_HOST, 20, 3, KM_OP_TYPE_EJECT, 0};
    km_core_timepoint_t t = {f.ms, f.ms * 1000u};
    assert(km_core_request_start(&f.core, &eject, KM_DIR_FORWARD, t) == KM_REQ_ACCEPTED);
    assert(f.core.op_state == KM_OP_STATE_EJECTING && f.core.stage == KM_STAGE_EJECT_TO_BAY);
    assert(!km_protocol_wire_encode_snapshot(&(km_status_snapshot_t){.op_state=KM_OP_STATE_EJECTING}).valid);
    advance(&f, 10);
    f.core.filament_present_bay[0] = false;
    advance(&f, 1);
    assert(f.core.stage == KM_STAGE_BRAKING && f.core.shared_path_owned);
    advance_to_event(&f, 600);
    km_event_t e = event(&f, KM_EVENT_EJECT_COMPLETE);
    assert(e.op_type == KM_OP_TYPE_EJECT && e.lane_idx == 0 && e.client_id == KM_CLIENT_HOST
        && e.session_id == 20 && e.req_id == 3 && e.result_code == KM_WIRE_OP_CODE_SUCCESS);
    assert(!km_protocol_wire_encode_event(&e).valid);
    assert(f.core.op_state == KM_OP_STATE_IDLE && !f.core.shared_path_owned
        && !f.feeder[0].base.enabled && !f.core.event_reservations);
    assert(km_core_request_start(&f.core, &eject, KM_DIR_FORWARD,
        (km_core_timepoint_t){f.ms, f.ms * 1000u}) == KM_REQ_ALREADY_COMPLETED);
    km_event_t replay;
    assert(km_core_find_terminal(&f.core, &eject, &replay) == KM_OK
        && replay.seq == e.seq && replay.type == KM_EVENT_EJECT_COMPLETE);
}

static void test_eject_rejections_and_faults(void)
{
    fixture_t f; setup(&f, false, false);
    km_request_identity_t r = {KM_CLIENT_HOST, 21, 1, KM_OP_TYPE_EJECT, 0};
    km_core_timepoint_t t = {0, 0};
    f.core.filament_present_hub = true;
    assert(km_core_request_start(&f.core, &r, KM_DIR_FORWARD, t) == KM_REQ_PATH_OCCUPIED);
    f.core.filament_present_hub = false;
    f.core.path_occupied = true; f.core.path_occupant_lane = 0;
    f.core.op_state = KM_OP_STATE_LOADED;
    assert(km_core_request_start(&f.core, &r, KM_DIR_FORWARD, t) == KM_REQ_PATH_OCCUPIED);
    f.core.path_occupied = false; f.core.op_state = KM_OP_STATE_IDLE;
    f.core.bay_valid[0] = false;
    assert(km_core_request_start(&f.core, &r, KM_DIR_FORWARD, t) == KM_REQ_UNAVAILABLE);
    sample(&f);
    f.core.filament_present_bay[0] = false;
    assert(km_core_request_start(&f.core, &r, KM_DIR_FORWARD, t) == KM_REQ_NO_SPOOL);
    f.core.filament_present_bay[0] = true;
    r.client = KM_CLIENT_INTERNAL; /* Bounded motion test independent of host heartbeat. */
    assert(km_core_request_start(&f.core, &r, KM_DIR_FORWARD, t) == KM_REQ_ACCEPTED);
    advance(&f, f.core.profile.unload_retract_timeout_ms);
    km_event_t e = event(&f, KM_EVENT_EJECT_FAILED);
    assert(e.result_code == KM_WIRE_OP_CODE_TIMEOUT && e.op_type == KM_OP_TYPE_EJECT);
    assert(f.core.op_state == KM_OP_STATE_FAULT && !f.feeder[0].base.enabled);
}

static void test_eject_cancel_and_bounce(void)
{
    for (unsigned bounce = 0; bounce < 2; ++bounce) {
        fixture_t f; setup(&f, false, false);
        km_request_identity_t r = {KM_CLIENT_HOST, 22, 1, KM_OP_TYPE_EJECT, 0};
        assert(km_core_request_start(&f.core, &r, KM_DIR_FORWARD,
            (km_core_timepoint_t){0, 0}) == KM_REQ_ACCEPTED);
        advance(&f, 10);
        if (bounce) {
            f.core.filament_present_bay[0] = false;
            advance(&f, 1);
            assert(f.core.stage == KM_STAGE_BRAKING);
            f.core.filament_present_bay[0] = true;
            advance_to_event(&f, 600);
            assert(event(&f, KM_EVENT_EJECT_FAILED).result_code == KM_WIRE_OP_CODE_ERROR_UNSPECIFIED);
            assert(f.core.op_state == KM_OP_STATE_FAULT);
        } else {
            assert(km_core_request_cancel_target(&f.core, &r,
                (km_core_timepoint_t){f.ms, f.ms * 1000u}) == KM_REQ_ACCEPTED);
            advance_to_event(&f, 600);
            km_event_t e = event(&f, KM_EVENT_EJECT_CANCELLED);
            assert(e.op_type == KM_OP_TYPE_EJECT && e.lane_idx == 0 && e.req_id == 1);
            assert(f.core.op_state == KM_OP_STATE_IDLE);
        }
        assert(!f.feeder[0].base.enabled);
    }
}

/* Without BAY switches a positive eject_final_steps admits EJECT and makes one
 * blind move back by exactly that distance in reference steps. */
static void test_eject_without_bay_moves_the_set_distance(void)
{
    fixture_t f; setup(&f, false, false);
    f.core.profile.has_bay_sensors = false;
    f.core.profile.eject_final_steps = 200;
    f.core.profile.eject_timeout_ms = 10000;
    int64_t start = km_trajectory_get_position(&f.core.trajectory);
    f.core.filament_present_bay[0] = false; /* BAY presence is not required. */
    km_request_identity_t eject = {KM_CLIENT_HOST, 20, 3, KM_OP_TYPE_EJECT, 0};
    km_core_timepoint_t t = {f.ms, f.ms * 1000u};
    assert(km_core_request_start(&f.core, &eject, KM_DIR_FORWARD, t) == KM_REQ_ACCEPTED);
    assert(f.core.op_state == KM_OP_STATE_EJECTING && f.core.stage == KM_STAGE_EJECT_TO_BAY);
    advance_to_event(&f, 3000);
    km_event_t e = event(&f, KM_EVENT_EJECT_COMPLETE);
    assert(e.op_type == KM_OP_TYPE_EJECT && e.lane_idx == 0 && e.result_code == KM_WIRE_OP_CODE_SUCCESS);
    assert(km_trajectory_get_position(&f.core.trajectory) == start - 200);
    assert(f.core.op_state == KM_OP_STATE_IDLE && !f.feeder[0].base.enabled);
}

/* Without BAY switches and no configured distance there is nothing to do. */
static void test_eject_without_bay_or_distance_is_unavailable(void)
{
    fixture_t f; setup(&f, false, false);
    f.core.profile.has_bay_sensors = false;
    f.core.profile.eject_final_steps = 0;
    km_request_identity_t eject = {KM_CLIENT_HOST, 20, 3, KM_OP_TYPE_EJECT, 0};
    km_core_timepoint_t t = {f.ms, f.ms * 1000u};
    assert(km_core_request_start(&f.core, &eject, KM_DIR_FORWARD, t) == KM_REQ_UNAVAILABLE);
}

/* A configured distance is only legal for the no-BAY path and needs a deadline. */
static void test_eject_distance_profile_rules(void)
{
    km_device_profile_t p;
    km_profile_init_nightowl(&p);
    p.eject_final_steps = 100;
    p.eject_timeout_ms = 1000;
    assert(p.has_bay_sensors && km_profile_validate(&p) == KM_ERR_INVALID_PARAM);
    p.has_bay_sensors = true;
    p.eject_final_steps = 0;
    assert(km_profile_validate(&p) == KM_OK);
}

/* Path reconciliation fixtures publish only through production acquisition
 * setters, never the lane-crediting compatibility setter, and count every
 * driver output call. */
static const km_driver_ops_t *mock_ops;
static km_driver_ops_t counted_ops;
static unsigned driver_calls;
static km_result_t counted_enable(km_driver_t *d, bool on)
{ ++driver_calls; return mock_ops->set_enable(d, on); }
static km_result_t counted_direction(km_driver_t *d, km_direction_t dir, uint32_t us)
{ ++driver_calls; return mock_ops->set_direction(d, dir, us); }
static km_result_t counted_pwm(km_driver_t *d, uint16_t duty)
{ ++driver_calls; return mock_ops->set_pwm_duty(d, duty); }
static void counted_stop(km_driver_t *d) { ++driver_calls; mock_ops->stop_outputs(d); }

static void boot(fixture_t *f, void (*init)(km_device_profile_t *))
{
    memset(f, 0, sizeof(*f));
    km_device_profile_t p;
    init(&p);
    assert(km_core_init(&f->core, &p) == KM_OK);
    for (uint8_t i = 0; i < p.lane_count; ++i) {
        mock_driver_init(&f->feeder[i], 2, 10, false);
        mock_driver_init(&f->rewinder[i], 2, 10, false);
        mock_ops = f->feeder[i].base.ops;
        counted_ops = *mock_ops;
        counted_ops.set_enable = counted_enable;
        counted_ops.set_direction = counted_direction;
        counted_ops.set_pwm_duty = counted_pwm;
        counted_ops.stop_outputs = counted_stop;
        f->feeder[i].base.ops = f->rewinder[i].base.ops = &counted_ops;
        assert(km_core_bind_lane_driver(&f->core, i, &f->feeder[i].base,
            p.has_powered_rewind ? &f->rewinder[i].base : NULL) == KM_OK);
    }
    driver_calls = 0;
}
static km_core_timepoint_t at(uint32_t ms) { return (km_core_timepoint_t){ms, ms * 1000u}; }
static void refused(fixture_t *f, bool occupied, uint8_t lane, km_core_timepoint_t t,
                    km_result_t expected)
{
    km_operation_core_t before;
    memcpy(&before, &f->core, sizeof(before));
    assert(km_core_reconcile_path(&f->core, occupied, lane, t) == expected);
    assert(!memcmp(&before, &f->core, sizeof(before)));
}

static void test_reconcile_adopt(void)
{
    fixture_t f; boot(&f, km_profile_init_nightowl);
    uint32_t age = f.core.profile.max_sensor_age_ms;
    /* Several bays hold filament; none identifies the shared-path source. */
    bool bays[KM_MAX_LANES] = {true, false, true, true};
    assert(km_core_set_presence_at(&f.core, bays, true, false, true, 10) == KM_OK);
    assert(f.core.path_occupied && f.core.path_occupant_lane == KM_LANE_UNKNOWN);
    assert(km_core_reconcile_path(NULL, false, KM_LANE_UNKNOWN, at(10)) == KM_ERR_INVALID_PARAM);
    for (unsigned lane = 0; lane <= UINT8_MAX; ++lane) {
        if (lane >= 4) refused(&f, true, (uint8_t)lane, at(10), KM_ERR_INVALID_PARAM);
        if (lane != KM_LANE_UNKNOWN) refused(&f, false, (uint8_t)lane, at(10), KM_ERR_INVALID_PARAM);
    }
    refused(&f, true, 0, (km_core_timepoint_t){10, 11000}, KM_ERR_INVALID_PARAM);
    refused(&f, false, KM_LANE_UNKNOWN, at(10), KM_ERR_CONFLICT); /* HUB present. */
    refused(&f, true, 1, at(10), KM_ERR_CONFLICT);                /* BAY 1 absent. */
    refused(&f, true, 0, at(10 + age + 1), KM_ERR_UNAVAILABLE);   /* Expired. */
    refused(&f, true, 0, at(9), KM_ERR_UNAVAILABLE);              /* Future sample. */
    assert(km_core_set_presence_at(&f.core, bays, false, true, true, 20) == KM_OK);
    refused(&f, false, KM_LANE_UNKNOWN, at(20), KM_ERR_CONFLICT); /* TOOLHEAD present. */
    refused(&f, true, 0, at(20), KM_ERR_CONFLICT);                /* HUB absent. */
    assert(km_core_set_presence_at(&f.core, bays, false, false, false, 30) == KM_OK);
    assert(f.core.path_occupied);
    refused(&f, false, KM_LANE_UNKNOWN, at(30), KM_ERR_UNAVAILABLE); /* Invalid is not absent. */

    assert(km_core_set_presence_at(&f.core, bays, true, true, true, 40) == KM_OK);
    km_event_t queued = {0}; queued.type = KM_EVENT_STOPPED;
    assert(km_event_queue_push(&f.core.event_queue, &queued) == KM_OK);
    refused(&f, true, 2, at(40), KM_ERR_BUSY);
    assert(km_event_queue_pop(&f.core.event_queue, &queued) == KM_OK);
    f.core.pending_fault_event = true; refused(&f, true, 2, at(40), KM_ERR_FAULTED);
    f.core.pending_fault_event = false;
    f.core.system_fault = KM_FAULT_EMERGENCY_STOP; refused(&f, true, 2, at(40), KM_ERR_FAULTED);
    f.core.system_fault = KM_FAULT_NONE;
    f.core.trajectory.position_uncertain = true; refused(&f, true, 2, at(40), KM_ERR_FAULTED);
    f.core.trajectory.position_uncertain = false;
    f.core.buffer_sensor.fault = KM_FAULT_BUFFER_SIGNAL; refused(&f, true, 2, at(40), KM_ERR_FAULTED);
    f.core.buffer_sensor.fault = KM_FAULT_NONE;
    f.feeder[3].injected_fault = KM_FAULT_DRIVER_FAULT; refused(&f, true, 2, at(40), KM_ERR_FAULTED);
    f.feeder[3].injected_fault = KM_FAULT_NONE;
    f.feeder[1].base.enabled = true; refused(&f, true, 2, at(40), KM_ERR_FAULTED);
    f.feeder[1].base.enabled = false;
    f.core.terminal_pending = true; refused(&f, true, 2, at(40), KM_ERR_BUSY);
    f.core.terminal_pending = false;
    f.core.event_reservations = 1; refused(&f, true, 2, at(40), KM_ERR_BUSY);
    f.core.event_reservations = 0;
    assert(driver_calls == 0);

    km_status_snapshot_t before = km_core_get_snapshot(&f.core);
    assert(km_core_reconcile_path(&f.core, true, 2, at(40)) == KM_OK);
    km_status_snapshot_t after = km_core_get_snapshot(&f.core);
    assert(after.path_occupied && after.path_occupant_lane == 2);
    assert(after.op_state == KM_OP_STATE_LOADED && after.stage == KM_STAGE_STOPPED);
    assert(!after.lane_active && after.active_client == KM_CLIENT_NONE);
    assert(after.system_fault == KM_FAULT_NONE && after.queued_events == before.queued_events);
    assert(!memcmp(after.client_session, before.client_session, sizeof(after.client_session)));
    assert(!memcmp(after.client_request, before.client_request, sizeof(after.client_request)));
    assert(driver_calls == 0 && f.core.terminal_history_count == 0);
    for (unsigned i = 0; i < 4; ++i) assert(!f.feeder[i].base.enabled);
    refused(&f, true, 2, at(40), KM_ERR_STALE);
    refused(&f, false, KM_LANE_UNKNOWN, at(40), KM_ERR_STALE);

    /* The adopted lane, and only that lane, may now UNLOAD. */
    km_core_host_comm_ready(&f.core, 40);
    km_request_identity_t other = {KM_CLIENT_HOST, 1, 1, KM_OP_TYPE_UNLOAD, 0};
    assert(km_core_request_start(&f.core, &other, KM_DIR_REVERSE, at(40)) == KM_REQ_PATH_OCCUPIED);
    km_request_identity_t unload = {KM_CLIENT_HOST, 1, 1, KM_OP_TYPE_UNLOAD, 2};
    assert(km_core_request_start(&f.core, &unload, KM_DIR_REVERSE, at(40)) == KM_REQ_ACCEPTED);
    refused(&f, false, KM_LANE_UNKNOWN, at(40), KM_ERR_BUSY);
    km_core_explicit_stop(&f.core, KM_FAULT_EMERGENCY_STOP, 40);
    refused(&f, false, KM_LANE_UNKNOWN, at(40), KM_ERR_BUSY); /* Fault unacknowledged. */
}

/* A stopped operation fault is repaired by the same operator statement, once
 * the host has acknowledged it; the driver's own latch still blocks. */
static void test_reconcile_after_fault(void)
{
    fixture_t f; boot(&f, km_profile_init_boxturtle);
    uint32_t age = f.core.profile.max_sensor_age_ms;
    bool bays[KM_MAX_LANES] = {true, false, true, true};
    assert(km_core_set_presence_at(&f.core, bays, true, false, true, 10) == KM_OK);
    for (uint8_t lane = 0; lane < 4; ++lane)
        km_core_set_load_sensor_at(&f.core, lane, bays[lane], true, 10);
    assert(km_core_reconcile_path(&f.core, true, 2, at(10)) == KM_OK);
    km_core_host_comm_ready(&f.core, 10);
    km_request_identity_t unload = {KM_CLIENT_HOST, 1, 1, KM_OP_TYPE_UNLOAD, 2};
    assert(km_core_request_start(&f.core, &unload, KM_DIR_REVERSE, at(10)) == KM_REQ_ACCEPTED);
    km_core_explicit_stop(&f.core, KM_FAULT_NO_PROGRESS, 10);
    assert(f.core.op_state == KM_OP_STATE_FAULT && f.core.shared_path_owned);
    assert(f.core.trajectory.position_uncertain);
    int64_t steps = f.core.trajectory.current_position_steps;
    km_status_snapshot_t faulted = km_core_get_snapshot(&f.core);

    refused(&f, true, 2, at(20), KM_ERR_BUSY); /* UNLOAD_FAILED not yet acknowledged. */
    km_event_t e;
    assert(km_event_queue_pop(&f.core.event_queue, &e) == KM_OK);
    assert(e.type == KM_EVENT_UNLOAD_FAILED && !km_event_queue_count(&f.core.event_queue));
    unsigned calls = driver_calls;
    f.core.pending_fault_event = true; refused(&f, true, 2, at(20), KM_ERR_FAULTED);
    f.core.pending_fault_event = false;
    f.feeder[3].injected_fault = KM_FAULT_DRIVER_FAULT; refused(&f, true, 2, at(20), KM_ERR_FAULTED);
    f.feeder[3].injected_fault = KM_FAULT_NONE;
    f.feeder[2].base.enabled = true; refused(&f, true, 2, at(20), KM_ERR_FAULTED);
    f.feeder[2].base.enabled = false;
    f.core.buffer_sensor.fault = KM_FAULT_BUFFER_SIGNAL; refused(&f, true, 2, at(20), KM_ERR_FAULTED);
    f.core.buffer_sensor.fault = KM_FAULT_NONE;
    refused(&f, true, 2, at(10 + age + 1), KM_ERR_UNAVAILABLE); /* Samples expired. */
    refused(&f, false, KM_LANE_UNKNOWN, at(20), KM_ERR_CONFLICT); /* HUB still present. */
    refused(&f, true, 1, at(20), KM_ERR_CONFLICT);                /* Lane 1 is empty. */

    /* The operator removed the filament: CLEAR from FAULT. */
    assert(km_core_set_presence_at(&f.core, bays, false, false, true, 30) == KM_OK);
    assert(km_core_reconcile_path(&f.core, false, KM_LANE_UNKNOWN, at(30)) == KM_OK);
    km_status_snapshot_t s = km_core_get_snapshot(&f.core);
    assert(s.op_state == KM_OP_STATE_IDLE && s.stage == KM_STAGE_STOPPED);
    assert(s.system_fault == KM_FAULT_NONE && !s.path_occupied && !s.lane_active);
    assert(s.active_client == KM_CLIENT_NONE && !s.position_uncertain);
    assert(!f.core.trajectory.faulted && f.core.trajectory.current_position_steps == steps);
    assert(!memcmp(s.client_session, faulted.client_session, sizeof(s.client_session)));
    assert(!memcmp(s.client_request, faulted.client_request, sizeof(s.client_request)));
    assert(driver_calls == calls && !km_event_queue_count(&f.core.event_queue));
    for (unsigned i = 0; i < 4; ++i) assert(!f.feeder[i].base.enabled);
    refused(&f, false, KM_LANE_UNKNOWN, at(30), KM_ERR_STALE);

    /* The failed request stays stale; new work is admitted and faults again the same way. */
    km_core_host_comm_ready(&f.core, 30);
    assert(km_core_request_start(&f.core, &unload, KM_DIR_REVERSE, at(30)) == KM_REQ_ALREADY_COMPLETED);
    km_request_identity_t reused = {KM_CLIENT_HOST, 1, 1, KM_OP_TYPE_EJECT, 0};
    assert(km_core_request_start(&f.core, &reused, KM_DIR_REVERSE, at(30)) == KM_REQ_REJECTED_STALE);
    assert(km_core_set_presence_at(&f.core, bays, true, false, true, 40) == KM_OK);
    assert(km_core_reconcile_path(&f.core, true, 2, at(40)) == KM_OK);
    unload.req_id = 2;
    assert(km_core_request_start(&f.core, &unload, KM_DIR_REVERSE, at(40)) == KM_REQ_ACCEPTED);
    km_core_explicit_stop(&f.core, KM_FAULT_SENSOR_FAILURE, 40);
    assert(f.core.op_state == KM_OP_STATE_FAULT && km_event_queue_count(&f.core.event_queue) == 1);
    assert(km_event_queue_pop(&f.core.event_queue, &e) == KM_OK && e.req_id == 2);
    /* Filament still in the path: ADOPT it from FAULT, then that lane may UNLOAD. */
    assert(km_core_reconcile_path(&f.core, true, 2, at(40)) == KM_OK);
    assert(f.core.op_state == KM_OP_STATE_LOADED && f.core.path_occupant_lane == 2);
    unload.req_id = 3;
    assert(km_core_request_start(&f.core, &unload, KM_DIR_REVERSE, at(40)) == KM_REQ_ACCEPTED);
}

static void test_reconcile_clear(void)
{
    fixture_t f; boot(&f, km_profile_init_boxturtle);
    bool bays[KM_MAX_LANES] = {true, true, false, true};
    assert(km_core_set_presence_at(&f.core, bays, false, true, true, 0) == KM_OK);
    assert(f.core.path_occupied && f.core.path_occupant_lane == KM_LANE_UNKNOWN);
    f.rewinder[1].base.pwm_duty_permille = 1;
    refused(&f, false, KM_LANE_UNKNOWN, at(0), KM_ERR_FAULTED);
    f.rewinder[1].base.pwm_duty_permille = 0;
    refused(&f, false, KM_LANE_UNKNOWN, at(0), KM_ERR_CONFLICT);
    assert(km_core_set_presence_at(&f.core, bays, false, false, true, 5) == KM_OK);
    assert(f.core.path_occupied); /* Absence alone never erases retained occupancy. */
    assert(km_core_reconcile_path(&f.core, false, KM_LANE_UNKNOWN, at(5)) == KM_OK);
    assert(!f.core.path_occupied && f.core.op_state == KM_OP_STATE_IDLE);
    assert(driver_calls == 0 && !km_event_queue_count(&f.core.event_queue));
    for (unsigned i = 0; i < 4; ++i)
        assert(!f.feeder[i].base.enabled && !f.rewinder[i].base.enabled);
    refused(&f, false, KM_LANE_UNKNOWN, at(5), KM_ERR_STALE);
    assert(km_core_set_presence_at(&f.core, bays, true, false, true, 6) == KM_OK);
    assert(f.core.path_occupied && f.core.path_occupant_lane == KM_LANE_UNKNOWN);
}

static void test_reconcile_virtual_hub(void)
{
    fixture_t f; boot(&f, km_profile_init_emu);
    bool bays[KM_MAX_LANES] = {true, true, true, true, true};
    assert(km_core_set_presence_at(&f.core, bays, false, false, true, 0) == KM_OK);
    for (uint8_t lane = 0; lane < 5; ++lane)
        km_core_set_load_sensor_at(&f.core, lane, lane == 1 || lane == 3, true, 0);
    assert(f.core.path_occupied && f.core.path_occupant_lane == KM_LANE_UNKNOWN);
    refused(&f, false, KM_LANE_UNKNOWN, at(0), KM_ERR_CONFLICT);
    refused(&f, true, 1, at(0), KM_ERR_CONFLICT); /* Exit 3 is also asserted. */
    km_core_set_load_sensor_at(&f.core, 3, false, false, 1);
    refused(&f, true, 1, at(1), KM_ERR_UNAVAILABLE);
    km_core_set_load_sensor_at(&f.core, 3, false, true, 1);
    assert(f.core.path_occupant_lane == KM_LANE_UNKNOWN);
    refused(&f, true, 3, at(1), KM_ERR_CONFLICT); /* Selected exit is clear. */
    assert(km_core_reconcile_path(&f.core, true, 1, at(1)) == KM_OK);
    km_sensor_snapshot_t s;
    assert(km_core_get_sensor_snapshot(&f.core, 1, &s) == KM_OK);
    assert(f.core.path_occupant_lane == 1 && s.hub.present && s.hub.fresh);
    assert(driver_calls == 0);

    boot(&f, km_profile_init_emu);
    assert(km_core_set_presence_at(&f.core, bays, false, false, true, 0) == KM_OK);
    for (uint8_t lane = 0; lane < 5; ++lane)
        km_core_set_load_sensor_at(&f.core, lane, lane == 0 || lane == 2, true, 0);
    for (uint8_t lane = 0; lane < 5; ++lane)
        km_core_set_load_sensor_at(&f.core, lane, false, true, 2);
    assert(f.core.path_occupied && f.core.path_occupant_lane == KM_LANE_UNKNOWN);
    assert(km_core_reconcile_path(&f.core, false, KM_LANE_UNKNOWN, at(2)) == KM_OK);
    assert(!f.core.path_occupied && driver_calls == 0);

    /* One asserted exit is already attributed and needs no reconciliation. */
    boot(&f, km_profile_init_emu);
    km_core_set_load_sensor_at(&f.core, 4, true, true, 0);
    assert(f.core.path_occupied && f.core.path_occupant_lane == 4);
    refused(&f, false, KM_LANE_UNKNOWN, at(0), KM_ERR_STALE);
}

static void test_lane_rotation_scales_active_lane(void)
{
    fixture_t f; setup(&f, true, false);
    f.core.profile.feeder_rotation_distance_um = 22000;
    for (uint8_t lane = 0; lane < KM_MAX_LANES; ++lane)
        f.core.profile.lane_rotation_distance_um[lane] = 22000;
    f.core.profile.lane_rotation_distance_um[1] = 24200;
    f.core.profile.unload_hub_clear_steps = 100;

    km_core_timepoint_t t = {f.ms, f.ms * 1000u};
    assert(km_core_request_load(&f.core, KM_CLIENT_HOST, 1, 1, 1, t) == KM_REQ_ACCEPTED);
    /* 22000/24200 reduces to 10/11. */
    assert(f.core.trajectory.scale_num == 10 && f.core.trajectory.scale_den == 11);

    arrived(&f); event(&f, KM_EVENT_LOAD_COMPLETE); advance(&f, 30);
    t = (km_core_timepoint_t){f.ms, f.ms * 1000u};
    assert(km_core_request_unload(&f.core, KM_CLIENT_HOST, 1, 2, t) == KM_REQ_ACCEPTED);
    advance(&f, 20);
    f.core.filament_present_hub = f.core.filament_present_toolhead = false;
    advance(&f, 1);
    assert(f.core.stage == KM_STAGE_UNLOAD_STOP_SETTLE);
    advance(&f, 100);
    assert(f.core.stage == KM_STAGE_UNLOAD_REWIND_TO_SPOOL);
    /* 100 reference clearance steps at 10/11 physical. */
    int64_t remaining = f.core.trajectory.model_position_steps
                        - f.core.trajectory.target_position_steps;
    assert(remaining > 0 && remaining <= 91);

    advance(&f, 1000);
    event(&f, KM_EVENT_UNLOAD_COMPLETE);

    t = (km_core_timepoint_t){f.ms, f.ms * 1000u};
    assert(km_core_request_load(&f.core, KM_CLIENT_HOST, 1, 3, 0, t) == KM_REQ_ACCEPTED);
    advance(&f, 1);
    assert(f.core.trajectory.scale_num == 1 && f.core.trajectory.scale_den == 1);
}

static void test_lane_rotation_profile_bounds(void)
{
    km_device_profile_t p; km_profile_init_boxturtle(&p);
    p.feeder_rotation_distance_um = 22000;
    for (uint8_t lane = 0; lane < KM_MAX_LANES; ++lane)
        p.lane_rotation_distance_um[lane] = 22000;
    assert(km_profile_validate(&p) == KM_OK);

    p.lane_rotation_distance_um[2] = 17600; /* 0.8x */
    assert(km_profile_validate(&p) == KM_OK);
    p.lane_rotation_distance_um[2] = 17599;
    assert(km_profile_validate(&p) == KM_ERR_INVALID_PARAM);
    p.lane_rotation_distance_um[2] = 27500; /* 1.25x */
    assert(km_profile_validate(&p) == KM_OK);
    p.lane_rotation_distance_um[2] = 27501;
    assert(km_profile_validate(&p) == KM_ERR_INVALID_PARAM);

    p.lane_rotation_distance_um[2] = 0;
    p.feeder_rotation_distance_um = 0;
    p.lane_rotation_distance_um[0] = 22000;
    assert(km_profile_validate(&p) == KM_ERR_INVALID_PARAM);
}

int main(void)
{
    test_reconcile_adopt();
    test_reconcile_after_fault();
    test_reconcile_clear();
    test_reconcile_virtual_hub();
    test_eject_lifecycle();
    test_eject_rejections_and_faults();
    test_eject_cancel_and_bounce();
    test_eject_without_bay_moves_the_set_distance();
    test_eject_without_bay_or_distance_is_unavailable();
    test_eject_distance_profile_rules();
    test_preload_new_host_session();
    test_emu_virtual_hub();
    test_emu_multilane_projection();
    test_cancel_preload_during_load_arrival();
    test_identity_and_cancel();
    test_load_follow_unload(true); test_load_follow_unload(false);
    test_unload_clearance_reasserted_while_braking(true);
    test_unload_clearance_reasserted_while_braking(false);
    test_unload_hub_clearance_success_and_reassertion();
    test_unload_hub_clearance_cancel_and_timeout();
    test_unload_clearance_keeps_current_regulation();
    test_unload_cancel_and_ownership(); test_fault_queue_and_reconnect();
    test_full_queue_idle_fault(); test_sensor_driver_and_watchdog();
    test_lane_binding_and_calibration();
    test_lane_driver_velocity_admission();
    test_presence_batch();
    test_idle_edges();
    test_sensor_snapshot();
    test_preload();
    test_lane_load_switch();
    test_lane_rotation_scales_active_lane();
    test_lane_rotation_profile_bounds();
    puts("Operation lifecycle, identity, queue, recovery, routing and calibration tests passed.");
    return 0;
}
