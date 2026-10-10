// SPDX-License-Identifier: GPL-3.0-only
#ifndef KLIPPER_MMU_TMC_UART_CODEC_H
#define KLIPPER_MMU_TMC_UART_CODEC_H
#include "klipper_mmu/types.h"

#define KM_TMC_UART_ENCODED_MAX 10u

/* Add 8N1 framing to a raw 4-byte read request or 8-byte write/response.
 * Same packed, least-significant-bit-first format as Klipper's tmcuart.c.
 * Returns 5 or 10 encoded bytes; returns 0 on invalid input without changing
 * output. Caller supplies 10 bytes; bytes beyond the returned length are
 * untouched. Input/output may overlap. Does not change sync byte or CRC. */
KM_API uint8_t km_tmc_uart_encode(const uint8_t *raw, uint8_t length,
                                uint8_t encoded[KM_TMC_UART_ENCODED_MAX]);

/* Strip and validate serial framing from a 10-byte packed response into
 * 8 raw bytes. Returns false without changing output on invalid arguments or
 * start/stop bits. Input/output may overlap. Protocol header/register/CRC
 * validation remains the existing km_tmc2209_decode() responsibility. */
KM_API bool km_tmc_uart_decode(const uint8_t *encoded, uint8_t length,
                              uint8_t raw[8]);
#endif
