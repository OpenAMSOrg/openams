// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef BASECMD_H
#define BASECMD_H

#include <stddef.h>
#include <stdint.h>

typedef void (*oid_type)(uint32_t *);

void *oid_alloc(uint8_t oid, oid_type type, size_t size);
void *oid_lookup(uint8_t oid, oid_type type);
void *test_oid_foreach(uint8_t index, oid_type type);
void *alloc_chunk(size_t size);

#define foreach_oid(oid, ptr, type) \
    for ((oid) = 0; (oid) < 8; ++(oid)) \
        if (((ptr) = test_oid_foreach((oid), (type))) != NULL)

#endif
