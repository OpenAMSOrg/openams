// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef TEST_ENCODER_SCHED_H
#define TEST_ENCODER_SCHED_H
#include <stdint.h>
uint8_t sched_is_shutdown(void);
/* No real registration needed: the test calls km_encoder_gpio_shutdown()
 * directly. Keep FUNC referenced so an unused-function warning cannot fire. */
#define DECL_SHUTDOWN(FUNC) \
    static void (*_test_shutdown_ref_##FUNC)(void) __attribute__((unused)) = FUNC
#endif
