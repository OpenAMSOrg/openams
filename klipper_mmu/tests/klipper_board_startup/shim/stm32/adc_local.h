// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef TEST_SHIM_STM32_ADC_LOCAL_H
#define TEST_SHIM_STM32_ADC_LOCAL_H
#include "gpio.h"
struct gpio_adc_route { uint8_t unit; uint8_t channel; };
enum { GPIO_ADC_LOCAL_OK = 0, GPIO_ADC_LOCAL_ERR_INVALID = -1,
       GPIO_ADC_LOCAL_ERR_BUSY = -2, GPIO_ADC_LOCAL_ERR_SHUTDOWN = -3,
       GPIO_ADC_LOCAL_ERR_TIMEOUT = -4 };
int gpio_adc_get_route(uint32_t pin, struct gpio_adc_route *out);
int gpio_adc_setup_local_pair_timeout(uint32_t pin_a, uint32_t pin_b,
                                      uint32_t timeout_us,
                                      struct gpio_adc *out_a,
                                      struct gpio_adc *out_b);
#endif
