// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef BOARD_GPIO_H
#define BOARD_GPIO_H

#include <stdint.h>

struct gpio_out {
    uint32_t pin;
};

struct gpio_out gpio_out_setup(uint32_t pin, uint32_t val);
void gpio_out_reset(struct gpio_out g, uint32_t val);
void gpio_out_write(struct gpio_out g, uint32_t val);
void gpio_out_toggle_noirq(struct gpio_out g);
void gpio_out_toggle(struct gpio_out g);

#endif
