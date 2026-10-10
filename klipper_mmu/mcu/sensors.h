// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KLIPPER_MMU_SENSORS_H
#define KLIPPER_MMU_SENSORS_H
#include "klipper_mmu/operation_core.h"

/* Controller sample callback, NULL context. Requires prior switch binding to
 * this core and, for analog feedback, explicit ADC startup and 12-bit paired
 * calibration. Does not set up peripherals or infer missing measurements.
 * Uses the ADC record's original acquisition time; invalid/missing/expired
 * records invalidate feedback. Shutdown stays latched until MCU reset. */
KM_API void km_sensors_sample(km_operation_core_t *core, void *context);
void km_sensors_shutdown(void);
#endif
