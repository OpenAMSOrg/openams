// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef BOARD_GPIO_H
#define BOARD_GPIO_H

#include <stdint.h>
#include <stdbool.h>

struct gpio_out {
    uint32_t pin;
};

struct gpio_in {
    uint32_t pin;
};

struct gpio_out gpio_out_setup(uint32_t pin, uint32_t val);
void gpio_out_reset(struct gpio_out g, uint32_t val);
void gpio_out_write(struct gpio_out g, uint32_t val);
void gpio_out_toggle_noirq(struct gpio_out g);
void gpio_out_toggle(struct gpio_out g);

struct gpio_in gpio_in_setup(uint32_t pin, int32_t pull_up);
void gpio_in_reset(struct gpio_in g, int32_t pull_up);
uint8_t gpio_in_read(struct gpio_in g);

#endif
