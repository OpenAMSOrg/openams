// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef __SIM_INTERNAL_H
#define __SIM_INTERNAL_H

#include <stdint.h>

void gpio_peripheral(uint32_t pin, uint32_t val, int pull_up);

#include <signal.h>
void console_sleep(sigset_t *sigset);
int console_setup(char *name);
int watchdog_setup(void);
void report_errno(char *where, int rc);



#endif

