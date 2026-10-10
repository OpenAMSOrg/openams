// SPDX-License-Identifier: GPL-3.0-or-later
#include <assert.h>
#include <inttypes.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "commands.h"
#include "clock.h"
#include "controller.h"
#include "shim_klipper.h"
#include "board/irq.h"
#include "mock_driver.h"
#include "fake_clock.h"
#include "tmc_uart_gpio.h"

static km_operation_core_t core;
static mock_driver_t feeders[4];
static km_fake_clock_t motion_clock;
static uint32_t ms, changed_count, response_count, values[24], value_count;
static char response[256];
/* Diagnostic TMC stand-in (docs/diagnostics.md); see km_tmc_uart_gpio_get_status below. */
static uint32_t diag_gstat[4], diag_drv_status[4];
static const uint64_t INSTANCE = UINT64_C(0x89abcdef12345678);
static const uint32_t SESSION = 0xabcdef01u, REQUEST = 0x12345678u;
static bool physical_ready = true;
static bool ready_cb(void) { return physical_ready; }

bool km_controller_get_switch_sample_delay(uint32_t *out_us)
{
    if (out_us) *out_us = 0;
    return false;
}

void km_controller_get_switch_sample_stats(uint32_t *count, uint32_t *max_us)
{
    if (count) *count = 0;
    if (max_us) *max_us = 0;
}

void command_sendf(const char *format, ...)
{
    assert(!test_shim_irq_is_disabled());
    assert(strlen(format) < sizeof(response));
    strcpy(response, format);
    value_count = 0;
    va_list args; va_start(args, format);
    const char *p = format;
    printf("{\"format\":\"%s\",\"values\":[", format);
    while ((p = strchr(p, '%'))) {
        assert(value_count < 24);
        uint32_t v = va_arg(args, uint32_t);
        if (value_count) putchar(',');
        printf("%" PRIu32, v);
        values[value_count++] = v;
        ++p;
    }
    va_end(args);
    puts("]}");
    ++response_count;
}

/* The controller exchange: task context, which masks only its own copies. */
static void changed(void)
{
    assert(!test_shim_irq_is_disabled());
    ++changed_count;
}

static void samples_path(bool hub, bool toolhead)
{
    for (uint8_t lane = 0; lane < 4; ++lane)
        km_core_set_sensors_at(&core, lane, true, hub, toolhead, true, ms);
    assert(km_buffer_update_switches(&core.buffer_sensor, false, false, ms) == KM_OK);
}
static void samples(bool occupied) { samples_path(occupied, occupied); }

/* "selector_gate_servo" swaps NightOwl's has_selector=false for a minimal
 * SERVO-kind profile, so km_selector_gate_set's degrees path (0 is a real
 * angle, -1 clears, out of range is refused) has something to exercise;
 * no servo needs binding since km_core_set_selector_gate never calls it. */
static bool servo_selector_requested;
/* "driver_restart_selector" swaps in a minimal LINEAR selector whose own
 * stepper is bound to feeders[1] (mcu/board_startup.c's physical slot 1),
 * so command_km_driver_restart's SELECTOR_LANE path has a real selector
 * driver to restart; every lane shares feeders[0] as the one gear driver,
 * same as a real selector machine (klipper_mmu/selector.h). */
static bool stepper_selector_requested;

static void prepare(void)
{
    shim_reset(520000000, 1, 1, 4);
    km_device_profile_t profile;
    km_profile_init_nightowl(&profile);
    if (servo_selector_requested) {
        profile.has_selector = true;
        profile.selector.kind = KM_SELECTOR_SERVO;
        profile.selector.has_servo = true;
        profile.selector.servo_dwell_ms = 5;
        for (uint8_t i = 0; i < profile.lane_count; ++i)
            profile.selector.servo_gate_us[i] = 1000;
    } else if (stepper_selector_requested) {
        profile.has_selector = true;
        profile.selector.kind = KM_SELECTOR_LINEAR;
        profile.selector.home.speed_steps_s = 1000;
        profile.selector.home.acceleration_steps_s2 = 10000;
        profile.selector.home.deceleration_steps_s2 = 10000;
        profile.selector.home.max_travel_steps = 20;
        profile.selector.home.deadline_us = 2000000;
        profile.selector.home.max_switch_age_us = 1000;
        profile.selector.home.home_direction = KM_DIR_REVERSE;
        profile.selector.velocity_steps_s = 1000;
        profile.selector.acceleration_steps_s2 = 10000;
    }
    assert(km_core_init(&core, &profile) == KM_OK);
    for (uint8_t lane = 0; lane < 4; ++lane) {
        mock_driver_init(&feeders[lane], 2, 10, false);
        km_driver_t *gear = stepper_selector_requested ? &feeders[0].base : &feeders[lane].base;
        assert(km_core_bind_lane_driver(&core, lane, gear, NULL) == KM_OK);
    }
    if (stepper_selector_requested)
        assert(km_core_bind_selector(&core, &feeders[1].base, NULL) == KM_OK);
    km_fake_clock_init(&motion_clock, 0);
    samples(false);
}

static void attach(void)
{
    shim_run_init(); shim_set_board_ready(true); shim_run_task();
    assert(km_commands_attach(&core, INSTANCE, changed, true) == KM_OK);
}

/* The fixture stands in for the future board scheduler and raw safe-off hook.
 * Commands themselves never own periodic sensing or pulse dispatch. */
static void advance_path(uint32_t duration, bool hub, bool toolhead)
{
    for (uint32_t i = 0; i < duration; ++i) {
        ++ms;
        shim_advance_ticks(520000);
        assert(km_fake_clock_run_trajectory(&motion_clock, &core.trajectory, ms * 1000u) == KM_OK);
        samples_path(hub, toolhead);
        km_core_update(&core, ms, ms * 1000u);
        (void)shim_dispatch_timer();
    }
}
static void advance(uint32_t duration, bool occupied)
{
    advance_path(duration, occupied, occupied);
}

static void raw_shutdown(void)
{
    for (unsigned lane = 0; lane < 4; ++lane) km_driver_shutdown(&feeders[lane].base);
}
DECL_SHUTDOWN(raw_shutdown);

static void call(const char *name, uint32_t *args)
{
    command_func_t fn = shim_find_command(name);
    assert(fn);
    uint32_t before = response_count;
    fn(args);
    assert(response_count == before + 1); /* One bounded response per request. */
}

static void expect_result(enum km_command_result code)
{
    assert(!strncmp(response, "km_command_result ", 18));
    assert(value_count == 5 && values[4] == (uint32_t)code);
}
static void expect_admission(km_admission_result_t code)
{
    assert(!strncmp(response, "km_admission ", 13));
    assert(value_count == 9 && values[7] == (uint32_t)code);
}

static void args_identity(uint32_t *args, uint32_t request, uint32_t op, uint32_t lane)
{
    args[0] = (uint32_t)(INSTANCE >> 32); args[1] = (uint32_t)INSTANCE;
    args[2] = KM_CLIENT_HOST; args[3] = SESSION; args[4] = request;
    args[5] = op; args[6] = lane; args[7] = KM_DIR_FORWARD;
}
static void heartbeat(void)
{
    uint32_t args[] = {(uint32_t)(INSTANCE >> 32), (uint32_t)INSTANCE, SESSION};
    call("km_heartbeat ", args); expect_result(KM_COMMAND_OK);
}

static void test_lifecycle(void)
{
    uint32_t query[] = {UINT32_MAX};
    call("km_transport_query ", query);
    assert(values[0] == UINT32_MAX && values[1] == 0 && values[4] == 0);
    uint32_t args[8]; args_identity(args, REQUEST, KM_OP_TYPE_LOAD, 2);
    call("km_start ", args); expect_result(KM_COMMAND_UNBOUND);
    assert(km_commands_attach(NULL, INSTANCE, changed, true) == KM_ERR_INVALID_PARAM);
    assert(km_commands_attach(&core, 0, changed, true) == KM_ERR_INVALID_PARAM);
    assert(km_commands_attach(&core, INSTANCE, NULL, true) == KM_ERR_INVALID_PARAM);
    assert(km_commands_attach(&core, INSTANCE, changed, true) == KM_ERR_UNAVAILABLE);
    irq_disable();
    assert(km_commands_attach(&core, INSTANCE, changed, true) == KM_ERR_INVALID_PARAM);
    irq_enable();
    attach();
    assert(km_commands_attach(&core, INSTANCE + 1, changed, true) == KM_ERR_BUSY);
    call("km_transport_query ", query);
    assert(values[0] == UINT32_MAX && values[1] == (uint32_t)(INSTANCE >> 32));
    assert(values[2] == (uint32_t)INSTANCE && values[3] == 0 && values[4] == 1);
    assert(changed_count == 0 && !core.shared_path_owned);
}

static void test_requests(void)
{
    attach(); heartbeat();
    uint32_t args[8]; args_identity(args, REQUEST, KM_OP_TYPE_LOAD, 2);
    for (unsigned index = 2; index <= 7; ++index) {
        if (index == 3 || index == 4) continue;
        uint32_t saved = args[index]; args[index] = 256 + saved;
        call("km_start ", args); expect_result(KM_COMMAND_INVALID);
        assert(changed_count == 0 && !core.shared_path_owned);
        args[index] = saved;
    }
    args[2] = KM_CLIENT_LOCAL_UI;
    call("km_start ", args); expect_admission(KM_REQ_REJECTED_CONFLICT);
    args[2] = KM_CLIENT_HOST; args[0] ^= 1;
    call("km_start ", args); expect_result(KM_COMMAND_INSTANCE_MISMATCH);
    args[0] ^= 1;
    call("km_start ", args); expect_admission(KM_REQ_ACCEPTED);
    assert(core.active_req_id == REQUEST && core.active_session_id == SESSION && core.active_lane == 2);
    call("km_start ", args); expect_admission(KM_REQ_ALREADY_IN_PROGRESS);
    advance(10, false);
    args[5] = KM_OP_TYPE_UNLOAD;
    call("km_cancel ", args); expect_admission(KM_REQ_REJECTED_STALE);
    args[5] = KM_OP_TYPE_LOAD; args[6] = 3;
    call("km_cancel ", args); expect_admission(KM_REQ_REJECTED_STALE);
    args[6] = 2;
    call("km_cancel ", args); expect_admission(KM_REQ_ACCEPTED);
    call("km_event_peek ", args); expect_result(KM_COMMAND_EMPTY);
    assert(core.shared_path_owned);
    advance(60, false);
    call("km_event_peek ", args);
    assert(!strncmp(response, "km_event ", 9));
    assert(values[5] == SESSION && values[6] == REQUEST && values[8] == 2);
    assert(values[9] == KM_EVENT_LOAD_CANCELLED && !core.shared_path_owned);
}

static void test_events(void)
{
    attach(); heartbeat();
    uint32_t args[8]; args_identity(args, REQUEST, KM_OP_TYPE_LOAD, 2);
    call("km_start ", args); expect_admission(KM_REQ_ACCEPTED);
    advance(20, true);
    assert(core.shared_path_owned && !core.terminal_pending);
    call("km_event_peek ", args);
    uint32_t seq = values[2]; assert(seq && values[9] == KM_EVENT_LOAD_COMPLETE);
    uint32_t event_values[24]; memcpy(event_values, values, sizeof(values));
    call("km_event_peek ", args); assert(!memcmp(event_values, values, value_count * sizeof(uint32_t)));
    args[7] = seq;
    for (unsigned i = 0; i < 8; ++i) {
        ++args[i];
        call("km_event_ack ", args);
        expect_result(i < 2 ? KM_COMMAND_INSTANCE_MISMATCH : KM_COMMAND_STALE);
        assert(core.event_queue.count == 1);
        --args[i];
    }
    call("km_event_ack ", args); expect_result(KM_COMMAND_OK);
    assert(!core.event_queue.count && core.shared_path_owned);
    call("km_event_ack ", args); expect_result(KM_COMMAND_STALE);
    call("km_terminal_query ", args); assert(values[9] == KM_EVENT_LOAD_COMPLETE);
    assert(!core.event_queue.count); /* History response never re-enqueues a terminal. */
    ++args[4]; call("km_terminal_query ", args); expect_result(KM_COMMAND_STALE); --args[4];
    /* One arrival record: stage, fault NONE and HUB-to-toolhead pulses. */
    call("km_load_attempts_query ", args);
    assert(!strncmp(response, "km_load_attempts ", 17) && value_count == 14);
    assert(values[2] == SESSION && values[3] == REQUEST && values[4] == 1);
    assert(values[5] == KM_STAGE_LOAD_FEED_TO_BUFFER && values[6] == KM_FAULT_NONE);
    assert(values[7] == core.load_attempts[0].travel_steps && !values[8] && !values[10]);
    args[0] ^= 1; call("km_load_attempts_query ", args); assert(!values[4]); args[0] ^= 1;
    ++args[4]; call("km_load_attempts_query ", args); assert(!values[4]); --args[4];
    args_identity(args, REQUEST + 1, KM_OP_TYPE_FOLLOW, 2); args[7] = KM_DIR_REVERSE;
    call("km_start ", args); expect_admission(KM_REQ_ACCEPTED);
    args_identity(args, REQUEST + 2, KM_OP_TYPE_UNLOAD, 2);
    call("km_start ", args); expect_admission(KM_REQ_ACCEPTED);
    advance(80, false);
    call("km_terminal_query ", args); assert(values[9] == KM_EVENT_UNLOAD_COMPLETE);
    assert(!core.path_occupied && !feeders[2].base.enabled);
}

static void test_preload_wire(void)
{
    attach(); heartbeat();
    uint32_t args[8]; args_identity(args, REQUEST, KM_OP_TYPE_PRELOAD, 2);
    call("km_start ", args); expect_admission(KM_REQ_ACCEPTED);
    advance(10, false);
    advance_path(5, true, false);
    /* No per-lane LOAD switch, a real HUB: PRELOAD retracts until the HUB
     * clears rather than settling the instant it trips. */
    assert(core.stage == KM_STAGE_UNLOAD_RETRACT_TO_HUB);
    advance_path(60, false, false);
    call("km_event_peek ", args);
    assert(!strncmp(response, "km_event ", 9));
    assert(values[7] == KM_OP_TYPE_PRELOAD && values[8] == 2
           && values[9] == KM_EVENT_PRELOAD_COMPLETE
           && values[10] == KM_WIRE_OP_CODE_SUCCESS);
    uint32_t seq = values[2];
    args[7] = seq;
    call("km_event_ack ", args); expect_result(KM_COMMAND_OK);
    call("km_terminal_query ", args);
    assert(values[7] == KM_OP_TYPE_PRELOAD && values[9] == KM_EVENT_PRELOAD_COMPLETE);
    assert(core.path_occupied && core.path_occupant_lane == 2);
    uint32_t other[8]; args_identity(other, REQUEST + 1u, KM_OP_TYPE_LOAD, 1);
    call("km_start ", other); expect_admission(KM_REQ_PATH_OCCUPIED);
    uint32_t same[8]; args_identity(same, REQUEST + 1u, KM_OP_TYPE_LOAD, 2);
    call("km_start ", same); expect_admission(KM_REQ_ACCEPTED);
}

static void test_snapshot(void)
{
    attach(); heartbeat();
    uint32_t args[] = {(uint32_t)(INSTANCE >> 32), (uint32_t)INSTANCE, UINT32_MAX - 1u, 0};
    call("km_snapshot_part ", args);
    uint32_t original[24]; memcpy(original, values, sizeof(values));
    uint32_t op[8]; args_identity(op, REQUEST, KM_OP_TYPE_LOAD, 2);
    call("km_start ", op); expect_admission(KM_REQ_ACCEPTED);
    advance(20, true);
    call("km_snapshot_part ", args);
    assert(!memcmp(original, values, value_count * sizeof(uint32_t)));
    for (uint32_t i = 1; i < 5; ++i) {
        args[3] = i; call("km_snapshot_part ", args);
        assert(values[2] == UINT32_MAX - 1u);
        if (i == 2) assert(values[8] == 0 && values[9] == 0); /* Original acquisition. */
    }
    args[3] = 256; call("km_snapshot_part ", args); expect_result(KM_COMMAND_INVALID);
    args[3] = 0; args[2] = 0; call("km_snapshot_part ", args); expect_result(KM_COMMAND_INVALID);
    args[2] = 1; call("km_snapshot_part ", args); expect_result(KM_COMMAND_STALE);
    args[2] = UINT32_MAX; call("km_snapshot_part ", args);
    assert(values[7] == SESSION && values[8] == REQUEST && values[9] == 2);
    args[2] = 1; call("km_snapshot_part ", args); expect_result(KM_COMMAND_STALE);
    uint32_t q[] = {12345}; call("km_transport_query ", q);
    assert(values[0] == 12345 && values[3] == UINT32_MAX);
}

static void test_sensor_snapshot(void)
{
    attach(); heartbeat();
    uint32_t args[] = {(uint32_t)(INSTANCE >> 32), (uint32_t)INSTANCE, 7, 0};
    call("km_sensor_part ", args);
    assert(!strncmp(response, "km_sensor_flags ", 16));
    assert(values[2] == 7 && values[3] == 4);
    assert(values[5] == 0x30fu && values[6] == 0xfu);
    assert(values[7] == 0x30fu && values[8] == 0x30fu);
    uint32_t original[9]; memcpy(original, values, sizeof(original));
    advance(20, true);
    call("km_sensor_part ", args);
    assert(!memcmp(original, values, sizeof(original)));
    args[3] = 1; call("km_sensor_part ", args);
    assert(!strncmp(response, "km_sensor_bays0 ", 16));
    assert(values[3] == 0 && values[6] == 0);
    args[3] = 2; call("km_sensor_part ", args);
    assert(!strncmp(response, "km_sensor_bays1 ", 16));
    assert(values[7] == 0 && values[8] == 0);
    args[3] = 0; ++args[2]; call("km_sensor_part ", args);
    assert(values[6] == 0x30fu && values[7] == 0x30fu && values[8] == 0x30fu);
    call("km_snapshot_part ", args);
    assert(!strncmp(response, "km_status_identity ", 19));
    km_core_set_sensors_at(&core, 0, true, true, true, false, ms);
    ++args[2]; call("km_snapshot_part ", args);
    call("km_sensor_part ", args);
    assert(values[6] & 1u); /* Retained presence is not confirmed absence. */
    assert(!(values[7] & 1u) && !(values[8] & 1u));
    args[3] = 3; call("km_sensor_part ", args);
    assert(!strncmp(response, "km_sensor_loads0 ", 17));
    assert(values[2] == 9 && values[3] == 0 && values[4] == 0);
    args[3] = 4; call("km_sensor_part ", args);
    assert(!strncmp(response, "km_sensor_loads1 ", 17));
    assert(values[2] == 9);
    /* Parts 5-6 (KM_SENSOR_BAYS2/LOADS2) are valid now; 7 is out of range. */
    args[3] = 7; call("km_sensor_part ", args); expect_result(KM_COMMAND_INVALID);
    args[3] = 0; args[2] = 7; call("km_sensor_part ", args); expect_result(KM_COMMAND_STALE);
    args[2] = 0; call("km_sensor_part ", args); expect_result(KM_COMMAND_INVALID);
    uint32_t q[] = {1}; call("km_transport_query ", q);
    assert(values[3] == 9); /* Shared snapshot/sensor query high-water. */
}

static void test_diag_wire(void)
{
    attach(); heartbeat();
    /* mcu/switch_gpio.c is not linked here, so bounce/idle-edge counters are
     * always zero; this checks the wire shape, not their counting logic
     * (tests/klipper_switch/test_switch.c and tests/test_operation_core.c). */
    diag_gstat[0] = 5;         /* reset + drv_err. */
    diag_drv_status[0] = 0x12345678u;
    diag_gstat[3] = 0xffu;     /* Only the low byte of gstat is meaningful. */
    diag_drv_status[3] = 0x1u; /* otpw. */
    uint32_t args[] = {(uint32_t)(INSTANCE >> 32), (uint32_t)INSTANCE, 20, 0};
    call("km_diag_part ", args);
    assert(!strncmp(response, "km_diag_bay_bounce ", 19));
    assert(values[2] == 20);
    for (unsigned i = 0; i < 8; ++i) assert(values[3 + i] == 0);

    args[3] = 1; call("km_diag_part ", args);
    assert(!strncmp(response, "km_diag_bay_idle ", 17));
    for (unsigned i = 3; i < 14; ++i) assert(values[i] == 0);

    args[3] = 2; call("km_diag_part ", args);
    assert(!strncmp(response, "km_diag_load_bounce ", 20));
    args[3] = 3; call("km_diag_part ", args);
    assert(!strncmp(response, "km_diag_load_idle ", 18));

    args[3] = 4; call("km_diag_part ", args);
    assert(!strncmp(response, "km_diag_tmc0 ", 13));
    assert(values[3] == 5 && values[4] == 0 && values[5] == 0 && values[6] == 0xffu);
    assert(values[7] == 0x12345678u && values[8] == 0 && values[9] == 0
           && values[10] == 0x1u);

    args[3] = 5; call("km_diag_part ", args);
    assert(!strncmp(response, "km_diag_tmc1 ", 13));
    for (unsigned i = 3; i < 11; ++i) assert(values[i] == 0);

    args[3] = 6; call("km_diag_part ", args); expect_result(KM_COMMAND_INVALID);
    args[3] = 0; args[2] = 0; call("km_diag_part ", args); expect_result(KM_COMMAND_INVALID);
    args[2] = 1; call("km_diag_part ", args); expect_result(KM_COMMAND_STALE);

    /* Shares the status/sensor query high-water: a replayed query serves the
     * retained capture rather than re-reading the registers. */
    diag_gstat[0] = 9;
    args[3] = 4; args[2] = 20; call("km_diag_part ", args);
    assert(values[3] == 5);
    uint32_t q[] = {20}; call("km_transport_query ", q);
    assert(values[3] == 20); /* Shared with snapshot/sensor/diag. */
}

static void test_lease_and_stop(void)
{
    attach(); heartbeat();
    uint32_t op[8]; args_identity(op, REQUEST, KM_OP_TYPE_LOAD, 2);
    call("km_start ", op); expect_admission(KM_REQ_ACCEPTED);
    advance(20, true);
    uint32_t args[] = {(uint32_t)(INSTANCE >> 32), (uint32_t)INSTANCE, SESSION - 1};
    call("km_heartbeat ", args); expect_result(KM_COMMAND_STALE);
    args[2] = SESSION + 1; call("km_heartbeat ", args); expect_result(KM_COMMAND_STALE);
    assert(core.last_host_heartbeat_ms == 0);
    heartbeat(); assert(core.last_host_heartbeat_ms == ms);
    ++args[0]; call("km_stop ", args); expect_result(KM_COMMAND_INSTANCE_MISMATCH);
    assert(feeders[2].base.enabled);
    --args[0]; call("km_stop ", args); expect_result(KM_COMMAND_OK);
    assert(core.system_fault == KM_FAULT_EMERGENCY_STOP && core.trajectory.position_uncertain);
    heartbeat(); /* Reconnection never clears a physical fault or resumes output. */
    assert(core.system_fault == KM_FAULT_EMERGENCY_STOP && !feeders[2].base.enabled);
}

static void test_shutdown(bool clock_fault)
{
    attach(); heartbeat();
    uint32_t snap[] = {(uint32_t)(INSTANCE >> 32), (uint32_t)INSTANCE, 1, 0};
    call("km_snapshot_part ", snap);
    uint32_t old[24]; memcpy(old, values, sizeof(values));
    uint32_t args[8]; args_identity(args, REQUEST, KM_OP_TYPE_LOAD, 2);
    uint32_t before = response_count;
    test_shim_use_longjmp = true;
    if (setjmp(test_shim_shutdown_jump) == 0) {
        if (clock_fault) {
            shim_advance_ticks(UINT32_C(0x80000000));
            call("km_start ", args);
        } else shutdown("review shutdown");
        assert(false);
    }
    test_shim_use_longjmp = false;
    assert(response_count == before && !core.shared_path_owned);
    test_shim_is_shutdown = false; /* Equivalent to upstream clearing its latch. */
    call("km_start ", args); expect_result(KM_COMMAND_SHUTDOWN);
    assert(km_commands_attach(&core, INSTANCE, changed, true) == KM_ERR_FAULTED);
    call("km_snapshot_part ", snap);
    assert(!memcmp(old, values, value_count * sizeof(uint32_t)));
    ++snap[2]; call("km_snapshot_part ", snap); expect_result(KM_COMMAND_SHUTDOWN);
    uint32_t query[] = {55}; call("km_transport_query ", query); assert(values[4] == 2);
}

static void test_lease_expiry(void)
{
    attach(); heartbeat();
    uint32_t args[8]; args_identity(args, REQUEST, KM_OP_TYPE_LOAD, 2);
    call("km_start ", args); expect_admission(KM_REQ_ACCEPTED);
    advance(core.profile.host_watchdog_ms - 1u, true);
    assert(core.system_fault == KM_FAULT_NONE && core.shared_path_owned);
    /* A heartbeat arrives at the lease deadline, before the next 1ms core
     * update. It must not erase the missed lease and conceal that deadline. */
    shim_advance_ticks(520000);
    uint32_t beat[] = {(uint32_t)(INSTANCE >> 32), (uint32_t)INSTANCE, SESSION};
    call("km_heartbeat ", beat); expect_result(KM_COMMAND_STALE);
    assert(core.system_fault == KM_FAULT_HOST_COMM_LOST && !core.host_connected);
    assert(!feeders[2].base.enabled && core.trajectory.position_uncertain);
    uint32_t events = core.event_queue.count;
    heartbeat();
    assert(core.host_connected && core.system_fault == KM_FAULT_HOST_COMM_LOST);
    assert(core.event_queue.count == events && !feeders[2].base.enabled);
}

static void test_start_permission(void)
{
    shim_run_init(); shim_set_board_ready(true); shim_run_task();
    assert(km_commands_attach(&core, INSTANCE, changed, false) == KM_OK);
    heartbeat();
    uint32_t args[8]; args_identity(args, REQUEST, KM_OP_TYPE_LOAD, 2);
    km_operation_core_t copy; memcpy(&copy, &core, sizeof(copy));
    uint32_t changes = changed_count;
    call("km_start ", args); expect_result(KM_COMMAND_UNAVAILABLE);
    assert(values[2] == KM_COMMAND_START && values[3] == REQUEST);
    args[0] ^= 1; call("km_start ", args); expect_result(KM_COMMAND_INSTANCE_MISMATCH); args[0] ^= 1;
    args[5] = 256; call("km_start ", args); expect_result(KM_COMMAND_INVALID); args[5] = KM_OP_TYPE_LOAD;
    args[7] = 2; call("km_start ", args); expect_result(KM_COMMAND_INVALID); args[7] = KM_DIR_FORWARD;
    assert(km_commands_attach(&core, INSTANCE, changed, true) == KM_ERR_BUSY);
    call("km_start ", args); expect_result(KM_COMMAND_UNAVAILABLE);
    assert(!memcmp(&copy, &core, sizeof(copy)) && changed_count == changes);
    uint32_t query[] = {7}; call("km_transport_query ", query); assert(values[4] == 1);
    uint32_t snap[] = {(uint32_t)(INSTANCE >> 32), (uint32_t)INSTANCE, 1, 0};
    call("km_snapshot_part ", snap); assert(!strncmp(response, "km_status_", 10));
    /* A capture first collects the edge output; it changes nothing. */
    assert(!memcmp(&copy, &core, sizeof(copy)) && changed_count == changes + 1);
    uint32_t stop[] = {(uint32_t)(INSTANCE >> 32), (uint32_t)INSTANCE};
    call("km_stop ", stop); expect_result(KM_COMMAND_OK);
    assert(core.system_fault == KM_FAULT_EMERGENCY_STOP && core.trajectory.position_uncertain);
    assert(changed_count == changes + 2);
}

static void test_permission_upgrade(void)
{
    shim_run_init(); shim_set_board_ready(true); shim_run_task();
    assert(km_commands_attach(&core, INSTANCE, changed, false) == KM_OK);
    heartbeat();
    uint32_t args[8]; args_identity(args, REQUEST, KM_OP_TYPE_LOAD, 2);
    call("km_start ", args); expect_result(KM_COMMAND_UNAVAILABLE);
    assert(km_commands_enable_start(&core, INSTANCE + 1u, ready_cb) == KM_ERR_BUSY);
    assert(km_commands_enable_start(&core, INSTANCE, ready_cb) == KM_OK);
    call("km_start ", args); expect_admission(KM_REQ_ACCEPTED);
    km_commands_revoke_start(&core, INSTANCE + 1u); /* Foreign revoke is inert. */
    assert(km_commands_enable_start(&core, INSTANCE, ready_cb) == KM_OK);
    km_commands_revoke_start(&core, INSTANCE);
    call("km_start ", args); expect_result(KM_COMMAND_UNAVAILABLE);
    assert(km_commands_enable_start(&core, INSTANCE, ready_cb) == KM_ERR_FAULTED);
}

static void test_permission_board_loss(void)
{
    shim_run_init(); shim_set_board_ready(true); shim_run_task();
    assert(km_commands_attach(&core, INSTANCE, changed, false) == KM_OK);
    assert(km_commands_enable_start(&core, INSTANCE, ready_cb) == KM_OK);
    physical_ready = false;
    uint32_t args[8]; args_identity(args, REQUEST, KM_OP_TYPE_LOAD, 2);
    call("km_start ", args); expect_result(KM_COMMAND_UNAVAILABLE);
    physical_ready = true;
    assert(km_commands_enable_start(&core, INSTANCE, ready_cb) == KM_ERR_FAULTED);
    call("km_start ", args); expect_result(KM_COMMAND_UNAVAILABLE);
}

static void test_permission_disabled(void)
{
    shim_run_init(); shim_set_board_ready(true); shim_run_task();
    assert(km_commands_attach(&core, INSTANCE, changed, true) == KM_ERR_UNAVAILABLE);
    assert(km_commands_attach(&core, INSTANCE, changed, false) == KM_OK);
    assert(km_commands_enable_start(&core, INSTANCE, ready_cb) == KM_ERR_UNAVAILABLE);
    uint32_t args[8]; args_identity(args, REQUEST, KM_OP_TYPE_LOAD, 2);
    call("km_start ", args); expect_result(KM_COMMAND_UNAVAILABLE);
}

static void test_permission_shutdown(void)
{
    shim_run_init(); shim_set_board_ready(true); shim_run_task();
    assert(km_commands_attach(&core, INSTANCE, changed, false) == KM_OK);
    assert(km_commands_enable_start(&core, INSTANCE, ready_cb) == KM_OK);
    shim_run_shutdown();
    test_shim_is_shutdown = false;
    assert(km_commands_enable_start(&core, INSTANCE, ready_cb) == KM_ERR_FAULTED);
    uint32_t args[8]; args_identity(args, REQUEST, KM_OP_TYPE_LOAD, 2);
    call("km_start ", args); expect_result(KM_COMMAND_SHUTDOWN);
}

static void reconcile(uint32_t query, uint32_t occupied, uint32_t lane,
                      enum km_command_result code, km_result_t status)
{
    uint32_t args[] = {(uint32_t)(INSTANCE >> 32), (uint32_t)INSTANCE, query, occupied, lane};
    if (code == KM_COMMAND_OK) {
        uint32_t snapshot[] = {args[0], args[1], query, 0};
        call("km_snapshot_part ", snapshot);
    }
    call("km_path_reconcile ", args);
    assert(!strncmp(response, "km_path_reconcile_result ", 25));
    assert(value_count == 5 && values[2] == query && values[3] == (uint32_t)code);
    assert(values[4] == (uint32_t)(-(int)status));
}

static void test_reconcile_wire(void)
{
    bool bays[KM_MAX_LANES] = {true, true, true, true};
    reconcile(1, 0, KM_LANE_UNKNOWN, KM_COMMAND_UNBOUND, KM_OK);
    attach();
    /* Production batch acquisition at boot: shared HUB, no source lane. */
    assert(km_core_set_presence_at(&core, bays, true, false, true, ms) == KM_OK);
    assert(core.path_occupied && core.path_occupant_lane == KM_LANE_UNKNOWN);
    km_operation_core_t copy; memcpy(&copy, &core, sizeof(copy));
    uint32_t changes = changed_count;
    uint32_t wrong[] = {(uint32_t)(INSTANCE >> 32), (uint32_t)INSTANCE ^ 1u, 2, 1, 2};
    call("km_path_reconcile ", wrong);
    assert(values[3] == KM_COMMAND_INSTANCE_MISMATCH && values[4] == 0);
    reconcile(0, 1, 2, KM_COMMAND_INVALID, KM_OK);
    reconcile(3, 2, 2, KM_COMMAND_INVALID, KM_OK);
    reconcile(4, 1, 256 + 2, KM_COMMAND_INVALID, KM_OK);
    assert(!memcmp(&copy, &core, sizeof(copy)) && changed_count == changes);

    reconcile(5, 0, KM_LANE_UNKNOWN, KM_COMMAND_OK, KM_ERR_CONFLICT);
    reconcile(6, 1, 4, KM_COMMAND_OK, KM_ERR_INVALID_PARAM);
    reconcile(7, 0, 2, KM_COMMAND_OK, KM_ERR_INVALID_PARAM);
    assert(core.path_occupied && core.path_occupant_lane == KM_LANE_UNKNOWN);
    assert(km_core_set_presence_at(&core, bays, false, false, true, ms) == KM_OK);
    assert(core.path_occupied); /* Absence alone never erases retained occupancy. */
    reconcile(8, 0, KM_LANE_UNKNOWN, KM_COMMAND_OK, KM_OK);
    assert(!core.path_occupied && core.op_state == KM_OP_STATE_IDLE);
    reconcile(8, 0, KM_LANE_UNKNOWN, KM_COMMAND_STALE, KM_OK);
    reconcile(9, 0, KM_LANE_UNKNOWN, KM_COMMAND_OK, KM_ERR_STALE);

    assert(km_core_set_presence_at(&core, bays, true, true, true, ms) == KM_OK);
    uint32_t unload[8]; args_identity(unload, REQUEST, KM_OP_TYPE_UNLOAD, 2);
    call("km_start ", unload); expect_admission(KM_REQ_UNAVAILABLE);
    reconcile(10, 1, 2, KM_COMMAND_OK, KM_OK);
    assert(core.path_occupant_lane == 2 && core.op_state == KM_OP_STATE_LOADED);
    for (unsigned lane = 0; lane < 4; ++lane) assert(!feeders[lane].base.enabled);
    assert(!core.event_queue.count && core.client_request[KM_CLIENT_HOST] == 0);
    heartbeat();
    uint32_t other[8]; args_identity(other, REQUEST, KM_OP_TYPE_UNLOAD, 1);
    call("km_start ", other); expect_admission(KM_REQ_PATH_OCCUPIED);
    call("km_start ", unload); expect_admission(KM_REQ_ACCEPTED);
    reconcile(11, 0, KM_LANE_UNKNOWN, KM_COMMAND_OK, KM_ERR_BUSY);
    shim_run_shutdown();
    test_shim_is_shutdown = false;
    reconcile(12, 0, KM_LANE_UNKNOWN, KM_COMMAND_SHUTDOWN, KM_OK);
}

/* Matches mcu/commands.c's KM_DRIVER_RESTART_SELECTOR (private to that
 * file): the reserved lane value naming the selector's own TMC2209 for
 * km_driver_restart, instead of a real lane. */
#define SELECTOR_LANE 254u

/* Feeder composition stand-ins for the TMC build's restart command. */
static km_result_t restart_status = KM_ERR_UNAVAILABLE;
static unsigned restart_calls;
static uint32_t restart_lane;
static bool foreign_lane3;
km_result_t km_tmc_feeder_restart(uint32_t slot)
{
    assert(!test_shim_irq_is_disabled()); /* The UART restart needs IRQs enabled. */
    ++restart_calls;
    restart_lane = slot;
    return restart_status;
}
km_driver_t *km_tmc_feeder_get_driver(uint32_t slot)
{
    return slot < 4 && !(slot == 3 && foreign_lane3) ? &feeders[slot].base : NULL;
}

/* mcu/switch_gpio.c is not linked here (tests/klipper_switch/test_switch.c
 * covers its bounce counting); no switch is bound. */
void km_switch_gpio_get_bounce(uint16_t bay[KM_MAX_LANES], uint16_t load[KM_MAX_LANES],
                               uint16_t *hub, uint16_t *toolhead)
{
    for (unsigned i = 0; i < KM_MAX_LANES; ++i) bay[i] = load[i] = 0;
    *hub = *toolhead = 0;
}
void km_switch_gpio_get_selector_bounce(uint16_t *selector) { *selector = 0; }

/* Stand-in for the diagnostic gstat/drv_status read (docs/diagnostics.md);
 * mcu/tmc_uart_gpio.c is not linked here. */
km_result_t km_tmc_uart_gpio_get_status(uint32_t slot, km_tmc_uart_status_t *out)
{
    if (slot >= 4) return KM_ERR_UNAVAILABLE;
    *out = (km_tmc_uart_status_t){0};
    out->gstat = diag_gstat[slot];
    out->drv_status = diag_drv_status[slot];
    return KM_OK;
}

static void restart(uint32_t query, uint32_t lane, enum km_command_result code,
                    km_result_t status, bool capture)
{
    uint32_t args[] = {(uint32_t)(INSTANCE >> 32), (uint32_t)INSTANCE, query, lane};
    if (capture) {
        uint32_t snapshot[] = {args[0], args[1], query, 0};
        call("km_snapshot_part ", snapshot);
    }
    call("km_driver_restart ", args);
    assert(!strncmp(response, "km_driver_restart_result ", 25));
    assert(value_count == 5 && values[2] == query && values[3] == (uint32_t)code);
    assert(values[4] == (uint32_t)(-(int)status));
}

static void test_driver_restart_wire(void)
{
    restart(1, 1, KM_COMMAND_UNBOUND, KM_OK, false);
    attach(); heartbeat();
    uint32_t wrong[] = {(uint32_t)(INSTANCE >> 32), (uint32_t)INSTANCE ^ 1u, 1, 1};
    call("km_driver_restart ", wrong);
    assert(values[3] == KM_COMMAND_INSTANCE_MISMATCH && values[4] == 0);
    restart(1, SELECTOR_LANE, KM_COMMAND_OK, KM_ERR_NOT_SUPPORTED, true); /* No selector on this profile. */
    assert(!restart_calls);
    restart(0, 1, KM_COMMAND_INVALID, KM_OK, false);
    restart(2, 256 + 1, KM_COMMAND_INVALID, KM_OK, true);
    restart(3, 1, KM_COMMAND_STALE, KM_OK, false); /* Not the latest capture. */
    assert(!restart_calls);

    km_operation_core_t copy; memcpy(&copy, &core, sizeof(copy));
    restart(4, 1, KM_COMMAND_OK, KM_ERR_UNAVAILABLE, true); /* Started. */
    assert(restart_calls == 1 && restart_lane == 1);
    restart(4, 1, KM_COMMAND_STALE, KM_OK, false); /* One attempt per capture. */
    restart(5, 4, KM_COMMAND_OK, KM_ERR_INVALID_PARAM, true);
    foreign_lane3 = true;
    restart(6, 3, KM_COMMAND_OK, KM_ERR_NOT_SUPPORTED, true);
    assert(restart_calls == 1 && !memcmp(&copy, &core, sizeof(copy)));

    uint32_t load[8]; args_identity(load, REQUEST, KM_OP_TYPE_LOAD, 2);
    call("km_start ", load); expect_admission(KM_REQ_ACCEPTED);
    advance(5, false);
    assert(feeders[2].base.enabled && core.stage != KM_STAGE_STOPPED);
    restart(7, 1, KM_COMMAND_OK, KM_ERR_BUSY, true); /* Another lane is moving. */
    restart(8, 2, KM_COMMAND_OK, KM_ERR_BUSY, true);
    assert(restart_calls == 1);
    uint32_t stop[] = {(uint32_t)(INSTANCE >> 32), (uint32_t)INSTANCE};
    call("km_stop ", stop); expect_result(KM_COMMAND_OK);
    assert(core.op_state == KM_OP_STATE_FAULT && !feeders[2].base.enabled);

    /* A stopped fault keeps its owner; the restart touches only the driver. */
    restart_status = KM_OK;
    memcpy(&copy, &core, sizeof(copy));
    restart(9, 2, KM_COMMAND_OK, KM_OK, true);
    assert(restart_calls == 2 && restart_lane == 2);
    assert(!memcmp(&copy, &core, sizeof(copy)) && core.shared_path_owned);

    uint32_t snapshot[] = {(uint32_t)(INSTANCE >> 32), (uint32_t)INSTANCE, 10, 0};
    call("km_snapshot_part ", snapshot);
    shim_advance_ticks((core.profile.max_sensor_age_ms + 1u) * 520000u);
    restart(10, 2, KM_COMMAND_STALE, KM_OK, false); /* Capture too old. */
    shim_run_shutdown();
    test_shim_is_shutdown = false;
    restart(11, 2, KM_COMMAND_SHUTDOWN, KM_OK, false);
    assert(restart_calls == 2);
}

/* "driver_restart_selector": the fixture's LINEAR selector (prepare() above)
 * has its own driver at feeders[1] (physical slot 1). The success/busy/
 * one-attempt-per-capture protocol mirrors test_driver_restart_wire's gear
 * coverage exactly; the only new thing is which driver and slot answer. */
static void test_driver_restart_selector_wire(void)
{
    attach(); heartbeat();
    restart(1, SELECTOR_LANE, KM_COMMAND_OK, KM_ERR_UNAVAILABLE, true); /* First step: chip restart. */
    assert(restart_calls == 1 && restart_lane == 1); /* Physical slot 1, not the sentinel. */
    restart(1, SELECTOR_LANE, KM_COMMAND_STALE, KM_OK, false); /* One attempt per capture. */

    feeders[1].base.enabled = true; /* The selector's own driver is stepping. */
    restart(2, SELECTOR_LANE, KM_COMMAND_OK, KM_ERR_BUSY, true);
    feeders[1].base.enabled = false;
    assert(restart_calls == 1);

    restart_status = KM_OK;
    restart(3, SELECTOR_LANE, KM_COMMAND_OK, KM_OK, true);
    assert(restart_calls == 2 && restart_lane == 1);

    /* Lane 1 shares the gear at feeders[0] on this profile (prepare()); it
     * is not its own TMC feeder either, same NOT_SUPPORTED as any lane
     * whose published driver does not match. */
    restart(4, 1, KM_COMMAND_OK, KM_ERR_NOT_SUPPORTED, true);
    assert(restart_calls == 2);
}

static void selector_gate_set(uint32_t gate, int32_t value, enum km_command_result code,
                              km_result_t status)
{
    uint32_t args[] = {(uint32_t)(INSTANCE >> 32), (uint32_t)INSTANCE, gate, (uint32_t)value};
    call("km_selector_gate_set ", args);
    assert(!strncmp(response, "km_selector_gate_set_result ", 28));
    assert(value_count == 4 && values[2] == (uint32_t)code);
    assert(values[3] == (uint32_t)(-(int)status));
}
static void servo_test(int32_t degrees, enum km_command_result code, km_result_t status)
{
    uint32_t args[] = {(uint32_t)(INSTANCE >> 32), (uint32_t)INSTANCE, (uint32_t)degrees};
    call("km_selector_servo_test ", args);
    assert(!strncmp(response, "km_selector_servo_test_result ", 30));
    assert(value_count == 4 && values[2] == (uint32_t)code);
    assert(values[3] == (uint32_t)(-(int)status));
}
static void motors_off(enum km_command_result code, km_result_t status)
{
    uint32_t args[] = {(uint32_t)(INSTANCE >> 32), (uint32_t)INSTANCE};
    call("km_selector_motors_off ", args);
    assert(!strncmp(response, "km_selector_motors_off_result ", 30));
    assert(value_count == 4 && values[2] == (uint32_t)code);
    assert(values[3] == (uint32_t)(-(int)status));
}

/* Wire-format round-trip for the two runtime selector calibration commands
 * (operation_core.h km_core_set_selector_gate / km_core_selector_servo_test).
 * NightOwl (this fixture's profile) has no selector, so both are correctly
 * refused with KM_ERR_INVALID_PARAM once the wire layer accepts the request;
 * tests/test_selector_core.c covers the accepted-write and idle-only paths
 * on an actual selector profile. */
static void test_selector_commands_wire(void)
{
    attach(); heartbeat();
    uint32_t wrong[] = {(uint32_t)(INSTANCE >> 32), (uint32_t)INSTANCE ^ 1u, 0, 5};
    call("km_selector_gate_set ", wrong);
    assert(values[2] == KM_COMMAND_INSTANCE_MISMATCH && values[3] == 0);
    selector_gate_set(0, 5, KM_COMMAND_OK, KM_ERR_INVALID_PARAM);
    selector_gate_set(300, -1, KM_COMMAND_INVALID, KM_OK); /* gate > UINT8_MAX. */

    call("km_selector_servo_test ", wrong);
    assert(values[2] == KM_COMMAND_INSTANCE_MISMATCH && values[3] == 0);
    servo_test(90, KM_COMMAND_OK, KM_ERR_INVALID_PARAM);
    servo_test(-1, KM_COMMAND_INVALID, KM_OK); /* Below 0 degrees. */
    servo_test(181, KM_COMMAND_INVALID, KM_OK); /* Above CONFIG_..._MAX_ANGLE_DEG=180. */

    call("km_selector_motors_off ", wrong);
    assert(values[2] == KM_COMMAND_INSTANCE_MISMATCH && values[3] == 0);
    motors_off(KM_COMMAND_OK, KM_ERR_INVALID_PARAM); /* No selector on this profile. */
}

/* SERVO-kind degrees semantics for km_selector_gate_set on an actual SERVO
 * profile ("selector_gate_servo" swaps it in, prepare() above): -1 clears
 * the gate to uncalibrated (servo_gate_us 0); 0 degrees is a real angle,
 * not a second "clear" (MMX's own table has one); past this image's own
 * servo angle range is refused with nothing written. */
static void test_selector_gate_set_servo_degrees(void)
{
    attach(); heartbeat();
    selector_gate_set(2, 90, KM_COMMAND_OK, KM_OK);
    assert(core.profile.selector.servo_gate_us[2] == 1500); /* KM_SELECTOR_SERVO_US(90). */
    selector_gate_set(2, -1, KM_COMMAND_OK, KM_OK);
    assert(core.profile.selector.servo_gate_us[2] == 0); /* Cleared, not "0 degrees". */
    selector_gate_set(2, 0, KM_COMMAND_OK, KM_OK);
    assert(core.profile.selector.servo_gate_us[2] == 500); /* 0 deg is a real angle. */
    selector_gate_set(2, 181, KM_COMMAND_OK, KM_ERR_INVALID_PARAM); /* Past MAX_ANGLE_DEG=180. */
    assert(core.profile.selector.servo_gate_us[2] == 500); /* Refused write changes nothing. */

    motors_off(KM_COMMAND_OK, KM_OK); /* Idle, real selector: accepted. */
    assert(!core.selector_homed && !core.selector_engaged);
}

static void capture_path(uint32_t query)
{
    uint32_t args[] = {(uint32_t)(INSTANCE >> 32), (uint32_t)INSTANCE, query, 0};
    for (uint32_t part = 0; part < 5; ++part) {
        args[3] = part; call("km_snapshot_part ", args);
    }
    for (uint32_t part = 0; part < 3; ++part) {
        args[3] = part; call("km_sensor_part ", args);
    }
}

static void test_reconciled_session(void)
{
    bool bays[KM_MAX_LANES] = {true, true, true, true};
    attach(); /* No HOST heartbeat or session exists at this occupied boot. */
    assert(km_core_set_presence_at(&core, bays, true, false, true, ms) == KM_OK);
    capture_path(1); /* Unknown source must not permit host preparation. */
    ms += core.profile.max_sensor_age_ms + 1u;
    shim_advance_ticks(ms * 520000u);
    assert(km_core_set_presence_at(&core, bays, true, false, true, ms) == KM_OK);
    reconcile(1, 1, 2, KM_COMMAND_STALE, KM_OK); /* Fresh sensors do not renew an old assertion. */
    assert(km_core_set_presence_at(&core, bays, false, false, true, ms) == KM_OK);
    reconcile(2, 0, KM_LANE_UNKNOWN, KM_COMMAND_OK, KM_OK);
    capture_path(3); /* CLEAR allows cold-session selection. */
    assert(km_core_set_presence_at(&core, bays, true, true, true, ms) == KM_OK);
    reconcile(2, 0, KM_LANE_UNKNOWN, KM_COMMAND_STALE, KM_OK);
    assert(core.path_occupied && core.path_occupant_lane == KM_LANE_UNKNOWN);
    reconcile(4, 1, 2, KM_COMMAND_OK, KM_OK);
    capture_path(5); /* ADOPT allows occupied-session selection for lane two. */
    assert(!core.host_connected && !core.event_queue.count);
    for (unsigned lane = 0; lane < 4; ++lane) assert(!feeders[lane].base.enabled);
}

/* A persistent native endpoint for Klipper's real host wire wrappers. Only
 * these commands and explicit simulated sensor-time steps are accepted. */
static uint32_t host_word(char *text)
{
    assert(text && *text && *text != '-');
    char *end;
    unsigned long long value = strtoull(text, &end, 10);
    assert(*end == '\0' && value <= UINT32_MAX);
    return (uint32_t)value;
}

static void host_session(void)
{
    attach();
    char line[256];
    while (fgets(line, sizeof(line), stdin)) {
        char *name = strtok(line, " \t\r\n");
        assert(name);
        if (!strcmp(name, "tick")) {
            char *duration = strtok(NULL, " \t\r\n");
            char *hub = strtok(NULL, " \t\r\n");
            char *toolhead = strtok(NULL, " \t\r\n");
            assert(duration && hub && toolhead);
            uint32_t span = host_word(duration);
            uint32_t hub_value = host_word(hub), toolhead_value = host_word(toolhead);
            assert(span <= 1000u && hub_value <= 1u && toolhead_value <= 1u);
            assert(strtok(NULL, " \t\r\n") == NULL);
            advance_path(span, hub_value != 0u, toolhead_value != 0u);
            puts("{\"tick\":true}");
        } else {
            const char *format = NULL;
            unsigned count = 0u;
            if (!strcmp(name, "km_heartbeat")) { format = "km_heartbeat "; count = 3u; }
            else if (!strcmp(name, "km_start")) { format = "km_start "; count = 8u; }
            else if (!strcmp(name, "km_event_peek")) { format = "km_event_peek "; count = 2u; }
            else if (!strcmp(name, "km_event_ack")) { format = "km_event_ack "; count = 8u; }
            else if (!strcmp(name, "km_load_attempts_query")) {
                format = "km_load_attempts_query "; count = 7u;
            }
            else assert(false);
            uint32_t args[8] = {0};
            for (unsigned i = 0u; i < count; ++i)
                args[i] = host_word(strtok(NULL, " \t\r\n"));
            assert(strtok(NULL, " \t\r\n") == NULL);
            call(format, args);
        }
        fflush(stdout);
    }
}

int main(int argc, char **argv)
{
    assert(argc == 2);
    servo_selector_requested = !strcmp(argv[1], "selector_gate_servo");
    stepper_selector_requested = !strcmp(argv[1], "driver_restart_selector");
    prepare();
    if (!strcmp(argv[1], "host_session")) host_session();
    else if (!strcmp(argv[1], "lifecycle")) test_lifecycle();
    else if (!strcmp(argv[1], "requests")) test_requests();
    else if (!strcmp(argv[1], "events")) test_events();
    else if (!strcmp(argv[1], "preload")) test_preload_wire();
    else if (!strcmp(argv[1], "snapshot")) test_snapshot();
    else if (!strcmp(argv[1], "sensor")) test_sensor_snapshot();
    else if (!strcmp(argv[1], "diag")) test_diag_wire();
    else if (!strcmp(argv[1], "lease")) test_lease_and_stop();
    else if (!strcmp(argv[1], "shutdown")) test_shutdown(false);
    else if (!strcmp(argv[1], "clock_fault")) test_shutdown(true);
    else if (!strcmp(argv[1], "lease_expiry")) test_lease_expiry();
    else if (!strcmp(argv[1], "start_permission")) test_start_permission();
    else if (!strcmp(argv[1], "permission_upgrade")) test_permission_upgrade();
    else if (!strcmp(argv[1], "permission_shutdown")) test_permission_shutdown();
    else if (!strcmp(argv[1], "permission_board_loss")) test_permission_board_loss();
    else if (!strcmp(argv[1], "permission_disabled")) test_permission_disabled();
    else if (!strcmp(argv[1], "reconcile")) test_reconcile_wire();
    else if (!strcmp(argv[1], "reconciled_session")) test_reconciled_session();
    else if (!strcmp(argv[1], "driver_restart")) test_driver_restart_wire();
    else if (!strcmp(argv[1], "driver_restart_selector")) test_driver_restart_selector_wire();
    else if (!strcmp(argv[1], "selector_commands")) test_selector_commands_wire();
    else if (!strcmp(argv[1], "selector_gate_servo")) test_selector_gate_set_servo_degrees();
    else assert(false);
    return 0;
}
