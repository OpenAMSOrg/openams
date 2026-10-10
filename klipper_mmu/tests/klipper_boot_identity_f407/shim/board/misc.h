// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include <stdint.h>
uint32_t timer_read_time(void);
uint32_t timer_from_us(uint32_t us);
uint8_t timer_is_before(uint32_t a, uint32_t b);
