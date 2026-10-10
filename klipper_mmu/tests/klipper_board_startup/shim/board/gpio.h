// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef TEST_SHIM_GPIO_H
#define TEST_SHIM_GPIO_H

#include <stdint.h>

struct gpio_out { uint32_t pin; };
struct gpio_in { uint32_t encoded; };
struct gpio_in gpio_in_setup(uint32_t pin, int32_t pull_up);
uint8_t gpio_in_read(struct gpio_in in);
struct gpio_out gpio_out_setup(uint32_t pin, uint32_t val);
void gpio_out_write(struct gpio_out g, uint32_t val);
void gpio_out_toggle_noirq(struct gpio_out g);

#endif
