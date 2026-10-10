# Passive-feeder STM32 hosted startup candidates

The pinned release matrix saves safe and actuator-permitted USB H723 images
for six independent-feeder software candidates: KMS, Angry Beaver, 3MS and
QuattroBox 1.0/1.1/2. All six share the same four-lane, no-PWM/no-ADC shape
and NightOwl's local GPIO/TMC UART startup path, but their sensor models
differ, matching their traced Happy Hare profiles (`docs/devices/*.json`):

- KMS has a per-lane LOAD/exit switch (`KLIPPER_MMU_LOAD_SENSORS`) and no
  separate shared HUB -- the owning lane's LOAD switch is its virtual hub
  boundary (`KLIPPER_MMU_VIRTUAL_HUB`, the same shape as EMU).
- Angry Beaver, 3MS and QuattroBox 1.0/1.1/2 have no per-lane BAY/entry switch
  (`KLIPPER_MMU_BAY_SENSORS=n`) and home to one shared, real, physical HUB
  input instead.

The Custom pin map, active levels, pulls, UART addresses, and current values
in the matrix are explicit examples for compile and hosted tests; they are
not verified hardware wiring for any of these devices.

Angry Beaver is also this catalogue's one G0B0/G0B1 row: Happy Hare's own
default board for it is real hardware (`BOARD_TYPE_MMB_2_0`,
`installer/mmu_types/Kconfig.angry_beaver:35`), an STM32G0B1 board that boots
through the host-established epoch (`KLIPPER_MMU_BOOT_IDENTITY_HOST`), the
same as `klipper_nightowl_runtime`'s own G0B row. `run_runtime.py` derives
the saved image's target from its `.config` (H723 vs G0B0/G0B1) and picks the
matching boot-gate Kconfig symbol and diagnostic-start command (`km_
diagnostic_start` vs the host-epoch `km_diagnostic_start_host`) the same way
`klipper_nightowl_runtime/run_runtime.py` already does; no other device here
has a G0B row.

`run_runtime.py --device <kms|angry_beaver|3ms|quattrobox_1_0|quattrobox_1_1|quattrobox_2>`
checks the saved image manifest, Kconfig values (including its BAY-sensor and
virtual-hub setting), and Klipper command dictionary against that device's
expected profile, then compiles the production startup and controller
sources against that image configuration (reusing
`tests/klipper_emu_runtime/Makefile` with `-DKM_TEST_PASSIVE_RUNTIME=1`, the
same pattern `klipper_nightowl_runtime` uses for
`-DKM_TEST_NIGHTOWL_RUNTIME=1`). The safe image reaches diagnostic READY but
rejects START with every feeder idle. The actuation image drives whichever of
BAY, HUB/virtual-hub-LOAD and TOOLHEAD that device's profile has through
production LOAD, FOLLOW, and UNLOAD commands, terminal retention/ACK, lane
isolation, and safe-off.

This is hosted software evidence only. It does not establish STM32 pin
routing, electrical levels, TMC UART response, motor current, step timing, or
mechanical operation for any of these six devices. Do not treat the example
pins or actuation image as qualified hardware settings. PRELOAD and EJECT are
not separately exercised by this hosted harness, matching the existing
NightOwl/EMU runtime tests' scope; see `docs/plan/device-and-stm32-rollout.md`
for the full list of recorded gaps (encoder, espooler, per-device speed/accel
tuning).
