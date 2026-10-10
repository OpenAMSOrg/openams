// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KLIPPER_MMU_H723_RESOURCES_H
#define KLIPPER_MMU_H723_RESOURCES_H
#include "pin_config.h"

typedef struct { uint8_t timer, channel; } km_h723_pwm_route_t;
/* Component identities within the full MCU plan; external users need distinct IDs. */
enum { KM_H723_OWNER_REWIND_TIMER=32, KM_H723_OWNER_DIFF_ADC=33 };

/* First route selected by upstream H7 gpio_pwm_setup, not every silicon route.
 * Pure lookup; every failure leaves output unchanged. Returns UNAVAILABLE
 * without the optional upstream query. No alternate-function data or I/O. */
KM_API km_result_t km_h723_pwm_route(uint32_t pin, km_h723_pwm_route_t *out);

typedef struct {
    km_resource_claim_t claims[KM_RESOURCE_MAX_CLAIMS];
    size_t count;
    /* [lane][0=forward,1=reverse]; unused entries are zero. */
    km_h723_pwm_route_t rewind[KM_MAX_LANES][2];
} km_h723_resource_plan_t;

/* MMU-side plan for currently supported H723 bindings. Includes configured pins,
 * whole rewind timers (one owner lane per timer; its two directions may share
 * distinct channels), and one paired ADC unit. Routes come from the shared
 * driver's query (1 on success, 0 on failure), not a copied table. ADC1, ADC2,
 * and ADC3 pairs are eligible. Saved buffer pins supply that pair when the
 * analog profile is active. Rejects alias channels even on different GPIO pins.
 * PC4 HUB and PC5 TRG1 preset aliases still conflict when those pins are used
 * for the analog pair. Encoder/current acquisition plans remain unsupported
 * until their physical backends exist.
 *
 * Serialized setup calls only, with fixed scratch storage; MCU output belongs in
 * persistent storage too. No hardware reservation or I/O. Merge all other MCU
 * users' claims and validate the complete plan before setup. The ADC driver
 * additionally excludes ordinary and local clients from the same unit until
 * reset. Enforce ownership against later reconfiguration.
 * Output is usable ONLY on KM_OK. Every failure sets count=0; other output bytes
 * are unspecified. Diagnostics must not alias output. Conflict indices refer to
 * the would-be claims, including the copied pin claims. Disabled menu is UNAVAILABLE.
 */
KM_API km_result_t km_config_h723_resources(km_h723_resource_plan_t *out,
                                          km_resource_diagnostic_t *diagnostic);
#endif
