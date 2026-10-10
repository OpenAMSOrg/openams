// SPDX-License-Identifier: GPL-3.0-or-later
// Adapted from review fixtures in iterations16/17; public API and timed outputs.
#include "klipper_mmu/operation_core.h"
#include "mock_driver.h"
#include "fake_clock.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

typedef struct {
    km_operation_core_t core;
    mock_driver_t feeder, rewinder;
    km_fake_clock_t clock;
    uint64_t total;
    bool hub, toolhead, demand;
} fixture_t;

static km_core_timepoint_t point(uint64_t total)
{
    km_core_timepoint_t t = {(uint32_t)(total / 1000u), (uint32_t)total};
    return t;
}

static void samples(fixture_t *f)
{
    uint32_t ms = point(f->total).ms;
    km_core_set_sensors_at(&f->core, 0, true, f->hub, f->toolhead, true, ms);
    km_core_set_encoder_at(&f->core, 0, true, ms);
    assert(km_buffer_update_switches(&f->core.buffer_sensor, f->demand, false, ms) == KM_OK);
}

static void setup(fixture_t *f, uint64_t start, bool powered, bool encoder)
{
    memset(f, 0, sizeof(*f));
    km_device_profile_t p;
    km_profile_init_boxturtle(&p);
    p.has_powered_rewind = powered;
    if (!powered) p.rewind_pwm_duty_permille = 0;
    p.has_encoder = encoder;
    if (encoder) p.encoder_detection_length_steps = 1000; /* km_profile_validate requires > 0. */
    assert(km_core_init(&f->core, &p) == KM_OK);
    mock_driver_init(&f->feeder, 2, 10, false);
    mock_driver_init(&f->rewinder, 2, 10, false);
    assert(km_core_bind_lane_driver(&f->core, 0, &f->feeder.base,
                                   powered ? &f->rewinder.base : NULL) == KM_OK);
    f->total = start;
    km_fake_clock_init(&f->clock, point(start).us);
    km_core_host_comm_ready(&f->core, point(start).ms);
    samples(f);
    km_core_update(&f->core, point(start).ms, point(start).us);
    assert(f->core.op_state != KM_OP_STATE_FAULT);
}

static void advance(fixture_t *f, uint32_t duration)
{
    uint64_t end = f->total + duration;
    while (f->total < end) {
        uint64_t next = f->total + 1000u;
        if (next > end) next = end;
        assert(km_fake_clock_run_trajectory(&f->clock, &f->core.trajectory, point(next).us) == KM_OK);
        f->total = next;
        samples(f);
        km_core_host_comm_ready(&f->core, point(next).ms);
        km_core_update(&f->core, point(next).ms, point(next).us);
        assert(f->core.op_state != KM_OP_STATE_FAULT);
    }
}

static void load(fixture_t *f)
{
    assert(km_core_request_load(&f->core, KM_CLIENT_HOST, 77, 1001, 0, point(f->total)) == KM_REQ_ACCEPTED);
    assert(f->core.trajectory.last_update_us == point(f->total).us);
}

static km_event_t pop(fixture_t *f, km_event_type_t type)
{
    km_event_t e;
    assert(km_event_queue_pop(&f->core.event_queue, &e) == KM_OK);
    assert(e.type == type && e.client_id == KM_CLIENT_HOST && e.session_id == 77);
    return e;
}

static void finish_cancel(fixture_t *f, km_event_type_t type, uint32_t req)
{
    assert(f->core.shared_path_owned && f->core.stage == KM_STAGE_BRAKING);
    assert(km_event_queue_count(&f->core.event_queue) == 0);
    for (unsigned i = 0; i < 300 && f->core.shared_path_owned; ++i) advance(f, 1000);
    assert(!f->core.shared_path_owned && f->core.stage == KM_STAGE_STOPPED);
    assert(!f->feeder.base.enabled && !f->rewinder.base.enabled);
    assert(!km_trajectory_is_moving(&f->core.trajectory));
    km_event_t e = pop(f, type);
    assert(e.req_id == req && e.timestamp_ms == point(f->total).ms);
    assert(!f->core.event_reservations && km_event_queue_count(&f->core.event_queue) == 0);
    assert(f->feeder.rising_edge_count == f->feeder.falling_edge_count);
    assert(!f->feeder.pulse_width_violations && !f->feeder.step_interval_violations);
}

static void cancel_matrix(void)
{
    const uint64_t bases[] = {0, 499, 999, UINT32_MAX - UINT64_C(10000),
                              UINT64_C(1000) * UINT32_MAX - 10000u};
    unsigned count = 0;
    for (unsigned b = 0; b < sizeof(bases) / sizeof(bases[0]); ++b) {
        for (uint32_t fractional = 0; fractional < 1000; fractional += 37) {
            fixture_t f; setup(&f, bases[b], false, false); load(&f);
            advance(&f, 10000u + fractional);
            assert(km_core_request_cancel5(&f.core, KM_CLIENT_HOST, 77, 1001,
                                          point(f.total)) == KM_REQ_ACCEPTED);
            assert(f.core.trajectory.last_update_us == point(f.total).us);
            finish_cancel(&f, KM_EVENT_LOAD_CANCELLED, 1001);
            ++count;
        }
    }
    assert(count == 140);
    puts("140 cancellation timing/epoch combinations passed");
}

static void starts(void)
{
    const uint64_t bases[] = {511, UINT64_C(1) << 32,
                              (UINT64_C(1) << 32) * 1000u + 777u};
    for (unsigned i = 0; i < sizeof(bases) / sizeof(bases[0]); ++i) {
        fixture_t f; setup(&f, bases[i], false, true);
        assert(km_core_request_ptfe_cal(&f.core, KM_CLIENT_HOST, 77, 1001, 0,
                                       point(f.total)) == KM_REQ_ACCEPTED);
        assert(f.core.trajectory.last_update_us == point(f.total).us);
        advance(&f, 10500);
        assert(km_core_request_cancel5(&f.core, KM_CLIENT_HOST, 77, 1001,
                                      point(f.total)) == KM_REQ_ACCEPTED);
        finish_cancel(&f, KM_EVENT_PTFE_CAL_CANCELLED, 1001);
    }
}

static void immediate_rewind(void)
{
    for (unsigned powered = 0; powered < 2; ++powered) {
        fixture_t f; setup(&f, 511, powered != 0, false);
        f.hub = true; f.toolhead = true; samples(&f);
        assert(km_core_request_unload(&f.core, KM_CLIENT_HOST, 77, 1001,
                                     point(f.total)) == KM_REQ_ACCEPTED);
        assert(f.core.trajectory.last_update_us == point(f.total).us);
        assert(f.rewinder.base.enabled == (powered != 0));
        if (powered) assert(f.rewinder.last_dir_change_us == point(f.total).us);
        advance(&f, 10500);
        assert(km_core_request_cancel5(&f.core, KM_CLIENT_HOST, 77, 1001,
                                      point(f.total)) == KM_REQ_ACCEPTED);
        finish_cancel(&f, KM_EVENT_UNLOAD_CANCELLED, 1001);
        assert(f.core.path_occupied);
    }
}

static void follow_fixture(fixture_t *f)
{
    setup(f, 511, true, false);
    f->hub = true; f->toolhead = true; f->demand = true; samples(f);
    assert(km_core_request_follow(&f->core, KM_CLIENT_HOST, 77, 1001, true,
                                 KM_DIR_FORWARD, point(f->total)) == KM_REQ_ACCEPTED);
    advance(f, 10500);
    assert(km_trajectory_get_velocity(&f->core.trajectory) > 0);
}

static void delayed_rewind(void)
{
    fixture_t f; follow_fixture(&f);
    assert(km_core_request_unload(&f.core, KM_CLIENT_HOST, 77, 1002,
                                 point(f.total)) == KM_REQ_ACCEPTED);
    km_event_t prior = pop(&f, KM_EVENT_STOPPED);
    assert(prior.req_id == 1001);
    assert(f.core.rewind_pending && !f.rewinder.base.enabled);
    for (unsigned i = 0; i < 100 && !f.rewinder.base.enabled; ++i) advance(&f, 333);
    assert(f.rewinder.base.enabled && !f.core.rewind_pending);
    assert(f.rewinder.last_dir_change_us == point(f.total).us);
    assert(point(f.total).us % 1000u != 0);
    assert(km_core_request_cancel5(&f.core, KM_CLIENT_HOST, 77, 1002,
                                  point(f.total)) == KM_REQ_ACCEPTED);
    finish_cancel(&f, KM_EVENT_UNLOAD_CANCELLED, 1002);
}

static void follow_disable(void)
{
    fixture_t f; follow_fixture(&f);
    assert(km_core_request_follow(&f.core, KM_CLIENT_HOST, 77, 1001, false,
                                 KM_DIR_FORWARD, point(f.total)) == KM_REQ_ACCEPTED);
    assert(f.core.trajectory.last_update_us == point(f.total).us);
    finish_cancel(&f, KM_EVENT_STOPPED, 1001);
}

static void invalid_request(void)
{
    fixture_t f; setup(&f, 10511, true, true);
    km_core_timepoint_t bad = point(f.total); ++bad.ms;
    km_operation_core_t before = f.core;
    assert(km_core_request_load(&f.core, KM_CLIENT_HOST, 77, 1001, 0, bad) == KM_REQ_REJECTED_STALE);
    assert(memcmp(&before, &f.core, sizeof(before)) == 0);
    assert(km_core_request_ptfe_cal(&f.core, KM_CLIENT_HOST, 77, 1001, 0, bad) == KM_REQ_REJECTED_STALE);
    assert(memcmp(&before, &f.core, sizeof(before)) == 0);
    assert(!f.feeder.base.enabled && !f.rewinder.base.enabled);
    load(&f); advance(&f, 10500); before = f.core;
    assert(km_core_request_cancel5(&f.core, KM_CLIENT_HOST, 77, 1001,
                                  point(f.total - 1u)) == KM_REQ_REJECTED_STALE);
    assert(memcmp(&before, &f.core, sizeof(before)) == 0);
    assert(f.feeder.base.enabled && !f.core.trajectory.faulted);
}

static void timer_ahead(void)
{
    fixture_t f; setup(&f, 511, false, false); load(&f); advance(&f, 10000);
    km_core_timepoint_t old = point(f.total);
    /* The motion service runs at least every KM_RATE_MAX_UPDATE_US and owns
     * the rate state's time: one such interval puts it past `old`. */
    f.total += KM_RATE_MAX_UPDATE_US;
    assert(km_fake_clock_run_trajectory(&f.clock, &f.core.trajectory, point(f.total).us) == KM_OK);
    km_operation_core_t before = f.core;
    assert(km_core_request_cancel5(&f.core, KM_CLIENT_HOST, 77, 1001, old) == KM_REQ_REJECTED_STALE);
    assert(memcmp(&before, &f.core, sizeof(before)) == 0);
    assert(km_core_request_cancel5(&f.core, KM_CLIENT_HOST, 77, 1001,
                                  point(f.total)) == KM_REQ_ACCEPTED);
    finish_cancel(&f, KM_EVENT_LOAD_CANCELLED, 1001);
}

static void invalid_update(void)
{
    for (unsigned bad_kind = 0; bad_kind < 2; ++bad_kind) {
        fixture_t f; setup(&f, 511, true, false); load(&f); advance(&f, 10500);
        uint32_t edges = f.feeder.rising_edge_count;
        km_core_timepoint_t bad = point(f.total);
        if (bad_kind) bad = point(f.total - 1u); else ++bad.ms;
        km_core_update(&f.core, bad.ms, bad.us);
        assert(f.core.op_state == KM_OP_STATE_FAULT && f.core.trajectory.faulted);
        assert(!f.feeder.base.enabled && !f.rewinder.base.enabled);
        assert(f.feeder.rising_edge_count == edges);
    }
}

static void long_idle(void)
{
    fixture_t f; setup(&f, 511, false, false);
    assert(km_core_recover(&f.core, 321, false, 0, point(f.total)) == KM_OK);
    for (unsigned i = 0; i < 5; ++i) {
        f.total += UINT64_C(1) << 30;
        km_core_update(&f.core, point(f.total).ms, point(f.total).us);
        assert(f.core.op_state != KM_OP_STATE_FAULT);
        assert(f.core.trajectory.last_update_us == point(f.total).us);
        assert(km_trajectory_get_position(&f.core.trajectory) == 321);
        assert(!f.feeder.base.enabled && !f.feeder.rising_edge_count);
    }
    km_fake_clock_init(&f.clock, point(f.total).us);
    km_core_host_comm_ready(&f.core, point(f.total).ms); samples(&f); load(&f);
    advance(&f, 10500);
    assert(km_core_request_cancel5(&f.core, KM_CLIENT_HOST, 77, 1001,
                                  point(f.total)) == KM_REQ_ACCEPTED);
    finish_cancel(&f, KM_EVENT_LOAD_CANCELLED, 1001);
}

static void coarse_epoch(void)
{
    fixture_t f; setup(&f, (UINT64_C(1) << 32) + 10500u, false, true);
    km_core_set_sensors(&f.core, 0, true, false, false);
    km_core_set_encoder(&f.core, 123, true);
    assert(f.core.path_sample_ms == point(f.total).ms);
    
    assert(f.core.encoder_sample_ms == point(f.total).ms);
    f.total += 1500u;
    samples(&f); km_core_host_comm_ready(&f.core, point(f.total).ms); load(&f);
    
    km_core_set_encoder(&f.core, 456, true);
    
    
    assert(f.core.encoder_sample_ms == point(f.total).ms);
    km_core_set_motor_ready(&f.core, false);
    km_event_t e = pop(&f, KM_EVENT_LOAD_FAILED);
    assert(e.req_id == 1001 && e.timestamp_ms == point(f.total).ms);
    assert(!f.feeder.base.enabled);
}

static void recovery(void)
{
    fixture_t f; setup(&f, 511, false, false); load(&f); advance(&f, 10500);
    km_core_explicit_stop(&f.core, KM_FAULT_EMERGENCY_STOP, point(f.total).ms);
    (void)pop(&f, KM_EVENT_LOAD_FAILED);
    km_core_timepoint_t bad = point(f.total); ++bad.ms;
    km_operation_core_t before = f.core;
    assert(km_core_recover(&f.core, 321, false, 0, bad) == KM_ERR_INVALID_PARAM);
    assert(memcmp(&before, &f.core, sizeof(before)) == 0);
    f.total += 1u;
    assert(km_core_recover(&f.core, 321, false, 0, point(f.total)) == KM_OK);
    assert(!f.feeder.base.enabled && !f.rewinder.base.enabled && !f.core.shared_path_owned);
    assert(!f.core.trajectory.faulted && !f.core.trajectory.position_uncertain);
    assert(km_trajectory_get_position(&f.core.trajectory) == 321);
    assert(f.core.trajectory.last_update_us == point(f.total).us);
}

static void wrap_ages(void)
{
    const uint64_t bases[] = {UINT32_MAX - UINT64_C(10000),
                              UINT64_C(1000) * UINT32_MAX - 10000u};
    for (unsigned i = 0; i < 2; ++i) {
        for (unsigned lease = 0; lease < 2; ++lease) {
            fixture_t f; setup(&f, bases[i], false, false); load(&f);
            f.total += lease ? 2000000u : 201000u;
            assert(km_fake_clock_run_trajectory(&f.clock, &f.core.trajectory, point(f.total).us) == KM_OK);
            km_core_update(&f.core, point(f.total).ms, point(f.total).us);
            assert(f.core.motion_fault == (lease ? KM_FAULT_HOST_COMM_LOST : KM_FAULT_SENSOR_FAILURE));
            assert(!f.feeder.base.enabled);
            km_event_t e = pop(&f, KM_EVENT_LOAD_FAILED);
            assert(e.timestamp_ms == point(f.total).ms);
        }
    }
}

static void horizon(void)
{
    const uint64_t gaps[] = {UINT64_C(1) << 31, UINT64_C(1) << 32,
                              (UINT64_C(1) << 32) + 1u};
    for (unsigned i = 0; i < sizeof(gaps) / sizeof(gaps[0]); ++i) {
        fixture_t f; setup(&f, 511, false, false);
        km_operation_core_t before = f.core;
        assert(km_core_request_load(&f.core, KM_CLIENT_HOST, 77, 1001, 0,
                                    point(f.total + gaps[i])) == KM_REQ_REJECTED_STALE);
        assert(memcmp(&before, &f.core, sizeof(before)) == 0);
        assert(!f.feeder.base.enabled);
    }
    fixture_t f; setup(&f, (UINT64_C(1) << 32) + 511u, false, false);
    assert(km_core_request_load(&f.core, KM_CLIENT_HOST, 77, 1001, 0,
                                point(511)) == KM_REQ_REJECTED_STALE);
    assert(!f.feeder.base.enabled);
}

static void null_arguments(void)
{
    km_core_timepoint_t t = point(511);
    assert(km_core_request_load(NULL, KM_CLIENT_HOST, 77, 1001, 0, t) != KM_REQ_ACCEPTED);
    assert(km_core_request_ptfe_cal(NULL, KM_CLIENT_HOST, 77, 1001, 0, t) != KM_REQ_ACCEPTED);
    assert(km_core_request_unload(NULL, KM_CLIENT_HOST, 77, 1001, t) != KM_REQ_ACCEPTED);
    assert(km_core_request_follow(NULL, KM_CLIENT_HOST, 77, 1001, true, KM_DIR_FORWARD, t) != KM_REQ_ACCEPTED);
    assert(km_core_request_follow(NULL, KM_CLIENT_HOST, 77, 1001, false, KM_DIR_FORWARD, t) != KM_REQ_ACCEPTED);
    assert(km_core_request_cancel5(NULL, KM_CLIENT_HOST, 77, 1001, t) != KM_REQ_ACCEPTED);
    assert(km_core_request_cancel4(NULL, KM_CLIENT_HOST, 1001, t) == KM_REQ_REJECTED_STALE);
    assert(km_core_recover(NULL, 0, false, 0, t) == KM_ERR_INVALID_PARAM);
    km_core_update(NULL, t.ms, t.us);
}

static void first_high_sample(void)
{
    fixture_t f;
    setup(&f, (UINT64_C(1) << 31) + 511u, false, false);
    load(&f);
    assert(!f.core.trajectory.faulted);
}

static void follow_clock(void)
{
    fixture_t f; setup(&f, 511, true, false);
    f.hub = true; f.toolhead = true; f.demand = true;
    f.total = 15511; samples(&f);
    assert(km_core_request_follow(&f.core, KM_CLIENT_HOST, 77, 1001, true,
                                 KM_DIR_FORWARD, point(f.total)) == KM_REQ_ACCEPTED);
    km_operation_core_t before = f.core;
    km_admission_result_t result = km_core_request_follow(&f.core, KM_CLIENT_HOST, 77, 1001,
                                   false, KM_DIR_FORWARD, point(f.total - 1u));
    assert(result == KM_REQ_REJECTED_STALE);
    assert(memcmp(&before, &f.core, sizeof(before)) == 0);
}

static void update_after_timer(void)
{
    fixture_t f; setup(&f, 511, false, false); load(&f); advance(&f, 10000);
    /* The motion service runs at least every KM_RATE_MAX_UPDATE_US and owns
     * the rate state's time; an update just before its last service is stale. */
    f.total += KM_RATE_MAX_UPDATE_US;
    assert(km_fake_clock_run_trajectory(&f.clock, &f.core.trajectory, point(f.total).us) == KM_OK);
    uint32_t edges = f.feeder.rising_edge_count;
    km_core_timepoint_t old = point(f.core.trajectory.last_update_us - 1u);
    km_core_update(&f.core, old.ms, old.us);
    assert(f.core.op_state == KM_OP_STATE_FAULT && !f.feeder.base.enabled);
    assert(edges == f.feeder.rising_edge_count);
}

static void update_after_recover(void)
{
    fixture_t f; setup(&f, 511, false, false);
    f.total = 15511;
    assert(km_core_recover(&f.core, 321, false, 0, point(f.total)) == KM_OK);
    km_core_timepoint_t old = point(f.total - 1u);
    km_core_update(&f.core, old.ms, old.us);
    assert(f.core.op_state == KM_OP_STATE_FAULT && !f.feeder.base.enabled);
}

static void recover_after_fault_idle(void)
{
    fixture_t f; setup(&f, 511, false, false); load(&f); advance(&f, 10000);
    km_core_explicit_stop(&f.core, KM_FAULT_EMERGENCY_STOP, point(f.total).ms);
    (void)pop(&f, KM_EVENT_LOAD_FAILED);
    for (unsigned i = 0; i < 3; ++i) {
        f.total += UINT64_C(1) << 30;
        km_core_update(&f.core, point(f.total).ms, point(f.total).us);
        assert(f.core.op_state == KM_OP_STATE_FAULT && !f.feeder.base.enabled);
    }
    km_core_host_comm_ready(&f.core, point(f.total).ms); samples(&f);
    km_result_t result = km_core_recover(&f.core, 321, false, 0, point(f.total));
    assert(result == KM_OK);
    assert(!f.feeder.base.enabled && !f.core.shared_path_owned);
    assert(!f.core.trajectory.position_uncertain && !f.core.trajectory.faulted);
    assert(km_trajectory_get_position(&f.core.trajectory) == 321);
}

static void full_wrap_admission(void)
{
    fixture_t f; setup(&f, 511, false, false);
    f.total += UINT64_C(1) << 32;
    samples(&f); km_core_host_comm_ready(&f.core, point(f.total).ms);
    km_operation_core_t before = f.core;
    km_admission_result_t result = km_core_request_load(&f.core, KM_CLIENT_HOST, 77, 1001, 0, point(f.total));
    assert(result == KM_REQ_REJECTED_STALE && !f.feeder.base.enabled);
    assert(memcmp(&before, &f.core, sizeof(before)) == 0);
}

static void coarse_rewind(void)
{
    fixture_t f; setup(&f, (UINT64_C(1) << 32) + 511u, false, false);
    km_core_timepoint_t old = point(511);
    km_core_update(&f.core, old.ms, old.us);
    assert(f.core.op_state == KM_OP_STATE_FAULT && !f.feeder.base.enabled);
}

static void fresh_without_update(fixture_t *f, uint64_t total)
{
    memset(f, 0, sizeof(*f));
    km_device_profile_t p;
    km_profile_init_boxturtle(&p);
    p.has_powered_rewind = false; p.rewind_pwm_duty_permille = 0; p.has_encoder = false;
    assert(km_core_init(&f->core, &p) == KM_OK);
    mock_driver_init(&f->feeder, 2, 10, false);
    assert(km_core_bind_lane_driver(&f->core, 0, &f->feeder.base, NULL) == KM_OK);
    f->total = total;
    km_fake_clock_init(&f->clock, point(total).us);
    km_core_host_comm_ready(&f->core, point(total).ms);
    samples(f);
}

static void first_follow(void)
{
    fixture_t f; fresh_without_update(&f, (UINT64_C(1) << 31) + 15511u);
    f.hub = true; f.toolhead = true; f.demand = true; samples(&f);
    assert(km_core_request_follow(&f.core, KM_CLIENT_HOST, 77, 1001, true,
                                 KM_DIR_FORWARD, point(f.total)) == KM_REQ_ACCEPTED);
    km_operation_core_t before = f.core;
    km_admission_result_t result = km_core_request_cancel5(&f.core, KM_CLIENT_HOST, 77, 1001,
                                                          point(f.total - 1u));
    printf("first follow without periodic update; older cancel=%u\n", (unsigned)result);
    assert(result == KM_REQ_REJECTED_STALE);
    assert(memcmp(&before, &f.core, sizeof(before)) == 0);
    assert(km_core_request_cancel5(&f.core, KM_CLIENT_HOST, 77, 1001,
                                  point(f.total)) == KM_REQ_ACCEPTED);
    finish_cancel(&f, KM_EVENT_STOPPED, 1001);
}

static void initial_fault_clock(void)
{
    fixture_t f; fresh_without_update(&f, 15511);
    km_core_explicit_stop(&f.core, KM_FAULT_EMERGENCY_STOP, point(f.total).ms);
    km_core_update(&f.core, point(f.total).ms, point(f.total).us);
    km_core_timepoint_t old = point(f.total - 1u);
    km_core_update(&f.core, old.ms, old.us);
    printf("first clock observed in fault; retained us=%u expected=%u\n",
           f.core.last_control_us, point(f.total).us);
    assert(f.core.last_control_ms == point(f.total).ms && f.core.last_control_us == point(f.total).us);
    assert(f.core.op_state == KM_OP_STATE_FAULT && !f.feeder.base.enabled);
    assert(km_core_recover(&f.core, 321, false, 0, old) == KM_ERR_INVALID_PARAM);
    assert(km_core_recover(&f.core, 321, false, 0, point(f.total)) == KM_OK);
    assert(!f.feeder.base.enabled && !f.core.trajectory.position_uncertain);
}

static void rejected_recovery(void)
{
    fixture_t f; setup(&f, 511, false, false);
    km_operation_core_t before = f.core;
    assert(km_core_recover(&f.core, INT64_MAX, false, 0, point(15511)) == KM_ERR_INVALID_PARAM);
    printf("invalid position rejected; retained us=%u expected=%u\n",
           f.core.last_control_us, before.last_control_us);
    assert(memcmp(&before, &f.core, sizeof(before)) == 0);
    f.total = 1511; samples(&f); load(&f);
}

int main(void)
{
    cancel_matrix();
    starts();
    immediate_rewind();
    delayed_rewind();
    follow_disable();
    invalid_request();
    timer_ahead();
    invalid_update();
    long_idle();
    coarse_epoch();
    recovery();
    wrap_ages();
    horizon();
    null_arguments();
    first_high_sample();
    follow_clock();
    update_after_timer();
    update_after_recover();
    recover_after_fault_idle();
    full_wrap_admission();
    coarse_rewind();
    first_follow();
    initial_fault_clock();
    rejected_recovery();
    puts("test_operation_time passed.");
    return 0;
}
