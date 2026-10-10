// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KLIPPER_MMU_MCU_CONTROLLER_H_INCLUDED
#define KLIPPER_MMU_MCU_CONTROLLER_H_INCLUDED

#include "klipper_mmu/types.h"
#include "klipper_mmu/operation_core.h"
#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/**
 * Nominal periodic control cadence: 1 millisecond (1000 microseconds).
 *
 * The timer interrupt wakes the control task at this cadence. The task runs
 * sensor acquisition, the operation core, follower, planning and events.
 * The interrupt services only STEP/DIR edges from the adopted integer image.
 */
#define KM_CONTROLLER_PERIOD_US 1000u

/**
 * Motion lease: five control periods after the control time of the task's
 * last completed cycle. Each rate tick checks the lease before generating a
 * window, not only when output is due: once it expires, no new window is
 * generated, so already-queued motion drains and no more is added. When the
 * local stepper then goes idle while the trajectory still expects motion,
 * that is an underrun fault (stop, safe-off, resync, latch). This bounds
 * motion without fresh sensor processing to the 5 ms switch qualification
 * time (about 0.13 mm at BoxTurtle's 26 mm/s feed) plus the two-tick lead
 * and one more tick, while tolerating a few milliseconds of Klipper task
 * jitter; it is not a measured interrupt latency.
 */
#define KM_CONTROLLER_LEASE_US (5u * KM_CONTROLLER_PERIOD_US)

/**
 * Finite future scheduling lead: 1 microsecond.
 *
 * Provides a strictly future target relative to observed clock ticks when
 * programming sched_add_timer(). This lead does not guarantee preventing
 * Klipper's "Timer too close" shutdown under real MCU runtimes with ISR latency,
 * context switches, or low clock frequencies; target platforms require separate
 * execution-cost qualification.
 */
#define KM_CONTROLLER_MIN_LEAD_US 1u

/**
 * Bounded firmware-local sensor sampling callback.
 *
 * Invoked by the control task, with interrupts enabled, before km_core_update().
 * Must be non-blocking, bounded, and perform no host I/O, polling waits,
 * dynamic allocation, or direct motor actuation. Fresh readings must retain actual
 * acquisition timestamps.
 *
 * @param core Pointer to the attached operation core.
 * @param context Caller-supplied opaque context pointer.
 */
typedef void (*km_controller_sample_fn)(km_operation_core_t *core, void *context);

/**
 * Attach a caller-owned, initialized static km_operation_core_t to the controller.
 *
 * Preconditions and Startup Contract:
 * - Must be called from task context with interrupts enabled (irq_save() == 0).
 * - Clock adapter must be in KM_CLOCK_STATE_READY.
 * - Neither Klipper nor this controller module may be in global shutdown.
 * - Profile must be valid per km_profile_validate().
 * - Operation core must be clean, initialized, and stopped: op_state == IDLE,
 *   stage == STOPPED, not moving, no active path ownership, no pending terminals,
 *   no latched system/motion faults, no active trajectory, no trajectory fault or
 *   uncertain position, and trajectory step_state == IDLE.
 * - Bound drivers must be disabled, logical STEP inactive, PWM zero, and not faulted.
 * - Rejects duplicate attachment without modifying existing attachment.
 * - Rejection does not modify outputs, change core state, or acquire a timer.
 * - Ownership remains immutable until MCU reset.
 *
 * @param core Caller-owned operation core.
 * @param sample_fn Optional bounded sensor sampling callback (may be NULL).
 * @param sample_context Opaque context pointer passed to sample_fn.
 * @return KM_OK on success,
 *         KM_ERR_INVALID_PARAM if parameters, core, profile, or bound drivers are invalid/dirty,
 *         KM_ERR_UNAVAILABLE if clock adapter is not yet READY,
 *         KM_ERR_FAULTED if system or controller is in shutdown,
 *         KM_ERR_BUSY if a core is already attached.
 */
KM_API km_result_t km_controller_attach(km_operation_core_t *core,
                                        km_controller_sample_fn sample_fn,
                                        void *sample_context);

/**
 * Exchange with the edge interrupt (commands.h notification signature).
 *
 * Task context only. Publishes the core's trajectory (a new image, a stop or a
 * recovery) and collects the edge service's output (position, pulse count,
 * pulse in flight, deadline, faults) into it, rearming the timer if needed.
 * An edge fault not yet seen stops the core. Callers outside the control task
 * call it before using the core and again after changing it. Each exchange
 * masks interrupts only for short, bounded copies.
 */
KM_API void km_controller_changed(void);

/* The control task (DECL_TASK), woken by the timer every period. */
void km_controller_task(void);

/**
 * Query whether the controller scheduler is attached to an operation core.
 */
KM_API bool km_controller_is_attached(void);

/**
 * Query whether the controller scheduler is in latched shutdown.
 */
KM_API bool km_controller_is_shutdown(void);

/* Internal callback from a successfully published GPIO switch batch. Only a
 * sample in the current control cycle can contribute to the observation. */
KM_API void km_controller_note_switch_sample(km_operation_core_t *core,
                                              uint32_t acquisition_us);
/* Latest sample-to-pre-update software interval, in microseconds. False means
 * the most recent controller cycle had no accepted switch batch/valid interval.
 * This is not a physical switch-edge latency. */
KM_API bool km_controller_get_switch_sample_delay(uint32_t *out_us);
/* Historical accepted samples since attachment; count saturates at UINT32_MAX.
 * Max and count survive invalid cycles and shutdown for read-only diagnosis. */
KM_API void km_controller_get_switch_sample_stats(uint32_t *count, uint32_t *max_us);

/**
 * Emergency firmware shutdown hook registered with Klipper DECL_SHUTDOWN.
 */
void km_controller_shutdown(void);

#ifdef __cplusplus
}
#endif

#endif /* KLIPPER_MMU_MCU_CONTROLLER_H_INCLUDED */
