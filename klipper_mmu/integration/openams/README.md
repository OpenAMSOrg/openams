# OpenAMS integration

Klipper-MMU binds to hooks that OpenAMS itself provides. Nothing here patches,
vendors or installs OpenAMS files: the Klipper-MMU host code lives in
`klipper_mmu/host/` and the OpenAMS plugin it binds to lives in `src/`, both in
this one repository, so there is no external revision to track or install.

## What OpenAMS provides

`src/oams_manager.py` discovers every `[klipper_mmu NAME]` section whose
observer returns a unit from `get_openams_unit()`, and `src/oams_runtime.py`
provides the hook surface `klipper_mmu/host/openams_managed_unit.py` binds:

| hook | purpose |
| --- | --- |
| `dispatch(action)` | every OpenAMS state action the unit applies |
| `accept_terminal(fps, code, value, gen)` | an exact MMU terminal, after its G-code waiter settles |
| `accept_reload_terminal(fps, code, value, gen, bay)` | the same for a runout auto-reload, which has no waiter |
| `accept_retired_terminal(fps, unit, gen, code, value, disarmed_proof)` | one old operation settled after a fresh physical STOP proof |
| `accept_verified_follower(fps, unit, following)` | a verified physical follower observation |
| `accept_firmware_fault(fps, physical_lane, code, identity, from_command=False)` | a retained `FAULT_ENTERED`; `from_command=True` runs PAUSE through `run_script_from_command`, because the caller already holds the G-code mutex |
| `accept_firmware_fault_cleared(physical_lane, identity)` | removes a latched fault after a verified clear |

`bind_runtime` raises `ValueError` naming any hook the runtime lacks. Only the
last one is optional: a runtime without it still binds, and a lane keeps its
latch until Klipper restarts (see
[path reconciliation](../../docs/path-reconciliation.md#from-a-stopped-fault)).
The OpenAMS side of this contract is pinned by
`test/test_openams_runtime_hook_contract.py`, which asserts every name, every
parameter order and the `from_command` keyword.

## Configuration

A `[klipper_mmu NAME]` unit joins OpenAMS only when all five topology fields
are present. They are never inferred from the profile:

```ini
[klipper_mmu box]
openams_idx: 7
openams_fps: fps0
openams_family: boxturtle
openams_bays: 4
openams_path_length_mm: 842.5

[fps fps0]
source_oams: klipper_mmu box
extruder: extruder
```

With all five, `get_openams_unit()` returns a unit and OpenAMS adds `box` to
its own topology, index map and lane, sharing the same groups, slots and UI
status as an `[oams]` unit. With none of them the observer returns `None` and
OpenAMS omits the section entirely (it is still loaded for its own commands).
A name or index that collides with an existing unit is a configuration error.

`[fps NAME] source_oams: klipper_mmu box` makes that lane's FPS read the MMU
buffer. The mirror accepts only a current, valid `buffer_position` capture
(0-1000 thousandths of a buffer stroke, reported as 0-1) and claims no second
ADC pin: `get_value()` fails closed and the status query reports `None` while
the feedback is stale.
Firmware alone uses Diff feedback for real-time control. The status view keeps
prep (`bay_present`), optional LOAD (`load_present`, null when unknown) and
hub occupancy separate; see [unpreload](../../host/unpreload.md) for
lane-local preload.

## What read-only means now

A unit is read-only until `openams_motion: True`, `diagnostic_startup: True`,
the lease bounds in [the connection contract](../../host/connection.md) and
firmware ACTUATION=1 are all present and the firmware dictionary matches
exactly: `KLIPPER_MMU_DEVICE_ID` to `openams_family` (`boxturtle` = 1,
`nightowl` = 2, `custom` = 3) and `KLIPPER_MMU_LANE_COUNT` to `openams_bays`.
A mismatch blocks HOST session preparation; the device ID does not qualify
hardware.

Read-only is per unit, not a property of the machine:

- A read-only unit refuses the motion commands that target it - through its
  lane, its group or one of its slots - with `OpenAMS motion unavailable for
  <unit>`. Every other unit keeps loading, unloading and following.
- It publishes a real physical snapshot, so it never holds the manager
  not-ready: the startup resync waits only for units whose motion can still
  come back.
- Its FPS feedback, bays, groups and status are live the whole time.
- A command that addresses every unit at once (`OAMSM_CLEAR_ERRORS`) still
  refuses while any unit is read-only, because it addresses that one too.
- A managed unit that is only *temporarily* motion-unavailable (a lost HOST
  session, a silent MCU) keeps the old conservative behavior: it holds the
  resync until its motion returns.

## How the host binds

After a fresh HOST session and a confirmed lease, the unit binds that session
and services retained events on Klipper's reactor under the shared MCU query
guard. A terminal must match its exact `Requests` identity; it is ACKed only
after `accept_terminal` (or `accept_reload_terminal` for runout auto-reload)
returns true. A failed LOAD/UNLOAD settles only after a verified follower
STOP. Anything else stays retained. Binding grants no motion: each command
checks the live lease and the physical view. `clear_errors` and the LED
methods raise.

After a disconnect, `KLIPPER_MMU_RECOVER_WAITER MMU=<name>` settles one retired
LOAD, UNLOAD or CALIBRATE waiter through `accept_retired_terminal`, only when a
fresh same-controller capture proves the follower stopped and the exact
terminal is still the queue head; repeat it to retry an uncertain ACK. A
retained `FAULT_ENTERED` goes through `accept_firmware_fault`, which settles
the waiter with failure and requires `PAUSE`; active or uncertain followers
block it, and the generic `ClearErrors` keeps the latch. Neither restores
motion.

`KLIPPER_MMU_RECONCILE_PATH` is the verified clear: after it clears a stopped
firmware fault and a fresh capture proves the firmware left FAULT, it calls
`accept_firmware_fault_cleared(physical_lane, identity)` with the exact pair
`accept_firmware_fault` was given.

The toolhead unload gate is OpenAMS' side of the same contract:
`OAMSM_UNLOAD_FROM_TOOLHEAD` runs the extruder macro only after the reverse
follower has been confirmed on the lane.

## Checks

```sh
make check-openams                      # against this repository
OPENAMS_SOURCE=/path/to/other make check-openams
```

`OPENAMS_SOURCE` defaults to the repository root, which is where `src/` lives,
so the binding suite runs with no environment variable. Set it only to run the
same suite against a different OpenAMS checkout. It runs the portable suite
plus every module that imports the real OpenAMS `src/`: runtime hook and STOP
gate, fault hook, runtime contract, event cycle, fault composition,
operation-bridge runtime, retired runtime and integration, terminal consumer
and failed-terminal integration, manager discovery, FPS source, status and the
reverse-follow unload gate. The checkout is only read. The repository's root
`make test` runs it as `test-binding`. The saved-image H723 join can also run
the actual OpenAMS FPS, manager and runtime when `OPENAMS_SOURCE` is set.
