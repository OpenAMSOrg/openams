// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef TEST_SHIM_IRQ_H
#define TEST_SHIM_IRQ_H

#include <stdint.h>

typedef unsigned long irqstatus_t;

irqstatus_t irq_save(void);
void irq_restore(irqstatus_t status);

#endif
