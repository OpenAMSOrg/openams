// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef COMPOSITION_GPIO_H
#define COMPOSITION_GPIO_H
#include <stdint.h>
struct gpio_out { uint32_t pin; };
struct gpio_in { uint32_t encoded; };
struct gpio_out gpio_out_setup(uint32_t pin, uint32_t value);
void gpio_out_write(struct gpio_out out, uint32_t value);
#endif
