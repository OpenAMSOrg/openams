// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef BOARD_IRQ_H
#define BOARD_IRQ_H

#include <stdint.h>
#include <stdbool.h>

typedef unsigned long irqstatus_t;

irqstatus_t irq_save(void);
void irq_restore(irqstatus_t flag);
void irq_disable(void);
void irq_enable(void);
void irq_wait(void);
void irq_poll(void);
bool test_shim_irq_is_disabled(void);

#endif
