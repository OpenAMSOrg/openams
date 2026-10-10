// SPDX-License-Identifier: GPL-3.0-or-later
/* Fixture query for prior local pins. Not a copy of the shared GPIO registry. */
#ifndef KLIPPER_PWM_BINDING_GPIO_LOCAL_H
#define KLIPPER_PWM_BINDING_GPIO_LOCAL_H

#include "board/gpio.h"

#include <stdint.h>

#if defined(KM_TEST_CONFIGURED_ADMISSION) && defined(CONFIG_WANT_GPIO_LOCAL) && CONFIG_WANT_GPIO_LOCAL
/* Modeled ownership boundary for saved rewind pins 0, 2, and 4. */
extern uint8_t km_test_local_pins[5];

static inline int gpio_pin_is_local(uint32_t pin)
{
    return pin < 5u && km_test_local_pins[pin] != 0u;
}

static inline struct gpio_out gpio_out_setup_local(uint32_t pin, uint32_t value)
{
    if (!gpio_pin_is_local(pin))
        return (struct gpio_out){0};
    return gpio_out_setup(pin, value);
}
#else
int gpio_pin_is_local(uint32_t pin);
struct gpio_out gpio_out_setup_local(uint32_t pin, uint32_t value);
#endif

#endif
