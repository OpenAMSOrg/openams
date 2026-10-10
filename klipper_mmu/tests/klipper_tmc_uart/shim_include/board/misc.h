// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef BOARD_MISC_H
#define BOARD_MISC_H

#include <stdint.h>

uint32_t timer_read_time(void);
uint32_t timer_from_us(uint32_t us);

#endif
