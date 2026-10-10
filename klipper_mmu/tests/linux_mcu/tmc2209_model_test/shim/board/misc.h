// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef SHIM_BOARD_MISC_H
#define SHIM_BOARD_MISC_H
#include <stdint.h>
uint8_t timer_is_before(uint32_t time1, uint32_t time2);
uint32_t timer_read_time(void);
#endif
