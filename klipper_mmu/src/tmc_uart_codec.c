// SPDX-License-Identifier: GPL-3.0-only
// Framing follows Klipper klippy/extras/tmc_uart.py _add_serial_bits(),
// Copyright (C) 2018-2021 Kevin O'Connor <kevin@koconnor.net>.
#include "klipper_mmu/tmc_uart_codec.h"
#include <string.h>

uint8_t km_tmc_uart_encode(const uint8_t *raw, uint8_t length, uint8_t encoded[10])
{
    if (!raw || !encoded || (length != 4u && length != 8u)) return 0;
    uint8_t candidate[10] = {0};
    for (unsigned i = 0; i < length; ++i) {
        const unsigned bit = i * 10u, at = bit / 8u, shift = bit % 8u;
        const uint32_t frame = ((uint32_t)raw[i] << 1u) | 0x200u;
        candidate[at] |= (uint8_t)(frame << shift);
        candidate[at + 1u] |= (uint8_t)(frame >> (8u - shift));
    }
    const uint8_t count = (uint8_t)((unsigned)length * 10u / 8u);
    memcpy(encoded, candidate, count);
    return count;
}

bool km_tmc_uart_decode(const uint8_t *encoded, uint8_t length, uint8_t raw[8])
{
    if (!encoded || !raw || length != 10u) return false;
    uint8_t candidate[8];
    for (unsigned i = 0; i < 8u; ++i) {
        const unsigned bit = i * 10u, at = bit / 8u, shift = bit % 8u;
        const uint32_t pair = (uint32_t)encoded[at] | ((uint32_t)encoded[at + 1u] << 8u);
        const uint32_t frame = pair >> shift;
        if ((frame & 0x201u) != 0x200u) return false;
        candidate[i] = (uint8_t)(frame >> 1u);
    }
    memcpy(raw, candidate, sizeof(candidate));
    return true;
}
