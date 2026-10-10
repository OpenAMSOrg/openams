// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef TEST_BOARD_MISC_H
#define TEST_BOARD_MISC_H

#include <stdint.h>
#include <stdbool.h>

uint32_t timer_read_time(void);
uint32_t timer_from_us(uint32_t us);
bool timer_is_before(uint32_t time1, uint32_t time2);


#ifndef container_of
#define container_of(ptr, type, member)     ((type *)((char *)(ptr) - offsetof(type, member)))
#endif
#ifndef ARRAY_SIZE
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
#endif
#ifndef DIV_ROUND_UP
#define DIV_ROUND_UP(n, d) (((n) + (d) - 1) / (d))
#endif
#ifndef likely
#define likely(x) __builtin_expect(!!(x), 1)
#endif
#ifndef unlikely
#define unlikely(x) __builtin_expect(!!(x), 0)
#endif

#endif
