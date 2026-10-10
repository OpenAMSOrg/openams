// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef PAIR_TEST_IRQ_H
#define PAIR_TEST_IRQ_H
typedef unsigned long irqstatus_t;
irqstatus_t irq_save(void);
void irq_restore(irqstatus_t flag);
#endif
