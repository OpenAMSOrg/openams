// SPDX-License-Identifier: GPL-3.0-or-later
#include "tmc_feeder.h"
#include "commands.h"
#include "pin_config.h"
#include "feeder_config.h"
#include "tmc_config.h"
#include "autoconf.h"
#include "clock.h"
#include "shim_tmc.h"

/* STEP/DIR is Klipper's local stepper (mcu/stepdir_gpio.c queues moves for it),
 * linked here as the real patched src/stepper.c. */
#include "stepper.h"

#include "board/irq.h"
#include "board/gpio.h"
#include "board/misc.h"
#include <assert.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <unistd.h>
#include <sys/wait.h>
#include "basecmd.h"

/* Public host-command stubs for shared Klipper tmcuart.c */
void *oid_alloc(uint8_t oid, oid_type type, size_t size)
{
    (void)oid; (void)type; (void)size; abort();
}
void *oid_lookup(uint8_t oid, oid_type type)
{
    (void)oid; (void)type; abort();
}
void *review_oid(uint8_t oid, oid_type type)
{
    (void)oid; (void)type; return NULL;
}
void *command_decode_ptr(uint32_t value)
{
    return (void *)(uintptr_t)value;
}

static unsigned gpio_ops, raw_calls, raw_clock_reads;
static bool inside_raw;
static uint8_t pin_level[SHIM_MAX_PINS];
#ifdef KM_TEST_IMAGE_AUTOCONF
static bool local_pin[SHIM_MAX_PINS];
#endif

#ifdef KM_TEST_IMAGE_AUTOCONF
static km_pin_config_t image_pins;
static km_step_dir_config_t image_step[KM_MAX_LANES];
static km_tmc_uart_config_t image_tmc[KM_MAX_LANES];
#endif

struct gpio_out __real_gpio_out_setup(uint32_t pin, uint32_t val);
void __real_gpio_out_reset(struct gpio_out g, uint32_t val);
void __real_gpio_out_write(struct gpio_out g, uint32_t val);
struct gpio_in __real_gpio_in_setup(uint32_t pin, int32_t pull_up);
void __real_gpio_in_reset(struct gpio_in g, int32_t pull_up);
void __real_km_stepdir_gpio_force_off(void *context);
uint32_t __real_timer_read_time(void);

struct gpio_out __wrap_gpio_out_setup(uint32_t pin, uint32_t val)
{
    ++gpio_ops;
    if (pin < SHIM_MAX_PINS) pin_level[pin] = (uint8_t)(val != 0u);
    return __real_gpio_out_setup(pin, val);
}
void __wrap_gpio_out_reset(struct gpio_out g, uint32_t val)
{
    ++gpio_ops;
    if (g.pin < SHIM_MAX_PINS) pin_level[g.pin] = (uint8_t)(val != 0u);
    __real_gpio_out_reset(g, val);
}
void __wrap_gpio_out_write(struct gpio_out g, uint32_t val)
{
    ++gpio_ops;
    if (g.pin < SHIM_MAX_PINS) pin_level[g.pin] = (uint8_t)(val != 0u);
    __real_gpio_out_write(g, val);
}
void __wrap_gpio_out_toggle_noirq(struct gpio_out g)
{
    ++gpio_ops;
    if (g.pin < SHIM_MAX_PINS) pin_level[g.pin] = pin_level[g.pin] ? 0u : 1u;
    __real_gpio_out_write(g, pin_level[g.pin]);
}
struct gpio_in __wrap_gpio_in_setup(uint32_t pin, int32_t pull_up)
{
    ++gpio_ops;
    return __real_gpio_in_setup(pin, pull_up);
}
#ifdef KM_TEST_IMAGE_AUTOCONF
int gpio_local_reserve(const uint32_t *pins, uint32_t count)
{
    if (!pins || !count) return -1;
    for (uint32_t i = 0; i < count; ++i) {
        if (pins[i] >= SHIM_MAX_PINS) return -1;
        local_pin[pins[i]] = true;
    }
    return 0;
}
int gpio_pin_is_local(uint32_t pin)
{
    /* The native fixture models Klipper's local ownership layer: these are
     * virtual canonical pins, accepted by the configured H723 pin factory. */
    return pin < SHIM_MAX_PINS;
}
struct gpio_out gpio_out_setup_local(uint32_t pin, uint32_t value)
{
    assert(gpio_pin_is_local(pin));
    return __wrap_gpio_out_setup(pin, value);
}
struct gpio_in gpio_in_setup_local(uint32_t pin, int32_t pull_up)
{
    assert(gpio_pin_is_local(pin));
    return __wrap_gpio_in_setup(pin, pull_up);
}
#endif
void __wrap_gpio_in_reset(struct gpio_in g, int32_t pull_up)
{
    ++gpio_ops;
    __real_gpio_in_reset(g, pull_up);
}
void __wrap_km_stepdir_gpio_force_off(void *context)
{
    ++raw_calls;
    inside_raw = true;
    __real_km_stepdir_gpio_force_off(context);
    inside_raw = false;
}
uint32_t __wrap_timer_read_time(void)
{
    if (inside_raw) ++raw_clock_reads;
    return __real_timer_read_time();
}

static km_step_dir_config_t step_config(void)
{
    return (km_step_dir_config_t){
        .enable_active_high = true, .step_active_high = true,
        .forward_high = true, .pulse_high_us = 10, .pulse_low_us = 10,
        
        
        .maximum_velocity_steps_s = 5000
    };
}
static km_tmc2209_config_t tmc_config(uint8_t address)
{
    return (km_tmc2209_config_t){
        .address = address, .sense_resistor_milliohms = 110,
        .maximum_current_ma = 1500, .run_current_ma = 500,
        .hold_current_ma = 200, .microsteps = 16,
        .interpolate = true, .stealthchop = true,
        .transaction_timeout_us = 10000, .diagnostic_interval_us = 100000
    };
}
static void init_clock_hz(uint32_t frequency)
{
    shim_reset(frequency);
    shim_set_ticks(10000u);
    shim_run_init();
    shim_set_board_ready(true);
    shim_run_task();
    assert(km_clock_get_state() == KM_CLOCK_STATE_READY);
    gpio_ops = raw_calls = raw_clock_reads = 0;
}
static void init_clock(void)
{
#ifdef KM_TEST_IMAGE_AUTOCONF
    init_clock_hz(CONFIG_CLOCK_FREQ);
#else
    init_clock_hz(20000000u);
#endif
}
static km_result_t setup_lane(uint32_t lane, uint32_t base)
{
    km_step_dir_config_t step = step_config();
    km_tmc2209_config_t tmc = tmc_config((uint8_t)lane);
    shim_attach_virtual_tmc(base + 3u, (uint8_t)lane, test_clock_freq);
    return km_tmc_feeder_setup(lane, base, base + 1u, base + 2u,
                                base + 3u, true, &step, &tmc, 500, 200);
}

static void dispatch_step(void)
{
    assert(shim_find_earliest_timer() != NULL);
    irqstatus_t flag = irq_save();
    shim_dispatch_earliest_timer();
    irq_restore(flag);
    shim_run_task();
}

static void dispatch_until_ready(uint32_t lane)
{
    for (unsigned step = 0; step < 50000 && !km_tmc_uart_gpio_is_ready(lane); ++step) {
        assert(km_tmc_uart_gpio_get_fault(lane) == KM_FAULT_NONE);
        dispatch_step();
    }
    assert(km_tmc_uart_gpio_is_ready(lane));
}
static void dispatch_until_fault(uint32_t lane)
{
    for (unsigned step = 0; step < 50000 && km_tmc_uart_gpio_get_fault(lane) == KM_FAULT_NONE; ++step) {
        dispatch_step();
    }
    assert(km_tmc_uart_gpio_get_fault(lane) != KM_FAULT_NONE);
}
static km_result_t enable(km_driver_t *d)
{
    irqstatus_t flag = irq_save();
    km_result_t res = km_driver_set_enable(d, true);
    irq_restore(flag);
    return res;
}
static void test_validation_and_delayed_ready(void)
{
    init_clock_hz(CONFIG_CLOCK_FREQ);
    km_step_dir_config_t step = step_config();
    km_tmc2209_config_t tmc = tmc_config(0);
    assert(km_tmc_feeder_setup(0, 10, 11, 12, 13, true,
                               &step, &tmc, 501, 200) == KM_ERR_INVALID_PARAM);
    assert(km_tmc_feeder_setup(0, 10, 11, 12, 12, true,
                               &step, &tmc, 500, 200) == KM_ERR_INVALID_PARAM);
    step.pulse_high_us = 0;
    assert(km_tmc_feeder_setup(0, 10, 11, 12, 13, true,
                               &step, &tmc, 500, 200) == KM_ERR_INVALID_PARAM);
    step = step_config();
    tmc.hold_current_ma = 0;
    assert(km_tmc_feeder_setup(0, 10, 11, 12, 13, true,
                               &step, &tmc, 500, 0) == KM_ERR_INVALID_PARAM);
    tmc = tmc_config(0);
#ifndef KM_TEST_IMAGE_AUTOCONF
    test_clock_freq = 12288000u; /* UART cannot use this physical tick rate. */
    assert(km_tmc_feeder_setup(0, 10, 11, 12, 13, true,
                               &step, &tmc, 500, 200) == KM_ERR_INVALID_PARAM);
#else
    /* In image mode CONFIG_CLOCK_FREQ is the exact compile-time image value;
     * it cannot be replaced by this shim's runtime frequency as in defaults. */
#endif
#ifdef KM_TEST_IMAGE_AUTOCONF
    test_clock_freq = CONFIG_CLOCK_FREQ;
#else
    test_clock_freq = 20000000u;
#endif
    assert(gpio_ops == 0);
    assert(km_tmc_feeder_get_driver(0) == NULL);

    assert(setup_lane(0, 10) == KM_OK);
    assert(km_tmc_feeder_get_driver(0) == NULL);
    assert(km_tmc_feeder_activate(0) == KM_ERR_UNAVAILABLE);
    assert(pin_level[12] == 0);
    assert(km_tmc_feeder_setup(0, 20, 21, 22, 23, true,
                               &step, &(km_tmc2209_config_t){0}, 500, 200) == KM_ERR_BUSY);
    unsigned before = gpio_ops;
    assert(km_tmc_feeder_setup(1, 20, 21, 12, 23, true,
                               &step, &(km_tmc2209_config_t){0}, 500, 200) == KM_ERR_INVALID_PARAM);
    assert(gpio_ops == before);
    dispatch_until_ready(0);
    assert(km_tmc_feeder_activate(0) == KM_OK);
    km_driver_t *d = km_tmc_feeder_get_driver(0);
    assert(d && d->initialized && !d->enabled && !d->caps.supports_current_setpoint);
    assert(d->run_current_ma == 500 && d->hold_current_ma == 200);
    assert(pin_level[12] == 0 && enable(d) == KM_OK);
    assert(pin_level[12] == 1);
    assert(km_tmc_feeder_activate(0) == KM_ERR_BUSY);
    assert(raw_calls > 0 && raw_clock_reads == 0);
}

static void test_cross_claims_and_failed_startup(void)
{
    init_clock();
    assert(setup_lane(0, 10) == KM_OK);
    km_step_dir_config_t step = step_config();
    km_tmc2209_config_t tmc = tmc_config(1);
    unsigned before = gpio_ops;
    /* An ENABLE-pin clash against lane 0 is caught before any GPIO touch. */
    assert(km_tmc_feeder_setup(1, 20, 21, 12, 23, true,
                               &step, &tmc, 500, 200) == KM_ERR_CONFLICT);
    assert(gpio_ops == before);

    /* Reusing lane 0's own UART pin (13) at the *same* address it already
     * uses is refused too, but only once km_tmc_uart_gpio_setup itself
     * checks the address -- this slot's own distinct STEP/DIR/ENABLE pins
     * (30/31/32) are prepared and immutably claimed first, the same as any
     * other UART setup failure. docs/selectors.md's one legitimate shared-
     * wire case (test_shared_uart_pin_*) needs a genuinely distinct address
     * to tell the two chips apart, which this attempt deliberately lacks. */
    km_tmc2209_config_t same_address = tmc_config(0);
    assert(km_tmc_feeder_setup(2, 30, 31, 32, 13, true,
                               &step, &same_address, 500, 200) == KM_ERR_CONFLICT);
    assert(pin_level[32] == 0);
    assert(km_tmc_feeder_get_driver(2) == NULL);
    assert(km_tmc_feeder_setup(2, 30, 31, 32, 13, true,
                               &step, &same_address, 500, 200) == KM_ERR_BUSY);

    shim_attach_virtual_tmc(23, 1, 20000000u);
    virtual_tmc_t *chip = shim_get_virtual_tmc(23);
    chip->no_reply = true;
    assert(km_tmc_feeder_setup(1, 20, 21, 22, 23, true,
                               &step, &tmc, 500, 200) == KM_OK);
    dispatch_until_fault(1);
    assert(km_tmc_feeder_activate(1) == KM_ERR_FAULTED);
    assert(km_tmc_feeder_get_driver(1) == NULL);
    assert(pin_level[22] == 0);
}

static void test_current_mismatch(void)
{
    init_clock();
    assert(setup_lane(0, 10) == KM_OK);
    dispatch_until_ready(0);
    assert(km_tmc_uart_gpio_set_current(0, 600, 300) == KM_OK);
    assert(km_tmc_feeder_activate(0) == KM_ERR_UNAVAILABLE);
    dispatch_until_ready(0);
    km_tmc_uart_status_t st;
    assert(km_tmc_uart_gpio_get_status(0, &st) == KM_OK);
    assert(st.ready && st.confirmed_run_ma == 600 && st.confirmed_hold_ma == 300);
    assert(km_tmc_feeder_activate(0) == KM_ERR_UNAVAILABLE);
    assert(km_tmc_feeder_get_driver(0) == NULL && pin_level[12] == 0);
}

static km_result_t setup_shared(uint32_t lane, uint32_t base, uint32_t uart_pin,
                                uint8_t address, uint32_t diagnostic_interval_us)
{
    km_step_dir_config_t step = step_config();
    km_tmc2209_config_t tmc = tmc_config(address);
    tmc.diagnostic_interval_us = diagnostic_interval_us;
    return km_tmc_feeder_setup(lane, base, base + 1u, base + 2u,
                               uart_pin, true, &step, &tmc, 500, 200);
}

/* Happy Hare's classic ERCF 1.1 EASY-BRD wiring: the gear's and the
 * selector's TMC2209 share one single-wire UART pin, told apart only by
 * address (docs/selectors.md). These three cases exercise
 * mcu/tmc_uart_gpio.c's sharing support through the real feeder composition. */
static void test_shared_uart_pin_two_addresses_both_confirm(void)
{
    init_clock();
    const uint32_t pin = 33, big_diag = 500000u; /* Well past this test's own setup time. */
    shim_attach_virtual_tmc(pin, 0, test_clock_freq);
    assert(setup_shared(0, 10, pin, 0, big_diag) == KM_OK);
    dispatch_until_ready(0);
    assert(km_tmc_uart_gpio_is_ready(0));

    /* The shim models one simulated chip per pin; re-pointing it to address
     * 1 stands in for the selector's own, differently-addressed TMC2209
     * answering on the same physical wire. Slot 0 already proved address 0
     * confirms above and its own generously-spaced diagnostic keeps it from
     * being disturbed by this reattachment. */
    shim_attach_virtual_tmc(pin, 1, test_clock_freq);
    assert(setup_shared(1, 20, pin, 1, big_diag) == KM_OK);
    dispatch_until_ready(1);
    assert(km_tmc_uart_gpio_is_ready(1));

    /* Sharing the wire never merged the two slots' own confirmed identity. */
    assert(km_tmc_uart_gpio_is_ready(0));
    km_tmc_uart_status_t st0, st1;
    assert(km_tmc_uart_gpio_get_status(0, &st0) == KM_OK && st0.ready
           && st0.fault == KM_FAULT_NONE);
    assert(km_tmc_uart_gpio_get_status(1, &st1) == KM_OK && st1.ready
           && st1.fault == KM_FAULT_NONE);
    uint32_t reported0, reported1;
    assert(km_tmc_uart_gpio_get_pin(0, &reported0) == KM_OK && reported0 == pin);
    assert(km_tmc_uart_gpio_get_pin(1, &reported1) == KM_OK && reported1 == pin);
}

static void test_shared_uart_pin_equal_address_refused(void)
{
    init_clock();
    const uint32_t pin = 33;
    shim_attach_virtual_tmc(pin, 0, test_clock_freq);
    assert(setup_shared(0, 10, pin, 0, 100000u) == KM_OK);
    dispatch_until_ready(0);

    /* Same wire, same address: ambiguous, refused outright, no dispatch or
     * chip traffic involved, and slot 0 is left completely untouched. */
    assert(setup_shared(1, 20, pin, 0, 100000u) == KM_ERR_CONFLICT);
    assert(km_tmc_uart_gpio_is_ready(0));
    assert(km_tmc_feeder_get_driver(1) == NULL);
}

static void test_shared_uart_pin_fault_on_one_does_not_fault_other(void)
{
    init_clock();
    const uint32_t pin = 33;
    shim_attach_virtual_tmc(pin, 0, test_clock_freq);
    assert(setup_shared(0, 10, pin, 0, 500000u) == KM_OK);
    dispatch_until_ready(0);

    /* Address 1 never matches this test's fixed address-0 chip: every one
     * of slot 1's own frames on the shared wire goes unanswered, exactly as
     * a real multidrop bus ignores a frame addressed to someone else, until
     * its own retry budget exhausts and it faults. Slot 0 keeps talking to
     * the (matching) chip throughout and must stay completely unaffected --
     * the transport-layer routing and cancellation this depends on live in
     * uart_completed/transport_abort (mcu/tmc_uart_gpio.c). */
    assert(setup_shared(1, 20, pin, 1, 500000u) == KM_OK);
    dispatch_until_fault(1);
    assert(km_tmc_uart_gpio_get_fault(0) == KM_FAULT_NONE);
    assert(km_tmc_uart_gpio_is_ready(0));
    assert(km_tmc_feeder_get_driver(1) == NULL);
}

static void test_fault_high_step_restart_and_isolation(void)
{
    init_clock();
    assert(setup_lane(0, 10) == KM_OK);
    assert(setup_lane(1, 20) == KM_OK);
    dispatch_until_ready(0);
    dispatch_until_ready(1);
    assert(km_tmc_feeder_activate(0) == KM_OK);
    assert(km_tmc_feeder_activate(1) == KM_OK);
    km_driver_t *d0 = km_tmc_feeder_get_driver(0);
    km_driver_t *d1 = km_tmc_feeder_get_driver(1);
    assert(d0 && d1 && enable(d0) == KM_OK && enable(d1) == KM_OK);
    shim_advance_ticks(200u);
    /* Step lane 0 through its local stepper until a STEP pulse is high. */
    struct stepper *s0 = km_stepdir_gpio_get_stepper_for_driver(d0);
    assert(s0);
    uint32_t start = test_shim_ticks + timer_from_us(200u);
    assert(km_stepdir_gpio_queue(d0, start, timer_from_us(1000u), 50u, 0, 1u) == 0);
    for (unsigned i = 0; i < 100000u && pin_level[10] != 1u; ++i)
        dispatch_step();
    assert(pin_level[10] == 1u && stepper_local_active(s0));

    /* A UART fault mid-motion: the binding's safe-off drops ENABLE and stops
     * the local stepper (queue discarded, STEP back inactive) at once; lane 1
     * is untouched. */
    shim_get_virtual_tmc(13)->stuck_low = true;
    dispatch_until_fault(0);
    assert(pin_level[12] == 0u && pin_level[10] == 0u && !stepper_local_active(s0));
    assert(pin_level[22] == 1 && km_tmc_uart_gpio_is_ready(1));
    assert(raw_clock_reads == 0);
    assert(km_driver_get_fault(d0) != KM_FAULT_NONE);
    /* Capture position after fault; the local stepper may have stepped further
     * before the fault was detected. */
    int64_t post_fault_position = km_stepdir_gpio_get_position_for_driver(d0);
    /* No further step, and the position stays where the fault found it. */
    for (unsigned i = 0; i < 200u; ++i) {
        dispatch_step();
        assert(pin_level[10] == 0u);
    }
    assert(km_stepdir_gpio_get_position_for_driver(d0) == post_fault_position);

    /* The core's stop on the latched driver fault (km_core_update ->
     * km_core_explicit_stop -> km_driver_shutdown), which the removed
     * per-edge service call used to stand for here. */
    km_driver_shutdown(d0);
    assert(!d0->enabled && !d0->step_pin_high);
    assert(km_driver_get_fault(d0) != KM_FAULT_NONE);

    shim_get_virtual_tmc(13)->stuck_low = false;
    assert(km_stepdir_gpio_clear_fault(0) == KM_ERR_UNAVAILABLE); /* UART faulted */
    assert(km_tmc_uart_gpio_restart(0) == KM_OK);
    assert(km_stepdir_gpio_clear_fault(0) == KM_ERR_UNAVAILABLE); /* restarting */
    dispatch_until_ready(0);
    assert(!d0->enabled && km_driver_get_fault(d0) != KM_FAULT_NONE);
    assert(enable(d0) == KM_ERR_FAULTED);
    assert(d1->enabled && km_driver_get_fault(d1) == KM_FAULT_NONE);
    /* The explicit clear after readiness drives nothing; enabling is separate. */
    unsigned writes = gpio_ops;
    assert(km_stepdir_gpio_clear_fault(0) == KM_OK && gpio_ops == writes);
    assert(km_driver_get_fault(d0) == KM_FAULT_NONE && pin_level[12] == 0 && pin_level[10] == 0);
    assert(enable(d0) == KM_OK && pin_level[12] == 1);
    assert(d1->enabled && km_tmc_uart_gpio_is_ready(1));
}

/* Klipper command boundary joined to the real feeder, UART and core. */
void command_km_snapshot_part(uint32_t *args);
void command_km_path_reconcile(uint32_t *args);
void command_km_driver_restart(uint32_t *args);
bool km_controller_get_switch_sample_delay(uint32_t *out_us)
{
    if (out_us) *out_us = 0;
    return false;
}
void km_controller_get_switch_sample_stats(uint32_t *count, uint32_t *max_us)
{
    *count = 0;
    *max_us = 0;
}
/* Diagnostic-only bounce counters (docs/diagnostics.md); mcu/switch_gpio.c
 * is not linked here. */
void km_switch_gpio_get_bounce(uint16_t bay[KM_MAX_LANES], uint16_t load[KM_MAX_LANES],
                               uint16_t *hub, uint16_t *toolhead)
{
    for (unsigned i = 0; i < KM_MAX_LANES; ++i) bay[i] = load[i] = 0;
    *hub = *toolhead = 0;
}
void km_switch_gpio_get_selector_bounce(uint16_t *selector) { *selector = 0; }
static char response[256];
static uint32_t reply[8];
void __wrap_command_sendf(const char *format, ...)
{
    snprintf(response, sizeof(response), "%s", format);
    va_list args;
    va_start(args, format);
    unsigned count = 0;
    for (const char *p = format; (p = strchr(p, '%')) && count < 8u; ++p)
        reply[count++] = va_arg(args, uint32_t);
    va_end(args);
}

static km_operation_core_t core;
static const uint64_t INSTANCE = UINT64_C(0x1122334455667788);
static void core_changed(void) {}

static km_core_timepoint_t core_now(void)
{
    km_core_timepoint_t now;
    assert(km_clock_get_time(&now) == KM_OK);
    return now;
}
static void sense(bool path)
{
    km_core_timepoint_t now = core_now();
    for (uint8_t lane = 0; lane < 4; ++lane)
        km_core_set_sensors_at(&core, lane, true, path, path, true, now.ms);
    assert(km_buffer_update_switches(&core.buffer_sensor, false, false, now.ms) == KM_OK);
}
static void capture(uint32_t query)
{
    uint32_t args[] = {(uint32_t)(INSTANCE >> 32), (uint32_t)INSTANCE, query, 0};
    command_km_snapshot_part(args);
    assert(!strncmp(response, "km_status_identity ", 19) && reply[2] == query);
}
static void restart(uint32_t query, uint32_t lane, uint32_t code, km_result_t status)
{
    uint32_t args[] = {(uint32_t)(INSTANCE >> 32), (uint32_t)INSTANCE, query, lane};
    command_km_driver_restart(args);
    assert(!strncmp(response, "km_driver_restart_result ", 25));
    assert(reply[2] == query && reply[3] == code && reply[4] == (uint32_t)(-(int)status));
}
static void reconcile_clear(uint32_t query, km_result_t status)
{
    uint32_t args[] = {(uint32_t)(INSTANCE >> 32), (uint32_t)INSTANCE, query, 0, KM_LANE_UNKNOWN};
    command_km_path_reconcile(args);
    assert(!strncmp(response, "km_path_reconcile_result ", 25));
    assert(reply[3] == 0 && reply[4] == (uint32_t)(-(int)status));
}

/* A protection fault during LOAD stops the core. Restart lane 0 through the
 * command while its supply is still off (fails, latch kept), then with power
 * back (ready, latch cleared); lane 1 is untouched; the core reconciles. */
static void test_restart_command_and_reconcile(void)
{
    init_clock();
    assert(setup_lane(0, 10) == KM_OK && setup_lane(1, 20) == KM_OK);
    dispatch_until_ready(0);
    dispatch_until_ready(1);
    assert(km_tmc_feeder_activate(0) == KM_OK && km_tmc_feeder_activate(1) == KM_OK);
    km_driver_t *d0 = km_tmc_feeder_get_driver(0), *d1 = km_tmc_feeder_get_driver(1);
    km_device_profile_t profile;
    km_profile_init_nightowl(&profile);
    assert(km_core_init(&core, &profile) == KM_OK);
    assert(km_core_bind_lane_driver(&core, 0, d0, NULL) == KM_OK);
    assert(km_core_bind_lane_driver(&core, 1, d1, NULL) == KM_OK);
    assert(km_commands_attach(&core, INSTANCE, core_changed, false) == KM_OK);

    sense(false);
    km_core_timepoint_t now = core_now();
    km_core_host_comm_ready(&core, now.ms);
    km_request_identity_t load = {KM_CLIENT_HOST, 1, 1, KM_OP_TYPE_LOAD, 0};
    irqstatus_t flag = irq_save();
    assert(km_core_request_start(&core, &load, KM_DIR_FORWARD, now) == KM_REQ_ACCEPTED);
    irq_restore(flag);
    assert(d0->enabled && pin_level[12] == 1);
    capture(1);
    restart(1, 1, 0, KM_ERR_BUSY); /* Lane 0 is feeding. */

    virtual_tmc_t *chip0 = shim_get_virtual_tmc(13);
    chip0->registers[0x6f] = 4; /* DRV_STATUS protection at the next diagnostic */
    dispatch_until_fault(0);
    assert(pin_level[12] == 0 && km_driver_get_fault(d0) != KM_FAULT_NONE);
    now = core_now();
    flag = irq_save();
    km_core_update(&core, now.ms, now.us); /* Controller service observes it. */
    irq_restore(flag);
    assert(core.op_state == KM_OP_STATE_FAULT && core.stage == KM_STAGE_STOPPED);
    assert(!d0->enabled && !d0->step_pin_high && core.shared_path_owned);
    km_event_t event;
    while (km_event_queue_peek(&core.event_queue, &event) == KM_OK)
        assert(km_event_queue_ack(&core.event_queue, event.seq) == KM_OK);
    sense(false);
    capture(2);
    reconcile_clear(2, KM_ERR_FAULTED); /* Reconciliation never clears a driver. */

    /* Supply still off: the chip does not answer, so the restart fails. */
    unsigned lane1_frames = shim_get_virtual_tmc(23)->valid_frames;
    chip0->registers[0x6f] = 0;
    chip0->no_reply = true;
    capture(3);
    restart(3, 0, 0, KM_ERR_UNAVAILABLE);
    restart(3, 0, 4, KM_OK); /* One attempt per capture. */
    assert(pin_level[12] == 0 && km_driver_get_fault(d0) != KM_FAULT_NONE);
    dispatch_until_fault(0);
    capture(4);
    restart(4, 0, 0, KM_ERR_FAULTED);
    assert(km_driver_get_fault(d0) != KM_FAULT_NONE);

    /* Power back: a new restart confirms the chip, then clears the latch. */
    chip0->no_reply = false;
    capture(5);
    restart(5, 0, 0, KM_ERR_UNAVAILABLE);
    capture(6);
    restart(6, 0, 0, KM_ERR_UNAVAILABLE);
    dispatch_until_ready(0);
    assert(km_driver_get_fault(d0) != KM_FAULT_NONE && enable(d0) == KM_ERR_FAULTED);
    capture(7);
    restart(7, 0, 0, KM_OK);
    assert(km_driver_get_fault(d0) == KM_FAULT_NONE && !d0->enabled && pin_level[12] == 0);
    assert(core.op_state == KM_OP_STATE_FAULT); /* The command never touches the core. */

    /* The other lane kept running its own diagnostics throughout. */
    assert(km_tmc_uart_gpio_is_ready(1) && km_driver_get_fault(d1) == KM_FAULT_NONE);
    assert(shim_get_virtual_tmc(23)->valid_frames > lane1_frames);

    sense(false);
    capture(8);
    reconcile_clear(8, KM_OK);
    assert(core.op_state == KM_OP_STATE_IDLE && !core.shared_path_owned);
    assert(!core.trajectory.position_uncertain && !d0->enabled && pin_level[12] == 0);
    assert(enable(d0) == KM_OK && pin_level[12] == 1);

    /* The same protection fault latches again exactly as before. */
    chip0->registers[0x6f] = 4;
    dispatch_until_fault(0);
    assert(pin_level[12] == 0 && km_driver_get_fault(d0) != KM_FAULT_NONE);
    assert(enable(d0) == KM_ERR_FAULTED);
    assert(km_tmc_uart_gpio_is_ready(1));
}

static void test_global_shutdown(void)
{
    init_clock();
    assert(setup_lane(0, 10) == KM_OK);
    shim_run_shutdown();
    assert(test_shim_clock_reads_in_shutdown == 0);
    assert(km_tmc_feeder_activate(0) == KM_ERR_FAULTED);
    assert(km_tmc_feeder_get_driver(0) == NULL && pin_level[12] == 0);
    sched_clear_shutdown();
    assert(km_tmc_feeder_activate(0) == KM_ERR_FAULTED);
}

#ifdef KM_TEST_IMAGE_AUTOCONF
/* Exercise the exact H723 image's saved factories and the production feeder
 * setup path. The image's configured lane count is required to be four by the
 * runner before this case is built. */
static void test_image_configured_four_lane_startup(void)
{
    init_clock_hz(CONFIG_CLOCK_FREQ);
    assert(km_config_pins(&image_pins, NULL) == KM_OK);
    assert(km_config_feeders(image_step) == KM_OK);
    assert(km_config_tmc(image_tmc) == KM_OK);
    for (unsigned lane = 0; lane < 4u; ++lane) {
        uint32_t step = image_pins.pin[KM_PIN_STEP][lane];
        uint32_t dir = image_pins.pin[KM_PIN_DIR][lane];
        uint32_t enable_pin = image_pins.pin[KM_PIN_ENABLE][lane];
        uint32_t uart = image_pins.pin[KM_PIN_UART][lane];
        assert(step < SHIM_MAX_PINS && dir < SHIM_MAX_PINS
               && enable_pin < SHIM_MAX_PINS && uart < SHIM_MAX_PINS);
        assert(step != dir && step != enable_pin && step != uart
               && dir != enable_pin && dir != uart && enable_pin != uart);
        shim_attach_virtual_tmc(uart, image_tmc[lane].tmc.address, CONFIG_CLOCK_FREQ);
        assert(km_tmc_feeder_setup(lane, step, dir, enable_pin, uart,
                    image_tmc[lane].uart_pull_up, &image_step[lane],
                    &image_tmc[lane].tmc,
                    image_tmc[lane].tmc.run_current_ma,
                    image_tmc[lane].tmc.hold_current_ma) == KM_OK);
        assert(pin_level[enable_pin] == (image_step[lane].enable_active_high ? 0 : 1));
        assert(!km_tmc_uart_gpio_is_ready(lane));
        assert(km_tmc_feeder_activate(lane) == KM_ERR_UNAVAILABLE);
        assert(km_tmc_feeder_get_driver(lane) == NULL);
    }
    for (unsigned lane = 0; lane < 4u; ++lane)
        dispatch_until_ready(lane);
    for (unsigned lane = 0; lane < 4u; ++lane) {
        km_tmc_uart_status_t status;
        assert(km_tmc_uart_gpio_get_status(lane, &status) == KM_OK);
        assert(status.ready && status.configured && status.fault == KM_FAULT_NONE);
        assert(status.requested_run_ma == image_tmc[lane].tmc.run_current_ma);
        assert(status.requested_hold_ma == image_tmc[lane].tmc.hold_current_ma);
        assert(status.confirmed_run_ma == image_tmc[lane].tmc.run_current_ma);
        assert(status.confirmed_hold_ma == image_tmc[lane].tmc.hold_current_ma);
        assert(km_tmc_feeder_activate(lane) == KM_OK);
        km_driver_t *d = km_tmc_feeder_get_driver(lane);
        assert(d && d->initialized && !d->enabled);
        assert(d->run_current_ma == image_tmc[lane].tmc.run_current_ma);
        assert(d->hold_current_ma == image_tmc[lane].tmc.hold_current_ma);
        assert(enable(d) == KM_OK);
        assert(pin_level[image_pins.pin[KM_PIN_ENABLE][lane]]
               == (image_step[lane].enable_active_high ? 1 : 0));
    }

    /* A single lane UART fault must safe-off only that lane. */
    uint32_t failed_uart = image_pins.pin[KM_PIN_UART][2];
    uint32_t failed_enable = image_pins.pin[KM_PIN_ENABLE][2];
    uint32_t other_enable = image_pins.pin[KM_PIN_ENABLE][1];
    shim_get_virtual_tmc(failed_uart)->stuck_low = true;
    dispatch_until_fault(2);
    assert(pin_level[failed_enable]
           == (image_step[2].enable_active_high ? 0 : 1));
    assert(pin_level[other_enable]
           == (image_step[1].enable_active_high ? 1 : 0));
    assert(km_tmc_uart_gpio_is_ready(1));
}
#endif

static void run_case(void (*fn)(void), const char *name)
{
    pid_t pid = fork();
    assert(pid >= 0);
    if (pid == 0) {
        alarm(15);
        fn();
        _exit(0);
    }
    int status = 0;
    assert(waitpid(pid, &status, 0) == pid);
    if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
        fprintf(stderr, "FAIL %s status=%d\n", name, status);
        exit(1);
    }
    printf("PASS %s\n", name);
}

int main(void)
{
    run_case(test_validation_and_delayed_ready, "validation_and_delayed_ready");
    run_case(test_cross_claims_and_failed_startup, "cross_claims_and_failed_startup");
    run_case(test_current_mismatch, "current_mismatch");
    run_case(test_shared_uart_pin_two_addresses_both_confirm,
             "shared_uart_pin_two_addresses_both_confirm");
    run_case(test_shared_uart_pin_equal_address_refused,
             "shared_uart_pin_equal_address_refused");
    run_case(test_shared_uart_pin_fault_on_one_does_not_fault_other,
             "shared_uart_pin_fault_on_one_does_not_fault_other");
    run_case(test_fault_high_step_restart_and_isolation, "fault_high_step_restart_and_isolation");
    run_case(test_restart_command_and_reconcile, "restart_command_and_reconcile");
    run_case(test_global_shutdown, "global_shutdown");
#ifdef KM_TEST_IMAGE_AUTOCONF
    run_case(test_image_configured_four_lane_startup,
             "image_configured_four_lane_startup");
#endif
    return 0;
}
