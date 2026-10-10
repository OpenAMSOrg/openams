// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef PROBE_FIXTURE_STM32_GPIO_LOCAL_H
#define PROBE_FIXTURE_STM32_GPIO_LOCAL_H
#include <stdint.h>
enum { GPIO_LOCAL_OK = 0, GPIO_LOCAL_ERR_INVALID = -1,
       GPIO_LOCAL_ERR_BUSY = -2, GPIO_LOCAL_ERR_SHUTDOWN = -3 };
int gpio_local_reserve(const uint32_t *pins, uint32_t count);
#endif
