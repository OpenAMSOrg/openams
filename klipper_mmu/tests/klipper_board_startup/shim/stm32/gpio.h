/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef TEST_SHIM_STM32_GPIO_H
#define TEST_SHIM_STM32_GPIO_H
#include <stdint.h>
struct gpio_adc { uint32_t pin; };
uint32_t gpio_adc_sample(struct gpio_adc adc);
uint16_t gpio_adc_read(struct gpio_adc adc);
void gpio_adc_cancel_sample(struct gpio_adc adc);
#endif
