// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KLIPPER_MMU_TMC_CONFIG_H
#define KLIPPER_MMU_TMC_CONFIG_H
#include "klipper_mmu/tmc_uart_config.h"

/* Pure saved-configuration factory for independent per-lane UART channels.
 * Validates all lanes before writing KM_MAX_LANES outputs; unused lanes are zero.
 * Error preserves output. Disabled pin menu, unset pull-up, or unset resistor/current
 * ceiling returns UNAVAILABLE. No initialization, pin access, readiness or measured-current
 * claim. The future board initializer must also validate pins and feeder timing. */
/* MCU callers should keep this output array in persistent storage: a large
 * automatic array adds to the nested register-validation stack requirement. */
KM_API km_result_t km_config_tmc(km_tmc_uart_config_t out[KM_MAX_LANES]);

/* Same contract as km_config_tmc, for the one selector-axis TMC2209 on its
 * own dedicated UART wire (KLIPPER_MMU_SELECTOR_UART_ADDRESS/_PULLUP,
 * KLIPPER_MMU_SELECTOR_RUN_CURRENT/_HOLD_CURRENT). Shares the gear's sense
 * resistor, current ceiling, microstep-register interpolation/stealthChop
 * and UART timing menu options: those are physical/board properties, not
 * per-axis ones. Disabled selector stepper, unset shared resistor/ceiling,
 * or an invalid combination returns KM_ERR_UNAVAILABLE/KM_ERR_INVALID_PARAM
 * exactly like km_config_tmc. No pin access, initialization or readiness. */
KM_API km_result_t km_config_selector_tmc(km_tmc_uart_config_t *out);
#endif
