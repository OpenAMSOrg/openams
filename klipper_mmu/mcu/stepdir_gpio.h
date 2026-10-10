// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KLIPPER_MMU_MCU_STEPDIR_GPIO_H_INCLUDED
#define KLIPPER_MMU_MCU_STEPDIR_GPIO_H_INCLUDED

#include "klipper_mmu/types.h"
#include "klipper_mmu/step_dir_driver.h"
#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * Capacity: up to KM_MAX_LANES (8) logical driver slots.
 */
#define KM_STEPDIR_MAX_SLOTS KM_MAX_LANES

/**
 * Prepare one slot's inactive GPIO outputs without requiring driver readiness.
 * Performs the same task-context, clock, config and pin checks as setup before
 * claiming pins. A successful claim is permanent until MCU reset. No driver is
 * published or enabled. The returned opaque context remains stable and may be
 * passed to km_stepdir_gpio_force_off from the UART binding.
 */
KM_API km_result_t km_stepdir_gpio_prepare(uint32_t slot,
                                           uint32_t step_pin,
                                           uint32_t dir_pin,
                                           uint32_t enable_pin,
                                           const km_step_dir_config_t *config,
                                           bool (*ready)(void *context),
                                           km_fault_t (*fault)(void *context),
                                           void *context,
                                           void **out_force_off_context);

/**
 * Complete portable initialization and publish a prepared driver once ready.
 * A false ready callback returns KM_ERR_UNAVAILABLE without consuming the
 * prepared slot. A repeated successful activation returns KM_ERR_BUSY.
 * A begun initialization is not repeated after a timing or callback fault.
 */
KM_API km_result_t km_stepdir_gpio_activate(uint32_t slot);

/**
 * Raw actuator safe-off callback for a prepared slot's opaque context.
 * Drives only inactive ENABLE: no clock read, portable driver method, delay,
 * allocation or completion of an active STEP pulse. After publication it
 * latches a driver fault; an enclosing controller must stop motion locally.
 * Caller must serialize with ordinary driver operations or use the shutdown
 * path. Never call this with an invented or stale context pointer.
 */
KM_API void km_stepdir_gpio_force_off(void *context);

/**
 * Explicitly clear a published slot's latched fault, such as the one raw
 * safe-off leaves, once the driver behind the ready/fault callbacks has been
 * restarted. Runs the portable clear under the IRQ serialization used by raw
 * safe-off. It never writes an output and refuses unless outputs are already
 * stopped: KM_ERR_BUSY while ENABLE or STEP is logically active,
 * KM_ERR_UNAVAILABLE while ready is false or the slot is unpublished, and
 * KM_ERR_FAULTED while the fault callback reports a fault or in global
 * shutdown. Success rebases uncertain output timing from a fresh clock; the
 * next raw safe-off latches again.
 */
KM_API km_result_t km_stepdir_gpio_clear_fault(uint32_t slot);

/**
 * Configure and initialize a logical STEP/DIR driver slot.
 *
 * Preconditions and Invariants:
 * - Must be called from ready task context with interrupts enabled (irq_save() == 0),
 *   never from an interrupt handler or DECL_INIT boot hook.
 * - The scheduler clock adapter must be in KM_CLOCK_STATE_READY, and neither Klipper
 *   nor this binding may be in global shutdown.
 * - Caller still owes full resource admission. With CONFIG_WANT_GPIO_LOCAL, STEP, DIR
 *   and ENABLE must already be locally reserved; this binding does not reserve them.
 *   A missing, ordinary or partial claim returns KM_ERR_CONFLICT before the slot is
 *   claimed or any output context is published. See board_startup.h:
 *   km_board_startup_begin validates the profile, saved configuration, and complete
 *   claims, initializes the core, then under CONFIG_WANT_GPIO_LOCAL reserves exactly
 *   the expected MMU KM_RESOURCE_PIN entries once before switch, ADC, feeder, and PWM
 *   setup. This adapter never reserves. Prior-local requirements, conflict side
 *   effects, claim immutability, and partial setup are unchanged. The caller still
 *   owes genuine complete external MCU admission, host/transport/crystal and other
 *   resource ownership, and actuation authorization. Automatic boot and host-command
 *   attachment remain unfinished. ACTUATION stays 0. No physical qualification is
 *   claimed.
 * - An explicit actuation build gate (and ACTUATION=0 default) remains a prerequisite
 *   for any production caller.
 * - Validates slot index (0 <= slot < KM_MAX_LANES), non-NULL config, and non-NULL ready callback.
 * - Rejects equal pin assignments within the same slot (step == dir, step == enable, dir == enable).
 * - Checks for pin collisions against all previously claimed slots (including partial or
 *   failed setups) before touching any hardware GPIO.
 * - Rejects duplicate setup on an already claimed slot.
 * - Validates portable timing configuration (pulse widths, max velocity, lateness budget)
 *   before hardware setup.
 * - Pin assignments become immutable once setup begins; claimed pins remain unavailable
 *   for reuse even if setup subsequently fails, until MCU reset.
 * - Physical setup order: Inactive ENABLE first, inactive STEP next, then initial direction.
 *   No positive enable or step transition occurs during setup.
 * - Partial setup tracking: If upstream shutdown occurs during GPIO setup, every touched
 *   pin is left disabled in the shutdown hook.
 * - Driver publication: The km_driver_t instance is published only after complete successful
 *   setup; never publishes a partially initialized driver.
 *
 * Serialization Contract:
 * - Setup is executed with interrupts enabled.
 * - Subsequent portable driver operations invoked by the controller (set_enable,
 *   set_direction, clear_fault, etc.) must run under one enclosing
 *   critical section (or timer IRQ context).
 *
 * @param slot Logical slot index (0 <= slot < KM_MAX_LANES).
 * @param step_pin Canonical pin ID for STEP output.
 * @param dir_pin Canonical pin ID for DIR output.
 * @param enable_pin Canonical pin ID for ENABLE output.
 * @param config Pointer to portable STEP/DIR configuration (copied stably).
 * @param ready Mandatory firmware-local driver readiness query callback.
 * @param fault Optional firmware-local driver fault query callback (may be NULL).
 * @param context Opaque user context pointer passed to ready and fault callbacks.
 * @return KM_OK on success.
 *         KM_ERR_INVALID_PARAM if slot is out of range, config or ready is NULL,
 *                              pins within slot are equal, config parameters are invalid,
 *                              or called with interrupts disabled.
 *         KM_ERR_UNAVAILABLE if scheduler clock is not yet ready or ready callback returns false.
 *         KM_ERR_BUSY if slot is already claimed.
 *         KM_ERR_CONFLICT if any pin collides with an already claimed slot, or if
 *                         CONFIG_WANT_GPIO_LOCAL and a pin lacks a prior local claim.
 *         KM_ERR_FAULTED if system or binding is in fault/shutdown, or if driver init faults.
 */
KM_API km_result_t km_stepdir_gpio_setup(uint32_t slot,
                                         uint32_t step_pin,
                                         uint32_t dir_pin,
                                         uint32_t enable_pin,
                                         const km_step_dir_config_t *config,
                                         bool (*ready)(void *context),
                                         km_fault_t (*fault)(void *context),
                                         void *context);

/**
 * Retrieve the exposed km_driver_t interface for a successfully initialized slot.
 *
 * The driver pointer is published only after activate (or combined setup) succeeds.
 * Returns NULL if the slot is invalid, not claimed, setup failed, or if the system
 * is in global shutdown.
 *
 * @param slot Logical slot index.
 * @return Pointer to base km_driver_t, or NULL.
 */
KM_API km_driver_t *km_stepdir_gpio_get_driver(uint32_t slot);

/**
 * Query whether the logical slot is currently active and published.
 *
 * @param slot Logical slot index.
 * @return True if slot is claimed, published, and not in shutdown.
 */
KM_API bool km_stepdir_gpio_is_slot_active(uint32_t slot);

/**
 * Query whether the STEP/DIR binding is in global shutdown.
 *
 * Once latched, global shutdown remains true until MCU reset, even if upstream
 * calls clear_shutdown.
 *
 * @return True if in global shutdown.
 */
KM_API bool km_stepdir_gpio_is_shutdown(void);

/**
 * Query canonical pin IDs claimed by a slot.
 *
 * Preserves outputs on error.
 *
 * @param slot Logical slot index.
 * @param out_step_pin Receives claimed STEP pin ID.
 * @param out_dir_pin Receives claimed DIR pin ID.
 * @param out_enable_pin Receives claimed ENABLE pin ID.
 * @return KM_OK if slot is claimed, KM_ERR_INVALID_PARAM if pointers NULL or slot out of range,
 *         KM_ERR_UNAVAILABLE if slot is not claimed.
 */
KM_API km_result_t km_stepdir_gpio_get_slot_pins(uint32_t slot,
                                                 uint32_t *out_step_pin,
                                                 uint32_t *out_dir_pin,
                                                 uint32_t *out_enable_pin);

/**
 * Emergency firmware shutdown hook for STEP/DIR GPIO binding.
 *
 * Unconditionally drives inactive ENABLE before inactive STEP on all touched slots
 * using raw GPIO writes and tracked setup flags. Performs no clock reads, no readiness
 * callbacks, no UART transactions, no delays, and no memory allocations.
 * Latches global shutdown permanently until MCU reset.
 */
void km_stepdir_gpio_shutdown(void);

struct stepper;
KM_API struct stepper *km_stepdir_gpio_get_stepper_for_driver(const km_driver_t *d);
KM_API int64_t km_stepdir_gpio_get_position_for_driver(const km_driver_t *d);
KM_API int km_stepdir_gpio_queue(const km_driver_t *d, uint32_t start_clock,
                                 uint32_t interval, uint16_t count, int16_t add,
                                 uint8_t forward);


#ifdef __cplusplus
}
#endif

#endif /* KLIPPER_MMU_MCU_STEPDIR_GPIO_H_INCLUDED */
