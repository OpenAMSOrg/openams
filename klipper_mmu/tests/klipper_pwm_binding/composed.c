// SPDX-License-Identifier: GPL-3.0-or-later
/* One composed native lifecycle: portable bridge -> binding -> installed pair API.
 * The product paired driver and its shared register harness remain separate.
 */
#include "pwm_bridge_gpio.h"
#include "clock.h"
#include "h723_resources.h"
#include "pin_config.h"
#include "rewind_config.h"
#include "stm32/pwm.h"
#include "gpio.h"
#include <assert.h>
#include <string.h>

static uint32_t now;
static km_resource_claim_t claim = {.kind=KM_RESOURCE_PIN,.instance=1,.owner=1};
static struct gpio_pwm_pair *captured_pair;
static unsigned enable_setups, enable_low_writes, enable_high_writes;

struct gpio_out __real_gpio_out_setup(uint32_t pin, uint32_t value);
int __real_gpio_pwm_pair_setup(const struct gpio_pwm_pair_config *config,
                               struct gpio_pwm_pair **out_pair);

struct gpio_out __wrap_gpio_out_setup(uint32_t pin, uint32_t value)
{
    if (pin == 4u) {
        enable_setups++;
        assert(value == 0u);
    }
    return __real_gpio_out_setup(pin, value);
}

int __wrap_gpio_pwm_pair_setup(const struct gpio_pwm_pair_config *config,
                               struct gpio_pwm_pair **out_pair)
{
    int result = __real_gpio_pwm_pair_setup(config, out_pair);
    if (result == GPIO_PWM_PAIR_OK) captured_pair = *out_pair;
    return result;
}

void gpio_out_write(struct gpio_out output, uint32_t value)
{
    (void)output;
    if (value) enable_high_writes++; else enable_low_writes++;
}

enum km_clock_state km_clock_get_state(void) { return KM_CLOCK_STATE_READY; }
km_result_t km_clock_get_completion_us(uint32_t *out)
{
    if (!out) return KM_ERR_INVALID_PARAM;
    *out=now;
    return KM_OK;
}
km_result_t km_resource_plan_validate(const km_resource_claim_t *p,size_t n,km_resource_diagnostic_t *d)
{
    (void)p; (void)n;
    if(d)*d=(km_resource_diagnostic_t){0};
    return KM_OK;
}
static km_pin_config_t configured_pins;
km_result_t km_config_pins_view(const km_pin_config_t **out,km_resource_diagnostic_t *d)
{
    if(d)*d=(km_resource_diagnostic_t){0};
    if(!out)return KM_ERR_INVALID_PARAM;
    memset(&configured_pins,0xff,sizeof(configured_pins)); configured_pins.count=1;
    configured_pins.pin[KM_PIN_REWIND_FORWARD][0]=0;
    configured_pins.pin[KM_PIN_REWIND_REVERSE][0]=2;
    configured_pins.pin[KM_PIN_REWIND_ENABLE][0]=4;
    *out=&configured_pins;
    return KM_OK;
}
km_result_t km_config_pins(km_pin_config_t *out,km_resource_diagnostic_t *d)
{
    if(d)*d=(km_resource_diagnostic_t){0};
    if(!out)return KM_ERR_INVALID_PARAM;
    const km_pin_config_t *view;
    km_result_t result=km_config_pins_view(&view,NULL);
    if(result==KM_OK)*out=*view;
    return result;
}
km_result_t km_config_h723_resources(km_h723_resource_plan_t *out,km_resource_diagnostic_t *d)
{
    if(d)*d=(km_resource_diagnostic_t){0};
    memset(out,0,sizeof(*out)); out->claims[0]=claim; out->count=1;
    out->rewind[0][0]=(km_h723_pwm_route_t){2,1};
    out->rewind[0][1]=(km_h723_pwm_route_t){2,3};
    return KM_OK;
}
km_result_t km_config_rewind(km_pwm_bridge_config_t *out)
{
    *out=(km_pwm_bridge_config_t){1000,10,100};
    return KM_OK;
}
km_result_t km_config_rewind_outputs(km_rewind_output_config_t out[KM_MAX_LANES])
{
    memset(out,0,sizeof(km_rewind_output_config_t)*KM_MAX_LANES);
    out[0]=(km_rewind_output_config_t){100,true,true,true};
    return KM_OK;
}
static bool ready(void *p) { (void)p; return true; }
static km_fault_t fault(void *p) { (void)p; return KM_FAULT_NONE; }

static struct gpio_pwm_pair_info info(void)
{
    struct gpio_pwm_pair_info result;
    assert(captured_pair);
    assert(gpio_pwm_pair_get_info(captured_pair, &result) == GPIO_PWM_PAIR_OK);
    assert(result.is_ready);
    return result;
}

int main(void)
{
    assert(km_pwm_bridge_gpio_setup(0,&claim,1,ready,fault,0)==KM_OK);
    km_driver_t *d=km_pwm_bridge_gpio_get_driver(0);
    assert(d && captured_pair && enable_setups == 1u && enable_low_writes);
    struct gpio_pwm_pair_info state = info();
    assert(!state.confirmed_forward_counts && !state.confirmed_reverse_counts);

    now=10; assert(km_driver_set_enable(d,true)==KM_OK && enable_high_writes == 1u);
    now=20; assert(km_driver_set_direction(d,KM_DIR_FORWARD,now)==KM_OK);
    now=30; assert(km_driver_set_pwm_duty(d,500)==KM_OK);
    state = info();
    assert(state.hwpwm_ticks == 26000u && state.confirmed_forward_counts == 13000u
           && !state.confirmed_reverse_counts);

    now=40; assert(km_driver_set_pwm_duty(d,0)==KM_OK);
    state = info();
    assert(!state.confirmed_forward_counts && !state.confirmed_reverse_counts);
    now=60; assert(km_driver_set_direction(d,KM_DIR_REVERSE,now)==KM_OK);
    now=70; assert(km_driver_set_pwm_duty(d,500)==KM_OK);
    state = info();
    assert(state.reverse_hwpwm_ticks == 26000u && !state.confirmed_forward_counts
           && state.confirmed_reverse_counts == 13000u);

    unsigned lows_before_disable = enable_low_writes;
    assert(km_driver_set_enable(d,false)==KM_OK && enable_low_writes > lows_before_disable);
    state = info();
    assert(state.is_ready && !state.confirmed_forward_counts && !state.confirmed_reverse_counts);
    now=90; assert(km_driver_set_enable(d,true)==KM_OK && enable_high_writes == 2u);
    assert(gpio_pwm_pair_is_ready(captured_pair));
    return 0;
}
