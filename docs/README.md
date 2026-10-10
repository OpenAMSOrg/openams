# OpenAMS documentation

Everything written down about OpenAMS is indexed here. OpenAMS has two parts —
the Klipper host plugin in `src/` and `moonraker/`, and the klipper-mmu MCU
add-on in `klipper_mmu/` — and both are documented in this one set. The
[repository README](../README.md) is the entry point for what OpenAMS is and
how to install it; [install.agents.md](../install.agents.md) is the same install
written for AI agents, with its tools in [`tools/agent/`](../tools/agent/).

New here? The [user guide](guide/README.md) covers install, configuration, daily
use and troubleshooting.

## Contracts

The interface contracts between the pieces. Each is a versioned schema
document whose example payloads double as test fixtures, and each carries a
marker for every claim — see the
[contracts index and legend](contracts/README.md) for what EXISTS, TO ADD and
OPEN mean.

| Contract | Between | Document |
|---|---|---|
| **C1** | OpenAMS units ↔ `openams` host (the unit wire protocol) | [`C1_firmware_host.md`](contracts/C1_firmware_host.md) |
| **C2** | `openams` host → UIs (status objects, webhooks) | [`C2_status.md`](contracts/C2_status.md) |
| **C3** | UIs → `openams` host (G-code commands) | [`C3_commands.md`](contracts/C3_commands.md) |
| **C4** | Moonraker `openams_spoolman` ↔ host ↔ Spoolman | [`C4_spool_linking.md`](contracts/C4_spool_linking.md) |
| **C5** | klipper-mmu firmware ↔ host ↔ `oams_manager` | [`C5_klipper_mmu_wire.md`](contracts/C5_klipper_mmu_wire.md) |

C2 and C4 have machine-readable schemas in `contracts/schemas/` and worked
payloads in `contracts/examples/`; `python3 docs/contracts/check_examples.py`
validates the examples against the schemas.

## Testing without hardware

- [klipper-mmu's whole-stack simulators](../klipper_mmu/README.md#how-it-is-tested)
  — the real MCU add-on image run as a Linux process against a simulated MMU,
  with a real Klipper host and OpenAMS on top.

## UI API

- [UI API](UI_API.md) — the versioned, AFC-independent status, webhook and
  command surface exposed by `oams_manager` for touchscreen front ends, and how
  to upgrade an existing installation onto it.

## klipper-mmu

klipper-mmu is the OpenAMS part that runs MMU motion on the MMU's own
microcontroller. Its documentation is in
[klipper_mmu/docs/](../klipper_mmu/docs/); start at its
[README](../klipper_mmu/README.md) and its
[architecture](../klipper_mmu/docs/architecture.md).

- **Install guide** — [docs/install.md](../klipper_mmu/docs/install.md):
  building the add-on, wiring an MMU, and the host side. The repository-level
  install is in the [root README](../README.md#install), and printers
  moving off the separate klipper-mmu repository start at the
  [migration guide](../klipper_mmu/docs/migrating-to-openams.md).
- **Checks guide** — [docs/checks.md](../klipper_mmu/docs/checks.md): what
  `make check` and the portable gate run.
- **Device dossiers** — [docs/devices/](../klipper_mmu/docs/devices/README.md):
  one page per supported MMU, generated from its behavior spec, with the gates,
  arrival sensor, buffer, rest positions and the decisions taken. There is a
  machine-readable `.json` beside each page.
- **Protocol and operation** —
  [protocol contract](../klipper_mmu/docs/protocol-contract.md),
  [G-code commands](../klipper_mmu/docs/g-code-commands.md),
  [selectors](../klipper_mmu/docs/selectors.md),
  [diagnostics](../klipper_mmu/docs/diagnostics.md),
  [motion core](../klipper_mmu/docs/motion-core.md) and
  [why the firmware owns the loop](../klipper_mmu/docs/why-firmware-owned.md).
- **Bring-up by device** — the
  [BoxTurtle quick start](../klipper_mmu/docs/boxturtle-quick-start.md), the
  [BoxTurtle H723 bench acceptance](../klipper_mmu/docs/boxturtle-h723-acceptance.md)
  record, the [migration report](../klipper_mmu/docs/migration.md) for Happy
  Hare and AFC users, and [local development](../klipper_mmu/docs/local-doc.md).

## Boards and migration

- [STM32F072 mainboard](stm32f072.md) — the original OpenAMS replacement
  mainboard under this host: what is detected, what is taken from the host
  instead of the firmware, and the config differences for owners moving from
  `klipper_openams`.

## Project documents

- [ARCHITECTURE.md](../ARCHITECTURE.md) — how the system is put together,
  including the HELIX binding and the protocol-v4 generation boundary
  (sections 11 and 11.1).
- [klipper-mmu implementation status](../klipper_mmu/IMPLEMENTATION_STATUS.md)
  — what works today, and what is still open.
