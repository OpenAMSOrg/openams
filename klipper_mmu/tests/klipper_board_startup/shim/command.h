// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef TEST_SHIM_COMMAND_H
#define TEST_SHIM_COMMAND_H

#include <stdint.h>

#define DECL_COMMAND(fn, desc) typedef int shim_command_decl_##fn
#define DECL_COMMAND_FLAGS(fn, flags, desc) typedef int shim_command_flags_##fn
#define DECL_CONSTANT(name, val) _Static_assert((val) >= 0, name)
#define DECL_CONSTANT_STR(name, val) _Static_assert(sizeof(val) > 1, name)

void sendf(const char *format, ...);

#endif
