// SPDX-License-Identifier: GPL-3.0-or-later
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "klipper_mmu/operation_core.h"
#include "klipper_mmu/protocol_wire.h"
#include "mock_driver.h"
#include "fake_clock.h"

/* Synthetic calibration and thresholds exercise the shared control path only;
 * they are not measured BoxTurtle, FPS or printer values. */
typedef struct {
    km_operation_core_t core;
    mock_driver_t feeder[KM_MAX_LANES], rewinder[KM_MAX_LANES];
    km_fake_clock_t clock;
    uint32_t ms;
    int32_t buffer;          /* Position encoded into raw DiffA/DiffB counts. */
    uint32_t adc_period;     /* 0 withholds new acquisitions. */
    bool adc_repeat;         /* Republish the previous acquisition record. */
    bool adc_off_path;
    bool adc_invalid;
    uint32_t adc_attempt_us;
} fixture_t;

static void fps_profile(km_device_profile_t *p, bool toolhead)
{
    km_profile_init_boxturtle(p);
    p->has_toolhead_sensor = toolhead;
    p->has_proportional_buffer = true;
    p->proportional_gain_milli = 8000;
    p->buffer_deadband_permille = 10;
    p->has_diff_calibration = true;
    p->diff_calibration = (km_diff_adc_config_t){
        .channel_map = KM_DIFF_CH_A_LOW_B_HIGH, .vref_mv = 3300, .adc_max_counts = 4095,
        .path_tolerance_counts = 50, .point_count = 3,
        .points = {{0, 2048, 0}, {2048, 2048, 500}, {2048, 4095, 1000}}};
    p->fps_arrival = true;
    p->fps_arrival_permille = 700;
    p->fps_arrival_samples = 3;
    p->fps_arrival_hold_ms = 20;
    p->fps_arrival_min_steps = 200;
    p->fps_arrival_max_steps = 3400;
    /* A restored HUB-to-arrival distance of 1600 steps and a 1400-step
     * window leave LOAD's arrival window, and so every arrival expectation
     * below, at the same 200..3000 the outer limits used to give. */
    p->fps_calibrated_steps = 1600;
    p->load_window_steps = 1400;
    p->load_cruise_velocity_steps_s = 8000; /* Above the feed: a real ramp. */
    p->load_crawl_velocity_steps_s = 2000;
    p->buffer_travel_steps = 15000;
}

/* Publish through the production ADC-pair record and calibrated decoder. */
static void publish_buffer(fixture_t *f)
{
    km_buffer_sensor_t *b = &f->core.buffer_sensor;
    if (b->type == KM_BUFFER_SENSOR_SWITCHED) {
        assert(km_buffer_update_switches(b, false, false, f->ms) == KM_OK);
        return;
    }
    if (!f->adc_repeat) f->adc_attempt_us = f->ms * 1000u;
    int32_t pos = f->buffer;
    km_adc_pair_sample_t s = {0};
    s.is_valid = !f->adc_invalid; s.result = KM_OK; s.present_mask = 3u;
    s.raw_a = (uint16_t)(pos >= 500 ? 2048 : pos * 2048 / 500);
    s.raw_b = (uint16_t)(pos >= 500 ? 2048 + (pos - 500) * 2047 / 500 : 2048);
    if (f->adc_off_path) { s.raw_a = 0; s.raw_b = 4095; }
    s.pair_start_us = s.a_attempt_us = s.a_complete_us = f->adc_attempt_us;
    s.b_attempt_us = s.b_complete_us = f->adc_attempt_us;
    (void)km_buffer_update_adc_pair(b, &s, f->ms, f->ms * 1000u);
}

static void advance(fixture_t *f, uint32_t duration)
{
    km_operation_core_t *c = &f->core;
    for (uint32_t i = 0; i < duration; ++i) {
        ++f->ms;
        assert(km_fake_clock_run_trajectory(&f->clock, &c->trajectory, f->ms * 1000u) == KM_OK);
        km_core_set_sensors_at(c, 0, true, c->filament_present_hub,
                               c->filament_present_toolhead, true, f->ms);
        if (f->adc_period && f->ms % f->adc_period == 0) publish_buffer(f);
        if (c->host_connected) km_core_host_comm_ready(c, f->ms); /* Host heartbeat. */
        km_core_update(c, f->ms, f->ms * 1000u);
    }
}

static void setup_profile(fixture_t *f, const km_device_profile_t *p)
{
    memset(f, 0, sizeof(*f));
    assert(km_core_init(&f->core, p) == KM_OK);
    for (uint8_t i = 0; i < p->lane_count; ++i) {
        mock_driver_init(&f->feeder[i], 2, 10, false);
        mock_driver_init(&f->rewinder[i], 2, 10, false);
        assert(km_core_bind_lane_driver(&f->core, i, &f->feeder[i].base,
                                       &f->rewinder[i].base) == KM_OK);
    }
    km_core_host_comm_ready(&f->core, 0);
    km_fake_clock_init(&f->clock, 0);
    f->buffer = KM_PERMILLE_MIN; /* The Hall buffer rests uncompressed. */
    f->adc_period = 5;
    advance(f, 5);
}

static void setup(fixture_t *f, bool toolhead)
{
    km_device_profile_t p; fps_profile(&p, toolhead); setup_profile(f, &p);
}

static km_admission_result_t load(fixture_t *f, uint32_t session, uint32_t req)
{
    km_core_timepoint_t t = {f->ms, f->ms * 1000u};
    return km_core_request_load(&f->core, KM_CLIENT_HOST, session, req, 0, t);
}

static km_admission_result_t calibrate(fixture_t *f, uint32_t session, uint32_t req)
{
    km_core_timepoint_t t = {f->ms, f->ms * 1000u};
    return km_core_request_ptfe_cal(&f->core, KM_CLIENT_HOST, session, req, 0, t);
}

static km_event_t event(fixture_t *f, km_event_type_t type, uint32_t value)
{
    km_event_t e;
    assert(km_event_queue_pop(&f->core.event_queue, &e) == KM_OK);
    assert(e.type == type && e.value == value);
    return e;
}

static void failed(fixture_t *f, km_fault_t reason)
{
    assert(f->core.op_state == KM_OP_STATE_FAULT && f->core.system_fault == reason);
    assert(!f->feeder[0].base.enabled);
    /* Never LOAD_COMPLETE or PTFE_CAL_COMPLETE. */
    km_event_t e = event(f, f->core.active_op == KM_OP_TYPE_CALIBRATE
                         ? KM_EVENT_PTFE_CAL_FAILED : KM_EVENT_LOAD_FAILED, (uint32_t)reason);
    assert(e.flags == KM_EVENT_FLAG_NONE);
    assert(km_event_queue_count(&f->core.event_queue) == 0);
}

static void settled(fixture_t *f)
{
    for (unsigned i = 0; i < 1000 && f->core.shared_path_owned; ++i) advance(f, 1);
    assert(!f->core.shared_path_owned && f->core.stage == KM_STAGE_STOPPED);
}

static int64_t pos(const fixture_t *f)
{
    return km_trajectory_get_position(&f->core.trajectory);
}

static int64_t travel(const fixture_t *f)
{
    return pos(f) - f->core.fps_hub_steps;
}

static void reach_hub(fixture_t *f)
{
    advance(f, 10);
    assert(f->core.stage == KM_STAGE_LOAD_FEED_TO_HUB);
    f->core.filament_present_hub = true;
    advance(f, 1);
    assert(f->core.stage == KM_STAGE_LOAD_FEED_TO_BUFFER);
    assert(f->core.fps_count == 0 && !f->core.fps_armed);
}

static void travel_to(fixture_t *f, int64_t steps)
{
    for (unsigned i = 0; i < 5000 && travel(f) < steps
         && f->core.stage == KM_STAGE_LOAD_FEED_TO_BUFFER; ++i) advance(f, 1);
    assert(travel(f) >= steps && f->core.stage == KM_STAGE_LOAD_FEED_TO_BUFFER);
}

static void arm(fixture_t *f)
{
    travel_to(f, 200);
    advance(f, 10); /* At least one uncompressed acquisition after minimum travel. */
    assert(f->core.fps_armed && f->core.fps_count == 0);
    assert(f->core.stage == KM_STAGE_LOAD_FEED_TO_BUFFER);
}

/* Hold compression until the operation leaves FEED_TO_BUFFER. */
static uint32_t hold(fixture_t *f)
{
    uint32_t start = f->ms;
    f->buffer = 750;
    for (unsigned i = 0; i < 200 && f->core.stage == KM_STAGE_LOAD_FEED_TO_BUFFER; ++i)
        advance(f, 1);
    return f->ms - start;
}

static void completes(fixture_t *f)
{
    uint32_t elapsed = hold(f);
    /* Five-ms acquisitions: three samples spanning 20 ms follow the press. */
    assert(elapsed >= 21 && elapsed <= 25);
    assert(f->core.stage == KM_STAGE_FOLLOW && f->core.op_state == KM_OP_STATE_FOLLOWING);
    event(f, KM_EVENT_LOAD_COMPLETE, 0);
}

static void test_profile_validation_and_defaults(void)
{
    void (*inits[])(km_device_profile_t *) = {km_profile_init_boxturtle,
        km_profile_init_boxturtle_enhanced, km_profile_init_nightowl, km_profile_init_emu};
    for (unsigned i = 0; i < sizeof(inits) / sizeof(inits[0]); ++i) {
        km_device_profile_t p; inits[i](&p);
        assert(!p.fps_arrival && !p.fps_arrival_permille && !p.fps_arrival_samples
               && !p.fps_arrival_hold_ms && !p.fps_arrival_min_steps && !p.fps_arrival_max_steps
               && !p.fps_calibrated_steps && !p.load_cruise_velocity_steps_s
               && !p.load_crawl_velocity_steps_s && !p.load_window_steps
               && !p.buffer_travel_steps);
    }
    km_device_profile_t p, q; fps_profile(&p, false);
    assert(km_profile_validate(&p) == KM_OK);
    q = p; q.has_proportional_buffer = false; q.has_diff_calibration = false;
    assert(km_profile_validate(&q) == KM_ERR_INVALID_PARAM); /* Needs calibrated analog. */
    q = p; q.has_hub_sensor = false;
    assert(km_profile_validate(&q) == KM_ERR_INVALID_PARAM);
    q = p; q.fps_arrival_permille = 510; /* Inside the follower deadband. */
    assert(km_profile_validate(&q) == KM_ERR_INVALID_PARAM);
    q = p; q.fps_arrival_permille = 1000; /* Clamped calibration end. */
    assert(km_profile_validate(&q) == KM_ERR_INVALID_PARAM);
    q = p; q.fps_arrival_samples = 1;
    assert(km_profile_validate(&q) == KM_ERR_INVALID_PARAM);
    q = p; q.fps_arrival_hold_ms = 0;
    assert(km_profile_validate(&q) == KM_ERR_INVALID_PARAM);
    q = p; q.fps_arrival_hold_ms = q.buffer_feed_timeout_ms;
    assert(km_profile_validate(&q) == KM_ERR_INVALID_PARAM);
    q = p; q.fps_arrival_min_steps = 0;
    assert(km_profile_validate(&q) == KM_ERR_INVALID_PARAM);
    q = p; q.fps_arrival_min_steps = q.fps_arrival_max_steps;
    assert(km_profile_validate(&q) == KM_ERR_INVALID_PARAM);
    q = p; q.fps_calibrated_steps = 0; /* Unknown S: admitted, LOAD refused. */
    assert(km_profile_validate(&q) == KM_OK);
    q = p; q.fps_calibrated_steps = q.fps_arrival_min_steps - 1;
    assert(km_profile_validate(&q) == KM_ERR_INVALID_PARAM);
    q = p; q.fps_calibrated_steps = q.fps_arrival_max_steps + 1;
    assert(km_profile_validate(&q) == KM_ERR_INVALID_PARAM);
    q = p; q.load_cruise_velocity_steps_s = 0;
    assert(km_profile_validate(&q) == KM_ERR_INVALID_PARAM);
    q = p; q.load_cruise_velocity_steps_s = KM_MOTION_MAX_VELOCITY_STEPS_S + 1;
    assert(km_profile_validate(&q) == KM_ERR_INVALID_PARAM);
    q = p; q.load_crawl_velocity_steps_s = q.load_cruise_velocity_steps_s;
    assert(km_profile_validate(&q) == KM_ERR_INVALID_PARAM);
    q = p; q.load_window_steps = 0;
    assert(km_profile_validate(&q) == KM_ERR_INVALID_PARAM);
    q = p; q.load_window_steps = (q.fps_arrival_max_steps - q.fps_arrival_min_steps) / 2;
    assert(km_profile_validate(&q) == KM_ERR_INVALID_PARAM);
    q = p; q.buffer_travel_steps = 0;
    assert(km_profile_validate(&q) == KM_ERR_INVALID_PARAM);
    q = p; q.fps_arrival = false; /* Parameters are never silently ignored. */
    assert(km_profile_validate(&q) == KM_ERR_INVALID_PARAM);
    q.fps_arrival_permille = 0; q.fps_arrival_samples = 0; q.fps_arrival_hold_ms = 0;
    q.fps_arrival_min_steps = q.fps_arrival_max_steps = 0;
    q.fps_calibrated_steps = 0; q.load_cruise_velocity_steps_s = 0;
    q.load_crawl_velocity_steps_s = 0; q.load_window_steps = 0; q.buffer_travel_steps = 0;
    assert(km_profile_validate(&q) == KM_OK);

    /* Without a toolhead switch, LOAD is runnable only with FPS arrival. */
    fixture_t f;
    km_profile_init_boxturtle(&p); p.has_toolhead_sensor = false;
    setup_profile(&f, &p);
    assert(load(&f, 1, 1) == KM_REQ_UNAVAILABLE);
    setup(&f, false);
    const int32_t not_neutral[] = {710, 750, 1000}; /* 710 decodes to 709. */
    for (unsigned i = 0; i < 3; ++i) {
        f.buffer = not_neutral[i]; advance(&f, 5);
        assert(load(&f, 1, 1) == KM_REQ_UNAVAILABLE && f.core.stage == KM_STAGE_STOPPED);
    }
    f.buffer = 0; advance(&f, 5); /* Uncompressed rest is admitted. */
    assert(load(&f, 1, 1) == KM_REQ_ACCEPTED);
}

static void test_disabled_keeps_toolhead_arrival(void)
{
    for (int analog = 0; analog < 2; ++analog) {
        km_device_profile_t p; fixture_t f;
        if (analog) {
            fps_profile(&p, true);
            p.fps_arrival = false; p.fps_arrival_permille = 0; p.fps_arrival_samples = 0;
            p.fps_arrival_hold_ms = p.fps_arrival_min_steps = p.fps_arrival_max_steps = 0;
            p.fps_calibrated_steps = 0; p.load_cruise_velocity_steps_s = 0;
            p.load_crawl_velocity_steps_s = 0; p.load_window_steps = 0;
            p.buffer_travel_steps = 0;
        } else km_profile_init_boxturtle(&p);
        setup_profile(&f, &p);
        assert(load(&f, 1, 1) == KM_REQ_ACCEPTED);
        reach_hub(&f);
        f.buffer = 750; advance(&f, 100); /* Pressure alone is not arrival here. */
        assert(f.core.stage == KM_STAGE_LOAD_FEED_TO_BUFFER);
        f.core.filament_present_toolhead = true; advance(&f, 1);
        assert(f.core.stage == KM_STAGE_FOLLOW);
        event(&f, KM_EVENT_LOAD_COMPLETE, 0);
    }
}

static void test_normal_arrival(void)
{
    for (int toolhead = 0; toolhead < 2; ++toolhead) {
        fixture_t f; setup(&f, toolhead);
        assert(load(&f, 1, 1) == KM_REQ_ACCEPTED);
        reach_hub(&f);
        int64_t hub_pulses = km_ref_position(&f.clock.ref);
        if (toolhead) f.core.filament_present_toolhead = true; /* Not sufficient alone. */
        arm(&f);
        completes(&f);
        /* Travel is completed reference steps, never the commanded model. */
        assert(travel(&f) == km_ref_position(&f.clock.ref) - hub_pulses);
        assert(travel(&f) >= 200 && travel(&f) < 3000);
        advance(&f, 20);
        assert(f.core.stage == KM_STAGE_FOLLOW && km_event_queue_count(&f.core.event_queue) == 0);
    }
}

static void test_short_spikes_reset(void)
{
    fixture_t f; setup(&f, false);
    assert(load(&f, 1, 1) == KM_REQ_ACCEPTED);
    reach_hub(&f); arm(&f);
    for (int i = 0; i < 3; ++i) { /* 36 ms of total compression, never sustained. */
        f.buffer = 750; advance(&f, 12);
        assert(f.core.fps_count >= 2 && f.core.stage == KM_STAGE_LOAD_FEED_TO_BUFFER);
        f.buffer = 550; advance(&f, 6);
        assert(f.core.fps_count == 0 && f.core.fps_armed);
    }
    completes(&f);
}

static void test_one_sample_is_not_sustained(void)
{
    for (int repeat = 0; repeat < 2; ++repeat) {
        fixture_t f; setup(&f, false);
        assert(load(&f, 1, 1) == KM_REQ_ACCEPTED);
        reach_hub(&f); arm(&f);
        f.buffer = 750;
        for (unsigned i = 0; i < 10 && !f.core.fps_count; ++i) advance(&f, 1);
        assert(f.core.fps_count == 1);
        if (repeat) f.adc_repeat = true; /* Re-read the same acquisition. */
        else f.adc_period = 0;           /* No newer acquisition at all. */
        advance(&f, 150);
        assert(f.core.stage == KM_STAGE_LOAD_FEED_TO_BUFFER && f.core.fps_count == 1);
        for (unsigned i = 0; i < 100 && f.core.stage == KM_STAGE_LOAD_FEED_TO_BUFFER; ++i)
            advance(&f, 1);
        failed(&f, KM_FAULT_BUFFER_SIGNAL); /* Stale evidence stops feeding. */
    }
}

/* LOAD retries (docs/fps-arrival.md): a synthetic clearance exercises the
 * retract; the factory profiles leave it 0. */
#define CLEAR 100

static void retry_profile(km_device_profile_t *p)
{
    fps_profile(p, false);
    p->unload_hub_clear_steps = CLEAR;
    p->hub_feed_timeout_ms = 300;
}

static uint8_t attempts(const fixture_t *f, uint32_t req, km_load_attempt_t *out)
{
    km_request_identity_t r = {KM_CLIENT_HOST, 1, req, KM_OP_TYPE_LOAD, 0};
    return km_core_get_load_attempts(&f->core, &r, out);
}

/* A retryable attempt ended without an event: check its record, then run the
 * retract. It stops, backs off with the powered rewind, to CLEAR behind the
 * HUB observation after HUB, else by CLEAR but not behind the feed start,
 * then feeds again at `speed`. */
static void retracts(fixture_t *f, km_motion_stage_t stage, km_fault_t fault,
                     int64_t at, int32_t speed)
{
    km_operation_core_t *c = &f->core;
    uint8_t n = c->load_attempt_count;
    km_load_attempt_t a[KM_LOAD_ATTEMPTS];
    assert(c->stage == KM_STAGE_LOAD_RETRY_RETRACT && attempts(f, 1, a) == n);
    assert(a[n - 1].stage == stage && a[n - 1].fault == fault && a[n - 1].travel_steps == at);
    assert(!km_event_queue_count(&c->event_queue) && c->event_reservations == 2);
    int64_t start = c->fps_hub_steps, furthest = pos(f);
    bool rewound = false;
    c->filament_present_hub = false; /* The tip backs out of HUB. */
    for (unsigned i = 0; i < 5000 && c->stage == KM_STAGE_LOAD_RETRY_RETRACT; ++i) {
        advance(f, 1);
        if (pos(f) > furthest) furthest = pos(f);
        rewound |= f->rewinder[0].base.enabled;
    }
    int64_t target = stage == KM_STAGE_LOAD_FEED_TO_BUFFER ? start - CLEAR
        : furthest - CLEAR > start ? furthest - CLEAR : start;
    assert(c->stage == KM_STAGE_LOAD_FEED_TO_HUB && pos(f) == target);
    assert(c->fps_hub_steps == target && !c->fps_armed && !c->fps_count);
    assert(rewound == (target < furthest) && !f->rewinder[0].base.enabled);
    assert(c->trajectory.target_velocity_steps_s == speed);
    assert(!km_event_queue_count(&c->event_queue) && c->event_reservations == 2);
}

/* Each ending of one attempt: stage, fault, and whether LOAD tries again. */
enum { NO_HUB, OBSTRUCTED, MAX_TRAVEL, DEADLINE, HUB_LOST, FULL, STALE };
static const struct {
    int cause; km_motion_stage_t stage; km_fault_t fault; bool retry;
} CAUSES[] = {
    {NO_HUB, KM_STAGE_LOAD_FEED_TO_HUB, KM_FAULT_NO_PROGRESS, true},
    {OBSTRUCTED, KM_STAGE_LOAD_FEED_TO_BUFFER, KM_FAULT_JAM, true},
    {MAX_TRAVEL, KM_STAGE_LOAD_FEED_TO_BUFFER, KM_FAULT_NO_PROGRESS, true},
    {DEADLINE, KM_STAGE_LOAD_FEED_TO_BUFFER, KM_FAULT_NO_PROGRESS, true},
    {HUB_LOST, KM_STAGE_LOAD_FEED_TO_BUFFER, KM_FAULT_FILAMENT_LOST, false},
    {FULL, KM_STAGE_LOAD_FEED_TO_BUFFER, KM_FAULT_BUFFER_SIGNAL, false},
    {STALE, KM_STAGE_LOAD_FEED_TO_BUFFER, KM_FAULT_BUFFER_SIGNAL, false},
};

/* Feed one attempt until it ends by `cause`; return the stage's duration. */
static uint32_t fail_attempt(fixture_t *f, int cause)
{
    f->buffer = 0; f->adc_period = 5;
    if (cause != NO_HUB) reach_hub(f);
    if (cause == HUB_LOST || cause == FULL || cause == STALE) arm(f);
    if (cause == OBSTRUCTED) f->buffer = 750;  /* Before minimum travel. */
    if (cause == HUB_LOST) f->core.filament_present_hub = false;
    if (cause == FULL) f->buffer = 1000;
    if (cause == STALE) f->adc_period = 0;
    km_motion_stage_t stage = f->core.stage;
    uint32_t entered = f->core.stage_entered_time_ms;
    for (unsigned i = 0; i < 5000 && f->core.stage == stage; ++i) advance(f, 1);
    if (cause == MAX_TRAVEL) assert(travel(f) >= 3000 && travel(f) < 3010);
    return f->ms - entered;
}

static void test_attempt_causes(void)
{
    for (unsigned k = 0; k < sizeof(CAUSES) / sizeof(CAUSES[0]); ++k) {
        km_device_profile_t p; retry_profile(&p);
        if (CAUSES[k].cause == DEADLINE) {
            p.buffer_feed_timeout_ms = 400; p.fps_arrival_max_steps = 1000000;
            /* Away from both the deadline and the calibrated window: the
             * attempt must end by the clock, not by travel. */
            p.fps_calibrated_steps = p.fps_arrival_max_steps;
        }
        fixture_t f; setup_profile(&f, &p);
        assert(load(&f, 1, 1) == KM_REQ_ACCEPTED);
        km_load_attempt_t a[KM_LOAD_ATTEMPTS];
        for (uint8_t n = 1; n <= KM_LOAD_ATTEMPTS; ++n) {
            uint32_t elapsed = fail_attempt(&f, CAUSES[k].cause);
            /* The last attempt feeds at half speed within twice the deadline. */
            uint32_t slow = n == KM_LOAD_ATTEMPTS;
            if (CAUSES[k].cause == NO_HUB) assert(elapsed == p.hub_feed_timeout_ms << slow);
            if (CAUSES[k].cause == DEADLINE) assert(elapsed == p.buffer_feed_timeout_ms << slow);
            assert(f.core.load_attempt_count == n);
            if (!CAUSES[k].retry || n == KM_LOAD_ATTEMPTS) {
                int64_t at = travel(&f);
                failed(&f, CAUSES[k].fault); /* One terminal, after the last attempt. */
                assert(attempts(&f, 1, a) == n && a[n - 1].stage == CAUSES[k].stage);
                assert(a[n - 1].fault == CAUSES[k].fault && a[n - 1].travel_steps == at);
                assert(attempts(&f, 2, a) == 0); /* Another identity has none. */
                break;
            }
            retracts(&f, CAUSES[k].stage, CAUSES[k].fault, travel(&f),
                     n + 1 == KM_LOAD_ATTEMPTS ? 2000 : 4000);
        }
    }
}

/* Compression sustained before minimum travel, even from before HUB, is an
 * obstruction; after the retract, released arrival completes the same LOAD. */
static void test_obstruction_then_arrival(void)
{
    km_device_profile_t p; retry_profile(&p);
    fixture_t f; setup_profile(&f, &p);
    assert(load(&f, 1, 1) == KM_REQ_ACCEPTED);
    advance(&f, 5);
    f.buffer = 750; advance(&f, 20);
    assert(f.core.stage == KM_STAGE_LOAD_FEED_TO_HUB);
    f.core.filament_present_hub = true;
    for (unsigned i = 0; i < 100 && f.core.stage != KM_STAGE_LOAD_RETRY_RETRACT; ++i)
        advance(&f, 1);
    assert(travel(&f) < 200);
    retracts(&f, KM_STAGE_LOAD_FEED_TO_BUFFER, KM_FAULT_JAM, travel(&f), 4000);
    f.buffer = 0;
    reach_hub(&f); arm(&f);
    int64_t at = travel(&f);
    completes(&f);
    km_load_attempt_t a[KM_LOAD_ATTEMPTS];
    assert(attempts(&f, 1, a) == 2 && a[1].stage == KM_STAGE_LOAD_FEED_TO_BUFFER);
    assert(a[1].fault == KM_FAULT_NONE && a[1].travel_steps >= at
           && a[1].travel_steps == travel(&f));
    assert(f.core.event_reservations == 1); /* The follow stop/fault remains. */
}

/* Success on the last, half-speed attempt; the host reads one terminal. */
static void test_success_on_last_attempt(void)
{
    km_device_profile_t p; retry_profile(&p);
    fixture_t f; setup_profile(&f, &p);
    assert(load(&f, 1, 1) == KM_REQ_ACCEPTED);
    for (uint8_t n = 1; n < KM_LOAD_ATTEMPTS; ++n) {
        fail_attempt(&f, NO_HUB);
        retracts(&f, KM_STAGE_LOAD_FEED_TO_HUB, KM_FAULT_NO_PROGRESS, travel(&f),
                 n == 1 ? 4000 : 2000);
    }
    reach_hub(&f); arm(&f);
    completes(&f);
    assert(km_event_queue_count(&f.core.event_queue) == 0);
    km_load_attempt_t a[KM_LOAD_ATTEMPTS];
    assert(attempts(&f, 1, a) == 3 && a[2].fault == KM_FAULT_NONE);
    /* The records stay for the host until the next LOAD. */
    km_core_timepoint_t t = {f.ms, f.ms * 1000u};
    km_request_identity_t r = {KM_CLIENT_HOST, 1, 1, KM_OP_TYPE_LOAD, 0};
    assert(km_core_request_cancel_target(&f.core, &r, t) == KM_REQ_ACCEPTED);
    settled(&f);
    event(&f, KM_EVENT_STOPPED, 0);
    assert(attempts(&f, 1, a) == 3);
}

/* Cancellation, faults and the spent LOAD budget during a retract end the
 * LOAD once. */
static void test_retract_interrupted(void)
{
    for (int fault = 0; fault < 3; ++fault) {
        km_device_profile_t p; retry_profile(&p);
        fixture_t f; setup_profile(&f, &p);
        assert(load(&f, 1, 1) == KM_REQ_ACCEPTED);
        fail_attempt(&f, MAX_TRAVEL);
        int64_t furthest = pos(&f);
        for (unsigned i = 0; i < 1000 && pos(&f) >= furthest; ++i) {
            if (pos(&f) > furthest) furthest = pos(&f);
            advance(&f, 1);
        }
        assert(f.core.stage == KM_STAGE_LOAD_RETRY_RETRACT && f.rewinder[0].base.enabled);
        km_load_attempt_t a[KM_LOAD_ATTEMPTS];
        if (fault) {
            km_fault_t stop = fault == 1 ? KM_FAULT_DRIVER_FAULT : KM_FAULT_NO_PROGRESS;
            /* km_core_update polls active_feeder/active_rewinder for a fault
             * every control tick (no separate per-edge service to notice it
             * first anymore); one advance(&f, 1) below is enough. */
            if (fault == 1) f.feeder[0].injected_fault = KM_FAULT_DRIVER_FAULT;
            else f.core.load_started_ms = f.ms + 1u - KM_LOAD_BUDGET_MS; /* Spent next ms. */
            advance(&f, 1);
            failed(&f, stop);
            assert(attempts(&f, 1, a) == 2 && a[1].stage == KM_STAGE_LOAD_RETRY_RETRACT
                   && a[1].fault == stop);
        } else {
            km_core_timepoint_t t = {f.ms, f.ms * 1000u};
            assert(km_core_request_cancel5(&f.core, KM_CLIENT_HOST, 1, 1, t) == KM_REQ_ACCEPTED);
            assert(!f.rewinder[0].base.enabled);
            settled(&f);
            event(&f, KM_EVENT_LOAD_CANCELLED, 0);
            assert(km_event_queue_count(&f.core.event_queue) == 0);
            assert(f.core.path_occupied && f.core.op_state == KM_OP_STATE_LOADED);
            assert(attempts(&f, 1, a) == 1);
        }
        assert(!f.feeder[0].base.enabled && !f.rewinder[0].base.enabled);
    }
}

/* A delayed but fresh release acquired before minimum travel cannot arm just
 * because the motor has advanced by the time the controller consumes it. */
static void test_delayed_release_before_minimum(void)
{
    fixture_t f; setup(&f, false);
    assert(load(&f, 1, 1) == KM_REQ_ACCEPTED);
    reach_hub(&f);
    travel_to(&f, 180);
    f.adc_period = 0;
    advance(&f, 1);
    uint32_t before_minimum = f.ms;
    assert(travel(&f) < 200);
    travel_to(&f, 220);
    f.adc_attempt_us = before_minimum * 1000u;
    f.adc_repeat = true;
    publish_buffer(&f);
    advance(&f, 5);
    assert(!f.core.fps_armed && f.core.fps_count == 0);
    f.adc_repeat = false;
    f.adc_period = 5;
    f.buffer = 750;
    advance(&f, 50); /* Unarmed: the compression is an obstruction, not arrival. */
    assert(!f.core.fps_armed && f.core.stage == KM_STAGE_LOAD_RETRY_RETRACT);
    assert(f.core.load_attempts[0].fault == KM_FAULT_JAM);
}

static void test_rejected_evidence(void)
{
    for (int kind = 0; kind < 3; ++kind) {
        fixture_t f; setup(&f, false);
        assert(load(&f, 1, 1) == KM_REQ_ACCEPTED);
        reach_hub(&f); arm(&f);
        f.buffer = 750; advance(&f, 6);
        assert(f.core.fps_count >= 1);
        if (kind == 0) f.adc_off_path = true;   /* Contradictory DiffA/DiffB pair. */
        else if (kind == 1) f.adc_invalid = true; /* Failed acquisition record. */
        else f.buffer = 1000;                   /* Fully compressed calibrated end. */
        advance(&f, 5);
        failed(&f, KM_FAULT_BUFFER_SIGNAL);
    }
    /* Uncompressed (0) after partial compression is a release, not a fault. */
    fixture_t f; setup(&f, false);
    assert(load(&f, 1, 1) == KM_REQ_ACCEPTED);
    reach_hub(&f); arm(&f);
    f.buffer = 750; advance(&f, 6);
    assert(f.core.fps_count >= 1);
    f.buffer = 0; advance(&f, 6);
    assert(f.core.stage == KM_STAGE_LOAD_FEED_TO_BUFFER && !f.core.fps_count && f.core.fps_armed);
    completes(&f);
}

static void test_contradictions(void)
{
    fixture_t f; setup(&f, false);
    assert(load(&f, 1, 1) == KM_REQ_ACCEPTED);
    reach_hub(&f); arm(&f);
    f.buffer = 750; advance(&f, 6);
    f.core.filament_present_hub = false; advance(&f, 1);
    failed(&f, KM_FAULT_FILAMENT_LOST);

    /* Selected toolhead corroboration absent when FPS qualifies. */
    setup(&f, true);
    assert(load(&f, 1, 1) == KM_REQ_ACCEPTED);
    reach_hub(&f); arm(&f);
    uint32_t elapsed = hold(&f);
    assert(elapsed >= 21 && elapsed <= 25);
    failed(&f, KM_FAULT_SENSOR_FAILURE);
}

static void test_cancel_stop_and_next_operation(void)
{
    fixture_t f; setup(&f, false);
    assert(load(&f, 1, 1) == KM_REQ_ACCEPTED);
    reach_hub(&f); arm(&f);
    f.buffer = 750; advance(&f, 12);
    assert(f.core.fps_count >= 2);
    km_core_timepoint_t t = {f.ms, f.ms * 1000u};
    assert(km_core_request_cancel(&f.core, KM_CLIENT_HOST, 1, 1, t) == KM_REQ_ACCEPTED);
    assert(f.core.stage == KM_STAGE_BRAKING);
    settled(&f); /* Continued pressure while braking completes nothing. */
    event(&f, KM_EVENT_LOAD_CANCELLED, 0);
    assert(km_event_queue_count(&f.core.event_queue) == 0);

    setup(&f, false);
    assert(load(&f, 1, 1) == KM_REQ_ACCEPTED);
    reach_hub(&f); arm(&f);
    f.buffer = 750; advance(&f, 12);
    assert(f.core.fps_count >= 2);
    km_core_explicit_stop(&f.core, KM_FAULT_EMERGENCY_STOP, f.ms);
    failed(&f, KM_FAULT_EMERGENCY_STOP);
    f.core.filament_present_hub = false; f.buffer = 500; advance(&f, 5);
    t = (km_core_timepoint_t){f.ms, f.ms * 1000u};
    assert(km_core_recover(&f.core, 0, false, 0, t) == KM_OK);
    assert(load(&f, 2, 1) == KM_REQ_ACCEPTED);
    assert(f.core.fps_count == 0 && !f.core.fps_armed);
    reach_hub(&f); /* The new window restarts from this HUB and position. */
    assert(f.core.fps_hub_steps == pos(&f));
    arm(&f);
    completes(&f);
}

static void test_preloaded_hub_entry(void)
{
    fixture_t f; setup(&f, false);
    km_core_timepoint_t t = {f.ms, f.ms * 1000u};
    assert(km_core_request_preload(&f.core, KM_CLIENT_HOST, 1, 1, 0, t) == KM_REQ_ACCEPTED);
    advance(&f, 10);
    f.core.filament_present_hub = true; advance(&f, 1);
    /* No per-lane LOAD switch, a real HUB: PRELOAD retracts until the HUB
     * clears rather than settling the instant it trips. */
    assert(f.core.stage == KM_STAGE_UNLOAD_RETRACT_TO_HUB);
    advance(&f, 10);
    f.core.filament_present_hub = false;
    settled(&f);
    event(&f, KM_EVENT_PRELOAD_COMPLETE, 0);
    assert(f.core.op_state == KM_OP_STATE_PRELOADED);
    assert(load(&f, 1, 2) == KM_REQ_ACCEPTED);
    /* The rest was behind the HUB, not at a LOAD switch: LOAD re-feeds
     * through the HUB again before entering FEED_TO_BUFFER. */
    assert(f.core.stage == KM_STAGE_LOAD_FEED_TO_HUB);
    advance(&f, 10);
    f.core.filament_present_hub = true;
    advance(&f, 1);
    assert(f.core.stage == KM_STAGE_LOAD_FEED_TO_BUFFER);
    assert(f.core.fps_hub_steps == km_trajectory_get_position(&f.core.trajectory));
    assert(!f.core.fps_armed && f.core.fps_count == 0);
    arm(&f);
    completes(&f);
}

/* P10: without an encoder or toolhead switch, calibration reuses LOAD's HUB
 * and FPS arrival stages and reports completed microsteps as an estimate. */
static void test_step_calibration(void)
{
    km_device_profile_t p; fixture_t f;
    km_profile_init_boxturtle(&p); p.has_toolhead_sensor = false;
    setup_profile(&f, &p);
    assert(calibrate(&f, 1, 1) == KM_REQ_UNAVAILABLE); /* No FPS arrival: no method. */
    setup(&f, false);
    f.buffer = 750; advance(&f, 5);
    assert(calibrate(&f, 1, 1) == KM_REQ_UNAVAILABLE && f.core.stage == KM_STAGE_STOPPED);
    f.buffer = 500; advance(&f, 5);
    assert(calibrate(&f, 1, 1) == KM_REQ_ACCEPTED);
    assert(f.core.op_state == KM_OP_STATE_CALIBRATING && f.core.cal_step_estimate);
    reach_hub(&f);
    int64_t hub_pulses = km_ref_position(&f.clock.ref);
    arm(&f);
    uint32_t elapsed = hold(&f);
    assert(elapsed >= 21 && elapsed <= 25);
    assert(f.core.stage == KM_STAGE_CALIBRATE_RETRACT);
    assert(km_event_queue_count(&f.core.event_queue) == 0); /* Never LOAD_COMPLETE. */
    uint32_t steps = (uint32_t)(km_ref_position(&f.clock.ref) - hub_pulses);
    assert(f.core.calibrated_ptfe_value == steps && travel(&f) == (int64_t)steps);
    assert(steps >= 200 && steps < 3000);
    int64_t arrival = km_trajectory_get_position(&f.core.trajectory);
    f.buffer = 500; advance(&f, 1000);
    assert(f.core.stage == KM_STAGE_CALIBRATE_RETRACT);
    assert(km_trajectory_get_position(&f.core.trajectory) < arrival);
    f.core.filament_present_hub = false;
    settled(&f);
    km_event_t e = event(&f, KM_EVENT_PTFE_CAL_COMPLETE, steps);
    assert(e.op_type == KM_OP_TYPE_CALIBRATE && e.flags == KM_EVENT_FLAG_STEP_ESTIMATE);
    assert(e.result_code == KM_WIRE_OP_CODE_SUCCESS);
    assert(!km_protocol_wire_encode_event(&e).valid); /* Not a legacy encoder length. */
    assert(f.core.op_state == KM_OP_STATE_IDLE && !f.core.path_occupied);
    assert(!f.feeder[0].base.enabled && km_event_queue_count(&f.core.event_queue) == 0);

    /* A fitted toolhead switch without an encoder must corroborate arrival. */
    setup(&f, true);
    assert(calibrate(&f, 1, 1) == KM_REQ_ACCEPTED && f.core.cal_step_estimate);
    reach_hub(&f); arm(&f);
    elapsed = hold(&f);
    assert(elapsed >= 21 && elapsed <= 25);
    failed(&f, KM_FAULT_SENSOR_FAILURE);

    /* With an encoder and toolhead switch, the encoder probe is unchanged. */
    km_profile_init_boxturtle(&p); p.has_encoder = true;
    p.encoder_detection_length_steps = 1000; /* km_profile_validate requires > 0. */
    setup_profile(&f, &p);
    km_core_set_encoder_at(&f.core, 0, true, f.ms);
    assert(calibrate(&f, 1, 1) == KM_REQ_ACCEPTED);
    assert(f.core.stage == KM_STAGE_CALIBRATE_PROBE && !f.core.cal_step_estimate);
}

static void test_step_calibration_failures(void)
{
    /* HUB loss, then contradictory, failed, fully compressed and stale samples. */
    for (int kind = 0; kind < 5; ++kind) {
        fixture_t f; setup(&f, false);
        assert(calibrate(&f, 1, 1) == KM_REQ_ACCEPTED);
        reach_hub(&f); arm(&f);
        f.buffer = 750; advance(&f, 6);
        assert(f.core.fps_count >= 1);
        if (kind == 0) f.core.filament_present_hub = false;
        else if (kind == 1) f.adc_off_path = true;
        else if (kind == 2) f.adc_invalid = true;
        else if (kind == 3) f.buffer = 1000;
        else f.adc_period = 0;
        for (unsigned i = 0; i < 200 && f.core.stage == KM_STAGE_LOAD_FEED_TO_BUFFER; ++i)
            advance(&f, 1);
        failed(&f, kind ? KM_FAULT_BUFFER_SIGNAL : KM_FAULT_FILAMENT_LOST);
    }

    /* Pressure before minimum travel, then maximum travel from HUB without
     * arrival. Calibration never retries or records LOAD attempts. */
    for (int pressed = 0; pressed < 2; ++pressed) {
        fixture_t f; setup(&f, false);
        assert(calibrate(&f, 1, 1) == KM_REQ_ACCEPTED);
        reach_hub(&f);
        f.buffer = pressed ? 750 : 0;
        for (unsigned i = 0; i < 3000 && f.core.stage == KM_STAGE_LOAD_FEED_TO_BUFFER; ++i)
            advance(&f, 1);
        if (pressed) assert(travel(&f) < 200);
        else assert(travel(&f) >= 3000 && travel(&f) < 3010);
        failed(&f, pressed ? KM_FAULT_JAM : KM_FAULT_NO_PROGRESS);
        assert(!f.core.load_attempt_count);
    }
    fixture_t f;

    /* The LOAD arrival deadline bounds the estimate when travel does not. */
    km_device_profile_t p; fps_profile(&p, false);
    p.buffer_feed_timeout_ms = 400; p.fps_arrival_max_steps = 1000000;
    setup_profile(&f, &p);
    assert(calibrate(&f, 1, 1) == KM_REQ_ACCEPTED);
    reach_hub(&f);
    uint32_t entered = f.ms;
    for (unsigned i = 0; i < 1000 && f.core.stage == KM_STAGE_LOAD_FEED_TO_BUFFER; ++i)
        advance(&f, 1);
    assert(f.ms - entered == 400);
    failed(&f, KM_FAULT_NO_PROGRESS);
}

static void test_step_calibration_cancel(void)
{
    fixture_t f; setup(&f, false);
    assert(calibrate(&f, 1, 1) == KM_REQ_ACCEPTED);
    reach_hub(&f); arm(&f);
    f.buffer = 750; advance(&f, 12);
    assert(f.core.fps_count >= 2);
    km_core_timepoint_t t = {f.ms, f.ms * 1000u};
    assert(km_core_request_cancel5(&f.core, KM_CLIENT_HOST, 1, 1, t) == KM_REQ_ACCEPTED);
    assert(f.core.stage == KM_STAGE_BRAKING);
    settled(&f); /* Continued pressure while braking completes nothing. */
    km_event_t e = event(&f, KM_EVENT_PTFE_CAL_CANCELLED, 0);
    assert(e.flags == KM_EVENT_FLAG_NONE && e.result_code == KM_WIRE_OP_CODE_CANCEL);
    assert(km_event_queue_count(&f.core.event_queue) == 0);
    assert(!f.feeder[0].base.enabled && !f.feeder[0].base.step_pin_high);
    assert(!f.rewinder[0].base.enabled && !f.rewinder[0].base.pwm_duty_permille);
    assert(km_trajectory_get_velocity(&f.core.trajectory) == 0);
    /* Filament remains past HUB; the path stays reserved for UNLOAD. */
    assert(f.core.path_occupied && f.core.op_state == KM_OP_STATE_LOADED);
}

/* The calibrated LOAD: a stored HUB-to-arrival distance, or the one this
 * session's step-estimated CALIBRATE measured, is where the follower stops
 * cruising and crawls into the arrival window. */

/* A filament plant: the tip reaches the extruder `arrival` steps past the
 * HUB, and feeding beyond it compresses the buffer, linearly, to full over
 * the next `travel` steps. Values are published on the 5 ms acquisition
 * cadence, one tick before the controller consumes each sample, as the
 * DiffA/DiffB pair is. */
typedef struct {
    int32_t peak_permille;
    int32_t peak_speed;
    int64_t crawl_at;   /* Travel when the core retargeted to the crawl. */
    int64_t arrival_at; /* Travel when the follower left FEED_TO_BUFFER. */
} plant_trace_t;

static void plant_run(fixture_t *f, int64_t arrival, int64_t travel_steps,
                      uint32_t limit_ms, plant_trace_t *tr)
{
    memset(tr, 0, sizeof(*tr));
    tr->crawl_at = tr->arrival_at = -1;
    for (uint32_t i = 0; i < limit_ms; ++i) {
        int64_t beyond = travel(f) - arrival;
        f->buffer = beyond <= 0 ? 0 : (int32_t)(beyond * KM_PERMILLE_MAX / travel_steps);
        if (f->buffer > KM_PERMILLE_MAX) f->buffer = KM_PERMILLE_MAX;
        bool was_crawl = f->core.load_crawl;
        advance(f, 1);
        int32_t speed = km_trajectory_get_velocity(&f->core.trajectory);
        if (speed > tr->peak_speed) tr->peak_speed = speed;
        /* The retarget itself, not a velocity that merely passes through the
         * crawl band while still ramping up to cruise. */
        if (tr->crawl_at < 0 && !was_crawl && f->core.load_crawl) tr->crawl_at = travel(f);
        if (f->buffer > tr->peak_permille) tr->peak_permille = f->buffer;
        if (tr->arrival_at < 0 && f->core.stage != KM_STAGE_LOAD_FEED_TO_BUFFER) {
            tr->arrival_at = travel(f);
            return;
        }
    }
}

/* A calibrated profile for a plant: S is the HUB-to-arrival distance, window
 * the arrival window around it, and outer_min the widest travel a CALIBRATE
 * without S may use to measure it. */
static void plant_profile(km_device_profile_t *p, uint32_t arrival, uint32_t window,
                          uint32_t outer_min)
{
    fps_profile(p, false);
    p->fps_arrival_min_steps = outer_min;
    p->fps_arrival_max_steps = arrival + 2u * window;
    p->fps_calibrated_steps = arrival;
    p->load_window_steps = window;
    p->load_cruise_velocity_steps_s = 20000;
    p->load_crawl_velocity_steps_s = 2000;
    p->buffer_travel_steps = 15000;
    /* The baseline fixture's 12 s buffer-feed deadline was sized for the old
     * single-speed LOAD; a calibrated approach that crawls the last stretch
     * needs the same headroom the real ARRIVAL_TIMEOUT default now gets
     * (1.25x the outer travel at the crawl speed). */
    p->buffer_feed_timeout_ms = (uint32_t)((uint64_t)5 * p->fps_arrival_max_steps * 1000u
        / (4u * p->load_crawl_velocity_steps_s));
    p->default_acceleration_steps_s2 = 40000; /* Reaches cruise well short of S. */
    p->default_deceleration_steps_s2 = 15000;
    p->default_jerk_steps_s3 = 20000000; /* Near-trapezoid ramps, so ramps add up. */
}

static void test_load_needs_calibration(void)
{
    km_device_profile_t p; fixture_t f;

    /* Nothing stored and nothing measured yet: LOAD is refused, so the host
     * can name the fix. CALIBRATE needs no calibrated distance. */
    plant_profile(&p, 4000, 3000, 1000);
    p.fps_calibrated_steps = 0;
    setup_profile(&f, &p);
    assert(f.core.calibrated_arrival_steps == 0);
    assert(load(&f, 1, 1) == KM_REQ_UNAVAILABLE);
    assert(f.core.stage == KM_STAGE_STOPPED && !f.core.path_occupied);
    assert(calibrate(&f, 2, 1) == KM_REQ_ACCEPTED);
    assert(f.core.op_state == KM_OP_STATE_CALIBRATING);

    /* A stored distance restored at config time is the session's S. */
    plant_profile(&p, 4000, 3000, 1000);
    setup_profile(&f, &p);
    assert(f.core.calibrated_arrival_steps == p.fps_calibrated_steps);
    assert(load(&f, 1, 1) == KM_REQ_ACCEPTED);
}

/* A step-estimated CALIBRATE crawls the path after the HUB and the LOAD of
 * the same session aims at what it measured, with no restart. */
static void test_calibration_feeds_the_next_load(void)
{
    km_device_profile_t p; fixture_t f;
    plant_profile(&p, 4000, 3000, 200);
    p.fps_calibrated_steps = 0;
    setup_profile(&f, &p);
    assert(load(&f, 1, 1) == KM_REQ_UNAVAILABLE);
    assert(calibrate(&f, 1, 1) == KM_REQ_ACCEPTED);
    assert(f.core.cal_step_estimate);
    reach_hub(&f);
    assert(f.core.trajectory.target_velocity_steps_s == (int32_t)p.load_crawl_velocity_steps_s);
    arm(&f);
    uint32_t elapsed = hold(&f);
    assert(elapsed >= 21 && elapsed <= 25);
    assert(f.core.stage == KM_STAGE_CALIBRATE_RETRACT);
    uint32_t measured = f.core.calibrated_ptfe_value;
    assert(measured >= p.fps_arrival_min_steps && measured <= p.fps_arrival_max_steps);
    assert(f.core.calibrated_arrival_steps == measured); /* Adopted at once. */
    f.core.filament_present_hub = false;
    settled(&f);
    event(&f, KM_EVENT_PTFE_CAL_COMPLETE, measured);
    assert(!f.core.path_occupied);

    /* The measured distance drives the next LOAD without a restart: the host
     * releases the press and asks again. */
    f.buffer = KM_PERMILLE_MIN; advance(&f, 5);
    assert(load(&f, 2, 1) == KM_REQ_ACCEPTED);
    reach_hub(&f);
    assert(f.core.calibrated_arrival_steps == measured);
    arm(&f);
    completes(&f);
}

/* Long path: cruise is reached, the crawl is commanded before the slowdown
 * point, and arrival stops the buffer well short of full. */
static void test_calibrated_load_long_path(void)
{
    km_device_profile_t p; fixture_t f; plant_trace_t tr;
    plant_profile(&p, 32000, 12000, 8000); /* S = 32000; slowdown at 20000. */
    setup_profile(&f, &p);
    assert(load(&f, 1, 1) == KM_REQ_ACCEPTED);
    reach_hub(&f);
    /* The crawl covers the window and the compression ramp to the sensor
     * threshold at 2000 steps/s; give it the wall time that takes. */
    plant_run(&f, p.fps_calibrated_steps, 15000, 20000, &tr);
    assert(f.core.stage == KM_STAGE_FOLLOW); /* Arrived; no NO_PROGRESS. */
    assert(tr.peak_speed >= (int32_t)p.load_cruise_velocity_steps_s);
    assert(tr.crawl_at > 0
           && tr.crawl_at < (int64_t)p.fps_calibrated_steps - p.load_window_steps);
    assert(tr.peak_permille > p.fps_arrival_permille); /* It did compress. */
    assert(tr.peak_permille < 900);
    event(&f, KM_EVENT_LOAD_COMPLETE, 0);
}

/* Short path: the same rule is a triangle. It never reaches cruise, and it
 * still crawls before the slowdown point. */
static void test_calibrated_load_short_path(void)
{
    km_device_profile_t p; fixture_t f; plant_trace_t tr;
    plant_profile(&p, 4000, 3000, 1000); /* S = 4000; slowdown at 1000. */
    setup_profile(&f, &p);
    assert(load(&f, 1, 1) == KM_REQ_ACCEPTED);
    reach_hub(&f);
    plant_run(&f, p.fps_calibrated_steps, 3000, 5000, &tr);
    assert(f.core.stage == KM_STAGE_FOLLOW);
    assert(tr.peak_speed < (int32_t)p.load_cruise_velocity_steps_s);
    assert(tr.crawl_at > 0
           && tr.crawl_at < (int64_t)p.fps_calibrated_steps - p.load_window_steps);
    assert(tr.peak_permille < 900);
    event(&f, KM_EVENT_LOAD_COMPLETE, 0);
}

int main(void)
{
    test_profile_validation_and_defaults();
    test_disabled_keeps_toolhead_arrival();
    test_normal_arrival();
    test_short_spikes_reset();
    test_one_sample_is_not_sustained();
    test_delayed_release_before_minimum();
    test_attempt_causes();
    test_obstruction_then_arrival();
    test_success_on_last_attempt();
    test_retract_interrupted();
    test_rejected_evidence();
    test_contradictions();
    test_cancel_stop_and_next_operation();
    test_preloaded_hub_entry();
    test_step_calibration();
    test_step_calibration_failures();
    test_step_calibration_cancel();
    test_load_needs_calibration();
    test_calibration_feeds_the_next_load();
    test_calibrated_load_long_path();
    test_calibrated_load_short_path();
    puts("FPS arrival tests passed.");
    return 0;
}
