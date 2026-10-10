// SPDX-License-Identifier: GPL-3.0-or-later
/* Reuse the board fixture's peripheral/configuration simulator. The four
 * production orchestration modules below are linked without substitutions. */
#define km_controller_attach fixture_controller_attach
#define km_controller_shutdown fixture_controller_shutdown
#define km_controller_is_attached fixture_controller_is_attached
#define km_controller_is_shutdown fixture_controller_is_shutdown
#define km_pwm_bridge_gpio_get_slot_pins fixture_pwm_get_slot_pins
#define km_pwm_bridge_gpio_is_slot_active fixture_pwm_is_slot_active
#define km_pwm_bridge_gpio_is_shutdown fixture_pwm_is_shutdown
#define km_pwm_bridge_gpio_setup fixture_pwm_setup
#define km_pwm_bridge_gpio_get_driver fixture_pwm_get_driver
#define km_pwm_bridge_gpio_service fixture_pwm_service
#define km_pwm_bridge_gpio_force_off fixture_pwm_force_off
#define km_pwm_bridge_gpio_shutdown fixture_pwm_shutdown
#define km_config_h723_resources fixture_config_h723_resources
#define gpio_local_reserve fixture_gpio_local_reserve
#define timer_read_time fixture_timer_read_time
#define timer_from_us fixture_timer_from_us
#define sched_wake_task fixture_sched_wake_task
#define sched_check_wake fixture_sched_check_wake
#define sched_add_timer fixture_sched_add_timer
#define sched_del_timer fixture_sched_del_timer
#define main board_fixture_main
#include "../klipper_board_startup/test_board_startup.c"
#undef main
#undef km_controller_attach
#undef km_controller_shutdown
#undef km_controller_is_attached
#undef km_controller_is_shutdown
#undef km_pwm_bridge_gpio_get_slot_pins
#undef km_pwm_bridge_gpio_is_slot_active
#undef km_pwm_bridge_gpio_is_shutdown
#undef km_pwm_bridge_gpio_setup
#undef km_pwm_bridge_gpio_get_driver
#undef km_pwm_bridge_gpio_service
#undef km_pwm_bridge_gpio_force_off
#undef km_pwm_bridge_gpio_shutdown
#undef km_config_h723_resources
#undef gpio_local_reserve
#undef timer_read_time
#undef timer_from_us
#undef sched_wake_task
#undef sched_check_wake
#undef sched_add_timer
#undef sched_del_timer
#undef KLIPPER_MMU_MCU_PWM_BRIDGE_GPIO_H
#include "pwm_bridge_gpio.h"
#include "diagnostic_startup.h"
#include "commands.h"
#include "boot_identity.h"
#include "calibration.h"
#include "messages.h"
#include "board/gpio.h"
#include "stm32/pwm.h"
#include <stdarg.h>
void command_km_transport_query(uint32_t *args);
bool km_controller_is_attached(void);

static uint8_t local_pins[128], enable_levels[128];
static unsigned pair_setup_count, pair_raw_count, pair_update_count;
static int pair_busy_lane = -1, pair_fault_lane = -1;
struct gpio_pwm_pair { bool active; uint32_t forward, reverse; } pairs[KM_MAX_LANES];
km_result_t km_config_h723_resources(km_h723_resource_plan_t *out,
                                     km_resource_diagnostic_t *diagnostic) {
    /* Saved fixture plan supplies routes; model the planner's whole-timer rule. */
    if (sim.profile.has_powered_rewind &&
        sim.expected_plan.rewind[0][0].timer == sim.expected_plan.rewind[1][0].timer)
        return KM_ERR_CONFLICT;
    return fixture_config_h723_resources(out, diagnostic);
}
int gpio_local_reserve(const uint32_t *pins, uint32_t count) {
    assert(pins && count);
    for (uint32_t i = 0; i < count; ++i) { assert(pins[i] < 128u); local_pins[pins[i]] = 1; }
    return 0;
}
int gpio_pin_is_local(uint32_t pin) { return pin < 128u && local_pins[pin]; }
struct gpio_out gpio_out_setup(uint32_t pin, uint32_t value) {
    assert(pin < 128u); enable_levels[pin] = (uint8_t)value; return (struct gpio_out){pin};
}
struct gpio_out gpio_out_setup_local(uint32_t pin, uint32_t value) {
    assert(gpio_pin_is_local(pin)); return gpio_out_setup(pin, value);
}
void gpio_out_write(struct gpio_out out, uint32_t value) {
    assert(out.pin < 128u); enable_levels[out.pin] = (uint8_t)value;
}
km_result_t km_clock_get_completion_us(uint32_t *out) {
    if (!out || sim.clock_state != KM_CLOCK_STATE_READY) return KM_ERR_FAULTED;
    *out = sim.now_us; return KM_OK;
}
int gpio_pwm_pair_setup(const struct gpio_pwm_pair_config *cfg, struct gpio_pwm_pair **out) {
    assert(cfg && out && cfg->raw_force_off && cfg->forward_pin < 128u);
    unsigned lane = cfg->forward_pin == 30u ? 0u : 1u;
    pair_setup_count++;
    cfg->raw_force_off(cfg->raw_force_off_context);
    if ((int)lane == pair_busy_lane) return GPIO_PWM_PAIR_ERR_BUSY;
    pairs[lane].active = true; pairs[lane].forward = pairs[lane].reverse = 0;
    *out = &pairs[lane]; return GPIO_PWM_PAIR_OK;
}
int gpio_pwm_pair_update(struct gpio_pwm_pair *pair, uint32_t forward, uint32_t reverse) {
    assert(pair && pair >= pairs && pair < pairs + KM_MAX_LANES);
    unsigned lane = (unsigned)(pair - pairs); pair_update_count++;
    if ((int)lane == pair_fault_lane) return GPIO_PWM_PAIR_ERR_HARDWARE;
    if (!pair->active || (forward && reverse)) return GPIO_PWM_PAIR_ERR_INVALID;
    pair->forward = forward; pair->reverse = reverse; return GPIO_PWM_PAIR_OK;
}
void gpio_pwm_pair_raw_disable(struct gpio_pwm_pair *pair) {
    assert(pair); pair_raw_count++; pair->active = false;
    pair->forward = pair->reverse = 0;
}
int gpio_pwm_pair_is_ready(const struct gpio_pwm_pair *pair) { return pair && pair->active; }
int gpio_pwm_pair_get_info(const struct gpio_pwm_pair *pair, struct gpio_pwm_pair_info *out) {
    if (!pair || !out) return GPIO_PWM_PAIR_ERR_INVALID;
    *out = (struct gpio_pwm_pair_info){pair->forward, pair->reverse, pair->active};
    return GPIO_PWM_PAIR_OK;
}
void gpio_pwm_pair_shutdown(void) {
    for (unsigned i = 0; i < KM_MAX_LANES; ++i) gpio_pwm_pair_raw_disable(&pairs[i]);
}
static struct timer *timers[4];
static size_t timer_count;
static uint32_t last_transport_state, last_transport_instance_low;
static uint32_t last_command_result;
static uint32_t last_diag_state;
static int32_t last_diag_result;

uint32_t timer_read_time(void) { return sim.now_us; }
uint32_t timer_from_us(uint32_t us) { return us; }
void sched_add_timer(struct timer *t) {
    assert(timer_count < 4);
    for (size_t i = 0; i < timer_count; ++i) assert(timers[i] != t);
    timers[timer_count++] = t;
}
void sched_del_timer(struct timer *t) {
    for (size_t i = 0; i < timer_count; ++i) if (timers[i] == t) {
        timers[i] = timers[--timer_count]; return;
    }
}
void sched_wake_task(struct task_wake *w) { w->wake = 1; }
uint8_t sched_check_wake(struct task_wake *w) { uint8_t v = w->wake; w->wake = 0; return v; }
km_result_t km_clock_deadline_ticks(uint32_t us, uint32_t *ticks, bool *due) {
    *ticks = us; *due = (int32_t)(us - sim.now_us) <= 0; return KM_OK;
}
km_result_t km_boot_identity_get(uint64_t *id) { *id = 0x1234; return KM_OK; }
const km_diff_adc_config_t *km_diff_calibration_get(void) {
    return sim.diff_calibration_provided ? &sim.calibration : NULL;
}
void sendf(const char *fmt, ...) {
    va_list a; va_start(a, fmt);
    if (strncmp(fmt, "km_diagnostic_state ", 20) == 0) {
        (void)va_arg(a, uint32_t);
        last_diag_state = va_arg(a, uint32_t);
        last_diag_result = va_arg(a, int);
    } else if (strncmp(fmt, "km_transport_state ", 19) == 0) {
        (void)va_arg(a, uint32_t);
        (void)va_arg(a, uint32_t);
        last_transport_instance_low = va_arg(a, uint32_t);
        (void)va_arg(a, uint32_t);
        last_transport_state = va_arg(a, uint32_t);
    } else if (strncmp(fmt, "km_command_result ", 18) == 0) {
        (void)va_arg(a, uint32_t); (void)va_arg(a, uint32_t);
        (void)va_arg(a, uint32_t); (void)va_arg(a, uint32_t);
        last_command_result = va_arg(a, uint32_t);
    }
    va_end(a);
}
static void fire_next_timer(void) {
    assert(timer_count);
    size_t k = 0;
    for (size_t i = 1; i < timer_count; ++i)
        if ((int32_t)(timers[i]->waketime - timers[k]->waketime) < 0) k = i;
    struct timer *t = timers[k];
    if ((int32_t)(sim.now_us - t->waketime) < 0) sim.now_us = t->waketime;
    uint_fast8_t rc = t->func(t);
    if (rc == SF_DONE) sched_del_timer(t);
    else assert(rc == SF_RESCHEDULE);
}
static void observe_transport(uint32_t expected) {
    uint32_t args[1] = {9};
    command_km_transport_query(args);
    assert(last_transport_state == expected);
    if (expected == 1) assert(last_transport_instance_low == 0x1234);
}
static void observe_diag(enum km_diagnostic_state expected, km_result_t result) {
    uint32_t args[1] = {8};
    command_km_diagnostic_query(args);
    if (!(last_diag_state == (uint32_t)expected && last_diag_result == result)) fprintf(stderr, "diag got %u %d expected %u %d board %d %d\n", last_diag_state, last_diag_result, (unsigned)expected, result, km_board_startup_get_state(), km_board_startup_get_result());
    assert(last_diag_state == (uint32_t)expected && last_diag_result == result);
}
void command_km_start(uint32_t *args);
static void check_start(enum km_command_result expected) {
    uint32_t args[8] = {0, 0x1234, KM_CLIENT_HOST, 1, 1,
                        KM_OP_TYPE_LOAD, 0, KM_DIR_FORWARD};
    command_km_start(args);
    assert(last_command_result == (uint32_t)expected);
}
static void configure_powered_rewind(void) {
    sim.profile.has_powered_rewind = true;
    sim.profile.rewind_pwm_duty_permille = 500;
    sim.rewind_limits = (km_pwm_bridge_config_t){800, 10, 100};
    for (unsigned lane = 0; lane < 2; ++lane) {
        uint32_t base = lane ? 40u : 30u;
        sim.expected_plan.rewind[lane][0] = (km_h723_pwm_route_t){(uint8_t)(lane + 2u), 1};
        sim.expected_plan.rewind[lane][1] = (km_h723_pwm_route_t){(uint8_t)(lane + 2u), 3};
        sim.pins.pin[KM_PIN_REWIND_FORWARD][lane] = (uint16_t)base;
        sim.pins.pin[KM_PIN_REWIND_REVERSE][lane] = (uint16_t)(base + 1u);
        sim.pins.pin[KM_PIN_REWIND_ENABLE][lane] = (uint16_t)(base + 2u);
        sim.rewind_outputs[lane] = (km_rewind_output_config_t){100, true, true, true};
        const enum km_pin_role roles[3] = {
            KM_PIN_REWIND_FORWARD, KM_PIN_REWIND_REVERSE, KM_PIN_REWIND_ENABLE
        };
        for (unsigned i = 0; i < 3; ++i)
            sim.expected_plan.claims[sim.expected_plan.count++] = (km_resource_claim_t){
                .kind=KM_RESOURCE_PIN, .instance=(uint16_t)(base + i), .owner=(uint16_t)roles[i]
            };
    }
}
int main(int argc, char **argv) {
    assert(argc == 2);
    reset_sim();
    sim.profile.lane_count = 2;
    sim.lane_status[0] = sim.lane_status[1] = LANE_STATUS_UNAVAILABLE;
    sim.profile.has_proportional_buffer = true;
    sim.profile.proportional_gain_milli = 8000;
    sim.profile.buffer_deadband_permille = 10;
    sim.calibration = (km_diff_adc_config_t){
        .channel_map = KM_DIFF_CH_A_LOW_B_HIGH, .vref_mv = 3300,
        .adc_max_counts = 4095, .path_tolerance_counts = 32, .point_count = 3,
        .points = {{0,2048,0}, {2048,2048,500}, {2048,4095,1000}}
    };
    sim.pins.pin[KM_PIN_BUFFER_A][0] = 31;
    sim.pins.pin[KM_PIN_BUFFER_B][0] = 32;
    sim.diff_calibration_provided = true;
    bool powered = strncmp(argv[1], "powered_", 8) == 0;
    if (powered) configure_powered_rewind();
    if (strcmp(argv[1], "powered_timer_conflict") == 0) {
        sim.expected_plan.rewind[1][0].timer = 2;
        sim.expected_plan.rewind[1][1].timer = 2;
    }
    if (strcmp(argv[1], "powered_conflict") == 0) pair_busy_lane = 0;
    if (strcmp(argv[1], "powered_partial") == 0) pair_busy_lane = 1;
    observe_transport(0);
    check_start(KM_COMMAND_UNBOUND);
    if (strcmp(argv[1], "missing_calibration") == 0) {
        sim.profile.has_proportional_buffer = true;
        sim.diff_calibration_provided = false;
    }
    command_km_diagnostic_start(NULL);
    km_diagnostic_task();
    if (strcmp(argv[1], "powered_timer_conflict") == 0) {
        observe_diag(KM_DIAGNOSTIC_FAULTED, KM_ERR_CONFLICT);
        assert(!km_controller_is_attached() && pair_setup_count == 0 && pair_raw_count == 0);
        assert(sim.switch_setup_count == 0 && sim.feeder_setup_count == 0);
        assert(!enable_levels[32] && !enable_levels[42]);
        observe_transport(0);
        puts("ok"); return 0;
    }
    if (strcmp(argv[1], "powered_conflict") == 0 ||
        strcmp(argv[1], "powered_partial") == 0) {
        observe_diag(KM_DIAGNOSTIC_FAULTED, KM_ERR_CONFLICT);
        assert(!km_controller_is_attached());
        assert(pair_setup_count == (strcmp(argv[1], "powered_partial") == 0 ? 2u : 1u));
        assert(pair_raw_count > 0 && !pairs[0].active && !pairs[1].active);
        assert(!enable_levels[32] && !enable_levels[42]);
        observe_transport(0);
        puts("ok"); return 0;
    }
    if (strcmp(argv[1], "missing_calibration") == 0) {
        observe_diag(KM_DIAGNOSTIC_FAULTED, KM_ERR_FAULTED);
        assert(sim.switch_setup_count == 0 && timer_count == 0);
        observe_transport(0);
    } else {
        observe_diag(KM_DIAGNOSTIC_WAITING, KM_ERR_UNAVAILABLE);
        assert(km_board_startup_get_state() == KM_BOARD_STARTUP_WAITING);
        assert(sim.feeder_setup_count == 2 && sim.feeder_activate_count >= 2);
        observe_transport(0);
        if (strcmp(argv[1], "ready") == 0 ||
            strcmp(argv[1], "powered_ready") == 0 ||
            strcmp(argv[1], "powered_fault") == 0) {
            if (powered) {
                assert(pair_setup_count == 2 && pairs[0].active && pairs[1].active);
                assert(!enable_levels[32] && !enable_levels[42]);
                assert(!km_pwm_bridge_gpio_get_driver(0)->enabled);
            }
            sim.lane_status[0] = sim.lane_status[1] = LANE_STATUS_READY;
            fire_next_timer(); km_diagnostic_task();
            assert(km_board_startup_get_state() == KM_BOARD_STARTUP_READY);
            assert(km_controller_is_attached());
            observe_diag(KM_DIAGNOSTIC_READY, KM_OK);
            observe_transport(1);
            if (strcmp(argv[1], "powered_fault") == 0) {
                km_driver_t *d = km_pwm_bridge_gpio_get_driver(0);
                assert(d && km_driver_set_enable(d, true) == KM_OK);
                pair_fault_lane = 0;
                assert(km_driver_set_pwm_duty(d, 500) == KM_ERR_FAULTED);
                assert(!enable_levels[32] && !pairs[0].active);
                assert(km_pwm_bridge_gpio_get_driver(0) == NULL);
                shutdown("test shutdown");
                assert(!pairs[1].active && !enable_levels[42]);
                puts("ok"); return 0;
            }
            check_start(KM_COMMAND_UNAVAILABLE);
            shutdown("test shutdown");
            assert(sim.switch_shutdown_count && sim.stepdir_shutdown_count);
            if (powered) assert(!pairs[0].active && !pairs[1].active && !enable_levels[32] && !enable_levels[42]);
            observe_transport(2);
        } else if (strcmp(argv[1], "fault") == 0) {
            sim.lane_status[0] = LANE_STATUS_FAULTED;
            fire_next_timer(); km_diagnostic_task();
            observe_diag(KM_DIAGNOSTIC_FAULTED, KM_ERR_FAULTED);
            assert(sim.switch_shutdown_count && sim.stepdir_shutdown_count);
            observe_transport(0);
        } else if (strcmp(argv[1], "timeout") == 0) {
            sim.now_us += 1000000;
            fire_next_timer(); km_diagnostic_task();
            observe_diag(KM_DIAGNOSTIC_FAULTED, KM_ERR_TIMEOUT);
            assert(sim.switch_shutdown_count && sim.stepdir_shutdown_count);
            observe_transport(0);
        } else return 2;
    }
    puts("ok");
    return 0;
}
