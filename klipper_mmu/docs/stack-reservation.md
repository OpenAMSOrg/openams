# Shared MCU stack reservation

Klipper-MMU uses Klipper's existing `STACK_SIZE` and linker layout. For STM32,
its native menu adds an enabled-only range of 4096–32768 bytes. The minimum
clamps upstream's ordinary 512-byte default only when the add-on is enabled;
explicit larger user values remain editable. An ARM compile assertion rejects
stale enabled configurations below 4096. With the add-on disabled, the ordinary
hidden upstream setting remains 512 bytes. There is no separate runtime stack
or patched linker.

This fixes a concrete reservation defect found during the pin-storage review.
The retained F407 LTO reports show `km_controller_timer_event` using 104 bytes,
`km_core_update` using 472, and `km_trajectory_set_target_velocity` using 408.
ARM disassembly confirms these nested calls. Even the first two exceed 512;
the full path also includes deeper calls, exception frames and the interrupted
foreground stack. Checking each function against 512 was insufficient.

The 4 KiB minimum is a conservative software reserve, not a measured universal
worst-case stack bound. It must coexist with `.data`, `.bss` and Klipper's
dynamic allocation space. The normal linker rejects a target that cannot fit
this reservation. Further controller/ISR simplification and whole-call-chain
analysis remain part of the timing review; physical stack high-water and
interrupt-latency measurements belong to later hardware qualification.

Evidence: the before image and LTO reports are retained under
that run's `build/pin-config-memory/`.
All 12 native configuration/profile tests pass, including enabled defaults,
stale-small values, an explicit 8192-byte override, disabled-build equivalence
and rejection of a stale 512-byte ARM configuration at compile time. Evidence:
`build/review-stack-profile.log`.
The next clean release must carry the new reservation in every enabled STM32
artifact; earlier 512-byte images are superseded as qualification candidates.
