# Host-established reset epoch

`KLIPPER_MMU_BOOT_IDENTITY_HOST` is a default-off Klipper menuconfig option for hosted diagnostic startup on STM32 targets that should not, or cannot, use an on-chip RNG provider. It is an alternative to the hardware providers in [boot identity](boot-identity.md), not a replacement: those providers are unchanged and remain mutually exclusive with it.

## What it is

- A lifetime discriminator. The Klipper host generates a fresh nonzero 64-bit value with `secrets.randbelow` on every `klippy:connect` and sends it as `km_diagnostic_start_host epoch_hi=%u epoch_lo=%u`.
- Not MCU entropy, not physical entropy qualification, and not cryptographic authentication. Anyone who can write to the MCU link before the host can bind a value. The trust boundary is the same trusted Klipper host that already sends configuration.
- Not a reset detector. A reset that Klipper's link lifecycle does not observe leaves firmware unbound and IDLE until a later connect sends a new epoch.

## Firmware latch

- The host-epoch build registers only `km_diagnostic_start_host`. The no-argument `km_diagnostic_start` is not registered.
- The first nonzero epoch received while unbound and `IDLE` binds it for the MCU lifetime and starts the shared diagnostic attempt (same deadline, board setup, and `km_commands_attach(..., allow_start=false)`).
- A zero epoch never binds or starts; it latches `FAULTED` with `KM_ERR_INVALID_PARAM`.
- Any later epoch in `WAITING`, `READY`, or `FAULTED`, after disconnect, or after shutdown is ignored. Shutdown is a one-way `FAULTED` latch and cannot reopen binding. Only an MCU reset clears it.
- No hardware provider is linked, so there is no RNG, timer, or deterministic fallback.

## Host ordering

`_build_config` validates `KLIPPER_MMU_BOOT_IDENTITY_HOST` as the integer 0 or 1 (absent means 0). When it is 1, the host does not add the `km_diagnostic_start` init command. `_connect` sends the epoch after Klipper's MCU connect handler has finished identify, config/CRC confirmation, and registered post-init callbacks, and before the first observation. Epochs are independent of transport and diagnostic query challenges. A host restart against an already-configured READY MCU sends a new epoch that firmware ignores, so the existing instance is preserved.

Errors raised by host commands during connect follow the existing connect behavior; reconnect error handling is tracked separately (P7) and is not changed here.

## Admission

The option only replaces the identity requirement. It depends on `MACH_STM32` and on no hardware provider being selected. The existing F207, F4x5 (F405/F407/F429), F765, G4 (G431/G474) and H743/H750 profile gates accept either their RNG option or the host epoch; each still requires Custom/1 or NightOwl/4, passive rewind, switched buffer, manual pin configuration, local GPIO, and shared TMC UART. H723 still requires its BoxTurtle, EMU, or NightOwl gate. The RNG clock and USB-route constraints are provider constraints and do not apply to the host epoch.

`diagnostic_startup.c` rejects the host epoch together with any hardware provider, and on any STM32 target without an existing profile gate. Host identity does not grant a peripheral, pin route, or motion; `ACTUATION` stays 0 unless the separate opt-in is selected. G0B1 and other targets are not admitted by this change; a future conversion must compose its own driver gate.

## Evidence

- `tests/klipper_diagnostic_startup/run_tests.py --mode host_epoch` (and `host_epoch_actuation`) runs the shared native cases that do not depend on a provider result, plus `host_zero` and `host_rebind`. The fixture defines no `km_boot_identity_get`, so the link proves no provider call.
- `tests/klipper_diagnostic_startup/test_f407_gate.py` compiles host-epoch startup for admitted targets and rejects two lanes, a combined hardware provider, and an unadmitted STM32.
- `tests/klipper_host/run_connection.py` checks ordering against pinned Klipper's `MCUConfigHelper`, a fresh epoch per connect, restart preservation with a modeled latch, and constant validation.

Fresh review checks passed 72 switched, 72 actuator, 66 host-epoch and 66 host-epoch actuator native scenarios, plus target compile gates. The integrated host connection suite passes 53 checks against pinned Klipper wrappers (serial/MCU lifecycle is modeled). A focused F407 Custom safe image resolved through native Kconfig and linked with only the host-epoch start command in its exact dictionary; that uncommitted candidate evidence is at that run's `build/review-host-epoch-link/`. It predates the occupied-path command and is not the combined qualification release.

Hardware and physical reset behavior are not exercised by these checks. The menu interface remains upstream Klipper menuconfig; the focused compile used its native Kconfig engine.
