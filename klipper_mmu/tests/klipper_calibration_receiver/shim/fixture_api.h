// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
#include <stdarg.h>
#include <stdint.h>
struct command_encoder;
void *console_receive_buffer(void);
void console_sendf(const struct command_encoder *, va_list);
