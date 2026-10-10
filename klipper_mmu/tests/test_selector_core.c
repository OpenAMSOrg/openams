// SPDX-License-Identifier: GPL-3.0-or-later
/* Shared-drive selector machines (klipper_mmu/selector.h): the selection
 * phase (SELECT_HOME/SELECT_MOVE/SERVO_DWELL) in front of a lane operation,
 * the HOME op, the shared-gear binding and the calibrated gate table. */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "klipper_mmu/operation_core.h"
#include "mock_driver.h"
#include "fake_clock.h"

typedef struct {
    km_servo_t base;
    uint16_t last_pulse_us;
    int calls;
} fake_servo_t;

static km_result_t fake_servo_pulse(km_servo_t *self, uint16_t pulse_us)
{
    fake_servo_t *s = (fake_servo_t *)self;
    s->last_pulse_us = pulse_us;
    ++s->calls;
    return KM_OK;
}

static void fake_servo_init(fake_servo_t *s)
{
    memset(s, 0, sizeof(*s));
    s->base.set_pulse_us = fake_servo_pulse;
}

typedef struct {
    km_operation_core_t core;
    mock_driver_t feeder;
    mock_driver_t selector;
    fake_servo_t servo;
    km_fake_clock_t clock;
    uint32_t ms;
    /* The selector's home endstop asserts once the shared trajectory's
     * position (home_direction REVERSE) reaches this value or below. 0
     * disables the switch (never asserts): a homing-failure scenario. */
    int64_t endstop_trigger;
    /* INDEXED only: gate g's own index switch asserts once the RAW hardware
     * step count (clock.ref.position -- never rebased by km_trajectory_recover,
     * unlike km_trajectory_get_position) sits within gate_half_width of
     * gate_center[g] (mod gate_period, if set): a fixed real-world layout,
     * unlike the core's own per-selection rebased frame. gate_period is the
     * drum's full rotation in steps (0 disables wraparound -- a single fixed
     * window suffices when a test never searches for a gate's other side). */
    int64_t gate_center[KM_MAX_LANES];
    int64_t gate_half_width;
    int64_t gate_period;
} fixture_t;

/* LINEAR, 12 lanes, the shared gear also bound to lane 10 (KM_MAX_LANES=12).
 * Small motion numbers throughout, same style as tests/test_selector_home.c. */
static void linear_profile(km_device_profile_t *p)
{
    km_profile_init_boxturtle(p);
    p->name = "Selector-Linear-Test";
    p->lane_count = 12;
    p->has_selector = true;
    p->has_powered_rewind = false; /* Shared-drive selector machines have no rewinder. */
    p->rewind_pwm_duty_permille = 0;
    p->selector.kind = KM_SELECTOR_LINEAR;
    p->selector.home.speed_steps_s = 1000;
    p->selector.home.acceleration_steps_s2 = 10000;
    p->selector.home.deceleration_steps_s2 = 10000;
    p->selector.home.max_travel_steps = 20;
    p->selector.home.deadline_us = 2000000;
    p->selector.home.max_switch_age_us = 1000;
    p->selector.home.home_direction = KM_DIR_REVERSE;
    p->selector.velocity_steps_s = 1000;
    p->selector.acceleration_steps_s2 = 10000;
    p->selector.gate_steps[0] = 2;
    p->selector.gate_steps[10] = 8;
    p->selector.has_servo = true;
    p->selector.servo_engage_us = 1400;
    p->selector.servo_release_us = 1900;
    p->selector.servo_move_us = 1100;
    p->selector.servo_dwell_ms = 5;
}

static void rotary_profile(km_device_profile_t *p)
{
    linear_profile(p);
    p->name = "Selector-Rotary-Test";
    p->selector.kind = KM_SELECTOR_ROTARY;
    p->selector.has_servo = false;
    p->selector.servo_engage_us = 0;
    p->selector.servo_release_us = 0;
    p->selector.servo_move_us = 0;
    p->selector.servo_dwell_ms = 0;
    p->selector.gate_steps[0] = 4;
    p->selector.gate_steps[10] = 8;
    /* Lane 0's single gear motor drives it the opposite way (Happy Hare
     * selector_gate_directions); lane 10 is the normal sense. */
    p->selector.gate_direction[0] = KM_DIR_REVERSE;
    p->selector.gate_direction[10] = KM_DIR_FORWARD;
    p->selector.release_gate[0] = 10;
    p->selector.release_gate[10] = 0;
}

/* INDEXED: the real ViViD dossier's own gate count and physical order
 * (docs/devices/vivid.json: 4 gates, selector_gate_order 0,3,1,2) -- unlike
 * LINEAR/ROTARY/SERVO's 12-lane fixtures, gate_order's index math treats
 * every one of profile.lane_count lanes as one circle, so this profile uses
 * ViViD's own real lane count rather than padding it out to KM_MAX_LANES. */
static void indexed_profile(km_device_profile_t *p)
{
    km_profile_init_boxturtle(p);
    p->name = "Selector-Indexed-Test";
    p->lane_count = 4;
    p->has_selector = true;
    p->has_powered_rewind = false;
    p->rewind_pwm_duty_permille = 0;
    p->selector.kind = KM_SELECTOR_INDEXED;
    p->selector.home.speed_steps_s = 1000;
    p->selector.home.acceleration_steps_s2 = 10000;
    p->selector.home.deceleration_steps_s2 = 10000;
    p->selector.home.max_travel_steps = 200;
    p->selector.home.deadline_us = 2000000;
    p->selector.home.max_switch_age_us = 1000;
    p->selector.home.home_direction = KM_DIR_FORWARD; /* Only validated; indexed_direction() picks the real one. */
    p->selector.velocity_steps_s = 1000;
    p->selector.acceleration_steps_s2 = 10000;
    p->selector.center_nudge_steps = 2;
    static const uint8_t order[4] = {0, 3, 1, 2};
    memcpy(p->selector.gate_order, order, sizeof(order));
}

/* AFC's own selector (Claymore, extras/AFC_vivid.py:159-199 select_lane):
 * no gate_order at all, so no calibrated switch width to center with either
 * (selector_cal_dis is optional and unset by default) -- selector.h's own
 * all-zero sentinel for "neither is known". */
static void forward_only_profile(km_device_profile_t *p)
{
    indexed_profile(p);
    p->name = "Selector-Indexed-ForwardOnly-Test";
    memset(p->selector.gate_order, 0, sizeof(p->selector.gate_order));
    p->selector.center_nudge_steps = 0;
    p->selector.unselect_steps = 30;
}

static void servo_profile(km_device_profile_t *p)
{
    km_profile_init_boxturtle(p);
    p->name = "Selector-Servo-Test";
    p->lane_count = 12;
    p->has_selector = true;
    p->has_powered_rewind = false;
    p->rewind_pwm_duty_permille = 0;
    p->selector.kind = KM_SELECTOR_SERVO;
    p->selector.has_servo = true;
    for (uint8_t i = 0; i < p->lane_count; ++i) p->selector.servo_gate_us[i] = (uint16_t)(600 + i * 100);
    p->selector.servo_dwell_ms = 5;
}

static void sample(fixture_t *f);

static void setup(fixture_t *f, const km_device_profile_t *profile_in)
{
    memset(f, 0, sizeof(*f));
    km_device_profile_t p = *profile_in;
    assert(km_core_init(&f->core, &p) == KM_OK);
    mock_driver_init(&f->feeder, 2, 10, false);
    for (uint8_t lane = 0; lane < p.lane_count; ++lane)
        assert(km_core_bind_lane_driver(&f->core, lane, &f->feeder.base, NULL) == KM_OK);
    if (p.selector.kind != KM_SELECTOR_SERVO) mock_driver_init(&f->selector, 2, 10, false);
    fake_servo_init(&f->servo);
    assert(km_core_bind_selector(&f->core,
        p.selector.kind == KM_SELECTOR_SERVO ? NULL : &f->selector.base,
        p.selector.has_servo ? &f->servo.base : NULL) == KM_OK);
    for (uint8_t lane = 0; lane < p.lane_count; ++lane) f->core.filament_present_bay[lane] = true;
    km_core_host_comm_ready(&f->core, 0);
    km_fake_clock_init(&f->clock, 0);
    sample(f);
}

static void sample(fixture_t *f)
{
    km_operation_core_t *c = &f->core;
    for (uint8_t lane = 0; lane < c->profile.lane_count; ++lane)
        km_core_set_sensors_at(c, lane, c->filament_present_bay[lane],
            c->filament_present_hub, c->filament_present_toolhead, true, f->ms);
    km_core_set_encoder_at(c, c->encoder_clicks, true, f->ms);
    assert(km_buffer_update_switches(&c->buffer_sensor, false, false, f->ms) == KM_OK);
    bool asserted = f->endstop_trigger != 0
        && km_trajectory_get_position(&c->trajectory) <= f->endstop_trigger;
    km_core_set_selector_endstop_at(c, asserted, true, f->ms * 1000u);
    if (c->profile.selector.kind == KM_SELECTOR_INDEXED) {
        int64_t raw = f->clock.ref.position;
        for (uint8_t g = 0; g < c->profile.lane_count; ++g) {
            int64_t d = raw - f->gate_center[g];
            if (f->gate_period > 0) {
                d %= f->gate_period;
                if (d > f->gate_period / 2) d -= f->gate_period;
                if (d < -(f->gate_period / 2)) d += f->gate_period;
            }
            /* gate_half_width==0 disables the model (setup()'s own first
             * sample, before a test has chosen real gate positions), same as
             * endstop_trigger==0 above; a real half-width is always > 0
             * (device_profile.c refuses center_nudge_steps==0). */
            bool hit = f->gate_half_width > 0 && (d < 0 ? -d : d) <= f->gate_half_width;
            km_core_set_selector_gate_switch_at(c, g, hit, true, f->ms * 1000u);
        }
    }
}

static void advance(fixture_t *f, uint32_t duration)
{
    for (uint32_t i = 0; i < duration; ++i) {
        ++f->ms;
        assert(km_fake_clock_run_trajectory(&f->clock, &f->core.trajectory, f->ms * 1000u) == KM_OK);
        sample(f);
        km_core_update(&f->core, f->ms, f->ms * 1000u);
        /* km_selector_home_start/found() and select_bind_stepper both call
         * km_trajectory_recover to re-baseline the shared trajectory's model
         * position (home to 0; the selector axis to its own last position,
         * independent of whatever the gear left it at). The fake clock's own
         * reference stepper has no idea a reset just happened and keeps
         * counting from wherever it was, same as a real driver's own step
         * counter (mcu/stepdir_gpio.c's s->position) -- km_trajectory_t's
         * own position_offset (src/trajectory.c, set by recover) reconciles
         * that now, so no test-side rebase is needed here any more. */
    }
}

static km_event_t event(fixture_t *f, km_event_type_t type)
{
    km_event_t e;
    assert(km_event_queue_pop(&f->core.event_queue, &e) == KM_OK);
    assert(e.type == type);
    return e;
}

static km_core_timepoint_t now(fixture_t *f) { return (km_core_timepoint_t){f->ms, f->ms * 1000u}; }

/* Auto-home (no prior km_core_request_start(HOME)) then move, on the very
 * first LOAD; the LINEAR grip engages, then the gear feed's own first stage
 * starts. Also exercises the shared gear (lane 10 of the 12-lane profile)
 * and km_core_set_selector_gate_steps replacing the compiled-in positions. */
static void test_auto_home_then_load(void)
{
    km_device_profile_t p; linear_profile(&p);
    fixture_t f; setup(&f, &p);
    f.endstop_trigger = -3; /* Within max_travel_steps=20, home_direction REVERSE. */
    assert(!f.core.selector_homed);
    assert(km_core_request_load(&f.core, KM_CLIENT_HOST, 1, 1, 10, now(&f)) == KM_REQ_ACCEPTED);
    assert(f.core.stage == KM_STAGE_SELECT_HOME);
    for (int i = 0; i < 200 && f.core.stage != KM_STAGE_LOAD_FEED_TO_HUB; ++i) advance(&f, 1);
    assert(f.core.selector_homed);
    assert(f.core.selector_engaged && f.core.selector_gate == 10);
    assert(f.core.stage == KM_STAGE_LOAD_FEED_TO_HUB);
    assert(km_trajectory_get_position(&f.core.trajectory) == 8); /* Lane 10's compiled-in gate_steps. */
    assert(f.core.active_feeder == &f.feeder.base && f.feeder.base.enabled);
    /* Selecting the same, already-engaged lane again needs no further move:
     * a fresh LOAD request on lane 10 (once this one clears) stays homed and
     * engaged without re-running SELECT_HOME/SELECT_MOVE. */
    assert(f.core.selector_homed && f.core.selector_engaged);

    /* The host's calibrated table replaces the compiled-in position and
     * requires a fresh SELECT_MOVE before the next filament motion. */
    assert(km_core_request_cancel(&f.core, KM_CLIENT_HOST, 1, 1, now(&f)) == KM_REQ_ACCEPTED);
    advance(&f, 400);
    event(&f, KM_EVENT_LOAD_CANCELLED);
    int32_t steps[12] = {0};
    steps[10] = 5;
    assert(km_core_set_selector_gate_steps(&f.core, steps, 12) == KM_OK);
    assert(!f.core.selector_engaged);
    assert(km_core_request_load(&f.core, KM_CLIENT_HOST, 2, 1, 10, now(&f)) == KM_REQ_ACCEPTED);
    assert(f.core.selector_homed); /* Still homed; only the gate table changed. */
    for (int i = 0; i < 200 && f.core.stage != KM_STAGE_LOAD_FEED_TO_HUB; ++i) advance(&f, 1);
    assert(km_trajectory_get_position(&f.core.trajectory) == 5);
}

/* A selector resting on its endstop (a power-up anywhere, Happy Hare's
 * "clear off the physical endstop" case) backs off by gate 0's position,
 * then homes and moves to the gate as usual. */
static void test_home_from_pressed_endstop(void)
{
    km_device_profile_t p; linear_profile(&p);
    fixture_t f; setup(&f, &p);
    f.endstop_trigger = 1; /* Pressed at the power-up position, 0. */
    advance(&f, 10);
    assert(f.core.selector_endstop_valid && f.core.selector_endstop_asserted);
    assert(km_core_request_load(&f.core, KM_CLIENT_HOST, 1, 1, 10, now(&f)) == KM_REQ_ACCEPTED);
    assert(f.core.stage == KM_STAGE_SELECT_MOVE && f.core.select_backoff);
    for (int i = 0; i < 400 && f.core.stage != KM_STAGE_LOAD_FEED_TO_HUB; ++i) advance(&f, 1);
    assert(f.core.op_state != KM_OP_STATE_FAULT && f.core.selector_homed);
    assert(f.core.stage == KM_STAGE_LOAD_FEED_TO_HUB && f.core.selector_gate == 10);
}

/* The switch never asserts within max_travel_steps: homing fails, latches
 * KM_FAULT_SELECTOR and clears homed; the next selection homes again once
 * the fault is cleared. */
static void test_homing_failure(void)
{
    km_device_profile_t p; linear_profile(&p);
    fixture_t f; setup(&f, &p);
    f.endstop_trigger = 0; /* Never asserts. */
    assert(km_core_request_load(&f.core, KM_CLIENT_HOST, 1, 1, 0, now(&f)) == KM_REQ_ACCEPTED);
    for (int i = 0; i < 2100 && f.core.op_state != KM_OP_STATE_FAULT; ++i) advance(&f, 1);
    assert(f.core.op_state == KM_OP_STATE_FAULT);
    assert(f.core.system_fault == KM_FAULT_SELECTOR);
    assert(!f.core.selector_homed);
    event(&f, KM_EVENT_LOAD_FAILED);
    /* km_core_clear_fault refuses while position is uncertain (it always is
     * after a failed home); recovery goes through km_core_recover, the same
     * as any other physical-reconciliation path. */
    assert(km_core_clear_fault(&f.core) == KM_ERR_UNAVAILABLE);
    assert(km_core_recover(&f.core, 0, false, 0, now(&f)) == KM_OK);
    assert(km_core_clear_fault(&f.core) == KM_OK);
    f.endstop_trigger = -3;
    assert(km_core_request_load(&f.core, KM_CLIENT_HOST, 1, 2, 0, now(&f)) == KM_REQ_ACCEPTED);
    assert(f.core.stage == KM_STAGE_SELECT_HOME); /* Homes again; the fault did not stick. */
}

/* LINEAR: move-then-engage sequence with exact pulses and dwell timing, and
 * the release pulse after UNLOAD completes. */
static void test_linear_servo_sequence(void)
{
    km_device_profile_t p; linear_profile(&p);
    fixture_t f; setup(&f, &p);
    f.endstop_trigger = -3;
    assert(km_core_request_load(&f.core, KM_CLIENT_HOST, 1, 1, 0, now(&f)) == KM_REQ_ACCEPTED);
    assert(f.servo.calls == 1 && f.servo.last_pulse_us == 1100); /* servo_move_us before homing. */
    for (int i = 0; i < 200 && f.core.stage != KM_STAGE_SELECT_MOVE; ++i) advance(&f, 1);
    assert(f.core.selector_homed);
    for (int i = 0; i < 200 && f.core.stage != KM_STAGE_SERVO_DWELL; ++i) advance(&f, 1);
    assert(km_trajectory_get_position(&f.core.trajectory) == 2); /* Lane 0's gate_steps. */
    assert(f.servo.last_pulse_us == 1400); /* servo_engage_us on arrival. */
    uint32_t dwell_start = f.ms;
    advance(&f, 1);
    assert(f.core.stage == KM_STAGE_SERVO_DWELL); /* Still dwelling after 1 ms of a 5 ms dwell. */
    for (int i = 0; i < 20 && f.core.stage == KM_STAGE_SERVO_DWELL; ++i) advance(&f, 1);
    assert(f.ms - dwell_start >= p.selector.servo_dwell_ms);
    assert(f.core.selector_engaged && f.core.selector_gate == 0);
    assert(f.core.stage == KM_STAGE_LOAD_FEED_TO_HUB);
    f.core.filament_present_hub = true;
    for (int i = 0; i < 30 && f.core.stage != KM_STAGE_FOLLOW; ++i) advance(&f, 1);
    f.core.filament_present_toolhead = true;
    for (int i = 0; i < 30 && f.core.stage != KM_STAGE_FOLLOW; ++i) advance(&f, 1);
    assert(f.core.stage == KM_STAGE_FOLLOW);
    event(&f, KM_EVENT_LOAD_COMPLETE);
    assert(f.core.selector_engaged); /* LOAD stays engaged: FOLLOW needs the gear. */

    assert(km_core_request_unload(&f.core, KM_CLIENT_HOST, 1, 2, now(&f)) == KM_REQ_ACCEPTED);
    assert(f.core.selector_engaged); /* Same lane: unload needs no reselect. */
    f.core.filament_present_hub = false; f.core.filament_present_toolhead = false;
    for (int i = 0; i < 400 && f.core.stage != KM_STAGE_STOPPED; ++i) advance(&f, 1);
    event(&f, KM_EVENT_UNLOAD_COMPLETE);
    assert(!f.core.selector_engaged); /* UNLOAD releases the grip. */
    assert(f.servo.last_pulse_us == 1900); /* servo_release_us. */
}

/* SERVO kind: no stepper, a gate angle per lane selects it. */
static void test_servo_kind_gate_angle(void)
{
    km_device_profile_t p; servo_profile(&p);
    fixture_t f; setup(&f, &p);
    assert(km_core_request_load(&f.core, KM_CLIENT_HOST, 1, 1, 3, now(&f)) == KM_REQ_ACCEPTED);
    assert(f.core.stage == KM_STAGE_SERVO_DWELL);
    assert(f.servo.last_pulse_us == p.selector.servo_gate_us[3]);
    for (int i = 0; i < 20 && f.core.stage == KM_STAGE_SERVO_DWELL; ++i) advance(&f, 1);
    assert(!f.core.selector_homed); /* SERVO has no axis; selector_at() never needs it homed. */
    assert(f.core.selector_engaged && f.core.selector_gate == 3);
    assert(f.core.stage == KM_STAGE_LOAD_FEED_TO_HUB);
}

/* ROTARY: lane 0's gate_direction is REVERSE (Happy Hare selector_gate_directions:
 * the single gear motor drives this gate the opposite way), so every gear
 * motion for lane 0 emits inverted DIR -- the stepper's own count runs
 * backwards while feeding -- yet the core's position still moves forward, so
 * position deltas (CALIBRATE's HUB-to-arrival distance) keep their sign. After UNLOAD completes,
 * the selector moves to release_gate[0] (lane 10)'s position, releasing the
 * lane, before the terminal event. */
static void test_rotary_gear_direction_and_release(void)
{
    km_device_profile_t p; rotary_profile(&p);
    fixture_t f; setup(&f, &p);
    f.endstop_trigger = -3;
    assert(km_core_request_load(&f.core, KM_CLIENT_HOST, 1, 1, 0, now(&f)) == KM_REQ_ACCEPTED);
    for (int i = 0; i < 200 && f.core.stage != KM_STAGE_LOAD_FEED_TO_HUB; ++i) advance(&f, 1);
    assert(f.core.stage == KM_STAGE_LOAD_FEED_TO_HUB);
    assert(km_trajectory_get_position(&f.core.trajectory) == p.selector.gate_steps[0]);
    assert(f.core.trajectory.invert_direction); /* gate_direction[0] == REVERSE. */
    int64_t pos_at_gate = km_trajectory_get_position(&f.core.trajectory);
    int64_t raw_at_gate = f.clock.ref.position;
    advance(&f, 200); /* Feeding (a logically-forward command) continues. */
    assert(f.clock.ref.position < raw_at_gate); /* The gear's pulses ran backwards... */
    assert(km_trajectory_get_position(&f.core.trajectory) > pos_at_gate); /* ...a forward feed. */
    f.core.filament_present_hub = true;
    for (int i = 0; i < 30 && f.core.stage != KM_STAGE_FOLLOW; ++i) advance(&f, 1);
    f.core.filament_present_toolhead = true;
    for (int i = 0; i < 30 && f.core.stage != KM_STAGE_FOLLOW; ++i) advance(&f, 1);
    assert(f.core.stage == KM_STAGE_FOLLOW); /* Completed normally despite the inverted gear. */
    event(&f, KM_EVENT_LOAD_COMPLETE);

    assert(km_core_request_unload(&f.core, KM_CLIENT_HOST, 1, 2, now(&f)) == KM_REQ_ACCEPTED);
    assert(f.core.trajectory.invert_direction); /* Still lane 0's gear while retracting. */
    f.core.filament_present_hub = false; f.core.filament_present_toolhead = false;
    for (int i = 0; i < 700 && !(f.core.stage == KM_STAGE_STOPPED && !f.core.selector_engaged); ++i)
        advance(&f, 1);
    event(&f, KM_EVENT_UNLOAD_COMPLETE);
    assert(!f.core.selector_engaged);
    /* release_gate[0] == 10: the selector parks at lane 10's position, not
     * lane 0's, and lane 10's own (non-inverted) gear direction applies there. */
    assert(km_trajectory_get_position(&f.core.trajectory) == p.selector.gate_steps[10]);
    assert(!f.core.trajectory.invert_direction);
}

/* PRELOAD on a selector machine parks behind the shared HUB sensor and
 * clears path_occupied on completion (item 3), so Happy Hare's "preload
 * every gate" works: PRELOAD lane 1, then PRELOAD lane 3 (a different lane,
 * needing the selector to move), then LOAD lane 1 -- all admitted. */
static void test_selector_preload_every_gate(void)
{
    km_device_profile_t p; linear_profile(&p);
    fixture_t f; setup(&f, &p);
    f.endstop_trigger = -3;
    assert(km_core_request_preload(&f.core, KM_CLIENT_HOST, 1, 1, 1, now(&f)) == KM_REQ_ACCEPTED);
    f.core.filament_present_hub = true;
    for (int i = 0; i < 200 && f.core.stage != KM_STAGE_UNLOAD_RETRACT_TO_HUB; ++i) advance(&f, 1);
    assert(f.core.stage == KM_STAGE_UNLOAD_RETRACT_TO_HUB); /* Retracting until HUB clears. */
    f.core.filament_present_hub = false;
    for (int i = 0; i < 400 && f.core.stage != KM_STAGE_STOPPED; ++i) advance(&f, 1);
    km_event_t e1 = event(&f, KM_EVENT_PRELOAD_COMPLETE);
    assert(e1.lane_idx == 1);
    assert(!f.core.path_occupied); /* Clear: the selector can move to another lane. */
    assert(f.core.op_state == KM_OP_STATE_PRELOADED);

    assert(km_core_request_preload(&f.core, KM_CLIENT_HOST, 1, 2, 3, now(&f)) == KM_REQ_ACCEPTED);
    f.core.filament_present_hub = true;
    for (int i = 0; i < 200 && f.core.stage != KM_STAGE_UNLOAD_RETRACT_TO_HUB; ++i) advance(&f, 1);
    f.core.filament_present_hub = false;
    for (int i = 0; i < 400 && f.core.stage != KM_STAGE_STOPPED; ++i) advance(&f, 1);
    km_event_t e3 = event(&f, KM_EVENT_PRELOAD_COMPLETE);
    assert(e3.lane_idx == 3);
    assert(!f.core.path_occupied);

    assert(km_core_request_load(&f.core, KM_CLIENT_HOST, 1, 3, 1, now(&f)) == KM_REQ_ACCEPTED);
    for (int i = 0; i < 200 && f.core.stage != KM_STAGE_LOAD_FEED_TO_HUB; ++i) advance(&f, 1);
    assert(f.core.stage == KM_STAGE_LOAD_FEED_TO_HUB);
    assert(f.core.active_lane == 1);
}

/* The selector never moves while filament is in the shared path, except that
 * the already-selected lane needs no move. */
static void test_interlock_path_occupied(void)
{
    km_device_profile_t p; linear_profile(&p);
    fixture_t f; setup(&f, &p);
    f.endstop_trigger = -3;
    assert(km_core_request_load(&f.core, KM_CLIENT_HOST, 1, 1, 0, now(&f)) == KM_REQ_ACCEPTED);
    f.core.filament_present_hub = true;
    for (int i = 0; i < 400 && f.core.stage != KM_STAGE_FOLLOW; ++i) advance(&f, 1);
    f.core.filament_present_toolhead = true;
    for (int i = 0; i < 30 && f.core.stage != KM_STAGE_FOLLOW; ++i) advance(&f, 1);
    assert(f.core.stage == KM_STAGE_FOLLOW && f.core.path_occupied);
    event(&f, KM_EVENT_LOAD_COMPLETE);
    /* Lane 10 needs the selector to move while lane 0's filament still owns
     * the shared path (FOLLOW/path_occupied): refused. */
    assert(km_core_request_preload(&f.core, KM_CLIENT_HOST, 1, 2, 10, now(&f)) == KM_REQ_PATH_OCCUPIED);
}

/* HOME (KM_OP_TYPE_HOME=8), through km_core_request_start; refused while an
 * operation runs or the path is occupied; emits HOME_COMPLETE/HOME_FAILED.
 * The host sends lane 0xff (KM_LANE_UNKNOWN): accepted and echoed back. */
static void test_home_op(void)
{
    km_device_profile_t p; linear_profile(&p);
    fixture_t f; setup(&f, &p);
    f.endstop_trigger = -3;
    km_request_identity_t r = {KM_CLIENT_HOST, 1, 1, KM_OP_TYPE_HOME, KM_LANE_UNKNOWN};
    assert(km_core_request_start(&f.core, &r, KM_DIR_FORWARD, now(&f)) == KM_REQ_ACCEPTED);
    assert(f.core.active_lane == KM_LANE_UNKNOWN);
    assert(f.core.stage == KM_STAGE_SELECT_HOME);
    for (int i = 0; i < 200 && f.core.stage != KM_STAGE_STOPPED; ++i) advance(&f, 1);
    assert(f.core.selector_homed);
    km_event_t e = event(&f, KM_EVENT_HOME_COMPLETE);
    assert(e.lane_idx == KM_LANE_UNKNOWN && e.session_id == 1 && e.req_id == 1);
    /* item 2: HOME_COMPLETE's value is the net travel from where HOME started
     * (position 0, never homed before) to the switch (endstop_trigger=-3). */
    assert(e.value == 3);

    /* Refused while an operation runs. */
    assert(km_core_request_preload(&f.core, KM_CLIENT_HOST, 2, 1, 0, now(&f)) == KM_REQ_ACCEPTED);
    km_request_identity_t r2 = {KM_CLIENT_HOST, 3, 1, KM_OP_TYPE_HOME, KM_LANE_UNKNOWN};
    assert(km_core_request_start(&f.core, &r2, KM_DIR_FORWARD, now(&f)) == KM_REQ_BUSY);
    f.core.filament_present_hub = true;
    for (int i = 0; i < 200 && f.core.stage != KM_STAGE_UNLOAD_RETRACT_TO_HUB; ++i) advance(&f, 1);
    assert(f.core.stage == KM_STAGE_UNLOAD_RETRACT_TO_HUB); /* Reached HUB; now retracting clear. */
    f.core.filament_present_hub = false;
    for (int i = 0; i < 700 && f.core.stage != KM_STAGE_STOPPED; ++i) advance(&f, 1);
    event(&f, KM_EVENT_PRELOAD_COMPLETE);
    /* A selector-machine PRELOAD retracts clear on its own (item 3), so
     * nothing is occupied here to refuse HOME with -- reconcile a stray
     * filament reading the way an unplanned boot with filament present
     * would, and confirm HOME still refuses to move with it there. */
    assert(!f.core.path_occupied);
    f.core.path_occupied = true; f.core.path_occupant_lane = 0;
    assert(km_core_request_start(&f.core, &r2, KM_DIR_FORWARD, now(&f)) == KM_REQ_PATH_OCCUPIED);
    f.core.path_occupied = false;

    /* Homing failure through the explicit op: latches KM_FAULT_SELECTOR,
     * clears homed, and emits HOME_FAILED. */
    km_device_profile_t p2; linear_profile(&p2);
    fixture_t g; setup(&g, &p2);
    g.endstop_trigger = 0;
    km_request_identity_t r3 = {KM_CLIENT_HOST, 1, 1, KM_OP_TYPE_HOME, KM_LANE_UNKNOWN};
    assert(km_core_request_start(&g.core, &r3, KM_DIR_FORWARD, now(&g)) == KM_REQ_ACCEPTED);
    for (int i = 0; i < 2100 && g.core.op_state != KM_OP_STATE_FAULT; ++i) advance(&g, 1);
    assert(!g.core.selector_homed && g.core.system_fault == KM_FAULT_SELECTOR);
    km_event_t fe = event(&g, KM_EVENT_HOME_FAILED);
    assert(fe.lane_idx == KM_LANE_UNKNOWN);
}

/* item 2, SERVO kind: no selector axis, so HOME completes synchronously
 * (no SELECT_HOME stage at all) and reports zero travel. */
static void test_home_op_servo_kind(void)
{
    km_device_profile_t p; servo_profile(&p);
    fixture_t f; setup(&f, &p);
    km_request_identity_t r = {KM_CLIENT_HOST, 1, 1, KM_OP_TYPE_HOME, KM_LANE_UNKNOWN};
    assert(km_core_request_start(&f.core, &r, KM_DIR_FORWARD, now(&f)) == KM_REQ_ACCEPTED);
    assert(f.core.stage == KM_STAGE_STOPPED && f.core.selector_homed);
    km_event_t e = event(&f, KM_EVENT_HOME_COMPLETE);
    assert(e.lane_idx == KM_LANE_UNKNOWN && e.value == 0);
}

/* item 2, backoff case: a standalone HOME that starts already resting on the
 * switch backs off by gate 0's position and re-approaches, finding the
 * switch again close to where it started. HOME_COMPLETE's value is that net
 * travel across the whole HOME request, not the backoff-and-return distance
 * (which would be gate_steps[0]=2 if it were measured from after the
 * backoff instead). */
static void test_home_op_reports_travel_from_pressed_endstop(void)
{
    km_device_profile_t p; linear_profile(&p);
    fixture_t f; setup(&f, &p);
    f.endstop_trigger = 1; /* Pressed at the power-up position, 0. */
    advance(&f, 10);
    assert(f.core.selector_endstop_valid && f.core.selector_endstop_asserted);
    km_request_identity_t r = {KM_CLIENT_HOST, 1, 1, KM_OP_TYPE_HOME, KM_LANE_UNKNOWN};
    assert(km_core_request_start(&f.core, &r, KM_DIR_FORWARD, now(&f)) == KM_REQ_ACCEPTED);
    assert(f.core.stage == KM_STAGE_SELECT_MOVE && f.core.select_backoff);
    for (int i = 0; i < 400 && f.core.stage != KM_STAGE_STOPPED; ++i) advance(&f, 1);
    assert(f.core.selector_homed);
    km_event_t e = event(&f, KM_EVENT_HOME_COMPLETE);
    assert(e.value < (uint32_t)p.selector.gate_steps[0]); /* Less than the 2-step backoff itself. */
}

/* item 1: motors off (any explicit_stop) unhomes -- a stale homed flag is
 * never trusted once the selector driver has lost holding torque, matching
 * Happy Hare's own disable_motors/_invalidate_persisted_position. The next
 * selection homes again automatically. */
static void test_explicit_stop_unhomes_selector(void)
{
    km_device_profile_t p; linear_profile(&p);
    fixture_t f; setup(&f, &p);
    f.endstop_trigger = -3;
    assert(km_core_request_load(&f.core, KM_CLIENT_HOST, 1, 1, 10, now(&f)) == KM_REQ_ACCEPTED);
    for (int i = 0; i < 200 && f.core.stage != KM_STAGE_LOAD_FEED_TO_HUB; ++i) advance(&f, 1);
    assert(f.core.selector_homed);
    km_core_explicit_stop(&f.core, KM_FAULT_EMERGENCY_STOP, f.ms);
    assert(!f.core.selector_homed);
    event(&f, KM_EVENT_LOAD_FAILED);
    assert(km_core_recover(&f.core, 0, false, 0, now(&f)) == KM_OK);
    f.endstop_trigger = -3;
    assert(km_core_request_load(&f.core, KM_CLIENT_HOST, 1, 2, 10, now(&f)) == KM_REQ_ACCEPTED);
    assert(f.core.stage == KM_STAGE_SELECT_HOME); /* Homes again; the stop cleared it. */
}

/* INDEXED: HOME always seeks gate 0's own switch; the search runs at
 * KM_DIR_FORWARD (no prior gate known, mmu_indexed_selector.py's lgate_
 * selected is None -> +1), finds it at the real hardware step count 20, then
 * centers by center_nudge_steps=2 in that same direction. HOME_COMPLETE's
 * value is the whole trip (search + center), unlike km_trajectory_get_
 * position, which found() itself keeps rebasing to 0 at every gate. */
static void test_indexed_home_to_gate_zero(void)
{
    km_device_profile_t p; indexed_profile(&p);
    fixture_t f; setup(&f, &p);
    f.gate_half_width = 1;
    f.gate_center[0] = 20;
    km_request_identity_t r = {KM_CLIENT_HOST, 1, 1, KM_OP_TYPE_HOME, KM_LANE_UNKNOWN};
    assert(km_core_request_start(&f.core, &r, KM_DIR_FORWARD, now(&f)) == KM_REQ_ACCEPTED);
    assert(f.core.stage == KM_STAGE_SELECT_HOME);
    for (int i = 0; i < 500 && f.core.stage != KM_STAGE_STOPPED; ++i) advance(&f, 1);
    assert(f.core.selector_homed);
    assert(f.core.selector_gate == 0 && f.core.selector_engaged); /* HH's lgate_selected = 0 after home. */
    km_event_t e = event(&f, KM_EVENT_HOME_COMPLETE);
    assert(e.value > 20 && e.value <= 23); /* ~20 to find the switch, 2 more to center on it. */
}

/* INDEXED direction choice along the real ViViD gate_order (0,3,1,2): from
 * gate 0, gate 2 sits at order-index 3 (forward distance 3, reverse 1) so
 * the seek runs REVERSE (decreasing raw steps); from gate 2, gate 3 is a
 * forward/reverse tie (distance 2 either way), which best_rotation_direction
 * breaks toward forward. Both selections land exactly on the target's own
 * centered position, and the shared gear (lane 10) is untouched by any of
 * this -- INDEXED has no per-gate gear direction/release, unlike ROTARY. */
static void test_indexed_direction_and_centering(void)
{
    km_device_profile_t p; indexed_profile(&p);
    fixture_t f; setup(&f, &p);
    f.gate_half_width = 1; f.gate_period = 80; /* 4 gates * 20-step pitch = one full rotation. */
    f.gate_center[0] = 20; f.gate_center[3] = 40; f.gate_center[1] = 60; f.gate_center[2] = 80;
    assert(km_core_request_preload(&f.core, KM_CLIENT_HOST, 1, 1, 0, now(&f)) == KM_REQ_ACCEPTED);
    f.core.filament_present_hub = true;
    for (int i = 0; i < 500 && f.core.stage != KM_STAGE_UNLOAD_RETRACT_TO_HUB; ++i) advance(&f, 1);
    assert(f.core.selector_gate == 0);
    f.core.filament_present_hub = false;
    for (int i = 0; i < 400 && f.core.stage != KM_STAGE_STOPPED; ++i) advance(&f, 1);
    event(&f, KM_EVENT_PRELOAD_COMPLETE);

    int64_t raw_before = f.clock.ref.position;
    assert(km_core_request_preload(&f.core, KM_CLIENT_HOST, 1, 2, 2, now(&f)) == KM_REQ_ACCEPTED);
    f.core.filament_present_hub = true;
    for (int i = 0; i < 500 && f.core.stage != KM_STAGE_UNLOAD_RETRACT_TO_HUB; ++i) advance(&f, 1);
    assert(f.core.selector_gate == 2);
    assert(f.clock.ref.position < raw_before); /* Reverse: the shorter way from gate 0 to gate 2 (1 hop vs 3). */
    f.core.filament_present_hub = false;
    for (int i = 0; i < 400 && f.core.stage != KM_STAGE_STOPPED; ++i) advance(&f, 1);
    event(&f, KM_EVENT_PRELOAD_COMPLETE);

    raw_before = f.clock.ref.position;
    assert(km_core_request_preload(&f.core, KM_CLIENT_HOST, 1, 3, 3, now(&f)) == KM_REQ_ACCEPTED);
    f.core.filament_present_hub = true;
    for (int i = 0; i < 500 && f.core.stage != KM_STAGE_UNLOAD_RETRACT_TO_HUB; ++i) advance(&f, 1);
    assert(f.core.selector_gate == 3);
    assert(f.clock.ref.position > raw_before); /* Tie (2 hops either way) broken forward, gate 2 to gate 3. */
}

/* AFC's own selector never picks a shorter reverse hop (empty gate_order,
 * selector.h's sentinel): from gate 3, gate 1 sits one hop back but three
 * hops forward, and forward-only always takes the three. Zero-distance
 * centering (center_nudge_steps=0) still lands exactly on the switch's own
 * leading edge, never faulting on a null move. */
static void test_indexed_forward_only_selection(void)
{
    km_device_profile_t p; forward_only_profile(&p);
    assert(km_profile_validate(&p) == KM_OK);

    fixture_t f; setup(&f, &p);
    /* None of these sit at raw position 0, so gate 0's switch is not already
     * asserted at power-up (a different scenario, test_indexed_home_from_
     * pressed_switch above). */
    f.gate_half_width = 1; f.gate_period = 80; /* 4 gates * 20-step pitch = one full rotation. */
    f.gate_center[0] = 20; f.gate_center[1] = 40; f.gate_center[2] = 60; f.gate_center[3] = 80;

    assert(km_core_request_preload(&f.core, KM_CLIENT_HOST, 1, 1, 3, now(&f)) == KM_REQ_ACCEPTED);
    assert(f.core.stage == KM_STAGE_SELECT_HOME); /* Auto-homes to gate 0 first, forward. */
    f.core.filament_present_hub = true;
    for (int i = 0; i < 500 && f.core.stage != KM_STAGE_UNLOAD_RETRACT_TO_HUB; ++i) advance(&f, 1);
    assert(f.core.selector_gate == 3);
    f.core.filament_present_hub = false;
    for (int i = 0; i < 400 && f.core.stage != KM_STAGE_STOPPED; ++i) advance(&f, 1);
    event(&f, KM_EVENT_PRELOAD_COMPLETE);

    int64_t raw_before = f.clock.ref.position;
    assert(km_core_request_preload(&f.core, KM_CLIENT_HOST, 1, 2, 1, now(&f)) == KM_REQ_ACCEPTED);
    f.core.filament_present_hub = true;
    for (int i = 0; i < 500 && f.core.stage != KM_STAGE_UNLOAD_RETRACT_TO_HUB; ++i) advance(&f, 1);
    assert(f.core.selector_gate == 1);
    assert(f.clock.ref.position > raw_before); /* Forward all the way around, not the shorter reverse hop. */
}

/* INDEXED, boot-pressed case: gate 0's switch already asserted at power-up
 * clears first (this firmware's own convention -- the full calibrated
 * switch width, opposite the search direction -- selector_home.c refuses to
 * start a fresh search on an already-asserted switch), then finds and
 * centers as usual. */
static void test_indexed_home_from_pressed_switch(void)
{
    km_device_profile_t p; indexed_profile(&p);
    fixture_t f; setup(&f, &p);
    f.gate_half_width = 1;
    f.gate_center[0] = 0; /* Pressed at the power-up position. */
    advance(&f, 5);
    assert(f.core.selector_gate_valid[0] && f.core.selector_gate_asserted[0]);
    km_request_identity_t r = {KM_CLIENT_HOST, 1, 1, KM_OP_TYPE_HOME, KM_LANE_UNKNOWN};
    assert(km_core_request_start(&f.core, &r, KM_DIR_FORWARD, now(&f)) == KM_REQ_ACCEPTED);
    assert(f.core.stage == KM_STAGE_SELECT_MOVE && f.core.select_backoff);
    for (int i = 0; i < 500 && f.core.stage != KM_STAGE_STOPPED; ++i) advance(&f, 1);
    assert(f.core.selector_homed && f.core.selector_gate == 0);
    event(&f, KM_EVENT_HOME_COMPLETE);
}

/* INDEXED without a gate order (AFC, Claymore), boot-pressed case: there is
 * no switch width to back off by, so it moves FORWARD by unselect_steps
 * (AFC unselect_lane) and searches forward, wrapping round to gate 0 again,
 * instead of faulting a switch that never clears. */
static void test_indexed_forward_only_home_from_pressed_switch(void)
{
    km_device_profile_t p; forward_only_profile(&p);
    fixture_t f; setup(&f, &p);
    f.gate_half_width = 1; f.gate_period = 80;
    f.gate_center[0] = 0; f.gate_center[1] = 20; f.gate_center[2] = 40; f.gate_center[3] = 60;
    advance(&f, 5);
    assert(f.core.selector_gate_valid[0] && f.core.selector_gate_asserted[0]);
    km_request_identity_t r = {KM_CLIENT_HOST, 1, 1, KM_OP_TYPE_HOME, KM_LANE_UNKNOWN};
    assert(km_core_request_start(&f.core, &r, KM_DIR_FORWARD, now(&f)) == KM_REQ_ACCEPTED);
    assert(f.core.stage == KM_STAGE_SELECT_MOVE && f.core.select_backoff);
    assert(f.core.select_target_steps == 30); /* Forward, AFC's unselect distance. */
    for (int i = 0; i < 1000 && f.core.stage != KM_STAGE_STOPPED; ++i) advance(&f, 1);
    assert(f.core.op_state != KM_OP_STATE_FAULT);
    assert(f.core.selector_homed && f.core.selector_gate == 0);
    assert(f.core.selector_position_steps > 30); /* Found gate 0 again going forward. */
    event(&f, KM_EVENT_HOME_COMPLETE);
}

/* INDEXED: the switch that never asserts within max_travel_steps faults the
 * same way as LINEAR/ROTARY's endstop; recovery homes again. */
static void test_indexed_homing_failure(void)
{
    km_device_profile_t p; indexed_profile(&p);
    fixture_t f; setup(&f, &p);
    f.gate_half_width = 1; f.gate_center[0] = 1000000; /* Unreachable within max_travel_steps=200. */
    assert(km_core_request_load(&f.core, KM_CLIENT_HOST, 1, 1, 0, now(&f)) == KM_REQ_ACCEPTED);
    for (int i = 0; i < 2100 && f.core.op_state != KM_OP_STATE_FAULT; ++i) advance(&f, 1);
    assert(f.core.op_state == KM_OP_STATE_FAULT && f.core.system_fault == KM_FAULT_SELECTOR);
    assert(!f.core.selector_homed);
    event(&f, KM_EVENT_LOAD_FAILED);
    assert(km_core_recover(&f.core, 0, false, 0, now(&f)) == KM_OK);
    assert(km_core_clear_fault(&f.core) == KM_OK);
    f.gate_half_width = 1; f.gate_center[0] = 20;
    assert(km_core_request_load(&f.core, KM_CLIENT_HOST, 1, 2, 0, now(&f)) == KM_REQ_ACCEPTED);
    assert(f.core.stage == KM_STAGE_SELECT_HOME); /* Homes again; the fault did not stick. */
}

/* INDEXED: re-selecting the gate already trusted (software position, same
 * as LINEAR/ROTARY's own zero-distance re-select) needs no motion, even
 * right after a PRELOAD released engagement without moving the selector. */
static void test_indexed_reselect_same_gate_is_free(void)
{
    km_device_profile_t p; indexed_profile(&p);
    fixture_t f; setup(&f, &p);
    f.gate_half_width = 1; f.gate_center[0] = 20;
    assert(km_core_request_preload(&f.core, KM_CLIENT_HOST, 1, 1, 0, now(&f)) == KM_REQ_ACCEPTED);
    f.core.filament_present_hub = true;
    for (int i = 0; i < 500 && f.core.stage != KM_STAGE_UNLOAD_RETRACT_TO_HUB; ++i) advance(&f, 1);
    f.core.filament_present_hub = false;
    for (int i = 0; i < 400 && f.core.stage != KM_STAGE_STOPPED; ++i) advance(&f, 1);
    event(&f, KM_EVENT_PRELOAD_COMPLETE);
    assert(f.core.selector_gate == 0 && !f.core.selector_engaged); /* Released, not moved. */

    int64_t raw_before = f.clock.ref.position;
    assert(km_core_request_load(&f.core, KM_CLIENT_HOST, 1, 2, 0, now(&f)) == KM_REQ_ACCEPTED);
    for (int i = 0; i < 20 && f.core.stage != KM_STAGE_LOAD_FEED_TO_HUB; ++i) advance(&f, 1);
    assert(f.core.stage == KM_STAGE_LOAD_FEED_TO_HUB);
    assert(f.clock.ref.position == raw_before); /* No selector motion at all. */
}

/* device_profile.c's INDEXED validation: gate_order must be a permutation of
 * every lane, center_nudge_steps must be a real in-range distance, and
 * gate_steps stays unused (no position table, klipper_mmu/selector.h). */
static void test_indexed_profile_validation(void)
{
    km_device_profile_t p; indexed_profile(&p);
    assert(km_profile_validate(&p) == KM_OK);

    km_device_profile_t bad = p;
    bad.selector.gate_order[1] = bad.selector.gate_order[0]; /* Duplicate, not a permutation. */
    assert(km_profile_validate(&bad) == KM_ERR_INVALID_PARAM);

    bad = p; bad.selector.gate_order[1] = 4; /* Past lane_count=4. */
    assert(km_profile_validate(&bad) == KM_ERR_INVALID_PARAM);

    bad = p; bad.selector.center_nudge_steps = 0; /* Never invented; must be set. */
    assert(km_profile_validate(&bad) == KM_ERR_INVALID_PARAM);

    bad = p; bad.selector.center_nudge_steps = bad.selector.home.max_travel_steps;
    assert(km_profile_validate(&bad) == KM_ERR_INVALID_PARAM);

    bad = p; bad.selector.gate_steps[0] = 5; /* No position table for INDEXED. */
    assert(km_profile_validate(&bad) == KM_ERR_INVALID_PARAM);

    /* AFC's forward-only variant (Claymore): empty gate_order and a zero
     * nudge validate together, but a real order still demands its own
     * nonzero nudge (ViViD's rule keeps applying to gate_order once it is
     * no longer all-zero), and the nudge still cannot reach max_travel_steps. */
    km_device_profile_t fwd; forward_only_profile(&fwd);
    assert(km_profile_validate(&fwd) == KM_OK);

    bad = fwd; bad.selector.gate_order[0] = 1; /* One nonzero entry: a real (partial) order now. */
    assert(km_profile_validate(&bad) == KM_ERR_INVALID_PARAM); /* Not a permutation, and nudge is 0. */

    bad = fwd; bad.selector.center_nudge_steps = bad.selector.home.max_travel_steps;
    assert(km_profile_validate(&bad) == KM_ERR_INVALID_PARAM); /* Out of range even with no order. */
    bad = fwd; bad.selector.unselect_steps = 0;
    assert(km_profile_validate(&bad) == KM_ERR_INVALID_PARAM); /* No order needs an unselect distance. */
}

/* item 3: a gate without a calibrated position (LINEAR/ROTARY: -1) or angle
 * (SERVO: 0) is refused at admission ("gate N is not calibrated"), never
 * making the whole profile invalid; a calibrated neighbour is unaffected. */
static void test_uncalibrated_gate_refused(void)
{
    km_device_profile_t p; linear_profile(&p);
    p.selector.gate_steps[5] = -1; /* Never measured. */
    fixture_t f; setup(&f, &p);
    f.endstop_trigger = -3;
    assert(km_core_request_load(&f.core, KM_CLIENT_HOST, 1, 1, 5, now(&f)) == KM_REQ_UNAVAILABLE);
    assert(km_core_request_load(&f.core, KM_CLIENT_HOST, 1, 2, 0, now(&f)) == KM_REQ_ACCEPTED);

    km_device_profile_t sp; servo_profile(&sp);
    sp.selector.servo_gate_us[5] = 0; /* Never measured. */
    fixture_t g; setup(&g, &sp);
    assert(km_core_request_load(&g.core, KM_CLIENT_HOST, 1, 1, 5, now(&g)) == KM_REQ_UNAVAILABLE);
    assert(km_core_request_load(&g.core, KM_CLIENT_HOST, 1, 2, 3, now(&g)) == KM_REQ_ACCEPTED);
}

/* item 4: the runtime single-gate setter and SERVO test move are refused
 * unless the core is fully idle (no active op, not homing/selecting), and
 * write exactly the gate/angle asked -- disjoint from km_core_set_selector_gate_steps'
 * looser FOLLOW-permitting check. */
static void test_runtime_gate_set_idle_only(void)
{
    km_device_profile_t p; linear_profile(&p);
    fixture_t f; setup(&f, &p);
    f.endstop_trigger = -3;
    assert(km_core_set_selector_gate(&f.core, 5, 15) == KM_OK);
    assert(f.core.profile.selector.gate_steps[5] == 15);
    assert(km_core_set_selector_gate(&f.core, 5, -1) == KM_OK); /* Back to uncalibrated. */
    assert(f.core.profile.selector.gate_steps[5] == -1);
    assert(km_core_set_selector_gate(&f.core, 5, 999999) == KM_ERR_INVALID_PARAM); /* Past max_travel_steps=20. */

    assert(km_core_request_load(&f.core, KM_CLIENT_HOST, 1, 1, 10, now(&f)) == KM_REQ_ACCEPTED);
    assert(km_core_set_selector_gate(&f.core, 0, 5) == KM_ERR_BUSY); /* An op is running. */
    for (int i = 0; i < 200 && f.core.stage != KM_STAGE_LOAD_FEED_TO_HUB; ++i) advance(&f, 1);
    assert(km_core_set_selector_gate(&f.core, 0, 5) == KM_ERR_BUSY); /* FOLLOW-bound feed, still not idle. */

    km_device_profile_t sp; servo_profile(&sp);
    fixture_t g; setup(&g, &sp);
    assert(km_core_selector_servo_test(&g.core, 1234) == KM_OK);
    assert(g.servo.last_pulse_us == 1234);
    assert(!g.core.selector_engaged); /* A scratch jog, not a real selection. */
    assert(km_core_set_selector_gate(&g.core, 3, 1400) == KM_OK);
    assert(g.core.profile.selector.servo_gate_us[3] == 1400);
    assert(km_core_set_selector_gate(&g.core, 3, 499) == KM_ERR_INVALID_PARAM); /* Below the 500us floor. */
}

/* item 2 (Happy Hare's MMU_MOTORS_OFF): idle-only, releases the selector (and
 * the shared gear, still holding after a completed selection) without
 * faulting, and unhomes it so the operator can move it by hand before
 * KLIPPER_MMU_CALIBRATE_SELECTOR; the next selection re-homes. */
static void test_selector_motors_off(void)
{
    km_device_profile_t p; linear_profile(&p);
    fixture_t f; setup(&f, &p);
    f.endstop_trigger = -3;
    assert(km_core_request_preload(&f.core, KM_CLIENT_HOST, 1, 1, 10, now(&f)) == KM_REQ_ACCEPTED);
    assert(km_core_selector_motors_off(&f.core) == KM_ERR_BUSY); /* An op is running. */
    f.core.filament_present_hub = true;
    for (int i = 0; i < 200 && f.core.stage != KM_STAGE_UNLOAD_RETRACT_TO_HUB; ++i) advance(&f, 1);
    assert(km_core_selector_motors_off(&f.core) == KM_ERR_BUSY); /* Still retracting, not idle. */
    f.core.filament_present_hub = false;
    for (int i = 0; i < 400 && f.core.stage != KM_STAGE_STOPPED; ++i) advance(&f, 1);
    event(&f, KM_EVENT_PRELOAD_COMPLETE);
    assert(f.core.stage == KM_STAGE_STOPPED);
    assert(f.core.selector_homed && f.selector.base.enabled); /* Still holding after arrival. */
    assert(km_core_selector_motors_off(&f.core) == KM_OK);
    assert(!f.selector.base.enabled && !f.feeder.base.enabled);
    assert(!f.core.selector_homed && !f.core.selector_engaged);

    assert(km_core_request_load(&f.core, KM_CLIENT_HOST, 1, 2, 10, now(&f)) == KM_REQ_ACCEPTED);
    assert(f.core.stage == KM_STAGE_SELECT_HOME); /* Homes again. */
}

/* Regression (found on Pico MMU's whole-stack sim): the deferred op's own
 * first stage must start its stage_entered_time_ms/last_progress_time_ms
 * fresh, not keep whatever begin() stamped for the selection phase
 * (SELECT_HOME/SELECT_MOVE/SERVO_DWELL) that preceded it. A real servo dwell
 * (or selector travel) otherwise gets silently deducted from the very next
 * stage's own stage_timeout()/progress_timeout_ms budget: here a 50 ms dwell
 * would outrun a 40 ms LOAD_FEED_TO_HUB budget on literally the first tick
 * of that stage if resume_operation_motion() left the old timestamp in
 * place, exactly the spurious KM_FAULT_NO_PROGRESS ("the lane made no
 * progress") pico_mmu hit before its gear had fed at all. */
static void test_selection_phase_does_not_steal_stage_budget(void)
{
    km_device_profile_t p; servo_profile(&p);
    p.selector.servo_dwell_ms = 50;  /* Deliberately larger than... */
    p.hub_feed_timeout_ms = 40;      /* ...this stage's own budget. */
    fixture_t f; setup(&f, &p);
    assert(km_core_request_preload(&f.core, KM_CLIENT_HOST, 1, 1, 0, now(&f)) == KM_REQ_ACCEPTED);
    assert(f.core.stage == KM_STAGE_SERVO_DWELL);
    for (int i = 0; i < 100 && f.core.stage == KM_STAGE_SERVO_DWELL; ++i) advance(&f, 1);
    assert(f.core.stage == KM_STAGE_LOAD_FEED_TO_HUB);
    assert(f.core.op_state != KM_OP_STATE_FAULT);
    /* The new stage's own clock, not the dwell's leftover one. */
    assert(f.core.stage_entered_time_ms == f.ms);
    assert(f.core.last_progress_time_ms == f.ms);
    advance(&f, 5); /* Well inside the fresh 40 ms budget. */
    assert(f.core.op_state != KM_OP_STATE_FAULT);
}

int main(void)
{
    test_auto_home_then_load();
    test_home_from_pressed_endstop();
    test_homing_failure();
    test_linear_servo_sequence();
    test_servo_kind_gate_angle();
    test_rotary_gear_direction_and_release();
    test_selector_preload_every_gate();
    test_interlock_path_occupied();
    test_home_op();
    test_home_op_servo_kind();
    test_home_op_reports_travel_from_pressed_endstop();
    test_explicit_stop_unhomes_selector();
    test_indexed_home_to_gate_zero();
    test_indexed_direction_and_centering();
    test_indexed_forward_only_selection();
    test_indexed_forward_only_home_from_pressed_switch();
    test_indexed_home_from_pressed_switch();
    test_indexed_homing_failure();
    test_indexed_reselect_same_gate_is_free();
    test_indexed_profile_validation();
    test_uncalibrated_gate_refused();
    test_runtime_gate_set_idle_only();
    test_selector_motors_off();
    test_selection_phase_does_not_steal_stage_budget();
    puts("selector core tests passed");
    return 0;
}
