// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef PROBE_FIXTURE_STM32_INTERNAL_H
#define PROBE_FIXTURE_STM32_INTERNAL_H
#define GPIO_LOCAL_OK 0
#define GPIO_LOCAL_ERR_INVALID 1
#define GPIO_LOCAL_ERR_BUSY 2
int gpio_local_reserve(const uint32_t *pins, uint32_t count);
#endif
