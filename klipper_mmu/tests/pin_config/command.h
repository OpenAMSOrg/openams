// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef TEST_PIN_CONFIG_COMMAND_H
#define TEST_PIN_CONFIG_COMMAND_H
/* Actual emitted reservations are tested through the upstream build dictionary. */
#define DECL_CONSTANT_STR(name, value) _Static_assert(sizeof(name) > 1 && sizeof(value) > 1, "pin reservation")
#endif
