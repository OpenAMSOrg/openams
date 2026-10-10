// SPDX-License-Identifier: GPL-3.0-or-later
#ifndef KLIPPER_MMU_BOOT_IDENTITY_H
#define KLIPPER_MMU_BOOT_IDENTITY_H

#include "klipper_mmu/types.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Opaque nonzero per-reset epoch from a verified target-specific entropy
 * provider. Providers must fail closed on unavailable clocks, RNG health, or
 * continuous-test errors; timer-only and constant identifiers are not valid
 * substitutes. No seed input. *out changes only on KM_OK.
 *
 * KLIPPER_MMU_BOOT_IDENTITY_HOST builds link no provider and never call this;
 * diagnostic startup binds a host-supplied epoch instead, with no fallback. */
KM_API km_result_t km_boot_identity_get(uint64_t *out);

#ifdef __cplusplus
}
#endif

#endif /* KLIPPER_MMU_BOOT_IDENTITY_H */
