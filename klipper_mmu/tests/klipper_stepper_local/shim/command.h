// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef COMMAND_H
#define COMMAND_H

#include <stdint.h>

#define HF_IN_SHUTDOWN 0x01

void test_sendf(const char *fmt, ...);
#define sendf(...) test_sendf(__VA_ARGS__)

// The test calls the command handlers directly, so registration is a no-op.
#define DECL_COMMAND(FUNC, MSG) extern int qc_unused_command_decl
#define DECL_CONSTANT(NAME, VALUE) extern int qc_unused_constant_decl

#endif
