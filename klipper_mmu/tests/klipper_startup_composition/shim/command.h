// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef COMPOSITION_COMMAND_H
#define COMPOSITION_COMMAND_H
#include <stdint.h>
#define HF_IN_SHUTDOWN 1
#define DECL_COMMAND(fn, desc)
#define DECL_COMMAND_FLAGS(fn, flags, desc)
#define DECL_CONSTANT(name, val)
#define DECL_CONSTANT_STR(name, val)
void sendf(const char *fmt, ...);
#endif
