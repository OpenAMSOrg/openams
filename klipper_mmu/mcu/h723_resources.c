// SPDX-License-Identifier: GPL-3.0-only
#include "h723_resources.h"
#include "autoconf.h"
#include <string.h>

#if CONFIG_MACH_STM32H723 && CONFIG_WANT_ADC && CONFIG_WANT_ADC_LOCAL
#include "stm32/adc_local.h"
#endif
#if CONFIG_MACH_STM32H723 && CONFIG_WANT_HARD_PWM_QUERY
#include "stm32/internal.h" // CMSIS timer identities; no register accesses
#include "stm32/pwm.h" // shared Klipper route lookup

// Translate opaque upstream identities into this plan's numeric resource IDs.
// Pin routing and alternate-function selection belong to Klipper's driver.
static const TIM_TypeDef *const timers[18] = {
    [1]=TIM1, [2]=TIM2, [3]=TIM3, [4]=TIM4, [5]=TIM5, [8]=TIM8,
    [12]=TIM12, [13]=TIM13, [14]=TIM14, [15]=TIM15, [16]=TIM16, [17]=TIM17,
};
#endif

km_result_t km_h723_pwm_route(uint32_t pin, km_h723_pwm_route_t *out)
{
    if (!out) return KM_ERR_INVALID_PARAM;
#if CONFIG_MACH_STM32H723 && CONFIG_WANT_HARD_PWM_QUERY
    struct gpio_pwm_route route;
    if (!gpio_pwm_get_route(pin, &route) || !route.channel || route.channel > 4u)
        return KM_ERR_NOT_SUPPORTED;
    for (uint8_t timer = 1; timer < 18u; ++timer) {
        if (timers[timer] && route.timer_id == (uintptr_t)timers[timer]) {
            *out = (km_h723_pwm_route_t){timer, route.channel};
            return KM_OK;
        }
    }
    return KM_ERR_NOT_SUPPORTED;
#else
    (void)pin;
    return KM_ERR_UNAVAILABLE;
#endif
}

#if CONFIG_KLIPPER_MMU_PIN_CONFIG && ((CONFIG_KLIPPER_MMU_POWERED_REWIND \
        && CONFIG_MACH_STM32H723 && CONFIG_WANT_HARD_PWM_QUERY && CONFIG_WANT_HARD_PWM_LOCAL_PAIR) \
    || (CONFIG_KLIPPER_MMU_DIFF_BUFFER && CONFIG_MACH_STM32H723 && CONFIG_WANT_ADC && CONFIG_WANT_ADC_LOCAL))
static uint16_t pin_index(const km_pin_config_t *pins, uint16_t pin)
{
    for (size_t i = 0; i < pins->count; ++i)
        if (pins->claims[i].instance == pin) return (uint16_t)i;
    return KM_RESOURCE_NO_INDEX;
}
#endif

km_result_t km_config_h723_resources(km_h723_resource_plan_t *out,
                                     km_resource_diagnostic_t *diagnostic)
{
    km_resource_diagnostic_t d = {KM_RESOURCE_DIAG_BAD_ARGUMENT,
                                 KM_RESOURCE_NO_INDEX, KM_RESOURCE_NO_INDEX};
    if (diagnostic) *diagnostic = d;
    if (!out) return KM_ERR_INVALID_PARAM;
    out->count = 0;
#if !CONFIG_KLIPPER_MMU_PIN_CONFIG
    d.reason = KM_RESOURCE_DIAG_NONE;
    if (diagnostic) *diagnostic = d;
    return KM_ERR_UNAVAILABLE;
#else
    const km_pin_config_t *pins;
    km_result_t result = km_config_pins_view(&pins, &d);
    if (result != KM_OK) {
        if (diagnostic) *diagnostic = d;
        return result;
    }
    /* No speculative acquisition or interrupt ownership for unimplemented
     * physical backends. Pin configuration alone does not implement them.
     * The encoder now has a real GPIO driver (mcu/encoder.c) that owns its
     * own pin(s) with no ADC/timer routing to resolve here, so it reaches
     * this plan like any other GPIO-only capability; current sensing still
     * has no driver and stays refused. */
#if CONFIG_KLIPPER_MMU_MEASURED_CURRENT
    if (diagnostic) *diagnostic = d;
    return KM_ERR_NOT_SUPPORTED;
#endif
    _Static_assert(KM_PIN_CONFIG_MAX_CLAIMS + 2u * KM_MAX_LANES + 3u
                   <= KM_RESOURCE_MAX_CLAIMS, "H723 plan capacity");
    size_t count = pins->count;
    memcpy(out->claims, pins->claims, count * sizeof(pins->claims[0]));
    memset(out->rewind, 0, sizeof(out->rewind));
#if CONFIG_KLIPPER_MMU_POWERED_REWIND
#if CONFIG_MACH_STM32H723 && CONFIG_WANT_HARD_PWM_QUERY && CONFIG_WANT_HARD_PWM_LOCAL_PAIR
    bool timer_seen[18] = {false};
    for (unsigned lane = 0; lane < CONFIG_KLIPPER_MMU_LANE_COUNT; ++lane) {
        int pair_status = gpio_pwm_pair_validate_pins(
            pins->pin[KM_PIN_REWIND_FORWARD][lane],
            pins->pin[KM_PIN_REWIND_REVERSE][lane]);
        if (pair_status != GPIO_PWM_PAIR_OK) {
            d.reason = KM_RESOURCE_DIAG_BAD_CLAIM;
            d.first_index = pin_index(pins, pins->pin[KM_PIN_REWIND_FORWARD][lane]);
            d.second_index = pin_index(pins, pins->pin[KM_PIN_REWIND_REVERSE][lane]);
            result = pair_status == GPIO_PWM_PAIR_ERR_INVALID
                ? KM_ERR_INVALID_PARAM : KM_ERR_NOT_SUPPORTED;
            goto failure;
        }
        for (unsigned direction = 0; direction < 2; ++direction) {
            enum km_pin_role role = direction ? KM_PIN_REWIND_REVERSE : KM_PIN_REWIND_FORWARD;
            uint16_t pin = pins->pin[role][lane];
            km_h723_pwm_route_t *route = &out->rewind[lane][direction];
            result = km_h723_pwm_route(pin, route);
            if (result != KM_OK) {
                d.reason = KM_RESOURCE_DIAG_BAD_CLAIM;
                d.first_index = pin_index(pins, pin);
                goto failure;
            }
            for (unsigned earlier = 0; earlier < 2u * lane + direction; ++earlier) {
                const km_h723_pwm_route_t *other = &out->rewind[earlier / 2u][earlier % 2u];
                if (route->timer != other->timer) continue;
                /* UG updates the whole timer: separate lanes cannot share it.
                 * A lane's two directions may share distinct channels. */
                if (earlier / 2u == lane && route->channel != other->channel)
                    continue;
                enum km_pin_role previous = earlier % 2u ? KM_PIN_REWIND_REVERSE : KM_PIN_REWIND_FORWARD;
                d = (km_resource_diagnostic_t){KM_RESOURCE_DIAG_DUPLICATE_RESOURCE,
                    pin_index(pins, pins->pin[previous][earlier / 2u]), pin_index(pins, pin)};
                result = KM_ERR_CONFLICT;
                goto failure;
            }
            if (!timer_seen[route->timer]) {
                timer_seen[route->timer] = true;
                out->claims[count++] = (km_resource_claim_t){
                    .kind=KM_RESOURCE_TIMER_EXCLUSIVE, .instance=route->timer,
                    .owner=KM_H723_OWNER_REWIND_TIMER};
            }
        }
    }
#else
    d.reason = KM_RESOURCE_DIAG_NONE;
    result = KM_ERR_UNAVAILABLE;
    goto failure;
#endif
#endif
#if CONFIG_KLIPPER_MMU_DIFF_BUFFER
#if CONFIG_MACH_STM32H723 && CONFIG_WANT_ADC && CONFIG_WANT_ADC_LOCAL
    struct gpio_adc_route route_a, route_b;
    uint16_t pin_a = pins->pin[KM_PIN_BUFFER_A][0];
    uint16_t pin_b = pins->pin[KM_PIN_BUFFER_B][0];
    if (gpio_adc_get_route(pin_a, &route_a) != 1 || gpio_adc_get_route(pin_b, &route_b) != 1
        || route_a.unit != route_b.unit || route_a.channel == route_b.channel) {
        d.reason = KM_RESOURCE_DIAG_BAD_CLAIM;
        d.first_index = pin_index(pins, pin_a);
        d.second_index = pin_index(pins, pin_b);
        result = KM_ERR_INVALID_PARAM;
        goto failure;
    }
    out->claims[count++] = (km_resource_claim_t){
        .kind = KM_RESOURCE_ADC_UNIT, .instance = route_a.unit,
        .owner = KM_H723_OWNER_DIFF_ADC};
    out->claims[count++] = (km_resource_claim_t){
        .kind = KM_RESOURCE_ADC_CHANNEL, .instance = route_a.unit,
        .subresource = route_a.channel, .owner = KM_H723_OWNER_DIFF_ADC};
    out->claims[count++] = (km_resource_claim_t){
        .kind = KM_RESOURCE_ADC_CHANNEL, .instance = route_b.unit,
        .subresource = route_b.channel, .owner = KM_H723_OWNER_DIFF_ADC};
#else
    d.reason = KM_RESOURCE_DIAG_NONE;
    result = KM_ERR_UNAVAILABLE;
    goto failure;
#endif
#endif
    result = km_resource_plan_validate(out->claims, count, &d);
    if (result == KM_OK) out->count = count;
#if CONFIG_KLIPPER_MMU_POWERED_REWIND || CONFIG_KLIPPER_MMU_DIFF_BUFFER
failure:
#endif
    if (diagnostic) *diagnostic = d;
    return result;
#endif
}
