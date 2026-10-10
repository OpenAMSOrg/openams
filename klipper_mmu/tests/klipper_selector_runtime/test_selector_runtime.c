// SPDX-License-Identifier: GPL-3.0-or-later
/* Hosted-runtime proof for every implemented shared-drive selector device
 * (LINEAR, ROTARY, SERVO, INDEXED; klipper_mmu/selector.h), not just one
 * representative per kind (tests/klipper_selector_runtime/run_runtime.py
 * DEVICES). Compiles the real board startup, pin/switch/TMC configuration,
 * diagnostic startup and command dispatch against one saved selector image's
 * autoconf.h -- the same shape as tests/klipper_emu_runtime and tests/
 * klipper_nightowl_runtime -- and proves the saved image reaches READY with
 * the selector bound, completes an explicit HOME, and lands one lane
 * selection. Kept as its own fixture (not a new case in tests/klipper_board_
 * startup/test_boxturtle_boot.c) because a selector machine mostly has no
 * BAY/LOAD/rewind and needs its own generic mocks instead of that file's
 * BoxTurtle-shaped ones. One test function covers every selector device:
 * which mocks matter (a selector TMC feeder slot, a servo, a per-lane
 * rewinder), how a selection completes (an endstop, a per-gate switch, or
 * nothing but a servo dwell), whether a lane's BAY switch gates LOAD
 * (Claymore's own per-lane LOAD1-4, docs/selectors.md), and whether a gate
 * needs the runtime gate-set path calibrated first (Pico MMU's uncalibrated
 * defaults) all come from the actual profile the saved image reports, not
 * from a macro per device. */
#include "autoconf.h"
#include "clock.h"
#include "config.h"
#include "pin_config.h"
#include "h723_resources.h"
#include "feeder_config.h"
#include "tmc_config.h"
#include "switch_config.h"
#include "rewind_config.h"
#include "pwm_bridge_gpio.h"
#include "stm32/pwm.h"
#include "switch_gpio.h"
#include "stepdir_gpio.h"
#include "tmc_uart_gpio.h"
#include "sensors.h"
#include "controller.h"
#include "commands.h"
#include "diagnostic_startup.h"
#include "board_startup.h"
#include "basecmd.h"
#include "board/irq.h"
#include "sched.h"
#include "stm32/gpio_local.h"
#include "stm32/internal.h"
#include "mock_driver.h"
#include "fake_clock.h"

#include <assert.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>

_Static_assert(CONFIG_WANT_GPIO_LOCAL,
               "selector runtime test requires local GPIO admission");
_Static_assert(CONFIG_WANT_TMCUART_LOCAL && CONFIG_KLIPPER_MMU_TMC_UART_SHARED,
               "selector runtime test requires the shared TMC UART gate");
_Static_assert(CONFIG_KLIPPER_MMU_DIAGNOSTIC_STARTUP,
               "selector runtime test requires production diagnostic startup");
/* Every H723 selector image uses SELECTOR_BOOT_GATES and its own hardware
 * boot identity. ERCF 2.0/3.0 on G0B0/G0B1 (Happy Hare's real MMB 2.0 board
 * default, mcu/Kconfig KLIPPER_MMU_G0B_BOOT_GATES) share the host-epoch
 * identity Angry Beaver's own passive G0B row already uses instead --
 * neither device here needs a fresh boot gate of its own. */
#if CONFIG_MACH_STM32G0B0 || CONFIG_MACH_STM32G0B1
_Static_assert(CONFIG_KLIPPER_MMU_G0B_BOOT_GATES,
               "selector G0B runtime requires admitted local startup drivers");
_Static_assert(CONFIG_KLIPPER_MMU_BOOT_IDENTITY_HOST,
               "selector G0B runtime requires its host-epoch identity gate");
#else
_Static_assert(CONFIG_KLIPPER_MMU_SELECTOR_BOOT_GATES,
               "selector runtime test requires the selector boot gate");
_Static_assert(!CONFIG_KLIPPER_MMU_BOOT_IDENTITY_HOST,
               "selector images use the H723 boot identity, not the host epoch");
#endif

/* ---- Generic scheduler / clock / GPIO mocks (device-independent). ---- */

static uint32_t now_us = 1000;
static struct timer *armed_timer;
static uint8_t input_levels[128]; /* 1 = idle (active-low), 0 = asserted. */
static uint32_t reserved_pins[KM_RESOURCE_MAX_CLAIMS];
static uint32_t reserved_count;
static unsigned reserve_calls;
static enum km_clock_state clock_state = KM_CLOCK_STATE_UNAVAILABLE;

irqstatus_t irq_save(void) { return 0ul; }
void irq_restore(irqstatus_t status) { (void)status; }
uint32_t timer_read_time(void) { return now_us; }
uint32_t timer_from_us(uint32_t us) { return us; }
int timer_is_before(uint32_t a, uint32_t b) { return (int32_t)(a - b) < 0; }
void sched_wake_task(struct task_wake *wake) { wake->wake = 1; }
uint8_t sched_check_wake(struct task_wake *wake)
{
    if (!wake->wake) return 0;
    wake->wake = 0;
    return 1;
}
void sched_add_timer(struct timer *timer) { armed_timer = timer; }
void sched_del_timer(struct timer *timer) { if (armed_timer == timer) armed_timer = NULL; }
uint8_t sched_is_shutdown(void) { return 0; }
void register_shutdown(shutdown_func_t function) { (void)function; }
void shutdown(const char *msg) { (void)msg; assert(0); }

static void fire_timer(void)
{
    struct timer *timer = armed_timer;
    assert(timer && timer->func);
    if (timer_is_before(now_us, timer->waketime)) now_us = timer->waketime;
    uint8_t result = timer->func(timer);
    assert(result == SF_RESCHEDULE || result == SF_DONE);
}

enum km_clock_state km_clock_get_state(void) { return clock_state; }
km_result_t km_clock_get_time(km_core_timepoint_t *out)
{
    if (clock_state != KM_CLOCK_STATE_READY) return KM_ERR_UNAVAILABLE;
    out->us = now_us;
    out->ms = now_us / 1000u;
    return KM_OK;
}
km_result_t km_clock_get_completion_us(uint32_t *out)
{
    if (!out || clock_state != KM_CLOCK_STATE_READY) return KM_ERR_UNAVAILABLE;
    *out = now_us;
    return KM_OK;
}
km_result_t km_clock_deadline_ticks(uint32_t deadline, uint32_t *ticks, bool *due)
{
    if (!ticks || !due || clock_state != KM_CLOCK_STATE_READY) return KM_ERR_UNAVAILABLE;
    *due = !km_time_before(now_us, deadline);
    *ticks = *due ? now_us : deadline;
    return KM_OK;
}

int gpio_local_reserve(const uint32_t *list, uint32_t count)
{
    assert(list && count && count <= KM_RESOURCE_MAX_CLAIMS);
    memcpy(reserved_pins, list, count * sizeof(*list));
    reserved_count = count;
    reserve_calls++;
    return GPIO_LOCAL_OK;
}
int gpio_pin_is_local(uint32_t pin)
{
    for (uint32_t i = 0; i < reserved_count; ++i)
        if (reserved_pins[i] == pin) return 1;
    return 0;
}
struct gpio_in gpio_in_setup_local(uint32_t pin, int32_t pull_up)
{
    (void)pull_up;
    return (struct gpio_in){.encoded = pin};
}
struct gpio_in gpio_in_setup(uint32_t pin, int32_t pull_up)
{
    (void)pull_up;
    return (struct gpio_in){.encoded = pin};
}
uint8_t gpio_in_read(struct gpio_in in) { return input_levels[in.encoded]; }

/* ---- Selector kind's own gate/servo pulse train: a scheduled software PWM
 * output would need real timing; this test only needs the last pulse width
 * a selection asked for, so it is mocked the same way tests/test_selector_
 * core.c's fake_servo does, not compiled from mcu/servo_pwm.c. ---- */
static km_servo_t mock_servo;
static bool servo_ever_pulsed; /* The dwell's own timeout may stop the pulse
                                * train again once engaged (servo_hold off,
                                * the default), so only "was it ever nonzero"
                                * proves a real selection pulse happened. */
static km_result_t mock_servo_set_pulse(km_servo_t *self, uint16_t pulse_us)
{
    (void)self;
    if (pulse_us) servo_ever_pulsed = true;
    return KM_OK;
}
km_result_t km_servo_pwm_setup(uint32_t pin, km_servo_t **out)
{
    (void)pin;
    mock_servo.set_pulse_us = mock_servo_set_pulse;
    mock_servo.user_data = NULL;
    *out = &mock_servo;
    return KM_OK;
}
bool km_servo_pwm_is_slot_active(uint32_t slot) { (void)slot; return true; }
void km_servo_pwm_shutdown(void) {}

/* ---- TMC feeder mock: slot 0 is the shared gear, slot 1 the selector's own
 * axis (mcu/board_startup.c: physical_feeders==1 with a selector, then
 * slot 1 when selector_uses_tmc_uart()) -- one slot-indexed mock covers
 * both, exactly like the gear/rewind slots in tests/klipper_emu_runtime's
 * own fixture. Readiness is a plain flag: no real single-wire UART timing
 * runs natively, so there is nothing to wait on. ---- */
static mock_driver_t feeders[KM_MAX_LANES];
static bool lane_ready[KM_MAX_LANES];

/* Claymore's own per-lane powered rewind (profile.has_powered_rewind; the
 * only selector device with one, docs/selectors.md). board_startup.c binds
 * one of these per lane exactly like the feeder slots above; neither an
 * explicit HOME nor landing a plain selection ever runs UNLOAD/EJECT/PRELOAD
 * (the only ops that drive a rewinder), so presence is everything this
 * fixture needs -- the mocks below are the same trivial link-target shape
 * tests/klipper_board_startup/test_boxturtle_boot.c uses when a scenario
 * needs no rewind readiness. */
static mock_driver_t rewinds[KM_MAX_LANES];

/* Every selector row in the release matrix leaves KLIPPER_MMU_SELECTOR_UART
 * on by default (docs/selectors.md), so the plain STEP/DIR selector branch
 * below is compiled but never reached at runtime; it still needs a link
 * target, same as EMU/NightOwl's own rewinder mocks for code their images
 * never enable either. */
km_result_t km_stepdir_gpio_setup(uint32_t slot, uint32_t step_pin, uint32_t dir_pin,
                                  uint32_t enable_pin, const km_step_dir_config_t *config,
                                  bool (*ready)(void *context), km_fault_t (*fault)(void *context),
                                  void *context)
{
    (void)slot; (void)step_pin; (void)dir_pin; (void)enable_pin; (void)config;
    (void)ready; (void)fault; (void)context;
    return KM_ERR_NOT_SUPPORTED;
}
km_driver_t *km_stepdir_gpio_get_driver(uint32_t slot) { (void)slot; return NULL; }
km_result_t km_stepdir_gpio_get_slot_pins(uint32_t slot, uint32_t *step, uint32_t *dir, uint32_t *enable)
{
    (void)slot; (void)step; (void)dir; (void)enable;
    return KM_ERR_UNAVAILABLE;
}
bool km_stepdir_gpio_is_slot_active(uint32_t slot) { (void)slot; return false; }
bool km_stepdir_gpio_is_shutdown(void) { return false; }
void km_stepdir_gpio_shutdown(void) {}

/* config_km_selector_gate's host-calibration receiver (mcu/config.c) uses
 * Klipper's ordinary oid_alloc registration idiom; this fixture never issues
 * that command, so the return value is never read. */
void *oid_alloc(uint8_t oid, oid_type type, size_t size)
{
    (void)oid; (void)type; (void)size;
    return NULL;
}
km_result_t km_tmc_uart_gpio_get_pin(uint32_t slot, uint32_t *pin)
{
    (void)slot; (void)pin;
    return KM_ERR_UNAVAILABLE;
}
km_result_t km_tmc_uart_gpio_get_status(uint32_t slot, km_tmc_uart_status_t *out_status)
{
    (void)slot;
    if (!out_status) return KM_ERR_INVALID_PARAM;
    *out_status = (km_tmc_uart_status_t){0};
    return KM_ERR_UNAVAILABLE;
}
bool km_tmc_uart_gpio_is_slot_active(uint32_t slot) { (void)slot; return false; }
bool km_tmc_uart_gpio_is_shutdown(void) { return false; }
void km_tmc_uart_gpio_shutdown(void) {}
km_result_t km_tmc_feeder_setup(uint32_t slot, uint32_t step_pin, uint32_t dir_pin,
                                uint32_t enable_pin, uint32_t uart_pin, bool uart_pull_up,
                                const km_step_dir_config_t *step_config,
                                const km_tmc2209_config_t *tmc_config,
                                uint16_t expected_run_ma, uint16_t expected_hold_ma)
{
    (void)step_pin; (void)dir_pin; (void)enable_pin; (void)uart_pin; (void)uart_pull_up;
    (void)step_config; (void)tmc_config; (void)expected_run_ma; (void)expected_hold_ma;
    assert(slot < KM_MAX_LANES);
    return KM_OK;
}
km_result_t km_tmc_feeder_activate(uint32_t slot)
{
    if (slot >= KM_MAX_LANES) return KM_ERR_INVALID_PARAM;
    return lane_ready[slot] ? KM_OK : KM_ERR_UNAVAILABLE;
}
km_driver_t *km_tmc_feeder_get_driver(uint32_t slot)
{
    return slot < KM_MAX_LANES ? &feeders[slot].base : NULL;
}
km_result_t km_tmc_feeder_restart(uint32_t slot) { (void)slot; return KM_ERR_NOT_SUPPORTED; }

/* Claymore's powered-rewind bridge (rewinds[] above). board_startup.c passes
 * NULL/0 for complete_claims/claim_count here (mcu/diagnostic_startup.c always
 * calls km_board_startup_begin(NULL, 0, ...)), the same as every other setup
 * call in this fixture. */
TIM_TypeDef km_test_tim1, km_test_tim2, km_test_tim3, km_test_tim4;
TIM_TypeDef km_test_tim5, km_test_tim8, km_test_tim12, km_test_tim13;
TIM_TypeDef km_test_tim14, km_test_tim15, km_test_tim16, km_test_tim17;

/* h723_resources.c's own hardware PWM route/pair lookup (CONFIG_WANT_HARD_
 * PWM_QUERY, CONFIG_WANT_HARD_PWM_LOCAL_PAIR -- on for Claymore's powered
 * rewind, mcu/Kconfig KLIPPER_MMU_SELECTOR_BOOT_GATES). Same AFC Lite
 * rewind-pair routes as tests/klipper_board_startup/test_boxturtle_boot.c's
 * own table: TIM2 PA0/PA1, TIM3 PA6/PA7, TIM12 PB14/PB15, TIM4 PD12/PD13 --
 * Claymore's own release-matrix rewind pins reuse this exact validated set. */
int gpio_pwm_get_route(uint32_t pin, struct gpio_pwm_route *out)
{
    struct { uint32_t pin; TIM_TypeDef *timer; uint8_t channel; } table[] = {
        {0u, TIM2, 1}, {1u, TIM2, 2},
        {6u, TIM3, 1}, {7u, TIM3, 2},
        {30u, TIM12, 1}, {31u, TIM12, 2},
        {60u, TIM4, 1}, {61u, TIM4, 2},
    };
    if (!out) return 0;
    for (size_t i = 0; i < sizeof(table) / sizeof(table[0]); ++i) {
        if (table[i].pin != pin) continue;
        out->timer_id = (uintptr_t)table[i].timer;
        out->channel = table[i].channel;
        return 1;
    }
    return 0;
}
int gpio_pwm_pair_validate_pins(uint32_t forward_pin, uint32_t reverse_pin)
{
    struct gpio_pwm_route forward, reverse;
    if (forward_pin == reverse_pin) return GPIO_PWM_PAIR_ERR_INVALID;
    if (!gpio_pwm_get_route(forward_pin, &forward) || !gpio_pwm_get_route(reverse_pin, &reverse))
        return GPIO_PWM_PAIR_ERR_NOT_SUPPORTED;
    if (forward.timer_id != reverse.timer_id || forward.channel == reverse.channel)
        return GPIO_PWM_PAIR_ERR_INVALID;
    return GPIO_PWM_PAIR_OK;
}

km_result_t km_pwm_bridge_gpio_get_slot_pins(uint32_t slot, uint32_t *forward,
                                             uint32_t *reverse, uint32_t *enable)
{
    (void)slot; (void)forward; (void)reverse; (void)enable;
    return KM_ERR_UNAVAILABLE;
}
bool km_pwm_bridge_gpio_is_slot_active(uint32_t slot) { (void)slot; return false; }
bool km_pwm_bridge_gpio_is_shutdown(void) { return false; }
void km_pwm_bridge_gpio_service(uint32_t slot) { (void)slot; }
km_result_t km_pwm_bridge_gpio_setup(uint32_t slot, const km_resource_claim_t *claims, size_t count,
                                     bool (*ready)(void *context), km_fault_t (*fault)(void *context),
                                     void *context)
{
    (void)ready; (void)fault; (void)context;
    assert(slot < KM_MAX_LANES);
    assert(claims == NULL && count == 0);
    return KM_OK;
}
km_driver_t *km_pwm_bridge_gpio_get_driver(uint32_t slot)
{
    return slot < KM_MAX_LANES ? &rewinds[slot].base : NULL;
}
void km_pwm_bridge_gpio_shutdown(void) {}

/* No ADC/analog buffer on a switched-buffer selector image. */
km_result_t km_adc_get_sample_at(km_adc_pair_sample_t *sample, km_core_timepoint_t *query_time)
{
    (void)sample; (void)query_time;
    return KM_ERR_UNAVAILABLE;
}

/* Diagnostic startup's own controller attachment is exercised through the
 * production command layer below (km_commands_attach). The attach stub keeps
 * the sample callback so advance() runs exactly what the controller would. */
bool km_controller_is_attached(void) { return false; }
bool km_controller_is_shutdown(void) { return false; }
void km_controller_changed(void) {}
static km_controller_sample_fn attached_sample;
static void *attached_context;
km_result_t km_controller_attach(km_operation_core_t *core, km_controller_sample_fn sample, void *context)
{
    (void)core;
    attached_sample = sample;
    attached_context = context;
    return KM_OK;
}
void km_controller_shutdown(void) {}
void km_controller_note_switch_sample(km_operation_core_t *core, uint32_t sample_us)
{ (void)core; (void)sample_us; }
bool km_controller_get_switch_sample_delay(uint32_t *out_us) { (void)out_us; return false; }
void km_controller_get_switch_sample_stats(uint32_t *count, uint32_t *max_us)
{ if (count) *count = 0u; if (max_us) *max_us = 0u; }

static uint64_t test_boot_identity = UINT64_C(0x123456789abcdef0);
km_result_t km_boot_identity_get(uint64_t *out)
{
    if (!out) return KM_ERR_INVALID_PARAM;
    *out = test_boot_identity;
    return KM_OK;
}

/* ---- Production command dispatch (mcu/commands.c, mcu/diagnostic_startup.c):
 * not declared by any header included above (a real Klipper build wires
 * these through DECL_COMMAND at their own definition site). ---- */
void command_km_diagnostic_start(uint32_t *args);
void command_km_diagnostic_start_host(uint32_t *args);
void command_km_diagnostic_query(uint32_t *args);
void command_km_start(uint32_t *args);
void command_km_heartbeat(uint32_t *args);
void command_km_event_peek(uint32_t *args);
void command_km_event_ack(uint32_t *args);
void command_km_terminal_query(uint32_t *args);
void command_km_selector_gate_set(uint32_t *args);

static char last_send_format[256];
static uint32_t last_send_values[24];

void sendf(const char *format, ...)
{
    (void)snprintf(last_send_format, sizeof(last_send_format), "%s", format);
    if (strncmp(format, "km_", 3u) != 0) return;
    va_list args;
    va_start(args, format);
    size_t count = 0u;
    for (const char *p = format; *p; ++p)
        if (*p == '%') {
            assert(count < sizeof(last_send_values) / sizeof(last_send_values[0]));
            last_send_values[count++] = va_arg(args, uint32_t);
        }
    va_end(args);
}

static void issue_heartbeat(uint32_t session)
{
    uint32_t args[] = {(uint32_t)(test_boot_identity >> 32), (uint32_t)test_boot_identity, session};
    command_km_heartbeat(args);
}

/* Dispatches km_start and returns the admission result (km_admission_
 * result_t), from "km_admission ... admission=%c fault=%c". */
static uint32_t issue_start(uint32_t session, uint32_t request, uint8_t operation,
                            uint8_t lane, uint8_t direction)
{
    uint32_t args[] = {(uint32_t)(test_boot_identity >> 32), (uint32_t)test_boot_identity,
                       KM_CLIENT_HOST, session, request, operation, lane, direction};
    last_send_format[0] = '\0';
    command_km_start(args);
    assert(strcmp(last_send_format,
        "km_admission instance_hi=%u instance_lo=%u client=%c session=%u request=%u "
        "operation=%c lane=%c admission=%c fault=%c") == 0);
    return last_send_values[7];
}

/* Peeks and acks the retained terminal event, and returns its type. Mirrors
 * tests/klipper_emu_runtime's own emu_ack_terminal, trimmed to what this
 * fixture needs to prove a HOME or a selection actually finished. */
static uint8_t ack_terminal(km_operation_core_t *core, uint32_t session, uint32_t request,
                            uint8_t operation, uint8_t lane)
{
    uint32_t peek[] = {(uint32_t)(test_boot_identity >> 32), (uint32_t)test_boot_identity};
    command_km_event_peek(peek);
    km_event_t event;
    assert(km_event_queue_peek(&core->event_queue, &event) == KM_OK);
    assert(event.session_id == session && event.req_id == request
           && event.op_type == operation && event.lane_idx == lane);
    uint32_t ack[] = {(uint32_t)(test_boot_identity >> 32), (uint32_t)test_boot_identity,
        KM_CLIENT_HOST, session, request, operation, lane, event.seq};
    command_km_event_ack(ack);
    assert(strcmp(last_send_format,
        "km_command_result instance_hi=%u instance_lo=%u command=%c query=%u result=%c") == 0);
    assert(last_send_values[4] == KM_COMMAND_OK);
    return (uint8_t)event.type;
}

/* Runtime single-gate calibration write (mcu/commands.c command_km_selector_
 * gate_set): the same after-start path KLIPPER_MMU_CALIBRATE_SELECTOR/
 * KLIPPER_MMU_SERVO use, proven here for Pico MMU's own uncalibrated gates
 * (docs/selectors.md). value is degrees for SERVO, selector microsteps
 * (or -1) for LINEAR/ROTARY. */
static void set_selector_gate(uint32_t gate, int32_t value)
{
    uint32_t args[] = {(uint32_t)(test_boot_identity >> 32), (uint32_t)test_boot_identity,
                       gate, (uint32_t)value};
    last_send_format[0] = '\0';
    command_km_selector_gate_set(args);
    assert(strcmp(last_send_format,
        "km_selector_gate_set_result instance_hi=%u instance_lo=%u result=%c status=%c") == 0);
    assert(last_send_values[2] == KM_COMMAND_OK && last_send_values[3] == 0u);
}

/* One control-cadence tick: run the reference stepper, run the sample
 * callback board startup attached (switches, plus the encoder when the image
 * has one), then update the core. */
static void advance(km_operation_core_t *core, km_fake_clock_t *motion)
{
    now_us += 1000u;
    assert(km_fake_clock_run_trajectory(motion, &core->trajectory, now_us) == KM_OK);
    assert(attached_sample);
    attached_sample(core, attached_context);
    km_core_update(core, now_us / 1000u, now_us);
}

/* Boots the saved image, verifies the selector is bound, completes an
 * explicit HOME, then lands a selection on a lane other than gate 0/HOME's
 * own resting gate -- the whole point of this fixture. */
static void test_selector_hosted_boot_home_select(void)
{
    km_device_profile_t profile;
    assert(km_config_profile(&profile, NULL) == KM_OK);
    /* Claymore is the one selector device with per-lane BAY switches (its
     * own LOAD1-4 implementing this catalogue's BAY role, not LOAD) and
     * powered rewind (docs/selectors.md); every other selector row has
     * neither, so has_bay_sensors/has_powered_rewind are read from the
     * profile below rather than asserted false here. */
    assert(profile.has_selector && !profile.has_load_sensors && profile.has_hub_sensor);
    const bool has_stepper = profile.selector.kind == KM_SELECTOR_LINEAR
        || profile.selector.kind == KM_SELECTOR_ROTARY
        || profile.selector.kind == KM_SELECTOR_INDEXED;
    const bool has_servo = profile.selector.has_servo;

    mock_driver_init(&feeders[0], 2, 10, false);
    /* mock_driver_init's own fixed 20000 steps/s predates per-device dossier
     * speeds (mcu/Kconfig.control's KLIPPER_MMU_HH_MOTION_DEFAULTS): several
     * saved images now legitimately need more (ERCF's own 80 mm/s retract is
     * tens of thousands of steps/s at any real gearing). This mock drives no
     * real pulse timing -- motion here comes from the reference stepper via
     * km_fake_clock_run_trajectory -- so admit whatever the image's own
     * Kconfig-checked profile asks for, up to the one limit that is real:
     * the control core's own ceiling, already enforced at build time
     * (mcu/feeder_mechanics.h). */
    feeders[0].base.caps.max_velocity_steps_s = KM_MOTION_MAX_VELOCITY_STEPS_S;
    lane_ready[0] = true;
    if (has_stepper) {
        mock_driver_init(&feeders[1], 2, 10, false);
        feeders[1].base.caps.max_velocity_steps_s = KM_MOTION_MAX_VELOCITY_STEPS_S;
        lane_ready[1] = true;
    }
    if (profile.has_powered_rewind)
        for (uint8_t lane = 0; lane < profile.lane_count; ++lane)
            mock_driver_init(&rewinds[lane], 2, 10, false);

    km_pin_config_t pins;
    assert(km_config_pins(&pins, NULL) == KM_OK);
    memset(input_levels, 1, sizeof(input_levels)); /* Idle: every active-low switch open. */

    clock_state = KM_CLOCK_STATE_READY;
    now_us = 1000u;
#if CONFIG_KLIPPER_MMU_BOOT_IDENTITY_HOST
    uint32_t challenge[] = {(uint32_t)(test_boot_identity >> 32), (uint32_t)test_boot_identity};
    command_km_diagnostic_start_host(challenge);
#else
    uint32_t challenge[] = {0x4567u};
    command_km_diagnostic_start(challenge);
#endif
    for (unsigned attempt = 0u; attempt < 200u
         && km_diagnostic_get_state() == KM_DIAGNOSTIC_WAITING; ++attempt) {
        km_diagnostic_task();
        if (km_diagnostic_get_state() == KM_DIAGNOSTIC_WAITING && armed_timer)
            fire_timer();
    }
    assert(km_diagnostic_get_state() == KM_DIAGNOSTIC_READY);
    assert(km_diagnostic_get_result() == KM_OK);
    assert(km_board_startup_get_state() == KM_BOARD_STARTUP_READY);
    assert(reserve_calls == 1u && reserved_count == pins.count);

    km_operation_core_t *core = km_board_startup_get_core();
    assert(core && core->profile.lane_count == profile.lane_count);
    assert(core->feeder_drivers[0] == &feeders[0].base);
    assert(has_stepper == (core->selector_driver == &feeders[1].base));
    assert(has_servo == (core->selector_servo == &mock_servo));

    if (!CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN) {
        /* The safe image reaches READY and binds the selector, but never
         * grants START -- the same shape NightOwl and the passive four
         * candidates prove for their own safe images. */
        issue_heartbeat(1u);
        assert(strcmp(last_send_format,
            "km_command_result instance_hi=%u instance_lo=%u command=%c query=%u result=%c") == 0);
        uint32_t start[] = {(uint32_t)(test_boot_identity >> 32), (uint32_t)test_boot_identity,
            KM_CLIENT_HOST, 1u, 1u, KM_OP_TYPE_HOME, KM_LANE_UNKNOWN, KM_DIR_FORWARD};
        command_km_start(start);
        assert(strcmp(last_send_format,
            "km_command_result instance_hi=%u instance_lo=%u command=%c query=%u result=%c") == 0);
        assert(last_send_values[4] == KM_COMMAND_UNAVAILABLE);
        puts("PASS: selector safe image reached diagnostic READY with the selector bound "
             "and denied START");
        return;
    }

    km_fake_clock_t motion;
    km_fake_clock_init(&motion, now_us);
    issue_heartbeat(1u);
    /* HOME needs a fresh (sampled recently, km_selector_home_start's own
     * freshness check) endstop/gate-switch reading before it will admit;
     * a few idle ticks give switch_gpio.c's debounce that first sample,
     * the same qualification EMU's own hosted fixture needs before START. */
    for (unsigned i = 0u; i < 10u; ++i) advance(core, &motion);

    /* Explicit HOME (KM_OP_TYPE_HOME=8; owner decision, docs/selectors.md). */
    assert(issue_start(1u, 1u, KM_OP_TYPE_HOME, KM_LANE_UNKNOWN, KM_DIR_FORWARD) == KM_REQ_ACCEPTED);
    if (has_stepper) {
        assert(core->stage == KM_STAGE_SELECT_HOME);
        for (unsigned i = 0u; i < 2000u && core->stage != KM_STAGE_STOPPED; ++i) {
            /* Give the search a real head start before any switch can
             * assert: an immediately-asserted switch with no observed
             * motion is indistinguishable from a stuck one and is correctly
             * refused (src/selector_home.c's own anti-noise check). Gate on
             * the trajectory having actually moved a couple of steps, not a
             * fixed tick count -- HTLF's own selector accelerates far more
             * gently than every other cataloged device (50 mm/s^2 vs.
             * 1200), so a fixed "30 ticks in" head start that works for the
             * others still finds HTLF sitting at position 0. */
            if (core->trajectory.current_position_steps <= -2
                || core->trajectory.current_position_steps >= 2) {
                if (profile.selector.kind == KM_SELECTOR_INDEXED)
                    input_levels[pins.pin[KM_PIN_SELECTOR_GATE][0]] = 0u;
                else
                    input_levels[pins.pin[KM_PIN_SELECTOR_ENDSTOP][0]] = 0u;
            }
            advance(core, &motion);
        }
    } else {
        /* SERVO: no selector axis, so HOME completes synchronously with no
         * SELECT_HOME stage at all (docs/selectors.md; tests/test_selector_
         * core.c's own test_home_op_servo_kind). */
        assert(core->stage == KM_STAGE_STOPPED);
    }
    assert(core->selector_homed);
    assert(ack_terminal(core, 1u, 1u, KM_OP_TYPE_HOME, KM_LANE_UNKNOWN) == KM_EVENT_HOME_COMPLETE);

    /* One selection: PRELOAD's own admission additionally requires both a
     * BAY and a HUB sensor (src/operation_core.c km_core_request_preload),
     * which a selector machine's real wiring has neither/one of (Claymore:
     * one of two), so LOAD is the op that actually admits here -- it only
     * needs has_bay_sensors when the profile has one at all (selector.h;
     * Claymore is the only selector row that does). Stopping as soon as the
     * selection phase (SELECT_HOME/SELECT_MOVE) ends is "one selection
     * landed"; this test does not drive LOAD to completion. */
    uint8_t target_lane = profile.lane_count > 1u ? 1u : 0u;
    if (profile.selector.kind == KM_SELECTOR_INDEXED)
        input_levels[pins.pin[KM_PIN_SELECTOR_GATE][0]] = 1u; /* Re-idle gate 0's switch. */
    else if (has_stepper)
        input_levels[pins.pin[KM_PIN_SELECTOR_ENDSTOP][0]] = 1u;
    if (profile.has_bay_sensors) {
        /* LOAD's own admission (km_core_request_load) refuses without
         * filament at the target lane's BAY; a few ticks give switch_gpio.c's
         * debounce a fresh, settled reading, the same qualification HOME
         * needed for its own endstop/gate switch above. Claymore's own BAY
         * (its LOAD1-4) is wired active-high, unlike every other active-low
         * switch in this fixture (docs/selectors.md), so the raw level that
         * means "asserted" is read from the real switch config rather than
         * assumed to be 0. */
        km_switch_gpio_config_t switches;
        assert(km_config_switches(&switches) == KM_OK);
        input_levels[pins.pin[KM_PIN_BAY][target_lane]] =
            switches.bay[target_lane].active_high ? 1u : 0u;
        for (unsigned i = 0u; i < 10u; ++i) advance(core, &motion);
    }
    if (profile.selector.kind == KM_SELECTOR_SERVO && profile.selector.servo_gate_us[target_lane] == 0) {
        /* Pico MMU ships no gate angles (Happy Hare calibrates them instead;
         * docs/selectors.md): selecting an uncalibrated gate is refused.
         * Prove the refusal, then calibrate it with the runtime gate-set
         * path (mcu/commands.c command_km_selector_gate_set) and prove the
         * very same selection lands. */
        assert(issue_start(1u, 2u, KM_OP_TYPE_LOAD, target_lane, KM_DIR_FORWARD) == KM_REQ_UNAVAILABLE);
        set_selector_gate(target_lane, 90);
    }
    assert(issue_start(1u, 2u, KM_OP_TYPE_LOAD, target_lane, KM_DIR_FORWARD) == KM_REQ_ACCEPTED);
    for (unsigned i = 0u; i < 2000u
         && (core->stage == KM_STAGE_SELECT_HOME || core->stage == KM_STAGE_SELECT_MOVE
             || core->stage == KM_STAGE_SERVO_DWELL); ++i) {
        /* Same head start as HOME above: let the search move away from an
         * already-idle switch before the target's own switch can assert.
         * LINEAR's grip servo (has_servo) engages through SERVO_DWELL right
         * after SELECT_MOVE lands, before selector_engaged is set. Unlike
         * HOME's search (always toward gate 0, hence the position-based
         * gate above), a shortest-hop INDEXED selection may legitimately
         * pass other gates first; asserting a switch as soon as any motion
         * is seen can trip the wrong one before the real target is reached,
         * so this one keeps the fixed tick-based head start. */
        if (i >= 30u && profile.selector.kind == KM_SELECTOR_INDEXED)
            input_levels[pins.pin[KM_PIN_SELECTOR_GATE][target_lane]] = 0u;
        advance(core, &motion);
    }
    assert(core->selector_gate == target_lane && core->selector_engaged);
    assert(!has_servo || servo_ever_pulsed); /* The grip/gate servo was actually pulsed. */
    assert(!core->system_fault);
    puts("PASS: selector image reached diagnostic READY with the selector bound, HOME "
         "completed, and one selection landed");
}

int main(void)
{
    test_selector_hosted_boot_home_select();
    return 0;
}
