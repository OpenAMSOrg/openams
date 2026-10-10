// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef PAIR_TEST_GPIO_H
#define PAIR_TEST_GPIO_H
#include <stdint.h>
struct gpio_out { void *regs; uint32_t bit; };
struct gpio_pwm { void *reg; uint32_t hwpwm_ticks; };
struct gpio_out gpio_out_setup(uint32_t pin, uint32_t val);
void gpio_peripheral(uint32_t pin, uint32_t function, int pull_up);
struct gpio_pwm gpio_pwm_setup(uint8_t pin, uint32_t cycle_time, uint32_t val);
void gpio_pwm_write(struct gpio_pwm g, uint32_t val);
#endif
