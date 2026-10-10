// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef TEST_BOARD_GPIO_H
#define TEST_BOARD_GPIO_H

#include <stdint.h>

struct gpio_adc {
    void *adc;
    uint32_t chan;
};

struct gpio_adc gpio_adc_setup(uint32_t pin);
uint32_t gpio_adc_sample(struct gpio_adc g);
uint16_t gpio_adc_read(struct gpio_adc g);
void gpio_adc_cancel_sample(struct gpio_adc g);

#endif
