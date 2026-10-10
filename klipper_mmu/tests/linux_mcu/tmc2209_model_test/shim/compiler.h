// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef SHIM_COMPILER_H
#define SHIM_COMPILER_H
#include <stddef.h>
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
#define container_of(ptr, type, member) \
    ((type *)((char *)(ptr) - offsetof(type, member)))
#endif
