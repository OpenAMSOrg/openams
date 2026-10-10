// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef TEST_MESSAGES_COMMAND_H
#define TEST_MESSAGES_COMMAND_H
void capture_sendf(const char *format, ...);
#define sendf capture_sendf
#endif
