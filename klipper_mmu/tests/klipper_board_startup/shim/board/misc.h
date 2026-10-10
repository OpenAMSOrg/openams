// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef TEST_SHIM_BOARD_MISC_H
#define TEST_SHIM_BOARD_MISC_H

#include <stdint.h>

uint32_t timer_read_time(void);
uint32_t timer_from_us(uint32_t us);
int timer_is_before(uint32_t a, uint32_t b);

#endif
