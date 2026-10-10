// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef COMMAND_H
#define COMMAND_H

#include <stdint.h>

#define HF_IN_SHUTDOWN 0x01

typedef void (*command_func_t)(uint32_t *args);

void shim_register_command(command_func_t func, uint32_t flags, const char *msg);
void shim_register_constant(const char *name, uint32_t val);
void command_sendf(const char *fmt, ...);
#define sendf(...) command_sendf(__VA_ARGS__)

#define _TEST_PASTE_CMD(a, b) a##b
#define TEST_PASTE_CMD(a, b) _TEST_PASTE_CMD(a, b)

#define DECL_COMMAND_FLAGS(FUNC, FLAGS, MSG) \
    static void __attribute__((constructor)) TEST_PASTE_CMD(_reg_cmd_, __LINE__)(void) { \
        shim_register_command(FUNC, (uint32_t)(FLAGS), MSG); \
    } _Static_assert(1, "registration")

#define DECL_COMMAND(FUNC, MSG) DECL_COMMAND_FLAGS(FUNC, 0, MSG)

#define DECL_CONSTANT(NAME, VALUE) \
    static void __attribute__((constructor)) TEST_PASTE_CMD(_reg_const_, __LINE__)(void) { \
        shim_register_constant(NAME, (uint32_t)(VALUE)); \
    } _Static_assert(1, "registration")

#endif
