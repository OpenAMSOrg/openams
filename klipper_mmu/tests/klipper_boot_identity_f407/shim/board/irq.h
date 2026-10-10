// SPDX-License-Identifier: GPL-3.0-or-later
#pragma once
typedef unsigned irqstatus_t;
irqstatus_t irq_save(void);
void irq_restore(irqstatus_t flags);
