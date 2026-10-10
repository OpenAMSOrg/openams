// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include <stddef.h>
#include <stdint.h>
void *dynmem_start(void);
void *dynmem_end(void);
uint16_t crc16_ccitt(uint8_t *, uint_fast8_t);
uint32_t timer_read_time(void);
uint32_t timer_from_us(uint32_t);
