# STM32H723 boot identity

Default-off experimental provider of one opaque 64-bit MCU identity. It reads the on-chip RNG once per MCU reset. The provider itself does not call `km_commands_attach`, start motors, or change actuation. The separate opt-in H723 diagnostic startup can consume this identity and attach the control core with immutable START permission false. Firmware remains `ACTUATION=0`; installed-board qualification is pending.

## Build

`KLIPPER_MMU_BOOT_IDENTITY_H723` is a bool in the existing `KLIPPER_MMU` menu. Default n. It depends on `MACH_STM32H723`. Select it with Klipper menuconfig. No second configuration system is added.

`mcu/boot_identity_h723.c` is compiled only when the MMU and this option are both `y` on the H723 target. Any other target, or an off build, gains no source line and no link dependency. Compiling that file without `CONFIG_MACH_STM32H723` and `CONFIG_KLIPPER_MMU_BOOT_IDENTITY_H723` stops with a preprocessor error.

## API

`km_boot_identity_get(uint64_t *out)`

- `KM_ERR_INVALID_PARAM` when `out` is NULL. The pointer is not written.
- `KM_ERR_UNAVAILABLE` before the one-shot sequence finishes.
- `KM_OK` only when a completed nonzero identity is stored and this module has not latched shutdown. `*out` is written only in that case.
- A latched `KM_ERR_TIMEOUT`, `KM_ERR_CONFLICT` (RNG peripheral clock already enabled), or `KM_ERR_FAULTED` (clock/seed flags, zero candidate, or shutdown).

There is no seed or value setter. The getter does not start, reset, or reclaim the provider and does not authorize motion. No command or host API is added.

Timer ticks schedule deadlines only. They are not entropy. There is no hash, ADC, wall time, or host nonce fallback. Identity bits stay on the MCU.

The separate default-off `KLIPPER_MMU_BOOT_IDENTITY_HOST` option is not a provider and is mutually exclusive with every hardware provider. A host-epoch build links no provider and never calls this getter; hosted diagnostic startup binds a Klipper-generated epoch instead. That epoch is a lifetime discriminator, not MCU entropy. See [host epoch integration](host-epoch-integration.md).

## Lifetime

`DECL_INIT` only wakes a task. The task runs one bounded nonblocking step per wake. A module one-shot timer wakes that task about every 100 µs while a wait is open. The timer callback only wakes the task and returns `SF_DONE`. It does not touch the RNG, the MMU, or the wire.

Each hardware wait (HSI48 ready, conditioning reset clear, each of the two data words) has its own 2 ms deadline taken from Klipper's timer when that wait starts. A result that becomes ready at or after the deadline is a timeout, including when the task was starved. Deadlines use `timer_is_before`.

The provider keeps at most one identity. Publication stores both words together under `irq_save` / `irq_restore`: the first word is the high 32 bits and the second is the low 32 bits. A single word is never returned. After success, timeout, conflict, hardware fault, zero result, or shutdown, the phase is terminal. Repeated init, reconnect, `sched_clear_shutdown`, and a soft config reset do not generate another value. Static state is fresh only after an MCU reset. The shutdown latch remains set across a cleared scheduler shutdown flag.

If shutdown is already pending, the task does not program the RNG. The shutdown hook latches `KM_ERR_FAULTED` even if init has not run. When this module owns the RNG clock it clears `RNGEN` and does not turn HSI48 off.

## Clock and registers

The H723 Klipper tree has no RNG driver. This file is a small missing-peripheral provider in the add-on, not a second driver framework.

The RNG bus clock is claimed only when `is_enabled_pclock((uint32_t)RNG)` is clear. Claim and `enable_pclock((uint32_t)RNG)` run in one critical section. An already enabled clock latches `KM_ERR_CONFLICT` and does not write RCC or RNG and does not reset that owner. After claim, the clock stays enabled through success and failure until MCU reset. Other RCC enable bits are left to the existing helper.

HSI48 is started with a read-modify-write of `RCC->CR` that sets `HSI48ON` only. It may already be running for USB. The provider waits until `HSI48RDY` or 2 ms. It does not change oscillator calibration and does not disable HSI48 on success, error, or shutdown. After ready, it clears only `RCC_D2CCIP2R_RNGSEL` so the RNG mux is HSI48 (value 0). Other mux fields stay as they were.

Configuration then disables the generator and its interrupt, writes `RNG_CR = 0x00F00D00 | CONDRST`, writes HTCR unlock `0x17590ABC` and immediately `0x0000AA74` in one short critical section, and clears `CONDRST`. The 2 ms conditioning wait rejects `CECS`, `CEIS`, `SECS`, and `SEIS`. `RNGEN` is then set with interrupts still off.

Exactly two data-register reads are attempted, at most one per task wake. The same four status flags are checked before and after each read. `DRDY` is not required to fall between reads. A flag error or a zero 64-bit pair rejects the candidate with no retry. `RNGEN` is cleared when the owned generator completes or fails. The bus clock and HSI48 stay on.

## What review can and cannot show

Native register and scheduler fixtures, enabled and disabled H723 builds, and command-replay composition against this provider can check refusal of a foreign RNG, preservation of USB HSI48 and unrelated mux bits, stuck HSI48, conditioning reset, and `DRDY`, deadline wrap, status flags around each read, no partial or zero publication, shutdown in every phase, and no second identity after `clear_shutdown`.

Those fixtures do not prove physical entropy quality. Hardware qualification on a real board is still required and is not authorized here. The diagnostic startup uses this identity for read-only command attachment; that does not grant operation admission.

## STM32H743/H750 (shared classic-RNG provider)

H743 and H750 do not use this H723 file or its AN4230 `CR`/`HTCR` recipe. The pinned `stm32h743xx.h` and `stm32h750xx.h` define only `RNG->CR`/`SR`/`DR` with `RNGEN`, `IE`, and `CED`; there is no `CONDRST` or `HTCR`. `KLIPPER_MMU_BOOT_IDENTITY_H743_H750` (default n) instead compiles the shared `mcu/boot_identity_rng_stm32.c` lifecycle used by F207/F405/F407/F765: owned AHB2 `RNGEN` only when initially off, `CR = RNGEN` (clock-error detection left enabled), startup-word discard, continuous-word comparison, `CECS`/`SECS`/`CEIS`/`SEIS` checks around each read, one 10 ms bounded deadline, and clock release after success or failure.

The option is limited to USB on PA11/PA12, an external crystal, and upstream's default core clock (H743 400 MHz, H750 480 MHz). Before claiming the RNG clock the provider checks, without writing any of them: PLL1 on, ready and selected as the system clock; HSE ready; `PLLCKSELR` source and `DIVM1`; `PLL1RGE`, wide VCO, integer mode, and `DIVP1EN`; `PLL1DIVR` `N1` and `P1` equal to pinned `stm32h7.c`; `D1CPRE`=1 and `HPRE`=2; `HSI48ON`/`HSI48RDY`; and `D2CCIP2R` with `USBSEL`=HSI48 and `RNGSEL`=HSI48. Once PLL1 is the system clock, a mismatch is a terminal `KM_ERR_CONFLICT`. USB's HSI48 and the selectors are never changed. The internal-oscillator and PB14/PB15 routes are rejected at compile time.

Native fixtures cover these checks with an MMIO model. Real-CMSIS object compiles use the pinned H743/H750 headers. Neither measures entropy on hardware.

## Sources

ST AN4230 Rev 14 (July 2026), section 5.1.3 Table 3, is the selected H72x/H73x 48 MHz configuration (`CR` `0x00F00D00`, `HTCR` `0xAA74`). That table row is not treated as a certificate.

The HAL reference is frozen at `e3c518ebda3e00c8e52b1625ca44fba65da3b63e` (2026-09-04). Its sequence is a register-programming reference. Its 3.2 `HTCR` `0x7274` is intentionally not programmed. The cause of that source discrepancy is not claimed.

- https://www.st.com/resource/en/application_note/an4230-introduction-to-random-number-generation-validation-using-the-nist-statistical-test-suite-for-stm32-mcus-and-mpus-stmicroelectronics.pdf
- https://github.com/STMicroelectronics/stm32h7xx-hal-driver/tree/e3c518ebda3e00c8e52b1625ca44fba65da3b63e
