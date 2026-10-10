// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef TEST_STM32_ADC_LOCAL_H
#define TEST_STM32_ADC_LOCAL_H

#include "board/gpio.h"
#include <stdint.h>

/* Mock stand-in for the shared local-pair status codes. Not register proof. */
#define GPIO_ADC_LOCAL_OK 0
#define GPIO_ADC_LOCAL_ERR_INVALID (-1)
#define GPIO_ADC_LOCAL_ERR_BUSY (-2)
#define GPIO_ADC_LOCAL_ERR_SHUTDOWN (-3)
#define GPIO_ADC_LOCAL_ERR_TIMEOUT (-4)

int gpio_adc_setup_local_pair(uint32_t pin_a, uint32_t pin_b,
                              struct gpio_adc *out_a, struct gpio_adc *out_b);
/* Immediate mock entry. Not hardware deadline proof. */
int gpio_adc_setup_local_pair_timeout(uint32_t pin_a, uint32_t pin_b, uint32_t timeout_us, struct gpio_adc *out_a, struct gpio_adc *out_b);

#endif
