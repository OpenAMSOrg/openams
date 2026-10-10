// SPDX-License-Identifier: GPL-3.0-or-later
#include <stddef.h>
#include <stdint.h>
typedef void (*oid_type)(uint32_t *);
void *oid_alloc(uint8_t oid, oid_type type, size_t size);
void *oid_lookup(uint8_t oid, oid_type type);
void *review_oid(uint8_t oid, oid_type type);
#define foreach_oid(oid, ptr, type) \
    for ((oid)=0; (oid)<4; ++(oid)) if (((ptr)=review_oid((oid), (type))) != NULL)
