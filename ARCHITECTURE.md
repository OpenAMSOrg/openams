# OpenAMS Klipper plugin — architecture

This document is the durable reference for how the host plugin is structured,
how it talks to OpenAMS units, and how to validate it. It is
meant to stay accurate as the code evolves; update it with behavioural changes.

## 1. Overview

The plugin drives one or more OpenAMS units over the Klipper MCU protocol. An
OAMS unit can be an AMS 1 fitted with the OpenAMS STM32F072 mainboard,
a stock AMS behind a Cortex Bridge (a four-bay AMS 1 or AMS 2 Pro, or a
single-bay AMS HT), a klipper-mmu device, or a single-bay inline follower. Filament is fed through a shared **FPS** (Filament
Buffer Pressure Sensor) into one toolhead extruder.
The plugin decides *what* to load/unload, handles filament runout with
automatic spool reload, and exposes G-code commands and webhooks.

Design goal: keep all decision logic in **pure, Klipper-free modules** that are
exhaustively unit-testable, and confine Klipper/MCU/reactor side effects to thin
adapters. There is no supported "managerless" mode — `[oams_manager]` is the
single coordinator and validator.

## 2. Module map

| File | Purity | Responsibility |
|------|--------|----------------|
| `oams_state.py`    | **pure** | Immutable per-lane state machine (the *store*): load/unload/calibrate lifecycle, runout sub-machine, op generations, deadlines. `reduce(system, action, world, now) -> (system, [effects])`. |
| `ams_family.py` | **pure** | Versioned AMS1/AMS HT/AMS2 capability profiles, aliases, firmware auto-detection, and explicit-family mismatch reporting. |
| `oams_ext.py` | hooks | Optional local extension hooks for the driver, the family registry, the tag reader and the manager; the defaults do nothing. |
| `oams_topology.py` | **pure** | Immutable config model: FPS lanes ↔ OAMS units ↔ filament groups, with all cross-section validation and the runtime group-edit operations. |
| `oams_runtime.py`  | reactor  | The only place that executes effects: arms deadline timers, settles G-code waiters, runs the (legacy) stall watchdog. Holds the live `SystemState`. |
| `oams.py`          | driver   | One per `[oams]`: sends firmware commands, mirrors telemetry, negotiates the protocol, turns firmware replies into `OpCompleted` actions. |
| `follower.py`      | driver   | One per `[follower]`: an inline stepper as a SINGLE-BAY unit, closed-loop controlled by custom Klipper MCU firmware (see `FOLLOWER_PROTOCOL.md`). Hosts the FPS-forwarding and motion-queue feed-forward streams; PRE switch = "ready", POST switch = "loaded". |
| `oams_manager.py`  | adapter  | Builds + validates the topology, owns the runtime, registers `OAMSM_*` commands and webhooks, drives the monitor tick, hosts runtime group editing. |
| `fps.py`           | driver   | One feed-path pressure sensor; reads the FPS ADC (mainline or Kalico API, auto-detected). |
| `helix_backend.py` | adapter  | Optional. Binds HELIX material operations (`openams.preflight`, `material.load`, `material.unload`, cancel) to the reducer and runtime without G-code or a blocking wait. Imported lazily by `OAMSManager.make_helix_backend()` (section 11). |
| `filament_group.py`| config   | Thin holder: parses its own bay list only. All cross-validation is the manager's. |
| `hdc1080.py`, `external_irq.py` | driver | Ancillary sensors. |

### klipper-mmu: the MMU add-on in this repository

`klipper_mmu/` is the second part of OpenAMS. It is not a fork of Klipper and
not a separate project: it is a Klipper MCU add-on plus a host module, shipped
in this repository, documented in [docs/README.md](docs/README.md) and
installed by the same `install.sh`. It is GPL-3.0-or-later because it links
GPL Klipper code; everything above stays MIT.

It has three layers, and only the third touches this document's code:

| Layer | Where | Role |
|------|-------|------|
| MCU add-on | `klipper_mmu/mcu/` | Compiled into the MMU's own firmware through `src/Kconfig` and `src/Makefile`. Runs load, follow, unload and their safety checks locally, from its own sensors, and keeps motion safe when the host goes quiet (a motion lease expires on its own; a fault latches until reconciled). |
| Host module | `klipper_mmu/host/` | A Klipper extra installed as `klippy/extras/klipper_mmu`. It owns the wire protocol, the Klipper command surface and the OpenAMS adapter that projects an MMU onto an OpenAMS unit. |
| Binding | `oams_manager.py` | Discovers every `[klipper_mmu ...]` object with `lookup_objects(module="klipper_mmu")` and binds it into the unit topology. |

A bound klipper-mmu unit is an ordinary OpenAMS unit from there on: same
status object, same `OAMSM_*` commands, same UIs, same toolchange macros. The
difference is only where the motion happens — on the MMU's own MCU, running
klipper-mmu, rather than inside an AMS unit — and the contract that describes the wire between
them is C5 (`docs/contracts/C5_klipper_mmu_wire.md`), with the device-side
detail in [`klipper_mmu/docs/architecture.md`](klipper_mmu/docs/architecture.md).

## 3. The pure-core pattern

`oams_state` and `oams_topology` import nothing from Klipper and do no I/O. They
take plain data in and return plain data (or new immutable state) out. This
makes the orchestration rules testable without a printer (`test_oams_state.py`,
`test_oams_topology.py` run under plain `unittest`), and lets the runtime-edit
API reuse the *exact same* validation the config loader uses.

The adapters (`oams_runtime`, `oams_manager`, `oams.py`) translate between
Klipper objects and the pure cores, and perform the side effects the cores
request as data (effects like `StartLoad`, `ArmDeadline`, `Settle`).

## 4. Command lifecycle

A blocking G-code command (e.g. `OAMSM_LOAD_FILAMENT`):

1. `oams_manager.cmd_LOAD_FILAMENT` → `runtime.request(fps, Load(...))`.
2. `Runtime.request` registers a `ReactorCompletion` and dispatches the action.
3. `reduce()` returns new state + effects; the runtime executes them — here
   `StartLoad` (driver sends the firmware command) and `ArmDeadline`.
4. The G-code handler blocks on `completion.wait()`. G-code is sequential, so it
   *must* block until the firmware op completes — this is inherently host-side.
5. The firmware later sends `oams_action_status(2)`. The driver marshals it onto
   the reactor thread and dispatches `OpCompleted`. `reduce()` produces a
   `Settle` effect; the runtime completes the `ReactorCompletion`; `wait()`
   returns and the handler responds.

All dispatch happens on the reactor thread (serial-thread replies are marshalled
via `register_async_callback`), so the store needs no locks.

## 5. Firmware protocol contract

Two firmware protocols exist: the OAMS unit protocol below (C1), and the
inline-follower protocol, which is fully specified in `FOLLOWER_PROTOCOL.md`
(same op-code enum values, gen echo, one-terminal-status and firmware-owned
liveness — mandatory from its v1, integer-only wire units).

The firmware is the single runtime source of truth for the protocol and
publishes it into the Klipper data dictionary; the host reads it at connect via
`mcu.get_constants()`. Everything is optional with built-in fallbacks, so the
plugin runs against old firmware that publishes nothing ("legacy mode").

Published integer constants (namespaced `OAMS_*`):
- `OAMS_PROTOCOL_VERSION` — contract version; bumped on any protocol change.
- action enum `OAMS_STATUS_*`: LOADING=0, UNLOADING=1, FORWARD_FOLLOWING=2,
  REVERSE_FOLLOWING=3, COASTING=4, STOPPED=5, CALIBRATING=6, ERROR=7.
- code enum `OAMS_OP_CODE_*`: SUCCESS=0, ERROR_UNSPECIFIED=1, ERROR_BUSY=2,
  SPOOL_ALREADY_IN_BAY=3, NO_SPOOL_IN_BAY=4, ERROR_KLIPPER_CALL=5,
  CANCEL_LOAD_SPOOL=6, TIMEOUT=7.
- follower dir: `OAMS_FOLLOWER_REVERSE`=0, `OAMS_FOLLOWER_FORWARD`=1.

How the host treats them (`oams.py:_resolve_protocol`):
- The **action enum** is read dynamically (it is interpreted only in the
  driver).
- The **op-code / follower-direction enums** flow into the pure reducer, which
  cannot read the dictionary, so they are **validated** against the host's
  compiled-in constants and a mismatch is logged loudly (the version gate is the
  real guard).

Semantic guarantees the host relies on:
- **Exactly one completion-class status per op**, in op order. Completion-class
  = action ∈ {LOADING, UNLOADING, CALIBRATING, ERROR} or code == 5.
- `ERROR_BUSY(2)` is a *rejection* carrying the requested action (an op was
  already in flight). The host keeps a single in-flight op per lane.
- `ERROR_KLIPPER_CALL(5)` only co-occurs with CALIBRATING; `COASTING(4)` is never
  emitted; follower statuses (2,3,5) are non-terminal replies to the follower
  command and are dropped.
- After a load SUCCESS the firmware auto-starts the forward follower.
- A PTFE-calibration completion returns the measured length (encoder clicks) in
  `value`; `ptfe_length` is in encoder clicks, `0` = uncalibrated.

Version-gated host behaviour:
- **v1+**: read/validate the published enums (above).
- **v2+** (generation matching): the firmware exposes `oams_cmd_*2 … gen=%c` and
  replies `oams_action_status2 … gen=%c`. The driver feature-detects the `*2`
  commands, sends the op generation on the wire, and matches each completion by
  the echoed gen — eliminating the FIFO heuristic used as the legacy fallback.
  The wire field is one byte: the reducer's `op_gen` is wider (see v4 below), so
  the runtime hands a one-byte driver the low byte and maps its echo back to the
  logical value (section 11.1). The reducer rejects any completion whose gen ≠
  the in-flight op's.
- **v4** (full-width generation, dormant): `oams_cmd_load_spool4 spool=%c
  gen=%u`, `oams_cmd_unload_spool4 gen=%u`, `oams_cmd_calibrate_hub_hes4
  spool=%c gen=%u`, `oams_cmd_calibrate_ptfe_length4 spool=%c gen=%u`,
  `oams_cmd_cancel4 action=%c gen=%u`, and the reply `oams_action_status4
  action=%c code=%c value=%u gen=%u`. No released firmware speaks it, and the
  connect guard (`MAX_PROTOCOL_VERSION`, contract C1) still refuses a unit
  that publishes a version above 3, so the family cannot be selected until
  both a firmware and a contract revision exist. Section 11.1 states the
  selection rule.
- **v3+** (firmware owns liveness): the firmware runs its own no-progress
  watchdog, stops the motors on a stall, and completes the op with
  `TIMEOUT(7)`. The host then (a) treats `TIMEOUT` as an ordinary op failure
  *without* sending a cancel (hardware already stopped), (b) downgrades its
  authoritative 120 s op deadline to a coarse disconnect backstop
  (`OAMS_DISCONNECT_BACKSTOP`, 300 s, only to release a blocked G-code wait if
  the MCU dies), and (c) disables its own redundant stall watchdog. This is
  gated by `Runtime.set_firmware_liveness`, set true only when **every** unit
  reports protocol ≥ 3 (conservative: any legacy/unconnected unit keeps the full
  host deadline).

Back-compat matrix (verified by unit tests):
- new host / old firmware: `*2` absent → legacy commands + FIFO; no version → legacy
  mode + built-in enum defaults; host keeps its 120 s deadline + stall watchdog.
- new host / v3 firmware: `*2` + gen matching; firmware-owned liveness; `TIMEOUT`
  handled as a failure.
- new host / partial or absent v4 set: the complete `*2` family, then legacy;
  the wire bytes are exactly those of the host before v4 existed.

### AMS family, telemetry, and control ownership

Each `[oams]` has `device_kind: auto|ams1|ams_ht|ams2`. Before connect,
`auto` uses the backward-compatible AMS1 shape; at connect it resolves
`BOARD_NAME`, `AMS_BAYS`, and `AMS_HT_DRYER` from the MCU dictionary and
the manager rebuilds topology with the negotiated bay count. Explicit family
selection owns topology: contradictory firmware facts produce
`capability_warnings` rather than silently remapping configured bays.

`openams/status` publishes, per unit, `family`, `bay_count`,
`capabilities`, `bays`, `supported_actions` and `telemetry`. `openams/topology` publishes the same family/capability shape
alongside lane and group mappings. This is the contract used by Mainsail:
controls are capability-driven and absent firmware features stay hidden.

The host polls the unit's fire-and-report status channels (board, domain,
dryer) round-robin, each about once per 1.3 seconds. This is observational
only. The unit owns real-time load/unload motion, the follower, and its own
protection.

Dryer start/stop and recoverable-fault clear are feature-detected query
commands. Dryer targets are clamped to firmware-published bounds, and nonzero
firmware result codes become G-code errors.

## 6. Configuration & validation

`[oams_manager]` validates the whole configuration in `oams_topology`, which
enforces: ≥1 FPS lane with unique names (F1); unique OAMS names and `oams_idx`
(O1) each resolving to a known lane — explicit `fps:` or the sole lane (O2);
every group bay references a defined OAMS and a valid bay for its hardware
family (G1); a group's bays share one FPS lane (G2); a bay belongs to at most
one group (G3). Errors are
user-facing and surfaced as Klipper config errors.

**Load-order independence.** Klipper does not guarantee the order in which
config sections are instantiated. `filament_group` therefore does *not* look up
OAMS objects at load time — it only parses its own bay list. The manager
force-loads every `fps` / `oams` / `filament_group` section up front
(`get_prefix_sections` + idempotent `load_object`) and validates centrally, so a
valid config never fails because of section ordering.

## 7. Runtime group editing & persistence

For the management UI, groups can be created and bays reassigned at runtime:
`OAMSM_CREATE_GROUP`, `OAMSM_DELETE_GROUP`, `OAMSM_ASSIGN_BAY`,
`OAMSM_UNASSIGN_BAY`, plus the read-only `openams/topology` webhook. Each edit:

1. goes through the same pure `oams_topology` validators (so a runtime edit can
   never produce a config the loader would reject);
2. **persists first**: the affected `[filament_group …]` sections are rewritten
   in place in the OpenAMS config file (`oams_config_io.apply_group_edits`,
   atomic temp-file + `os.replace`), preserving every other section, option and
   comment;
3. then swaps the live model and rebuilds the derived runtime maps
   (`_rebuild_derived`).

Persisting before swapping means a file-write failure aborts the edit with the
model untouched — saved and live state never diverge. The change takes effect
immediately *and* survives a restart with **no `SAVE_CONFIG`**: Klipper's
`SAVE_CONFIG` can only rewrite the main `printer.cfg`, never an included subfile
like `oams.cfg`, so the plugin edits its own file directly. The target file is
`[oams_manager] openams_config_path` (default: `oams.cfg` next to the main
printer config). Reassigning a bay **moves** it (never silently shares);
`OAMSM_DELETE_GROUP` removes the section outright. Edits are refused on a lane
that is mid-op, handling a runout, or printing from the affected group.

The same in-place writeback (via `oams_config_io.set_option` and the manager's
`persist_config_option`) is used for **calibration** results
(`OAMS_CALIBRATE_PTFE_LENGTH`, `OAMS_CALIBRATE_HUB_HES`), which write
`ptfe_length` / `hub_hes_on` back onto the `[oams …]` section — again because
`SAVE_CONFIG` cannot reach them in `oams.cfg`.

## 8. Tunables

Config-exposed (all defaulted, so none are required in the config):
- `[oams_manager] reload_before_toolhead_distance` — extra margin before the
  runout reload point.
- `[oams_manager] monitor_interval` — runout/health monitor period (default 1 s).
- `[oams_manager] openams_config_path` — file that runtime writeback (group
  edits, calibration results) is written to (default: `oams.cfg` next to the
  main printer config).
- `[oams] device_kind` — `auto` (default), `ams1`, `ams_ht`, or `ams2`.
- `[oams_manager] toolhead_load_macro` / `toolhead_unload_macro` — bare names of
  printer-specific, extruder-only macros used by `OAMSM_LOAD_TO_TOOLHEAD` and
  `OAMSM_UNLOAD_FROM_TOOLHEAD` (defaults: `LOAD_FILAMENT` / `UNLOAD_FILAMENT`).
  The unload macro must heat and eject the filament past the extruder gears
  (about 100 mm); `_OAMS_TOOLHEAD_EJECT` in `oams_macros.cfg` does. The
  manager waits for their queued motion. Reverse-follower cleanup is
  fail-safe if the unload callback raises.
- `[fps] extruder`, `[oams] fps:` — topology wiring.

Intentionally internal (safety-relevant timings, kept out of config to avoid
foot-guns; change in `oams_state.py`/`oams_runtime.py` with care): the op
deadline / disconnect backstop, `PAUSE_DISTANCE` (runout geometry; note
`LaneWorld.path_len` is always MILLIMETRES — the OAMS driver converts its
encoder clicks via `FILAMENT_PATH_LENGTH_FACTOR` in `oams.py`, the follower
uses its `path_length` directly), and the host stall-detection thresholds
(only used against
pre-v3 firmware).

## 9. Validation

**Unit tests** (no Klipper, no hardware, no third-party deps):
```
python3 -m unittest discover -s test
```
CI runs these on every push/PR (`.github/workflows/tests.yml`). Coverage: the
pure reducer (`test_oams_state`), the runtime effect executor with a fake
reactor (`test_oams_runtime`), the driver's protocol negotiation with a stubbed
`mcu` (`test_oams_driver`), the pure topology (`test_oams_topology`), and the
manager's validation + group editing with a fake printer (`test_oams_manager`).

**Hardware bring-up — read-only.** `OAMSM_SELFTEST` reports, without moving
filament: each FPS reading and extruder binding; every OAMS unit's connection,
negotiated protocol version, gen-matching and liveness flags, and per-bay
ready/loaded sensors; the validated topology; and live lane state — ending in
`PASS` or `WARN -> …`. Run it first on any new setup or after a firmware update.

For AMS HT, also verify the capability and safety surfaces:

1. Confirm `openams/status` reports `family=ams_ht`, `bay_count=1`, its
   telemetry channels, and only firmware-supported actions.
2. Start/stop the dryer inside the published target bounds and confirm rejected
   commands surface an error.

**Hardware integration — manual, destructive (moves filament).** With a spool in
a ready bay, verify each capability surfaces exactly one completion:

1. `OAMSM_SELFTEST` → `PASS`; confirm the target bay shows `ready`.
2. `OAMSM_LOAD_FILAMENT GROUP=<g>` → "loaded"; `current_spool` set; forward
   follower running.
3. `OAMSM_FOLLOWER ENABLE=1 DIRECTION=1` / `ENABLE=0` → follower starts/stops;
   `following` reflects it in `openams/status`.
4. `OAMSM_UNLOAD_FILAMENT` → "unloaded"; bay no longer loaded; follower stopped.
5. `OAMS_CALIBRATE_PTFE_LENGTH SPOOL=<n>` → returns a length in clicks; written
   straight into `oams.cfg` (no `SAVE_CONFIG`).
6. Runout (v3 firmware): start a print from a group with a spare ready bay; cut
   the active filament. Expect PAUSING → COASTING → auto-reload of the spare, or
   a PAUSE if no spare. The lane never wedges.
7. Stall (v3 firmware): block a bay so the encoder cannot advance during a load.
   Expect the firmware to stop and complete with `TIMEOUT`, surfaced as a load
   failure — no host-side cancel, no double completion.
8. Runtime edit: `OAMSM_CREATE_GROUP GROUP=test`,
   `OAMSM_ASSIGN_BAY GROUP=test OAMS=<o> BAY=<b>`, check `openams/topology`,
   confirm `oams.cfg` was updated in place (no `SAVE_CONFIG` needed) and the
   edit survives a restart.

## 10. HelixScreen UI API v1

`oams_manager.get_status()` and the `openams/status` webhook additionally
publish the versioned contract in `docs/UI_API.md`: `api_version`, `schema`,
`commands` (only the advertised operations whose G-code command is actually
registered), `lanes` (the versioned array), `units`/`groups` (built from a
manager-assigned, stable global slot id per bay — `_slot_entries()`), all
additive to the pre-existing fields. `get_status()` also publishes
`lanes_by_fps`: the same per-lane data (plus the pre-existing, non-versioned
per-lane fields) keyed by FPS name instead of array position — additive and
outside the v1 contract; `oams_macros.cfg` reads its own lane's just-loaded
slot off it (`lanes_by_fps.fps.current_slot`). `_ui_lanes()` builds the array
once; both `get_status()` and the webhook's `api` key call it, so the shape
is defined in one place. `OAMSM_LOAD_FILAMENT` takes an optional `SLOT=`
(routed as `oams_state.LoadBay` rather than group-wide `Load`, bypassing bay
selection for that one bay but still resolving its group for runout
protection); `OAMSM_VALIDATE_LOAD` checks a `GROUP=`/`SLOT=` pair against the
same rules before any motion.

## 11. HELIX portable-machine-program binding

`helix_backend.HelixOpenAMSBackend` is the boundary between HELIX semantic
operations and the same reducer described above. It invokes no G-code and does
not duplicate the load/unload state machine:

1. `openams.preflight` resolves the requested group, rejects busy or
   conflicting lanes, and records the target and current lanes under the HELIX
   program generation.
2. `material.unload` and `material.load` call `Runtime.request_async()`. The
   reducer still chooses and executes the exact `StartUnload`/`StartLoad`,
   deadline, follower and settlement effects.
3. Settlement reaches the HELIX operation callback from a fresh reactor
   callback. No G-code mutex and no blocking `ReactorCompletion.wait()` is
   involved, so the Klippy reactor is never held.
4. Cancellation recalls an in-flight load or unload through the existing
   reducer `Cancel`. Every cancel effect (`CancelLoad`, `CancelUnload`) names
   the generation it cancels; with protocol v4 the wire cancel also names the
   action, so a delayed or cross-action cancel cannot touch a newer physical
   operation.
5. The remaining track, cutter, checkpoint, job and resource operations are
   delegated to the machine-level backend.

The host adapter opts into request context explicitly
(`helix_request_context`): every selection and cancellation is keyed by the
immutable HELIX program generation and is cleared when the lease is released or
a safe pause ends the program. An injected legacy backend keeps its original
call signature.

`OAMSManager.make_helix_backend(machine_backend)` creates the backend lazily.
Nothing else in OpenAMS imports HELIX, so importing any OpenAMS module under
stock Klipper has no HELIX dependency (a test starts a fresh interpreter to
hold this).

`Runtime.request_async(fps, action, callback)` is the only runtime addition the
binding needs. Its waiter answers the same `test`/`complete`/`wait` calls as a
reactor completion, so the MMU terminal paths treat it like a G-code waiter.

### 11.1 Protocol-v4 generation boundary

The reducer and the HELIX adapter use one nonzero 32-bit logical operation
generation throughout. Zero is the firmware's sentinel for "no active
operation", so `UINT32_MAX` wraps to one and the reducer never issues zero.

**Selection is atomic.** The driver reads the dictionary once, at connect, and
picks one wire family for every operation:

- v4 is chosen only when the unit publishes `OAMS_PROTOCOL_VERSION` 4 or
  higher and the dictionary holds every v4 load, unload, calibration and cancel
  command; it then registers `oams_action_status4`;
- if any of them is missing the driver discards all of the v4 handles and uses
  the complete `*2` family, and if that is absent too, the legacy commands.
  Families are never mixed, and no partial v4 handle remains to be used by
  mistake;
- the v4 family has no atomic-load variant, so on v4 the PTFE length is written
  first and `oams_cmd_load_spool4` follows, as in the plain `*2` family.

**Who narrows the value.** The reducer's generation is wider than the `*2`
wire byte, the legacy FIFO and the one-byte wire of the follower and the
klipper-mmu operation bridge. `Runtime` therefore decides per operation, from
the driver that will carry it. A driver that declares
`supports_native_operations` (v4 selected) receives the full generation. Every
other driver receives the low byte, which is exactly what the reducer's old
one-byte counter produced, and the runtime remembers the pair. The echo, the
MMU terminal acceptance and the reload and retired-terminal hooks map that
byte back to the logical value before the reducer compares it; an unrelated
byte passes through unchanged and is rejected, as before. For the first 255
operations on a lane the two values are equal, so nothing differs; past them
the `*2` and legacy wire bytes are identical to the ones sent before the
widening.

**v4 completions.** The v4 status carries the whole generation and is matched
exactly. Every status still reaches the reducer, for an auditable transition.
The driver's own mirrors (`current_spool`, the blocking standalone waiter)
move only when action and generation match the operation in flight, so a stale
completion is observable but physically inert at the host boundary.

**Cancel.** `CancelLoad` and `CancelUnload` carry the lane's generation. A v4
driver sends `oams_cmd_cancel4 action=%c gen=%u`. An older driver keeps its
single `oams_cmd_load_spool_cancel` for loads and sends nothing for an unload:
the follower stop that the reducer also emits still applies.
