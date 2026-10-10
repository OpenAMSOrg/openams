/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef PIN_ADC_MISC_H
#define PIN_ADC_MISC_H
#include <stdint.h>
uint32_t timer_read_time(void);
uint32_t timer_from_us(uint32_t us);
static inline int timer_is_before(uint32_t a, uint32_t b)
{
    return (int32_t)(a - b) < 0;
}
#endif
