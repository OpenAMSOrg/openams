// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef TEST_SHIM_STM32_GPIO_LOCAL_H
#define TEST_SHIM_STM32_GPIO_LOCAL_H

#include <stdint.h>
#include "board/gpio.h"

enum {
    GPIO_LOCAL_OK = 0,
    GPIO_LOCAL_ERR_INVALID = -1,
    GPIO_LOCAL_ERR_BUSY = -2,
    GPIO_LOCAL_ERR_SHUTDOWN = -3
};

int gpio_local_reserve(const uint32_t *pins, uint32_t count);
struct gpio_in gpio_in_setup_local(uint32_t pin, int32_t pull_up);
struct gpio_out gpio_out_setup_local(uint32_t pin, uint32_t val);
int gpio_pin_is_local(uint32_t pin);

#endif
