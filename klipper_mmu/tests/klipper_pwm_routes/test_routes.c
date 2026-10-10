// SPDX-License-Identifier: GPL-3.0-or-later
// Exercise the actual upstream lookup and MMU resource adapter without I/O.
#include "autoconf.h"
#include "h723_resources.h"
#include "stm32/pwm.h"
#include "stm32/internal.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

TIM_TypeDef review_timers[18];
uint32_t get_pclock_frequency(uint32_t base) { (void)base; abort(); }
int is_enabled_pclock(uint32_t base) { (void)base; abort(); }
void enable_pclock(uint32_t base) { (void)base; abort(); }
void gpio_peripheral(uint32_t pin, uint32_t fn, int pull) {
    (void)pin; (void)fn; (void)pull; abort();
}
_Noreturn void shutdown(const char *message) { (void)message; abort(); }

#if CONFIG_WANT_HARD_PWM_QUERY
static int injected;
int __real_gpio_pwm_get_route(uint32_t, struct gpio_pwm_route *);
int __wrap_gpio_pwm_get_route(uint32_t pin, struct gpio_pwm_route *out)
{
    if (!injected) return __real_gpio_pwm_get_route(pin, out);
    *out = (struct gpio_pwm_route){(uintptr_t)TIM2, 1};
    switch (injected) {
    case 1: out->timer_id = 0; break;
    case 2: out->timer_id = (uintptr_t)&review_timers[6]; break;
    case 3: out->channel = 0; break;
    case 4: out->channel = 5; break;
    case 5: out->timer_id = (uintptr_t)TIM8; break;
    default: abort();
    }
    return 1;
}
#endif

static void check_pin(uint32_t pin)
{
    const km_h723_pwm_route_t sentinel = {99, 99};
    km_h723_pwm_route_t out = sentinel;
    assert(km_h723_pwm_route(pin, NULL) == KM_ERR_INVALID_PARAM);
    km_result_t r = km_h723_pwm_route(pin, &out);
#if CONFIG_WANT_HARD_PWM_QUERY
    struct gpio_pwm_route raw;
    if (__real_gpio_pwm_get_route(pin, &raw)) {
        assert(r == KM_OK && out.timer < 18 && out.channel > 0 && out.channel <= 4);
        assert(raw.timer_id == (uintptr_t)&review_timers[out.timer]);
        assert(raw.channel == out.channel);
        printf("%u %u %u\n", pin, out.timer, out.channel);
        return;
    }
    assert(r == KM_ERR_NOT_SUPPORTED);
#else
    assert(r == KM_ERR_UNAVAILABLE);
#endif
    assert(memcmp(&out, &sentinel, sizeof(out)) == 0);
}

int main(void)
{
    memset(review_timers, 0xa5, sizeof(review_timers));
    for (uint32_t pin = 0; pin <= UINT16_MAX; ++pin) check_pin(pin);
    const uint32_t oversized[] = {65536, 65537, UINT32_C(0x80000000), UINT32_MAX};
    for (unsigned i=0; i<sizeof(oversized)/sizeof(oversized[0]); ++i) check_pin(oversized[i]);
#if CONFIG_WANT_HARD_PWM_QUERY
    for (injected=1; injected<=5; ++injected) {
        km_h723_pwm_route_t out = {99, 99};
        km_result_t r = km_h723_pwm_route(0, &out);
        if (injected == 5) assert(r == KM_OK && out.timer == 8 && out.channel == 1);
        else assert(r == KM_ERR_NOT_SUPPORTED && out.timer == 99 && out.channel == 99);
    }
#endif
    const unsigned char *bytes = (const unsigned char *)review_timers;
    for (size_t i=0; i<sizeof(review_timers); ++i) assert(bytes[i] == 0xa5);
    return 0;
}
