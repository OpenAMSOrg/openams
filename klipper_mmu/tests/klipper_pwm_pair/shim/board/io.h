// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef PAIR_TEST_IO_H
#define PAIR_TEST_IO_H
#include <stdint.h>
void writel(void *addr, uint32_t value);
uint32_t readl(const void *addr);
#endif
