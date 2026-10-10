// SPDX-License-Identifier: GPL-3.0-or-later
#include "board_startup.h"
#include "autoconf.h"
#include "clock.h"
#include "config.h"
#include "pin_config.h"
#include "h723_resources.h"
#include "feeder_config.h"
#include "tmc_config.h"
#include "switch_config.h"
#include "rewind_config.h"
#include "switch_gpio.h"
#include "stepdir_gpio.h"
#include "servo_pwm.h"
#include "sensors.h"
#include "encoder.h"
#ifndef KM_ENCODER_BUILD
#define KM_ENCODER_BUILD 1
#endif
#include "controller.h"
#include "board/irq.h"
#include "command.h"
#include "sched.h"
#include <string.h>

/* Saved headers from before the selector add-on have neither. */
#ifndef CONFIG_KLIPPER_MMU_SELECTOR_STEPPER
#define CONFIG_KLIPPER_MMU_SELECTOR_STEPPER 0
#endif
#ifndef CONFIG_KLIPPER_MMU_SERVO
#define CONFIG_KLIPPER_MMU_SERVO 0
#endif
#ifndef CONFIG_KLIPPER_MMU_PINS_SELECTOR_UART
#define CONFIG_KLIPPER_MMU_PINS_SELECTOR_UART ""
#endif
#ifndef CONFIG_KLIPPER_MMU_SELECTOR_ENDSTOP_ACTIVE_HIGH
#define CONFIG_KLIPPER_MMU_SELECTOR_ENDSTOP_ACTIVE_HIGH (-1)
#endif
#ifndef CONFIG_KLIPPER_MMU_SELECTOR_ENDSTOP_PULL
#define CONFIG_KLIPPER_MMU_SELECTOR_ENDSTOP_PULL (-2)
#endif
#ifndef CONFIG_KLIPPER_MMU_SELECTOR_RUN_CURRENT
#define CONFIG_KLIPPER_MMU_SELECTOR_RUN_CURRENT 0
#endif
#ifndef CONFIG_KLIPPER_MMU_SELECTOR_HOLD_CURRENT
#define CONFIG_KLIPPER_MMU_SELECTOR_HOLD_CURRENT 0
#endif

#if defined(CONFIG_WANT_ADC) && CONFIG_WANT_ADC \
    && defined(CONFIG_WANT_ADC_LOCAL) && CONFIG_WANT_ADC_LOCAL
#include "adc.h"
#include "adc_config.h"
#define KM_HAS_ADC_SUPPORT 1
#else
#define KM_HAS_ADC_SUPPORT 0
#endif

#if defined(CONFIG_WANT_HARD_PWM_LOCAL_PAIR) && CONFIG_WANT_HARD_PWM_LOCAL_PAIR
#include "pwm_bridge_gpio.h"
#define KM_HAS_PWM_SUPPORT 1
#else
#define KM_HAS_PWM_SUPPORT 0
#endif

#if defined(CONFIG_WANT_TMCUART_LOCAL) && CONFIG_WANT_TMCUART_LOCAL && \
    defined(CONFIG_KLIPPER_MMU_TMC_UART_SHARED) && CONFIG_KLIPPER_MMU_TMC_UART_SHARED
#include "tmc_uart_gpio.h"
#include "tmc_feeder.h"
#define KM_HAS_TMC_UART_SUPPORT 1
#else
#define KM_HAS_TMC_UART_SUPPORT 0
#endif

#if defined(CONFIG_WANT_GPIO_LOCAL) && CONFIG_WANT_GPIO_LOCAL
#include "stm32/gpio_local.h"
#define KM_HAS_GPIO_LOCAL 1
#else
#define KM_HAS_GPIO_LOCAL 0
#endif

/* Board startup lifecycle state */
static enum km_board_startup_state startup_state = KM_BOARD_STARTUP_UNINITIALIZED;
static km_result_t startup_result = KM_ERR_UNAVAILABLE;
static bool startup_faulted = false;
static bool physical_mutation_started = false;

/* Ownership and mutation tracking for clock-free safe-off */
static bool switch_owned = false;
static bool encoder_owned = false;
#if KM_HAS_ADC_SUPPORT
static bool adc_started = false;
#endif
static bool stepdir_touched = false;
#if KM_HAS_TMC_UART_SUPPORT
static bool tmc_uart_touched = false;
#endif
#if KM_HAS_PWM_SUPPORT
static bool pwm_touched = false;
#endif
static bool controller_attached = false;
static bool selector_stepdir_touched = false;
#if CONFIG_KLIPPER_MMU_SERVO
static bool selector_servo_touched = false;
#endif

static uint32_t start_us = 0;
static uint32_t startup_timeout_us = 0;
static size_t configured_lanes = 0;
static bool lane_confirmed[KM_MAX_LANES] = {false};

/* Static scratch storage to avoid MCU stack overflow */
static km_operation_core_t board_core;
static km_device_profile_t board_profile;
static const km_pin_config_t *board_pins;
static km_h723_resource_plan_t board_expected_plan;
static km_step_dir_config_t board_feeders[KM_MAX_LANES];
#if KM_HAS_TMC_UART_SUPPORT
static km_tmc_uart_config_t board_tmc[KM_MAX_LANES];
#endif
static km_switch_gpio_config_t board_switches;
static km_encoder_gpio_config_t board_encoder_config;
#if KM_HAS_PWM_SUPPORT
static km_pwm_bridge_config_t board_rewind_limits;
static km_rewind_output_config_t board_rewind_outputs[KM_MAX_LANES];
#endif
#if KM_HAS_ADC_SUPPORT
static km_adc_pair_config_t board_adc_config;
#endif
#if KM_HAS_GPIO_LOCAL
/* Full-width pin ids for one MMU batch. Static so the MCU stack stays small.
 * Ownership only: not released, and not physical output activity. */
static uint32_t board_local_pins[KM_RESOURCE_MAX_CLAIMS];
static bool gpio_batch_reserved = false;
#endif
static km_board_startup_detail_t detail = {
    .lane = KM_STARTUP_NO_LANE, .pin = {KM_PIN_UNUSED, KM_PIN_UNUSED}};

/* Breadcrumb: the step (and lane) any later failure belongs to. */
static void at(enum km_startup_step step, uint32_t lane)
{
    detail.step = (uint8_t)step;
    detail.lane = (uint8_t)lane;
}

/* Record a resource diagnostic, naming the claims when the pin view exists. */
static void note_claims(const km_resource_diagnostic_t *d, const km_pin_config_t *pins)
{
    uint16_t index[2] = {d->first_index, d->second_index};
    detail.reason = (uint8_t)d->reason;
    for (unsigned k = 0; k < 2u; ++k) {
        if (!pins || index[k] >= pins->count)
            continue;
        const km_resource_claim_t *claim = &pins->claims[index[k]];
        uint8_t lane = 0;
        for (uint16_t j = 0; j < index[k]; ++j)
            if (pins->claims[j].owner == claim->owner)
                ++lane;
        detail.pin[k] = claim->instance;
        detail.role[k] = (uint8_t)claim->owner;
        detail.pin_lane[k] = lane;
    }
}

static bool task_irqs_enabled(void)
{
    irqstatus_t state = irq_save();
    irq_restore(state);
    return state == 0ul;
}

static bool claim_match(const km_resource_claim_t *a, const km_resource_claim_t *b)
{
    return a->kind == b->kind
        && a->instance == b->instance
        && a->subresource == b->subresource
        && a->owner == b->owner
        && a->period_ticks == b->period_ticks
        && a->tick_hz == b->tick_hz;
}

static bool complete_claims_contain_expected(const km_resource_claim_t *complete, size_t complete_count,
                                             const km_resource_claim_t *expected, size_t expected_count)
{
    for (size_t i = 0; i < expected_count; ++i) {
        bool found = false;
        for (size_t j = 0; j < complete_count; ++j) {
            if (claim_match(&expected[i], &complete[j])) {
                found = true;
                break;
            }
        }
        if (!found) return false;
    }
    return true;
}

/* The controller invokes its sample callback once per control cadence, just
 * before km_core_update(). Encoder sampling piggybacks on the same cadence
 * used for every other MMU sensor here, matching Happy Hare's own encoder
 * poll_time default of 1 ms (extras/mmu/unit/mmu_encoder.py) against this
 * controller's own 1 ms period (controller.h KM_CONTROLLER_PERIOD_US). */
static void board_sample(km_operation_core_t *core, void *context)
{
    km_sensors_sample(core, context);
    if (KM_ENCODER_BUILD && board_profile.has_encoder) {
        km_encoder_gpio_sample(core, context);
    }
}

#if KM_HAS_PWM_SUPPORT
static bool rewind_ready_cb(void *context)
{
    (void)context;
    return startup_state == KM_BOARD_STARTUP_READY && !startup_faulted && !sched_is_shutdown();
}

static km_fault_t rewind_fault_cb(void *context)
{
    (void)context;
    return (startup_faulted || sched_is_shutdown()) ? KM_FAULT_DRIVER_FAULT : KM_FAULT_NONE;
}

/* The controller invokes its sample callback once per control cadence, just
 * before km_core_update(). Service only the active, enabled rewind lane so a
 * newly latched driver fault is visible to that same core update. */
static void board_controller_sample(km_operation_core_t *core, void *context)
{
    board_sample(core, context);
    if (!core || !core->active_rewinder) {
        return;
    }
    for (size_t lane = 0; lane < configured_lanes; ++lane) {
        km_driver_t *rewinder = core->rewinder_drivers[lane];
        if (rewinder && rewinder == core->active_rewinder && rewinder->enabled) {
            km_pwm_bridge_gpio_service((uint32_t)lane);
            return;
        }
    }
}
#endif

#if KM_HAS_GPIO_LOCAL
/* One all-or-none reserve of validated MMU PIN claims. Non-PIN entries stay
 * with their timer/ADC owners. Does not touch clocks, MMIO, or outputs. */
static km_result_t reserve_expected_mmu_pins(void)
{
    uint32_t count = 0;
    size_t limit = board_expected_plan.count;
    if (limit > KM_RESOURCE_MAX_CLAIMS) {
        return KM_ERR_INVALID_PARAM;
    }
    for (size_t i = 0; i < limit; ++i) {
        if (board_expected_plan.claims[i].kind != KM_RESOURCE_PIN) {
            continue;
        }
        if (count >= KM_RESOURCE_MAX_CLAIMS) {
            return KM_ERR_INVALID_PARAM;
        }
        board_local_pins[count++] = (uint32_t)board_expected_plan.claims[i].instance;
    }
    int status = gpio_local_reserve(board_local_pins, count);
    if (status == GPIO_LOCAL_OK) {
        gpio_batch_reserved = true;
        return KM_OK;
    }
    /* Nothing was reserved. Do not latch; old preconditions can still be retried. */
    if (status == GPIO_LOCAL_ERR_INVALID) {
        return KM_ERR_INVALID_PARAM;
    }
    if (status == GPIO_LOCAL_ERR_BUSY) {
        return KM_ERR_CONFLICT;
    }
    return KM_ERR_FAULTED;
}
#endif

/* True when the profile needs a selector stepper axis at all (LINEAR/ROTARY/
 * INDEXED, not SERVO-only or NONE). Shared by every phase that must agree on
 * it. INDEXED (klipper_mmu/selector.h) still has a real stepper axis -- it
 * just has no gate position table -- so it needs the same driver setup as
 * LINEAR/ROTARY here. */
static bool selector_needs_stepper(void)
{
    return board_profile.selector.kind == KM_SELECTOR_LINEAR
        || board_profile.selector.kind == KM_SELECTOR_ROTARY
        || board_profile.selector.kind == KM_SELECTOR_INDEXED;
}

#if CONFIG_KLIPPER_MMU_SELECTOR_STEPPER
/* The selector axis and gear never move at the same time (km_core's single
 * trajectory; selector.h), so both may share this one readiness policy. */
static bool selector_ready_cb(void *context)
{
    (void)context;
    /* The selector axis activates during startup (WAITING), before READY; the
     * core does not command it until startup attaches the controller. */
    return (startup_state == KM_BOARD_STARTUP_WAITING
            || startup_state == KM_BOARD_STARTUP_READY)
        && !startup_faulted && !sched_is_shutdown();
}

static km_fault_t selector_fault_cb(void *context)
{
    (void)context;
    return (startup_faulted || sched_is_shutdown()) ? KM_FAULT_DRIVER_FAULT : KM_FAULT_NONE;
}

/* Whether the selector's own TMC2209 is reachable over its own dedicated
 * single-wire UART pin (KLIPPER_MMU_PINS_SELECTOR_UART, reserved only when
 * KLIPPER_MMU_SELECTOR_UART is on; docs/selectors.md). Purely data-driven:
 * no separate compile-time branch, so one image can be tested both ways. */
#if KM_HAS_TMC_UART_SUPPORT
static bool selector_uses_tmc_uart(void)
{
    return selector_needs_stepper()
        && board_pins->pin[KM_PIN_SELECTOR_UART][0] != KM_PIN_UNUSED;
}
#endif
#endif /* CONFIG_KLIPPER_MMU_SELECTOR_STEPPER */

/* Selector axis (LINEAR/ROTARY only) and/or servo (selector.h). The gear
 * itself is already bound to every lane by the caller; this only sets up
 * and binds the separate selector hardware. Called after the deadline check
 * that follows lane binding, before controller attachment. With its own TMC
 * UART, the selector's feeder (slot 1) was already set up and confirmed
 * during WAITING, exactly like the gear's slot 0; this just fetches the
 * published driver. Without one, it stays the plain STEP/DIR axis it always
 * was, set up here and gated only by this startup's own state. */
static km_result_t setup_selector(void)
{
    km_driver_t *stepper = NULL;
    km_servo_t *servo = NULL;
    if (selector_needs_stepper()) {
#if CONFIG_KLIPPER_MMU_SELECTOR_STEPPER
#if KM_HAS_TMC_UART_SUPPORT
        if (selector_uses_tmc_uart()) {
            stepper = km_tmc_feeder_get_driver(1);
            if (!stepper) {
                return KM_ERR_FAULTED;
            }
        } else
#endif
        {
            km_step_dir_config_t config = board_feeders[0];
            config.maximum_velocity_steps_s = board_profile.selector.velocity_steps_s;
            if (km_step_dir_config_validate(&config) != KM_OK) {
                return KM_ERR_INVALID_PARAM;
            }
            uint32_t step_pin = board_pins->pin[KM_PIN_SELECTOR_STEP][0];
            uint32_t dir_pin = board_pins->pin[KM_PIN_SELECTOR_DIR][0];
            uint32_t enable_pin = board_pins->pin[KM_PIN_SELECTOR_ENABLE][0];
            selector_stepdir_touched = true;
            km_result_t res = km_stepdir_gpio_setup(1, step_pin, dir_pin, enable_pin,
                                                    &config, selector_ready_cb,
                                                    selector_fault_cb, NULL);
            if (res != KM_OK) {
                return res;
            }
            stepper = km_stepdir_gpio_get_driver(1);
            if (!stepper) {
                return KM_ERR_FAULTED;
            }
        }
#else
        return KM_ERR_NOT_SUPPORTED; /* No selector stepper in this image. */
#endif
    }
    if (board_profile.selector.has_servo) {
#if CONFIG_KLIPPER_MMU_SERVO
        uint32_t servo_pin = board_pins->pin[KM_PIN_SERVO][0];
        selector_servo_touched = true;
        km_result_t res = km_servo_pwm_setup(servo_pin, &servo);
        if (res != KM_OK) {
            return res;
        }
#else
        return KM_ERR_NOT_SUPPORTED; /* mcu/servo_pwm.c is not in this image. */
#endif
    }
    return km_core_bind_selector(&board_core, stepper, servo);
}

static void cleanup_touched_components(void)
{
    if (!physical_mutation_started) {
        return;
    }
#if KM_HAS_PWM_SUPPORT
    if (board_profile.has_powered_rewind && pwm_touched) {
        km_pwm_bridge_gpio_shutdown();
    }
#endif
    if (stepdir_touched || selector_stepdir_touched) {
        km_stepdir_gpio_shutdown();
    }
#if KM_HAS_TMC_UART_SUPPORT
    if (tmc_uart_touched) {
        km_tmc_uart_gpio_shutdown();
        tmc_uart_touched = false;
    }
#endif
#if CONFIG_KLIPPER_MMU_SERVO
    if (selector_servo_touched) {
        km_servo_pwm_shutdown();
    }
#endif
#if KM_HAS_ADC_SUPPORT
    if (board_profile.has_proportional_buffer && adc_started) {
        km_adc_shutdown();
    }
#endif
    if (switch_owned) {
        km_switch_gpio_shutdown();
    }
    if (KM_ENCODER_BUILD && encoder_owned) {
        km_encoder_gpio_shutdown();
    }
    if (controller_attached) {
        km_controller_shutdown();
    }
}

static void fail_and_cleanup(km_result_t err)
{
    irqstatus_t flag = irq_save();
    if (!detail.cause)
        detail.cause = (int8_t)err;
    startup_faulted = true;
    startup_state = KM_BOARD_STARTUP_FAULTED;
    if (startup_result == KM_OK || startup_result == KM_ERR_UNAVAILABLE) {
        startup_result = (err != KM_OK && err != KM_ERR_UNAVAILABLE) ? err : KM_ERR_FAULTED;
    }
    cleanup_touched_components();
    irq_restore(flag);
}

static km_result_t check_deadline(uint32_t *remaining_us)
{
    if (startup_faulted || sched_is_shutdown()) {
        fail_and_cleanup(KM_ERR_FAULTED);
        return KM_ERR_FAULTED;
    }
    km_core_timepoint_t t_now;
    if (km_clock_get_time(&t_now) != KM_OK) {
        fail_and_cleanup(KM_ERR_FAULTED);
        return KM_ERR_FAULTED;
    }
    uint32_t elapsed_us = (uint32_t)(t_now.us - start_us);
    if (elapsed_us >= startup_timeout_us) {
        fail_and_cleanup(KM_ERR_TIMEOUT);
        return KM_ERR_TIMEOUT;
    }
    if (remaining_us != NULL) {
        *remaining_us = startup_timeout_us - elapsed_us;
    }
    return KM_OK;
}

#if !KM_HAS_TMC_UART_SUPPORT
/* Generic step/dir readiness policy for a plain-driven feeder lane (no
 * shared TMC UART, drivers set in hardware -- Happy Hare's
 * CHOICE_GEAR_TMC_NONE): active only once this startup has reached WAITING
 * or READY and is not faulted or shut down. Logic mirrors the selector's own
 * non-UART fallback (selector_ready_cb/selector_fault_cb above); duplicated
 * rather than shared so this feeder-only path stays independent of
 * selector-owned code that may change separately. */
static bool feeder_stepdir_ready_cb(void *context)
{
    (void)context;
    return (startup_state == KM_BOARD_STARTUP_WAITING
            || startup_state == KM_BOARD_STARTUP_READY)
        && !startup_faulted && !sched_is_shutdown();
}
static km_fault_t feeder_stepdir_fault_cb(void *context)
{
    (void)context;
    return (startup_faulted || sched_is_shutdown()) ? KM_FAULT_DRIVER_FAULT : KM_FAULT_NONE;
}
#endif

static km_result_t advance_startup(void)
{
    km_result_t res = check_deadline(NULL);
    if (res != KM_OK) {
        return res;
    }

    /* With a selector, only physical slot 0 (the gear) needs activation;
     * every logical lane below shares that one confirmed driver. A selector
     * with its own TMC UART adds physical slot 1, confirmed the same way. */
    /* Plain STEP/DIR feeders need no confirmation handshake: setup succeeding
     * below is confirmation, exactly like the selector's own non-UART
     * fallback (setup_selector). Nothing here needs confirming. */
    size_t physical_lanes = KM_HAS_TMC_UART_SUPPORT
        ? (board_profile.has_selector ? 1u : configured_lanes) : 0u;
#if KM_HAS_TMC_UART_SUPPORT && CONFIG_KLIPPER_MMU_SELECTOR_STEPPER
    if (board_profile.has_selector && selector_uses_tmc_uart()) {
        physical_lanes = 2u;
    }
#endif
    bool all_confirmed = true;
    for (size_t i = 0; i < physical_lanes; ++i) {
        if (!lane_confirmed[i]) {
#if KM_HAS_TMC_UART_SUPPORT
            km_result_t ares = km_tmc_feeder_activate((uint32_t)i);
#else
            km_result_t ares = KM_ERR_NOT_SUPPORTED;
#endif
            if (ares == KM_OK) {
                lane_confirmed[i] = true;
            } else if (ares == KM_ERR_UNAVAILABLE) {
                if (all_confirmed)
                    at(KM_STARTUP_STEP_DRIVER_CONFIRM, (uint32_t)i);
                all_confirmed = false;
            } else {
                at(KM_STARTUP_STEP_DRIVER_CONFIRM, (uint32_t)i);
                detail.cause = (int8_t)ares;
                fail_and_cleanup(KM_ERR_FAULTED);
                return KM_ERR_FAULTED;
            }

            res = check_deadline(NULL);
            if (res != KM_OK) {
                return res;
            }
        }
    }

    if (!all_confirmed) {
        startup_state = KM_BOARD_STARTUP_WAITING;
        startup_result = KM_ERR_UNAVAILABLE;
        return KM_ERR_UNAVAILABLE;
    }

#if !KM_HAS_TMC_UART_SUPPORT
    /* No shared TMC UART for the gear: each physical feeder lane is a plain
     * STEP/DIR axis, drivers set in hardware (Happy Hare's
     * CHOICE_GEAR_TMC_NONE) -- the same one-shot path already used below for
     * a selector without its own TMC UART. No run/hold current is claimed
     * for a driver this firmware never talks to (mcu/Kconfig.tmc). */
    {
        uint32_t physical_feeders = board_profile.has_selector ? 1u : (uint32_t)configured_lanes;
        for (uint32_t lane = 0; lane < physical_feeders; ++lane) {
            at(KM_STARTUP_STEP_DRIVER_SETUP, lane);
            stepdir_touched = true;
            uint32_t step_pin = board_pins->pin[KM_PIN_STEP][lane];
            uint32_t dir_pin = board_pins->pin[KM_PIN_DIR][lane];
            uint32_t enable_pin = board_pins->pin[KM_PIN_ENABLE][lane];
            res = km_stepdir_gpio_setup(lane, step_pin, dir_pin, enable_pin,
                                        &board_feeders[lane], feeder_stepdir_ready_cb,
                                        feeder_stepdir_fault_cb, NULL);
            if (res != KM_OK) {
                fail_and_cleanup(res);
                return res;
            }
            res = check_deadline(NULL);
            if (res != KM_OK) {
                return res;
            }
        }
    }
#endif

    /* All lanes confirmed. Check deadline before driver binding */
    res = check_deadline(NULL);
    if (res != KM_OK) {
        return res;
    }

#if KM_HAS_TMC_UART_SUPPORT
    /* With a selector, the one gear driver (physical slot 0) is bound to
     * every logical lane; km_core's own trajectory keeps it and the
     * selector axis from moving at once (selector.h). */
    km_driver_t *shared_gear = board_profile.has_selector
                             ? km_tmc_feeder_get_driver(0) : NULL;
#else
    /* No shared TMC UART: the shared gear (if any) is the plain STEP/DIR
     * driver set up above, at the same physical slot 0. */
    km_driver_t *shared_gear = board_profile.has_selector
                             ? km_stepdir_gpio_get_driver(0) : NULL;
#endif
    if (board_profile.has_selector && !shared_gear) {
        fail_and_cleanup(KM_ERR_FAULTED);
        return KM_ERR_FAULTED;
    }
    for (uint8_t i = 0; i < (uint8_t)configured_lanes; ++i) {
        at(KM_STARTUP_STEP_BIND, i);
#if KM_HAS_TMC_UART_SUPPORT
        km_driver_t *feeder = board_profile.has_selector
                             ? shared_gear : km_tmc_feeder_get_driver((uint32_t)i);
#else
        km_driver_t *feeder = board_profile.has_selector
                             ? shared_gear : km_stepdir_gpio_get_driver((uint32_t)i);
#endif
        km_driver_t *rewinder = NULL;
#if KM_HAS_PWM_SUPPORT
        if (board_profile.has_powered_rewind) {
            rewinder = km_pwm_bridge_gpio_get_driver((uint32_t)i);
        }
#endif
        if (!feeder || (board_profile.has_powered_rewind && !rewinder)) {
            fail_and_cleanup(KM_ERR_FAULTED);
            return KM_ERR_FAULTED;
        }
        res = km_core_bind_lane_driver(&board_core, i, feeder, rewinder);
        if (res != KM_OK) {
            fail_and_cleanup(res);
            return res;
        }
    }

    /* Check deadline after binding, before selector setup and controller attachment */
    res = check_deadline(NULL);
    if (res != KM_OK) {
        return res;
    }

    if (board_profile.has_selector) {
        at(KM_STARTUP_STEP_SELECTOR_SETUP, KM_STARTUP_NO_LANE);
        res = setup_selector();
        if (res != KM_OK) {
            fail_and_cleanup(res);
            return res;
        }
        res = check_deadline(NULL);
        if (res != KM_OK) {
            return res;
        }
    }

    at(KM_STARTUP_STEP_ATTACH, KM_STARTUP_NO_LANE);
    controller_attached = true;
#if KM_HAS_PWM_SUPPORT
    res = km_controller_attach(&board_core, board_profile.has_powered_rewind
                               ? board_controller_sample : board_sample, NULL);
#else
    res = km_controller_attach(&board_core, board_sample, NULL);
#endif
    if (res != KM_OK) {
        fail_and_cleanup(res);
        return res;
    }

    /* Publication critical section: final clock observation, latch/shutdown check, READY publication */
    irqstatus_t pub_flag = irq_save();
    if (startup_faulted || sched_is_shutdown()) {
        irq_restore(pub_flag);
        fail_and_cleanup(KM_ERR_FAULTED);
        return (startup_result != KM_OK && startup_result != KM_ERR_UNAVAILABLE) ? startup_result : KM_ERR_FAULTED;
    }
    km_core_timepoint_t t_final;
    if (km_clock_get_time(&t_final) != KM_OK) {
        irq_restore(pub_flag);
        fail_and_cleanup(KM_ERR_FAULTED);
        return KM_ERR_FAULTED;
    }
    if ((uint32_t)(t_final.us - start_us) >= startup_timeout_us) {
        irq_restore(pub_flag);
        fail_and_cleanup(KM_ERR_TIMEOUT);
        return KM_ERR_TIMEOUT;
    }
    startup_state = KM_BOARD_STARTUP_READY;
    startup_result = KM_OK;
    irq_restore(pub_flag);
    return KM_OK;
}

static km_result_t begin_board(const km_resource_claim_t *complete_claims,
                               size_t claim_count, uint32_t timeout_us,
                               const km_diff_adc_config_t *adc_calibration)
{
    /* 1. Preconditions and call context checks (no I/O, no state mutation) */
    if (!task_irqs_enabled()) {
        return KM_ERR_INVALID_PARAM;
    }
#if KM_HAS_GPIO_LOCAL
    if (complete_claims == NULL) {
        if (claim_count != 0) {
            return KM_ERR_INVALID_PARAM;
        }
    } else if (claim_count == 0 || claim_count > KM_RESOURCE_MAX_CLAIMS) {
        return KM_ERR_INVALID_PARAM;
    }
#else
    if (!complete_claims || claim_count == 0 || claim_count > KM_RESOURCE_MAX_CLAIMS) {
        return KM_ERR_INVALID_PARAM;
    }
#endif
    if (timeout_us == 0 || timeout_us >= (1u << 31)) {
        return KM_ERR_INVALID_PARAM;
    }
    if (startup_faulted || sched_is_shutdown()) {
        return KM_ERR_FAULTED;
    }
    enum km_clock_state clk_state = km_clock_get_state();
    if (clk_state == KM_CLOCK_STATE_UNAVAILABLE) {
        return KM_ERR_UNAVAILABLE;
    }
    if (clk_state != KM_CLOCK_STATE_READY) {
        return KM_ERR_FAULTED;
    }
    if (startup_state != KM_BOARD_STARTUP_UNINITIALIZED) {
        return KM_ERR_BUSY;
    }
    detail = (km_board_startup_detail_t){.lane = KM_STARTUP_NO_LANE,
                                         .pin = {KM_PIN_UNUSED, KM_PIN_UNUSED}};
    at(KM_STARTUP_STEP_PROFILE, KM_STARTUP_NO_LANE);

    /* 2. Check absence of attached controller and existing shutdowns */
    if (km_controller_is_attached()) {
        return KM_ERR_BUSY;
    }
    if (km_controller_is_shutdown() || km_stepdir_gpio_is_shutdown()) {
        return KM_ERR_FAULTED;
    }
#if KM_HAS_TMC_UART_SUPPORT
    if (km_tmc_uart_gpio_is_shutdown()) {
        return KM_ERR_FAULTED;
    }
#endif
#if KM_HAS_PWM_SUPPORT
    if (km_pwm_bridge_gpio_is_shutdown()) {
        return KM_ERR_FAULTED;
    }
#endif

    /* Preflight ALL KM_MAX_LANES slots in each component affected by global cleanup */
    for (uint32_t i = 0; i < KM_MAX_LANES; ++i) {
        uint32_t step, dir, en;
        if (km_stepdir_gpio_get_slot_pins(i, &step, &dir, &en) == KM_OK ||
            km_stepdir_gpio_is_slot_active(i)) {
            return KM_ERR_BUSY;
        }
#if KM_HAS_TMC_UART_SUPPORT
        uint32_t uart;
        if (km_tmc_uart_gpio_get_pin(i, &uart) == KM_OK ||
            km_tmc_uart_gpio_is_slot_active(i)) {
            return KM_ERR_BUSY;
        }
#endif
#if KM_HAS_PWM_SUPPORT
        uint32_t fwd, rev, ren;
        if (km_pwm_bridge_gpio_get_slot_pins(i, &fwd, &rev, &ren) == KM_OK ||
            km_pwm_bridge_gpio_is_slot_active(i)) {
            return KM_ERR_BUSY;
        }
#endif
    }

    /* 3. Validate saved factories before touching any hardware */
    km_result_t res = km_config_profile(&board_profile, adc_calibration);
    if (res != KM_OK) {
        return res;
    }
    /* Capabilities check: unsupported current sensing must reject, do not
     * downgrade. The encoder capability now has a real driver (encoder.h);
     * a selector profile (klipper_mmu/selector.h) is supported below. */
    if (board_profile.has_current_sensing) {
        return KM_ERR_NOT_SUPPORTED;
    }
    if (board_profile.has_selector
        && board_profile.selector.kind != KM_SELECTOR_SERVO
        && !CONFIG_KLIPPER_MMU_SELECTOR_STEPPER) {
        return KM_ERR_NOT_SUPPORTED;
    }
    if (board_profile.has_selector && board_profile.selector.has_servo
        && !CONFIG_KLIPPER_MMU_SERVO) {
        return KM_ERR_NOT_SUPPORTED;
    }
    if (board_profile.lane_count == 0 || board_profile.lane_count > KM_MAX_LANES) {
        return KM_ERR_INVALID_PARAM;
    }
    configured_lanes = board_profile.lane_count;

    /* Build-time feature availability checks. A gear with no shared TMC UART
     * (CONFIG_KLIPPER_MMU_TMC_UART_SHARED=0, drivers set in hardware --
     * Happy Hare's CHOICE_GEAR_TMC_NONE) is no longer categorically
     * unsupported: it runs plain STEP/DIR instead, set up further down. */
    if (board_profile.has_powered_rewind && !KM_HAS_PWM_SUPPORT) {
        return KM_ERR_NOT_SUPPORTED;
    }
    if (board_profile.has_proportional_buffer && !KM_HAS_ADC_SUPPORT) {
        return KM_ERR_NOT_SUPPORTED;
    }

    km_resource_diagnostic_t diag;
    at(KM_STARTUP_STEP_PINS, KM_STARTUP_NO_LANE);
    res = km_config_pins_view(&board_pins, &diag);
    if (res != KM_OK) {
        note_claims(&diag, NULL);
        return res;
    }

    at(KM_STARTUP_STEP_RESOURCES, KM_STARTUP_NO_LANE);
    res = km_config_h723_resources(&board_expected_plan, &diag);
    if (res != KM_OK) {
        note_claims(&diag, board_pins);
        return res;
    }

    at(KM_STARTUP_STEP_SAVED_CONFIG, KM_STARTUP_NO_LANE);
    res = km_config_feeders(board_feeders);
    if (res != KM_OK) {
        return res;
    }

#if KM_HAS_TMC_UART_SUPPORT
    res = km_config_tmc(board_tmc);
    if (res != KM_OK) {
        return res;
    }
#endif

#if KM_HAS_TMC_UART_SUPPORT && CONFIG_KLIPPER_MMU_SELECTOR_STEPPER
    /* Selector's own TMC2209, on its own dedicated UART wire, at physical
     * slot 1 (board_tmc[1] is otherwise unused: KM_GEAR_DRIVERS is 1 with a
     * selector, so km_config_tmc left it zeroed). Validated here, before the
     * deadline starts, exactly like the gear's own km_config_tmc call above. */
    if (board_profile.has_selector && selector_uses_tmc_uart()) {
        res = km_config_selector_tmc(&board_tmc[1]);
        if (res != KM_OK) {
            return res;
        }
    }
#endif

    res = km_config_switches(&board_switches);
    if (res != KM_OK) {
        return res;
    }
    if (KM_ENCODER_BUILD && board_profile.has_encoder) {
        res = km_config_encoder(&board_encoder_config);
        if (res != KM_OK) {
            return res;
        }
    }

#if KM_HAS_PWM_SUPPORT
    if (board_profile.has_powered_rewind) {
        res = km_config_rewind(&board_rewind_limits);
        if (res != KM_OK) {
            return res;
        }
        res = km_config_rewind_outputs(board_rewind_outputs);
        if (res != KM_OK) {
            return res;
        }
    }
#endif

#if KM_HAS_ADC_SUPPORT
    if (board_profile.has_proportional_buffer) {
        enum km_adc_state adc_st = km_adc_get_state();
        if (adc_st == KM_ADC_STATE_RUNNING) {
            return KM_ERR_BUSY;
        }
        if (adc_st == KM_ADC_STATE_FAULTED || adc_st == KM_ADC_STATE_SHUTDOWN) {
            return KM_ERR_FAULTED;
        }
        res = km_config_adc_pair(&board_adc_config);
        if (res != KM_OK) {
            return res;
        }
    }
#endif

#if KM_HAS_TMC_UART_SUPPORT
    /* Validate exact run/hold targets across feeder, profile, and TMC. A
     * gear with no TMC UART never claims a current (mcu/Kconfig.tmc), so
     * there is nothing here to cross-check. */
    /* A selector machine configures one gear driver, not one per lane. */
    size_t gear_drivers = board_profile.has_selector ? 1u : configured_lanes;
    for (size_t i = 0; i < gear_drivers; ++i) {
        if (board_tmc[i].tmc.run_current_ma != board_profile.default_run_current_ma ||
            board_tmc[i].tmc.hold_current_ma != board_profile.default_hold_current_ma) {
            return KM_ERR_INVALID_PARAM;
        }
    }
#endif

    /* Supplied arrays stay caller completeness assertions. NULL,0 skips only that list. */
#if KM_HAS_GPIO_LOCAL
    if (complete_claims != NULL) {
#endif
    res = km_resource_plan_validate(complete_claims, claim_count, &diag);
    if (res != KM_OK) {
        return res;
    }
    if (!complete_claims_contain_expected(complete_claims, claim_count,
                                          board_expected_plan.claims, board_expected_plan.count)) {
        return KM_ERR_CONFLICT;
    }
#if KM_HAS_GPIO_LOCAL
    }
#endif

    /* ALL VALIDATION SUCCEEDED WITHOUT I/O. Deadline starts here.
     * A later local GPIO batch is ownership, not output activity.
     * Peripheral setup begins at the switch call below. */
    km_core_timepoint_t t_start;
    res = km_clock_get_time(&t_start);
    if (res != KM_OK) {
        return res;
    }
    start_us = t_start.us;
    startup_timeout_us = timeout_us;
    for (size_t i = 0; i < KM_MAX_LANES; ++i) {
        lane_confirmed[i] = false;
    }

    /* Initialize core RAM data structure (no I/O or GPIO) */
    res = km_core_init(&board_core, &board_profile);
    if (res != KM_OK) {
        return res;
    }

#if KM_HAS_GPIO_LOCAL
    /* Expected-plan PIN ids only, once. External complete-plan entries are not reserved.
     * Rejecting leaves no local claim and does not latch or clean up components. */
    at(KM_STARTUP_STEP_RESERVE, KM_STARTUP_NO_LANE);
    res = reserve_expected_mmu_pins();
    if (res != KM_OK) {
        return res;
    }
    /* Synchronous reserve time is inside start_us. Recheck before any component setup.
     * Failure latches with claims retained; component flags are still clear. */
    res = check_deadline(NULL);
    if (res != KM_OK) {
        return res;
    }
#endif

    /* Configure switch inputs: track ownership before call.
     * Pure BUSY is a preexisting foreign binding (no I/O). Without a retained
     * local batch, revert and stay retryable. After a successful batch, latch
     * and do not shut that foreign switch down or release pins. */
    at(KM_STARTUP_STEP_SWITCHES, KM_STARTUP_NO_LANE);
    physical_mutation_started = true;
    switch_owned = true;
    res = km_switch_gpio_setup(&board_core, &board_switches);
    if (res != KM_OK) {
        if (res == KM_ERR_BUSY) {
            switch_owned = false;
            physical_mutation_started = false;
#if KM_HAS_GPIO_LOCAL
            if (gpio_batch_reserved) {
                fail_and_cleanup(KM_ERR_BUSY);
                return KM_ERR_BUSY;
            }
#endif
            return KM_ERR_BUSY;
        }
        fail_and_cleanup(res);
        return res;
    }
    res = check_deadline(NULL);
    if (res != KM_OK) {
        return res;
    }

    if (KM_ENCODER_BUILD && board_profile.has_encoder) {
        encoder_owned = true;
        res = km_encoder_gpio_setup(&board_encoder_config);
        if (res != KM_OK) {
            fail_and_cleanup(res);
            return res;
        }
        res = check_deadline(NULL);
        if (res != KM_OK) {
            return res;
        }
    }

#if KM_HAS_ADC_SUPPORT
    /* Optionally start sequential ADC */
    if (board_profile.has_proportional_buffer) {
        at(KM_STARTUP_STEP_ADC, KM_STARTUP_NO_LANE);
        uint32_t remaining = 0u;
        res = check_deadline(&remaining);
        if (res != KM_OK) {
            return res;
        }
        adc_started = true;
        res = km_adc_start(board_pins->pin[KM_PIN_BUFFER_A][0],
                           board_pins->pin[KM_PIN_BUFFER_B][0],
                           &board_adc_config, remaining);
        if (res != KM_OK) {
            fail_and_cleanup(res);
            return res;
        }
        res = check_deadline(NULL);
        if (res != KM_OK) {
            return res;
        }
    }
#endif

#if KM_HAS_TMC_UART_SUPPORT
    /* Prepare feeder lane(s). With a selector, the gear is one shared driver
     * (board_pins/board_feeders/board_tmc index 0) bound to every logical
     * lane below; only that one physical slot is set up here. */
    uint32_t physical_feeders = board_profile.has_selector ? 1u : (uint32_t)configured_lanes;
    for (uint32_t lane = 0; lane < physical_feeders; ++lane) {
        at(KM_STARTUP_STEP_DRIVER_SETUP, lane);
        stepdir_touched = true;
        /* Feeder setup may claim UART even when it returns an error. */
        tmc_uart_touched = true;
        uint32_t step_pin = board_pins->pin[KM_PIN_STEP][lane];
        uint32_t dir_pin = board_pins->pin[KM_PIN_DIR][lane];
        uint32_t enable_pin = board_pins->pin[KM_PIN_ENABLE][lane];
        uint32_t uart_pin = board_pins->pin[KM_PIN_UART][lane];
        res = km_tmc_feeder_setup(lane, step_pin, dir_pin, enable_pin, uart_pin,
                                  board_tmc[lane].uart_pull_up, &board_feeders[lane],
                                  &board_tmc[lane].tmc,
                                  board_profile.default_run_current_ma,
                                  board_profile.default_hold_current_ma);
        if (res != KM_OK) {
            fail_and_cleanup(res);
            return res;
        }
        res = check_deadline(NULL);
        if (res != KM_OK) {
            return res;
        }
    }

#if CONFIG_KLIPPER_MMU_SELECTOR_STEPPER
    /* Selector's own TMC feeder, physical slot 1, set up and later confirmed
     * exactly like the gear's slot 0 above. Its step/dir pulse timing and
     * polarity are shared board electrical properties, same as the gear's
     * (board_feeders[0]); only its own velocity differs. */
    if (board_profile.has_selector && selector_uses_tmc_uart()) {
        at(KM_STARTUP_STEP_DRIVER_SETUP, 1);
        selector_stepdir_touched = true;
        tmc_uart_touched = true;
        km_step_dir_config_t selector_config = board_feeders[0];
        selector_config.maximum_velocity_steps_s = board_profile.selector.velocity_steps_s;
        uint32_t sel_step_pin = board_pins->pin[KM_PIN_SELECTOR_STEP][0];
        uint32_t sel_dir_pin = board_pins->pin[KM_PIN_SELECTOR_DIR][0];
        uint32_t sel_enable_pin = board_pins->pin[KM_PIN_SELECTOR_ENABLE][0];
        uint32_t sel_uart_pin = board_pins->pin[KM_PIN_SELECTOR_UART][0];
        res = km_tmc_feeder_setup(1, sel_step_pin, sel_dir_pin, sel_enable_pin,
                                  sel_uart_pin, board_tmc[1].uart_pull_up,
                                  &selector_config, &board_tmc[1].tmc,
                                  CONFIG_KLIPPER_MMU_SELECTOR_RUN_CURRENT,
                                  CONFIG_KLIPPER_MMU_SELECTOR_HOLD_CURRENT);
        if (res != KM_OK) {
            fail_and_cleanup(res);
            return res;
        }
        res = check_deadline(NULL);
        if (res != KM_OK) {
            return res;
        }
    }
#endif
#endif

#if KM_HAS_PWM_SUPPORT
    /* Optionally prepare rewind bridges */
    if (board_profile.has_powered_rewind) {
        for (uint32_t lane = 0; lane < configured_lanes; ++lane) {
            at(KM_STARTUP_STEP_REWIND_SETUP, lane);
            pwm_touched = true;
            res = km_pwm_bridge_gpio_setup(lane, complete_claims, claim_count,
                                           rewind_ready_cb, rewind_fault_cb, NULL);
            if (res != KM_OK) {
                fail_and_cleanup(res);
                return res;
            }
            res = check_deadline(NULL);
            if (res != KM_OK) {
                return res;
            }
        }
    }
#endif

    startup_state = KM_BOARD_STARTUP_WAITING;
    startup_result = KM_ERR_UNAVAILABLE;

    at(KM_STARTUP_STEP_DRIVER_CONFIRM, KM_STARTUP_NO_LANE);
    km_result_t adv = advance_startup();
    if (adv == KM_OK || adv == KM_ERR_UNAVAILABLE) {
        return KM_OK;
    }
    return adv;
}

KM_API km_result_t km_board_startup_begin(const km_resource_claim_t *complete_claims,
                                          size_t claim_count,
                                          uint32_t timeout_us,
                                          const km_diff_adc_config_t *adc_calibration)
{
    km_result_t res = begin_board(complete_claims, claim_count, timeout_us,
                                  adc_calibration);
    if (res != KM_OK && !detail.cause)
        detail.cause = (int8_t)res;
    return res;
}

KM_API void km_board_startup_get_detail(km_board_startup_detail_t *out)
{
    irqstatus_t flag = irq_save();
    *out = detail;
    irq_restore(flag);
}

KM_API km_result_t km_board_startup_service(void)
{
    if (!task_irqs_enabled()) {
        return KM_ERR_INVALID_PARAM;
    }

    irqstatus_t flag = irq_save();
    if (startup_faulted || sched_is_shutdown()) {
        if (!startup_faulted) {
            irq_restore(flag);
            fail_and_cleanup(KM_ERR_FAULTED);
            return KM_ERR_FAULTED;
        }
        km_result_t r = (startup_result != KM_OK && startup_result != KM_ERR_UNAVAILABLE) ? startup_result : KM_ERR_FAULTED;
        irq_restore(flag);
        return r;
    }

    if (startup_state == KM_BOARD_STARTUP_READY) {
        irq_restore(flag);
        return KM_OK;
    }
    if (startup_state == KM_BOARD_STARTUP_UNINITIALIZED) {
        irq_restore(flag);
        return KM_ERR_UNAVAILABLE;
    }
    irq_restore(flag);

    return advance_startup();
}

KM_API enum km_board_startup_state km_board_startup_get_state(void)
{
    irqstatus_t flag = irq_save();
    enum km_board_startup_state s = startup_state;
    if (startup_faulted || sched_is_shutdown()) {
        s = KM_BOARD_STARTUP_FAULTED;
    }
    irq_restore(flag);
    return s;
}

KM_API km_result_t km_board_startup_get_result(void)
{
    irqstatus_t flag = irq_save();
    km_result_t r = startup_result;
    if (startup_faulted || sched_is_shutdown()) {
        if (r == KM_OK || r == KM_ERR_UNAVAILABLE) {
            r = KM_ERR_FAULTED;
        }
    }
    irq_restore(flag);
    return r;
}

KM_API km_operation_core_t *km_board_startup_get_core(void)
{
    irqstatus_t flag = irq_save();
    km_operation_core_t *core = NULL;
    if (startup_state == KM_BOARD_STARTUP_READY && !startup_faulted && !sched_is_shutdown()) {
        core = &board_core;
    }
    irq_restore(flag);
    return core;
}

void km_board_startup_shutdown(void)
{
    fail_and_cleanup(KM_ERR_FAULTED);
}
DECL_SHUTDOWN(km_board_startup_shutdown);
