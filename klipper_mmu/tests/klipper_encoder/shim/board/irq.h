// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef TEST_ENCODER_IRQ_H
#define TEST_ENCODER_IRQ_H
typedef unsigned long irqstatus_t;
irqstatus_t irq_save(void);
void irq_restore(irqstatus_t flag);
#endif
