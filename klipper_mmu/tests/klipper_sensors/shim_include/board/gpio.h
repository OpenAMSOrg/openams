// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef TEST_SENSORS_GPIO_H
#define TEST_SENSORS_GPIO_H
#include <stdint.h>
struct gpio_adc { void *adc; uint32_t chan; };
struct gpio_adc gpio_adc_setup(uint32_t pin);
uint32_t gpio_adc_sample(struct gpio_adc g);
uint16_t gpio_adc_read(struct gpio_adc g);
void gpio_adc_cancel_sample(struct gpio_adc g);
struct gpio_in { uint32_t encoded; };
struct gpio_in gpio_in_setup(uint32_t pin, int32_t pull);
uint8_t gpio_in_read(struct gpio_in g);
#endif
