// SPDX-License-Identifier: GPL-3.0-or-later
#define _DEFAULT_SOURCE
#define _POSIX_C_SOURCE 200809L

#include "board_startup.h"
#include "clock.h"
#include "config.h"
#include "pin_config.h"
#include "encoder.h"
#include "h723_resources.h"
#include "feeder_config.h"
#include "tmc_config.h"
#include "switch_config.h"
#include "rewind_config.h"
#include "adc_config.h"
#include "switch_gpio.h"
#include "adc.h"
#include "tmc_feeder.h"
#include "pwm_bridge_gpio.h"
#include "stepdir_gpio.h"
#include "tmc_uart_gpio.h"
#include "sensors.h"
#include "controller.h"
#include "board/irq.h"
#include "board/misc.h"
#include "command.h"
#include "sched.h"
#include "stm32/gpio_local.h"
#include "autoconf.h"
#include "mock_driver.h"
#include "boxturtle_boot.c"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <assert.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

/* =========================================================================
 * Controlled Test Harness & Sim State
 *
 * NOTE ON TEST HARNESS PURITY:
 * All portable common C control core code (operation_core, device_profile,
 * resource_plan, buffer_sensor, follower, trajectory, etc.) is the REAL,
 * unaltered product source linked directly from $(PRODUCT_ROOT).
 * The shims below provide simulated hardware peripheral callbacks and
 * saved-configuration factories to test STM32H723 board startup lifecycle.
 * ========================================================================= */

typedef enum {
    LANE_STATUS_UNAVAILABLE = 0,
    LANE_STATUS_READY       = 1,
    LANE_STATUS_FAULTED     = 2,
} sim_lane_status_t;

/* Persistent shutdown hooks registered via constructor macros */
static shutdown_func_t initial_shutdown_hooks[16];
static size_t initial_shutdown_hook_count = 0;

static struct {
    /* Interrupt and shutdown state */
    unsigned long irq_status;
    uint8_t sched_shutdown;
    bool in_shutdown_hook;

    /* Clock state */
    enum km_clock_state clock_state;
    uint32_t now_us;
    uint32_t clock_reads_in_shutdown;
    uint32_t clock_read_count;

    /* Configuration controls */
    km_device_profile_t profile;
    bool profile_valid;
    bool diff_calibration_provided;
    km_diff_adc_config_t calibration;

    /* Factory and plan controls */
    km_pin_config_t pins;
    km_h723_resource_plan_t expected_plan;
    km_step_dir_config_t feeder_configs[KM_MAX_LANES];
    km_tmc_uart_config_t tmc_configs[KM_MAX_LANES];
    km_switch_gpio_config_t switch_config;
    km_encoder_gpio_config_t encoder_config;
    km_pwm_bridge_config_t rewind_limits;
    km_rewind_output_config_t rewind_outputs[KM_MAX_LANES];
    km_adc_pair_config_t adc_pair_config;

    /* Simulated peripheral state */
    enum km_adc_state adc_state;
    bool controller_attached;
    bool controller_shutdown;
    km_controller_sample_fn controller_sample_fn;
    void *controller_sample_context;
    bool stepdir_shutdown;
    bool tmc_uart_shutdown;
    bool pwm_shutdown;

    /* Injection flags for testing rejection/failure */
    int sim_preclaimed_stepdir_slot;
    int sim_preclaimed_uart_slot;
    int sim_preclaimed_pwm_slot;
    int sim_feeder_setup_fail_lane;
    bool sim_switch_setup_fail;
    bool sim_switch_setup_busy;
    bool sim_encoder_config_fail;
    bool sim_encoder_setup_fail;
    bool sim_adc_start_fail;
    km_result_t sim_adc_result;
    uint32_t sim_switch_setup_cost_us;
    uint32_t sim_adc_cost_us;
    uint32_t captured_adc_timeout_us;
    bool sim_pwm_setup_fail;
    bool sim_controller_attach_fail;
    bool sim_tmc_config_fail;
    uint32_t sim_setup_delay_us;
    uint32_t sim_activation_delay_us;
    uint32_t sim_controller_attach_delay_us;
    uint32_t sim_irq_save_delay_us;
    bool sim_irq_save_delay_applied;
    bool sim_shutdown_during_attach;
    int sim_missing_rewind_driver_lane;
    bool captured_uart_pull_up[KM_MAX_LANES];
    uint32_t captured_step[KM_MAX_LANES];

    /* Selector scenario (klipper_mmu/selector.h): local stand-ins for
     * km_core_bind_selector/km_stepdir_gpio_setup+get_driver, since neither
     * has a real body yet (Agent A's core work lands separately). */
    km_servo_t *captured_selector_servo;
    uint32_t selector_stepdir_setup_count;
    bool sim_selector_stepdir_setup_fail;

    /* Selector-over-TMC-UART scenario: km_config_selector_tmc's fake. */
    km_tmc_uart_config_t selector_tmc_config;
    bool sim_selector_tmc_config_fail;

    /* Lane activation status */
    sim_lane_status_t lane_status[KM_MAX_LANES];

    /* Telemetry and call tracking */
    uint32_t switch_setup_count;
    uint32_t switch_shutdown_count;
    uint32_t encoder_setup_count;
    uint32_t encoder_shutdown_count;
    uint32_t encoder_sample_count;
    uint32_t adc_start_count;
    uint32_t adc_stop_count;
    uint32_t adc_shutdown_count;
    uint32_t feeder_setup_count;
    uint32_t feeder_activate_count;
    uint32_t pwm_setup_count;
    uint32_t pwm_shutdown_count;
    uint32_t stepdir_shutdown_count;
    uint32_t tmc_uart_shutdown_count;
    uint32_t controller_attach_count;
    uint32_t sensor_sample_count;
    uint32_t pwm_service_count[KM_MAX_LANES];
    uint32_t controller_shutdown_count;
    uint32_t gpio_ops_count;

    /* Saved rewind callbacks */
    bool (*rewind_ready_cb[KM_MAX_LANES])(void *);
    km_fault_t (*rewind_fault_cb[KM_MAX_LANES])(void *);

    /* Registered shutdown hooks */
    shutdown_func_t shutdown_hooks[16];
    size_t shutdown_hook_count;
} sim;

/* Mock drivers for feeder and rewind */
static mock_driver_t sim_feeder_driver[KM_MAX_LANES];
static mock_driver_t sim_rewind_driver[KM_MAX_LANES];
/* Selector axis mock driver (klipper_mmu/selector.h); stepdir_gpio.c is not
 * linked here, so km_stepdir_gpio_get_driver is faked to publish this. */
static mock_driver_t sim_selector_driver;

/* Real mcu/servo_pwm.c is linked directly (not stubbed) so the servo scenario
 * exercises its actual scheduled software PWM against these board/gpio fakes.
 * Logged in acquisition order; at_us comes from the fake clock/timer below. */
#if CONFIG_KLIPPER_MMU_SERVO
#define MAX_GPIO_EVENTS 64
typedef struct { uint32_t at_us; uint32_t pin; uint32_t val; } gpio_event_t;
static gpio_event_t gpio_events[MAX_GPIO_EVENTS];
static unsigned gpio_event_count;
#endif

/* =========================================================================
 * Shim Implementations
 * ========================================================================= */

irqstatus_t irq_save(void)
{
    irqstatus_t old = sim.irq_status;
    sim.irq_status = 1ul;
    if (sim.sim_irq_save_delay_us > 0 && !sim.sim_irq_save_delay_applied && sim.controller_attached) {
        sim.now_us += sim.sim_irq_save_delay_us;
        sim.sim_irq_save_delay_applied = true;
    }
    return old;
}

void irq_restore(irqstatus_t status)
{
    sim.irq_status = status;
}

uint8_t sched_is_shutdown(void)
{
    return sim.sched_shutdown;
}

void sched_clear_shutdown(void)
{
    sim.sched_shutdown = 0;
}

void register_shutdown(shutdown_func_t f)
{
    if (initial_shutdown_hook_count < 16) {
        initial_shutdown_hooks[initial_shutdown_hook_count++] = f;
    }
    if (sim.shutdown_hook_count < 16) {
        sim.shutdown_hooks[sim.shutdown_hook_count++] = f;
    }
}

static struct timer *armed_timer;
static const km_resource_claim_t *last_pwm_claims;
static size_t last_pwm_count;
static uint32_t reserved_pins[KM_RESOURCE_MAX_CLAIMS];
static uint32_t reserved_count;

uint32_t timer_read_time(void) { return sim.now_us; }
uint32_t timer_from_us(uint32_t us) { return us; }
void sched_wake_task(struct task_wake *wake) { wake->wake = 1; }
uint8_t sched_check_wake(struct task_wake *wake)
{
    if (!wake->wake) return 0;
    wake->wake = 0;
    return 1;
}
/* Klipper's scheduler corrupts its list if a queued timer is added again. */
void sched_add_timer(struct timer *timer) { assert(armed_timer != timer); armed_timer = timer; }
void sched_del_timer(struct timer *timer) { if (armed_timer == timer) armed_timer = NULL; }

#if CONFIG_KLIPPER_MMU_SERVO
/* board/gpio.h fakes for mcu/servo_pwm.c, the one real production driver
 * this suite links directly instead of stubbing at the mcu API boundary. */
static void log_gpio(uint32_t pin, uint32_t val)
{
    if (gpio_event_count < MAX_GPIO_EVENTS)
        gpio_events[gpio_event_count++] = (gpio_event_t){sim.now_us, pin, val};
}
struct gpio_out gpio_out_setup(uint32_t pin, uint32_t val)
{
    log_gpio(pin, val);
    return (struct gpio_out){.pin = pin};
}
struct gpio_out gpio_out_setup_local(uint32_t pin, uint32_t val)
{
    return gpio_out_setup(pin, val);
}
void gpio_out_write(struct gpio_out g, uint32_t val) { log_gpio(g.pin, val); }
void gpio_out_toggle_noirq(struct gpio_out g) { (void)g; }
int gpio_pin_is_local(uint32_t pin) { (void)pin; return 1; }

/* Advances the single fake-armed timer (there is only ever one: the servo's)
 * up to target_us, dispatching its callback at each of its own waketimes,
 * the same way Klipper's real scheduler would. */
static void run_armed_timer_until(uint32_t target_us)
{
    while (armed_timer && (int32_t)(target_us - armed_timer->waketime) >= 0) {
        struct timer *t = armed_timer;
        sim.now_us = t->waketime;
        if (t->func(t) == SF_DONE) armed_timer = NULL;
    }
    if ((int32_t)(target_us - sim.now_us) > 0) sim.now_us = target_us;
}
#endif
int gpio_local_reserve(const uint32_t *pins, uint32_t count)
{
    if (count > KM_RESOURCE_MAX_CLAIMS || (pins == NULL && count != 0u))
        return GPIO_LOCAL_ERR_INVALID;
    reserved_count = count;
    if (count)
        memcpy(reserved_pins, pins, count * sizeof(uint32_t));
    return GPIO_LOCAL_OK;
}

void shutdown(const char *msg)
{
    (void)msg;
    sim.sched_shutdown = 1;
    sim.in_shutdown_hook = true;
    for (size_t i = 0; i < sim.shutdown_hook_count; ++i) {
        if (sim.shutdown_hooks[i]) {
            sim.shutdown_hooks[i]();
        }
    }
    sim.in_shutdown_hook = false;
}

enum km_clock_state km_clock_get_state(void)
{
    return sim.clock_state;
}

km_result_t km_clock_get_time(km_core_timepoint_t *out)
{
    if (!out) return KM_ERR_INVALID_PARAM;
    if (sim.in_shutdown_hook) {
        sim.clock_reads_in_shutdown++;
    }
    sim.clock_read_count++;
    if (sim.clock_state == KM_CLOCK_STATE_UNAVAILABLE) {
        return KM_ERR_UNAVAILABLE;
    }
    if (sim.clock_state != KM_CLOCK_STATE_READY) {
        return KM_ERR_FAULTED;
    }
    out->us = sim.now_us;
    out->ms = sim.now_us / 1000u;
    return KM_OK;
}

km_result_t km_config_profile(km_device_profile_t *out, const km_diff_adc_config_t *cal)
{
    if (!out) return KM_ERR_INVALID_PARAM;
    if (!sim.profile_valid) return KM_ERR_INVALID_PARAM;
    if (sim.profile.has_proportional_buffer && !cal) return KM_ERR_UNAVAILABLE;
    if (!sim.profile.has_proportional_buffer && cal) return KM_ERR_INVALID_PARAM;
    *out = sim.profile;
    if (cal) {
        out->diff_calibration = *cal;
        out->has_diff_calibration = true;
    }
    return KM_OK;
}

km_result_t km_config_pins_view(const km_pin_config_t **out, km_resource_diagnostic_t *d)
{
    if (d) memset(d, 0, sizeof(*d));
    if (!out) return KM_ERR_INVALID_PARAM;
    *out = &sim.pins;
    return KM_OK;
}

km_result_t km_config_pins(km_pin_config_t *out, km_resource_diagnostic_t *d)
{
    if (d) memset(d, 0, sizeof(*d));
    if (!out) return KM_ERR_INVALID_PARAM;
    const km_pin_config_t *view;
    km_result_t result = km_config_pins_view(&view, NULL);
    if (result == KM_OK) *out = *view;
    return result;
}

km_result_t km_config_h723_resources(km_h723_resource_plan_t *out, km_resource_diagnostic_t *d)
{
    if (d) memset(d, 0, sizeof(*d));
    if (!out) return KM_ERR_INVALID_PARAM;
    *out = sim.expected_plan;
    return KM_OK;
}

km_result_t km_config_feeders(km_step_dir_config_t out[KM_MAX_LANES])
{
    if (!out) return KM_ERR_INVALID_PARAM;
    for (size_t i = 0; i < KM_MAX_LANES; ++i) {
        out[i] = sim.feeder_configs[i];
    }
    return KM_OK;
}

km_result_t km_config_tmc(km_tmc_uart_config_t out[KM_MAX_LANES])
{
    if (!out) return KM_ERR_INVALID_PARAM;
    if (sim.sim_tmc_config_fail) return KM_ERR_INVALID_PARAM;
    for (size_t i = 0; i < KM_MAX_LANES; ++i) {
        out[i] = sim.tmc_configs[i];
    }
    return KM_OK;
}

km_result_t km_config_selector_tmc(km_tmc_uart_config_t *out)
{
    if (!out) return KM_ERR_INVALID_PARAM;
    if (sim.sim_selector_tmc_config_fail) return KM_ERR_INVALID_PARAM;
    *out = sim.selector_tmc_config;
    return KM_OK;
}

km_result_t km_config_switches(km_switch_gpio_config_t *out)
{
    if (!out) return KM_ERR_INVALID_PARAM;
    *out = sim.switch_config;
    return KM_OK;
}

km_result_t km_config_encoder(km_encoder_gpio_config_t *out)
{
    if (!out) return KM_ERR_INVALID_PARAM;
    if (sim.sim_encoder_config_fail) return KM_ERR_UNAVAILABLE;
    *out = sim.encoder_config;
    return KM_OK;
}

km_result_t km_config_rewind(km_pwm_bridge_config_t *out)
{
    if (!out) return KM_ERR_INVALID_PARAM;
    *out = sim.rewind_limits;
    return KM_OK;
}

km_result_t km_config_rewind_outputs(km_rewind_output_config_t out[KM_MAX_LANES])
{
    if (!out) return KM_ERR_INVALID_PARAM;
    for (size_t i = 0; i < KM_MAX_LANES; ++i) {
        out[i] = sim.rewind_outputs[i];
    }
    return KM_OK;
}

km_result_t km_config_adc_pair(km_adc_pair_config_t *out)
{
    if (!out) return KM_ERR_INVALID_PARAM;
    *out = sim.adc_pair_config;
    return KM_OK;
}

km_result_t km_switch_gpio_setup(km_operation_core_t *core, const km_switch_gpio_config_t *config)
{
    (void)core; (void)config;
    if (sim.sim_switch_setup_busy) return KM_ERR_BUSY;
    sim.switch_setup_count++;
    sim.gpio_ops_count++;
    if (sim.sim_switch_setup_cost_us > 0u) {
        sim.now_us += sim.sim_switch_setup_cost_us;
    }
    if (sim.sim_switch_setup_fail) return KM_ERR_FAULTED;
    return KM_OK;
}

void km_switch_gpio_shutdown(void)
{
    sim.switch_shutdown_count++;
}

km_result_t km_encoder_gpio_setup(const km_encoder_gpio_config_t *config)
{
    (void)config;
    if (sim.sim_encoder_setup_fail) return KM_ERR_FAULTED;
    sim.encoder_setup_count++;
    sim.gpio_ops_count++;
    return KM_OK;
}

void km_encoder_gpio_shutdown(void)
{
    sim.encoder_shutdown_count++;
}

void km_encoder_gpio_sample(km_operation_core_t *core, void *context)
{
    (void)core; (void)context;
    sim.encoder_sample_count++;
}

int32_t km_encoder_gpio_get_clicks(void)
{
    return 0;
}

/* Diagnostic-only bounce counters (docs/diagnostics.md); this simulation
 * fakes switch_gpio.c entirely, so every switch reports zero. */
void km_switch_gpio_get_bounce(uint16_t bay[KM_MAX_LANES], uint16_t load[KM_MAX_LANES],
                               uint16_t *hub, uint16_t *toolhead)
{
    for (unsigned i = 0; i < KM_MAX_LANES; ++i) bay[i] = load[i] = 0;
    *hub = *toolhead = 0;
}
void km_switch_gpio_get_selector_bounce(uint16_t *selector) { *selector = 0; }

enum km_adc_state km_adc_get_state(void)
{
    return sim.adc_state;
}

km_result_t km_adc_start(uint32_t pin_a, uint32_t pin_b, const km_adc_pair_config_t *config, uint32_t timeout_us)
{
    assert(config != NULL);
    assert(pin_a == sim.pins.pin[KM_PIN_BUFFER_A][0]);
    assert(pin_b == sim.pins.pin[KM_PIN_BUFFER_B][0]);
    assert(timeout_us > 0u && timeout_us < 0x80000000u);
    sim.adc_start_count++;
    sim.captured_adc_timeout_us = timeout_us;
    if (sim.sim_adc_cost_us > 0u) {
        sim.now_us += sim.sim_adc_cost_us;
    }
    if (sim.sim_adc_start_fail) return KM_ERR_FAULTED;
    if (sim.sim_adc_result != KM_OK) {
        if (sim.sim_adc_result == KM_ERR_TIMEOUT) {
            sim.adc_state = KM_ADC_STATE_FAULTED;
        }
        return sim.sim_adc_result;
    }
    sim.adc_state = KM_ADC_STATE_RUNNING;
    return KM_OK;
}

km_result_t km_adc_stop(void)
{
    sim.adc_stop_count++;
    sim.adc_state = KM_ADC_STATE_STOPPED;
    return KM_OK;
}

void km_adc_shutdown(void)
{
    sim.adc_shutdown_count++;
    sim.adc_state = KM_ADC_STATE_SHUTDOWN;
}

km_result_t km_stepdir_gpio_get_slot_pins(uint32_t slot, uint32_t *s, uint32_t *d, uint32_t *e)
{
    if ((int)slot == sim.sim_preclaimed_stepdir_slot) {
        if (s) { *s = 1; }
        if (d) { *d = 2; }
        if (e) { *e = 3; }
        return KM_OK;
    }
    return KM_ERR_UNAVAILABLE;
}

bool km_stepdir_gpio_is_slot_active(uint32_t slot)
{
    return (int)slot == sim.sim_preclaimed_stepdir_slot;
}

bool km_stepdir_gpio_is_shutdown(void)
{
    return sim.stepdir_shutdown;
}

void km_stepdir_gpio_shutdown(void)
{
    sim.stepdir_shutdown_count++;
    sim.stepdir_shutdown = true;
}

/* Selector axis only: the gear still goes through km_tmc_feeder_setup below,
 * on slot 0. This binding (stepdir_gpio.c is not linked here) always uses
 * slot 1, matching mcu/board_startup.c's setup_selector(). */
km_result_t km_stepdir_gpio_setup(uint32_t slot, uint32_t step_pin, uint32_t dir_pin,
                                  uint32_t enable_pin, const km_step_dir_config_t *config,
                                  bool (*ready)(void *context), km_fault_t (*fault)(void *context),
                                  void *context)
{
    (void)step_pin; (void)dir_pin; (void)enable_pin; (void)config;
    (void)ready; (void)fault; (void)context;
    sim.selector_stepdir_setup_count++;
    if (slot != 1u || sim.sim_selector_stepdir_setup_fail) return KM_ERR_FAULTED;
    return KM_OK;
}

km_driver_t *km_stepdir_gpio_get_driver(uint32_t slot)
{
    return slot == 1u ? &sim_selector_driver.base : NULL;
}

km_result_t km_tmc_uart_gpio_get_pin(uint32_t slot, uint32_t *out_pin)
{
    if ((int)slot == sim.sim_preclaimed_uart_slot) {
        if (out_pin) *out_pin = 4;
        return KM_OK;
    }
    return KM_ERR_UNAVAILABLE;
}

bool km_tmc_uart_gpio_is_slot_active(uint32_t slot)
{
    return (int)slot == sim.sim_preclaimed_uart_slot;
}

bool km_tmc_uart_gpio_is_shutdown(void)
{
    return sim.tmc_uart_shutdown;
}

/* Diagnostic-only gstat/drv_status read (docs/diagnostics.md); this
 * simulation fakes tmc_uart_gpio.c entirely, so no slot is ever ready. */
km_result_t km_tmc_uart_gpio_get_status(uint32_t slot, km_tmc_uart_status_t *out_status)
{
    (void)slot;
    if (!out_status) return KM_ERR_INVALID_PARAM;
    *out_status = (km_tmc_uart_status_t){0};
    return KM_ERR_UNAVAILABLE;
}

void km_tmc_uart_gpio_shutdown(void)
{
    assert(sim.stepdir_shutdown);
    sim.tmc_uart_shutdown = true;
    sim.tmc_uart_shutdown_count++;
}

km_result_t km_tmc_feeder_setup(uint32_t slot, uint32_t step_pin, uint32_t dir_pin,
                                uint32_t enable_pin, uint32_t uart_pin,
                                bool uart_pull_up,
                                const km_step_dir_config_t *step_config,
                                const km_tmc2209_config_t *tmc_config,
                                uint16_t expected_run_ma, uint16_t expected_hold_ma)
{
    (void)dir_pin; (void)enable_pin; (void)uart_pin;
    (void)step_config; (void)tmc_config; (void)expected_run_ma; (void)expected_hold_ma;
    if (slot < KM_MAX_LANES) {
        sim.captured_uart_pull_up[slot] = uart_pull_up;
        sim.captured_step[slot] = step_pin;
    }
    sim.feeder_setup_count++;
    sim.gpio_ops_count++;
    if (sim.sim_setup_delay_us > 0) {
        sim.now_us += sim.sim_setup_delay_us;
    }
    if ((int)slot == sim.sim_feeder_setup_fail_lane) {
        return KM_ERR_FAULTED;
    }
    return KM_OK;
}

km_result_t km_tmc_feeder_activate(uint32_t slot)
{
    sim.feeder_activate_count++;
    if (sim.sim_activation_delay_us > 0) {
        sim.now_us += sim.sim_activation_delay_us;
    }
    if (slot >= KM_MAX_LANES) return KM_ERR_INVALID_PARAM;
    if (sim.lane_status[slot] == LANE_STATUS_READY) return KM_OK;
    if (sim.lane_status[slot] == LANE_STATUS_FAULTED) return KM_ERR_FAULTED;
    return KM_ERR_UNAVAILABLE;
}

km_driver_t *km_tmc_feeder_get_driver(uint32_t slot)
{
    if (slot >= KM_MAX_LANES) return NULL;
    return &sim_feeder_driver[slot].base;
}

km_result_t km_tmc_feeder_restart(uint32_t slot)
{
    /* Linked by the TMC build's restart command; not exercised here. */
    (void)slot;
    return KM_ERR_NOT_SUPPORTED;
}

km_result_t km_pwm_bridge_gpio_get_slot_pins(uint32_t slot, uint32_t *f, uint32_t *r, uint32_t *e)
{
    if ((int)slot == sim.sim_preclaimed_pwm_slot) {
        if (f) { *f = 5; }
        if (r) { *r = 6; }
        if (e) { *e = 7; }
        return KM_OK;
    }
    return KM_ERR_UNAVAILABLE;
}

bool km_pwm_bridge_gpio_is_slot_active(uint32_t slot)
{
    return (int)slot == sim.sim_preclaimed_pwm_slot;
}

bool km_pwm_bridge_gpio_is_shutdown(void)
{
    return sim.pwm_shutdown;
}

km_result_t km_pwm_bridge_gpio_setup(uint32_t slot,
                                     const km_resource_claim_t *complete_claims,
                                     size_t claim_count,
                                     bool (*ready)(void *context),
                                     km_fault_t (*fault)(void *context),
                                     void *context)
{
    (void)context;
    last_pwm_claims = complete_claims;
    last_pwm_count = claim_count;
    if (!ready) return KM_ERR_INVALID_PARAM;
    sim.pwm_setup_count++;
    sim.gpio_ops_count++;
    if (slot < KM_MAX_LANES) {
        sim.rewind_ready_cb[slot] = ready;
        sim.rewind_fault_cb[slot] = fault;
    }
    if (sim.sim_pwm_setup_fail) return KM_ERR_FAULTED;
    return KM_OK;
}

km_driver_t *km_pwm_bridge_gpio_get_driver(uint32_t slot)
{
    if (slot >= KM_MAX_LANES) return NULL;
    if ((int)slot == sim.sim_missing_rewind_driver_lane) return NULL;
    return &sim_rewind_driver[slot].base;
}

void km_pwm_bridge_gpio_service(uint32_t slot)
{
    if (slot < KM_MAX_LANES) ++sim.pwm_service_count[slot];
}

void km_pwm_bridge_gpio_force_off(uint32_t slot)
{
    (void)slot;
}

void km_pwm_bridge_gpio_shutdown(void)
{
    sim.pwm_shutdown_count++;
    sim.pwm_shutdown = true;
}

bool km_controller_is_attached(void)
{
    return sim.controller_attached;
}

bool km_controller_is_shutdown(void)
{
    return sim.controller_shutdown;
}

km_result_t km_controller_attach(km_operation_core_t *core,
                                 km_controller_sample_fn sample_fn,
                                 void *sample_context)
{
    (void)core;
    sim.controller_attach_count++;
    if (sim.sim_controller_attach_delay_us > 0) {
        sim.now_us += sim.sim_controller_attach_delay_us;
    }
    if (sim.sim_shutdown_during_attach) {
        shutdown("injected shutdown during controller attach");
    }
    if (sim.sim_controller_attach_fail) return KM_ERR_FAULTED;
    if (sim.controller_attached) return KM_ERR_BUSY;
    sim.controller_attached = true;
    sim.controller_sample_fn = sample_fn;
    sim.controller_sample_context = sample_context;
    return KM_OK;
}

void km_controller_shutdown(void)
{
    sim.controller_shutdown_count++;
    sim.controller_shutdown = true;
}

void km_sensors_sample(km_operation_core_t *core, void *context)
{
    (void)core; (void)context;
    ++sim.sensor_sample_count;
}

/* =========================================================================
 * Fixture Setup Helpers
 * ========================================================================= */

static void reset_sim(void)
{
    memset(&sim, 0, sizeof(sim));
    sim.clock_state = KM_CLOCK_STATE_READY;
    sim.now_us = 1000u;
    sim.sim_preclaimed_stepdir_slot = -1;
    sim.sim_preclaimed_uart_slot = -1;
    sim.sim_preclaimed_pwm_slot = -1;
    sim.sim_feeder_setup_fail_lane = -1;
    sim.sim_missing_rewind_driver_lane = -1;

    /* Preserve constructor-registered shutdown hooks */
    sim.shutdown_hook_count = initial_shutdown_hook_count;
    for (size_t i = 0; i < initial_shutdown_hook_count; ++i) {
        sim.shutdown_hooks[i] = initial_shutdown_hooks[i];
    }

    /* Base valid profile: 2 lanes, switched buffer, passive rewind */
    km_profile_init_nightowl(&sim.profile);
    sim.profile_valid = true;
    sim.profile.lane_count = 2u;

    /* Default pin setup */
    memset(&sim.pins, 0, sizeof(sim.pins));
    for (size_t role = 0; role < KM_PIN_ROLE_COUNT; ++role) {
        for (size_t lane = 0; lane < KM_MAX_LANES; ++lane) {
            sim.pins.pin[role][lane] = KM_PIN_UNUSED;
        }
    }
    sim.pins.pin[KM_PIN_STEP][0] = 10;
    sim.pins.pin[KM_PIN_DIR][0] = 11;
    sim.pins.pin[KM_PIN_ENABLE][0] = 12;
    sim.pins.pin[KM_PIN_UART][0] = 13;
    sim.pins.pin[KM_PIN_STEP][1] = 20;
    sim.pins.pin[KM_PIN_DIR][1] = 21;
    sim.pins.pin[KM_PIN_ENABLE][1] = 22;
    sim.pins.pin[KM_PIN_UART][1] = 23;

    /* Expected claims in expected plan */
    sim.expected_plan.count = 8;
    sim.expected_plan.claims[0] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=10, .owner=KM_PIN_STEP};
    sim.expected_plan.claims[1] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=11, .owner=KM_PIN_DIR};
    sim.expected_plan.claims[2] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=12, .owner=KM_PIN_ENABLE};
    sim.expected_plan.claims[3] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=13, .owner=KM_PIN_UART};
    sim.expected_plan.claims[4] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=20, .owner=KM_PIN_STEP};
    sim.expected_plan.claims[5] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=21, .owner=KM_PIN_DIR};
    sim.expected_plan.claims[6] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=22, .owner=KM_PIN_ENABLE};
    sim.expected_plan.claims[7] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=23, .owner=KM_PIN_UART};

    /* TMC configs matching exact targets */
    for (size_t i = 0; i < KM_MAX_LANES; ++i) {
        sim.tmc_configs[i].tmc.run_current_ma = sim.profile.default_run_current_ma;
        sim.tmc_configs[i].tmc.hold_current_ma = sim.profile.default_hold_current_ma;
        sim.tmc_configs[i].tmc.maximum_current_ma = 1500u;
        sim.tmc_configs[i].tmc.sense_resistor_milliohms = 110u;
        sim.tmc_configs[i].uart_pull_up = true;
    }

    /* Drivers clean and initialized */
    memset(sim_feeder_driver, 0, sizeof(sim_feeder_driver));
    memset(sim_rewind_driver, 0, sizeof(sim_rewind_driver));
    for (size_t i = 0; i < KM_MAX_LANES; ++i) {
        mock_driver_init(&sim_feeder_driver[i], 2, 10, false);
        mock_driver_init(&sim_rewind_driver[i], 2, 10, false);
    }
    memset(&sim_selector_driver, 0, sizeof(sim_selector_driver));
    mock_driver_init(&sim_selector_driver, 2, 10, false);
    armed_timer = NULL;
#if CONFIG_KLIPPER_MMU_SERVO
    gpio_event_count = 0;
#endif
}

static size_t populate_claims(km_resource_claim_t *dest)
{
    memcpy(dest, sim.expected_plan.claims, sim.expected_plan.count * sizeof(km_resource_claim_t));
    return sim.expected_plan.count;
}

/* =========================================================================
 * Test 1: No-I/O Validation Rejection
 * ========================================================================= */
static void test_validation_no_io_rejection(void)
{
    reset_sim();
    km_resource_claim_t claims[KM_RESOURCE_MAX_CLAIMS];
    size_t count = populate_claims(claims);

    /* 1. Context: IRQ disabled */
    sim.irq_status = 1ul;
    assert(km_board_startup_begin(claims, count, 100000u, NULL) == KM_ERR_INVALID_PARAM);
    assert(sim.gpio_ops_count == 0);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_UNINITIALIZED);
    sim.irq_status = 0ul;

    /* 2. Arguments: NULL claims, count 0, count > MAX */
    assert(km_board_startup_begin(NULL, count, 100000u, NULL) == KM_ERR_INVALID_PARAM);
    assert(km_board_startup_begin(claims, 0, 100000u, NULL) == KM_ERR_INVALID_PARAM);
    assert(km_board_startup_begin(claims, KM_RESOURCE_MAX_CLAIMS + 1, 100000u, NULL) == KM_ERR_INVALID_PARAM);
#if !defined(CONFIG_WANT_GPIO_LOCAL) || CONFIG_WANT_GPIO_LOCAL == 0
    assert(km_board_startup_begin(NULL, 0u, 100000u, NULL) == KM_ERR_INVALID_PARAM);
#endif
    assert(sim.gpio_ops_count == 0);

    /* 3. Timeout: 0 or >= 2^31 */
    assert(km_board_startup_begin(claims, count, 0u, NULL) == KM_ERR_INVALID_PARAM);
    assert(km_board_startup_begin(claims, count, 0x80000000u, NULL) == KM_ERR_INVALID_PARAM);
    assert(sim.gpio_ops_count == 0);

    /* 4. Clock state: UNAVAILABLE or FAULTED */
    sim.clock_state = KM_CLOCK_STATE_UNAVAILABLE;
    assert(km_board_startup_begin(claims, count, 100000u, NULL) == KM_ERR_UNAVAILABLE);
    sim.clock_state = KM_CLOCK_STATE_FAULTED;
    assert(km_board_startup_begin(claims, count, 100000u, NULL) == KM_ERR_FAULTED);
    sim.clock_state = KM_CLOCK_STATE_READY;
    assert(sim.gpio_ops_count == 0);

    /* 5. Preclaimed component slots */
    sim.sim_preclaimed_stepdir_slot = 0;
    assert(km_board_startup_begin(claims, count, 100000u, NULL) == KM_ERR_BUSY);
    sim.sim_preclaimed_stepdir_slot = -1;

    sim.sim_preclaimed_uart_slot = 1;
    assert(km_board_startup_begin(claims, count, 100000u, NULL) == KM_ERR_BUSY);
    sim.sim_preclaimed_uart_slot = -1;
    assert(sim.gpio_ops_count == 0);

    /* 6. Controller already attached */
    sim.controller_attached = true;
    assert(km_board_startup_begin(claims, count, 100000u, NULL) == KM_ERR_BUSY);
    sim.controller_attached = false;
    assert(sim.gpio_ops_count == 0);

    /* 7. Unsupported capabilities */
    sim.profile.has_current_sensing = true;
    assert(km_board_startup_begin(claims, count, 100000u, NULL) == KM_ERR_NOT_SUPPORTED);
    sim.profile.has_current_sensing = false;
    /* has_encoder is no longer categorically unsupported here: it has a real
     * MCU driver (encoder.h), and test_encoder_startup below covers its own
     * startup path end to end. A selector profile (klipper_mmu/selector.h)
     * is likewise no longer categorically unsupported: test_selector_linear_
     * servo_startup below covers its real startup path end to end. A build
     * without the selector's own capability (CONFIG_KLIPPER_MMU_SELECTOR_
     * STEPPER/_SERVO, both on for this whole binary) still rejects an
     * inconsistent profile in mcu/board_startup.c, but that is a compile-time
     * gate this shared translation unit cannot toggle per sub-test. */
    assert(sim.gpio_ops_count == 0);

    /* 8. Exact current target mismatch */
    sim.tmc_configs[0].tmc.run_current_ma = 900u; // mismatch profile
    assert(km_board_startup_begin(claims, count, 100000u, NULL) == KM_ERR_INVALID_PARAM);
    sim.tmc_configs[0].tmc.run_current_ma = sim.profile.default_run_current_ma;
    assert(sim.gpio_ops_count == 0);

    /* 9. Missing expected claim */
    assert(km_board_startup_begin(claims, count - 1, 100000u, NULL) == KM_ERR_CONFLICT);
    assert(sim.gpio_ops_count == 0);

    /* 10. Conflicting claim in complete claims */
    claims[count] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=10, .owner=99}; // collision with pin 10
    assert(km_board_startup_begin(claims, count + 1, 100000u, NULL) == KM_ERR_CONFLICT);
    assert(sim.gpio_ops_count == 0);

    /* Assert that state is STILL UNINITIALIZED, no latching occurred, core is NULL */
    assert(sim.tmc_uart_shutdown_count == 0);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_UNINITIALIZED);
    assert(km_board_startup_get_result() == KM_ERR_UNAVAILABLE);
    assert(km_board_startup_get_core() == NULL);
}

static uint16_t preset_pin(char port, unsigned bit)
{
    return (uint16_t)(((unsigned)(port - 'A')) * 16u + bit);
}

/* Image admission: km_boxturtle_boot_task calls begin(NULL, 0). Pin ids are the
 * committed AFC Lite defaults (STEP PD4,PC12,PE2,PD15, toolhead PA3). */
static void test_boxturtle_from_uninitialized(void)
{
    _Static_assert(CONFIG_WANT_GPIO_LOCAL, "image GPIO admission");
    _Static_assert(CONFIG_WANT_TMCUART_LOCAL && CONFIG_KLIPPER_MMU_TMC_UART_SHARED,
                   "image TMC UART admission");
    _Static_assert(CONFIG_WANT_HARD_PWM_LOCAL_PAIR, "image PWM admission");
    reset_sim();
    km_profile_init_boxturtle(&sim.profile);
    sim.profile_valid = true;
    assert(strcmp(sim.profile.name, "BoxTurtle-Baseline") == 0);
    assert(sim.profile.lane_count == 4u);
    assert(sim.profile.has_powered_rewind);
    assert(km_profile_validate(&sim.profile) == KM_OK);
    const char step_port[] = {'D', 'C', 'E', 'D'};
    const unsigned step_bit[] = {4u, 12u, 2u, 15u};
    const char dir_port[] = {'D', 'C', 'E', 'D'};
    const unsigned dir_bit[] = {3u, 11u, 3u, 14u};
    const char en_port[] = {'D', 'D', 'E', 'C'};
    const unsigned en_bit[] = {6u, 1u, 0u, 7u};
    const char uart_port[] = {'D', 'D', 'E', 'C'};
    const unsigned uart_bit[] = {5u, 0u, 1u, 6u};
    const char fwd_port[] = {'A', 'A', 'B', 'D'};
    const unsigned fwd_bit[] = {1u, 7u, 15u, 13u};
    const char rev_port[] = {'A', 'A', 'B', 'D'};
    const unsigned rev_bit[] = {0u, 6u, 14u, 12u};
    const char ren_port[] = {'A', 'A', 'B', 'D'};
    const unsigned ren_bit[] = {2u, 5u, 13u, 11u};
    sim.expected_plan.count = 0;
    for (size_t lane = 0; lane < 4u; ++lane) {
        sim.tmc_configs[lane].tmc.run_current_ma = sim.profile.default_run_current_ma;
        sim.tmc_configs[lane].tmc.hold_current_ma = sim.profile.default_hold_current_ma;
        sim.lane_status[lane] = LANE_STATUS_UNAVAILABLE;
        sim.pins.pin[KM_PIN_STEP][lane] = preset_pin(step_port[lane], step_bit[lane]);
        sim.pins.pin[KM_PIN_DIR][lane] = preset_pin(dir_port[lane], dir_bit[lane]);
        sim.pins.pin[KM_PIN_ENABLE][lane] = preset_pin(en_port[lane], en_bit[lane]);
        sim.pins.pin[KM_PIN_UART][lane] = preset_pin(uart_port[lane], uart_bit[lane]);
        sim.pins.pin[KM_PIN_REWIND_FORWARD][lane] = preset_pin(fwd_port[lane], fwd_bit[lane]);
        sim.pins.pin[KM_PIN_REWIND_REVERSE][lane] = preset_pin(rev_port[lane], rev_bit[lane]);
        sim.pins.pin[KM_PIN_REWIND_ENABLE][lane] = preset_pin(ren_port[lane], ren_bit[lane]);
        const uint16_t ids[] = {
            sim.pins.pin[KM_PIN_STEP][lane], sim.pins.pin[KM_PIN_DIR][lane],
            sim.pins.pin[KM_PIN_ENABLE][lane], sim.pins.pin[KM_PIN_UART][lane],
            sim.pins.pin[KM_PIN_REWIND_FORWARD][lane],
            sim.pins.pin[KM_PIN_REWIND_REVERSE][lane],
            sim.pins.pin[KM_PIN_REWIND_ENABLE][lane]
        };
        const uint8_t roles[] = {
            KM_PIN_STEP, KM_PIN_DIR, KM_PIN_ENABLE, KM_PIN_UART,
            KM_PIN_REWIND_FORWARD, KM_PIN_REWIND_REVERSE, KM_PIN_REWIND_ENABLE
        };
        for (size_t role = 0; role < sizeof(roles); ++role) {
            sim.expected_plan.claims[sim.expected_plan.count++] = (km_resource_claim_t){
                .kind = KM_RESOURCE_PIN, .instance = ids[role], .owner = roles[role]};
        }
    }
    sim.pins.pin[KM_PIN_TOOLHEAD][0] = preset_pin('A', 3);
    sim.expected_plan.claims[sim.expected_plan.count++] = (km_resource_claim_t){
        .kind = KM_RESOURCE_PIN, .instance = preset_pin('A', 3), .owner = KM_PIN_TOOLHEAD};

    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_UNINITIALIZED);
    assert(sim.irq_status == 0ul);
    km_boxturtle_boot_init();
    km_boxturtle_boot_task();
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_WAITING);
    assert(sim.pwm_setup_count == 4u);
    assert(last_pwm_claims == NULL && last_pwm_count == 0u);
    assert(sim.captured_step[0] == preset_pin('D', 4));
    assert(sim.captured_step[1] == preset_pin('C', 12));
    assert(sim.captured_step[2] == preset_pin('E', 2));
    assert(sim.captured_step[3] == preset_pin('D', 15));
    assert(preset_pin('D', 4) != preset_pin('A', 1));
    bool saw_toolhead = false, saw_forward = false;
    for (uint32_t i = 0; i < reserved_count; ++i) {
        if (reserved_pins[i] == preset_pin('A', 3)) saw_toolhead = true;
        if (reserved_pins[i] == preset_pin('A', 1)) saw_forward = true;
        for (uint32_t j = i + 1u; j < reserved_count; ++j)
            assert(reserved_pins[i] != reserved_pins[j]);
    }
    assert(saw_toolhead && saw_forward);

    for (size_t lane = 0; lane < 4u; ++lane)
        sim.lane_status[lane] = LANE_STATUS_READY;
    assert(armed_timer && armed_timer->func);
    assert(armed_timer->func(armed_timer) == SF_RESCHEDULE);
    km_boxturtle_boot_task();
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_READY);
    assert(km_board_startup_get_result() == KM_OK);
    km_operation_core_t *core = km_board_startup_get_core();
    assert(core != NULL);
    assert(strcmp(core->profile.name, "BoxTurtle-Baseline") == 0);
    for (size_t lane = 0; lane < 4u; ++lane) {
        assert(core->feeder_drivers[lane] == &sim_feeder_driver[lane].base);
        assert(core->rewinder_drivers[lane] == &sim_rewind_driver[lane].base);
        assert(!sim_feeder_driver[lane].base.enabled);
        assert(sim_feeder_driver[lane].base.pwm_duty_permille == 0);
        assert(sim_feeder_driver[lane].step_count == 0);
        assert(!sim_rewind_driver[lane].base.enabled);
        assert(sim_rewind_driver[lane].base.pwm_duty_permille == 0);
        assert(sim_rewind_driver[lane].step_count == 0);
    }
}

static void test_switched_passive_progression(void)
{
    reset_sim();
    km_resource_claim_t claims[KM_RESOURCE_MAX_CLAIMS];
    size_t count = populate_claims(claims);

    /* Both lanes initially unavailable */
    sim.lane_status[0] = LANE_STATUS_UNAVAILABLE;
    sim.lane_status[1] = LANE_STATUS_UNAVAILABLE;

    km_result_t res = km_board_startup_begin(claims, count, 50000u, NULL);
    assert(res == KM_OK);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_WAITING);
    assert(km_board_startup_get_result() == KM_ERR_UNAVAILABLE);
    assert(km_board_startup_get_core() == NULL);
    assert(sim.switch_setup_count == 1);
    assert(sim.adc_start_count == 0);
    assert(sim.pwm_setup_count == 0);
    assert(sim.feeder_setup_count == 2);

    /* Advancing service while still unavailable returns KM_ERR_UNAVAILABLE */
    sim.now_us += 1000u;
    assert(km_board_startup_service() == KM_ERR_UNAVAILABLE);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_WAITING);
    assert(km_board_startup_get_core() == NULL);

    /* Confirm lane 0 */
    sim.lane_status[0] = LANE_STATUS_READY;
    sim.now_us += 1000u;
    assert(km_board_startup_service() == KM_ERR_UNAVAILABLE);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_WAITING);

    /* Confirm lane 1 */
    sim.lane_status[1] = LANE_STATUS_READY;
    sim.now_us += 1000u;
    km_result_t sres = km_board_startup_service();
    assert(sres == KM_OK);

    /* Now startup is READY */
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_READY);
    assert(km_board_startup_get_result() == KM_OK);
    km_operation_core_t *core = km_board_startup_get_core();
    assert(core != NULL);
    assert(core->feeder_drivers[0] == &sim_feeder_driver[0].base);
    assert(core->feeder_drivers[1] == &sim_feeder_driver[1].base);
    assert(core->rewinder_drivers[0] == NULL);
    assert(sim.controller_attached == true);

    /* Further service call in READY state is a no-op returning KM_OK */
    assert(km_board_startup_service() == KM_OK);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_READY);
    assert(sim.tmc_uart_shutdown_count == 0);
}

/* =========================================================================
 * Test: Encoder Capability Startup (encoder.h)
 * Each scenario gets its own test function: run_test() forks a fresh child
 * per function, but a single process's board_startup.c module state (own
 * static startup_state/encoder_owned/etc.) is not reset by reset_sim() alone,
 * so a second km_board_startup_begin() call in the same process would find
 * the module already attached from the first. See the many other one-
 * begin()-per-function tests above for the same reason.
 * ========================================================================= */
static void test_encoder_disabled_by_default(void)
{
    /* Disabled (the default): no encoder config/setup call, and the composed
     * sample callback does not touch the encoder driver. */
    reset_sim();
    km_resource_claim_t claims[KM_RESOURCE_MAX_CLAIMS];
    size_t count = populate_claims(claims);
    sim.lane_status[0] = LANE_STATUS_READY;
    sim.lane_status[1] = LANE_STATUS_READY;
    assert(km_board_startup_begin(claims, count, 50000u, NULL) == KM_OK);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_READY);
    assert(sim.encoder_setup_count == 0);
    km_operation_core_t *core = km_board_startup_get_core();
    sim.controller_sample_fn(core, sim.controller_sample_context);
    assert(sim.sensor_sample_count == 1);
    assert(sim.encoder_sample_count == 0);
}

static void test_encoder_startup_success(void)
{
    /* Enabled with a valid pin/pull config: setup runs once during startup,
     * and every control-task sample thereafter also samples the encoder. */
    reset_sim();
    km_resource_claim_t claims[KM_RESOURCE_MAX_CLAIMS];
    size_t count = populate_claims(claims);
    sim.profile.has_encoder = true;
    sim.profile.encoder_detection_length_steps = 1000; /* km_profile_validate requires > 0. */
    sim.encoder_config = (km_encoder_gpio_config_t){.pin_a = 30, .pin_b = 31, .pull = 1};
    sim.lane_status[0] = LANE_STATUS_READY;
    sim.lane_status[1] = LANE_STATUS_READY;
    assert(km_board_startup_begin(claims, count, 50000u, NULL) == KM_OK);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_READY);
    assert(sim.encoder_setup_count == 1);
    km_operation_core_t *core = km_board_startup_get_core();
    sim.controller_sample_fn(core, sim.controller_sample_context);
    assert(sim.sensor_sample_count == 1);
    assert(sim.encoder_sample_count == 1);
    sim.controller_sample_fn(core, sim.controller_sample_context);
    assert(sim.encoder_sample_count == 2);
}

static void test_encoder_config_unavailable_rejected(void)
{
    /* An unavailable encoder config (unset pins/pull in the real factory;
     * simulated here directly) is propagated before any physical mutation. */
    reset_sim();
    km_resource_claim_t claims[KM_RESOURCE_MAX_CLAIMS];
    size_t count = populate_claims(claims);
    sim.profile.has_encoder = true;
    sim.profile.encoder_detection_length_steps = 1000; /* km_profile_validate requires > 0. */
    sim.sim_encoder_config_fail = true;
    assert(km_board_startup_begin(claims, count, 50000u, NULL) == KM_ERR_UNAVAILABLE);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_UNINITIALIZED);
    assert(sim.gpio_ops_count == 0);
    assert(sim.encoder_setup_count == 0);
}

static void test_encoder_setup_failure_cleans_up(void)
{
    /* A GPIO setup failure after switches have already been touched faults
     * and cleans up, like the analogous switch-setup regression above. */
    reset_sim();
    km_resource_claim_t claims[KM_RESOURCE_MAX_CLAIMS];
    size_t count = populate_claims(claims);
    sim.profile.has_encoder = true;
    sim.profile.encoder_detection_length_steps = 1000; /* km_profile_validate requires > 0. */
    sim.encoder_config = (km_encoder_gpio_config_t){.pin_a = 30, .pin_b = 31, .pull = 1};
    sim.sim_encoder_setup_fail = true;
    assert(km_board_startup_begin(claims, count, 50000u, NULL) == KM_ERR_FAULTED);
    assert(sim.switch_setup_count == 1);
    assert(sim.switch_shutdown_count > 0);
    assert(sim.encoder_shutdown_count > 0);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_FAULTED);
    assert(km_board_startup_get_core() == NULL);
}

static void test_five_lane_virtual_hub_startup(void)
{
    reset_sim();
    km_profile_init_emu(&sim.profile);
    assert(sim.profile.lane_count == 5u && sim.profile.load_is_virtual_hub);
    for (size_t lane = 2; lane < 5u; ++lane) {
        const uint16_t first = (uint16_t)(30u + 4u * lane);
        for (unsigned role = 0; role < 4u; ++role) {
            enum km_pin_role pin_role = (enum km_pin_role)(KM_PIN_STEP + role);
            uint16_t pin = (uint16_t)(first + role);
            sim.pins.pin[pin_role][lane] = pin;
            sim.expected_plan.claims[sim.expected_plan.count++] =
                (km_resource_claim_t){.kind = KM_RESOURCE_PIN, .instance = pin,
                                      .owner = pin_role};
        }
    }
    km_resource_claim_t claims[KM_RESOURCE_MAX_CLAIMS];
    size_t count = populate_claims(claims);
    for (size_t lane = 0; lane < 4u; ++lane)
        sim.lane_status[lane] = LANE_STATUS_READY;
    assert(km_board_startup_begin(claims, count, 50000u, NULL) == KM_OK);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_WAITING);
    assert(sim.feeder_setup_count == 5u && sim.pwm_setup_count == 0u);
    assert(reserved_count == count);
    assert(sim.feeder_activate_count == 5u);
    assert(km_board_startup_get_core() == NULL);
    sim.lane_status[4] = LANE_STATUS_READY;
    sim.now_us += 1000u;
    assert(km_board_startup_service() == KM_OK);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_READY);
    km_operation_core_t *core = km_board_startup_get_core();
    assert(core && core->profile.lane_count == 5u);
    assert(core->profile.load_is_virtual_hub);
    for (size_t lane = 0; lane < 5u; ++lane) {
        assert(core->feeder_drivers[lane] == &sim_feeder_driver[lane].base);
        assert(core->rewinder_drivers[lane] == NULL);
        assert(!sim_feeder_driver[lane].base.enabled);
    }
    km_board_startup_shutdown();
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_FAULTED);
    assert(sim.stepdir_shutdown_count == 1u);
    assert(sim.tmc_uart_shutdown_count == 1u);
    assert(sim.pwm_shutdown_count == 0u);
}

/* =========================================================================
 * Test 3: Analog Differential Buffer / Powered Rewind Progression
 * ========================================================================= */
static void test_analog_powered_progression(void)
{
    reset_sim();

    /* Enable analog buffer and powered rewind with valid proportional gain */
    sim.profile.has_proportional_buffer = true;
    sim.profile.has_powered_rewind = true;
    sim.profile.proportional_gain_milli = 8000;
    sim.profile.buffer_deadband_permille = 10;
    sim.profile.rewind_pwm_duty_permille = 600;

    km_diff_adc_config_t cal = {
        .channel_map = KM_DIFF_CH_A_LOW_B_HIGH,
        .vref_mv = 3300,
        .adc_max_counts = 4095,
        .path_tolerance_counts = 32,
        .point_count = 3,
        .points = {
            {0, 2048, 0},
            {2048, 2048, 500},
            {2048, 4095, 1000}
        }
    };
    sim.profile.diff_calibration = cal;
    sim.profile.has_diff_calibration = true;
    assert(km_profile_validate(&sim.profile) == KM_OK);

    /* Add rewind pins and claims */
    sim.pins.pin[KM_PIN_REWIND_FORWARD][0] = 30;
    sim.pins.pin[KM_PIN_REWIND_REVERSE][0] = 31;
    sim.pins.pin[KM_PIN_REWIND_ENABLE][0] = 32;
    sim.pins.pin[KM_PIN_REWIND_FORWARD][1] = 40;
    sim.pins.pin[KM_PIN_REWIND_REVERSE][1] = 41;
    sim.pins.pin[KM_PIN_REWIND_ENABLE][1] = 42;

    sim.expected_plan.claims[sim.expected_plan.count++] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=30, .owner=KM_PIN_REWIND_FORWARD};
    sim.expected_plan.claims[sim.expected_plan.count++] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=31, .owner=KM_PIN_REWIND_REVERSE};
    sim.expected_plan.claims[sim.expected_plan.count++] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=32, .owner=KM_PIN_REWIND_ENABLE};
    sim.expected_plan.claims[sim.expected_plan.count++] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=40, .owner=KM_PIN_REWIND_FORWARD};
    sim.expected_plan.claims[sim.expected_plan.count++] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=41, .owner=KM_PIN_REWIND_REVERSE};
    sim.expected_plan.claims[sim.expected_plan.count++] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=42, .owner=KM_PIN_REWIND_ENABLE};

    /* Add ADC claims */
    sim.expected_plan.claims[sim.expected_plan.count++] = (km_resource_claim_t){.kind=KM_RESOURCE_ADC_UNIT, .instance=1, .owner=KM_H723_OWNER_DIFF_ADC};
    sim.expected_plan.claims[sim.expected_plan.count++] = (km_resource_claim_t){.kind=KM_RESOURCE_ADC_CHANNEL, .instance=1, .subresource=4, .owner=KM_H723_OWNER_DIFF_ADC};
    sim.expected_plan.claims[sim.expected_plan.count++] = (km_resource_claim_t){.kind=KM_RESOURCE_ADC_CHANNEL, .instance=1, .subresource=8, .owner=KM_H723_OWNER_DIFF_ADC};

    km_resource_claim_t claims[KM_RESOURCE_MAX_CLAIMS];
    size_t count = populate_claims(claims);

    /* Begin startup without calibration in analog mode fails */
    assert(km_board_startup_begin(claims, count, 50000u, NULL) == KM_ERR_UNAVAILABLE);

    /* Begin with calibration */
    sim.lane_status[0] = LANE_STATUS_UNAVAILABLE;
    sim.lane_status[1] = LANE_STATUS_UNAVAILABLE;
    km_result_t res = km_board_startup_begin(claims, count, 50000u, &cal);
    assert(res == KM_OK);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_WAITING);
    assert(sim.adc_start_count == 1);
    assert(sim.pwm_setup_count == 2);

    /* Check that rewind ready callback returns false while WAITING */
    assert(sim.rewind_ready_cb[0] != NULL);
    assert(sim.rewind_ready_cb[0](NULL) == false);

    /* Confirm lanes */
    sim.lane_status[0] = LANE_STATUS_READY;
    sim.lane_status[1] = LANE_STATUS_READY;
    assert(km_board_startup_service() == KM_OK);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_READY);

    /* Check that rewind ready callback returns true once READY */
    assert(sim.rewind_ready_cb[0](NULL) == true);

    km_operation_core_t *core = km_board_startup_get_core();
    assert(core != NULL);
    assert(core->feeder_drivers[0] == &sim_feeder_driver[0].base);
    assert(core->rewinder_drivers[0] == &sim_rewind_driver[0].base);
    assert(core->feeder_drivers[1] == &sim_feeder_driver[1].base);
    assert(core->rewinder_drivers[1] == &sim_rewind_driver[1].base);

    /* Production periodic callback keeps sensor sampling and services only
     * the currently active enabled rewind lane. */
    assert(sim.controller_sample_fn != NULL);
    sim.controller_sample_fn(core, sim.controller_sample_context);
    assert(sim.sensor_sample_count == 1u);
    assert(sim.pwm_service_count[0] == 0u && sim.pwm_service_count[1] == 0u);
    core->active_rewinder = core->rewinder_drivers[1];
    core->active_rewinder->enabled = true;
    sim.controller_sample_fn(core, sim.controller_sample_context);
    assert(sim.sensor_sample_count == 2u);
    assert(sim.pwm_service_count[0] == 0u && sim.pwm_service_count[1] == 1u);
    core->active_rewinder->enabled = false;
    sim.controller_sample_fn(core, sim.controller_sample_context);
    assert(sim.sensor_sample_count == 3u);
    assert(sim.pwm_service_count[0] == 0u && sim.pwm_service_count[1] == 1u);
}

/* =========================================================================
 * Test 4: Multi-Lane Current Confirmation
 * ========================================================================= */
static void test_multi_lane_current_confirmation(void)
{
    reset_sim();
    sim.profile.lane_count = 4u;

    for (size_t l = 0; l < 4; ++l) {
        sim.pins.pin[KM_PIN_STEP][l] = (uint16_t)(10 + l * 4);
        sim.pins.pin[KM_PIN_DIR][l] = (uint16_t)(11 + l * 4);
        sim.pins.pin[KM_PIN_ENABLE][l] = (uint16_t)(12 + l * 4);
        sim.pins.pin[KM_PIN_UART][l] = (uint16_t)(13 + l * 4);
    }
    sim.expected_plan.count = 16;
    for (size_t l = 0; l < 4; ++l) {
        sim.expected_plan.claims[l * 4 + 0] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=(uint16_t)(10 + l*4), .owner=KM_PIN_STEP};
        sim.expected_plan.claims[l * 4 + 1] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=(uint16_t)(11 + l*4), .owner=KM_PIN_DIR};
        sim.expected_plan.claims[l * 4 + 2] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=(uint16_t)(12 + l*4), .owner=KM_PIN_ENABLE};
        sim.expected_plan.claims[l * 4 + 3] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=(uint16_t)(13 + l*4), .owner=KM_PIN_UART};
        sim.lane_status[l] = LANE_STATUS_UNAVAILABLE;
    }

    km_resource_claim_t claims[KM_RESOURCE_MAX_CLAIMS];
    size_t count = populate_claims(claims);

    assert(km_board_startup_begin(claims, count, 50000u, NULL) == KM_OK);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_WAITING);

    /* Confirm lane 0 */
    sim.lane_status[0] = LANE_STATUS_READY;
    assert(km_board_startup_service() == KM_ERR_UNAVAILABLE);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_WAITING);

    /* Confirm lane 1 */
    sim.lane_status[1] = LANE_STATUS_READY;
    assert(km_board_startup_service() == KM_ERR_UNAVAILABLE);

    /* Confirm lane 2 */
    sim.lane_status[2] = LANE_STATUS_READY;
    assert(km_board_startup_service() == KM_ERR_UNAVAILABLE);

    /* Confirm lane 3 */
    sim.lane_status[3] = LANE_STATUS_READY;
    assert(km_board_startup_service() == KM_OK);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_READY);
    assert(km_board_startup_get_core() != NULL);
}

/* =========================================================================
 * Test 5: Failure After Earlier Lanes Prepared
 * ========================================================================= */
static void test_failure_after_earlier_lanes_prepared(void)
{
    reset_sim();
    km_resource_claim_t claims[KM_RESOURCE_MAX_CLAIMS];
    size_t count = populate_claims(claims);

    /* Lane 0 setup succeeds; lane 1 setup fails */
    sim.sim_feeder_setup_fail_lane = 1;

    assert(km_board_startup_begin(claims, count, 50000u, NULL) == KM_ERR_FAULTED);

    /* Verify safe-off was executed */
    assert(sim.stepdir_shutdown_count > 0);
    assert(sim.switch_shutdown_count > 0);
    assert(sim.tmc_uart_shutdown_count == 1);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_FAULTED);
    assert(km_board_startup_get_result() == KM_ERR_FAULTED);
    assert(km_board_startup_get_core() == NULL);

    /* Duplicate calls remain faulted and do not reinitialize */
    assert(km_board_startup_begin(claims, count, 50000u, NULL) == KM_ERR_FAULTED);
    assert(km_board_startup_service() == KM_ERR_FAULTED);
    assert(sim.tmc_uart_shutdown_count == 1);
}

/* =========================================================================
 * Test 6: UART Fault During Waiting
 * ========================================================================= */
static void test_failure_uart_fault_during_waiting(void)
{
    reset_sim();
    km_resource_claim_t claims[KM_RESOURCE_MAX_CLAIMS];
    size_t count = populate_claims(claims);

    sim.lane_status[0] = LANE_STATUS_UNAVAILABLE;
    sim.lane_status[1] = LANE_STATUS_UNAVAILABLE;

    assert(km_board_startup_begin(claims, count, 50000u, NULL) == KM_OK);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_WAITING);

    /* Lane 0 ready, lane 1 encounters communication line fault */
    sim.lane_status[0] = LANE_STATUS_READY;
    sim.lane_status[1] = LANE_STATUS_FAULTED;

    assert(km_board_startup_service() == KM_ERR_FAULTED);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_FAULTED);
    assert(km_board_startup_get_result() == KM_ERR_FAULTED);
    assert(km_board_startup_get_core() == NULL);
    assert(sim.stepdir_shutdown_count > 0);
    assert(sim.tmc_uart_shutdown_count == 1);
}

/* =========================================================================
 * Test 7: Deadline Including Setup Duration
 * ========================================================================= */
static void test_deadline_including_setup_duration(void)
{
    reset_sim();
    km_resource_claim_t claims[KM_RESOURCE_MAX_CLAIMS];
    size_t count = populate_claims(claims);

    /* Setup takes 60us per feeder; with 2 feeders, that's 120us elapsed */
    sim.sim_setup_delay_us = 60u;
    /* Timeout set to 100us (< 120us) */
    assert(km_board_startup_begin(claims, count, 100u, NULL) == KM_ERR_TIMEOUT);

    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_FAULTED);
    assert(km_board_startup_get_result() == KM_ERR_TIMEOUT);
    assert(km_board_startup_get_core() == NULL);
    assert(sim.stepdir_shutdown_count > 0);
    assert(sim.tmc_uart_shutdown_count == 1);
}

/* =========================================================================
 * Test 8: Deadline Counter Wrap
 * ========================================================================= */
static void test_deadline_counter_wrap(void)
{
    reset_sim();
    km_resource_claim_t claims[KM_RESOURCE_MAX_CLAIMS];
    size_t count = populate_claims(claims);

    sim.now_us = 0xFFFFFFF0u; /* 16 us before 2^32 */
    sim.lane_status[0] = LANE_STATUS_UNAVAILABLE;
    sim.lane_status[1] = LANE_STATUS_UNAVAILABLE;

    assert(km_board_startup_begin(claims, count, 200u, NULL) == KM_OK);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_WAITING);

    /* Advance time across 32-bit wrap to 0x00000050 (112 us elapsed < 200 us) */
    sim.now_us = 0x00000050u;
    assert(km_board_startup_service() == KM_ERR_UNAVAILABLE);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_WAITING);

    /* Advance time to 0x00000100 (272 us elapsed > 200 us) */
    sim.now_us = 0x00000100u;
    assert(km_board_startup_service() == KM_ERR_TIMEOUT);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_FAULTED);
    assert(km_board_startup_get_result() == KM_ERR_TIMEOUT);
}

/* =========================================================================
 * Test 9: Exact Deadline - Ready Boundary (elapsed >= timeout expires)
 * ========================================================================= */
static void test_deadline_exact_ready(void)
{
    reset_sim();
    km_resource_claim_t claims[KM_RESOURCE_MAX_CLAIMS];
    size_t count = populate_claims(claims);

    sim.now_us = 1000u;
    sim.lane_status[0] = LANE_STATUS_UNAVAILABLE;
    sim.lane_status[1] = LANE_STATUS_UNAVAILABLE;
    assert(km_board_startup_begin(claims, count, 500u, NULL) == KM_OK);

    /* Exactly at deadline (1000 + 500 = 1500us): elapsed >= timeout expires */
    sim.now_us = 1500u;
    sim.lane_status[0] = LANE_STATUS_READY;
    sim.lane_status[1] = LANE_STATUS_READY;
    assert(km_board_startup_service() == KM_ERR_TIMEOUT);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_FAULTED);
    assert(km_board_startup_get_result() == KM_ERR_TIMEOUT);
    assert(km_board_startup_get_core() == NULL);
}

/* =========================================================================
 * Test 10: Exact Deadline - Unconfirmed Boundary
 * ========================================================================= */
static void test_deadline_exact_unconfirmed(void)
{
    reset_sim();
    km_resource_claim_t claims[KM_RESOURCE_MAX_CLAIMS];
    size_t count = populate_claims(claims);

    sim.now_us = 1000u;
    sim.lane_status[0] = LANE_STATUS_UNAVAILABLE;
    sim.lane_status[1] = LANE_STATUS_UNAVAILABLE;
    assert(km_board_startup_begin(claims, count, 500u, NULL) == KM_OK);

    /* Advance clock to exactly 1500us without confirming lanes */
    sim.now_us = 1500u;
    assert(km_board_startup_service() == KM_ERR_TIMEOUT);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_FAULTED);
    assert(km_board_startup_get_result() == KM_ERR_TIMEOUT);
    assert(km_board_startup_get_core() == NULL);
}

/* =========================================================================
 * Test 11: Global Shutdown & Clear-Shutdown
 * ========================================================================= */
static void test_global_shutdown_and_clear_shutdown(void)
{
    reset_sim();
    km_resource_claim_t claims[KM_RESOURCE_MAX_CLAIMS];
    size_t count = populate_claims(claims);

    sim.lane_status[0] = LANE_STATUS_UNAVAILABLE;
    sim.lane_status[1] = LANE_STATUS_UNAVAILABLE;
    assert(km_board_startup_begin(claims, count, 50000u, NULL) == KM_OK);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_WAITING);

    /* Trigger emergency shutdown */
    shutdown("test emergency");

    /* Verify no clock reads occurred during shutdown */
    assert(sim.clock_reads_in_shutdown == 0);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_FAULTED);
    assert(km_board_startup_get_core() == NULL);

    /* Clear shutdown */
    sched_clear_shutdown();
    assert(sched_is_shutdown() == 0);

    /* State MUST remain latched in FAULTED */
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_FAULTED);
    assert(km_board_startup_get_result() == KM_ERR_FAULTED);
    assert(km_board_startup_service() == KM_ERR_FAULTED);
    assert(km_board_startup_begin(claims, count, 50000u, NULL) == KM_ERR_FAULTED);
}

/* =========================================================================
 * Test 12: Duplicate Calls
 * ========================================================================= */
static void test_duplicate_calls(void)
{
    reset_sim();
    km_resource_claim_t claims[KM_RESOURCE_MAX_CLAIMS];
    size_t count = populate_claims(claims);

    sim.lane_status[0] = LANE_STATUS_UNAVAILABLE;
    sim.lane_status[1] = LANE_STATUS_UNAVAILABLE;

    assert(km_board_startup_begin(claims, count, 50000u, NULL) == KM_OK);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_WAITING);

    /* Duplicate begin while waiting */
    assert(km_board_startup_begin(claims, count, 50000u, NULL) == KM_ERR_BUSY);

    /* Confirm lanes */
    sim.lane_status[0] = LANE_STATUS_READY;
    sim.lane_status[1] = LANE_STATUS_READY;
    assert(km_board_startup_service() == KM_OK);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_READY);

    /* Duplicate begin when ready */
    assert(km_board_startup_begin(claims, count, 50000u, NULL) == KM_ERR_BUSY);

    /* Duplicate service when ready does not re-attach or reschedule */
    uint32_t attach_before = sim.controller_attach_count;
    assert(km_board_startup_service() == KM_OK);
    assert(sim.controller_attach_count == attach_before);

    /* Calling service with IRQs disabled */
    sim.irq_status = 1ul;
    assert(km_board_startup_service() == KM_ERR_INVALID_PARAM);
    sim.irq_status = 0ul;
}

/* =========================================================================
 * Test 13: Regression - Activation Elapsed Cost Timeout (Probe 1)
 * ========================================================================= */
static void test_regression_activation_elapsed_cost(void)
{
    reset_sim();
    km_resource_claim_t claims[KM_RESOURCE_MAX_CLAIMS];
    size_t count = populate_claims(claims);

    assert(km_board_startup_begin(claims, count, 500u, NULL) == KM_OK);
    sim.lane_status[0] = LANE_STATUS_READY;
    sim.lane_status[1] = LANE_STATUS_READY;
    sim.sim_activation_delay_us = 600u; /* exceeds 500us timeout during activation */

    km_result_t res = km_board_startup_service();
    assert(res == KM_ERR_TIMEOUT);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_FAULTED);
    assert(km_board_startup_get_core() == NULL);
}

/* =========================================================================
 * Test 14: Regression - Attachment Elapsed Cost Timeout (Probe 2)
 * ========================================================================= */
static void test_regression_attachment_elapsed_cost(void)
{
    reset_sim();
    km_resource_claim_t claims[KM_RESOURCE_MAX_CLAIMS];
    size_t count = populate_claims(claims);

    assert(km_board_startup_begin(claims, count, 500u, NULL) == KM_OK);
    sim.lane_status[0] = LANE_STATUS_READY;
    sim.lane_status[1] = LANE_STATUS_READY;
    sim.sim_controller_attach_delay_us = 600u; /* exceeds 500us timeout during attach */

    km_result_t res = km_board_startup_service();
    assert(res == KM_ERR_TIMEOUT);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_FAULTED);
    assert(km_board_startup_get_core() == NULL);
}

/* =========================================================================
 * Test 15: Regression - Preclaimed Unrelated Slot 7 Causes Pure BUSY (Probe 3)
 * ========================================================================= */
static void test_regression_unrelated_slot(void)
{
    reset_sim();
    km_resource_claim_t claims[KM_RESOURCE_MAX_CLAIMS];
    size_t count = populate_claims(claims);

    sim.sim_preclaimed_stepdir_slot = 7;
    sim.sim_feeder_setup_fail_lane = 1;

    km_result_t res = km_board_startup_begin(claims, count, 500u, NULL);
    assert(res == KM_ERR_BUSY);
    assert(sim.gpio_ops_count == 0);
    assert(sim.stepdir_shutdown_count == 0);
    assert(sim.switch_shutdown_count == 0);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_UNINITIALIZED);
    assert(km_board_startup_get_core() == NULL);
}

/* =========================================================================
 * Test 16: Regression - Powered Profile with Missing Rewinder (Probe 4)
 * ========================================================================= */
static void test_regression_missing_rewinder(void)
{
    reset_sim();
    sim.profile.has_powered_rewind = true;
    sim.profile.rewind_pwm_duty_permille = 250;
    assert(km_profile_validate(&sim.profile) == KM_OK);

    sim.pins.pin[KM_PIN_REWIND_FORWARD][0] = 30;
    sim.pins.pin[KM_PIN_REWIND_REVERSE][0] = 31;
    sim.pins.pin[KM_PIN_REWIND_ENABLE][0] = 32;
    sim.pins.pin[KM_PIN_REWIND_FORWARD][1] = 40;
    sim.pins.pin[KM_PIN_REWIND_REVERSE][1] = 41;
    sim.pins.pin[KM_PIN_REWIND_ENABLE][1] = 42;

    sim.expected_plan.claims[sim.expected_plan.count++] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=30, .owner=KM_PIN_REWIND_FORWARD};
    sim.expected_plan.claims[sim.expected_plan.count++] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=31, .owner=KM_PIN_REWIND_REVERSE};
    sim.expected_plan.claims[sim.expected_plan.count++] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=32, .owner=KM_PIN_REWIND_ENABLE};
    sim.expected_plan.claims[sim.expected_plan.count++] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=40, .owner=KM_PIN_REWIND_FORWARD};
    sim.expected_plan.claims[sim.expected_plan.count++] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=41, .owner=KM_PIN_REWIND_REVERSE};
    sim.expected_plan.claims[sim.expected_plan.count++] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=42, .owner=KM_PIN_REWIND_ENABLE};

    km_resource_claim_t claims[KM_RESOURCE_MAX_CLAIMS];
    size_t count = populate_claims(claims);

    /* Lane 1 rewind driver returns NULL */
    sim.sim_missing_rewind_driver_lane = 1;
    sim.lane_status[0] = LANE_STATUS_READY;
    sim.lane_status[1] = LANE_STATUS_READY;

    km_result_t res = km_board_startup_begin(claims, count, 500u, NULL);
    assert(res == KM_ERR_FAULTED);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_FAULTED);
    assert(km_board_startup_get_core() == NULL);
}

/* =========================================================================
 * Test 17: Regression - Switch GPIO Pure BUSY Rejection (Probe 5)
 * ========================================================================= */
static void test_regression_switch_pure_busy(void)
{
    reset_sim();
    km_resource_claim_t claims[KM_RESOURCE_MAX_CLAIMS];
    size_t count = populate_claims(claims);

    sim.sim_switch_setup_busy = true;
    km_result_t res = km_board_startup_begin(claims, count, 500u, NULL);
    assert(res == KM_ERR_BUSY);
    assert(sim.gpio_ops_count == 0);
    assert(sim.stepdir_shutdown_count == 0);
    assert(sim.switch_shutdown_count == 0);
    /* Local GPIO admission reserves the pin batch before switch setup, so a
     * foreign binding latches instead of staying retryable. */
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_FAULTED);
    assert(km_board_startup_get_core() == NULL);
}

/* =========================================================================
 * Test 18: Regression - Shutdown Injected During Commit / Attach (Atomicity)
 * ========================================================================= */
static void test_regression_shutdown_during_commit(void)
{
    reset_sim();
    km_resource_claim_t claims[KM_RESOURCE_MAX_CLAIMS];
    size_t count = populate_claims(claims);

    assert(km_board_startup_begin(claims, count, 50000u, NULL) == KM_OK);
    sim.lane_status[0] = LANE_STATUS_READY;
    sim.lane_status[1] = LANE_STATUS_READY;
    sim.sim_shutdown_during_attach = true;

    km_result_t res = km_board_startup_service();
    assert(res == KM_ERR_FAULTED);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_FAULTED);
    assert(km_board_startup_get_core() == NULL);
}

/* =========================================================================
 * Test 19: Regression - Service Under Scheduler Shutdown Cannot Return Prior OK
 * ========================================================================= */
static void test_service_under_scheduler_shutdown_cannot_return_ok(void)
{
    reset_sim();
    km_resource_claim_t claims[KM_RESOURCE_MAX_CLAIMS];
    size_t count = populate_claims(claims);

    assert(km_board_startup_begin(claims, count, 50000u, NULL) == KM_OK);
    sim.lane_status[0] = LANE_STATUS_READY;
    sim.lane_status[1] = LANE_STATUS_READY;
    assert(km_board_startup_service() == KM_OK);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_READY);

    /* Scheduler shutdown is triggered externally without calling startup hook */
    sim.sched_shutdown = 1;
    assert(km_board_startup_service() == KM_ERR_FAULTED);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_FAULTED);
    assert(km_board_startup_get_result() == KM_ERR_FAULTED);
    assert(km_board_startup_get_core() == NULL);
}

/* =========================================================================
 * Test 20: Regression - First Lane Feeder Setup Partial Failure (Probe 7)
 * ========================================================================= */
static void test_regression_first_lane_feeder_partial_failure(void)
{
    reset_sim();
    km_resource_claim_t claims[KM_RESOURCE_MAX_CLAIMS];
    size_t count = populate_claims(claims);

    /* Lane 0 feeder setup fails after GPIO touched */
    sim.sim_feeder_setup_fail_lane = 0;

    km_result_t res = km_board_startup_begin(claims, count, 500u, NULL);
    assert(res == KM_ERR_FAULTED);
    assert(sim.feeder_setup_count == 1);
    assert(sim.stepdir_shutdown_count > 0);
    assert(sim.tmc_uart_shutdown_count == 1);
    assert(sim.switch_shutdown_count > 0);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_FAULTED);
    assert(km_board_startup_get_result() == KM_ERR_FAULTED);
    assert(km_board_startup_get_core() == NULL);
}

/* =========================================================================
 * Test 21: Regression - Partial ADC Setup Failure Safe-Off
 * ========================================================================= */
static void test_regression_partial_adc_setup_failure(void)
{
    reset_sim();
    sim.profile.has_proportional_buffer = true;
    sim.profile.proportional_gain_milli = 8000;
    sim.profile.buffer_deadband_permille = 10;
    km_diff_adc_config_t cal = {
        .channel_map = KM_DIFF_CH_A_LOW_B_HIGH,
        .vref_mv = 3300,
        .adc_max_counts = 4095,
        .path_tolerance_counts = 32,
        .point_count = 3,
        .points = {
            {0, 2048, 0},
            {2048, 2048, 500},
            {2048, 4095, 1000}
        }
    };
    sim.profile.diff_calibration = cal;
    sim.profile.has_diff_calibration = true;
    sim.expected_plan.claims[sim.expected_plan.count++] = (km_resource_claim_t){.kind=KM_RESOURCE_ADC_UNIT, .instance=1, .owner=KM_H723_OWNER_DIFF_ADC};
    sim.expected_plan.claims[sim.expected_plan.count++] = (km_resource_claim_t){.kind=KM_RESOURCE_ADC_CHANNEL, .instance=1, .subresource=4, .owner=KM_H723_OWNER_DIFF_ADC};
    sim.expected_plan.claims[sim.expected_plan.count++] = (km_resource_claim_t){.kind=KM_RESOURCE_ADC_CHANNEL, .instance=1, .subresource=8, .owner=KM_H723_OWNER_DIFF_ADC};

    km_resource_claim_t claims[KM_RESOURCE_MAX_CLAIMS];
    size_t count = populate_claims(claims);

    sim.sim_adc_start_fail = true;

    km_result_t res = km_board_startup_begin(claims, count, 500u, &cal);
    assert(res == KM_ERR_FAULTED);
    assert(sim.adc_start_count == 1);
    assert(sim.adc_shutdown_count > 0);
    assert(sim.switch_shutdown_count > 0);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_FAULTED);
    assert(km_board_startup_get_result() == KM_ERR_FAULTED);
    assert(km_board_startup_get_core() == NULL);
}

/* =========================================================================
 * Test 22: Regression - Partial PWM Setup Failure Safe-Off
 * ========================================================================= */
static void test_regression_partial_pwm_setup_failure(void)
{
    reset_sim();
    sim.profile.has_powered_rewind = true;
    sim.profile.rewind_pwm_duty_permille = 250;
    assert(km_profile_validate(&sim.profile) == KM_OK);

    sim.pins.pin[KM_PIN_REWIND_FORWARD][0] = 30;
    sim.pins.pin[KM_PIN_REWIND_REVERSE][0] = 31;
    sim.pins.pin[KM_PIN_REWIND_ENABLE][0] = 32;
    sim.pins.pin[KM_PIN_REWIND_FORWARD][1] = 40;
    sim.pins.pin[KM_PIN_REWIND_REVERSE][1] = 41;
    sim.pins.pin[KM_PIN_REWIND_ENABLE][1] = 42;

    sim.expected_plan.claims[sim.expected_plan.count++] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=30, .owner=KM_PIN_REWIND_FORWARD};
    sim.expected_plan.claims[sim.expected_plan.count++] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=31, .owner=KM_PIN_REWIND_REVERSE};
    sim.expected_plan.claims[sim.expected_plan.count++] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=32, .owner=KM_PIN_REWIND_ENABLE};
    sim.expected_plan.claims[sim.expected_plan.count++] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=40, .owner=KM_PIN_REWIND_FORWARD};
    sim.expected_plan.claims[sim.expected_plan.count++] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=41, .owner=KM_PIN_REWIND_REVERSE};
    sim.expected_plan.claims[sim.expected_plan.count++] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=42, .owner=KM_PIN_REWIND_ENABLE};

    km_resource_claim_t claims[KM_RESOURCE_MAX_CLAIMS];
    size_t count = populate_claims(claims);

    sim.sim_pwm_setup_fail = true;

    km_result_t res = km_board_startup_begin(claims, count, 500u, NULL);
    assert(res == KM_ERR_FAULTED);
    assert(sim.pwm_setup_count == 1);
    assert(sim.pwm_shutdown_count > 0);
    assert(sim.stepdir_shutdown_count > 0);
    assert(sim.switch_shutdown_count > 0);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_FAULTED);
    assert(km_board_startup_get_result() == KM_ERR_FAULTED);
    assert(km_board_startup_get_core() == NULL);
}

/* =========================================================================
 * Test 23: Regression - Partial Switch Setup Failure Safe-Off
 * ========================================================================= */
static void test_regression_partial_switch_setup_failure(void)
{
    reset_sim();
    km_resource_claim_t claims[KM_RESOURCE_MAX_CLAIMS];
    size_t count = populate_claims(claims);

    sim.sim_switch_setup_fail = true;

    km_result_t res = km_board_startup_begin(claims, count, 500u, NULL);
    assert(res == KM_ERR_FAULTED);
    assert(sim.switch_setup_count == 1);
    assert(sim.switch_shutdown_count > 0);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_FAULTED);
    assert(km_board_startup_get_result() == KM_ERR_FAULTED);
    assert(km_board_startup_get_core() == NULL);
}

/* =========================================================================
 * Test 24: Regression - Publication Critical Section Interrupt Delay (Probe 8)
 * ========================================================================= */
static void test_regression_irq_delay_before_publication(void)
{
    reset_sim();
    km_resource_claim_t claims[KM_RESOURCE_MAX_CLAIMS];
    size_t count = populate_claims(claims);

    assert(km_board_startup_begin(claims, count, 500u, NULL) == KM_OK);
    sim.lane_status[0] = LANE_STATUS_READY;
    sim.lane_status[1] = LANE_STATUS_READY;

    /* 600us interrupt delay injected between controller attach and irq_save for publication */
    sim.sim_irq_save_delay_us = 600u;

    km_result_t res = km_board_startup_service();
    assert(res == KM_ERR_TIMEOUT);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_FAULTED);
    assert(km_board_startup_get_result() == KM_ERR_TIMEOUT);
    assert(km_board_startup_get_core() == NULL);
    assert(sim.controller_attached == true);
    assert(sim.controller_shutdown_count > 0);
}

/* =========================================================================
 * Test 25: TMC UART Receiver Pull-up Composition Regression
 * ========================================================================= */
static void test_regression_tmc_uart_pullup_composition(void)
{
    /* 1. Prove malformed factory rejection causes zero physical setup */
    reset_sim();
    km_resource_claim_t claims[KM_RESOURCE_MAX_CLAIMS];
    size_t count = populate_claims(claims);

    sim.sim_tmc_config_fail = true;

    km_result_t res = km_board_startup_begin(claims, count, 100000u, NULL);
    assert(res == KM_ERR_INVALID_PARAM);
    assert(sim.gpio_ops_count == 0);
    assert(sim.feeder_setup_count == 0);
    assert(sim.switch_setup_count == 0);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_UNINITIALIZED);
    assert(km_board_startup_get_result() == KM_ERR_UNAVAILABLE);
    assert(km_board_startup_get_core() == NULL);

    /* 2. Prove distinct per-lane factory values reach actual setup argument */
    reset_sim();
    sim.sim_tmc_config_fail = false;
    sim.tmc_configs[0].uart_pull_up = true;
    sim.tmc_configs[1].uart_pull_up = false;

    assert(km_board_startup_begin(claims, count, 100000u, NULL) == KM_OK);
    assert(sim.feeder_setup_count == 2);
    assert(sim.captured_uart_pull_up[0] == true);
    assert(sim.captured_uart_pull_up[1] == false);
}

#if CONFIG_KLIPPER_MMU_SERVO
/* =========================================================================
 * Selector scenario: LINEAR axis + grip servo behind one shared gear
 * (klipper_mmu/selector.h). Hand-written CONFIG_* shim values stand in for
 * Agent C's not-yet-available Kconfig.selector.
 * ========================================================================= */
static void test_selector_linear_servo_startup(void)
{
    reset_sim();

    sim.profile.lane_count = 8u;
    sim.profile.has_selector = true;
    sim.profile.has_bay_sensors = false;
    sim.profile.has_load_sensors = false;
    sim.profile.has_powered_rewind = false;
    sim.profile.has_proportional_buffer = false;
    sim.profile.has_encoder = false;
    sim.profile.has_current_sensing = false;
    sim.profile.selector = (km_selector_profile_t){
        .kind = KM_SELECTOR_LINEAR,
        .home = {.speed_steps_s = 4800u, .acceleration_steps_s2 = 96000u,
                 .deceleration_steps_s2 = 96000u, .max_travel_steps = 15000u,
                 .deadline_us = 10000000u, .max_switch_age_us = 20000u,
                 .home_direction = KM_DIR_REVERSE},
        .gate_steps = {320, 2160, 4000, 5840, 7680, 9520, 11360, 13200},
        .velocity_steps_s = 16000u,
        .acceleration_steps_s2 = 96000u,
        .has_servo = true,
        .servo_engage_us = 1400u,
        .servo_release_us = 1900u,
        .servo_move_us = 1650u,
        .servo_dwell_ms = 250u,
    };

    /* The gear: one shared driver at physical slot 0, regardless of the
     * 8 logical lanes above. */
    sim.pins.pin[KM_PIN_STEP][0] = 10;
    sim.pins.pin[KM_PIN_DIR][0] = 11;
    sim.pins.pin[KM_PIN_ENABLE][0] = 12;
    sim.pins.pin[KM_PIN_UART][0] = 13;
    sim.feeder_configs[0] = (km_step_dir_config_t){
        .step_active_high = true, .pulse_high_us = 3, .pulse_low_us = 3,
        .maximum_velocity_steps_s = 100000u,
    };
    sim.tmc_configs[0].tmc.run_current_ma = sim.profile.default_run_current_ma;
    sim.tmc_configs[0].tmc.hold_current_ma = sim.profile.default_hold_current_ma;
    sim.lane_status[0] = LANE_STATUS_READY;

    /* The selector axis and its grip servo. */
    sim.pins.pin[KM_PIN_SELECTOR_STEP][0] = 30;
    sim.pins.pin[KM_PIN_SELECTOR_DIR][0] = 31;
    sim.pins.pin[KM_PIN_SELECTOR_ENABLE][0] = 32;
    sim.pins.pin[KM_PIN_SELECTOR_ENDSTOP][0] = 33;
    sim.pins.pin[KM_PIN_SERVO][0] = 40;

    sim.expected_plan.count = 9;
    sim.expected_plan.claims[0] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=10, .owner=KM_PIN_STEP};
    sim.expected_plan.claims[1] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=11, .owner=KM_PIN_DIR};
    sim.expected_plan.claims[2] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=12, .owner=KM_PIN_ENABLE};
    sim.expected_plan.claims[3] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=13, .owner=KM_PIN_UART};
    sim.expected_plan.claims[4] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=30, .owner=KM_PIN_SELECTOR_STEP};
    sim.expected_plan.claims[5] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=31, .owner=KM_PIN_SELECTOR_DIR};
    sim.expected_plan.claims[6] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=32, .owner=KM_PIN_SELECTOR_ENABLE};
    sim.expected_plan.claims[7] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=33, .owner=KM_PIN_SELECTOR_ENDSTOP};
    sim.expected_plan.claims[8] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=40, .owner=KM_PIN_SERVO};

    km_resource_claim_t claims[KM_RESOURCE_MAX_CLAIMS];
    size_t count = populate_claims(claims);

    assert(km_board_startup_begin(claims, count, 100000u, NULL) == KM_OK);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_READY);

    /* One physical gear, not eight: setup/activation ran exactly once. */
    assert(sim.feeder_setup_count == 1u);
    assert(sim.feeder_activate_count >= 1u);
    assert(sim.selector_stepdir_setup_count == 1u);

    /* The one gear is bound to all eight lanes, and the core holds the
     * selector stepper and servo board_startup.c bound. */
    km_operation_core_t *core = km_board_startup_get_core();
    assert(core != NULL);
    for (unsigned lane = 0; lane < sim.profile.lane_count; ++lane) {
        assert(core->feeder_drivers[lane] == &sim_feeder_driver[0].base);
    }
    assert(core->selector_driver == &sim_selector_driver.base);
    assert(core->selector_servo != NULL);
    sim.captured_selector_servo = core->selector_servo;

    /* Servo pulse edges, in mcu/servo_pwm.c's real scheduled software PWM
     * against the fake board/gpio.h and sched.h timer above: engaging
     * (1400us active) inside the fixed 20ms period, then releasing. */
    assert(sim.captured_selector_servo->set_pulse_us(sim.captured_selector_servo, 1400u) == KM_OK);
    unsigned before = gpio_event_count;
    run_armed_timer_until(sim.now_us + 1u);
    assert(gpio_event_count > before);
    assert(gpio_events[gpio_event_count - 1].pin == 40u);
    assert(gpio_events[gpio_event_count - 1].val == 1u);           /* rising edge */
    uint32_t rose_at = gpio_events[gpio_event_count - 1].at_us;

    run_armed_timer_until(rose_at + 1400u);
    assert(gpio_events[gpio_event_count - 1].val == 0u);           /* falls after 1400us */
    assert(gpio_events[gpio_event_count - 1].at_us == rose_at + 1400u);

    run_armed_timer_until(rose_at + 20000u);
    assert(gpio_events[gpio_event_count - 1].val == 1u);           /* next 20ms period */
    assert(gpio_events[gpio_event_count - 1].at_us == rose_at + 20000u);

    /* Stopping mid-pulse never cuts the pulse short (a runt is a short
     * command a servo may follow): it ends at its full width, then no more. */
    assert(sim.captured_selector_servo->set_pulse_us(sim.captured_selector_servo, 0u) == KM_OK);
    assert(gpio_events[gpio_event_count - 1].val == 1u);           /* still high */
    run_armed_timer_until(rose_at + 40000u);
    assert(gpio_events[gpio_event_count - 1].val == 0u);           /* stopped, pin low */
    assert(gpio_events[gpio_event_count - 1].at_us == rose_at + 21400u);
    assert(armed_timer == NULL);

    /* A stop and restart before the pending event retires the timer reuse it
     * (the fake scheduler asserts it is never added twice). */
    assert(sim.captured_selector_servo->set_pulse_us(sim.captured_selector_servo, 1400u) == KM_OK);
    assert(sim.captured_selector_servo->set_pulse_us(sim.captured_selector_servo, 0u) == KM_OK);
    assert(sim.captured_selector_servo->set_pulse_us(sim.captured_selector_servo, 1600u) == KM_OK);
    run_armed_timer_until(sim.now_us + 1u);
    assert(gpio_events[gpio_event_count - 1].val == 1u);           /* pulsing again */
    uint32_t rerose_at = gpio_events[gpio_event_count - 1].at_us;
    run_armed_timer_until(rerose_at + 1600u);
    assert(gpio_events[gpio_event_count - 1].val == 0u);           /* new 1600us pulse */
    assert(gpio_events[gpio_event_count - 1].at_us == rerose_at + 1600u);
}
#endif

/* =========================================================================
 * Selector scenario: TMC2209-over-UART selector axis (klipper_mmu/selector.h,
 * KLIPPER_MMU_SELECTOR_UART). The selector's own TMC feeder occupies physical
 * slot 1, set up before the deadline starts and confirmed during WAITING
 * exactly like the gear's slot 0 -- no plain STEP/DIR fallback runs.
 * ========================================================================= */
static void selector_uart_fixture(void)
{
    reset_sim();

    sim.profile.lane_count = 4u;
    sim.profile.has_selector = true;
    sim.profile.has_bay_sensors = false;
    sim.profile.has_load_sensors = false;
    sim.profile.has_powered_rewind = false;
    sim.profile.has_proportional_buffer = false;
    sim.profile.has_encoder = false;
    sim.profile.has_current_sensing = false;
    sim.profile.selector = (km_selector_profile_t){
        .kind = KM_SELECTOR_LINEAR,
        .home = {.speed_steps_s = 4800u, .acceleration_steps_s2 = 96000u,
                 .deceleration_steps_s2 = 96000u, .max_travel_steps = 15000u,
                 .deadline_us = 10000000u, .max_switch_age_us = 20000u,
                 .home_direction = KM_DIR_REVERSE},
        .gate_steps = {320, 2160, 4000, 5840},
        .velocity_steps_s = 16000u,
        .acceleration_steps_s2 = 96000u,
    };

    /* The gear: one shared driver at physical slot 0. */
    sim.pins.pin[KM_PIN_STEP][0] = 10;
    sim.pins.pin[KM_PIN_DIR][0] = 11;
    sim.pins.pin[KM_PIN_ENABLE][0] = 12;
    sim.pins.pin[KM_PIN_UART][0] = 13;
    sim.feeder_configs[0] = (km_step_dir_config_t){
        .step_active_high = true, .pulse_high_us = 3, .pulse_low_us = 3,
        .maximum_velocity_steps_s = 100000u,
    };
    sim.tmc_configs[0].tmc.run_current_ma = sim.profile.default_run_current_ma;
    sim.tmc_configs[0].tmc.hold_current_ma = sim.profile.default_hold_current_ma;
    sim.tmc_configs[0].tmc.maximum_current_ma = 1500u;
    sim.tmc_configs[0].tmc.sense_resistor_milliohms = 110u;
    sim.lane_status[0] = LANE_STATUS_READY;

    /* The selector axis, on its own dedicated UART pin/slot 1. */
    sim.pins.pin[KM_PIN_SELECTOR_STEP][0] = 30;
    sim.pins.pin[KM_PIN_SELECTOR_DIR][0] = 31;
    sim.pins.pin[KM_PIN_SELECTOR_ENABLE][0] = 32;
    sim.pins.pin[KM_PIN_SELECTOR_ENDSTOP][0] = 33;
    sim.pins.pin[KM_PIN_SELECTOR_UART][0] = 34;
    sim.selector_tmc_config = (km_tmc_uart_config_t){
        .clock_hz = 520000000u, .uart_pull_up = true,
        .tmc = {.address = 0u, .sense_resistor_milliohms = 110u,
                .maximum_current_ma = 1500u, .run_current_ma = 500u,
                .hold_current_ma = 200u, .microsteps = 16u,
                .interpolate = true, .transaction_timeout_us = 20000u,
                .diagnostic_interval_us = 100000u},
    };

    sim.expected_plan.count = 9;
    sim.expected_plan.claims[0] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=10, .owner=KM_PIN_STEP};
    sim.expected_plan.claims[1] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=11, .owner=KM_PIN_DIR};
    sim.expected_plan.claims[2] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=12, .owner=KM_PIN_ENABLE};
    sim.expected_plan.claims[3] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=13, .owner=KM_PIN_UART};
    sim.expected_plan.claims[4] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=30, .owner=KM_PIN_SELECTOR_STEP};
    sim.expected_plan.claims[5] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=31, .owner=KM_PIN_SELECTOR_DIR};
    sim.expected_plan.claims[6] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=32, .owner=KM_PIN_SELECTOR_ENABLE};
    sim.expected_plan.claims[7] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=33, .owner=KM_PIN_SELECTOR_ENDSTOP};
    sim.expected_plan.claims[8] = (km_resource_claim_t){.kind=KM_RESOURCE_PIN, .instance=34, .owner=KM_PIN_SELECTOR_UART};
}

static void test_selector_uart_startup(void)
{
    selector_uart_fixture();
    sim.lane_status[1] = LANE_STATUS_READY; /* Selector's own TMC UART, confirmed. */

    km_resource_claim_t claims[KM_RESOURCE_MAX_CLAIMS];
    size_t count = populate_claims(claims);

    assert(km_board_startup_begin(claims, count, 100000u, NULL) == KM_OK);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_READY);

    /* Gear (slot 0) and selector (slot 1) each go through the feeder factory
     * exactly once; the plain step/dir fallback never runs. */
    assert(sim.feeder_setup_count == 2u);
    assert(sim.feeder_activate_count >= 2u);
    assert(sim.selector_stepdir_setup_count == 0u);
    assert(sim.captured_step[1] == 30u);

    km_operation_core_t *core = km_board_startup_get_core();
    assert(core != NULL);
    assert(core->selector_driver == &sim_feeder_driver[1].base);
}

static void test_selector_uart_unconfirmed_then_ready(void)
{
    selector_uart_fixture();
    sim.lane_status[1] = LANE_STATUS_UNAVAILABLE; /* Not confirmed yet. */

    km_resource_claim_t claims[KM_RESOURCE_MAX_CLAIMS];
    size_t count = populate_claims(claims);

    /* Same outcome an unconfirmed gear lane gets: parks in WAITING, not
     * FAULTED, and the plain step/dir fallback never runs while waiting. */
    assert(km_board_startup_begin(claims, count, 100000u, NULL) == KM_OK);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_WAITING);
    assert(km_board_startup_get_result() == KM_ERR_UNAVAILABLE);
    assert(km_board_startup_get_core() == NULL);
    assert(sim.selector_stepdir_setup_count == 0u);

    sim.now_us += 1000u;
    assert(km_board_startup_service() == KM_ERR_UNAVAILABLE);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_WAITING);

    sim.lane_status[1] = LANE_STATUS_READY;
    sim.now_us += 1000u;
    assert(km_board_startup_service() == KM_OK);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_READY);

    km_operation_core_t *core = km_board_startup_get_core();
    assert(core != NULL);
    assert(core->selector_driver == &sim_feeder_driver[1].base);
    assert(sim.selector_stepdir_setup_count == 0u);
}

/* Happy Hare's classic ERCF 1.1 EASY-BRD wiring: the selector's own TMC2209
 * shares the gear's physical UART pin at a distinct address
 * (mcu/pin_config.c, mcu/tmc_uart_gpio.c; docs/selectors.md). board_startup.c
 * itself never compares the two pins -- it only checks KM_PIN_SELECTOR_UART
 * against KM_PIN_UNUSED and forwards whichever pin km_config_pins_view
 * reports -- so reaching READY here proves no hidden equal-pin assumption
 * crept into its own sequencing; the sharing mechanics themselves are
 * covered at the real driver level by tests/klipper_tmc_feeder's
 * test_shared_uart_pin_* cases. */
static void test_selector_uart_shares_gear_pin_reaches_ready(void)
{
    selector_uart_fixture();
    sim.pins.pin[KM_PIN_SELECTOR_UART][0] = sim.pins.pin[KM_PIN_UART][0]; /* Shared wire. */
    sim.selector_tmc_config.tmc.address = 1u; /* Distinct from the gear's own address 0. */
    sim.expected_plan.count = 8u; /* No separate claim for the now-shared pin. */
    sim.lane_status[1] = LANE_STATUS_READY;

    km_resource_claim_t claims[KM_RESOURCE_MAX_CLAIMS];
    size_t count = populate_claims(claims);

    assert(km_board_startup_begin(claims, count, 100000u, NULL) == KM_OK);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_READY);
    assert(sim.feeder_setup_count == 2u);
    assert(sim.feeder_activate_count >= 2u);
    assert(sim.selector_stepdir_setup_count == 0u);
    assert(sim.captured_step[1] == 30u);

    km_operation_core_t *core = km_board_startup_get_core();
    assert(core != NULL);
    assert(core->selector_driver == &sim_feeder_driver[1].base);
}

/* =========================================================================
 * Main Runner - Fresh Process per Boot Scenario with Signal Diagnostics
 * ========================================================================= */
static void run_test(void (*test_fn)(void), const char *name)
{
    fflush(stdout);
    fflush(stderr);
    pid_t pid = fork();
    if (pid < 0) {
        perror("fork");
        exit(1);
    }
    if (pid == 0) {
        test_fn();
        exit(0);
    }
    int status = 0;
    if (waitpid(pid, &status, 0) < 0) {
        perror("waitpid");
        exit(1);
    }
    if (WIFSIGNALED(status)) {
        fprintf(stderr, "[-] Test suite %s CRASHED with signal %d\n", name, WTERMSIG(status));
        exit(1);
    }
    if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
        fprintf(stderr, "[-] Test suite %s FAILED (exit status %d)\n", name, WEXITSTATUS(status));
        exit(1);
    }
    printf("[+] Suite %s PASSED\n", name);
}

int main(void)
{
    printf("=== Starting STM32H723 Klipper-MMU Board Startup Tests ===\n");
    run_test(test_validation_no_io_rejection, "test_validation_no_io_rejection");
    run_test(test_boxturtle_from_uninitialized, "test_boxturtle_from_uninitialized");
    run_test(test_switched_passive_progression, "test_switched_passive_progression");
    run_test(test_five_lane_virtual_hub_startup, "test_five_lane_virtual_hub_startup");
    run_test(test_analog_powered_progression, "test_analog_powered_progression");
    run_test(test_multi_lane_current_confirmation, "test_multi_lane_current_confirmation");
    run_test(test_failure_after_earlier_lanes_prepared, "test_failure_after_earlier_lanes_prepared");
    run_test(test_failure_uart_fault_during_waiting, "test_failure_uart_fault_during_waiting");
    run_test(test_deadline_including_setup_duration, "test_deadline_including_setup_duration");
    run_test(test_deadline_counter_wrap, "test_deadline_counter_wrap");
    run_test(test_deadline_exact_ready, "test_deadline_exact_ready");
    run_test(test_deadline_exact_unconfirmed, "test_deadline_exact_unconfirmed");
    run_test(test_global_shutdown_and_clear_shutdown, "test_global_shutdown_and_clear_shutdown");
    run_test(test_duplicate_calls, "test_duplicate_calls");
    run_test(test_regression_activation_elapsed_cost, "test_regression_activation_elapsed_cost");
    run_test(test_regression_attachment_elapsed_cost, "test_regression_attachment_elapsed_cost");
    run_test(test_regression_unrelated_slot, "test_regression_unrelated_slot");
    run_test(test_regression_missing_rewinder, "test_regression_missing_rewinder");
    run_test(test_regression_switch_pure_busy, "test_regression_switch_pure_busy");
    run_test(test_regression_shutdown_during_commit, "test_regression_shutdown_during_commit");
    run_test(test_service_under_scheduler_shutdown_cannot_return_ok, "test_service_under_scheduler_shutdown_cannot_return_ok");
    run_test(test_regression_first_lane_feeder_partial_failure, "test_regression_first_lane_feeder_partial_failure");
    run_test(test_regression_partial_adc_setup_failure, "test_regression_partial_adc_setup_failure");
    run_test(test_regression_partial_pwm_setup_failure, "test_regression_partial_pwm_setup_failure");
    run_test(test_regression_partial_switch_setup_failure, "test_regression_partial_switch_setup_failure");
    run_test(test_regression_irq_delay_before_publication, "test_regression_irq_delay_before_publication");
    run_test(test_regression_tmc_uart_pullup_composition, "test_regression_tmc_uart_pullup_composition");
    run_test(test_encoder_disabled_by_default, "test_encoder_disabled_by_default");
    run_test(test_encoder_startup_success, "test_encoder_startup_success");
    run_test(test_encoder_config_unavailable_rejected, "test_encoder_config_unavailable_rejected");
    run_test(test_encoder_setup_failure_cleans_up, "test_encoder_setup_failure_cleans_up");
#if CONFIG_KLIPPER_MMU_SERVO
    run_test(test_selector_linear_servo_startup, "test_selector_linear_servo_startup");
#endif
    run_test(test_selector_uart_startup, "test_selector_uart_startup");
    run_test(test_selector_uart_unconfirmed_then_ready, "test_selector_uart_unconfirmed_then_ready");
    run_test(test_selector_uart_shares_gear_pin_reaches_ready,
             "test_selector_uart_shares_gear_pin_reaches_ready");
    printf("=== All 35 Board Startup Test Suites PASSED! ===\n");
    return 0;
}
