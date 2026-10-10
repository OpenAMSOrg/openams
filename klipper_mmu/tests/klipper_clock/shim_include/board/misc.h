// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef TEST_BOARD_MISC_H
#define TEST_BOARD_MISC_H

#include <stdint.h>

uint32_t timer_read_time(void);
uint32_t timer_from_us(uint32_t us);
uint8_t timer_is_before(uint32_t time1, uint32_t time2);

#endif
