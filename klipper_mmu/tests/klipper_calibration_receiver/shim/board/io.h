// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include <stdint.h>
static inline uint8_t readb(const uint8_t *p) { return *p; }
static inline void writeb(uint8_t *p, uint8_t v) { *p = v; }
