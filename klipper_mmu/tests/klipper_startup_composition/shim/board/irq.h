// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef DIAG_FIXTURE_IRQ_H
#define DIAG_FIXTURE_IRQ_H
#include <stdint.h>
typedef unsigned long irqstatus_t;
irqstatus_t irq_save(void);
void irq_restore(irqstatus_t old);
#endif
