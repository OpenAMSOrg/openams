// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KLIPPER_MMU_MCU_TMC_FEEDER_H_INCLUDED
#define KLIPPER_MMU_MCU_TMC_FEEDER_H_INCLUDED

#include "stepdir_gpio.h"
#include "tmc_uart_gpio.h"

/* Explicit, fixed-current composition for one prepared feeder lane.
 * Caller owns complete external/peripheral pin reservations and actuation gate.
 * Must run in ready task context; neither setup nor activation starts motion. */
KM_API km_result_t km_tmc_feeder_setup(uint32_t slot,
                                       uint32_t step_pin, uint32_t dir_pin,
                                       uint32_t enable_pin, uint32_t uart_pin,
                                       bool uart_pull_up,
                                       const km_step_dir_config_t *step_config,
                                       const km_tmc2209_config_t *tmc_config,
                                       uint16_t expected_run_ma,
                                       uint16_t expected_hold_ma);

/* Call after asynchronous UART confirmation. False readiness leaves the
 * prepared slot unpublished for a later explicit attempt. No polling here. */
KM_API km_result_t km_tmc_feeder_activate(uint32_t slot);

/* One step of an explicit restart of an activated lane. Task context, IRQs
 * enabled. The caller must already have proven that nothing moves or can be
 * admitted on the lane and that its outputs are stopped. The first call
 * restarts the TMC controller, forcing the actuator off and latching the
 * STEP/DIR fault, and returns KM_ERR_UNAVAILABLE. Later calls return
 * KM_ERR_UNAVAILABLE while the chip reconfirms its configuration. Once it is
 * ready with the fixed currents, the STEP/DIR latch is cleared and KM_OK is
 * returned; no output is enabled. A fault during the restart returns
 * KM_ERR_FAULTED, keeps the latch and ends that restart. */
KM_API km_result_t km_tmc_feeder_restart(uint32_t slot);

/* Returns only a published driver. It may subsequently be faulted; callers
 * must still use driver readiness/fault checks before enabling or stepping. */
KM_API km_driver_t *km_tmc_feeder_get_driver(uint32_t slot);

#endif
