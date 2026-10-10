// SPDX-License-Identifier: GPL-3.0-or-later
/* Route-fixture stand-in. The installed PWM driver includes this header when
 * local GPIO admission is selected. Route queries never call these functions. */
#ifndef PAIR_TEST_GPIO_LOCAL_H
#define PAIR_TEST_GPIO_LOCAL_H
#include <stdint.h>
/* The caller includes gpio.h first. This file must not include another
 * gpio.h: the ADC fixture and the PWM fixture define different structs. */

static inline int gpio_pin_is_local(uint32_t pin)
{
    (void)pin;
    return 1;
}

static inline struct gpio_out gpio_out_setup_local(uint32_t pin, uint32_t value)
{
    return gpio_out_setup(pin, value);
}

static inline void gpio_peripheral_local(uint32_t pin, uint32_t mode, int pull)
{
    gpio_peripheral(pin, mode, pull);
}
#endif
