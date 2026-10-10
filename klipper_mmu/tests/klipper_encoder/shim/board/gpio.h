// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef TEST_ENCODER_GPIO_H
#define TEST_ENCODER_GPIO_H
#include <stdint.h>
struct gpio_in { uint32_t encoded; };
struct gpio_in gpio_in_setup(uint32_t pin, int32_t pull_up);
uint8_t gpio_in_read(struct gpio_in g);
#endif
