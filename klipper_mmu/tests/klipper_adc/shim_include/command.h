// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef TEST_COMMAND_H
#define TEST_COMMAND_H

#include <stdint.h>

#define HF_IN_SHUTDOWN 0x01

typedef void (*command_func_t)(uint32_t *args);

void shim_register_command(command_func_t func, uint32_t flags, const char *msg);
void shim_register_constant(const char *name, uint32_t val);
void command_sendf(const char *fmt, ...);

#define _TEST_PASTE(a, b) a##b
#define TEST_PASTE(a, b) _TEST_PASTE(a, b)

#define DECL_COMMAND_FLAGS(FUNC, FLAGS, MSG) \
    static void __attribute__((constructor)) TEST_PASTE(_reg_cmd_, __LINE__)(void) { \
        shim_register_command(FUNC, (uint32_t)(FLAGS), MSG); \
    } _Static_assert(1, "registration")

#define DECL_COMMAND(FUNC, MSG) DECL_COMMAND_FLAGS(FUNC, 0, MSG)

#define DECL_CONSTANT(NAME, VALUE) \
    static void __attribute__((constructor)) TEST_PASTE(_reg_const_, __LINE__)(void) { \
        shim_register_constant(NAME, (uint32_t)(VALUE)); \
    } _Static_assert(1, "registration")

#define sendf(...) command_sendf(__VA_ARGS__)

#endif
