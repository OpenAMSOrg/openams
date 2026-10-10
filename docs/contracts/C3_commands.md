# C3 — UIs to host: commands

**Version: v1 (2026-09-28).** All G-code commands are implemented and
tested.

Status: v1, 2026-09-28. Companion: `ARCHITECTURE.md` §5 C3, §6.2. Decision
numbers (D2, D3, D5, D7) label the design decisions these contracts follow.

Every command below is marked **EXISTS** (cited to the file/line that
registers and implements it) or **FUTURE** (not in v1, with the reason).
Preconditions and error strings are quoted verbatim from the source.
"Idempotent" means calling it again in the same state produces the same
observable effect (no double motion, no exception) — noted per command.

Manager commands (`OAMSM_*`) are plain G-code commands
(`gcode.register_command`, `oams_manager.py:449-476`). Per-unit commands
(`OAMS_*`) are **mux commands keyed on `OAMS=<oams_idx>`**
(`gcode.register_mux_command(name, "OAMS", oams_id, ...)`, `oams.py:1565-1614`)
— every `OAMS_*` command below takes an implicit required `OAMS=<idx>`
parameter not repeated in each row. Two are registered by the manager rather
than by `src/oams.py`, because a klipper-mmu unit owns what they do:
`OAMS_CLEAR_FAULT`, only for the units that recover differently
(`_register_fault_clear_routes`), and the four `OAMS_PRELOAD` /
`OAMS_UNPRELOAD` / `OAMS_EJECT` / `OAMS_HOME` of §2, which **every** unit
gets, so a unit that cannot perform one refuses with the reason instead of
leaving the command unknown.

## 1. Manager commands (`src/oams_manager.py`) — EXISTS

### `OAMSM_LOAD_FILAMENT GROUP=<name>`
`cmd_LOAD_FILAMENT`, `oams_manager.py:729-743`.
- **Preconditions:** hardware snapshot must be ready (else `"OpenAMS hardware
  snapshot unavailable for %s"`); `GROUP` must name an existing group
  (`"Group %s does not exist"`) with at least one assigned bay (`"Group %s has
  no bays assigned"`); the group's lane must be `UNLOADED` and its hub sensor
  clear (`_load_guard`, `oams_state.py:467-480`: `"lane busy (%s)"`, `"follower
  state unknown on unit(s) %s; stop and verify them before loading"`,
  `"filament still detected in a hub on this lane; unload it first"`); at
  least one group member must be `ready` (F1S sensor) or the request is
  rejected with `"no ready spool in group %s"` (`oams_state.py:515-516`).
- **Effect:** starts a firmware load op on the first ready member in declared
  order (`_group_candidates`, `oams_state.py:459-464, 507-509`); lane
  `op -> LOADING`; on success `op -> LOADED`, `following=true`. Blocks
  (`Runtime.request(...).wait()`) until the firmware terminal status or the
  op deadline.
- **Errors:** raises `gcmd.error` with the reducer's message unless the
  result is `ok` or `OAMS_OP_CODE_CANCEL` (`oams_manager.py:738-743`).
- **Idempotent:** no — calling it again while `LOADING`/`LOADED` is rejected
  by `_load_guard`, not silently repeated.
- **D7 (calibration precondition): EXISTS (H10, 2026-09-28).** `_group_candidates` (`src/oams_state.py:585-639`) skips uncalibrated bays and `_uncalibrated_msg` raises an error. The *runout* path refuses to auto-reload an uncalibrated bay (`oams_state.py:744-751`) — the same guard covers explicit loads.
- **Firmware fault latched (H13).** A
  lane-scoped request (`Load`, `LoadBay`) issued while a firmware fault is
  latched replies `firmware fault (%s) latched; OAMS_CLEAR_FAULT first`
  (where `%s` is the fault name from `_controller_fault_name`). Previously
  the reducer returned an unsettled completion with no refusal text, causing
  callers to block indefinitely (C2 §5.1). Fixed (H13).
- **Bay positioning (EXISTS, H16, 2026-09-28).** A load targeting a bay whose `positioning` is `true` is refused. Error text: `"bay %d is positioning; wait for the firmware to complete insertion before loading"`. `src/oams.py:609-645`. The firmware also replies `Busy` for a `request_load` issued during `Positioning`; the host guard is the user-facing layer.
- **Motion unavailable (EXISTS, H15/H15b, 2026-09-28).** A load on a unit with `normal_motion=False` in capabilities (`AMS_CAP_F1S_NORMAL_MOTION=0`) is refused. Error text: `"This firmware does not support normal filament motion"` (`src/ams_family.py` `motion_block_reason`).

- **Host arrival not possible (C1 §7.2).** A load (and `OAMSM_VALIDATE_LOAD`) on a unit reporting `AMS_CAP_HOST_ARRIVAL_CONFIRM=1` whose lane has no toolhead-side arrival signal (`[fps]` reads `source_oams:`) is refused at once: `"This unit needs the host to confirm toolhead arrival, but lane %s has no arrival signal; give [fps %s] a 'pin' that reads the toolhead side"`. It is never left to time out.

### `OAMSM_UNLOAD_FILAMENT [FPS=<lane>]`
`cmd_UNLOAD_FILAMENT`, `oams_manager.py:683-725`.
- **Preconditions:** motion must be available on every unit, else stops
  followers first, then `"OpenAMS motion unavailable for %s"`; hardware
  snapshot ready, else same as above; `FPS=` required when more than one lane
  exists and more than one is `LOADED` (`"Multiple FPS have a spool loaded;
  specify FPS=<name>"`), otherwise the sole/ambiguous lane is inferred.
- **Effect:** stops any lane not `LOADED` with an informational reply
  (`"No spool is loaded on lane %s (follower stopped)"`), else starts a
  firmware unload; lane `op -> UNLOADING`, on success `op -> UNLOADED`.
- **Errors:** raises on unload failure (`result.message`, e.g. `"Spool
  unloading failed (%s)"` / `"timed out unloading spool"`).
- **Idempotent:** calling with nothing loaded is a no-op informational reply,
  not an error (`"No spool is loaded on any lane (followers stopped); specify
  FPS=<name> to target a lane"` when ambiguous with nothing loaded anywhere).

### `OAMSM_LOAD_FILAMENT_CANCEL [FPS=<lane>]`
`cmd_LOAD_FILAMENT_CANCEL`, `oams_manager.py:671-679`.
- Cancels an in-flight load if one is running on the resolved lane
  (`prefer_op=OP_LOADING`); otherwise replies `"No load filament operation is
  in progress"` (never errors — designed to run from cleanup/error macros).
  Idempotent.

### `OAMSM_FOLLOWER ENABLE=<0|1> DIRECTION=<0|1> [FPS=<lane>]`
`cmd_FOLLOWER`, `oams_manager.py:646-667`.
- **Preconditions:** `ENABLE` requires a hardware snapshot; starting
  (`ENABLE=1`) requires an unambiguous lane (`"Multiple FPS present; specify
  FPS=<name>"`); stopping (`ENABLE=0`) with no `FPS=` stops followers on
  every lane (`"Followers stopped on all lanes"`).
- If the resolved lane has nothing loaded: `"No spool is currently loaded on
  lane %s"`. Otherwise dispatches `Follow` through the store so
  `LaneState.following/direction` stay truthful.
- Idempotent (re-enabling the same direction is a harmless re-send).

### `OAMSM_CURRENT_LOADED_GROUP`
`cmd_CURRENT_LOADED_GROUP`, `oams_manager.py:632-642`. Read-only; reports
every lane's loaded group, or `"No group is currently loaded"`.

### `OAMSM_CLEAR_ERRORS`
`cmd_CLEAR_ERRORS`, `oams_manager.py:622-628`. Stops the monitor, calls
`oam.clear_errors()` on every unit, dispatches `ClearErrors()` (full lane
resync from hardware, `oams_state.py:386-400`), restarts the monitor.
Idempotent; safe at any time.
**Read-only units are skipped, not obeyed (2026-09-29).** The command
addresses every unit, and a read-only Klipper-MMU unit's `clear_errors()`
raises, so one such unit used to refuse the whole command and block error
recovery on every AMS behind it. It is now left alone and named:
`"OAMSM_CLEAR_ERRORS: skipped read-only box; it holds no error state to
clear"`, after the units that could be cleared are. The hardware-snapshot
guard still applies to those (`"OpenAMS hardware snapshot unavailable for %s"`).

### `OAMSM_SELFTEST`
`cmd_SELFTEST`, `oams_manager.py:480-548`. Read-only diagnostic report: FPS
values, per-unit connection/protocol/sensor state, group membership, lane
state, and a `PASS`/`WARN -> ...` summary. Moves nothing. Idempotent.

### `OAMSM_CREATE_GROUP GROUP=<name>`
`cmd_CREATE_GROUP`, `oams_manager.py:930-938`. Creates an empty group;
validated by `oams_topology.with_group` (`oams_topology.py:229-235`,
`"filament_group '%s' already exists."`); persisted immediately via
`_persist_and_apply` (in-place config rewrite, no `SAVE_CONFIG`). Not
idempotent (second call errors: group already exists).

### `OAMSM_DELETE_GROUP GROUP=<name>`
`cmd_DELETE_GROUP`, `oams_manager.py:942-949`.
- **Preconditions:** group must exist (`"filament_group '%s' does not
  exist."`); refused while its lane is busy or handling a runout, or while it
  is the loaded group (`_assert_editable`, `oams_manager.py:832-853`:
  `"Cannot edit filament groups while lane %s is busy (%s)."` /
  `"...handling a runout."` / `"Cannot edit group '%s' while it is loaded on
  lane %s; unload it first."`).

### `OAMSM_ASSIGN_BAY GROUP=<name> OAMS=<unit> BAY=<index>`
`cmd_ASSIGN_BAY`, `oams_manager.py:953-980`. Moves a bay into a group,
removing it from its current group (the donor) first
(`oams_topology.with_bay`, `oams_topology.py:246-256`). Both destination and
donor groups are protected by `_assert_editable` if either is currently
loaded. `OAMS=` unknown: `"Unknown OAMS '%s'."`.

### `OAMSM_UNASSIGN_BAY GROUP=<name> OAMS=<unit> BAY=<index>`
`cmd_UNASSIGN_BAY`, `oams_manager.py:984-1000`. Removes a bay from a group
(`"<unit> Spool <n> is not in '<group>'."` if it is not a member; a spool in a
sentence is its unit's config name and its 1-based position, `oams2 Spool 3`); same editability
guard.

### `OAMSM_LOAD_TO_TOOLHEAD GROUP=<name>`
`cmd_LOAD_TO_TOOLHEAD`, `oams_manager.py:772-786`. Runs
`OAMSM_LOAD_FILAMENT` then the configured `toolhead_load_macro` (default
`LOAD_FILAMENT`) followed by `M400`. On a macro failure the AMS load is
**not** unwound (filament stays loaded, forward follower stays armed) —
error text: `"AMS load reached the toolhead, but %s failed: %s. Filament
remains loaded and forward following remains armed."`.

### `OAMSM_UNLOAD_FROM_TOOLHEAD [FPS=<lane>]`
`cmd_UNLOAD_FROM_TOOLHEAD`, `oams_manager.py:791-821`. Reverse-follows while
the configured `toolhead_unload_macro` retracts, waits up to
`FOLLOW_CONFIRM_TIMEOUT` (2.0 s) for a *fresh* reducer confirmation of the
reverse follower (`_wait_for_follower`, `oams_manager.py:751-767`,
`"Reverse follower on lane %s was not confirmed; toolhead unload was
stopped"`), then calls `OAMSM_UNLOAD_FILAMENT`. Any failure stops followers
on that lane before re-raising.

## 2. Per-unit commands (`src/oams.py`) — EXISTS, `OAMS=<idx>` implicit

### `OAMS_LOAD_SPOOL SPOOL=<0..bay_count-1>`
`cmd_OAMS_LOAD_SPOOL`, `oams.py:1779-1802`. Routed through the store as
`LoadBay` when a runtime is bound (so runout protection applies if the bay
belongs to a group); low-level, does not resolve groups. Standalone fallback
(no manager bound) blocks with its own timeout.

**Firmware-level refusals:** beyond the
host-side `_load_guard`, the firmware itself may refuse at the
`request_load` level:
- **Latched fault:** `firmware fault (%s) latched; OAMS_CLEAR_FAULT first`
  (H13).
- **Bay positioning (EXISTS, H16, 2026-09-28):** `"bay %d is positioning; wait for the firmware to complete insertion before loading"` (`src/oams.py:609-645`).
- **Motion unavailable (EXISTS, H15/H15b, 2026-09-28):** `"This firmware does not support normal filament motion"` (`src/ams_family.py` `motion_block_reason`).

### `OAMS_UNLOAD_SPOOL [SPOOL=<n>]`
`cmd_OAMS_UNLOAD_SPOOL`, `oams.py:1806-1841`. `SPOOL=` is a safety check, not
a selector: `"Refusing to unload: <unit> Spool <n> is loaded on this OAMS, not <unit> Spool <m>"`
if it does not match what is actually loaded.

**Firmware-level refusals:**
- **Latched fault:** `firmware fault (%s) latched; OAMS_CLEAR_FAULT first`
  (H13).
- **Bay positioning (EXISTS, H16, 2026-09-28):** `"bay %d is positioning; wait for the firmware to complete insertion before unloading"` (`src/oams.py:609-645`).
- **Motion unavailable (EXISTS, H15/H15b, 2026-09-28):** same as load (`src/ams_family.py` `motion_block_reason`).

### `OAMS_FOLLOWER ENABLE=<0|1> DIRECTION=<0|1>`
`cmd_OAMS_FOLLOWER`, `oams.py:1845-1864`. Goes through the store when this
unit is the lane's loaded unit; otherwise drives the hardware directly (idle
units are not lane-tracked).

### `OAMS_CALIBRATE_PTFE_LENGTH SPOOL=<bay>`
`cmd_OAMS_CALIBRATE_PTFE_LENGTH`, `oams.py:2266-2282`. Requires
`capabilities.ptfe_calibration` (`"PTFE calibration is unsupported by this
unit"`). Runs a firmware calibration op via the store's `Calibrate` action
(lane `op -> CALIBRATING`, then back to `prior_op`); persists the result to
`ptfe_length` (single-bay units) or `ptfe_length_<bay>` via
`persist_config_option` (`oams.py:2234-2245`, reusing
`oams_config_io.set_option`). Error: `"Calibration of PTFE length failed"`.
This is the D7 calibration action C2 must expose per-bay state for.

### `OAMS_CALIBRATE_HUB_HES SPOOL=<bay>`
`cmd_OAMS_CALIBRATE_HUB_HES`, `oams.py:2249-2262`. Requires
`capabilities.hub_hes_calibration` (`"Hub HES calibration is unsupported by
this unit"`); persists the calibrated threshold into `hub_hes_on`.

### `OAMS_DRYER_START TARGET=<°C> [DURATION=<s>] [HOLD_AFTER=<0|1>]`
`cmd_OAMS_DRYER_START`, `oams.py:2094-2109`. Requires
`capabilities.dryer` (`"This unit has no dryer"`, answered at once, before any
firmware command; the same text refuses `OAMS_DRYER_STOP`) and
`capabilities.heater_control` (`"This firmware does not support heater
control"`). `TARGET` clamped to
`[capabilities.dryer_target_min_c, dryer_target_max_c]` (defaults 30-80°C);
`DURATION` defaults 3600 s, range 1 s to 7 days; `HOLD_AFTER` 0 or 1. Sends
`ams_ht_dryer_start target_centic duration_s hold_after`. Rejection text:
`"%s rejected: %s (%d)"` where `%s` names one of
`accepted/busy/invalid bay/invalid parameter/faulted/unavailable`
(`_AMS_HT_RESULTS`, `oams.py:1868-1871`). **No `ROTATE=` parameter exists**
today — **EXISTS (H6, 2026-09-28):** `ROTATE=` parameter added for `spool_rotation_during_drying`-capable units (AMS 2 Pro per `ams_family.py:82`); gate in `src/oams.py:2544-2549`.

A cycle can already be running when Klipper starts (C1 §4.4: a firmware may keep
it across host sessions); it is adopted, shown in `openams_ui` `dryer`, and
`OAMS_DRYER_START` then reaches the firmware's `Busy`, reported as `"a dryer
cycle is already running (...); OAMS_DRYER_STOP first"`.

### `OAMS_DRYER_STOP`
`cmd_OAMS_DRYER_STOP`, `oams.py:2113-2116`. Same rejection-text convention.
Idempotent (stopping an already-stopped dryer is accepted by firmware or
reported per the same result codes).

### `OAMS_CLEAR_FAULT`
`cmd_OAMS_CLEAR_FAULT`, `oams.py:2120-2123`. Clears the unit's recoverable
latched fault (C1 §6.3; distinct from the lane-level `OAMSM_CLEAR_ERRORS`).

### `OAMS_PRELOAD SPOOL=<bay>` / `OAMS_UNPRELOAD SPOOL=<bay>` / `OAMS_EJECT SPOOL=<bay>` / `OAMS_HOME`
**EXISTS (2026-09-29).** Registered by the manager, once per unit, under the
same `OAMS=<idx>` mux key every other per-unit command uses
(`OAMSManager._register_mmu_op_routes`, `oams_manager.py:1142-1155`), and
routed through the unit object: `OpenAMSManagedUnit.preload/unpreload/eject/
home` (`klipper_mmu/host/openams_managed_unit.py:441-458`), each of which runs
the same path its own `KLIPPER_MMU_*` command runs (`start_preload`,
`start_unpreload`, `start_eject`, `start_home`). They exist because a
klipper-mmu unit reached these four operations only through its own commands,
so a UI speaking C2 had nothing to send (C5 §7.1, §7.7).

- **Arguments:** `SPOOL=<bay>`, the 0-based bay index, required for
  `PRELOAD`/`UNPRELOAD`/`EJECT`; `OAMS_HOME` takes none. A missing, non-numeric
  or out-of-range `SPOOL` is refused before the unit is called
  (`"OAMS_PRELOAD: SPOOL=<bay> is required"` / `"SPOOL must be a bay number"` /
  `"bay 4 is outside unit box"`).
- **Refusals:** a unit that cannot perform the operation refuses, and says
  why — `"OAMS_PRELOAD: unit oams1 is not a klipper-mmu unit"` (every AMS
  unit, and a `[follower]`), `"OAMS_HOME: unit box feeds each lane
  independently, so it has no selector to home"` (a feeder-per-lane unit,
  C2 §4.4.1), and whatever the unit itself raises — a read-only
  Klipper-MMU physical observation, a busy path, a latched fault — reported as
  `"OAMS_EJECT: <the unit's reason>"`. `OAMS=<idx>` naming no unit is
  `"OAMS_EJECT: no OpenAMS unit with index 9"`.
- **Result:** the command returns as soon as the unit has issued the request;
  it does not block for the operation's terminal event, exactly as
  `KLIPPER_MMU_PRELOAD` does. The reply is one acknowledgement line
  (`"OAMS_PRELOAD: unit box bay 2"` / `"OAMS_HOME: unit box"`), and the
  operation's outcome reaches the UIs as the unit's own state and faults
  (C2 §4.8, C5 §7.6). Cancellable with `KLIPPER_MMU_PRELOAD CANCEL=1`
  (C3 §5), the same request's cancel.
- **Idempotent:** no — a second call is a second request, and the firmware
  refuses it (`ALREADY_IN_PROGRESS`, C5 §7.4) exactly as its own command's
  would be.
- **What a UI may offer (C2 §5.6):** a klipper-mmu unit publishes
  `preload`, `unpreload` and `eject` per bay and `home` unit-wide, and only
  where the family's selector kind has a selector to home. An AMS unit
  publishes none of them, and its refusal above is the only way to learn that.

### `OAMS_RFID_SCAN BAY=<i>`
`cmd_OAMS_RFID_SCAN`, `oams.py`. A unit whose `capabilities.rfid` is false
refuses at once (`"This unit has no RFID reader"`).

**A unit that decodes its own tags (`capabilities.tray_info`, C1 §4.7.1).**
`BAY` is required. With `capabilities.tray_read` the command sends
`ams_tray_read` (refused while the bay is positioning; `busy` retried up to
five times, one second apart) and answers `Tray read started`, or `Tray read: <busy|invalid_bay|
unavailable|faulted>`; the unit may turn the spool, and the outcome arrives
later as the bay's `tag` (C2 §4.6, `format: "unit_summary"`). Without
`tray_read` the unit reads on its own and the command only reports what it
last said. `supported_actions` lists `rfid_scan` for such a unit only with
`tray_read`.

**The reader service (S4b, EXISTS — `src/oams_tags.py`).** One `TagReader`
per unit keeps `bays[i].tag` (C2 §4.6). On a unit that decodes its own tags
and takes `ams_tray_read`, it triggers a read on a `ready` rise (once the bay
stops positioning, gated on `settings.read_tag_on_insertion`), at startup for
every ready bay the unit reports as not read (gated on
`settings.read_tags_at_startup`), and on the explicit read above; a `ready`
fall clears the bay's tag unless `OAMSManager.unit_runout_active` says the
unit's lane is mid-runout (C4 §3). On an OpenAMS 1 mainboard, the separate
`[mfrc522 <name>]` object (§3) hands its own reads and removals to the same
per-unit `TagReader` instead of decoding independently; its `bays_a` /
`bays_b` options (default `0,1` / `2,3`) say which bays each of its two
readers covers, and a card is attributed to the covered bay whose `ready`
rose most recently within 30 s, else the only ready covered bay, else not
attributed at all. The same UID read on another bay of the unit moves there.

### `OAMS_PID_SET P=<f> I=<f> D=<f> [TARGET=<f>]` / `OAMS_CURRENT_PID_SET P= I= D= [TARGET=]`
`oams.py:1638-1661`. Runtime PID tuning for the hub motor / rewind current
sensor; AMS 1-era commands, gated on `capabilities.runtime_pid_tuning`. Not
part of the ordinary UI action set (no `supported_actions` entry beyond the
generic `configure_pid` for AMS 1, §2.1 of C2).

### `OAMS_PID_AUTOTUNE`
`oams.py:1665-1668`. Always errors: `"OAMS PID autotune is unsupported; use
OAMS_PID_SET and OAMS_CURRENT_PID_SET with qualified values"`. Kept only so
generic Klipper tooling that expects the command name gets an explicit
answer instead of "unknown command".

## 3. RFID reader commands (`src/mfrc522.py`) — EXISTS

### `OAMSM_RFID_READ OAMS=<index> RFID_CARD=<0|1>`
`OpenAmsRfidRegistry.cmd_RFID_READ`, `mfrc522.py:295-307`. Forces an
immediate read of one of the two shared-bus readers on unit `OAMS`; replies
with the same line `format_last_read()` builds:
`"OAMSM_RFID_READ OAMS=%d RFID_CARD=%d STATUS=%s LAST_READ_STATUS=%s
UID=%s BLOCK=%s VERSION=%s FILAMENT=%s ERROR=%s"`
(`mfrc522.py:485-499`), where `FILAMENT` is the JSON-encoded decoded Bambu
tag (or `NONE`). Errors: `"Usage: OAMSM_RFID_READ OAMS=<index>
RFID_CARD=<0|1>"`, `"No RFID reader configured for OAMS=%d RFID_CARD=%d"`.
Idempotent (read-only).

### `MFRC522_QUERY READER=<name>`
`OpenAmsMfrc522.cmd_QUERY`, `mfrc522.py:501-503` (mux on `READER`). Same
effect as `OAMSM_RFID_READ` for a single named reader endpoint.

**Note:** neither command's result is wired into `oams_manager` status
today (confirmed: no reference to `mfrc522`/`bambu_lab_tag_processor` in
`oams_manager.py` or `oams.py`). `devices[*].bays[i].tag` (C2 §3.3) requires
this wiring plus the reader→bay mapping open question (C2 §6.2).

## 4. Commands added in this design (all EXISTS as of v1)

### `OAMSM_SET_BAY_SPOOL OAMS=<unit> BAY=<index> SPOOL=<spoolman_id> [SOURCE=tag|manual]` (D2)
**RESOLVED (review, C3 1):** persisted in the unit's own
`[oams <name>]` config section as `spool_id_<bay>:` and `link_source_<bay>:`,
consistent with the existing `ptfe_length_<bay>:` convention (`oams.py:304-306`)
— using `oams_config_io.set_option` (`oams_config_io.py:216-249`), the same
primitive `persist_config_option` already wires up for calibration writeback
(`oams.py:2234-2245`). Publishes `devices[*].bays[i].spool_id` /
`.link_source` (default `SOURCE=manual` when the caller omits it — a UI's
Assign-spool picker never sends `SOURCE=tag`), and mirrors it to the
Spoolman spool's `location` (C4). **Preconditions (RESOLVED, C3 3): allowed
at any time**, including on a lane's currently-loaded bay — a change there
triggers the active-spool update through `openams_spoolman` (C4 §7.2), the
same as a load/unload would. `OAMS=`/`BAY=` must still resolve to a real bay
(mirror `OAMSM_ASSIGN_BAY`'s `"Unknown OAMS '%s'."` / bay-range check style,
`oams_manager.py:955-959`). **Move semantics (C4 13):** if `SPOOL=` already
names a spool linked to another bay, the host (or the component observing
the resulting state) clears that bay's link rather than allowing two bays to
claim one spool.

### `OAMSM_CLEAR_BAY_SPOOL OAMS=<unit> BAY=<index>` (D2)
Clears `spool_id_<bay>`/`link_source_<bay>`; mirrors `location` cleared (to
`""`, C4 §5.1) in Spoolman. **Preconditions: allowed at any time** (same
resolution as above). It also clears the bay's `filament` entry (below).

### `OAMSM_SET_BAY_FILAMENT OAMS=<unit> BAY=<index> [MATERIAL=<text>] [TEMP=<int>] [SOURCE=spool|table]` (H2)
Publishes what a bay holds of filament so the macros can heat before a load,
an unload or a toolchange: the material and the **working temperature**, from
Spoolman via `openams_spoolman` (C4 §7.4). Argument checks mirror
`OAMSM_SET_BAY_SPOOL` above (`OAMS=`/`BAY=` must resolve to a real bay,
`SOURCE` must be one of its two values, default `spool`).

- **Clearing:** no `TEMP` (or `TEMP=0`) and no `MATERIAL` clears the bay.
- **Range:** `TEMP` is 150-400 when given.
- **Always allowed**, like a spool link: it is metadata, not motion.
- **Never persisted.** The command calls no `persist_config_option` and
  writes no file: this is an operating copy Spoolman does not own and Klipper
  forgets on every restart, after which the component pushes it again.

**A spool link change clears the bay's `filament`:** `OAMSM_SET_BAY_SPOOL`
linking a *different* spool, `OAMSM_CLEAR_BAY_SPOOL`, and a spool moved away
from a bay all clear it. The values described the old spool; the component
pushes the new ones on its next pass. Relinking the *same* spool changes
nothing.

### `OAMSM_SET_TRAY_INFO OAMS=<unit> BAY=<index> [SPOOL=<spoolman_id>] | CODE=<code> MATERIAL=<text> COLOR=<RRGGBB[AA]> MIN=<C> MAX=<C>`
Writes a bay's **stored filament setting** into a unit that keeps one
(`capabilities.tray_write`, C1 §4.7.2): what the unit reports for a spool whose
tag it does not read. `OAMS=`/`BAY=` resolve as for `OAMSM_SET_BAY_SPOOL`. A
unit without the capability is refused at once (`"<unit> cannot store a
filament setting."`). Metadata, not motion, so **allowed at any time**; nothing
is persisted on the host (the unit keeps it).

- **Explicit form.** `MATERIAL` (1-8 printable ASCII characters), `MIN` and
  `MAX` (°C, 150-400, `MIN` not above `MAX`) are required; `CODE` (0-6
  printable ASCII characters, no spaces, for example a Bambu filament code such
  as `GFL99`; absent means the material's generic code from the C4 §7.4 table,
  e.g. `GFG99` for PETG, since a unit's preset needs one) and `COLOR`
  (`RRGGBB`, opaque, or `RRGGBBAA`; absent means no colour) are optional. The
  other values are sent as given. `SPOOL=` with any of
  these is refused (`"Give SPOOL= or CODE=, MATERIAL=, COLOR=, MIN=, MAX=, not
  both."`). The command waits for the unit's admission only (at most 2 s,
  pausing the command, not the reactor): `accepted` answers `"Writing <code>
  <material> #<RRGGBBAA> <min>-<max> C to <unit> Spool <n>."`; a refusal is the
  command's error (`"<unit> Spool <n> refused the filament setting:
  <busy|invalid_bay|unavailable|faulted>."`, for `invalid_field` with `"(it
  cannot store this code or material; nothing was written)"`, which is
  permanent; no answer: `"<unit> Spool <n> did not answer the filament setting
  write."`). The completion follows in the console: `"Stored the filament
  setting of <unit> Spool <n>."` or `"<unit> Spool <n> did not store the
  filament setting (<failed|timeout>)."`. One write per bay at a time
  (`"a filament setting is already being written to <unit> Spool <n>"`).
- **From Spoolman.** `SPOOL=<id>`, or no field at all for the bay's linked
  spool (none: `"<unit> Spool <n> has no linked spool: …"`). The host never
  reads Spoolman, so it calls `openams_spoolman`'s remote method
  `openams_spoolman_tray_write` (`unit`, `bay`, `spool_id`) and answers
  `"Asked openams_spoolman to write spool <id>'s filament to <unit> Spool
  <n>."`; without the component it is refused (`"openams_spoolman is not
  available to read spool <id> (…); give CODE=, MATERIAL=, COLOR=, MIN= and MAX=
  instead."`). The component maps the spool (C4 §7.5) and sends the explicit
  form, so the write reports as above; a failure on its side (Spoolman offline,
  no such spool) goes to the console as `"!! The filament of spool <id> was not
  written to <unit> Spool <n>: <reason>"`.
- **Automatic sync.** With `settings.sync_unit_tray_info` on, `openams_spoolman`
  sends the explicit form itself when a bay's linked spool changes (C4 §7.5).

### `OAMSM_SET_OPTION NAME=<setting> VALUE=<bool>` (D3)
Sets one of the six settings in C2 §4.9 (accepted names:
`read_tag_on_insertion, read_tags_at_startup, auto_reload_on_runout,
auto_create_spoolman_spools, apply_pa_on_load` — RESOLVED, C2 3 — and the
additive `sync_unit_tray_info`, C4 §7.5). `NAME` must
be one of these six (proposed error text, mirroring existing style:
`"Unknown OpenAMS setting '%s'"`). Persists via
`persist_config_option("oams_manager", name, value)` — the writeback
primitive already exists (`oams_manager.py:892-906`). **Preconditions
(RESOLVED, C3 3): allowed at any time**, regardless of lane state.

### `OAMSM_STAGE PLAN=<names> [LABEL_<NAME>="<text>"] | NAME=<step> [DETAIL="<text>"] | FAILED=1 | DONE=1 [FPS=<lane> | GROUP=<group>]` (H2)
Called by the toolchange and unload macros to publish their plan and progress
(C2 §5.3). **It never raises**: every mistake below is a warning and a no-op,
so a stage report can never abort a toolchange.
- `PLAN=cut,retract,feed` begins a plan on the lane, replacing any previous
  one (`index` null, `failed` null). A plan has 1 to 8 steps and no
  duplicates. A step name is one of the **standard** names (`cut`, `retract`,
  `feed`, `purge`, `calibrate`, `heat`, `home`, `clean`) or a **custom** one
  matching `^[a-z][a-z0-9_]{0,15}$`.
- `LABEL_<NAME>="<text>"` (with `PLAN=`) is the display label of the step
  `<name>`. Klipper upper-cases parameter names, so they are matched
  case-insensitively (`LABEL_BRUSH` labels the step `brush`). The label is
  trimmed, at most 47 bytes of UTF-8, cut on a code point boundary and never
  in the middle of a character; an empty label is the same as none. A label
  may be given for a standard name too, overriding the interface's own. A
  label for a name the plan does not carry is **reported and dropped**, and
  the plan is still accepted. A custom step needs no label: the interface
  shows its name.
- `NAME=<step>` enters that step of the active plan (`index` = its position).
  `DETAIL="<text>"` sets the plan's `detail` — trimmed, at most 24 bytes of
  UTF-8, cut on a code point boundary. `DETAIL` with any other request is a
  mistake to report and ignore.
- `FAILED=1` marks the current step failed (the first failure wins).
- `DONE=1` ends the plan (`stage` becomes null).

**Lane:** `FPS=`; else `GROUP=` through the group → lane map; else the sole
lane; else the single lane with an active plan or an operation in flight;
else a warning.

**Warnings (no-op):** an unresolvable lane; an invalid or unknown step name,
a plan of more than 8 steps or one that repeats a step, or a `NAME=` not in
the plan; a `NAME=` earlier than the current step; `LABEL_<NAME>` without
`PLAN=`, or `DETAIL` without `NAME=`; `FAILED` or `DONE` with no plan;
`FAILED=1` before any step started.

**Failure attribution (C2 §5.3 rule 6).** A macro that raises mid-plan never
reaches its own `FAILED=1`, so the manager listens for Klipper's
`gcode:command_error` and fails the current step of the one running macro
plan. The macro is not expected to report anything on the error path.

### `OAMSM_HEAT_WAIT HEATER=<name> MINIMUM=<C> [FPS=<lane>]` (Stop)
`_OAMS_HEAT` waits for the extruder with this instead of `TEMPERATURE_WAIT`. It
polls the heater's `temperature` with the reactor (every 0.5 s) and returns
when it is at least `MINIMUM`, exactly like `TEMPERATURE_WAIT`, but it raises
`Stopped by the user` as soon as the lane's stop flag is set (see
`openams/stop` below). The lane is `FPS=`, else the lane whose extruder is
`HEATER`, else as for `OAMSM_STAGE`. An unknown heater raises. Without a stop
it behaves as a plain wait.

### `OAMSM_CHECK_STOP [FPS=<lane>]` (Stop)
The macros call it right before entering each step (`home`, `heat`, `cut`,
`retract`, `feed`) and before each `_OAMS_HOOK_*` hook. It raises `Stopped by
the user` when the lane's stop flag is set and does nothing otherwise (an
unresolvable lane is "not set").

### Klipper webhook `openams/stop` [`lane`] (Stop)
Stops the **whole** running sequence, which G-code cannot do: a command queues
behind the macro it should interrupt, while a webhook runs in the reactor even
while a macro waits. Without `lane`, every lane running a sequence is stopped;
a lane **is running a sequence** when it has an active macro plan, a firmware
load or unload in flight, or a heat wait in progress. For each such lane it
sets the stop flag and cancels the in-flight firmware load or unload at once
(the `openams/cancel_load` path, extended to unloads). Answers
`{"result": "stopping"}`, or `{"result": "nothing running"}` (also for an
unknown lane). The flag clears when a new plan is announced (`OAMSM_STAGE
PLAN=`), when the plan ends (`DONE=1`) and when the stop has taken effect.

**When a stop takes effect** (a check or the heat wait raised, or the
cancelled operation made the macro fail): the manager fails the current step
(C2 §5.3), sets the lane's `message` to `Stopped by the user`, and, if the
sequence raised the extruder target (`_OAMS_PLAN` `raised` is 1) and no print
is running (`print_stats.state` is not `printing`), sets the heater back to
`_OAMS_PLAN`'s `pre_target`. It never touches the heater during a print. A
cancelled unload leaves the spool loaded, with message `cancelled` and no
fault. Firmware that cannot cancel an unload on the wire (anything but the
native-operation protocol) still stops its follower; the sequence then stops
when the unload ends, at the next boundary.

Moonraker exposes it as `POST /server/openams/stop` (C4 §9.1).

### `OAMS_DRYER_START ... ROTATE=<0|1>` (H6)
Extends the existing `OAMS_DRYER_START` (§2) with spool rotation during
drying, gated on `capabilities.spool_rotation_during_drying` (`ams_family.py`:
`true` only for AMS 2 Pro today).

**H6 (2026-09-28):** every unit reports
`AMS_CAP_DRYER_SPOOL_ROTATION = 0` and no rotation parameter exists on the
wire (C1), so `ROTATE=1` is refused with `"this unit cannot rotate spools
while drying"` whenever the capability is false, and `ROTATE=0` (the default)
is today's command. `OAMS_DRYER_START` is also refused with `"the dryer needs
the power adapter; connect it and retry"` while the unit's `dryer.adapter` is
`false` (C2 §4.7).

### Load refused on an uncalibrated bay (D7)
See §1 `OAMSM_LOAD_FILAMENT` above — **EXISTS (H10, 2026-09-28):** `src/oams_state.py:585-639` (`_group_candidates` / `_uncalibrated_msg`).

## 5. Open questions

Items the design review resolved are marked **RESOLVED** in place
above (D2 persistence, §OAMSM_SET_BAY_SPOOL; the precondition rule, both
commands). What remains open:

1. **`OAMSM_STAGE` lane inference.** **RESOLVED (H2 design, F-3):** `FPS=`, else `GROUP=`, else the sole lane, else the single lane with an active plan or an operation in flight, else a warning (§4).
