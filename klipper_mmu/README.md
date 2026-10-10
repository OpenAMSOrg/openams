# Klipper-MMU

**Klipper-MMU is part of OpenAMS.** It lives in `klipper_mmu/` of the
[OpenAMS repository](../README.md), is installed by OpenAMS' `install.sh`, and
its units appear in OpenAMS as ordinary units. Its own documentation set is
indexed with the rest in [docs/README.md](../docs/README.md). This file
describes the add-on itself.

Your multi-material unit is a small robot whose whole job is shoving plastic
noodles through tubes at the right moment. Most Klipper MMU setups make that
robot phone home for every single move: the printer host plans each feed, ships
it to the MCU, and hopes the USB cable is in a good mood. Klipper-MMU lets the
robot think for itself. The MMU's own microcontroller runs the load, the unload
and the safety checks locally, from its own sensors, and tells the host how it
went.

It is an **optional add-on to upstream Klipper**, not a fork. Turn it off in
`make menuconfig` and you have exactly the Klipper you started with.

> **Safety first, jokes second.** Nothing here has been qualified on real MMU
> hardware yet. Every device below is a *software candidate*: it builds, it
> boots, and it passes the simulators, and that is all it has earned. Default
> images advertise `ACTUATION=0` and will not move a motor. The actuator build
> is an explicit, experimental opt-in; do not flash it onto a printer you care
> about until you have checked pins, polarities, currents and safe-off on your
> own bench.

## Why the MMU should own its own loop

Filament feeding is a feedback problem: push until a switch trips, follow the
buffer, stop when something jams. When the host owns that loop, every decision
waits on the host, the transport and the host's scheduler, and a stalled host is
a runaway motor. Klipper-MMU keeps motion, following and protection on the MCU,
and the host only asks for operations (PRELOAD, LOAD, UNLOAD, EJECT, CALIBRATE)
and hears how they ended. If the host goes quiet, a lease on the MCU expires on
its own and outputs stop. A fault stops outputs and stays latched until someone
reconciles the real physical state; reconnecting never quietly restores motion.

The long version, with receipts from Happy Hare's and AFC's own source, is in
[why the firmware owns the MMU loop](docs/why-firmware-owned.md).

## What it drives

One shared, data-driven control core runs every device; there is no
per-device copy of the controller. Device profiles come from Happy Hare's and
AFC's own configurations (traced, cited, never guessed), and every pin, polarity
and pull stays editable.

| Device | How it picks a lane | Lanes | Notes |
| --- | --- | --- | --- |
| BoxTurtle | one feeder per lane | 4 / 8 | powered rewind; switched or analog (DiffA/DiffB) buffer |
| NightOwl | one feeder per lane | 2 / 4 | passive rewind; also built for most other STM32 targets |
| EMU | one feeder per lane | 5 | each lane's LOAD switch doubles as the hub |
| KMS | one feeder per lane | 4 | virtual hub, with BAY and LOAD switches |
| QIDI Box | one feeder per lane | 4 | plain STEP/DIR gear (no TMC UART), tension-only buffer |
| Angry Beaver, 3MS, QuattroBox 1.0 / 1.1 / 2 | one feeder per lane | 4 | no per-lane BAY switch; gates are learned the Happy Hare way |
| ERCF 1.1 / 2.0 / 3.0, Tradrack | linear selector + grip servo | 9 / 8 / 8, 10 | ERCF encoder; Springy, Triple Decky, Thumper Blocks and Jack Rabbit options |
| 3D Chameleon, HTLF, MMX6 | rotary selector | 4, 4, 6 | per-gate gear direction |
| MMX, Low Rider, Pico MMU | servo selector | 4, 6, 4 | one servo angle per gate |
| BTT ViViD, Claymore | indexed selector | 4 | a switch per gate; ViViD takes the short way round, Claymore only goes forward |

Stock Bambu AMS units and OpenAMS keep their own firmware and transport;
klipper-mmu does not run on them. The full per-device story, including what is
still missing for each one, lives in the
rollout plan and the
[device dossiers](docs/devices/README.md).

**MCUs.** Every device builds for the STM32H723 (the AFC Lite board). NightOwl
and the generic Custom profile also build for F207, F405, F407, F429, F765,
G431, G474, H743, H750, G0B0 and G0B1. Angry Beaver and ERCF 2.0/3.0 build for
the G0B0/G0B1 on BTT's MMB board, which is what Happy Hare ships them on. On 128
KiB parts, Klipper-MMU's own code builds size-optimised so an 8-lane ERCF still
fits with room to spare.

## What it knows how to do

- **Load and unload** with the device's own sensors: BAY, LOAD, a real or
  virtual HUB, a toolhead switch, or buffer (FPS) arrival. Speeds, gearing,
  parking distances and timeouts come from each device's own dossier, not from
  one device's numbers stretched over everyone else.
- **Follow** the extruder from a switched or analog buffer, and never decide a
  jam or an arrival from buffer pressure alone.
- **Selectors** of every kind (linear, rotary, servo, indexed), homing on first
  use, with Happy Hare-style calibration saved on the host
  ([selectors](docs/selectors.md)).
- **An encoder** (ERCF) with Happy Hare's distance-based clog detection: no
  encoder click over 10 mm of gear travel is a stall, however slowly you print.
- **TMC2209 drivers** over Klipper's own UART code, with confirmed currents and
  driver diagnostics, or plain STEP/DIR where the hardware has no UART.
- **Explain itself**: `KLIPPER_MMU_DIAGNOSE` names the switch, lane or driver at
  fault in plain words ([diagnostics](docs/diagnostics.md)).

## Getting started

1. Install OpenAMS from the repository root — the [root README](../README.md#install)
   covers the whole system, and the same `install.sh` also links this host
   module and, with `--with-mcu-addon`, the MCU add-on. For the add-on on its
   own, read the [installation guide](docs/install.md): `tools/install.py
   install` adds Klipper-MMU's build hooks to a Klipper checkout, `check`
   inspects them and `remove` takes them out again; it changes only those MCU
   source hooks. The host module is a separate, equally optional step.
2. In Klipper's `make menuconfig`, pick your MCU first, then enable Klipper-MMU
   and choose the device, an optional wiring preset, the lane count and your
   pins.
3. Add a `[klipper_mmu NAME]` section on the host (for example
   `[klipper_mmu box]`). Every command is listed in the
   [G-code command reference](docs/g-code-commands.md).
4. Coming from Happy Hare or AFC? The
   [migration report](docs/migration.md) reads your existing configuration
   (read-only) and shows what maps where.

BoxTurtle owners can take the shortcut: the
[BoxTurtle quick start](docs/boxturtle-quick-start.md) goes from an AFC Lite
board to a first LOAD in one page.

## How it is tested

Since there is no qualified hardware yet, the tests carry the weight, in four
layers:

- **Native suites**: the portable C core, built with AddressSanitizer and
  UndefinedBehaviorSanitizer. `make test-summary`.
- **The portable gate**: native suites, host tests, hosted-startup fixtures and
  docs checks, all in one command. `python3 tools/check.py`
  ([what it runs](docs/checks.md)).
- **Whole-stack simulators**: the real firmware image runs as a Linux process
  against a simulated MMU (motors, switches, buffer, encoder, TMC chips), with a
  real Klipper host and OpenAMS on top. Twenty devices run their operations
  end to end (PRELOAD, CALIBRATE, LOAD, extrude, UNLOAD, as far as each device
  supports them):
  `make -f tests/linux_mcu/Makefile test-devices`, plus `make check-integration`
  for BoxTurtle. These found most of the interesting bugs
  ([how the sim works](tests/linux_mcu/README.md)).
- **Release matrix**: every device image on every STM32 target, built from a
  pinned Klipper, with saved-configuration and hosted-runtime checks.
  `python3 tests/release_matrix/run.py`.

No hardware or flashing is needed for any of it.

## Finding your way around

| Where | What |
| --- | --- |
| `src/`, `include/` | the portable control core: allocation-free C, the same code in tests and firmware |
| `mcu/` | the Klipper MCU integration: menus, pin plans, drivers, startup |
| `host/` | the Klipper host module and its OpenAMS adapter |
| `sim/`, `tests/` | the simulator and every test layer above |
| `tools/` | helper scripts ([index](tools/README.md)) |
| `docs/` | everything else, indexed with the rest of OpenAMS' documentation in [docs/README.md](../docs/README.md) |

Good next reads: [architecture](docs/architecture.md),
[implementation status](IMPLEMENTATION_STATUS.md) (what works today and what is
still open), the feature map, the
[protocol contract](docs/protocol-contract.md), and the
[device dossiers](docs/devices/README.md).

## License

GPL-3.0, like Klipper. Each source file carries its SPDX header: most are
GPL-3.0-or-later and a few are GPL-3.0-only. Third-party license texts are in
`licenses/`. The license boundary between this add-on and the rest of the
repository is in the [root README's License section](../README.md#license).
