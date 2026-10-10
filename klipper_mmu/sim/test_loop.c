// SPDX-License-Identifier: GPL-3.0-or-later
/* Native one-lane fixture: the production controller drives a causal plant.
 * Parameters and callback costs below are synthetic, never board measurements. */
#include "plant.h"
#include "feedback.h"
#include "klipper_mmu/operation_core.h"
#include "klipper_mmu/step_dir_driver.h"
#include "klipper_mmu/pwm_bridge_driver.h"
#include "klipper_mmu/adc_pair.h"
#include "klipper_mmu/mcu_clock.h"
#include "stepper_reference.h"
#include <assert.h>
#include <math.h>
#include <stdio.h>
#include <string.h>
#ifdef MMU_SIM_PROFILE_HEADER
#include MMU_SIM_PROFILE_HEADER
#endif

typedef struct fixture fixture_t;
typedef struct {
    km_driver_t base;
    fixture_t *f;
    km_fault_t fault;
} sim_feeder_t;

struct fixture {
    mmu_sim_plant_t plant;
    km_operation_core_t core;
    sim_feeder_t feeder;
    km_pwm_bridge_driver_t rewinder;
    km_adc_pair_t adc;
    km_adc_pair_sample_t adc_sample;
    km_mcu_clock_t clock;
    km_ref_stepper_t ref; /* B2 tick API: the reference executor stands in for
                           * Klipper's stepper.c; the plant is fed from its
                           * executed steps, not the driver's edge callbacks. */
    uint64_t epoch, next_control, next_sample, next_heartbeat, next_motion, motion_last_us, delivery_at;
    uint32_t switch_delivery_us;
    uint64_t enable_at;
    uint32_t sample_ms, clock_hz;
    uint64_t adc_ready_at;
    uint16_t adc_held, pwm_forward, pwm_reverse, peak_rewind;
    uint8_t adc_channel;
    double demand;
    bool enabled, rewind_enabled;
    bool sample_bay, sample_load, sample_hub, sample_arrival, sample_low, sample_high, pending;
    bool send_samples, send_heartbeats, send_adc;
    bool adc_active, adc_stuck, has_adc_sample, corrupt_adc;
    uint64_t corrupt_at, fault_at;
    unsigned completed_pulses, reverse_pulses, truncated, load_events, unload_events, cancel_events;
    unsigned preload_events, preload_cancel_events, eject_events, eject_failed_events;
    unsigned adc_samples, adc_errors;
    const char *name;
    FILE *trace;
};

static uint64_t raw_ticks(const fixture_t *f)
{
    return f->plant.time_us * f->clock_hz / 1000000;
}
static km_core_timepoint_t point(fixture_t *f)
{
    uint64_t ticks = raw_ticks(f);
    km_core_timepoint_t now;
    assert(km_mcu_clock_observe(&f->clock, (UINT32_MAX - 20u) + (uint32_t)ticks, &now) == KM_OK);
    /* Independent rational oracle; no private bridge fields are consulted. */
    uint64_t expected = f->epoch + ticks * 1000000 / f->clock_hz;
    assert(now.ms == (uint32_t)(expected / 1000) && now.us == (uint32_t)expected);
    return now;
}
static uint64_t deadline_time(fixture_t *f, uint32_t deadline)
{
    (void)point(f);
    uint32_t tick; bool due;
    assert(km_mcu_clock_deadline_ticks(&f->clock, deadline, &tick, &due) == KM_OK);
    if (due) return f->plant.time_us;
    uint64_t ticks = raw_ticks(f);
    uint32_t delta = tick - ((UINT32_MAX - 20u) + (uint32_t)ticks);
    assert(delta < UINT32_C(0x80000000));
    /* Plant resolution is 1 us: reach or exceed the requested raw timer tick. */
    uint64_t when = ((ticks + delta) * 1000000 + f->clock_hz - 1u) / f->clock_hz;
    assert(when >= f->plant.time_us);
    return when;
}
static void advance(fixture_t *f, uint64_t until)
{
    assert(until >= f->plant.time_us && until - f->plant.time_us <= UINT32_MAX);
    assert(mmu_sim_advance(&f->plant, (uint32_t)(until - f->plant.time_us), f->demand));
    assert(fabs(mmu_sim_material_error_mm(&f->plant)) < 1e-8);
}
static void callback_time(fixture_t *f) { advance(f, f->plant.time_us + 2); }
static uint32_t clock_us(void *context) { return point(context).us; }
static bool ready(void *context) { (void)context; return true; }

static km_result_t feeder_init(km_driver_t *d, const void *config) { (void)d; (void)config; return KM_OK; }
static km_result_t feeder_set_enable(km_driver_t *d, bool enable) {
    fixture_t *f = ((sim_feeder_t *)d)->f; callback_time(f);
    if (enable && !f->enabled) f->enable_at = f->plant.time_us;
    f->enabled = enable;
    return KM_OK;
}
static km_result_t feeder_set_direction(km_driver_t *d, km_direction_t dir, uint32_t now_us) {
    (void)d; (void)dir; (void)now_us;
    return KM_OK;
}
static km_fault_t feeder_get_fault(const km_driver_t *d) {
    return ((sim_feeder_t *)d)->fault;
}
static km_result_t feeder_clear_fault(km_driver_t *d) {
    ((sim_feeder_t *)d)->fault = KM_FAULT_NONE;
    return KM_OK;
}
static void feeder_stop_outputs(km_driver_t *d) {
    feeder_set_enable(d, false);
}
static const km_driver_ops_t feeder_ops = {
    .init = feeder_init,
    .set_enable = feeder_set_enable,
    .set_direction = feeder_set_direction,
    .get_fault = feeder_get_fault,
    .clear_fault = feeder_clear_fault,
    .stop_outputs = feeder_stop_outputs
};
static void feeder_setup(sim_feeder_t *feeder, fixture_t *f) {
    memset(feeder, 0, sizeof(*feeder));
    feeder->base.ops = &feeder_ops;
    feeder->base.caps.min_pulse_width_us = 4;
    feeder->base.caps.min_step_interval_us = 8;
    feeder->base.caps.max_velocity_steps_s = 10000;
    feeder->base.caps.supports_step_dir = true;
    feeder->f = f;
    km_driver_init(&feeder->base, NULL);
}
static void apply_rewind(fixture_t *f)
{
    /* This first model supports takeup only; never silently invent payout. */
    assert(!f->rewind_enabled || !f->pwm_forward);
    uint16_t duty = f->rewind_enabled ? f->pwm_reverse : 0;
    if (duty > f->peak_rewind) f->peak_rewind = duty;
    assert(mmu_sim_rewind(&f->plant, duty));
}
static void rewind_disable(void *context)
{
    fixture_t *f = context; callback_time(f);
    f->rewind_enabled = false; apply_rewind(f);
}
static km_result_t rewind_enable(void *context, bool enabled)
{
    fixture_t *f = context; callback_time(f);
    f->rewind_enabled = enabled; apply_rewind(f); return KM_OK;
}
static km_result_t rewind_pwm(void *context, km_direction_t direction, uint16_t duty)
{
    fixture_t *f = context; callback_time(f);
    if (direction == KM_DIR_FORWARD) f->pwm_forward = duty;
    else f->pwm_reverse = duty;
    apply_rewind(f); return KM_OK;
}

static km_result_t adc_poll(void *context, uint8_t channel, uint16_t *raw, uint32_t *retry)
{
    fixture_t *f = context; callback_time(f);
    assert(channel <= 1);
    if (!f->adc_active) {
        uint16_t a, b;
        assert(mmu_sim_diff_sample(&f->plant, &f->core.profile.diff_calibration, &a, &b));
        f->adc_held = channel == 0 ? a : b;
        f->adc_channel = channel; f->adc_active = true;
        f->adc_ready_at = f->plant.time_us + 40;
    }
    assert(channel == f->adc_channel);
    if (!f->adc_stuck && f->plant.time_us >= f->adc_ready_at) {
        *raw = f->adc_held; f->adc_active = false; return KM_OK;
    }
    *retry = 40; return KM_ERR_BUSY;
}
static void adc_cancel(void *context) { ((fixture_t *)context)->adc_active = false; }

static void acquire(fixture_t *f)
{
    assert(!f->pending);
    f->sample_ms = point(f).ms;
    f->sample_bay = f->plant.bay_present;
    f->sample_load = f->plant.load_present;
    f->sample_hub = f->plant.hub_present;
    f->sample_arrival = f->plant.arrival_present;
    double fraction = mmu_sim_buffer_mm(&f->plant) / f->plant.parameters.buffer_capacity_mm;
    f->sample_low = fraction <= .25; f->sample_high = fraction >= .75;
    if (f->core.profile.has_proportional_buffer && f->send_adc)
        assert(km_adc_pair_begin(&f->adc, point(f).us) == KM_OK);
    f->delivery_at = f->plant.time_us + f->switch_delivery_us; f->pending = true;
}
static void deliver(fixture_t *f)
{
    km_core_set_sensors_at(&f->core, 0, f->sample_bay, f->sample_hub, f->sample_arrival, true, f->sample_ms);
    if (f->core.profile.has_load_sensors)
        km_core_set_load_sensor_at(&f->core, 0, f->sample_load, true, f->sample_ms);
    if (f->core.profile.has_proportional_buffer) {
        km_core_timepoint_t now = point(f);
        km_result_t result = km_adc_pair_get_sample(&f->adc, now.us, &f->adc_sample);
        f->has_adc_sample = result == KM_OK;
        bool acquired = result == KM_OK && f->adc_sample.is_valid && f->adc_sample.result == KM_OK;
        if (f->corrupt_adc && acquired) {
            /* Acquisition completed normally. A paired (0,0) reading cannot
             * lie on the calibrated DiffA/DiffB path at any buffer position. */
            f->adc_sample.raw_a = 0;
            f->adc_sample.raw_b = 0;
            if (!f->corrupt_at) f->corrupt_at = f->plant.time_us;
        }
        result = km_buffer_update_adc_pair(&f->core.buffer_sensor,
            result == KM_OK ? &f->adc_sample : NULL, now.ms, now.us);
        if (acquired)
            ++f->adc_samples;
        if (result != KM_OK) ++f->adc_errors;
    } else assert(km_buffer_update_switches(&f->core.buffer_sensor,
        f->sample_low, f->sample_high, f->sample_ms) == KM_OK);
    f->pending = false;
}
static void events(fixture_t *f)
{
    km_event_t event;
    while (km_event_queue_pop(&f->core.event_queue, &event) == KM_OK) {
        if (event.type == KM_EVENT_LOAD_COMPLETE) {
            assert(f->plant.hub_present && f->plant.arrival_present); ++f->load_events;
        }
        if (event.type == KM_EVENT_UNLOAD_COMPLETE) {
            assert(!f->plant.hub_present && !f->plant.arrival_present); ++f->unload_events;
        }
        if (event.type == KM_EVENT_LOAD_CANCELLED) ++f->cancel_events;
        if (event.type == KM_EVENT_PRELOAD_COMPLETE) {
            assert(f->plant.load_present && !f->plant.hub_present); ++f->preload_events;
        }
        if (event.type == KM_EVENT_PRELOAD_CANCELLED) ++f->preload_cancel_events;
        if (event.type == KM_EVENT_EJECT_COMPLETE) {
            assert(!f->plant.bay_present && !f->plant.load_present
                   && !f->plant.hub_present);
            ++f->eject_events;
        }
        if (event.type == KM_EVENT_EJECT_FAILED) ++f->eject_failed_events;
    }
}
static void trace_row(fixture_t *f)
{
    if (!f->trace) return;
    assert(fprintf(f->trace, "%s,%llu,%u,%u,%lld,%.9f,%.9f,%.9f,%.9f,%u,%u,%u,%u,%u",
        f->name, (unsigned long long)f->plant.time_us, (unsigned)f->core.stage,
        (unsigned)f->core.system_fault, (long long)f->plant.shaft_steps,
        f->plant.material_mm, mmu_sim_buffer_mm(&f->plant), f->plant.upstream_slack_mm,
        f->plant.consumed_mm, (unsigned)f->enabled, (unsigned)f->plant.rewind_duty,
        (unsigned)f->plant.hub_present, (unsigned)f->plant.arrival_present,
        f->core.buffer_sensor.last_sample_time_ms) > 0);
    const km_adc_pair_sample_t *sample = &f->adc_sample;
    if (f->has_adc_sample)
        assert(fprintf(f->trace, ",%u,%u,%u,%d,%u,%u,%u,%u,%u",
            (unsigned)sample->raw_a, (unsigned)sample->raw_b, (unsigned)sample->present_mask,
            (int)sample->result, (unsigned)sample->is_valid, sample->a_attempt_us,
            sample->a_complete_us, sample->b_attempt_us, sample->b_complete_us) > 0);
    else assert(fputs(",,,,,,,,,", f->trace) >= 0);
    assert(fprintf(f->trace, ",%u\n", (unsigned)f->core.buffer_sensor.is_valid) > 0);
}

/* B2's tick API replaces the per-edge deadline: motion now runs at the same
 * fixed 1 ms cadence as the control tick (window with anchor_restart =
 * !active, room check, queue all, commit, observe, tests/stepper_reference
 * as the executor), never jumping ahead to a sparse edge deadline. Sample,
 * heartbeat, ADC and delivery deadlines stay event-driven around it. */
static void run_motion_tick(fixture_t *f)
{
    uint32_t step_us = (uint32_t)(f->plant.time_us - f->motion_last_us);
    if (step_us > 0) {
        bool active = km_ref_active(&f->ref) != 0;
        uint32_t min_int = f->feeder.base.caps.min_step_interval_us;
        if (!min_int) min_int = 1;

        km_trajectory_window_t w;
        km_result_t res = km_trajectory_window(&f->core.trajectory, step_us, 1,
                                               min_int, !active, &w);
        if (res != KM_OK) {
            km_trajectory_emergency_stop(&f->core.trajectory);
        } else {
            if (w.count > 0 && km_ref_room(&f->ref) >= (int)w.count) {
                for (int i = 0; i < w.count; i++) {
                    int q = km_ref_queue(&f->ref, (uint32_t)f->motion_last_us,
                                         (uint32_t)f->motion_last_us,
                                         w.segments[i].interval, w.segments[i].count,
                                         w.segments[i].add, w.segments[i].dir);
                    assert(q == 0);
                }
            }
            km_trajectory_commit(&f->core.trajectory, &w);

            km_ref_step_t steps[64];
            int n_out = 0;
            km_ref_advance(&f->ref, (uint32_t)f->plant.time_us, steps, 64, &n_out);
            /* Tick granularity (1 ms) is already coarser than this fixture's
             * step rates (a few hundred steps/s at most), so every step in a
             * tick lands at the same plant time; mmu_sim_step treats a
             * same-time repeat as starting from rest rather than dividing by
             * zero, and no scenario here steps faster than 1/tick. */
            for (int i = 0; i < n_out; i++) {
                int dir = steps[i].dir ? 1 : -1;
                assert(mmu_sim_step(&f->plant, dir));
                ++f->completed_pulses;
                if (dir < 0) ++f->reverse_pulses;
            }
            km_trajectory_observe(&f->core.trajectory, km_ref_position(&f->ref),
                                  km_ref_active(&f->ref) != 0);
        }
    }
    f->motion_last_us = f->plant.time_us;
    f->next_motion += 1000;
}

static void run_for(fixture_t *f, uint32_t duration_us)
{
    uint64_t end = f->plant.time_us + duration_us;
    unsigned iterations = 0;
    while (f->plant.time_us < end) {
        assert(++iterations < 1000000);
        uint64_t next = end;
        if (f->next_control < next) next = f->next_control;
        if (f->next_sample < next) next = f->next_sample;
        if (f->next_heartbeat < next) next = f->next_heartbeat;
        if (f->next_motion < next) next = f->next_motion;
        if (f->pending && f->delivery_at < next) next = f->delivery_at;
        uint32_t deadline;
        if (f->core.profile.has_proportional_buffer
            && km_adc_pair_next_deadline(&f->adc, &deadline) == KM_OK) {
            uint64_t due = deadline_time(f, deadline);
            if (due < next) next = due;
        }
        if (next > f->plant.time_us) advance(f, next);
        /* Equal-time ordering: acquisition/delivery, host lease, safety/control,
         * then the motion tick. Callbacks can advance this clock. */
        if (f->next_sample <= f->plant.time_us) {
            if (f->send_samples) acquire(f);
            f->next_sample += 1000;
        }
        if (f->core.profile.has_proportional_buffer
            && km_adc_pair_next_deadline(&f->adc, &deadline) == KM_OK
            && !km_time_before(point(f).us, deadline))
            assert(km_adc_pair_service(&f->adc, point(f).us) == KM_OK);
        if (f->pending && f->delivery_at <= f->plant.time_us) deliver(f);
        if (f->next_heartbeat <= f->plant.time_us) {
            if (f->send_heartbeats) km_core_host_comm_ready(&f->core, point(f).ms);
            f->next_heartbeat += 100000;
        }
        if (f->next_control <= f->plant.time_us) {
            km_pwm_bridge_service(&f->rewinder);
            km_core_timepoint_t now = point(f);
            km_core_update(&f->core, now.ms, now.us);
            if (f->corrupt_at && !f->fault_at && f->core.system_fault)
                f->fault_at = f->plant.time_us;
            f->next_control += 1000; events(f); trace_row(f);
        }
        if (f->next_motion <= f->plant.time_us) run_motion_tick(f);
        assert(fabs(mmu_sim_material_error_mm(&f->plant)) < 1e-8);
    }
}

static void setup_with_spool(fixture_t *f, bool analog, bool local_load, uint64_t epoch,
                             const char *name, FILE *trace, bool finite_spool,
                             double initial_material_mm, double bay_clear_mm)
{
    memset(f, 0, sizeof(*f)); f->epoch = epoch; f->name = name; f->trace = trace;
    f->switch_delivery_us = 200;
#ifdef MMU_SIM_PROFILE_HEADER
    f->switch_delivery_us = mmu_sim_switch_delivery_us;
#endif
    f->clock_hz = analog ? 3333333u : 20000000u;
    km_core_timepoint_t origin = {(uint32_t)(epoch / 1000), (uint32_t)epoch};
    assert(km_mcu_clock_init(&f->clock, f->clock_hz, UINT32_MAX - 20u, origin) == KM_OK);
    mmu_sim_parameters_t model = {.step_mm = .125, .forward_transfer = 1, .reverse_transfer = 1,
        .hub_mm = 10, .arrival_mm = 30, .buffer_capacity_mm = 20, .switch_hysteresis_mm = .25,
        .rewind_max_mm_s = 10, .rewind_time_constant_s = .1};
#ifdef MMU_SIM_PROFILE_HEADER
    model = mmu_sim_profile;
#endif
    /* The local PRELOAD scenario supplies synthetic geometry only when the
     * selected profile has no physical LOAD edge. */
    if (local_load && model.load_mm == 0) model.load_mm = model.hub_mm / 2;
    if (finite_spool) {
        model.finite_spool = true;
        model.spool_budget_mm = model.arrival_mm + 3; /* Synthetic FOLLOW runout. */
    }
    model.bay_clear_mm = bay_clear_mm;
    assert(mmu_sim_init(&f->plant, &model, initial_material_mm, 0));
    feeder_setup(&f->feeder, f);
    /* The reference executor stands in for Klipper's stepper.c: LEAD is a
     * real-MCU dispatcher-jitter guard (contract, "The controller"), not
     * needed by this synchronous tick loop. */
    km_ref_init(&f->ref, KM_REF_MAX_MOVES, 0);
    km_pwm_bridge_config_t rewind = {1000, 20, 50};
    km_pwm_bridge_io_t rewind_io = {rewind_disable, rewind_enable, rewind_pwm, clock_us, ready, NULL, NULL};
    assert(km_pwm_bridge_init(&f->rewinder, &rewind, &rewind_io, f) == KM_OK);
    km_device_profile_t profile; km_profile_init_boxturtle(&profile);
    profile.lane_count = 1; profile.default_feed_velocity_steps_s = 400;
    if (local_load) profile.default_feed_velocity_steps_s = 200;
    profile.default_rewind_velocity_steps_s = 300;
    profile.default_acceleration_steps_s2 = 2000; profile.default_deceleration_steps_s2 = 2500;
    profile.hub_feed_timeout_ms = 1500; profile.host_watchdog_ms = 500;
    profile.has_proportional_buffer = analog; profile.has_diff_calibration = analog;
    profile.has_load_sensors = local_load;
    profile.proportional_gain_milli = 1000; profile.buffer_deadband_permille = 10;
    profile.diff_calibration = (km_diff_adc_config_t){.vref_mv = 3300, .adc_max_counts = 4095,
        .path_tolerance_counts = 2, .point_count = 3,
        .points = {{0, 2048, 0}, {2048, 2048, 500}, {2048, 4095, 1000}}};
    assert(km_core_init(&f->core, &profile) == KM_OK);
    assert(km_core_bind_lane_driver(&f->core, 0, &f->feeder.base, &f->rewinder.base) == KM_OK);
    km_adc_pair_config_t adc = {4095, 120, 180, 200000};
    km_adc_pair_hal_t adc_io = {adc_poll, adc_cancel};
    assert(km_adc_pair_init(&f->adc, &adc, &adc_io, f, point(f).us) == KM_OK);
    f->send_samples = f->send_heartbeats = f->send_adc = true;
    acquire(f);
    uint32_t deadline;
    while (analog && km_adc_pair_next_deadline(&f->adc, &deadline) == KM_OK) {
        advance(f, deadline_time(f, deadline));
        assert(km_adc_pair_service(&f->adc, point(f).us) == KM_OK);
    }
    uint64_t first_acquisition_at = f->delivery_at - f->switch_delivery_us;
    advance(f, f->delivery_at); deliver(f);
    assert(f->plant.time_us - first_acquisition_at == f->switch_delivery_us);
    fprintf(stderr, "%s first switch acquisition=%llu delivery=%llu\n", name,
        (unsigned long long)first_acquisition_at,
        (unsigned long long)f->plant.time_us);
    km_core_host_comm_ready(&f->core, point(f).ms);
    km_core_timepoint_t now = point(f); km_core_update(&f->core, now.ms, now.us);
    f->next_control = f->next_sample = f->plant.time_us + 1000;
    f->next_heartbeat = f->plant.time_us + 100000;
    f->motion_last_us = f->plant.time_us;
    f->next_motion = f->plant.time_us + 1000;
    assert(!f->core.profile.has_encoder && !f->core.profile.has_current_sensing);
    assert(!f->feeder.base.caps.supports_current_measurement && !f->rewinder.base.caps.supports_current_measurement);
}
static void setup(fixture_t *f, bool analog, bool local_load, uint64_t epoch, const char *name, FILE *trace)
{
    setup_with_spool(f, analog, local_load, epoch, name, trace, false, 0, 0);
}
static void load(fixture_t *f)
{
    assert(km_core_request_load(&f->core, KM_CLIENT_HOST, 1, 1, 0, point(f)) == KM_REQ_ACCEPTED);
    run_for(f, 2000000);
    if (f->core.system_fault) fprintf(stderr, "%s load fault=%u stage=%u at=%llu\n", f->name,
        (unsigned)f->core.system_fault, (unsigned)f->core.stage, (unsigned long long)f->plant.time_us);
    assert(f->core.stage == KM_STAGE_FOLLOW && f->load_events == 1);
    assert(f->plant.arrival_present && f->plant.shaft_steps > 0);
    assert(f->completed_pulses * f->plant.parameters.step_mm >= f->plant.parameters.arrival_mm);
}
static void normal(bool analog, uint64_t epoch, const char *name, FILE *trace)
{
    fixture_t f; setup(&f, analog, false, epoch, name, trace); load(&f);
    f.demand = 10; run_for(&f, 3000000);
    assert(f.core.stage == KM_STAGE_FOLLOW && !f.core.system_fault);
    fprintf(stderr, "%s following consumed=%.17g unmet=%.17g lost=%.17g buffer=%.17g\n",
        name, f.plant.consumed_mm, f.plant.unmet_consumption_mm,
        f.plant.lost_travel_mm, mmu_sim_buffer_mm(&f.plant));
    assert(f.plant.consumed_mm >= 30 - 1e-8 && f.plant.unmet_consumption_mm < 1e-8);
    const mmu_sim_parameters_t *p = &f.plant.parameters;
    double expected_slip = p->step_mm
        * ((f.completed_pulses - f.reverse_pulses) * (1 - p->forward_transfer)
           + f.reverse_pulses * (1 - p->reverse_transfer));
    assert(mmu_sim_buffer_mm(&f.plant) > 0);
    assert(fabs(f.plant.lost_travel_mm - expected_slip) < 1e-8);
    f.demand = 0;
    double payout = f.plant.spool_payout_mm;
    assert(km_core_request_unload(&f.core, KM_CLIENT_HOST, 1, 2, point(&f)) == KM_REQ_ACCEPTED);
    run_for(&f, 3000000);
    assert(f.unload_events == 1 && !f.core.shared_path_owned && !f.core.path_occupied);
    assert(!f.plant.hub_present && !f.plant.arrival_present && !f.enabled && !f.rewind_enabled);
    assert(f.peak_rewind > 0 && f.plant.spool_payout_mm < payout);
    assert(!f.truncated && f.core.trajectory.current_position_steps == f.plant.shaft_steps);
    if (analog) assert(f.adc_samples > 1000);
    printf("%s: load/follow/unload; consumed %.3f mm, pulses %u, remaining %.3f mm\n",
        name, f.plant.consumed_mm, f.completed_pulses, f.plant.material_mm);
}
static void local_preload(FILE *trace)
{
    fixture_t f;
    setup(&f, false, true, 0, "preload-load", trace);
    assert(km_core_request_preload(&f.core, KM_CLIENT_HOST, 1, 1, 0, point(&f)) == KM_REQ_ACCEPTED);
    while (!f.preload_events && f.plant.time_us < 1000000) run_for(&f, 1000);
    assert(f.preload_events == 1 && f.core.op_state == KM_OP_STATE_PRELOADED);
    assert(f.core.path_occupied && !f.core.shared_path_owned);
    assert(f.plant.load_present && !f.plant.hub_present && !f.plant.arrival_present);
    assert(km_core_request_load(&f.core, KM_CLIENT_HOST, 1, 2, 0, point(&f)) == KM_REQ_ACCEPTED);
    run_for(&f, 1500000);
    assert(f.load_events == 1 && f.core.stage == KM_STAGE_FOLLOW && !f.core.system_fault);
    assert(f.plant.arrival_present && f.plant.load_present);

    setup(&f, false, true, 0, "preload-unload", trace);
    assert(km_core_request_preload(&f.core, KM_CLIENT_HOST, 1, 1, 0, point(&f)) == KM_REQ_ACCEPTED);
    while (!f.preload_events && f.plant.time_us < 1000000) run_for(&f, 1000);
    assert(f.preload_events == 1 && f.plant.load_present);
    assert(km_core_request_unload(&f.core, KM_CLIENT_HOST, 1, 2, point(&f)) == KM_REQ_ACCEPTED);
    run_for(&f, 800000);
    assert(f.unload_events == 1 && !f.core.path_occupied && !f.core.system_fault);
    assert(!f.plant.load_present && !f.plant.hub_present && !f.enabled);

    setup(&f, false, true, 0, "cancel-preload", trace);
    assert(km_core_request_preload(&f.core, KM_CLIENT_HOST, 1, 1, 0, point(&f)) == KM_REQ_ACCEPTED);
    run_for(&f, 50000);
    assert(!f.plant.load_present);
    assert(km_core_request_cancel5(&f.core, KM_CLIENT_HOST, 1, 1, point(&f)) == KM_REQ_ACCEPTED);
    run_for(&f, 200000);
    assert(f.preload_cancel_events == 1 && !f.core.path_occupied && !f.core.system_fault);
    assert(!f.plant.load_present && !f.enabled && !km_ref_active(&f.ref) && !f.truncated);
    puts("local LOAD preload/load, preload/unload, and early cancel passed");
}
static void eject_after_unload(FILE *trace)
{
    /* Synthetic geometry: a short tip begins beyond BAY, before LOAD. The
     * BAY edge and switch hysteresis are fit targets for hardware data. The
     * unload stops once LOAD clears: about 0.45 mm of switch acquisition delay
     * at 37.5 mm/s plus a jerk-limited stop of v/D + D/J = 27 steps (3.4 mm,
     * 1.1 mm more than a trapezoid), 4.1 mm in all. The LOAD-to-BAY gap must
     * exceed that, here 5 - 0.4 = 4.6 mm (it was 3 mm, sized for the
     * trapezoid). On hardware the gap must exceed the full-speed stop. */
    fixture_t f;
    setup_with_spool(&f, false, true, 0, "eject-after-unload", trace,
                     false, 4, 0.4);
    assert(f.plant.bay_present && !f.plant.load_present);
    assert(km_core_request_preload(&f.core, KM_CLIENT_HOST, 1, 1, 0, point(&f)) == KM_REQ_ACCEPTED);
    while (!f.preload_events && f.plant.time_us < 1000000) run_for(&f, 1000);
    assert(f.preload_events == 1 && f.plant.load_present);
    assert(km_core_request_unload(&f.core, KM_CLIENT_HOST, 1, 2, point(&f)) == KM_REQ_ACCEPTED);
    run_for(&f, 800000);
    assert(f.unload_events == 1 && f.plant.bay_present && !f.plant.load_present);
    assert(!f.plant.hub_present && f.core.op_state == KM_OP_STATE_IDLE);
    int64_t before = f.plant.shaft_steps;
    assert(km_core_request_eject(&f.core, KM_CLIENT_HOST, 1, 3, 0, point(&f)) == KM_REQ_ACCEPTED);
    run_for(&f, 800000);
    assert(f.eject_events == 1 && !f.eject_failed_events && !f.plant.bay_present);
    assert(f.plant.shaft_steps < before && !f.enabled && !f.rewind_enabled);
    assert(!f.core.system_fault && f.core.op_state == KM_OP_STATE_IDLE);

    /* UNLOAD from a completed LOAD clears the hub, not the lane LOAD switch.
     * Stuck BAY has to start from the same cleared preload path as success. */
    setup_with_spool(&f, false, true, 0, "eject-stuck-bay", trace,
                     false, 4, 0.4); /* LOAD-to-BAY gap: see above */
    assert(km_core_request_preload(&f.core, KM_CLIENT_HOST, 1, 1, 0, point(&f)) == KM_REQ_ACCEPTED);
    while (!f.preload_events && f.plant.time_us < 1000000) run_for(&f, 1000);
    assert(f.preload_events == 1 && f.plant.load_present);
    assert(km_core_request_unload(&f.core, KM_CLIENT_HOST, 1, 2, point(&f)) == KM_REQ_ACCEPTED);
    run_for(&f, 800000);
    assert(f.unload_events == 1 && f.plant.bay_present && !f.plant.load_present);
    f.plant.blocked = true; /* Shaft motion loses traction, so the tip stays past BAY. */
    double stuck_material = f.plant.material_mm;
    int64_t stuck_shaft = f.plant.shaft_steps;
    assert(km_core_request_eject(&f.core, KM_CLIENT_HOST, 1, 3, 0, point(&f)) == KM_REQ_ACCEPTED);
    run_for(&f, 8500000);
    assert(!f.eject_events && f.eject_failed_events == 1 && f.plant.bay_present);
    assert(f.plant.material_mm == stuck_material && f.plant.shaft_steps < stuck_shaft);
    assert(f.core.system_fault == KM_FAULT_NO_PROGRESS);
    assert(!f.enabled && !f.rewind_enabled);
    puts("synthetic EJECT after UNLOAD and stuck-BAY failure passed");
}
static void faults(FILE *trace)
{
    fixture_t f; setup(&f, false, false, 0, "cancel-load", trace);
    assert(km_core_request_load(&f.core, KM_CLIENT_HOST, 1, 1, 0, point(&f)) == KM_REQ_ACCEPTED);
    run_for(&f, 120000);
    int64_t before_cancel = f.plant.shaft_steps;
    assert(km_core_request_cancel5(&f.core, KM_CLIENT_HOST, 1, 1, point(&f)) == KM_REQ_ACCEPTED);
    assert(f.core.shared_path_owned && f.core.stage == KM_STAGE_BRAKING);
    /* Cancelled mid-ramp (about 183 steps/s, still accelerating at +1280):
     * the jerk-limited stop first removes that acceleration, 177 ms in all. */
    run_for(&f, 300000);
    assert(f.cancel_events == 1 && !f.core.shared_path_owned && !f.enabled && !km_ref_active(&f.ref));
    assert(f.plant.shaft_steps > before_cancel && !f.truncated);
    assert(f.plant.shaft_steps == f.core.trajectory.current_position_steps);
    setup(&f, false, false, 0, "blocked-load", trace); f.plant.blocked = true;
    assert(km_core_request_load(&f.core, KM_CLIENT_HOST, 1, 1, 0, point(&f)) == KM_REQ_ACCEPTED);
    run_for(&f, 7000000); /* Three HUB deadlines, the last at half speed. */
    assert(f.core.system_fault == KM_FAULT_NO_PROGRESS && !f.enabled && !f.load_events);
    assert(f.core.load_attempt_count == KM_LOAD_ATTEMPTS);
    assert(!f.plant.hub_present && f.plant.material_mm == 0 && f.plant.shaft_steps > 0);
    setup(&f, true, false, 0, "stale-sensors", trace); load(&f);
    f.send_samples = false; run_for(&f, 202000);
    assert(f.core.system_fault == KM_FAULT_SENSOR_FAILURE && !f.enabled && !f.rewind_enabled);
    setup(&f, true, false, 0, "adc-timeout", trace); load(&f);
    f.adc_stuck = true; run_for(&f, 53000);
    assert(f.adc_errors > 0 && f.core.system_fault == KM_FAULT_BUFFER_SIGNAL);
    assert(!f.enabled && !f.rewind_enabled && !f.core.buffer_sensor.is_valid);
    setup(&f, true, false, 0, "adc-stale", trace); load(&f);
    f.send_adc = false;
    /* Finish any pair already started, then verify cached samples keep their
     * acquisition age while ordinary switches continue updating. */
    run_for(&f, 1000);
    uint32_t last_sample = f.core.buffer_sensor.last_sample_time_ms;
    run_for(&f, 100000);
    assert(!f.core.system_fault && f.core.buffer_sensor.last_sample_time_ms == last_sample);
    run_for(&f, 153000);
    assert(f.core.system_fault == KM_FAULT_BUFFER_SIGNAL && !f.enabled && !f.rewind_enabled);
    assert(f.core.hub_sensor_valid && point(&f).ms - f.core.path_sample_ms <= 2);
    setup(&f, false, false, 0, "host-lease", trace); load(&f);
    f.demand = 10; run_for(&f, 1000000);
    f.send_heartbeats = false;
    int64_t before_stall = f.plant.shaft_steps;
    run_for(&f, 300000);
    assert(!f.core.system_fault && f.core.stage == KM_STAGE_FOLLOW);
    assert(f.plant.shaft_steps > before_stall && f.plant.unmet_consumption_mm < 1e-8);
    run_for(&f, 501000);
    assert(f.core.system_fault == KM_FAULT_HOST_COMM_LOST && !f.enabled && !f.rewind_enabled);
    puts("graceful cancel, blocked load, stale acquisition, ADC timeout/staleness and host-lease scenarios passed");
}
static void finite_spool_runout(FILE *trace)
{
    fixture_t f; setup_with_spool(&f, false, false, 0, "finite-spool-runout", trace, true, 0, 0);
    assert(km_core_request_load(&f.core, KM_CLIENT_HOST, 1, 1, 0, point(&f)) == KM_REQ_ACCEPTED);
    run_for(&f, 750000);
    assert(f.core.stage == KM_STAGE_FOLLOW && f.load_events == 1 && f.plant.bay_present);
    f.demand = 10;
    run_for(&f, 1500000);
    assert(!f.plant.bay_present);
    assert(f.core.system_fault == KM_FAULT_FILAMENT_LOST);
    assert(f.core.stage == KM_STAGE_STOPPED && !f.enabled && !f.rewind_enabled);
    assert(fabs(mmu_sim_material_error_mm(&f.plant)) < 1e-8);
    puts("finite-spool BAY runout in FOLLOW stopped outputs");
}
static void contradictory_adc_follow(FILE *trace)
{
    fixture_t healthy, faulted;
    setup(&healthy, true, false, 0, "adc-pair-healthy", trace);
    setup(&faulted, true, false, 0, "adc-pair-corrupt", trace);
    load(&healthy); load(&faulted);
    healthy.demand = faulted.demand = 10;
    run_for(&healthy, 100000);
    run_for(&faulted, 100000);
    assert(healthy.core.stage == KM_STAGE_FOLLOW && !healthy.core.system_fault);
    assert(faulted.core.stage == KM_STAGE_FOLLOW && !faulted.core.system_fault);
    assert(healthy.plant.arrival_present && faulted.plant.arrival_present);
    assert(healthy.plant.bay_present && faulted.plant.bay_present);
    assert(fabs(healthy.plant.consumed_mm - faulted.plant.consumed_mm) < 1e-8);
    assert(fabs(mmu_sim_buffer_mm(&healthy.plant) - mmu_sim_buffer_mm(&faulted.plant)) < 1e-8);

    faulted.corrupt_adc = true;
    run_for(&healthy, 55000);
    run_for(&faulted, 55000);
    assert(faulted.corrupt_at && faulted.fault_at);
    assert(healthy.core.stage == KM_STAGE_FOLLOW && !healthy.core.system_fault);
    assert(faulted.core.system_fault == KM_FAULT_BUFFER_SIGNAL);
    assert(faulted.core.stage == KM_STAGE_STOPPED);
    assert(!faulted.enabled && !faulted.rewind_enabled && !faulted.core.buffer_sensor.is_valid);
    assert(faulted.plant.arrival_present && faulted.plant.bay_present);
    assert(faulted.plant.unmet_consumption_mm < 1e-8);
    assert(fabs(mmu_sim_material_error_mm(&faulted.plant)) < 1e-8);
    assert(faulted.fault_at - faulted.corrupt_at >= 50000);
    assert(faulted.fault_at - faulted.corrupt_at <= 52000);
    fprintf(stderr, "contradictory ADC delivered=%llu stopped=%llu latency=%llu us; healthy fault=%u\n",
        (unsigned long long)faulted.corrupt_at, (unsigned long long)faulted.fault_at,
        (unsigned long long)(faulted.fault_at - faulted.corrupt_at),
        (unsigned)healthy.core.system_fault);
}
int main(int argc, char **argv)
{
    assert(argc <= 2);
    FILE *trace = argc == 2 ? fopen(argv[1], "w") : NULL;
    assert(argc == 1 || trace);
    if (trace) assert(fputs("scenario,time_us,stage,fault,shaft_steps,material_mm,buffer_mm,slack_mm,consumed_mm,enabled,rewind_permille,hub,arrival,sample_ms,raw_a,raw_b,present_mask,adc_result,adc_valid,a_attempt_us,a_complete_us,b_attempt_us,b_complete_us,buffer_valid\n", trace) >= 0);
    normal(false, 0, "switched", trace);
    normal(true, UINT32_MAX - UINT64_C(500000), "analog-wrap", trace);
    local_preload(trace);
    eject_after_unload(trace);
    faults(trace);
    contradictory_adc_follow(trace);
    finite_spool_runout(trace);
    if (trace) assert(fclose(trace) == 0);
    return 0;
}
