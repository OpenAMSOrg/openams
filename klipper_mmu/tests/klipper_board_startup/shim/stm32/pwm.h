// SPDX-License-Identifier: GPL-3.0-only
#ifndef TEST_SHIM_STM32_PWM_H
#define TEST_SHIM_STM32_PWM_H
#include <stdint.h>

struct gpio_pwm_route {
    uintptr_t timer_id;
    uint8_t channel;
};

enum {
    GPIO_PWM_PAIR_OK = 0,
    GPIO_PWM_PAIR_ERR_INVALID = -1,
    GPIO_PWM_PAIR_ERR_NOT_SUPPORTED = -3
};

int gpio_pwm_get_route(uint32_t pin, struct gpio_pwm_route *out);
int gpio_pwm_pair_validate_pins(uint32_t forward_pin, uint32_t reverse_pin);
#endif
