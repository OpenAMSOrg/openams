// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KLIPPER_MMU_COMMANDS_H
#define KLIPPER_MMU_COMMANDS_H

#include <stdbool.h>
#include "klipper_mmu/operation_core.h"

/* Explicit firmware attachment, not a host configuration or activation command.
 * Call once from ready task context, IRQs enabled. The caller owns a static,
 * initialized core, verified nonzero MCU-instance identity and exclusive hardware.
 * allow_start is committed only on success, beside the controller, identity and
 * callback. true is refused unless CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN is on,
 * and is for a firmware caller that already owns validated hardware.
 * Diagnostic attachment passes false;
 * only its firmware startup may later enable START. No host setter exists.
 * Attachment never initializes, clears, enables
 * or takes ownership of the core's physical path.
 *
 * changed() is mandatory: under the controller critical section it must update
 * the local trajectory timer after command mutation (including fault/rejection).
 * It must be bounded, non-reentrant and perform no host communication. The caller
 * also owns periodic core/sensor/lease service and raw, clock-free safe-off in
 * global shutdown. This command boundary is not the physical scheduler.
 * Attachment and snapshot query high-water marks persist until MCU reset. */
KM_API km_result_t km_commands_attach(km_operation_core_t *core, uint64_t instance,
                                     void (*changed)(void), bool allow_start);
/* Diagnostic startup attaches with allow_start=false. A build-gated caller may
 * enable after READY publication; revocation is permanent until MCU reset. */
/* ready() is a bounded, IRQ-safe physical readiness check, sampled on every
 * START. A failed check permanently revokes this attachment's permission. */
KM_API km_result_t km_commands_enable_start(km_operation_core_t *core, uint64_t instance,
                                           bool (*ready)(void));
KM_API void km_commands_revoke_start(km_operation_core_t *core, uint64_t instance);
/* Read-only explicit diagnostic command; echoes challenge and actual instance. */
void command_km_transport_query(uint32_t *args);
void command_km_switch_delay_query(uint32_t *args);

enum km_command_kind {
    KM_COMMAND_START = 1, KM_COMMAND_CANCEL, KM_COMMAND_HEARTBEAT, KM_COMMAND_STOP,
    KM_COMMAND_EVENT_PEEK, KM_COMMAND_EVENT_ACK, KM_COMMAND_TERMINAL, KM_COMMAND_SNAPSHOT
};
enum km_command_result {
    KM_COMMAND_OK, KM_COMMAND_UNBOUND, KM_COMMAND_INSTANCE_MISMATCH,
    KM_COMMAND_INVALID, KM_COMMAND_STALE, KM_COMMAND_EMPTY, KM_COMMAND_SHUTDOWN,
    KM_COMMAND_UNAVAILABLE
};
/* km_transport_state `state`: binding and clock, not start permission. */
enum km_binding_state {
    KM_BINDING_UNBOUND, KM_BINDING_ATTACHED, KM_BINDING_SHUTDOWN,
    KM_BINDING_CLOCK_UNAVAILABLE
};

#endif
