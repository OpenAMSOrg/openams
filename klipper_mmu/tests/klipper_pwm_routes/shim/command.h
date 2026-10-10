// SPDX-License-Identifier: GPL-3.0-or-later
#include <stddef.h>
#include <stdint.h>
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
#define DIV_ROUND_CLOSEST(n, d) (((n) + (d)/2) / (d))
#define DECL_CONSTANT(name, value) _Static_assert((value) > 0, name)
_Noreturn void shutdown(const char *message);
