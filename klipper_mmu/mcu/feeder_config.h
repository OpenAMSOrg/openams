// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KLIPPER_MMU_FEEDER_CONFIG_H
#define KLIPPER_MMU_FEEDER_CONFIG_H
#include "klipper_mmu/step_dir_driver.h"

/* Pure saved-configuration factory. All configured lanes are validated before
 * writing out; unused entries are zero. No pins, driver state or outputs change.
 * Caller supplies KM_MAX_LANES elements. Disabled pin menu returns UNAVAILABLE.
 * Pin ownership, TMC initialization and physical timing qualification are separate. */
KM_API km_result_t km_config_feeders(km_step_dir_config_t out[KM_MAX_LANES]);
#endif
