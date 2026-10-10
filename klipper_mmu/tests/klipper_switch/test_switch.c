// SPDX-License-Identifier: GPL-3.0-or-later
#include "switch_gpio.h"
#include "controller.h"
#include "clock.h"
#include "../fake_clock.h"
#include "../mock_driver.h"
#include "shim_klipper.h"
#include "board/irq.h"
#include "board/misc.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include <sys/wait.h>

static km_operation_core_t core;
static km_switch_gpio_config_t config;
static unsigned setups, reads;
static int32_t pulls[128];
static bool levels[128];
static uint32_t read_cost, fail_setup = UINT32_MAX, fail_read = UINT32_MAX;

struct gpio_in gpio_in_setup(uint32_t pin, int32_t pull)
{
    assert(!test_shim_irq_is_disabled());
    assert(pin < 128 && pull >= -1 && pull <= 1);
    assert(!test_shim_is_shutdown);
    ++setups;
    if (pin == fail_setup) shutdown("injected input setup failure");
    pulls[pin] = pull;
    return (struct gpio_in){pin ^ 0x5a5a};
}
uint8_t gpio_in_read(struct gpio_in g)
{
    uint32_t pin = g.encoded ^ 0x5a5a;
    assert(pin < 128 && !test_shim_irq_is_disabled() && !test_shim_is_shutdown); /* Task context. */
    ++reads;
    if (pin == fail_read) shutdown("injected input read failure");
    bool level = levels[pin];
    shim_advance_ticks(read_cost);
    return level ? 1u : 0u;
}
static void fixture_ex(unsigned lanes, bool analog, bool load, uint32_t hz)
{
    shim_reset(hz, 1, 1, lanes);
    shim_run_init();
    shim_set_board_ready(true);
    shim_run_task();
    km_device_profile_t profile;
    km_profile_init_boxturtle(&profile);
    profile.lane_count = (uint8_t)lanes;
    profile.has_proportional_buffer = analog;
    profile.has_load_sensors = load;
    profile.has_diff_calibration = analog;
    profile.proportional_gain_milli = analog ? 8000u : 0u;
    if (analog) profile.diff_calibration = (km_diff_adc_config_t){
        .channel_map = KM_DIFF_CH_A_LOW_B_HIGH, .vref_mv = 3300,
        .adc_max_counts = 4095, .path_tolerance_counts = 64, .point_count = 3,
        .points = {{0,2048,0},{2048,2048,500},{2048,4095,1000}}};
    assert(km_core_init(&core, &profile) == KM_OK);
    for (unsigned i = 0; i < lanes; ++i)
        config.bay[i] = (km_switch_input_t){(uint16_t)i, (int8_t)((int)(i % 3) - 1), i % 2 != 0};
    for (unsigned i = 0; i < lanes; ++i)
        config.load[i] = (km_switch_input_t){(uint16_t)(32u + i), 1, true};
    config.hub = (km_switch_input_t){100, 1, true};
    config.toolhead = (km_switch_input_t){101, -1, false};
    config.buffer_low = (km_switch_input_t){102, 0, true};
    config.buffer_high = (km_switch_input_t){103, 1, false};
    levels[101] = levels[103] = true; /* Inactive active-low inputs. */
}
static void fixture(unsigned lanes, bool analog, uint32_t hz)
{ fixture_ex(lanes, analog, false, hz); }
static void sample(void) { km_switch_gpio_sample(&core, NULL); }
static void sample_step(void)
{
    shim_advance_ticks(timer_from_us(1000));
    sample();
}
static void sample_stable(void)
{
    /* Eight periods cover the 5 ms qualification plus scan execution time. */
    for (unsigned i = 0; i < 8; ++i) sample_step();
}
static void operation_step(km_fake_clock_t *motion)
{
    sample_step();
    km_core_timepoint_t time;
    assert(km_clock_get_time(&time) == KM_OK);
    assert(km_fake_clock_run_trajectory(motion, &core.trajectory, time.us) == KM_OK);
    km_core_update(&core, time.ms, time.us);
}
static void assert_invalid(void)
{
    assert(!core.bay_sensors_valid && !core.hub_sensor_valid && !core.toolhead_sensor_valid);
    for (unsigned i = 0; i < core.profile.lane_count; ++i) assert(!core.bay_valid[i]);
}
static void basic(uint32_t hz)
{
    fixture(8, false, hz);
    assert(km_switch_gpio_setup(&core, &config) == KM_OK && setups == 12);
    assert_invalid();
    for (unsigned i = 0; i < 8; ++i) {
        assert(pulls[i] == config.bay[i].pull);
        levels[i] = true; /* Mixed active polarities. */
    }
    levels[100] = true;
    sample_stable();
    reads = 0;
    read_cost = timer_from_us(110);
    shim_advance_ticks(timer_from_us(990));
    km_core_timepoint_t before, after;
    assert(km_clock_get_time(&before) == KM_OK);
    sample();
    assert(km_clock_get_time(&after) == KM_OK && after.ms > before.ms);
    assert(reads == 12 && !test_shim_irq_is_disabled());
    for (unsigned i = 0; i < 8; ++i) {
        assert(core.bay_valid[i] && core.filament_present_bay[i] == (i % 2 != 0));
        assert(core.bay_sample_ms[i] == before.ms);
    }
    assert(core.path_sample_ms == before.ms && core.path_occupant_lane == KM_LANE_UNKNOWN);
    assert(core.buffer_sensor.last_sample_time_ms == before.ms && core.buffer_sensor.is_valid);
    assert(core.buffer_sensor.position_permille == 500);
    assert(shim_gpio_events_count == 0); /* Inputs never drive a motor or other output. */
    config.hub.pin = 2; /* Caller edits cannot alter the immutable binding. */
    levels[100] = false;
    sample_stable();
    assert(!core.filament_present_hub && core.path_occupied);
    assert(km_switch_gpio_setup(&core, &config) == KM_ERR_BUSY && setups == 12);
    levels[102] = true;
    sample_stable(); assert(core.buffer_sensor.position_permille == 250);
    levels[103] = false;
    sample_stable(); assert(!core.buffer_sensor.is_valid && core.buffer_sensor.fault == KM_FAULT_BUFFER_SIGNAL);
    levels[102] = false;
    sample_stable(); assert(core.buffer_sensor.is_valid && core.buffer_sensor.position_permille == 750);
}
/* A tension-only buffer (docs/devices/qidi_box.md) claims only buffer_low:
 * buffer_high.pin stays KM_SWITCH_PIN_UNUSED, so it is never set up or
 * sampled, and its missing state is never fabricated (mcu/switch_gpio.c). */
static void single_switch_buffer(void)
{
    fixture(4, false, 520000000);
    config.buffer_high.pin = KM_SWITCH_PIN_UNUSED;
    assert(km_switch_gpio_setup(&core, &config) == KM_OK && setups == 4 + 1 + 1 + 1);
    assert_invalid();
    sample_stable();
    assert(core.buffer_sensor.is_valid && core.buffer_sensor.position_permille == 500);
    levels[102] = true; /* Tension switch active: demand forward feed. */
    sample_stable();
    assert(core.buffer_sensor.is_valid && core.buffer_sensor.position_permille == 250);
    levels[102] = false;
    sample_stable();
    /* Back to neutral, never to the unreachable compressed/750 state. */
    assert(core.buffer_sensor.is_valid && core.buffer_sensor.position_permille == 500);
    km_switch_gpio_shutdown();
    assert_invalid();
}
/* This build has no room for the INDEXED selector's per-gate switches
 * (mcu/switch_gpio.c KM_SWITCH_INPUTS_MAX; docs/selectors.md): a profile
 * that asks for them anyway is refused outright, never silently sampled
 * without them. tests/klipper_switch/test_selector_gate.c is the same
 * fixture shape built WITH that room, where the claim succeeds. */
static void indexed_gate_needs_image_support(void)
{
    fixture(3, false, 520000000);
    core.profile.has_selector = true;
    core.profile.selector = (km_selector_profile_t){
        .kind = KM_SELECTOR_INDEXED,
        .home = {.speed_steps_s = 4800u, .acceleration_steps_s2 = 96000u,
                 .deceleration_steps_s2 = 96000u, .max_travel_steps = 90000u,
                 .deadline_us = 10000000u, .max_switch_age_us = 20000u,
                 .home_direction = KM_DIR_REVERSE},
        .velocity_steps_s = 16000u, .acceleration_steps_s2 = 96000u,
        .gate_order = {0, 2, 1}, .center_nudge_steps = 200u,
    };
    for (unsigned i = 0; i < 3; ++i)
        config.selector_gate[i] = (km_switch_input_t){(uint16_t)(40u + i), 1, true};
    assert(km_switch_gpio_setup(&core, &config) == KM_ERR_UNAVAILABLE && setups == 0);
}
static void invalid_config(void)
{
    fixture(1, false, 520000000);
    assert(km_switch_gpio_setup(NULL, &config) == KM_ERR_INVALID_PARAM);
    assert(km_switch_gpio_setup(&core, NULL) == KM_ERR_INVALID_PARAM);
    irq_disable();
    assert(km_switch_gpio_setup(&core, &config) == KM_ERR_INVALID_PARAM);
    assert(test_shim_irq_is_disabled()); irq_enable();
    config.bay[0].pull = 2;
    assert(km_switch_gpio_setup(&core, &config) == KM_ERR_INVALID_PARAM);
    config.bay[0].pull = -2;
    assert(km_switch_gpio_setup(&core, &config) == KM_ERR_INVALID_PARAM);
    config.bay[0].pull = 0; config.bay[0].pin = 128;
    assert(km_switch_gpio_setup(&core, &config) == KM_ERR_INVALID_PARAM);
    config.bay[0].pin = config.hub.pin;
    assert(km_switch_gpio_setup(&core, &config) == KM_ERR_CONFLICT);
    config.bay[0].pin = 0;
    core.shared_path_owned = true;
    assert(km_switch_gpio_setup(&core, &config) == KM_ERR_BUSY);
    core.shared_path_owned = false;
    assert(setups == 0);
    assert(km_switch_gpio_setup(&core, &config) == KM_OK && setups == 5);
}
static void analog_separation(void)
{
    fixture(4, true, 520000000);
    config.buffer_low.pin = config.buffer_high.pin = UINT16_MAX;
    config.buffer_low.pull = 5; /* Unused analog entries must never be touched. */
    assert(km_switch_gpio_setup(&core, &config) == KM_OK && setups == 6);
    assert(km_buffer_update_diff_adc(&core.buffer_sensor, 2048, 2048, 0) == KM_OK);
    km_buffer_sensor_t saved = core.buffer_sensor;
    sample();
    assert(reads == 6 && !memcmp(&saved, &core.buffer_sensor, sizeof(saved)));
    km_switch_gpio_shutdown();
    assert_invalid();
    assert(!memcmp(&saved, &core.buffer_sensor, sizeof(saved)));
}
static void shutdown_case(bool during_setup)
{
    fixture(4, false, 520000000);
    if (during_setup) fail_setup = 2;
    else {
        assert(km_switch_gpio_setup(&core, &config) == KM_OK);
        sample_stable(); assert(core.hub_sensor_valid);
        fail_read = 2;
    }
    test_shim_use_longjmp = true;
    if (setjmp(test_shim_shutdown_jump) == 0) {
        if (during_setup) (void)km_switch_gpio_setup(&core, &config);
        else sample();
        assert(false);
    }
    test_shim_use_longjmp = false;
    assert_invalid();
    assert(test_shim_clock_reads_in_shutdown == 0);
    unsigned was_reads = reads, was_setups = setups;
    sched_clear_shutdown();
    sample();
    assert(reads == was_reads && setups == was_setups);
    assert(km_switch_gpio_setup(&core, &config) == KM_ERR_FAULTED);
}
static void controller_sample(void)
{
    fixture(4, false, 520000000);
    assert(km_switch_gpio_setup(&core, &config) == KM_OK);
    assert(km_controller_attach(&core, km_switch_gpio_sample, NULL) == KM_OK);
    read_cost = timer_from_us(5);
    for (unsigned i = 0; i < 20; ++i) {
        struct timer *t = shim_find_earliest_timer(); assert(t);
        shim_set_ticks(t->waketime);
        irq_disable(); shim_dispatch_timer(t); irq_enable();
        shim_run_task(); /* The timer only wakes the control task. */
    }
    assert(reads > 0 && core.hub_sensor_valid && core.bay_sensors_valid);
    assert(core.buffer_sensor.is_valid && !core.path_occupied);
    assert(core.last_control_ms >= core.path_sample_ms);
}
static void debounce_case(void)
{
    fixture(1, false, 520000000);
    assert(km_switch_gpio_setup(&core, &config) == KM_OK);
    levels[0] = true; /* Lane zero is active-low: begin stably absent. */
    sample();
    assert(!core.bay_valid[0] && !core.hub_sensor_valid);
    sample_stable();
    assert(core.bay_valid[0] && !core.filament_present_bay[0]);

    levels[0] = false;
    sample_step(); sample_step(); /* Brief active pulse. */
    assert(core.bay_valid[0] && !core.filament_present_bay[0]);
    levels[0] = true;
    sample_step();
    sample_stable();
    assert(core.bay_valid[0] && !core.filament_present_bay[0]);

    levels[0] = false;
    sample_step();
    assert(core.bay_valid[0] && !core.filament_present_bay[0]);
    for (unsigned i = 0; i < 6; ++i) sample_step();
    assert(core.bay_valid[0] && core.filament_present_bay[0]);

    levels[0] = true;
    sample_step();
    assert(core.bay_valid[0] && core.filament_present_bay[0]
           && core.buffer_sensor.is_valid);
    levels[0] = false; /* Reverse bounce does not accept a false absence. */
    sample_step(); sample_stable();
    assert(core.bay_valid[0] && core.filament_present_bay[0]);
    levels[0] = true;
    sample_stable();
    assert(core.bay_valid[0] && !core.filament_present_bay[0]);

    levels[102] = true;
    sample_step();
    assert(core.buffer_sensor.is_valid
           && core.buffer_sensor.position_permille == core.buffer_sensor.switch_neutral_permille);
    levels[102] = false;
    sample_stable();
    assert(core.buffer_sensor.is_valid);
    levels[102] = true;
    for (unsigned i = 0; i < 6; ++i) sample_step();
    assert(core.buffer_sensor.is_valid
           && core.buffer_sensor.position_permille == core.buffer_sensor.switch_low_permille);

    shim_advance_ticks(timer_from_us(KM_SWITCH_MAX_SAMPLE_GAP_US + 1000u));
    sample();
    assert(!core.bay_valid[0] && !core.hub_sensor_valid
           && !core.buffer_sensor.is_valid);
    sample_stable();
    assert(core.bay_valid[0] && core.hub_sensor_valid && core.buffer_sensor.is_valid);

    /* A caller that misses the sample-gap bound cannot establish stable input. */
    for (unsigned i = 0; i < 4; ++i) {
        shim_advance_ticks(timer_from_us(KM_SWITCH_MAX_SAMPLE_GAP_US + 1000u));
        sample();
    }
    assert(!core.bay_valid[0] && !core.hub_sensor_valid);
}
static void bounce_counters(void)
{
    fixture(1, false, 520000000);
    uint16_t bay[KM_MAX_LANES], load[KM_MAX_LANES], hub, toolhead;
    km_switch_gpio_get_bounce(bay, load, &hub, &toolhead); /* Unbound: zero. */
    assert(bay[0] == 0 && hub == 0 && toolhead == 0);

    assert(km_switch_gpio_setup(&core, &config) == KM_OK);
    /* Before any level has ever qualified, each new raw value replaces the
     * still-unqualified candidate: three rejected candidacies. */
    levels[0] = true;  sample_step(); /* First observation: a reset, not a bounce. */
    levels[0] = false; sample_step(); /* Replaces the candidate. */
    levels[0] = true;  sample_step(); /* Replaces it again. */
    levels[0] = false; sample_step(); /* And again. */
    km_switch_gpio_get_bounce(bay, load, &hub, &toolhead);
    assert(bay[0] == 3);
    sample_stable();
    km_switch_gpio_get_bounce(bay, load, &hub, &toolhead);
    assert(bay[0] == 3 && core.filament_present_bay[0]); /* Settled present. */

    /* Once qualified, a brief excursion that returns to the qualified level
     * before settling is also a rejected candidacy. */
    levels[0] = true; sample_step(); sample_step(); /* Brief absent pulse. */
    levels[0] = false; sample_step();
    km_switch_gpio_get_bounce(bay, load, &hub, &toolhead);
    assert(bay[0] == 4);
    sample_stable();
    km_switch_gpio_get_bounce(bay, load, &hub, &toolhead);
    assert(bay[0] == 4); /* Settled; no further change. */

    /* A stale sampling gap is a reset, never a bounce. */
    shim_advance_ticks(timer_from_us(KM_SWITCH_MAX_SAMPLE_GAP_US + 1000u));
    sample();
    km_switch_gpio_get_bounce(bay, load, &hub, &toolhead);
    assert(bay[0] == 4);
}
static void active_load_debounce(void)
{
    fixture(1, false, 520000000);
    levels[0] = false;   /* Active-low bay: spool present. */
    levels[100] = false; /* Hub active-high: path initially clear. */
    levels[101] = true;  /* Toolhead active-low: path initially clear. */
    assert(km_switch_gpio_setup(&core, &config) == KM_OK);
    mock_driver_t feeder, rewinder;
    mock_driver_init(&feeder, 2, 10, false);
    mock_driver_init(&rewinder, 2, 10, false);
    assert(km_core_bind_lane_driver(&core, 0, &feeder.base, &rewinder.base) == KM_OK);
    km_core_timepoint_t time;
    assert(km_clock_get_time(&time) == KM_OK);
    km_core_host_comm_ready(&core, time.ms);
    km_fake_clock_t motion;
    km_fake_clock_init(&motion, time.us);
    assert(km_core_request_load(&core, KM_CLIENT_HOST, 1, 1, 0, time)
           == KM_REQ_UNAVAILABLE);
    assert(core.op_state == KM_OP_STATE_IDLE && core.system_fault == KM_FAULT_NONE);
    sample_stable();
    assert(km_clock_get_time(&time) == KM_OK);
    km_fake_clock_init(&motion, time.us);
    assert(km_core_request_load(&core, KM_CLIENT_HOST, 1, 1, 0, time)
           == KM_REQ_ACCEPTED);
    assert(core.stage == KM_STAGE_LOAD_FEED_TO_HUB);

    levels[100] = true;
    operation_step(&motion); operation_step(&motion); /* A short, unaccepted sensor pulse. */
    assert(core.op_state == KM_OP_STATE_LOADING
           && core.stage == KM_STAGE_LOAD_FEED_TO_HUB
           && core.system_fault == KM_FAULT_NONE);
    levels[100] = false;
    operation_step(&motion);
    assert(core.op_state == KM_OP_STATE_LOADING
           && core.stage == KM_STAGE_LOAD_FEED_TO_HUB
           && core.system_fault == KM_FAULT_NONE);

    levels[100] = true;
    for (unsigned i = 0; i < 7; ++i) operation_step(&motion);
    assert(core.op_state == KM_OP_STATE_LOADING
           && core.stage == KM_STAGE_LOAD_FEED_TO_BUFFER
           && core.filament_present_hub
           && core.system_fault == KM_FAULT_NONE);

    levels[101] = false;
    for (unsigned i = 0; i < 7; ++i) operation_step(&motion);
    assert(core.op_state == KM_OP_STATE_FOLLOWING && core.stage == KM_STAGE_FOLLOW
           && core.filament_present_toolhead && core.system_fault == KM_FAULT_NONE);
    assert(km_clock_get_time(&time) == KM_OK);
    assert(km_core_request_unload(&core, KM_CLIENT_HOST, 1, 2, time)
           == KM_REQ_ACCEPTED);
    assert(core.stage == KM_STAGE_UNLOAD_RETRACT_TO_HUB);

    levels[100] = false;
    levels[101] = true;
    operation_step(&motion); /* Begin brief clear candidate. */
    levels[100] = true;
    levels[101] = false;
    operation_step(&motion); /* Return to qualified occupied levels. */
    assert(core.op_state == KM_OP_STATE_UNLOADING
           && core.stage == KM_STAGE_UNLOAD_RETRACT_TO_HUB
           && core.filament_present_hub && core.filament_present_toolhead
           && core.system_fault == KM_FAULT_NONE);
    levels[100] = false;
    levels[101] = true;
    for (unsigned i = 0; i < 5; ++i) {
        operation_step(&motion);
        assert(core.stage == KM_STAGE_UNLOAD_RETRACT_TO_HUB
               && core.op_state == KM_OP_STATE_UNLOADING
               && core.system_fault == KM_FAULT_NONE);
    }
    operation_step(&motion);
    assert(core.op_state == KM_OP_STATE_UNLOADING
           && core.stage == KM_STAGE_BRAKING
           && !core.filament_present_hub && !core.filament_present_toolhead
           && core.system_fault == KM_FAULT_NONE);
}
static void load_inputs(void)
{
    fixture_ex(4, false, true, 520000000);
    config.load[1].pin = config.bay[1].pin;
    assert(km_switch_gpio_setup(&core, &config) == KM_ERR_CONFLICT && setups == 0);
    config.load[1].pin = 33;
    config.load[1].pull = 2;
    assert(km_switch_gpio_setup(&core, &config) == KM_ERR_INVALID_PARAM && setups == 0);
    config.load[1].pull = -1;
    assert(km_switch_gpio_setup(&core, &config) == KM_OK && setups == 12);
    levels[32] = levels[34] = true;
    sample_stable();
    reads = 0;
    km_core_timepoint_t before;
    assert(km_clock_get_time(&before) == KM_OK);
    sample();
    assert(reads == 12);
    for (unsigned i = 0; i < 4; ++i) {
        assert(core.load_valid[i]);
        assert(core.filament_present_load[i] == (i == 0 || i == 2));
        assert(core.load_sample_ms[i] == before.ms);
    }
    km_switch_gpio_shutdown();
    for (unsigned i = 0; i < 4; ++i) assert(!core.load_valid[i]);
}
static void virtual_hub_input(void)
{
    fixture_ex(1, false, true, 520000000);
    km_device_profile_t profile;
    km_profile_init_emu(&profile);
    /* This case exercises the documented unambiguous one-lane virtual hub. */
    profile.lane_count = 1;
    assert(km_core_init(&core, &profile) == KM_OK);
    assert(km_switch_gpio_setup(&core, &config) == KM_ERR_INVALID_PARAM);
    assert(setups == 0);
    config.hub.pin = KM_SWITCH_PIN_UNUSED;
    config.load[0].pin = config.toolhead.pin;
    assert(km_switch_gpio_setup(&core, &config) == KM_ERR_CONFLICT);
    config.load[0].pin = 32;
    assert(km_switch_gpio_setup(&core, &config) == KM_OK && setups == 5);
    assert(pulls[32] == config.load[0].pull);
    km_core_timepoint_t before;
    levels[32] = true;
    sample_stable();
    reads = 0;
    assert(km_clock_get_time(&before) == KM_OK);
    sample();
    assert(reads == 5 && core.load_valid[0] && core.hub_sensor_valid);
    assert(core.filament_present_load[0] && core.filament_present_hub);
    assert(core.load_sample_ms[0] == before.ms && core.path_sample_ms == before.ms);
    levels[32] = false;
    reads = 0;
    sample_stable();
    assert(!core.filament_present_load[0] && !core.filament_present_hub);
    assert(reads == 40);
    km_switch_gpio_shutdown();
    assert(!core.load_valid[0] && !core.hub_sensor_valid);
}
/* Analog FPS profile; a virtual HUB derives presence from LOAD inputs. */
static void fixture_fps(bool load, bool virtual_hub, bool toolhead)
{
    fixture_ex(4, true, load || virtual_hub, 520000000);
    km_device_profile_t p = core.profile;
    if (virtual_hub) {
        p.load_is_virtual_hub = true;
        p.has_powered_rewind = false;
        p.rewind_pwm_duty_permille = 0;
        config.hub.pin = KM_SWITCH_PIN_UNUSED;
    }
    p.has_toolhead_sensor = toolhead;
    p.fps_arrival = true; p.fps_arrival_permille = 700; p.fps_arrival_samples = 3;
    p.fps_arrival_hold_ms = 20; p.fps_arrival_min_steps = 200; p.fps_arrival_max_steps = 3000;
    /* The calibrated LOAD profile: synthetic values exercising only the
     * switch/sensor wiring this suite covers, not measured hardware. */
    p.load_window_steps = 1000; p.buffer_travel_steps = 5000;
    p.load_cruise_velocity_steps_s = 2000; p.load_crawl_velocity_steps_s = 500;
    assert(km_core_init(&core, &p) == KM_OK);
}
static void optional_toolhead(unsigned kind)
{
    bool virtual_hub = (kind & 2u) != 0, toolhead = (kind & 4u) != 0;
    bool load = (kind & 1u) != 0 || virtual_hub;
    fixture_fps(load, virtual_hub, toolhead);
    unsigned expected = 4u + (virtual_hub ? 0u : 1u) + (toolhead ? 1u : 0u) + (load ? 4u : 0u);
    if (!toolhead) {
        /* A missing switch cannot keep a dummy pin assignment. */
        assert(km_switch_gpio_setup(&core, &config) == KM_ERR_INVALID_PARAM && setups == 0);
        config.toolhead.pin = KM_SWITCH_PIN_UNUSED;
    }
    assert(km_switch_gpio_setup(&core, &config) == KM_OK && setups == expected);
    assert(pulls[101] == (toolhead ? -1 : 0));
    levels[100] = true;  /* HUB asserted, if physically bound. */
    levels[101] = false; /* Active-low toolhead asserted, if bound. */
    levels[34] = true;   /* Lane 2 LOAD asserted. */
    sample_stable();
    reads = 0;
    sample();
    assert(reads == expected);
    assert(core.toolhead_sensor_valid && core.filament_present_toolhead == toolhead);
    if (!virtual_hub) assert(core.hub_sensor_valid && core.filament_present_hub);
    if (virtual_hub && !toolhead)
        assert(core.path_occupant_lane == 2 && core.hub_sensor_valid && core.filament_present_hub);
    for (unsigned i = 0; i < 4; ++i)
        assert(core.load_valid[i] == load && core.filament_present_load[i] == (load && i == 2));
    if (toolhead) {
        levels[101] = true;
        sample_step(); /* Still debounced as present. */
        assert(core.filament_present_toolhead);
        sample_stable();
        assert(core.toolhead_sensor_valid && !core.filament_present_toolhead);
    }
}
static void toolhead_required_without_fps(void)
{
    for (unsigned analog = 0; analog < 2; ++analog) {
        fixture_ex(4, analog != 0, false, 520000000);
        km_device_profile_t p = core.profile;
        p.has_toolhead_sensor = false;
        assert(km_core_init(&core, &p) == KM_OK);
        config.toolhead.pin = KM_SWITCH_PIN_UNUSED;
        assert(km_switch_gpio_setup(&core, &config) == KM_ERR_INVALID_PARAM && setups == 0);
    }
}
static void clock_wrap(void)
{
    fixture(4, false, 520000000);
    assert(km_switch_gpio_setup(&core, &config) == KM_OK);
    km_core_timepoint_t time;
    /* Keep clock observations within its horizon while approaching raw wrap. */
    for (unsigned i = 0; i < 42; ++i) {
        shim_advance_ticks(100000000); assert(km_clock_get_time(&time) == KM_OK);
    }
    shim_set_ticks(UINT32_MAX - 100);
    assert(km_clock_get_time(&time) == KM_OK);
    read_cost = 100;
    sample_stable();
    assert(km_clock_get_time(&time) == KM_OK);
    sample();
    assert(core.path_sample_ms == time.ms && core.hub_sensor_valid);
    assert(test_shim_ticks < timer_from_us(10000));
}
static void rejected_runtime(unsigned kind)
{
    fixture(4, false, 520000000);
    assert(km_switch_gpio_setup(&core, &config) == KM_OK);
    sample_stable();
    if (kind == 0) ++core.profile.lane_count;
    if (kind == 1) shim_advance_ticks(0x80000000u);
    if (kind == 3) core.profile.has_toolhead_sensor = false;
    test_shim_use_longjmp = true;
    if (setjmp(test_shim_shutdown_jump) == 0) {
        km_switch_gpio_sample(&core, kind == 2 ? &config : NULL);
        assert(false);
    }
    test_shim_use_longjmp = false;
    assert(test_shim_is_shutdown && !core.hub_sensor_valid);
    assert(test_shim_clock_reads_in_shutdown == 0);
}
static void run(unsigned which)
{
    switch (which) {
    case 0: basic(1000000); break;
    case 1: basic(12288000); break;
    case 2: basic(520000000); break;
    case 3: invalid_config(); break;
    case 4: analog_separation(); break;
    case 5: shutdown_case(true); break;
    case 6: shutdown_case(false); break;
    case 7: controller_sample(); break;
    case 8: clock_wrap(); break;
    case 9: rejected_runtime(0); break;
    case 10: rejected_runtime(1); break;
    case 11: rejected_runtime(2); break;
    case 12: load_inputs(); break;
    case 13: virtual_hub_input(); break;
    case 14: debounce_case(); break;
    case 15: active_load_debounce(); break;
    case 24: toolhead_required_without_fps(); break;
    case 25: rejected_runtime(3); break;
    case 26: bounce_counters(); break;
    case 27: single_switch_buffer(); break;
    case 28: indexed_gate_needs_image_support(); break;
    default:
        assert(which >= 16 && which < 24);
        optional_toolhead(which - 16u);
    }
}
int main(void)
{
    for (unsigned i = 0; i < 29; ++i) {
        pid_t child = fork(); assert(child >= 0);
        if (!child) { run(i); _exit(0); }
        int status; assert(waitpid(child, &status, 0) == child);
        if (!WIFEXITED(status) || WEXITSTATUS(status)) {
            fprintf(stderr, "switch scenario %u failed\n", i); return 1;
        }
    }
    puts("29 switch input / controller scenarios passed");
    return 0;
}
