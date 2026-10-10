// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef BOARD_IRQ_H
#define BOARD_IRQ_H

typedef unsigned long irqstatus_t;

irqstatus_t irq_save(void);
void irq_restore(irqstatus_t flag);
void irq_disable(void);
void irq_enable(void);

#endif
