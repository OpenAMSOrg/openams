// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KLIPPER_MMU_MCU_CALIBRATION_H_INCLUDED
#define KLIPPER_MMU_MCU_CALIBRATION_H_INCLUDED

#include "klipper_mmu/buffer_sensor.h"

#ifdef __cplusplus
extern "C" {
#endif

/*
 * Lifetime of the one DiffA/DiffB calibration object.
 *
 * The host sends one ordinary config command before finalize_config:
 *   config_km_diff_calibration oid=%c data=%*s
 * That command text is part of the configuration CRC. oid_alloc is the
 * only phase guard; this module does not keep its own finalize flag.
 *
 * The wire bytes are message-owned and are not retained. They are copied
 * into a temporary km_diff_adc_config_t, checked for exact length, then
 * passed once through km_diff_adc_config_validate. Only a complete
 * accepted record is copied into the oid object and published.
 *
 * km_diff_calibration_get returns that immutable object only when it was
 * published, this module has not seen MCU shutdown, and the scheduler is
 * not shut down. A matching-CRC reconnect does not replay config commands
 * and does not need to: the published object stays in place.
 *
 * DECL_SHUTDOWN latches denial for the life of the firmware image and
 * drops the published pointer. config_reset clears Klipper oids and the
 * scheduler shutdown flag, but it does not clear this latch and it does
 * not recreate the object. The getter then returns NULL. Another
 * config_km_diff_calibration is refused until a full MCU reset.
 *
 * No pin, ADC, motion, or board startup is registered here.
 */
const km_diff_adc_config_t *km_diff_calibration_get(void);

#ifdef __cplusplus
}
#endif

#endif /* KLIPPER_MMU_MCU_CALIBRATION_H_INCLUDED */
