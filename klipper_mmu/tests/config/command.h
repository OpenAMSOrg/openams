// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef TEST_CONFIG_COMMAND_H
#define TEST_CONFIG_COMMAND_H
/* Actual dictionary emission is checked in the upstream build matrix. */
#define DECL_CONSTANT(name, value) _Static_assert((value) >= 0, "nonnegative build constant")
#define DECL_COMMAND(fn, format) _Static_assert(sizeof(format) > 1, "command format")
#endif
