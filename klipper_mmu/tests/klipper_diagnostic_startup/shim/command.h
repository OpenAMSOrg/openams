// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef DIAG_FIXTURE_COMMAND_H
#define DIAG_FIXTURE_COMMAND_H
#include <stdint.h>
#define HF_IN_SHUTDOWN 1
void fixture_register_command(const char *fn, uint32_t flags, const char *fmt);
void fixture_register_constant(const char *name, int value);
#define FIXTURE_JOIN_INNER(a, b) a##b
#define FIXTURE_JOIN(a, b) FIXTURE_JOIN_INNER(a, b)
#define DECL_CONSTANT_IMPL(name, value, n) \
    static void FIXTURE_JOIN(fixture_ctor_const_, n)(void) __attribute__((constructor, used)); \
    static void FIXTURE_JOIN(fixture_ctor_const_, n)(void) { fixture_register_constant(name, (int)(value)); }
#define DECL_CONSTANT(name, value) DECL_CONSTANT_IMPL(name, value, __COUNTER__)
#define DECL_COMMAND(fn, fmt) \
    static void fixture_ctor_cmd_##fn(void) __attribute__((constructor, used)); \
    static void fixture_ctor_cmd_##fn(void) { fixture_register_command(#fn, 0u, fmt); }
#define DECL_COMMAND_FLAGS(fn, flags, fmt) \
    static void fixture_ctor_cmd_##fn(void) __attribute__((constructor, used)); \
    static void fixture_ctor_cmd_##fn(void) { fixture_register_command(#fn, (uint32_t)(flags), fmt); }
void sendf(const char *fmt, ...);
#endif
