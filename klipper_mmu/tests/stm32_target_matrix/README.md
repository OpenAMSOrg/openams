# Pinned STM32 compile survey

Run from the Klipper-MMU root with the pinned Klipper archive available at
`../build/klipper-ce7002bedf37e938bb483572949f3703ac6476cb.tar.gz`:

```sh
python3 -B tests/stm32_target_matrix/run.py
python3 -B tests/stm32_target_matrix/run.py --all
```

The default run builds representative F0, F2, F4, F7, G0, G4 and H7
targets. `--all` discovers each STM32 processor model for which the pinned
Klipper Kconfig makes `KLIPPER_MMU` selectable, including flash-size and
limited-code-size constraints. `--targets MACH_STM32H723 MACH_STM32F407` can
select a smaller explicit run. Each target is configured twice through
Klipper's native Kconfig library and `make olddefconfig`: add-on disabled, then
enabled as a one-lane Custom profile with explicit example pins.
F765 uses Klipper's USB PA11/PA12 transport because the pinned upstream
USART1 source does not compile for its register layout; the other listed
representatives use USART1. The selected transport is recorded per target.
The selected 64 KiB F103 and L412 targets overflow flash with this add-on's
portable core, so `make menuconfig` now hides the add-on for them. A pinned
survey before that gate showed both disabled images built and both enabled
images overflowed. The pinned `--all` survey from commit `a0d1ea0` passed all
18 selectable STM32 targets, with disabled and enabled builds for each. Its
artifacts are under `build/stm32-eligible-a0d1ea0-20260922/`.

The runner builds firmware only. It records the exact target, pinned upstream
identity, `.config`, ELF, binary and dictionary SHA256 values in
`build/stm32-target-matrix/`; complete images and per-target JSON are retained
there, including failure details. It refuses to overwrite an existing output
directory; use `--build-dir` for another run. A failed target is recorded and
the survey proceeds to remaining targets before returning failure.

These results establish **core compile-only coverage** for this pinned
Klipper revision. The one-lane pins are illustrative. They do not establish
physical support, peripheral behavior, board wiring, motion, or hardware
qualification. The release integration and hardware acceptance checks remain
separate.
