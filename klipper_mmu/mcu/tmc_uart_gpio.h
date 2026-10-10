// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KLIPPER_MMU_MCU_TMC_UART_GPIO_H_INCLUDED
#define KLIPPER_MMU_MCU_TMC_UART_GPIO_H_INCLUDED

#include "klipper_mmu/types.h"
#include "klipper_mmu/tmc2209.h"
#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * Capacity: up to KM_MAX_LANES (8) static independent slots.
 */
#define KM_TMC_UART_MAX_SLOTS KM_MAX_LANES

/**
 * Mandatory caller-supplied actuator safe-off callback.
 *
 * Must immediately disable the associated actuator without reading clocks,
 * blocking, host I/O, allocations, reentering this adapter, or calling
 * clock-dependent portable driver operations. Storage is caller-owned/static.
 *
 * @param context Caller-supplied opaque context pointer.
 */
typedef void (*km_tmc_uart_force_off_fn)(void *context);

/**
 * Copied read-only snapshot of slot status and current setpoints.
 *
 * Distinguishes requested vs confirmed currents; does not expose a mutable controller pointer.
 */
typedef struct {
    bool configured;
    bool ready;
    km_fault_t fault;
    uint16_t requested_run_ma;
    uint16_t requested_hold_ma;
    uint16_t confirmed_run_ma;
    uint16_t confirmed_hold_ma;
    uint32_t drv_status;
    uint32_t gstat;
    uint8_t ifcnt;
} km_tmc_uart_status_t;

/**
 * Configure and initialize a single-wire TMC2209 GPIO/timer slot.
 *
 * Preconditions and Startup Contract:
 * - Must be called from ready task context with interrupts enabled (irq_save() == 0),
 *   never from an interrupt handler or DECL_INIT boot hook.
 * - Scheduler clock adapter must be in KM_CLOCK_STATE_READY.
 * - Neither Klipper nor this binding may be in global shutdown.
 * - Validates slot index (0 <= slot < KM_MAX_LANES), non-NULL config, and non-NULL force_off.
 * - Validates the actual timer clock (integral MHz, exactly divisible by 40000 baud,
 *   bit period >= 16 raw ticks) and bounded transaction/diagnostic deadlines.
 * - Validates portable TMC2209 configuration parameters before touching physical GPIO.
 * - Rejects duplicate setup on an already claimed slot (KM_ERR_BUSY).
 * - Checks for pin collisions against all previously claimed slots (KM_ERR_CONFLICT), except
 *   exactly one legitimate sharing: another already-active slot on the same pin whose TMC2209
 *   config address differs (Happy Hare's classic ERCF 1.1 EASY-BRD wiring, where the gear's and
 *   the selector's TMC2209 share one single-wire UART pin at distinct addresses;
 *   docs/selectors.md). Equal addresses on one shared wire are refused with KM_ERR_CONFLICT just
 *   as clearly. A shared slot reuses the existing physical channel outright (no second GPIO/pad
 *   claim); tmcuart_local_submit's own busy check serializes every sharer's transactions onto
 *   that one wire, and each slot's own confirmation, restart and fault stay entirely its own.
 * - Pin/slot ownership is immutable once physical setup begins; claimed pins remain unavailable
 *   for reuse even if physical setup subsequently fails, until MCU reset -- including for the
 *   sharing case above, which requires the earlier slot to have reached a working channel.
 * - Caller must have established complete resource ownership and actuator safe-off before setup.
 * - Actuator safe-off is immediately asserted via force_off(context).
 * - The shared upstream tmcuart_local_setup owns the single-wire pad and bit timers.
 *   Upstream idle is driven high; this adapter performs no GPIO operations.
 * - Initialization is asynchronous: KM_OK return indicates successfully configured and
 *   initializing, never proven chip readiness. Chip readiness is achieved later via real
 *   controller register confirmation and diagnostics.
 * - A scheduler safety timer expires each pending transaction at its own configured
 *   timeout. Once ready, the two-read GSTAT/DRV_STATUS diagnostic sequence also
 *   has one fixed stale expiry at next_diagnostic_us + 2 * transaction_timeout_us;
 *   starting the second read does not renew that overall bound. Setup validates
 *   all converted deadlines stay below the raw tick half range.
 * - Never positively enables an actuator, creates a host instance, attaches commands, or reports
 *   a TMC current setting as measured current.
 *
 * @param slot Logical slot index (0 <= slot < KM_MAX_LANES).
 * @param pin Canonical GPIO pin ID for single-wire UART.
 * @param pull_up True to enable pull-up during the shared driver's receive-input phase.
 *        Idle is driven high by upstream tmcuart.c.
 * @param config Pointer to caller-supplied TMC2209 configuration (copied stably).
 * @param force_off Mandatory immediate actuator safe-off callback.
 * @param context Opaque caller context passed to force_off callback.
 * @return KM_OK on asynchronous setup and task dispatch,
 *         KM_ERR_INVALID_PARAM if parameters, frequency, or config are invalid, or IRQs disabled,
 *         KM_ERR_UNAVAILABLE if scheduler clock is not yet READY,
 *         KM_ERR_BUSY if slot is already claimed,
 *         KM_ERR_CONFLICT if pin is already claimed by another slot,
 *         KM_ERR_FAULTED if system or binding is in fault/shutdown.
 */
KM_API km_result_t km_tmc_uart_gpio_setup(uint32_t slot,
                                          uint32_t pin,
                                          bool pull_up,
                                          const km_tmc2209_config_t *config,
                                          km_tmc_uart_force_off_fn force_off,
                                          void *context);

/**
 * Query whether the TMC2209 on the given slot has confirmed configuration and is ready.
 * Pure read-only query; never starts work, advances time, or auto-recovers.
 *
 * @param slot Logical slot index.
 * @return True if claimed, active, and chip is ready.
 */
KM_API bool km_tmc_uart_gpio_is_ready(uint32_t slot);

/**
 * Query current driver fault status for the given slot.
 * Pure read-only query.
 *
 * @param slot Logical slot index.
 * @return Driver fault code, or KM_FAULT_DRIVER_FAULT if slot invalid/unclaimed.
 */
KM_API km_fault_t km_tmc_uart_gpio_get_fault(uint32_t slot);

/**
 * Retrieve a copied snapshot of the slot's current status and current setpoints.
 * Pure read-only query; does not expose a mutable controller pointer.
 *
 * @param slot Logical slot index.
 * @param out_status Receives copied status snapshot.
 * @return KM_OK on success,
 *         KM_ERR_INVALID_PARAM if pointers NULL or slot out of range,
 *         KM_ERR_UNAVAILABLE if slot is not claimed or inactive,
 *         KM_ERR_FAULTED if system or binding is in shutdown.
 */
KM_API km_result_t km_tmc_uart_gpio_get_status(uint32_t slot,
                                               km_tmc_uart_status_t *out_status);

/**
 * Explicitly restart a slot after an ordinary local fault.
 *
 * From IRQ-enabled task context: forces actuator off, cancels the shared transfer,
 * applies a new 256-bit quiet guard, and restarts register confirmation explicitly.
 *
 * @param slot Logical slot index.
 * @return KM_OK on successful restart initiation,
 *         KM_ERR_INVALID_PARAM if slot out of range,
 *         KM_ERR_UNAVAILABLE if slot not claimed,
 *         KM_ERR_INVALID_PARAM if interrupts are disabled,
 *         KM_ERR_FAULTED if system or binding is in shutdown.
 */
KM_API km_result_t km_tmc_uart_gpio_restart(uint32_t slot);

/**
 * Asynchronously request a new run/hold current setpoint.
 *
 * From IRQ-enabled task context: accepted changes force the actuator off and clear
 * readiness until register confirmation and diagnostics finish. Early rejections
 * preserve existing monitoring; not a live-motion-current guarantee.
 *
 * @param slot Logical slot index.
 * @param run_ma Target run current in milliamperes.
 * @param hold_ma Target hold current in milliamperes.
 * @return KM_OK on asynchronous acceptance,
 *         KM_ERR_INVALID_PARAM if parameters out of range,
 *         KM_ERR_UNAVAILABLE if slot not claimed,
 *         KM_ERR_INVALID_PARAM if interrupts are disabled,
 *         KM_ERR_FAULTED if system or binding is in shutdown.
 */
KM_API km_result_t km_tmc_uart_gpio_set_current(uint32_t slot,
                                                uint16_t run_ma,
                                                uint16_t hold_ma);

/**
 * Query whether a slot has been claimed and configured.
 *
 * @param slot Logical slot index.
 * @return True if slot is claimed, active, and not in shutdown.
 */
KM_API bool km_tmc_uart_gpio_is_slot_active(uint32_t slot);

/**
 * Query the canonical GPIO pin ID claimed by a slot.
 *
 * @param slot Logical slot index.
 * @param out_pin Receives claimed pin ID.
 * @return KM_OK if slot is claimed, KM_ERR_INVALID_PARAM if out_pin NULL or slot out of range,
 *         KM_ERR_UNAVAILABLE if slot is not claimed.
 */
KM_API km_result_t km_tmc_uart_gpio_get_pin(uint32_t slot, uint32_t *out_pin);

/**
 * Query whether the TMC UART GPIO binding is in latched global shutdown.
 *
 * Once latched, global shutdown remains true until MCU reset, even if upstream
 * calls clear_shutdown.
 *
 * @return True if in global shutdown.
 */
KM_API bool km_tmc_uart_gpio_is_shutdown(void);

/**
 * Emergency firmware shutdown hook registered with Klipper DECL_SHUTDOWN.
 *
 * Permanently latches global shutdown until MCU reset. Invokes only raw force-off;
 * upstream removes timers and its shared UART shutdown hook handles its pad.
 * Never reads the clock or invokes task-only shared UART methods.
 */
void km_tmc_uart_gpio_shutdown(void);

#ifdef __cplusplus
}
#endif

#endif /* KLIPPER_MMU_MCU_TMC_UART_GPIO_H_INCLUDED */
