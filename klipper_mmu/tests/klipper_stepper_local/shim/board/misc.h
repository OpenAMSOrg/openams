// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef BOARD_MISC_H
#define BOARD_MISC_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

uint32_t timer_read_time(void);
uint32_t timer_from_us(uint32_t us);
bool timer_is_before(uint32_t time1, uint32_t time2);

#define container_of(ptr, type, member) \
    ((type *)((char *)(ptr) - offsetof(type, member)))
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
#define DIV_ROUND_UP(n, d) (((n) + (d) - 1) / (d))
#define likely(x) __builtin_expect(!!(x), 1)
#define unlikely(x) __builtin_expect(!!(x), 0)

#endif
