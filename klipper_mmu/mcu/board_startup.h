// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KLIPPER_MMU_MCU_BOARD_STARTUP_H
#define KLIPPER_MMU_MCU_BOARD_STARTUP_H

#include "klipper_mmu/types.h"
#include "klipper_mmu/operation_core.h"
#include "klipper_mmu/resource_plan.h"
#include "klipper_mmu/buffer_sensor.h"
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * Board startup composition lifecycle states for STM32H723 Klipper-MMU.
 */
enum km_board_startup_state {
    KM_BOARD_STARTUP_UNINITIALIZED = 0,
    KM_BOARD_STARTUP_WAITING       = 1,
    KM_BOARD_STARTUP_READY         = 2,
    KM_BOARD_STARTUP_FAULTED       = 3,
};

/**
 * Begin asynchronous startup and composition of Klipper-MMU on STM32H723.
 *
 * Preconditions and Invariants:
 * - Must be called from task context with interrupts enabled (irq_save() == 0).
 * - Scheduler clock adapter must be in KM_CLOCK_STATE_READY.
 * - Neither Klipper nor this module may be in global shutdown.
 * - With CONFIG_WANT_GPIO_LOCAL, complete_claims NULL and claim_count 0 selects configured
 *   admission (no external list). A non-NULL pointer with count 0, NULL with a nonzero count,
 *   or a count above KM_RESOURCE_MAX_CLAIMS is KM_ERR_INVALID_PARAM. A supplied array is an
 *   immutable caller completeness assertion, not an enumeration.
 * - With GPIO local off, NULL and 0 stay KM_ERR_INVALID_PARAM. That caller supplies and enforces
 *   complete external ownership.
 * - timeout_us must be nonzero and < 2^31 (KM_TIME_WRAP_HORIZON_US).
 * - Optional DiffA/DiffB calibration is passed to profile factory (NULL if switched buffer).
 * - One immutable board instance until MCU reset. Duplicate start returns KM_ERR_BUSY.
 *
 * Validation Phase (no I/O, no GPIO reservation or peripheral setup):
 * - Validates arguments, task context, clock readiness, and shutdown state.
 * - Validates absence of preclaimed component slots and already-attached controller.
 * - Validates all enabled saved factories: profile, pins, h723_resources, feeders, TMC,
 *   switches, rewind outputs/limits, and ADC pair.
 * - Rejects unsupported capabilities (encoder, measured current) with KM_ERR_NOT_SUPPORTED.
 *   A selector profile (has_selector) is supported: the shared gear is bound to every
 *   lane, and the selector axis and/or servo are set up and bound (klipper_mmu/selector.h).
 * - Saved factories, including km_config_h723_resources, run in both modes before reservation.
 *   That factory validates the internal MMU plan. Configured admission skips only external-list
 *   validation and fieldwise inclusion. A supplied array keeps km_resource_plan_validate and
 *   fieldwise expected-claim inclusion, without memcmp on padded structs.
 * - Does not discover clients, synthesize a claim list, or run host checks. Trusted Klipper
 *   configuration checks RESERVE_PINS (MMU pins, transport, crystal), aliases, and BUS_PINS
 *   before deployment. Shared GPIO/ADC/PWM registries cover participating driver APIs; direct
 *   MMIO is outside that guarantee. This API does not attach commands or run automatically.
 *   ACTUATION stays 0 as metadata. Outputs are initialized inactive regardless.
 *
 * Preparation & Asynchronous Progression Phase:
 * - Takes fresh start timestamp.
 * - Initializes static operation core.
 * - When CONFIG_WANT_GPIO_LOCAL is enabled, reserves exactly the KM_RESOURCE_PIN entries of
 *   the validated MMU expected plan once through gpio_local_reserve, before switch/ADC/feeder/PWM
 *   setup. The batch is all-or-none and does not include non-PIN claims or arbitrary external
 *   plan entries. INVALID/BUSY/SHUTDOWN map to KM_ERR_INVALID_PARAM, KM_ERR_CONFLICT, and
 *   KM_ERR_FAULTED. A reject before success does not latch, clean up components, or release
 *   pins. An already-reserved batch is conflict. Success persists until reset with no release.
 *   Reservation is ownership, not clocks, MMIO, or motion. Deadline and shutdown are rechecked
 *   after success and before component setup; that failure latches with GPIOs retained.
 *   With the option off, the caller still owes any GPIO ownership.
 * - Configures switch inputs via km_switch_gpio_setup. Pure KM_ERR_BUSY is a foreign binding:
 *   without a retained batch it stays retryable; with a retained batch the board latches and
 *   does not shut that switch down.
 * - Optionally starts sequential ADC via km_adc_start.
 * - Prepares feeder lanes via km_tmc_feeder_setup.
 * - Optionally prepares rewind bridges via km_pwm_bridge_gpio_setup with mandatory
 *   readiness callback and separate ENABLE inactive.
 * - All outputs remain disabled (STEP inactive, PWM zero).
 * - Takes fresh observed timestamp and accounts for setup duration against timeout_us.
 * - Performs non-blocking activation check of feeder lanes.
 *
 * State Transitions & Return:
 * - Returns KM_OK on successful initiation (state is WAITING or READY).
 * - If all lanes immediately confirm, transitions to READY, binds drivers, attaches controller,
 *   and exposes static core.
 * - If any lane is pending, transitions to WAITING; caller must advance via km_board_startup_service.
 * - Any failure after a successful local GPIO batch or after physical mutation latches FAULTED.
 *   Touched outputs are turned safely off using raw clock-free shutdown paths. Local GPIO claims
 *   are not released. A failed reserve itself does not authorize motion.
 */
KM_API km_result_t km_board_startup_begin(const km_resource_claim_t *complete_claims,
                                          size_t claim_count,
                                          uint32_t timeout_us,
                                          const km_diff_adc_config_t *adc_calibration);

/**
 * Service waiting asynchronous startup without blocking.
 *
 * Advances unconfirmed feeder lanes via km_tmc_feeder_activate.
 * Evaluates elapsed time against startup timeout using fresh coherent clock readings.
 * Handles exact timeout boundary and 32-bit counter wrap.
 *
 * When all lanes confirm: binds feeder/rewinder drivers to core, attaches controller
 * using km_sensors_sample, transitions state to READY, and publishes static core.
 *
 * Ready-state service does not restart or reschedule controller; returns KM_OK.
 * Failure after physical mutation permanently latches FAULTED and returns error code.
 * Duplicate starts/services do not reinitialize or re-enable.
 *
 * @return KM_OK if READY,
 *         KM_ERR_UNAVAILABLE if still WAITING (pending),
 *         KM_ERR_TIMEOUT if startup timeout exceeded,
 *         KM_ERR_INVALID_PARAM if called with IRQs disabled,
 *         KM_ERR_FAULTED on system/driver fault or shutdown.
 */
KM_API km_result_t km_board_startup_service(void);

/**
 * Query current board startup lifecycle state.
 */
KM_API enum km_board_startup_state km_board_startup_get_state(void);

/**
 * Query current board startup result code.
 *
 * Returns:
 * - KM_ERR_UNAVAILABLE while uninitialized or waiting,
 * - KM_OK when ready,
 * - latched error code (e.g. KM_ERR_FAULTED, KM_ERR_TIMEOUT) when faulted.
 */
KM_API km_result_t km_board_startup_get_result(void);

/* The startup step a failure happened in, for the host's plain-language
 * report. Values are wire-visible (km_diagnostic_detail); append only. The
 * first five are recorded by diagnostic startup before the board begins. */
enum km_startup_step {
    KM_STARTUP_STEP_NONE = 0,
    KM_STARTUP_STEP_CLOCK, KM_STARTUP_STEP_IDENTITY, KM_STARTUP_STEP_CALIBRATION,
    KM_STARTUP_STEP_DEADLINE, KM_STARTUP_STEP_ALREADY_STARTED,
    KM_STARTUP_STEP_PROFILE, KM_STARTUP_STEP_PINS, KM_STARTUP_STEP_RESOURCES,
    KM_STARTUP_STEP_SAVED_CONFIG, KM_STARTUP_STEP_RESERVE, KM_STARTUP_STEP_SWITCHES,
    KM_STARTUP_STEP_ADC, KM_STARTUP_STEP_DRIVER_SETUP, KM_STARTUP_STEP_REWIND_SETUP,
    KM_STARTUP_STEP_DRIVER_CONFIRM, KM_STARTUP_STEP_BIND, KM_STARTUP_STEP_ATTACH,
    KM_STARTUP_STEP_SELECTOR_SETUP, /* Selector axis and/or servo (selector.h). */
};
#define KM_STARTUP_NO_LANE 0xffu
#define KM_STARTUP_NO_PIN 0xffffu  /* KM_PIN_UNUSED */

/* Where board startup stopped. cause is that step's own result (a TMC
 * activation error, not the latched KM_ERR_FAULTED). For PINS/RESOURCES,
 * reason is the km_resource_diag_reason_t and pin/role/pin_lane name up to
 * two offending pin claims (pin KM_PIN_UNUSED when unknown). */
typedef struct {
    uint8_t step, lane, reason;
    int8_t cause;
    uint16_t pin[2];
    uint8_t role[2], pin_lane[2];
} km_board_startup_detail_t;

KM_API void km_board_startup_get_detail(km_board_startup_detail_t *out);

/**
 * Expose initialized static operation core.
 *
 * Exposes core ONLY after controller attachment succeeds (state == KM_BOARD_STARTUP_READY).
 * Returns NULL if uninitialized, waiting, or faulted (never publishes partial core).
 */
KM_API km_operation_core_t *km_board_startup_get_core(void);

/**
 * Emergency firmware shutdown hook registered with DECL_SHUTDOWN.
 *
 * Latches board failure until MCU reset without reading clocks.
 * Leaves all touched outputs safely off. Does not release a local GPIO batch.
 * Remains latched even if upstream calls sched_clear_shutdown.
 */
void km_board_startup_shutdown(void);

#ifdef __cplusplus
}
#endif

#endif /* KLIPPER_MMU_MCU_BOARD_STARTUP_H */
