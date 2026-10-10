// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef TEST_SHIM_BASECMD_H
#define TEST_SHIM_BASECMD_H

#include <stddef.h>
#include <stdint.h>

typedef void (*oid_type)(uint32_t *args);
void *oid_alloc(uint8_t oid, oid_type type, size_t size);
void *command_decode_ptr(uint32_t value);

#endif
