// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef PAIR_BINDING_GPIO_H
#define PAIR_BINDING_GPIO_H
#include <stdint.h>
struct gpio_out { uint32_t pin; };
struct gpio_out gpio_out_setup(uint32_t pin, uint32_t value);
void gpio_out_write(struct gpio_out output, uint32_t value);
#endif
