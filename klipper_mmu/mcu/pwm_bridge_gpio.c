// SPDX-License-Identifier: GPL-3.0-or-later
// MMU policy binding for Klipper's shared STM32 paired-PWM driver.
#include "pwm_bridge_gpio.h"
#include "clock.h"
#include "h723_resources.h"
#include "pin_config.h"
#include "rewind_config.h"
#include "autoconf.h"
#if CONFIG_WANT_GPIO_LOCAL
#include "stm32/gpio_local.h"
#endif
#include "board/gpio.h"
#include "board/irq.h"
#include "command.h"
#include "sched.h"
#include "stm32/pwm.h"
#include <stdint.h>

#define PAIR_SCALE 32768u

typedef struct {
    bool claimed, published, enable_touched, raw_latched;
    uint32_t forward_pin, reverse_pin, enable_pin;
    bool enable_active_high;
    struct gpio_out enable_gpio;
    struct gpio_pwm_pair *pair;
    uint16_t requested_forward, requested_reverse;
    uint32_t last_completion_us;
    km_fault_t fault_latch;
    bool (*user_ready)(void *);
    km_fault_t (*user_fault)(void *);
    void *user_context;
    km_pwm_bridge_driver_t driver;
} pwm_slot_t;

static pwm_slot_t slots[KM_PWM_BRIDGE_MAX_SLOTS];
static bool global_shutdown_latched;
// Setup is serialized task work. Keep large plan scratch off the MCU stack.
static km_h723_resource_plan_t expected_plan;
static const km_pin_config_t *saved_pins;
static km_rewind_output_config_t saved_outputs[KM_MAX_LANES];

static bool task_irqs_enabled(void)
{
    irqstatus_t state = irq_save();
    irq_restore(state);
    return state == 0ul;
}

static void enable_off(void *context)
{
    pwm_slot_t *s = context;
    if (s && s->enable_touched)
        gpio_out_write(s->enable_gpio, s->enable_active_high ? 0u : 1u);
}

static void latch_off(pwm_slot_t *s)
{
    if (!s || !s->claimed) return;
    s->raw_latched = true;
    s->fault_latch = KM_FAULT_DRIVER_FAULT;
    enable_off(s);
    if (s->pair) gpio_pwm_pair_raw_disable(s->pair);
}

static bool live(const pwm_slot_t *s)
{
    return s && s->claimed && s->pair && !s->raw_latched
        && !global_shutdown_latched && !sched_is_shutdown()
        && s->fault_latch == KM_FAULT_NONE
        && gpio_pwm_pair_is_ready(s->pair);
}

static bool io_ready(void *context)
{
    pwm_slot_t *s = context;
    if (!live(s)) return false;
    bool ready = !s->user_ready || s->user_ready(s->user_context);
    return ready && live(s);
}

static km_fault_t io_fault(void *context)
{
    pwm_slot_t *s = context;
    if (!s || s->fault_latch != KM_FAULT_NONE || s->raw_latched
        || global_shutdown_latched || sched_is_shutdown())
        return KM_FAULT_DRIVER_FAULT;
    km_fault_t fault = s->user_fault ? s->user_fault(s->user_context) : KM_FAULT_NONE;
    return live(s) ? fault : KM_FAULT_DRIVER_FAULT;
}

static uint32_t io_now_us(void *context)
{
    pwm_slot_t *s = context;
    if (s->raw_latched || global_shutdown_latched || sched_is_shutdown())
        return s->last_completion_us;
    uint32_t observed;
    if (km_clock_get_completion_us(&observed) != KM_OK) {
        latch_off(s);
        shutdown("MMU PWM clock unavailable"); // never invent completion
    }
    s->last_completion_us = observed;
    return observed;
}

static km_result_t io_write_enable(void *context, bool enabled)
{
    pwm_slot_t *s = context;
    if (!enabled) {
        enable_off(s);
        return KM_OK;
    }
    if (!io_ready(s) || km_clock_get_state() != KM_CLOCK_STATE_READY) {
        enable_off(s);
        return KM_ERR_UNAVAILABLE;
    }
    struct gpio_pwm_pair_info info;
    if (gpio_pwm_pair_get_info(s->pair, &info) != GPIO_PWM_PAIR_OK ||
        !info.is_ready || info.confirmed_forward_counts || info.confirmed_reverse_counts) {
        latch_off(s);
        return KM_ERR_FAULTED;
    }
    if (!io_ready(s)) {
        enable_off(s);
        return KM_ERR_UNAVAILABLE;
    }
    gpio_out_write(s->enable_gpio, s->enable_active_high ? 1u : 0u);
    if (!live(s)) {
        enable_off(s);
        return KM_ERR_FAULTED;
    }
    return KM_OK;
}

static km_result_t io_write_pwm(void *context, km_direction_t direction,
                                uint16_t duty_permille)
{
    pwm_slot_t *s = context;
    if (direction != KM_DIR_FORWARD && direction != KM_DIR_REVERSE)
        return KM_ERR_INVALID_PARAM;
    if (duty_permille > 1000u) return KM_ERR_INVALID_PARAM;
    if (!live(s)) { enable_off(s); return KM_ERR_FAULTED; }
    uint16_t forward = s->requested_forward, reverse = s->requested_reverse;
    if (direction == KM_DIR_FORWARD) forward = duty_permille;
    else reverse = duty_permille;
    if (forward && reverse) { enable_off(s); return KM_ERR_INVALID_PARAM; }
    uint32_t f = (uint32_t)forward * PAIR_SCALE / 1000u;
    uint32_t r = (uint32_t)reverse * PAIR_SCALE / 1000u;
    if (gpio_pwm_pair_update(s->pair, f, r) != GPIO_PWM_PAIR_OK) {
        latch_off(s);
        return KM_ERR_FAULTED;
    }
    struct gpio_pwm_pair_info info;
    if (gpio_pwm_pair_get_info(s->pair, &info) != GPIO_PWM_PAIR_OK ||
        !info.is_ready || (!forward && info.confirmed_forward_counts) ||
        (!reverse && info.confirmed_reverse_counts) ||
        (info.confirmed_forward_counts && info.confirmed_reverse_counts)) {
        latch_off(s);
        return KM_ERR_FAULTED;
    }
    uint32_t completed;
    if (km_clock_get_completion_us(&completed) != KM_OK || !live(s)) {
        latch_off(s);
        return KM_ERR_FAULTED;
    }
    s->requested_forward = forward;
    s->requested_reverse = reverse;
    s->last_completion_us = completed;
    return KM_OK;
}

static const km_pwm_bridge_io_t bridge_io = {
    .force_disable=enable_off, .write_enable=io_write_enable,
    .write_pwm=io_write_pwm, .now_us=io_now_us,
    .ready=io_ready, .fault=io_fault,
};

static bool same_claim(const km_resource_claim_t *a, const km_resource_claim_t *b)
{
    return a->kind == b->kind && a->instance == b->instance
        && a->subresource == b->subresource && a->owner == b->owner
        && a->period_ticks == b->period_ticks && a->tick_hz == b->tick_hz;
}

static bool contains_expected(const km_resource_claim_t *complete, size_t count)
{
    for (size_t i = 0; i < expected_plan.count; ++i) {
        bool found = false;
        for (size_t j = 0; j < count; ++j)
            if (same_claim(&expected_plan.claims[i], &complete[j])) {
                found = true; break;
            }
        if (!found) return false;
    }
    return true;
}

static km_result_t validate_setup(uint32_t lane, const km_resource_claim_t *claims,
                                  size_t count, km_pwm_bridge_config_t *limits,
                                  uint32_t *cycle_time)
{
#if CONFIG_HARD_PWM_LOCAL_MAX_PAIRS < CONFIG_KLIPPER_MMU_LANE_COUNT
    // The whole configured rewind topology must fit in the shared driver.
    return KM_ERR_NOT_SUPPORTED;
#endif
    if (!limits || !cycle_time) return KM_ERR_INVALID_PARAM;
#if defined(CONFIG_WANT_GPIO_LOCAL) && CONFIG_WANT_GPIO_LOCAL
    if (claims == NULL) {
        if (count != 0) return KM_ERR_INVALID_PARAM;
    } else if (count == 0 || count > KM_RESOURCE_MAX_CLAIMS) {
        return KM_ERR_INVALID_PARAM;
    }
#else
    if (!claims || !count || count > KM_RESOURCE_MAX_CLAIMS)
        return KM_ERR_INVALID_PARAM;
#endif
    km_result_t result;
#if defined(CONFIG_WANT_GPIO_LOCAL) && CONFIG_WANT_GPIO_LOCAL
    if (claims != NULL) {
#endif
    result = km_resource_plan_validate(claims, count, NULL);
    if (result != KM_OK) return result;
#if defined(CONFIG_WANT_GPIO_LOCAL) && CONFIG_WANT_GPIO_LOCAL
    }
#endif
    result = km_config_pins_view(&saved_pins, NULL);
    if (result != KM_OK) return result;
    /* Shared structural preflight: powered pairs and translated routes. */
    result = km_config_h723_resources(&expected_plan, NULL);
    if (result != KM_OK) return result;
#if defined(CONFIG_WANT_GPIO_LOCAL) && CONFIG_WANT_GPIO_LOCAL
    if (claims != NULL) {
#endif
    if (!contains_expected(claims, count)) return KM_ERR_CONFLICT;
#if defined(CONFIG_WANT_GPIO_LOCAL) && CONFIG_WANT_GPIO_LOCAL
    }
#endif
    result = km_config_rewind(limits);
    if (result != KM_OK) return result;
    if (km_pwm_bridge_config_validate(limits) != KM_OK)
        return KM_ERR_INVALID_PARAM;
    result = km_config_rewind_outputs(saved_outputs);
    if (result != KM_OK) return result;
    uint32_t f = saved_pins->pin[KM_PIN_REWIND_FORWARD][lane];
    uint32_t r = saved_pins->pin[KM_PIN_REWIND_REVERSE][lane];
    uint32_t e = saved_pins->pin[KM_PIN_REWIND_ENABLE][lane];
    if (f >= 128u || r >= 128u || e >= 128u || f == r || f == e || r == e)
        return KM_ERR_INVALID_PARAM;
    uint32_t hz = CONFIG_CLOCK_FREQ;
    uint32_t cycle_us = saved_outputs[lane].cycle_us;
    if (!cycle_us || hz < 1000000u || hz % 1000000u ||
        cycle_us > UINT32_MAX / (hz / 1000000u))
        return KM_ERR_INVALID_PARAM;
    *cycle_time = cycle_us * (hz / 1000000u);
    return KM_OK;
}

KM_API km_result_t km_pwm_bridge_gpio_setup(uint32_t lane,
    const km_resource_claim_t *complete_claims, size_t claim_count,
    bool (*ready)(void *), km_fault_t (*fault)(void *), void *context)
{
    if (!task_irqs_enabled() || lane >= KM_PWM_BRIDGE_MAX_SLOTS || !ready)
        return KM_ERR_INVALID_PARAM;
    if (global_shutdown_latched || sched_is_shutdown()) return KM_ERR_FAULTED;
    enum km_clock_state state = km_clock_get_state();
    if (state == KM_CLOCK_STATE_UNAVAILABLE) return KM_ERR_UNAVAILABLE;
    if (state != KM_CLOCK_STATE_READY) return KM_ERR_FAULTED;
    if (slots[lane].claimed) return KM_ERR_BUSY;
    km_pwm_bridge_config_t limits;
    uint32_t cycle_time;
    km_result_t result = validate_setup(lane, complete_claims, claim_count,
                                        &limits, &cycle_time);
    if (result != KM_OK) return result;
#if CONFIG_WANT_GPIO_LOCAL
    /* Forward, reverse and ENABLE must already be local before ENABLE setup. */
    if (!gpio_pin_is_local(saved_pins->pin[KM_PIN_REWIND_FORWARD][lane])
        || !gpio_pin_is_local(saved_pins->pin[KM_PIN_REWIND_REVERSE][lane])
        || !gpio_pin_is_local(saved_pins->pin[KM_PIN_REWIND_ENABLE][lane]))
        return KM_ERR_CONFLICT;
#endif
    irqstatus_t irq = irq_save();
    if (global_shutdown_latched || sched_is_shutdown() || slots[lane].claimed) {
        irq_restore(irq);
        return KM_ERR_FAULTED;
    }
    pwm_slot_t *s = &slots[lane];
    s->claimed = true;
    s->forward_pin = saved_pins->pin[KM_PIN_REWIND_FORWARD][lane];
    s->reverse_pin = saved_pins->pin[KM_PIN_REWIND_REVERSE][lane];
    s->enable_pin = saved_pins->pin[KM_PIN_REWIND_ENABLE][lane];
    s->enable_active_high = saved_outputs[lane].enable_active_high;
    s->user_ready = ready;
    s->user_fault = fault;
    s->user_context = context;
    irq_restore(irq);

    // First physical action: make the separate motor ENABLE inactive.
#if CONFIG_WANT_GPIO_LOCAL
    s->enable_gpio = gpio_out_setup_local(s->enable_pin,
                                          s->enable_active_high ? 0u : 1u);
#else
    s->enable_gpio = gpio_out_setup(s->enable_pin,
                                    s->enable_active_high ? 0u : 1u);
#endif
    s->enable_touched = true;
    enable_off(s);
    if (global_shutdown_latched || sched_is_shutdown()) {
        latch_off(s);
        return KM_ERR_FAULTED;
    }
    struct gpio_pwm_pair_config pair_config = {
        .forward_pin=s->forward_pin, .reverse_pin=s->reverse_pin,
        .forward_active_low=!saved_outputs[lane].forward_active_high,
        .reverse_active_low=!saved_outputs[lane].reverse_active_high,
        .cycle_time=cycle_time,
        .raw_force_off=enable_off, .raw_force_off_context=s,
    };
    int pair_result = gpio_pwm_pair_setup(&pair_config, &s->pair);
    if (pair_result != GPIO_PWM_PAIR_OK || global_shutdown_latched ||
        sched_is_shutdown() || s->raw_latched) {
        latch_off(s);
        if (pair_result == GPIO_PWM_PAIR_ERR_BUSY) return KM_ERR_CONFLICT;
        if (pair_result == GPIO_PWM_PAIR_ERR_NOT_SUPPORTED) return KM_ERR_NOT_SUPPORTED;
        if (pair_result == GPIO_PWM_PAIR_ERR_INVALID) return KM_ERR_INVALID_PARAM;
        return KM_ERR_FAULTED;
    }
    uint32_t completed;
    if (km_clock_get_completion_us(&completed) != KM_OK) {
        latch_off(s);
        return KM_ERR_FAULTED;
    }
    s->last_completion_us = completed;
    result = km_pwm_bridge_init(&s->driver, &limits, &bridge_io, s);
    if (result != KM_OK || s->driver.latched_fault != KM_FAULT_NONE ||
        !live(s)) {
        latch_off(s);
        return KM_ERR_FAULTED;
    }
    irq = irq_save();
    if (global_shutdown_latched || sched_is_shutdown() || !live(s)) {
        irq_restore(irq);
        latch_off(s);
        return KM_ERR_FAULTED;
    }
    s->published = true;
    irq_restore(irq);
    return KM_OK;
}

KM_API km_driver_t *km_pwm_bridge_gpio_get_driver(uint32_t lane)
{
    if (lane >= KM_PWM_BRIDGE_MAX_SLOTS) return NULL;
    pwm_slot_t *s = &slots[lane];
    return s->published && live(s) ? &s->driver.base : NULL;
}

KM_API void km_pwm_bridge_gpio_service(uint32_t lane)
{
    if (lane < KM_PWM_BRIDGE_MAX_SLOTS && slots[lane].published &&
        !slots[lane].raw_latched && !global_shutdown_latched &&
        !sched_is_shutdown())
        km_pwm_bridge_service(&slots[lane].driver);
}

KM_API bool km_pwm_bridge_gpio_is_slot_active(uint32_t lane)
{
    return km_pwm_bridge_gpio_get_driver(lane) != NULL;
}

KM_API bool km_pwm_bridge_gpio_is_shutdown(void)
{
    return global_shutdown_latched || sched_is_shutdown();
}

KM_API km_result_t km_pwm_bridge_gpio_get_slot_pins(uint32_t lane,
    uint32_t *forward, uint32_t *reverse, uint32_t *enable)
{
    if (lane >= KM_PWM_BRIDGE_MAX_SLOTS || !forward || !reverse || !enable)
        return KM_ERR_INVALID_PARAM;
    pwm_slot_t *s = &slots[lane];
    if (!s->claimed) return KM_ERR_UNAVAILABLE;
    *forward = s->forward_pin;
    *reverse = s->reverse_pin;
    *enable = s->enable_pin;
    return KM_OK;
}

KM_API void km_pwm_bridge_gpio_force_off(uint32_t lane)
{
    if (lane >= KM_PWM_BRIDGE_MAX_SLOTS) return;
    irqstatus_t irq = irq_save();
    latch_off(&slots[lane]);
    irq_restore(irq);
}

void km_pwm_bridge_gpio_shutdown(void)
{
    irqstatus_t irq = irq_save();
    global_shutdown_latched = true;
    for (unsigned i = 0; i < KM_PWM_BRIDGE_MAX_SLOTS; ++i)
        latch_off(&slots[i]);
    gpio_pwm_pair_shutdown();
    irq_restore(irq);
}
DECL_SHUTDOWN(km_pwm_bridge_gpio_shutdown);
