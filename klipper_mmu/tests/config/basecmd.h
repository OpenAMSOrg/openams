// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef TEST_CONFIG_BASECMD_H
#define TEST_CONFIG_BASECMD_H
#include <stddef.h>
#include <stdint.h>
void *oid_alloc(uint8_t oid, void (*type)(uint32_t *args), size_t size);
#endif
