# C2 — Host → UIs (status)

**Version: v1 (2026-09-28).** Covers the full `oams_manager` v2 status
object, the compact `openams_ui` projection, and the spool-linking surface;
every work package (H1–H18, S3–S4b) is resolved below.

Status: v1, review resolutions applied.
Companions: `C3_commands.md` (what changes these fields),
`C4_spool_linking.md` (the Spoolman side), `ARCHITECTURE.md` §4
(ownership), §5 (contracts), §8.1 (faults).

**Read this first (design decision, global): `lanes` is the UI API v1
ARRAY** — the shape HelixScreen already reads
(`legacy:docs/UI_API.md`, `helix:src/printer/ams_backend_openams.cpp:327-346`).
**The per-FPS object this line publishes today (keyed by lane name) is
`lanes_by_fps`** — same shape, additive, just renamed so both can coexist
under one status object (P0.3b). §4.1
and §4.2 give both shapes; §7 explains why HelixScreen needs no change.

Machine-readable forms:
- `schemas/oams_manager_status_v2.schema.json`: the full `oams_manager` object;
- `schemas/openams_ui_v1.schema.json`: the compact per-unit projection;
- `examples/c2/oams_manager_<scenario>.json` and `examples/c2/openams_ui_<scenario>.json`
  for the scenarios `idle`, `loading`, `loaded`, `runout_reload`, `error`, `drying`,
  `uncalibrated`, `multi_unit` (AMS 1 + AMS 2 Pro + AMS HT) and `remote_unit`;
- `check_examples.py` validates every example against its schema and holds every
  minified `openams_ui` example to 1024 bytes.

Evidence paths are relative to this repo's root (`src/…`) unless prefixed:
`legacy:` is the legacy klipper_openams host;
`helix:` is the HelixScreen checkout; `display:` is the display checkout;
`klipper:` is a Klipper checkout.

## 1. How to read this document

Every field carries an **origin**:

| Origin | Meaning |
|---|---|
| **EXISTS-v1** | published today by this line's `oams_manager` (`schema_version: 1`), file:line given |
| **UI-API-v1** | published by the legacy HelixScreen contract (`legacy:docs/UI_API.md`, `api_version: 1`); this host publishes it too (`docs/UI_API.md`) |
| **NEW-v2** | the design adds it; the work package that adds it is named |

"Source of truth" is the owner from `ARCHITECTURE.md` §4: **FW** firmware, **HR**
host reducer (`oams_state.py`), **HC** host config (the OpenAMS config file),
**SM** Spoolman (via the `openams_spoolman` component), **TAG** the tag itself
(read by firmware or an OpenAMS 1 mainboard's readers, decoded by the host).

Decision numbers (D2, D7, …) label the design decisions these contracts
follow; the display's own work packages are written "display WP D4".

## 2. The objects a UI can read

| Object | What | Status |
|---|---|---|
| `oams_manager` | full system status: lanes, topology, every unit's driver status | EXISTS-v1 (`src/oams_manager.py:394-407`); v2 below |
| `openams_ui <unit>` | compact per-unit projection for small clients | NEW-v2 (H5), §6 |
| `oams <name>` | one OAMS unit's driver status, identical to `oams_manager.devices[<name>]` (`src/oams.py:280-281`) | EXISTS-v1 |
| `follower <name>` | one inline follower's status (`src/follower.py:483-496`) | EXISTS-v1 |
| `fps` / `fps <name>` | `{fps_value}` (`src/fps.py:109-116`) | EXISTS-v1 |
| `mfrc522 <name>` | OpenAMS 1 mainboard dual RFID reader: `{oams, readers:[{oams, rfid_card, present, uid, block, filament, version, last_read_status, last_read_time, error}]}` (`src/mfrc522.py:471-483`, `:582-586`) | EXISTS-v1, not linked to a bay: **OPEN, a hardware fact (review, C2 9): which bays each reader covers** |
| `hdc1080 <name>` | OpenAMS 1 mainboard temperature/humidity `{temperature, humidity}` (`src/hdc1080.py:284-288`) | EXISTS-v1; **RESOLVED (review, C2 10):** each unit's config section names its own environment sensor (H1) |
| webhooks `openams/status`, `openams/topology`, `openams/cancel_load` | request/response, not subscribable | EXISTS-v1, §8 |
| `openams_spoolman` (Moonraker component) | spool-link status | **EXISTS (S3/H4, 2026-09-28):** `moonraker/openams_spoolman/` — `C4_spool_linking.md` |

Klipper's API server polls each subscribed object's `get_status()` every 0.25 s
and sends only the top-level fields whose value changed
(`klipper:klippy/webhooks.py:531`, `:549-595`). A change inside a nested value
resends the whole top-level field.

## 3. Versioning and compatibility

- **`schema_version: 2`** is the version of this contract. v2 is additive: every
  EXISTS-v1 field keeps its key, type, and meaning (ARCHITECTURE §1.3: "A field is
  never re-meant").
- **`api_version` stays `1`** and **`schema` stays `"openams.manager"`**. HelixScreen
  accepts the object only when `api_version == 1` exactly and `schema ==
  "openams.manager"` (`helix:include/openams_api.h:16-31`); bumping `api_version`
  would disconnect every deployed HelixScreen. The two version keys are
  independent: `schema_version` versions this line's shape, `api_version` the
  UI API line.
- **Clients ignore unknown fields** (ARCHITECTURE §8.3; `legacy:docs/UI_API.md:194-196`).
- **`lanes` collision — RESOLVED (review, global; was OQ-1).**
  Both lines publish a top-level `lanes` with different types: this line an
  **object keyed by FPS lane** (`src/oams_manager.py:396`), UI API v1 an
  **array** of `{id, state, current_group, current_slot, following, direction,
  message, pressure, set_point}` (`legacy:src/oams_manager.py:163-170`, `:183`),
  which HelixScreen reads as an array
  (`helix:src/printer/ams_backend_openams.cpp:327-346`). One key cannot carry
  both. **Resolution: `lanes` is the UI API v1 array (HelixScreen's shape,
  unchanged); the per-FPS object moves to `lanes_by_fps` (§4.2), same shape,
  including the v1 per-lane enrichment fields this document already folded
  in (`pressure`, `set_point`, plus the v2 additions).** A HelixScreen reading
  a v2 `oams_manager` sees `lanes` exactly as before — see §7.

## 4. `oams_manager` fields

### 4.1 Top level

| Key | Type | Null | Origin | Source of truth | WP | Evidence / note |
|---|---|---|---|---|---|---|
| `schema_version` | int | no | EXISTS-v1 key, value 2 in v2 | — | H1 | `src/oams_manager.py:403` (value 1 today) |
| `api_version` | int, const 1 | no | UI-API-v1 | — | P0.3b | `legacy:src/oams_manager.py:181`; must stay 1 (§3) |
| `schema` | string, const `"openams.manager"` | no | UI-API-v1 | — | P0.3b | `legacy:src/oams_manager.py:181` |
| `ready` | bool | no | EXISTS-v1 | HR | — | `src/oams_manager.py:403`; false until the first hardware-snapshot resync and whenever a unit's snapshot or motion permission is lost (`:259-276`, `:323-338`) |
| `current_group` | string | yes | EXISTS-v1 (also UI-API-v1) | HR | — | `src/oams_manager.py:397-404`: group of the first LOADED lane; kept top-level for macros (`oams_macros.cfg:65`) |
| `lanes` | array, §4.2a | no | UI-API-v1 | HR | P0.3b | RESOLVED: the legacy array shape, unchanged; `legacy:src/oams_manager.py:163-170` |
| `lanes_by_fps` | object, lane name → §4.2 | no | EXISTS-v1 (was published as `lanes`) | HR | — | `src/oams_manager.py:396`; renamed per the design review so `lanes` can be the array above |
| `topology` | object, §4.3 | no | EXISTS-v1 | HC | — | `src/oams_manager.py:405`, `:379-392` |
| `devices` | object, unit name → §4.4 | no | EXISTS-v1 | FW + HC + HR | — | `src/oams_manager.py:406-407` |
| `settings` | object, §4.9 | no | NEW-v2 | HC | H7 | D3 |
| `commands` | object `{load?, unload?, cancel?, reset?}` → G-code word | no | UI-API-v1 | host (registered commands) | P0.3b | `legacy:src/oams_manager.py:171-179`: a key appears only while its command is registered |
| `units` | array, §4.10 | no | UI-API-v1 | FW + HC | P0.3b | `legacy:src/oams_manager.py:130-146` |
| `groups` | array, §4.10 | no | UI-API-v1 | HC | P0.3b | `legacy:src/oams_manager.py:148-157` |

### 4.2a `lanes[]` — UI-API-v1 array (RESOLVED, the `lanes` key)

One entry per FPS lane, in the order the legacy line sorts them (today, the
sole lane `"fps"`). Frozen field meanings (`legacy:docs/UI_API.md`,
`legacy:src/oams_manager.py` `_build_api_status`); HelixScreen reads this
shape verbatim (`helix:src/printer/ams_backend_openams.cpp:327-345`).

| Key | Type | Note |
|---|---|---|
| `id` | string | `"fps"` today (one lane) |
| `state` | string | lower-case `op` name: `unloaded`, `loading`, `loaded`, `unloading`, `calibrating`. HelixScreen also understands `error`/`paused`, never emitted by either host line today |
| `current_group` | string or null | |
| `current_slot` | int or null | the global slot id (§4.10) of the loaded spool, not a bay index |
| `following` | bool | |
| `direction` | 0 or 1 | |
| `message` | string or null | **doc/code drift, kept:** the legacy line always publishes `null` here (`legacy:src/oams_manager.py`); this line may publish `lanes_by_fps.<l>.message` verbatim. HelixScreen does not read this field, so the drift is harmless |
| `pressure` | number or null | fraction 0..1 |
| `set_point` | number or null | |

### 4.2 `lanes_by_fps.<fps>` (was published as `lanes` — RESOLVED, renamed)

One entry per FPS lane (one per toolhead), keyed by the lane's short name.

| Key | Type | Unit | Null | Origin | Source | WP | Evidence / note |
|---|---|---|---|---|---|---|---|
| `op` | enum `UNLOADED`, `LOADING`, `LOADED`, `UNLOADING`, `CALIBRATING` | — | no | EXISTS-v1 | HR | — | `src/oams_manager.py:375`; `src/oams_state.py:56-60`; machine in §5.1 |
| `group` | string | — | yes | EXISTS-v1 | HR | — | `src/oams_manager.py:375` |
| `unit` | `[oams_idx, bay]` | — | yes | EXISTS-v1 | HR | — | `src/oams_manager.py:375`; a Python tuple, JSON-encoded as an array (`src/oams_state.py:88`) |
| `runout` | enum `idle`, `pausing`, `coasting`, `loading` | — | no | EXISTS-v1 | HR | — | `src/oams_manager.py:376`; `src/oams_state.py:63-66`; meaningful only while `op` is `LOADED`; §5.2 |
| `following` | bool | — | yes | EXISTS-v1 | HR | — | `src/oams_manager.py:376`; `null` means a follower command failed and its state is unknown (`src/oams_state.py:89-90`, `:575-584`) |
| `since` | number | s, Klipper reactor monotonic clock | no | EXISTS-v1 | HR | — | `src/oams_manager.py:377`; not wall time |
| `message` | string | — | yes | EXISTS-v1 | HR | — | `src/oams_manager.py:377`; values in §5.1 |
| `state` | string, lower-case `op` | — | no | EXISTS-v1 (UI-API-v1 carryover) | HR | — | `src/oams_manager.py` `_lane_status`: `op.lower()`, the value `lanes[].state` (§4.2a) is built from; published on this object since UI API v1, so it stays (added to the table 2026-09-28, H1 review) |
| `current_slot` | int | — | yes | EXISTS-v1 (UI-API-v1 carryover) | HR | — | `src/oams_manager.py` `_lane_status`: the global slot id (§4.10) of `unit`, the value `lanes[].current_slot` is built from; same history as `state`. `oams_macros.cfg` reads it (`lanes_by_fps.fps.current_slot`, the targeted toolchange and `OPENAMS_UNLOAD`), so it must not be dropped |
| `direction` | int 0 (reverse) or 1 (forward) | — | no | NEW-v2 here; UI-API-v1 `lanes[].direction` | HR | H1 | `LaneState.direction` exists (`src/oams_state.py:91`), not published today; `legacy:src/oams_manager.py:167` |
| `stage` | object `{plan, index, failed}` | — | yes | NEW-v2 | HR (from macros via `OAMSM_STAGE`, and the manager's own operations) | H2 | §5.3 |
| `motion_stage` | string | — | yes | NEW-v2 | FW | H2 | the loaded or busy unit's firmware `motion_stage` name (C1 §3), or `null`; diagnostic only, never a plan step (§5.3) |
| `reload_target` | `[oams_idx, bay]` | — | yes | NEW-v2 | HR | H1 | `LaneState.reload_target` exists (`src/oams_state.py:98`), not published today |
| `runout_from` | `[oams_idx, bay]` | — | yes | NEW-v2 | HR | H1 | the bay that ran out; today only implicit (`unit` during the runout phases, lost when the reload succeeds, `src/oams_state.py:634`); §5.2 |
| `pressure` | number 0..1 | fraction of FPS compression | yes | NEW-v2 here; UI-API-v1 `lanes[].pressure` | FW | P0.3b | `legacy:src/oams_manager.py:186-196` (rounded to 0.01, republished only after a 0.02 move); `null` when FPS feedback is unavailable (`src/oams_manager.py:409-411`) |
| `set_point` | number | fraction | yes | NEW-v2 here; UI-API-v1 `lanes[].set_point` | HC | P0.3b | `legacy:src/oams_manager.py:198-206`: the feeding unit's `fps_target` |
| `pressure_band` | `[low, high]` | fractions | yes | NEW-v2 | HC | — | the same unit's `fps_lower_threshold` and `fps_upper_threshold`, the band its regulator keeps the pressure in; `null` when no unit reports a valid pair. Informational: a reading outside it is not a fault by itself |
| `extruder` | string | — | no | NEW-v2 | HC | H1 | the lane's extruder; today only in the `openams/status` webhook (`src/oams_manager.py:419-420`); C4 needs it for `SET_PRESSURE_ADVANCE EXTRUDER=` |
| `motion_settled` | bool | — | no | NEW-v2 | HR | H1 | ; published form of the motion-safety predicate (§5.6): `true` iff nothing is moving and no motion action is in progress: op is `UNLOADED` or `LOADED` with runout `idle`, the printer is not printing, `following` is not `null`, no bay positioning, no `stop`/`pause` fault on any unit of the lane, no `follower_uncertain`, and all units connected. `following` may be `true` on an idle `LOADED` lane (corrected 2026-09-28: requiring `false` made every loaded lane unsettled and hid **Unload**). A UI uses this in place of re-deriving the predicate itself |
| `motion_blocked_reason` | string | — | n/a (absent when not applicable) | NEW-v2 (additive) | HR | — | non-null only while `following` is `null`: why `motion_settled` is false (follower state unknown after a restart, or a follower command was not acknowledged). absent otherwise. After a restart the manager resolves it itself (see the restart note below), so it is normally transient |
| `filament_temp_c` | int | °C | yes | NEW-v2 | SM, via the component | H2 | the loaded bay's `devices.<unit>.bays[i].filament.temp_c` (§4.5.1), so an unload can heat to the temperature of what is actually in the extruder. `null` when the lane holds nothing, or when that bay's temperature is unknown |

Not published, by design: `op_gen`, `op_deadline`, `pause_origin`, `coast_origin`,
`prior_op` (reducer bookkeeping, `src/oams_state.py:96-106`) and
`uncertain_follower_units` (`:93`; RESOLVED, C2 11: published derived, per unit, as `devices.<unit>.follower_uncertain`, §4.4).

### 4.3 `topology` — EXISTS-v1, unchanged

`src/oams_manager.py:379-392`, built from `src/oams_topology.py`.

| Key | Type | Evidence |
|---|---|---|
| `schema_version` | int, 1 | `:382` |
| `fps` | array of lane names | `:383` |
| `oams.<unit>.idx` | int | `:384`; `oams_idx`, unique across units and followers (`src/oams_topology.py:163-168`) |
| `oams.<unit>.lane` | string | `:384` |
| `oams.<unit>.kind` | `oams` or `follower` | `:385`; `src/oams_topology.py:51` |
| `oams.<unit>.family` | any name in the family registry (`src/ams_family.py` `FAMILIES`): `ams1`, `ams2`, `ams_ht`, or one of the 23 klipper-mmu devices (`boxturtle`, `nightowl`, `custom`, `3d_chameleon`, `3ms`, `angry_beaver`, `emu`, `ercf_1_1`, `ercf_2_0`, `ercf_3_0`, `htlf`, `kms`, `mmx`, `mmx6`, `quattrobox_1_0`, `quattrobox_1_1`, `quattrobox_2`, `tradrack`, `low_rider`, `pico_mmu`, `qidi_box`, `btt_vivid`, `claymore`); plus `inline_follower`, which is a unit **kind** rather than a registry family and appears only here | `:385`; `src/oams_topology.py:147-148`; `src/ams_family.py` |
| `oams.<unit>.bays` | int 1..16 | `:386`; `src/oams_topology.py:140-143` |
| `oams.<unit>.capabilities` | object, the same dict as `devices.<unit>.capabilities` (`{}` for a follower) | `:387-388` |
| `groups.<g>.lane` | string, or `null` for an empty group | `:390`; `src/oams_topology.py:100-102` |
| `groups.<g>.bays` | array of `"<unit>-<bay>"` in declared (fallback) order | `:391` |

Rules G1-G3 hold for every published topology: every group bay names a defined
unit and a valid bay; a group's bays share one lane; a bay is in at most one
group (`src/oams_topology.py:15-23`, `:183-222`).

### 4.4 `devices.<unit>`

The EXISTS part is the unit driver's `get_webhook_status()`. Its shape depends on
the unit kind; v2 adds the same NEW-v2 block to every kind.

**EXISTS-v1, OAMS unit** (`src/oams.py:408-445`), source FW unless noted,
no WP (unchanged):

| Key | Evidence | Key | Evidence |
|---|---|---|---|
| `schema_version` (1) | `:412` | `kp`, `ki`, `kd` (HC) | `:430` |
| `current_spool` | `:413` | `encoder_clicks` | `:431` |
| `oams_idx` (HC) | `:414` | `i_value` | `:431` |
| `connected` | `:415`, `:290-293` | `protocol_version` | `:432` |
| `fps_value` | `:416` | `gen_matched_protocol` | `:433` |
| `fps_upper_threshold`, `fps_lower_threshold` (HC) | `:417-418` | `family` | `:434` |
| `f1s_hes_value`, `hub_hes_value` (arrays, one per bay) | `:419-420` | `bay_count` | `:435` |
| `fps_target`, `current_kp`, `current_ki`, `current_kd`, `current_target` (HC) | `:421-425` | `capabilities` (§4.4.1) | `:436` |
| `f1s_hes_value_0`..`_3`, `hub_hes_value_0`..`_3` | `:426-429` | `capability_warnings` | `:437` |
| `bays` (§4.5) | `:438-441` | `telemetry` (§4.4.2) | `:442` |
| `supported_actions` (§5.6) | `:443`, `:358-406` | | |

**EXISTS-v1, inline follower** (`src/follower.py:483-499`): `current_spool`,
`pre_switch`, `post_switch`, `following`, `velocity`, `step_count`, `fps_stale`,
`ff_underrun`, `op_in_flight`, `error_latched`, `protocol_version`. It has no
`bays`, `connected`, or `supported_actions` today; v2 requires them (single bay 0).

**NEW-v2, every kind:**

| Key | Type | Null | Source | WP | Note |
|---|---|---|---|---|---|
| `identity.serial` | string | yes | FW (the identity query, C1 §2); the unit MCU's `canbus_uuid` when the firmware has no identity query (STM32F072 board) | H1 | the display binds by it (D4) |
| `identity.firmware` | string | yes | FW | H1 | today `capabilities.firmware_version` from the dictionary `MCU_VERSION` (`src/ams_family.py:285`) |
| `identity.board` | string | yes | FW | H1 | today `capabilities.board_name` (`src/ams_family.py:284`): the dictionary's `BOARD_NAME`, or `"oams-stm32f072"` for the STM32F072 replacement mainboard, which has none (recognized from its `MCU` constant, C1 §1.4) |
| `calibration.state` | enum `configured`, `calibrating`, `uncalibrated` | no | HC + HR | H1 | D7, derivation §5.4 |
| `calibration.path_length_mm` | number | no | HC | H1 | the unit's shared `ptfe_length` in mm; `0` when none is configured (the sample config's `ptfe_length: 0`), never `null`: `calibration.state` is `uncalibrated` then |
| `environment` | `{temp_c, rh_pct, source}` | yes | FW, or a host `[hdc1080]` sensor when the firmware has no environment query (`source: "hdc1080"`) | H1, F3 | §5.9 |
| `dryer` | `{state, target_c, remaining_s, heater_pct, fan_pct, fault, adapter}` | yes: no dryer | FW | H1, H6, F2, F5 | §5.9 |
| `faults` | array of §4.8 | no | FW + HR | H1 | ARCHITECTURE §8.1; §5.7 |
| `follower_uncertain` | bool | no | HR | H1 | **RESOLVED (review, C2 11):** true iff this unit appears in any lane's `LaneState.uncertain_follower_units` (`src/oams_state.py:93`) — a follower stop that was never confirmed. Safety-relevant; published because a UI must be able to show it even though the reducer never surfaces it as a fault |

`connected`, `bays`, `supported_actions`, and `follower_uncertain` become
required for every kind.

#### 4.4.1 `capabilities` (EXISTS-v1)

The family profile from the **family registry** (`src/ams_family.py`:
`_PROFILES` for the AMS families, `_mmu_profile` for the klipper-mmu
devices; the full list is `FAMILIES`) refined by firmware constants
(`resolve_capabilities`), then by the driver
(`telemetry`, `telemetry_channels`; `src/oams.py`
`_setup_ams_ht_telemetry`). Every
key is EXISTS-v1 and open content (new firmware may add keys). The ones UIs
gate on: `family`, `display_name`, `bays`, `dryer`, `dryer_target_min_c`,
`dryer_target_max_c`, `spool_rotation_during_drying`,
`ptfe_calibration`, `hub_hes_calibration`, `rfid`, `rfid_reader_scope`,
`board_name`, `firmware_version`, `firmware_capability_schema`. A UI must not
infer an ability from `family` (ARCHITECTURE §1.4); it reads `supported_actions`.
`tray_info`, `tray_read` and `tray_write` (a unit that decodes its own tags,
C1 §4.7.1, and stores a per-bay filament setting, C1 §4.7.2) appear only when
the firmware publishes their constants; the driver turns `tray_write` false
when the dictionary lacks its command.

**The registry's `family` enum in the two schemas is checked against
`ams_family.FAMILIES`, not generated from it** — a schema is a published
artifact, so a registry edit must not silently rewrite it.
`test/test_ams_family_registry.py` fails when the two disagree:

| Schema | Where | Enum |
|---|---|---|
| `openams_ui_v1` | `properties.family` | `FAMILIES` + `inline_follower` |
| `oams_manager_status_v2` | `$defs.topology…properties.family` | `FAMILIES` + `inline_follower` |
| `oams_manager_status_v2` | `$defs.device.properties.family` | `FAMILIES` (a follower's driver carries no OAMS family) |

**klipper-mmu profiles.** A `[klipper_mmu <name>]` unit is a topology unit of
`kind: "oams"` whose `family` is the device id, exactly as
`klipper_mmu/host/__init__.py` `_OPENAMS_DEVICE_IDS` spells it. Its profile
adds three keys the AMS profiles do not have: `selector_kind` (a feeder per
lane, `linear`, `rotary`, `servo` or `indexed`),
`selector_kind_firmware` (the matching `KLIPPER_MMU_SELECTOR_KIND_*`), and
`bays_min` / `bays_max` (the lane-count range, since a device is built for one
lane count — BoxTurtle 4 or 8, NightOwl 2 or 4, up to 12 on `custom`).
It adds four more that say what such a unit can do beyond an AMS: `preload`,
`unpreload` and `eject` are true for every klipper-mmu family, and `home` is
true exactly when `selector_kind` is not `feeder_per_lane` — a lane fed
directly by its own feeder has no selector to home. An AMS profile carries
none of them, so an AMS unit publishes none of them (§5.6, C3 §2).
`bays` is the installed count, and `spool_drive_count` tracks it. Capability
defaults: **no dryer and no RFID**; motion as klipper-mmu reports it, which
means the MCU owns it (`device_owned_motion_control`,
`autonomous_follower`, `normal_motion` all true). The only
encoder is the one the linear machines wire in, so `encoder` is true exactly
for `linear`. A klipper-mmu unit publishes no `telemetry` tree and no `dryer`
object; its `ptfe_calibration` default is false
because C5 §8 records that the geometry calibration workflow is not
persisted or homed yet. Its `supported_actions` are `clear_errors`, `load`,
`unload` and `follower` — the four that exist on both wires — plus
`preload`, `unpreload`, `eject` and `home` as the capabilities above allow
(C5 §7.1, C3 §2). C5 §7 maps every
C5 op, state, stage, admission result, fault and result code to its C1/C2
counterpart and lists, item by item, the ones that have none.

**Bay counts.** Every bay container allows 16: `openams_ui.bays`
(`maxItems: 16`), `topology.oams.<unit>.bays` (`minimum: 1`,
`maximum: 16`), and `devices.<unit>.bays`, which carries no bound of its own.
1 through 12 all validate in both schemas
(`test/test_ams_family_registry.py`); the generated
`boxturtle_8` example is the 8-lane case end to end.

#### 4.4.2 `telemetry` (EXISTS-v1, open content)

Channel name → last frame with a host `timestamp` (reactor monotonic) and, for
enumerated fields, a host-side `*_name`. Channels exist only when the firmware
advertises them (`src/oams.py` `_setup_ams_ht_telemetry`).

| Channel | Named fields added by the host | Evidence |
|---|---|---|
| `board` | — | `src/oams.py` `_board_status_received` |
| `domain` | `motion_state_name`, `motion_stage_name`, `dryer_state_name`, `motion_fault_name`, `dryer_fault_name`, `system_fault_name` | `_domain_status_received` |
| `dryer` | `target_c`, `heater_c`, `chamber_c`, `state_name`, `fault_name`, `humidity_gm3`, `vent_position_name` | `_dryer_status_received` |
| `environment` | — (`rh_centipercent`, `temperature_centic`, `valid`) | `_environment_status_received` |
| `rfid` | `result_name`, `source`, `bay` | `src/oams_tags.py`: a unit-decoded read that failed or was refused (C1 §4.7.1) |
| `tray_write` | `bay`, `result_name` (`accepted`, `busy`, `invalid_bay`, `unavailable`, `faulted`, `invalid_field`, `written`, `failed` or `timeout`) | `src/oams.py` `_publish_tray_write`: each admission, completion or timeout of a stored-setting write (C1 §4.7.2); only on a unit with `capabilities.tray_write`, and only after a write |

v2 does not re-mean any of these. The NEW-v2 `environment` and `dryer` blocks
are named projections of `telemetry.dryer` so a UI need not parse raw telemetry.

### 4.5 `devices.<unit>.bays[i]`

**RESOLVED (review, C2 4/7, global "Spool facts leave the host
status"):** there is no `spool` summary field here. The host never talks
HTTP, so it cannot itself hold Spoolman facts (material, color, vendor,
remaining weight) fresh; those live only in the C4 `openams_spoolman` status,
keyed `"<unit>-<bay>"` (`C4_spool_linking.md` §8). This table carries only
the link (`spool_id`, `link_source`) plus host-owned facts. **Amended (H2):**
the host also holds an **operating copy** of the bay's material and working
temperature, `filament` below, pushed by that component
(`OAMSM_SET_BAY_FILAMENT`, C3). It is not a Spoolman summary: it is what the
machine heats to, it is held in memory only and never persisted, and Spoolman
stays the store — after a Klipper restart the entry is `null` until the
component pushes it again.

| Key | Type | Unit | Null | Origin | Source | WP | Evidence / note |
|---|---|---|---|---|---|---|---|
| `index` | int | — | no | EXISTS-v1 | HC | — | `src/oams.py:439` |
| `ready` | bool | — | no | EXISTS-v1 | FW (F1S bay sensor) | — | `src/oams.py:439`; `bay_loadable()` is the value the reducer uses (`src/oams.py:329-332`, `src/oams_manager.py:228`) |
| `loaded` | bool | — | no | EXISTS-v1 | FW (hub HES) | — | `src/oams.py:439` |
| `positioning` | bool | — | no | NEW-v2 | FW | H16 | **Review.** `true` while this bay is the subject of a firmware-autonomous insertion positioning sequence (the `positioning` motion state, §4.1 / C1 §4.1). `false` otherwise. A UI must not offer load, unload, or calibrate_ptfe on this bay while `positioning` is `true`; the firmware replies `Busy`. Cleared automatically when the `Positioning` motion state exits. |
| `calibrated` | bool | — | no | NEW-v2 | HC | H1 | D7: true iff `calibration_state` is `configured` |
| `calibration_state` | enum `configured`, `calibrating`, `uncalibrated` | — | no | NEW-v2 | HC + HR | H1 | D7; §5.4 |
| `path_length_mm` | number | mm | no | NEW-v2 | HC | H1 | D7; effective `ptfe_length_<bay>` or shared `ptfe_length`, clicks ÷ 1.14 (`src/oams.py:40`, `:296-313`); `0` when none is configured, never `null` |
| `spool_id` | int | Spoolman spool id | yes | NEW-v2 | HC (D2) | H3 | set by `OAMSM_SET_BAY_SPOOL` (C3) |
| `link_source` | enum `tag`, `manual`, `none` | — | no | NEW-v2 | HC (D2) | H3 | §5.5 |
| `tag` | §4.6 | — | yes | NEW-v2 | TAG | H1 | last successful read; `null` when no tag is present |
| `spool_hint` | `{mass_g, outer_radius_mm, valid}` | g, mm | yes | NEW-v2 | SM, via the component | reserved (D6) | the firmware may ignore it in v1 |
| `filament` | `{material, temp_c, source}` | — | yes | NEW-v2 (H2) | SM, via the component | H2 | §4.5.1; `null` when nothing was pushed, when it was cleared, or after a Klipper restart |
| `supported_actions` | array of `load`, `unload`, `calibrate_ptfe`, `set_spool`, `clear_spool` | — | no | NEW-v2 | HR | H1 | derivation §5.6 |

#### 4.5.1 `devices.<unit>.bays[i].filament` — NEW-v2 (H2)

The bay's operating copy of what it holds, pushed by `openams_spoolman` with
`OAMSM_SET_BAY_SPOOL`'s sibling `OAMSM_SET_BAY_FILAMENT` (C3) and computed by
that component (C4 §7.4). It exists so the macros can heat the nozzle before a
load, an unload or a toolchange; it is **memory only** — never written to a
config file, gone after a restart, and pushed again by the component when
Klipper comes back. Spoolman stays the store (C4 §8).

| Key | Type | Null | Note |
|---|---|---|---|
| `material` | string or null | yes | the filament's `material` as Spoolman has it, trimmed, at most 16 characters |
| `temp_c` | int (°C) or null | yes | the working temperature: the spool's own `settings_extruder_temp`, else the material table's, else unknown |
| `source` | enum `spool`, `table` | no | where `temp_c` came from |

`null` (the whole entry) when nothing was pushed, when it was cleared, when the
spool link changed (the values described the old spool; C3), or after a Klipper
restart until the component pushes again. A bay with no temperature is
**unknown**, not wrong: the macros then use their own default.

### 4.6 `devices.<unit>.bays[i].tag` — NEW-v2 (H1)

| Key | Type | Note |
|---|---|---|
| `uid` | lowercase hex string, or null | as the readers publish it: `src/mfrc522.py:476`, `src/oams.py:2159-2160`. **`null` exactly when `format` is `unit_summary`**: a unit that decodes its own tags reports no UID. Every consumer must accept a null `uid` |
| `format` | enum `bambu`, `spoolman_link`, `openspool`, `unit_summary`, `unknown` | `unit_summary` is a tag decoded by the unit itself (`AMS_CAP_TRAY_INFO`, C1 §4.7.1); `bambu` decodes on AMS 1 (`src/bambu_lab_tag_processor.py`); `spoolman_link` / `openspool` are NDEF records a reader may find; `unknown` is a tag this host cannot decode |
| `read_at` | number, reactor monotonic s | changes only on a new read (an insertion, startup, or an explicit `OAMS_RFID_SCAN`), never on a periodic poll of a tag already attributed: `openams_spoolman` treats a new `(uid, read_at)` as a re-read, which overrides a manual link (C4 §3) |
| `spoolman_id` | int or null | from a `web+spoolman:s-<id>` NDEF record |
| `openspool` | object or null (optional key) | an OpenSpool NDEF record (`application/json`), its fields verbatim: `protocol`, `version`, `type`, `color_hex`, `brand`, `min_temp`, `max_temp` (strings, as the OpenSpool format writes them); null or absent for other formats |
| `summary` | object (present exactly for `format: "unit_summary"`, else the key is absent) | the unit's decoded summary (C1 §4.7.1), below |
| `bambu` | object or null | `BambuLabTagProcessor.decode()` output with its keys verbatim: `manufacturer`, `type`, `detailed`, `color_argb`, `weight_g`, `diameter_mm`, `drying_temp_c`, `drying_time_h`, `bed_temp_c`, `hotend_max_c`, `hotend_min_c`, `nozzle_diameter`, `spool_width_mm`, `length_m`, `tray_uid`, `production` (`src/bambu_lab_tag_processor.py:68-87`). This is the dict an OpenAMS 1 mainboard already publishes as `mfrc522 <name>.readers[i].filament` (`src/mfrc522.py:478`) |

A failed read does not replace `tag`; it surfaces as an `info` fault (§5.7).

**`unit_summary` (a unit with `AMS_CAP_TRAY_INFO`).** The tag of a bay whose
unit decoded the spool's tag itself. `uid` is `null`, `spoolman_id` is `null`,
`bambu` is `null`, and `summary` is:

| Key | Type | Note |
|---|---|---|
| `code` | string or null | the manufacturer's filament code, e.g. `"GFB60"`; null when the unit reported none |
| `material` | string or null | the material name, e.g. `"ABS"` |
| `color_argb` | int or null | `0xAARRGGBB`, the same form as `bambu.color_argb` (the unit's wire value is `0xRRGGBBAA`, converted); null when the unit reported no colour |
| `nozzle_min_c`, `nozzle_max_c` | int or null | the nozzle range in °C; null when unreported |
| `manufacturer` | always null | A unit summary does not say who made the spool: a `GF…` code names a Bambu Studio filament *preset* (`GFB60` is Polymaker PolyLite ABS, `GF?99` the generic presets), and a per-bay stored setting carries no tag. A maker is published only from a decoded tag (the `bambu` object). |

```json
{"uid": null, "format": "unit_summary", "read_at": 412.7, "spoolman_id": null,
 "bambu": null,
 "summary": {"code": "GFB60", "material": "ABS", "color_argb": 4279312947,
             "nozzle_min_c": 240, "nozzle_max_c": 270, "manufacturer": "Bambu"}}
```

The unit's state decides the tag (C1 §4.7.1): *decoded* (4) publishes it;
*read, no tag* (3) publishes none (`tag` is `null`); *read failed* (5)
publishes none and leaves a `telemetry.rfid` note (`result_name:
"read_failed"`, `source: "tray_info"`, `bay`); *no spool* (0) and *present, not
read* (1) clear the tag as a falling `ready` does (kept while the bay is in
use or the lane is in a runout); *reading* (2) leaves what is published. A
repeat of an unchanged summary keeps its `read_at`, so only a read counts as a
re-read. No UID means a bay's tag is never "the same tag" as another bay's: the
move rule below applies to UIDs only. With `AMS_CAP_TRAY_READ` the insertion,
startup and `OAMS_RFID_SCAN` reads below ask the unit (`ams_tray_read`).

**Reads and attribution (S4, 2026-09-28).** A bay's tag comes from the reader
whose field covers it: a unit that decodes its own tags reports every bay (C1
§4.7.1), and on an OpenAMS 1 mainboard the `[mfrc522]` endpoint its config
maps to the bay (`bays_a` / `bays_b`, default `0,1` / `2,3`, not yet verified
against the hardware). A read happens:
- when a bay's `ready` rises and `settings.read_tag_on_insertion` is on, once
  that bay's `positioning` has ended (the firmware is busy until then);
- at startup with `settings.read_tags_at_startup`, for each ready bay that is
  the only ready bay its reader covers (a shared reader cannot tell several
  spools apart, so those bays wait for an insertion or an explicit read);
- on `OAMS_RFID_SCAN OAMS=<unit> BAY=<i>` (C3), the explicit re-read.

**Coverage note (OPEN, a hardware question):** on a reader that covers several bays, an explicit `OAMS_RFID_SCAN BAY=` reads whatever tag is in the field. The result is attributed to the named bay regardless of which bay the tag is physically closest to. Coverage per family (which reader sees a tag at each bay) is a hardware fact still to be measured.

A read that finds a tag sets `tag` with a new `read_at`; the same UID
published on another bay of the same unit moves to this one. A read that finds
no tag sets `tag` to `null`. When `ready` falls while the unit's lane has no
runout in progress, `tag` becomes `null` (the spool is gone); during a runout
it is kept (C4 §3: a runout is not removal). On an OpenAMS 1 mainboard, the endpoint's own
continuous polling also clears a UID it reports removed.

### 4.7 `devices.<unit>.environment` and `.dryer` — NEW-v2

`environment`: `temp_c` (°C), `rh_pct` (% relative humidity, 0-100),
`source` (string naming the sensor). `null` when the unit has no
environment sensor.

A unit whose firmware has no environment query (the STM32F072 board, C1 §1.4)
takes the reading from a host `[hdc1080]` sensor: `source` is `"hdc1080"`,
the sensor is the one the `[oams NAME]` option `environment_sensor: <object
name>` names, or the only HDC1080 sensor when the option is absent. No sensor,
several sensors without the option, or a sensor with no reading yet give
`null`. The same unit's `identity.serial` is its MCU's `canbus_uuid` (a unit
with neither an identity query nor a `canbus_uuid` keeps `serial: null`), and
its `identity.board` is `"oams-stm32f072"`.

`dryer` (`null` exactly when `capabilities.dryer` is false):

| Key | Type | Unit | Mapping from today's telemetry |
|---|---|---|---|
| `state` | string | — | `telemetry.dryer.state_name`: `off`, `preheat`, `hold`, `timed_dry`, `cooldown`, `fault` (`src/oams.py:1424-1427`); `unknown` (the host's own seventh value, in the schema's enum) before the first frame or for a state number the host does not know. **RESOLVED (review, C2 12): this is the final vocabulary** — the firmware owns the dryer states and reports exactly these six names; the host passes them through unchanged |
| `target_c` | number or null | °C | `telemetry.dryer.target_c` (`src/oams.py:1419-1423`; `null` when the firmware sends the -32768 sentinel) |
| `remaining_s` | int or null | s | `telemetry.dryer.remaining_s` |
| `heater_pct` | number or null | % | `heater_permille / 10` |
| `fan_pct` | number or null | % | `fan_permille / 10` |
| `fault` | string | — | `telemetry.dryer.fault_name` (`src/oams.py:1428`), `none` when clear |
| `adapter` | bool or null | — | whether the dryer's power adapter is connected, where the unit reports it; `null` elsewhere (H6). While it is `false`, `OAMS_DRYER_START` is refused and `supported_actions` omits `dryer_start` |

### 4.8 `devices.<unit>.faults[]` — NEW-v2 (H1)

| Key | Type | Note |
|---|---|---|
| `severity` | enum `stop`, `pause`, `info` | assigned by the host, §5.7 |
| `code` | string | a controller fault name (`src/oams.py:1316-1344`), a lane `message` (§5.1), or a reader result name (§5.7) |
| `text` | string | human text; for lane faults the reducer's reason (§5.7) |
| `bay` | int or null | `null`: unit-wide |
| `actions` | array of `clear_errors`, `clear_fault`, `resume`, `retry_load` | the recoveries a UI may offer (ARCHITECTURE §8.1), §5.7 |
| `source` | enum `firmware`, `lane`, `tag` | which vocabulary `code` comes from |

### 4.9 `settings` — NEW-v2 (D3, H7)

**RESOLVED (review, C2 3): these exact key names are accepted.**

| Key | Type | Default (D3) | Consumer |
|---|---|---|---|
| `read_tag_on_insertion` | bool | true | host reader scheduling |
| `read_tags_at_startup` | bool | true | host reader scheduling |
| `auto_reload_on_runout` | bool | true; false means pause instead of reloading | reducer `_runout_tick` (a behavior change, see C3 §6) |
| `auto_create_spoolman_spools` | bool | true | `openams_spoolman` (C4) |
| `apply_pa_on_load` | bool | false | `openams_spoolman` (C4) |
| `sync_unit_tray_info` | bool | true; keep a unit's stored per-bay filament setting (`capabilities.tray_write`) in step with the bay's linked Spoolman spool | `openams_spoolman` (C4 §7.5). **Additive**: the schema does not require it, and a host that predates it omits it (read as true) |

Persisted in the OpenAMS config like groups (D3); set by `OAMSM_SET_OPTION` (C3).
D3 removed two rows, which must not appear: a toolhead-sensor runout switch and
a remaining-filament estimate.

### 4.10 `units[]` and `groups[]` — UI-API-v1 (P0.3b)

| Key | Type | Evidence |
|---|---|---|
| `units[].id` | string, `str(oams_idx)` | `legacy:src/oams_manager.py:142` |
| `units[].name` | string, the config section's last token | `:143` |
| `units[].kind` | string (`"oams"` in the legacy line) | `:144` |
| `units[].topology` | enum `hub`, `linear`, `parallel`, `mixed`; UIs branch on this, not on a family (`legacy:docs/UI_API.md:119-124`) | `:144` (`"hub"` in the legacy line) |
| `units[].lane` | string (`"fps"` in the legacy line) | `:144` |
| `units[].connected` | bool (the manager's `ready` in the legacy line) | `:145` |
| `units[].slots[]` | `{id, bay, ready, loaded}` | `:137-139` |
| `groups[]` | `{name, lane, slots:[global slot id], next_bay, next_filament_temp_c}`, sorted by name | `:148-157`; the last two are H2 (§4.10.1) |

#### 4.10.1 `groups[].next_bay` and `groups[].next_filament_temp_c` — NEW-v2 (H2)

| Key | Type | Null | Note |
|---|---|---|---|
| `next_bay` | `[oams_idx, bay]` | yes | the bay a load of this group would take **now** |
| `next_filament_temp_c` | int (°C) | yes | that bay's `devices.<unit>.bays[i].filament.temp_c` (§4.5.1) |

`next_bay` is selected exactly as a load selects it — the reducer's own
`_group_candidates` (`src/oams_state.py:595`) with readiness, over the same
lane world a `Load(group)` is reduced against (`src/oams_manager.py`
`build_world`) — so the published answer and the command that runs can never
disagree. It uses the same `[oams_idx, bay]` pair shape as
`lanes_by_fps.<lane>.unit`, not a global slot id.

`null` when no bay of the group is ready right now, or when the hardware
snapshot cannot be built (the field is advisory and never fails a status).
`next_filament_temp_c` is `null` whenever `next_bay` is, and whenever that
bay's temperature is unknown; the macros then heat to their own default.

**Global slot ids.** Units are sorted by `(oams_idx, name)` and their bays
numbered 0, 1, 2, … across the whole snapshot (`legacy:src/oams_manager.py:100-115`).
UIs address a bay by this id (`OPENAMS_LOAD … SLOT=<id>`, C3) and must never
derive it from a unit name or an array position (`legacy:docs/UI_API.md:125-127`).
**RESOLVED (review, C2 14):** the id is stable only while the
set of units and their `oams_idx`s is; it renumbers when the topology
changes. Clients key their own state on `(unit, bay)`, not the global id, and
re-read `units[]` whenever `topology` changes.

In the merged line, `lane` and `connected` should carry each unit's real lane
and connection (`topology.oams.<u>.lane`, `devices.<u>.connected`). The examples
do so. The final values for a follower's `kind` and `topology` are P0.3b's call.

## 5. Semantics

### 5.1 Lane op machine (today)

The pure reducer `reduce(system, action, world, now)` (`src/oams_state.py:317-423`)
drives each lane. Transitions as the code has them:

| From | Event | Guard | To | `message` | Evidence |
|---|---|---|---|---|---|
| `UNLOADED` | `Load(group)` | `_load_guard` passes and a group member is ready (members in declared order, a local reservation first) | `LOADING` | `null` | `src/oams_state.py:503-514`, `:459-480` |
| `UNLOADED` | `LoadBay(unit)` | `_load_guard`, and the unit's local reservation allows the bay | `LOADING` (group: the bay's group, if any) | `null` | `:518-531` |
| `LOADING` | firmware SUCCESS | generation matches | `LOADED`, following forward | `loaded` | `:653-659` |
| `LOADING` | CANCEL | | `UNLOADED`; a follower enabled mid-load is stopped | `cancelled` | `:660-670` |
| `LOADING` | other code, or timeout | | `UNLOADED`; a timed-out firmware op is cancelled | `load failed` | `:671-683` |
| `LOADED` | `Unload` | runout is not `loading` | `UNLOADING` | | `:533-547` |
| `UNLOADING` | SUCCESS | | `UNLOADED` | `unloaded` | `:685-690` |
| `UNLOADING` | failure or timeout | | `LOADED`, follower stopped | `unload failed` | `:691-700` |
| `UNLOADED` or `LOADED` | `Calibrate(bay, kind)` | not busy, no reload in flight | `CALIBRATING` | | `:549-561` |
| `CALIBRATING` | any completion | | the op before calibrating | `calibrated` or `calibration failed` | `:702-710` |
| any | `ClearErrors` | (the automatic resync keeps an in-flight op) | recomputed from the hardware: `LOADED` if a grouped bay's hub sees filament, else `UNLOADED` | | `:386-400`, `:432-438` |
| any | `FirmwareFault` on the lane | | op frozen; any in-flight op settles as failed; print paused; lane-scoped actions ignored until the verified clear resyncs the lane | | `:324-362`, `:416-418`, `:364-384` |

Refusals do not change state and reach the caller as the command's error (C3):
`lane busy (%s)`, `follower state unknown on unit(s) %s; …`, `filament still
detected in a hub on this lane; unload it first`, `no ready spool in group %s`,
`nothing loaded`, `lane is auto-loading the next spool after a runout; …`,
`busy, cannot calibrate now` (`src/oams_state.py:471-479`, `:515-516`, `:535-542`,
`:555-556`).

Completions carry the op generation; a completion whose generation does not match
the lane's is ignored (`:599-605`). A per-op deadline (120 s, or a 300 s disconnect
backstop when every unit runs protocol ≥ 3) times an op out (`:69-78`, `:607-619`).

**Invariant (EXISTS, H13, 2026-09-28).** Every request either starts a generation or settles synchronously with a refusal. A latched firmware fault refuses with `firmware fault (%s) latched; OAMS_CLEAR_FAULT first`. Fixed in `src/oams_state.py`.

**Follower resolution (EXISTS, bounded).** A `LOADED` lane with `following=null` (startup resync, `OAMSM_CLEAR_ERRORS`, fault-clear resync) is resolved by the manager on every tick, through the same path as `OAMSM_FOLLOWER ENABLE=1 DIRECTION=1`, when the manager is ready and the lane is `LOADED`, runout idle and settled except for the unknown follower (not printing, units connected, no stop/pause fault, no uncertain unit, no positioning). If the unit refuses the command, or it is not confirmed, the manager first establishes a known stopped state (`following=false`, which makes `motion_settled` true so a UI can load/unload) and retries the forward follower with a backoff (`FOLLOWER_RETRY_BACKOFF`, 5 s, 15 s, 45 s after the first immediate try) while the lane stays idle. After the last try the lane stays stopped (or unknown only if even the stop was unconfirmed) until `OAMSM_CLEAR_ERRORS` or a restart begins a new resolution. An explicit `OAMSM_FOLLOWER ENABLE=0` cancels a pending retry. When a print job starts or resumes (`print_stats.state` becomes `printing`), every idle `LOADED` lane without a forward follower is queued for the same bounded arming (it never stops anything), so a lane left stopped by a failed flow or a refused re-arm still feeds during the print.

**Failed flow follower stop (EXISTS).** When an OpenAMS flow ends without `DONE` (a G-code error inside the plan, `OAMSM_STAGE FAILED=1`, or the user's stop), the manager stops that lane's follower once the lane is quiet: `following=false`, the lane stays as it physically is (a failed unload stays `LOADED`), stage `failed` names the step. The stop is deferred while a print job runs and while a firmware load/unload is in flight, and dropped if a newer plan starts. A successful flow (`DONE`) never triggers it, so the forward follower a load left armed stays.

**Runout needs a print job (EXISTS).** `_runout_tick` starts a runout only when `print_stats.state == "printing"` (`LaneWorld.printing`), not when `idle_timeout` reads `Printing` (true for any G-code), and never while the lane's macro plan changed step within `STAGE_FLOW_FRESH_S` (180 s). While such a flow runs on a `LOADED` lane, the lane's own bay is published `loaded` even if the hub sensor flickers (the unit's tip pull-back); outside a flow the sensor is shown as read.

**ClearErrors resync caveat (EXISTS, H14, 2026-09-28).** `_resync_lane` now sets `following=None` (honest unknown) rather than `False`, so `following` is `null` in the status JSON until the follower state is confirmed. `src/oams_state.py:545-561`.

### 5.2 Runout sub-machine (today)

Runs on every monitor tick (default 1 s, `src/oams_state.py:78`) while `op` is
`LOADED` (`src/oams_state.py:620-621`, `:716-770`):

| From | Condition | To | Effect | Evidence |
|---|---|---|---|---|
| `idle` | printing, and the lane's bay no longer sees filament at the hub | `pausing` | remember the extruder position | `:724-729` |
| `pausing` | the extruder has fed `PAUSE_DISTANCE` (60 mm) more | `coasting` | follower off | `:736-741`, `:69` |
| `coasting` | follower state unknown | (hold) | no automatic reload | `:719-722` |
| `coasting` | unit `path_len` ≤ 0 | op `UNLOADED`, runout `idle` | pause: "ptfe_length is not calibrated (0); cannot auto-load the next spool. Run OAMS_CALIBRATE_PTFE_LENGTH." | `:743-751` |
| `coasting` | consumed + 60 mm + `reload_before_toolhead_distance` > `path_len`, and a group member other than the empty bay is ready | `loading`, `reload_target` set | start the load of that member | `:752-761`; `src/oams_manager.py:32` |
| `coasting` | same, no ready member | op `UNLOADED` | pause: "filament runout on group %s and no spare spool available" | `:762-766` |
| `coasting` | `settings.auto_reload_on_runout` is false (H7), when the reload would start | op `UNLOADED` | message `auto reload off`; pause: "filament runout on group %s (auto reload is off)" | H7 |
| `loading` | SUCCESS | `idle`; lane `unit` becomes `reload_target`; following forward | message `next spool loaded` | `:632-639` |
| `loading` | failure or timeout | op `UNLOADED` | pause: "failed to load next spool (%s)" or "timed out loading next spool" | `:640-651` |

**v2 additions (H1):** `reload_target` is published. `runout_from` is set to the
lane's `unit` on entering `pausing` and kept after a successful reload so a UI can
show "T0 · A1 ran out → B2" (ARCHITECTURE §6.3). **RESOLVED (H2, §5.3 rule 5):** `runout_from` and `reload_target` are kept through success, failure and the `UNLOADED` after a failed reload, and cleared only by the next user-initiated load or unload on that lane or by `OAMSM_CLEAR_ERRORS`. D3's `auto_reload_on_runout = false` would
turn the `coasting` reload into the pause branch (H7, C3 §6).

### 5.3 Stage (H2) — RESOLVED (H2 design, reviewed)

Two different things are called "stage":
- **the operation plan** (user-facing): the steps a toolchange or unload goes
  through, reported by the macros and by the manager's own operations. A UI's
  stepper renders the **published plan** and must not hard-code step lists;
- **the firmware motion stage** (diagnostic): the unit's `motion_stage`
  (C1 §3), published separately as `lanes_by_fps.<l>.motion_stage`. It is
  never a plan step.

**Vocabulary** — the **standard** names every interface knows how to
translate, plus **custom** ones a user adds (a UI maps an unknown name to its
own text or to the name itself):

| Standard name | Meaning (`oams_macros.cfg`) |
|---|---|
| `cut` | the tip retract, the cutter move (`CUT_FILAMENT`, or the user's own `OAMS_CUT` when they define one) and the post-cut extruder retract |
| `retract` | the unload op pulling the filament back through the path, plus the unload sensor checks |
| `feed` | the load op, the inlet check, `CLEAN_NOZZLE`, the extruder reload and the outlet check |
| `purge` | a real purge. No macro purges today, so no plan contains it until one does |
| `calibrate` | a PTFE-length calibration |
| `heat` | reaching extrusion temperature (`_OAMS_HEAT`): the flow sets the extruder to the filament's printing temperature and waits for it |
| `home` | homing the axes (`G28`) |
| `clean` | wiping or brushing the nozzle |

A **custom step** is any other name matching `^[a-z][a-z0-9_]{0,15}$` — a
brushing action, a park move, whatever a user's own macro does. A plan has 1
to 8 steps and no duplicates. A standard name is also a legal custom name, so
the standard set is fixed and open at the same time.

**Plans** (each an ordered subset of the vocabulary, with the **hook steps**
below marked `(h)`):

| Flow | Plan |
|---|---|
| switching groups (`_TX`, something loaded) | `[home,] [(h),] [heat,] cut, (h), retract, [(h),] feed, [(h),]` |
| load into an empty toolhead (`_TX`) | `[home,] [(h),] [heat,] feed, [(h),]` |
| unload (`SAFE_UNLOAD_FILAMENT`, `OPENAMS_UNLOAD`) | `[home,] [(h),] [heat,] cut, (h), retract` |
| a bare `OAMSM_LOAD_FILAMENT` / `OAMSM_UNLOAD_FILAMENT` (no macro) | automatic `feed` / `retract` (rule 2) |
| runout reload | automatic `feed`, created on entering the runout `loading` phase (`pausing` and `coasting` are not steps) |
| calibration | automatic `calibrate` |

**Hook steps.** A user macro named `_OAMS_HOOK_<point>` in the printer's own
files is called by the flows at a step boundary, and OpenAMS ships none of
them. The points, and where each one sits in the plans above:

| Point | Called | A hook with a step lands |
|---|---|---|
| `_OAMS_HOOK_BEFORE_HEAT` | where the heat step is entered, or where it would be | before `heat` |
| `_OAMS_HOOK_AFTER_CUT` | right after the cutter moves, before the extruder retracts and the toolhead sensor check | after `cut` |
| `_OAMS_HOOK_AFTER_RETRACT` | after the retract, before the load (a swap only) | after `retract` |
| `_OAMS_HOOK_AFTER_FEED` | after the load succeeded, before the plan ends | after `feed` |

A hook that declares `variable_step` (a valid step name, a standard one is
legal) gets that step in the plan at the point's own place, announced with
`LABEL_<STEP>` when it declares `variable_label`, and entered
(`OAMSM_STAGE NAME=`) just before the hook runs; without `variable_step` it
runs inside the step around it and adds no step. A point a flow does not pass
is not in that flow's plan, and its hook does not run (there is no
`_OAMS_HOOK_AFTER_RETRACT` in a load into an empty toolhead, and none of the
load-side hooks in an unload). An error in a hook fails its step, by rule 6's
last paragraph like any other G-code error. A plan carries at most 8 steps, so
a hook step that does not fit is left out of the plan (the hook still runs,
without a step of its own) and a `RESPOND` names it. All of this is the
macro's doing: the manager sees only the ordinary `PLAN=`, `NAME=` and
`LABEL_<STEP>` it already accepts.

`home` is in the plan exactly when the axes are not homed. `heat` is in it
when the flow will heat, and whenever it homes: the plan is announced before
any motion, homing runs before heating, and a homing override can change the
heater (a probe calibration with its own target), so `_OAMS_HEAT` decides
again after homing. A flow that homes and then finds the extruder hot enough
skips `heat` (an intermediate step may be skipped). A running print with its
own hot target is never heated, so its plan carries no `heat`.

`_TX` for the group that is already loaded, or during an excluded object,
announces no plan.

**Published** (per lane; `openams_ui` carries it as `lane.stage`, §6.2):
`stage: null | {"plan": [names], "index": int | null, "failed": name | null,
"labels": {name: label}, "detail": string | null}`.
`index` is 0-based into `plan` and monotonic within a plan; `null` means
announced but not started. `failed` names the step that did not complete;
`message` carries the cause. `labels` holds only the steps that have a label
(`{}` when none); `detail` is the current step's own text (rule 6) or `null`.
The automatic plan (the manager's own one-step plan) has `labels: {}` and
`detail: null`.

**Revision note (H1, additive).** `labels` and `detail` are new keys inside
`stage`, and the plan is no longer drawn from a closed list. Both are
additive: `schema_version` stays **2** and the `openams_ui` projection stays
**`v` 1**, whose `lane.stage` grew from `[plan, index, failed]` to
`[plan, index, failed, detail, labels]` — a client reading the first three
keeps working unchanged.

**Rules.**
1. **Information only.** `motion_settled` and `supported_actions` (§5.6) never
   depend on `stage`. A UI may *withhold* an action because of `stage`; it
   never *offers* one because of it.
2. **The manager's own operations.** When the manager starts a firmware load,
   unload or calibration on a lane, it enters `feed` / `retract` / `calibrate`
   in the lane's active plan if present; without a plan it creates the
   one-step plan (automatic), which it ends on success and keeps on failure.
   A synchronous refusal (no operation started: lane busy, hub occupied, a
   load guard) never creates a plan; with an active plan it marks the
   current step failed. **A macro plan whose `index` is still `null`
   (announced, never entered) or that the step being entered would move
   backwards through is stale — it has ended; the automatic plan is created
   in its place** (this is what protects a later
   op from a strict macro raise that never sent `FAILED=1`). **A runout
   reload always creates the automatic plan, replacing any macro plan.**
3. **Failure attribution.** A firmware operation that fails (refused, error,
   timeout, cancel) sets `failed` to the current step; so does a macro's
   `OAMSM_STAGE FAILED=1`. The first failure wins until a new plan. A
   completion, timeout or cancel never ends a macro-announced plan: the
   macro continues it or fails it. `failed` means "the step did not
   complete", not "the hardware malfunctioned".
4. **Clearing.** `DONE`, a new `PLAN`, `OAMSM_CLEAR_ERRORS`, and a Klipper
   restart or shutdown clear `stage` (it is not persisted). A plan with
   `failed = null` and no update for 10 minutes while the lane is
   `motion_settled` is dropped (a guard against aborted macros). A failed
   plan is never dropped by time.
5. **`runout_from` / `reload_target`** (§5.2): `runout_from` is set to the
   loaded bay on entering `pausing` (overwriting an earlier value);
   `reload_target` on entering `loading`. Both are kept through success,
   failure and the `UNLOADED` that follows a failed reload, and are cleared
   only by the next user-initiated load or unload on that lane or by
   `OAMSM_CLEAR_ERRORS`, never by time.
6. **Labels, details and attribution.** A plan may carry a display **label**
   per step (`LABEL_<STEP>`, C3 §4): it is trimmed, at most 47 bytes of UTF-8
   (cut on a code point boundary, never mid-character), and an empty one is
   the same as none. A label for a step the plan does not carry is reported
   and dropped, and the plan is still accepted. A label may also be given for
   a standard name, where it overrides the interface's own label; a custom
   step with no label is valid, and the interface shows its name. A step
   **detail** (`DETAIL` on `NAME=`) is trimmed and at most 24 bytes of UTF-8
   (cut on a code point boundary); it belongs to the step it was given with,
   so entering any later step clears it — including the manager's own entry
   (`_stage_step`), which brings none. **A G-code command that raises** fails
   the current step of the running macro plan: the manager listens for
   Klipper's `gcode:command_error` and dispatches the same failure as
   `FAILED=1`, but only when *exactly one* lane has a macro plan that has
   started and not failed; with none or several it does nothing, because
   guessing would fail the wrong one. Without it a strict macro raise left the
   progress bar on the step until rule 4's 10-minute stale-plan drop.
   **Stop.** The user's Stop (`openams/stop`, C3 §4) stops the whole sequence:
   the macros raise `Stopped by the user` at the next step boundary or inside
   the heat wait, and a firmware load or unload in flight is cancelled. When
   the stop takes effect, the current step is `failed` (whatever other lanes
   are running, which the lone-plan rule above does not constrain), the lane's
   `message` is `Stopped by the user`, and heat the sequence raised is put back
   on an idle printer.

The writer command is `OAMSM_STAGE` (C3 §4); it never raises.

### 5.4 Calibration (D7)

D7 makes PTFE-length calibration a prerequisite for any load and requires the
state per unit and per bay.

**Today** the only signal is the path length: `path_length_mm` is 0 until
`OAMS_CALIBRATE_PTFE_LENGTH` writes a value (`src/oams.py:122-126`, `:296-300`,
`:2266-2282`), and only the runout coasting branch refuses on it
(`src/oams_state.py:744-751`). Loads are not refused (C3 §6).

**v2 derivation** (grounded in the two config levels the driver already has: a
shared `ptfe_length` and optional per-bay `ptfe_length_<n>`,
`src/oams.py:302-313`):

| Field | `configured` | `calibrating` | `uncalibrated` |
|---|---|---|---|
| `bays[i].calibration_state` | effective length (`ptfe_length_<i>`, else `ptfe_length`) > 0 | a PTFE calibration of this bay is in flight | otherwise |
| `calibration.state` (unit) | shared `ptfe_length` > 0 | any PTFE calibration on this unit is in flight | otherwise |

`calibrated` is `calibration_state == "configured"`. The lane state does not record
which kind of calibration is running (the kind rides only on the `StartCalibrate`
effect, `src/oams_state.py:274-278`, `:557-561`), so H1 must keep it to tell a
PTFE calibration from a hub-HES one. **RESOLVED (review, C2 16):
this derivation is accepted as an H1 proposal.**
`examples/c2/oams_manager_uncalibrated.json` shows all three states: a fresh
unit whose bay 0 has an override (configured), bays 1-2 uncalibrated with load
withheld and calibrate offered, and a second lane re-calibrating a bay.

**Enforcement (H10).** D7 enforcement uses
three layers, each with a distinct job:

- **Reducer** (policy, the user-facing refusal): `LaneWorld.path_len` keyed
  by `(oams_idx, bay)`; `_load_guard` refuses `Load` and `LoadBay` for a bay
  whose `calibration_state` is not `configured` (message `<unit> Spool <n> is
  not calibrated; run OAMS_CALIBRATE_PTFE_LENGTH SPOOL=<bay index>`); `_group_candidates`
  skips uncalibrated bays; `_runout_tick` checks `reload_target` before
  `_begin_op`.
- **Driver** (mechanism): `start_load_spool` raises `command_error` if the
  per-bay length is `None`, 0, or outside `[PTFE_MIN, PTFE_MAX]` clicks; it
  always sends the length before every load and never sends 0.
- **Firmware** (admission, ARCHITECTURE principle 5): a load the firmware
  cannot serve because no host-programmed path length is active is reported
  as an `Uncalibrated` result (**EXISTS, H10/F9, 2026-09-28:** host enforces via `src/oams_state.py:585-639`; firmware via op code 8).

The firmware's built-in default path length is never used for a
host-initiated load (H10 and F9 are landed).

### 5.5 `link_source` (D2)

`tag`: linked from a tag read by `openams_spoolman`. `manual`: assigned by a user
(the display's Assign spool picker, C4). `none`: no link. The host owns and
persists the value with the link (ARCHITECTURE §4); `OAMSM_SET_BAY_SPOOL` carries
the source explicitly (`SOURCE=tag|manual`, C3 §4, resolved). **A link made from
the Spoolman web UI (RESOLVED, review, C2 17): not in v1** — it
would need a component that watches Spoolman for changes, which does not exist
(C4 §9 item 3). `link_source` stays exactly `tag` / `manual` / `none`; a
web-UI-made link is future work, not a fourth value.

### 5.6 `supported_actions`

**Per unit (EXISTS-v1)** is the list `_supported_actions` builds from the
resolved firmware commands and capabilities (`src/oams.py:358-406`):
`clear_errors` always; `load`, `unload`, `follower` with normal motion and the
command resolved; `configure_pid` on AMS 1 only; `calibrate_ptfe` and
`calibrate_hub_hes` per capability; `dryer_start` (with `heater_control`),
`dryer_stop`, `clear_fault`, `rfid_scan` (a unit that reads its own tags on
request). It describes what the unit can do, not
what is allowed now. A klipper-mmu unit publishes its own four on top of that
list — `preload`, `unpreload`, `eject` and `home` (C3 §2, C5 §7.1) — and an
AMS unit publishes none of them.

**Motion-safety rule (corrected by the maintainer, 2026-09-28).** A UI never infers
stopped motion from `op` alone. Common conditions for every motion action (`load`, `unload`, `calibrate_ptfe`):
- no `stop`- or `pause`-severity fault is latched on the lane or unit (the host latches these; `info` items such as a failed tag read do not block motion);
- no bay of the lane's units is `positioning`;
- `devices.<unit>.follower_uncertain` is `false`;
- the unit is reachable (§4.4 `connected`);
- the printer is not printing;
- the lane's `following` is not `null` (unknown).

Then per action:
- **`load` / `calibrate_ptfe`** also need `op` `UNLOADED` with runout `idle`, and `following` exactly `false`;
- **`unload`** also needs `op` `LOADED` with runout `idle`. `following` may be `true`: an armed follower on an idle, loaded lane is the normal state, and the host's unload sequence stops it first.

The host enforces the same predicates (`_load_guard` for load, the unload path for unload). The list is the contract; the UI adds nothing.

Cases where `op` alone misleads:
- while printing, a `LOADED` lane with `following=true` is in motion (the follower feeds the extruder);
- `UNLOADED` after a host `Timeout` may have a failed `CancelLoad`/`SetFollower(0)` with `following=null`;
- a frozen lane under a `pause`-severity fault withholds every action even though `op` is not transitioning.

The `motion_settled` lane field (§4.2) is the published form of the common conditions, plus `op` ∈ {`UNLOADED`, `LOADED`} and runout `idle`. A UI offers `load` when `motion_settled` and `op == UNLOADED` and `following == false`, and offers `unload` when `motion_settled` and `op == LOADED`.

**Per bay (NEW-v2, H1)** is what the bay allows now, so a UI can gray out
correctly. **RESOLVED (review, C2 16): accepted as
an H1 proposal**, as the examples apply it, including that a `stop` fault on
the unit withholds every motion action (ARCHITECTURE design principle 5):

| Action | Offered when |
|---|---|
| `load` | the unit offers `load`; no `stop` fault on the unit; the lane is `UNLOADED` with no reload in flight; the bay is ready, not loaded, and `configured` (D7); `bays[i].positioning` is `false` |
| `unload` | the unit offers `unload`; no `stop` fault; the lane is `LOADED` from this bay with runout `idle`; `bays[i].positioning` is `false` |
| `calibrate_ptfe` | the unit offers `calibrate_ptfe`; no `stop` fault; the lane is `UNLOADED` (the path is shared); the bay is ready and not calibrating; `bays[i].positioning` is `false` |
| `set_spool` | the bay is ready (a spool is present) |
| `clear_spool` | the bay has a link |
| `preload`, `unpreload`, `eject` | the unit is a klipper-mmu unit that can perform them (§4.4.1). These are its own operations, not OpenAMS reducer ops: they do not move the lane's OpenAMS state, so the `op`/`motion_settled` rules above do not gate them, and the unit's own admission (a latched fault, a busy path, a read-only unit) refuses a request it cannot serve — the same answer `KLIPPER_MMU_PRELOAD` gives (C3 §2). An AMS unit never lists them |

**No motion while any bay of the unit is positioning (2026-09-27).** The firmware reply `Busy` applies to the entire unit while
any bay's `Positioning` sequence runs (the motion stage is unit-wide, not
per-bay). A UI must suppress load, unload, and calibrate_ptfe on every bay
of the unit while `bays[i].positioning` is `true` for any `i`.

**Motion unavailable (EXISTS, H15/H15b, 2026-09-28).** A unit that publishes
`AMS_CAP_F1S_NORMAL_MOTION=0` has `normal_motion=False` in `capabilities`.
While `normal_motion=False`, the unit does not offer `load`, `unload`, or
`calibrate_ptfe` in `supported_actions`, and every bay's `supported_actions`
omits them. The reason text for any attempt is
`"This firmware does not support normal filament motion"`
(`src/ams_family.py` `motion_block_reason`).

`rfid_scan` stays unit-level: neither `OAMS_RFID_SCAN` nor `OAMSM_RFID_READ` takes
a bay (C3 §3, §5).

### 5.7 Faults (ARCHITECTURE §8.1)

**Sources today, none of them in the status as faults:**
- firmware faults latched by the reducer, `SystemState.firmware_faults`
  (`src/oams_state.py:118-119`, `:324-362`): not published;
- controller fault names in telemetry: `telemetry.domain.{motion,dryer,system}_fault_name`
  and `telemetry.dryer.fault_name` (`src/oams.py:1316-1368`, `:1428`);
- lane `message` values (§5.1) and the reducer's pause reasons, which today reach
  only the console (`M118`, `src/oams_runtime.py:482-489`); H1 must keep the reason
  so `text` can carry it;
- reader results: OpenAMS 1 `last_read_status` `ERROR` with `error` text
  (`src/mfrc522.py:409`, `:454-460`); a unit that reads its own tags:
  `telemetry.rfid.result_name` (`src/oams_tags.py`).

**Severity** (host-assigned, following §8.1's categories). **RESOLVED
(review, C2 18): the table below is accepted as H1's proposal,
with the evidence already cited per code.** In particular, every
dryer-safety fault — heater, thermistor, and fan-stall alike — is `stop` for
the dryer, confirming `fan_stalled`'s placement below rather than leaving it
as a question.

| Severity | §8.1 category | Codes |
|---|---|---|
| `stop` | heater, motor, vent faults | `invalid_heater_thermistor`, `invalid_motor_thermistor`, `heater_overtemperature`, `motor_overtemperature`, `chamber_overtemperature`, `heater_drive_fault`, `motor_drive_fault`, `vent_actuator_fault`, `fan_stalled` (dryer fan; every dryer-safety fault is `stop`) |
| `pause` | runout without spare, feed failure | lane: `no spare spool`, `auto reload off`, `reload failed`, `load failed`, `ptfe_length uncalibrated`; firmware: `motion_timeout`, `motion_stalled`, `follower_buffer_invalid`, `follower_filament_lost`, `follower_starved`, `follower_stalled`, `follower_timeout`, `lane_obstructed` (names its bay: the hub sensor reads filament without the feeder inlet, most likely a broken piece stuck in the lane), `rfid_tag_error` (classified 2026-10-07: the unit rejected a spool's RFID tag and keeps reporting it; text starts `RFID Tag Error`) |
| `info` | tag read failures | OpenAMS 1 reader `ERROR`; a unit that reads its own tags: `read_failed` |
| `info` (classified 2026-10-05) | a dryer start that never ran | `dryer_start_refused`: the unit acknowledged the start and kept reporting off (an AMS 2 Pro with filament loaded). The heater never ran, so it never withholds motion; `clear_fault` clears it. |
| `stop` (classified 2026-09-28, H9 review) | power, drive and session faults | `supply_undervoltage`, `supply_overvoltage`, `first_stage_overcurrent`, `ac_sync_lost` (heater control), `host_communication_lost` (the session is gone: recovery is `FIRMWARE_RESTART`, so this entry offers no action) |
| `pause` (classified 2026-09-28) | a failed lane op | lane `unload failed`, `calibration failed`, `spare spool is not calibrated` |
| `stop` (reclassified 2026-09-28, review) | dryer faults that stop the dryer | `supply_monitor_invalid`, `environment_sensor_invalid` (the firmware raises both only as dryer faults: heater off, needs `clear_fault`); `invalid_request` is a latched motion / emergency-stop system fault. None of the three is informational. **Any name published in `dryer_fault_name` is `stop`** — a dryer fault is never `info`. `invalid_request` as a request result (a command's own reply code) is not a fault and is not published here. |
| `stop` (default) | any fault name not listed above, including `unknown` | a new or unrecognized fault withholds motion until someone looks at it |

**Klipper-mmu units (C5 §7.6, EXIST 2026-09-29).** A klipper-mmu unit
publishes none of the names above: its wire is a different enumeration
(`km_fault`, C5 §3.6) and the manager latches the raw value from
`accept_firmware_fault`. The host therefore does not classify a klipper-mmu
fault by name at all — the unit answers for itself through
`firmware_fault_info(value, physical_lane)`
(`klipper_mmu/host/openams_fault_projection.py`) and the manager publishes that
entry verbatim. Every code is `mmu_`-prefixed so a client can tell the two
vocabularies apart, and no wire number is ever published. The `text` is a
user-facing sentence within the 48 characters §6.2 allows.

| `km_fault` (C5 §3.6) | `code` | Severity | `act` | `text` |
|---|---|---|---|---|
| `HOST_COMM_LOST` (1) | `mmu_host_comm_lost` | `stop` | none — recovery is `FIRMWARE_RESTART` | Host link lost; restart the firmware |
| `DRIVER_OVERCURRENT` (2) | `mmu_driver_overcurrent` | `stop` | none (see the note below) | Overcurrent; remove the cause, then clear |
| `DRIVER_FAULT` (3) | `mmu_driver_fault` | `stop` | none (see the note below) | Driver fault; remove the cause, then clear |
| `NO_PROGRESS` (4) | `mmu_no_progress` | `pause` | `retry_load` | No progress; retry the load |
| `JAM` (5) | `mmu_jam` | `pause` | `retry_load` | Jammed; clear the path, then retry |
| `BUFFER_SIGNAL` (6) | `mmu_buffer_signal` | `pause` | `clear_fault` | Follower signal lost; clear the fault |
| `FILAMENT_LOST` (7) | `mmu_filament_lost` | `pause` | `retry_load` | Filament lost; retry the load |
| `STARVED` (8) | `mmu_starved` | `pause` | `retry_load` | Follower starved; retry the load |
| `STALLED` (9) | `mmu_stalled` | `pause` | `retry_load` | Follower stalled; retry the load |
| `EMERGENCY_STOP` (10) | `mmu_emergency_stop` | `stop` | `clear_fault` | Emergency stop; clear to resume |
| `SENSOR_FAILURE` (11) | `mmu_sensor_failure` | `stop` | `clear_fault` | Sensor failure; check it, then clear |
| `CONTROL_TIMING` (12) | `mmu_control_timing` | `stop` | `clear_fault` | Control timing lost; clear to resume |
| `SELECTOR` (13) | `mmu_selector` | `stop` | none (see the note below) | Check the path, then clear |
| a value outside `km_fault` | `mmu_unknown` | `stop` | none | Unknown MMU fault; check the unit |

**Note, three corrections to C5 §7.6's token column (2026-09-29).** The
severities are C5 §7.6's unchanged. The tokens are not, because the host can
only offer a token that really clears or resumes the unit:

1. **`clear_errors` is never offered on a klipper-mmu unit.** That unit's
   `clear_errors` raises; the token would clear nothing.
2. **`clear_fault` is withheld on a fault whose physical state a person must
   settle first** — `DRIVER_OVERCURRENT`, `DRIVER_FAULT` and `SELECTOR`. A
   lane latch survives every recovery except reconciliation, and those three
   need `KLIPPER_MMU_RESTART_DRIVER` (after the cause is removed) or
   `KLIPPER_MMU_RECONCILE_PATH ACTION=ADOPT LANE=n` first. Their `text` says
   what to do instead of offering a button.
3. **`clear_fault` on a klipper-mmu unit routes to that unit's fault
   acknowledgement**, not to an AMS HT control command: the unit registers
   `OAMS_CLEAR_FAULT OAMS=<idx>` for its own index, so the UIs keep sending
   the one command §5.7 already names. It settles the retained event; the
   firmware fault and the path stay, and only reconciliation clears them.

**Actions** (tokens → commands, §8.1; the display renders only these, never its own):

| Token | Command | Typical faults |
|---|---|---|
| `clear_fault` | `OAMS_CLEAR_FAULT OAMS=<idx>` — the AMS HT control command on an AMS unit, a klipper-mmu unit's fault acknowledgement on a klipper-mmu one (see the note above) | firmware `stop` faults |
| `clear_errors` | `OAMSM_CLEAR_ERRORS` | after a failed load or unload |
| `retry_load` | the lane's load again (`OAMSM_LOAD_FILAMENT GROUP=<group>`, or `OAMS_LOAD_SPOOL` for a bay) | `load failed`, `reload failed` |
| `resume` | `RESUME` | a paused print once the cause is fixed |

### 5.8 Settings

See §4.9. The component reads `auto_create_spoolman_spools`,
`apply_pa_on_load` and `sync_unit_tray_info` from this object (C4); the host
reads the other three.

### 5.9 Environment and dryer mapping

- **AMS 2 Pro / AMS HT:** `environment.temp_c` = `telemetry.dryer.chamber_c`;
  `rh_pct` from firmware RH (F3 landed) — **EXISTS (F3, 2026-09-28):** `src/oams.py:540-547` publishes `{temp_c, rh_pct, source: "firmware"}`.
  **RESOLVED (v1):** `source` is `"firmware"` for a reading the unit itself reports (`ams_get_environment` and the dryer chamber temperature), as built (`src/oams.py:548`); the earlier `"dryer_chamber"` proposal is withdrawn. `source` only names the sensor; no client logic depends on it.
- **OpenAMS 1 mainboard:** the `hdc1080 <name>` object (`temperature`, `humidity`,
  `src/hdc1080.py:284-288`); nothing ties a
  sensor to a unit today; **RESOLVED (review, C2 10):** each
  unit's config section names its environment sensor (H1). `source` names
  the sensor (`"hdc1080"` in the
  examples). **EXISTS (2026-09-30):** the `[oams NAME]` option
  `environment_sensor` does this; without it the sole HDC1080 object is used.
  This fallback is taken only by a unit whose firmware lacks
  `ams_get_environment` (the STM32F072 board, C1 §1.4).
- **Dryer:** §4.7.

## 6. `openams_ui <unit>` — the compact projection (H5)

### 6.1 Object and subscription

One Klipper object per unit, named `openams_ui <unit>` (as `oams <name>` is)
— RESOLVED (review, C2 6): accepted. The display subscribes to its bound unit's object only (plus
`print_stats` and the `openams_spoolman` status, ARCHITECTURE §6.1) and finds the
unit by `serial` (D4). It is a view: every value is a pure function of the
`oams_manager` v2 status; it holds no state of its own (ARCHITECTURE §7.3). The
examples were produced by exactly such a function.

### 6.2 Keys

| Key | Type | From `oams_manager` v2 |
|---|---|---|
| `v` | 1 | projection version |
| `unit`, `family` | string | the unit's name; `devices.<u>.family` |
| `idx` | int | `topology.oams.<u>.idx`: what `OAMS_*` commands take as `OAMS=` (added 2026-09-28, D5: the display sends them) |
| `serial` | string or null | `devices.<u>.identity.serial` |
| `online`, `ready` | bool | `devices.<u>.connected`; `ready` and the unit may move filament and has no `stop` fault (§6.2a) |
| `cal` | `c` configured, `u` uncalibrated, `p` calibrating | `devices.<u>.calibration.state` |
| `lane.id`, `.op`, `.group`, `.runout` | | the unit's lane (`topology.oams.<u>.lane`) and its `lanes_by_fps` entry |
| `lane.ms` | bool | `lanes_by_fps.<l>.motion_settled` (§4.2): the host's motion-safety predicate, so a UI never re-derives it (added 2026-09-28, Join 2 review: the display was guessing it) |
| `lane.ext` | string | **RESOLVED (review, C2 19):** the lane's extruder name (`lanes_by_fps.<l>.extruder`) — the display gap analysis's "which side" (`side`, `display:src/model/mmu_model.h:109`) reads off this on a dual-extruder printer |
| `lane.bay`, `.from`, `.to` | `"<unit>-<bay>"` or null | `lanes_by_fps.<l>.unit`, `.runout_from`, `.reload_target` |
| `lane.stage` | `[plan, index, failed, detail, labels]` or null | `lanes_by_fps.<l>.stage` (§5.3): `plan` is the list of step names (standard or custom). The last two elements are appended (H1, additive): `detail` is the current step's text or null, `labels` maps a step name to the display label it was given (`{}` when none) |
| `bays[i].id` | int | the bay's global slot id (§4.10, UI API v1 `units[].slots[].id`): what `OPENAMS_LOAD GROUP=<g> SLOT=<id>` takes (added 2026-09-28, D5) |
| `bays[i].s` | `e` empty, `r` ready, `l` loaded, `x` error, `p` positioning | `loaded`, else `positioning` (H16: the firmware positions an inserted spool on its own), else `ready`, else empty; `x` while a `stop` or `pause` fault names the bay |
| `bays[i].cal` | `c`, `u`, `p` | `bays[i].calibration_state` |
| `bays[i].a` | letters in this order: `l` load, `u` unload, `c` calibrate_ptfe, `a` assign (set_spool), `d` detach (clear_spool), then `p` preload, `q` unpreload, `e` eject | `bays[i].supported_actions`. The last three are a klipper-mmu unit's own operations (§5.6, C3 §2, C5 §7.1) and are appended after the five, which keep their order and their meaning; an AMS unit never offers them |
| `bays[i].sp` | `{id, src}` or null | **RESOLVED (review, C2 4/7): the link only** — `spool_id`, `link_source` (`t`/`m`); `null` when `link_source` is `none`. A `tag` link whose bay `tag` record is a unit's stored setting (`format: unit_summary`, no UID: an AMS 2 Pro tray summary) goes out as `m`, so no display shows it as an RFID read (2026-10-05). Material/color/vendor/remaining do **not** live here any more; they are in the C4 `openams_spoolman` status (`bays.<unit>-<bay>.spool`), which the display merges by `id` |
| `groups[]` | `{n, b?, s?}` | every group on the unit's lane, and every empty group (no bays, so on no lane yet: any lane may fill it) by name only; members `b` (`"<unit>-<bay>"`, declared order) and one state letter per member `s` (`l`, `r`, `e`, `p`) only for groups holding one of this unit's bays. This is what the Groups screen and its "Add to group" sheet need (`display:docs/GROUPS.md`) |
| `env` | `{t, rh}` or null | `environment.temp_c`, `rh_pct`, rounded to integers (the display model is integral, `display:src/model/mmu_model.h:118-121`) |
| `dryer` | `{state, target, min, heat, fan, fault, adapter}` or null | `dryer`, with the target rounded and the remaining time in whole minutes, rounded up (`display:src/model/mmu_model.h:123-124`) |
| `fault` | `{sev, code, text, unit, bay, act}` or null | the most severe fault on the unit's lane (`stop` > `pause` > `info`, then first listed); `text` cut to 48 characters. The display model has one active error (`display:src/model/mmu_model.h:193-201`) |
| `nf` | int | how many faults the lane has, including the one shown |
| `act` | unit-level actions: `dryer_start`, `dryer_stop`, `clear_fault`, `rfid_scan`, and a klipper-mmu unit's `home` | `devices.<u>.supported_actions`; `load`, `unload`, and `calibrate_ptfe` travel per bay; engineering actions are dropped; `clear_errors` is always available (`src/oams.py:364`) and not listed. `home` (C3 §2 `OAMS_HOME`) is offered only where the family's selector kind has a selector to move (§4.4.1), so the `claymore_4` example carries it and `boxturtle_8` does not |

### 6.2a Capability-limited units

A unit whose firmware reports a capability unavailable (C1 §7) is a normal
`openams_ui` unit; the projection says honestly what it can do, so a UI needs
no capability logic of its own:

| Firmware reports | `openams_ui` shows |
|---|---|
| normal motion unavailable (`AMS_CAP_F1S_NORMAL_MOTION=0`) | `ready` false; no `l`, `u` or `c` in any `bays[].a`; bay states are only `e`/`r`/`x` (never `p` or `l`); `OAMSM_LOAD_FILAMENT`, `OAMSM_VALIDATE_LOAD`, `OAMS_LOAD_SPOOL`, `OAMS_UNLOAD_SPOOL` and `OAMS_CALIBRATE_PTFE_LENGTH` are refused at once with the reason (C3) |
| PTFE calibration unavailable | no `c` in `bays[].a`; load and unload are unaffected |
| no dryer (`AMS_DRYER=0`; always for an AMS 1) | `dryer` is `null`; `act` has no `dryer_start`/`dryer_stop`; the commands are refused with `"This unit has no dryer"` |
| dryer present | `dryer` always carries all seven keys (`state`, `target`, `min`, `heat`, `fan`, `fault`, `adapter`), `"unknown"`/`"none"` before the first telemetry; `dryer_start` is listed only while a start is possible (heater control, adapter connected) |
| no RFID reader and no tray reads (`tray_read`) | no `rfid_scan` in `act`; `OAMS_RFID_SCAN` is refused |
| host sensor configuration / runtime PID tuning unavailable | nothing: `config_oams_*` and `oams_cmd_pid` are no-ops (C1 §4.10), and the PID actions are not published |

`bays[].a` always carries `a` for a bay holding a spool and `d` for a bay with a
spool link, whatever the unit can do: they are host-side links (C4).

A unit that needs host arrival confirmation (C1 §7.2) on a lane with no arrival
signal is online and ready, but offers no `l` on its bays and carries an `info`
fault `arrival_signal_missing` naming the cause.

A unit whose firmware reports its relayed device's link down (C1 §7.2
`AMS_CAP_LINK_STATUS`) is `online: false` at once; no published field changes,
only `connected` is driven earlier than the stats-silence rule would.

`ready` is per unit: the manager is ready, the unit is online, it may move
filament, and no `stop` fault is on it. `online` is the unit's reachability
(§4.4 `connected`): a unit whose frames stopped is `online: false` with no
fault and no actions while it has no work on its lane; only when it holds,
loads, unloads, calibrates or is the reload target of a lane does it also
raise the `unit_unreachable` `stop` fault.

### 6.3 Rate and coalescing

- **≤ 2 Hz.** Klipper polls `get_status()` every 0.25 s (§2). The object returns a
  cached snapshot and rebuilds it at most every 0.5 s, only when the source status
  changed; a burst of changes inside one window collapses into one rebuild.
- **No churn from noise.** Temperatures and humidity are integers, remaining
  drying time is in minutes, pressure is not carried. A sensor wobbling inside
  one unit of rounding produces no update.
- **Field grouping.** A change resends its whole top-level field (§2): a bay change
  resends `bays`, a stage step resends `lane`. Keep fast-changing values out of
  large fields.
- **Terminal states are never coalesced away.** The rebuild after a window must
  reflect the latest state, so a short `LOADING` → `LOADED` still ends at `LOADED`.
  An intermediate stage may be skipped; the display shows progress from status
  only (ARCHITECTURE §6.2).

### 6.4 Size budget

The requirement is ≤ 1 KB per unit. **RESOLVED (review, C2 4/7
and 7): moving the spool summary to C4 (§6.2) settles the budget question —
recomputed below.** Measured minified sizes of the examples (regenerated
with `docs/contracts/tools/gen_examples.py` after the summary was removed):

| Scenario | Bound unit | Bytes |
|---|---|---|
| idle | AMS 1 | 640 |
| positioning | AMS 1 | 635 |
| loading | AMS 1 | 676 |
| loaded | AMS 1 | 638 |
| runout_reload | AMS 1 | 671 |
| error | AMS HT | 750 |
| drying | AMS 2 Pro | 778 |
| uncalibrated | AMS 1 | 572 |
| multi_unit | AMS 2 Pro | 802 |
| remote_unit | AMS 1 | 687 |
| **boxturtle_8** | **BoxTurtle, 8 lanes** | **956** |
| claymore_4 | Claymore, 4 lanes (indexed selector) | 643 |

`boxturtle_8` is the widest single unit the projection has to carry: an
8-lane klipper-mmu device (§4.4.1) on a lane with three groups, seven of its
bays holding a spool and bay 2 calibrating. Its 8 `bays[]` entries cost about
464 bytes — 24 of them the three klipper-mmu letters every bay of it carries
— so eight lanes is the practical ceiling for a 1 KB projection — a
12-lane unit would not fit. The contract still validates up to 16 (§4.3,
§4.4.1); the budget, not the schema, is the binding limit.

Three rules keep these under the limit, each grounded in a consumer: one fault
plus a count (the display has one active error); no per-bay group tag (it follows
from `groups[].b`); no `clear_errors` in `act` (always present).

**Recomputed worst case: 992 bytes, now under budget.** The same scenario the
draft used to find 1263 bytes (an AMS 2 Pro with four linked Bambu Lab
spools, the dryer running, a runout reload in flight with a stage, two
faults, five groups on the lane) — rebuilt with `bays[i].sp` trimmed to
`{id, src}` only — measures 992 bytes, 32 bytes of headroom. A pathological
case with many more groups sharing the lane (each contributing a name-only
`{"n": "..."}` entry) can still push past 1 KB; §9 keeps that as a residual
note, not a work item, since ARCHITECTURE's group-count expectations do not
suggest double-digit groups on one lane in practice.

### 6.5 What the display model needs

`display:src/model/mmu_model.h` against the projection:

| Display model | From |
|---|---|
| unit `name`, `variant`, `slot_count`, `connected` (`:104-110`) | `unit`, `family`, `bays` length, `online` |
| capabilities `has_humidity_pct`, `has_temp`, `can_dry`, `rfid` (`:112-116`) | `env`, `dryer`, `act` |
| `humidity_pct`, `temp_c` (`:119-120`) | `env.rh`, `env.t` |
| `dry_state`, `dry_target_c`, `dry_remaining_min`, `power_adapter` (`:122-126`) | `dryer.state`, `.target`, `.min`, `.adapter` |
| slot `state`, `info_source` (`:80-86`) | `bays[i].s`, `bays[i].sp.src` |
| slot `color`, `material`, `brand`, `remaining_pct` (`:80-86`) | **RESOLVED (review, C2 4/7): no longer from `openams_ui`.** The display reads these from the C4 `openams_spoolman` status (`bays.<unit>-<bay>.spool.{color_hex,material,vendor,remaining_pct}`) by the same `id` `bays[i].sp.id` gives, merging the two objects (C4 §8). The edit form (C4 §9) also prefills from `remaining_g`, `initial_g` and `flow_k` |
| groups, `current_group` (`:95-102`, `:165-169`) | `groups`, `lane.group` |
| `loaded_unit`, `loaded_slot`, `busy`, `busy_unit`, `busy_slot` (`:172-181`) | `lane.op`, `lane.bay` (a bay on another unit is "busy elsewhere") |
| `steps[]`, `step_current`, `step_failed` (`:182-185`) | `lane.stage` |
| active error `error_code`, `error_text`, `error_primary`, `error_secondary` (`:193-201`) | `fault.code`, `.text`, `.act` |
| `runout_from_unit/slot`, `runout_to_unit/slot` (`:214-217`) | `lane.from`, `lane.to` |
| identity `serial`, `firmware` (`:345-350`) | `serial`; firmware from `oams_manager` on demand |

**Display gaps — RESOLVED (review, C2 19), each disposed of
separately:**

| Gap | Resolution |
|---|---|
| `humidity_level` (the five-step index) | the display derives it from `env.rh` itself; not a host field |
| `dry_rotate` | H6 (no rotation status exists in `dryer` until H6 adds it) |
| `side` (lane → extruder side) | `lane.ext` (§6.2, new), added specifically for this |
| `nozzle_min_c`/`nozzle_max_c` and `k_value_x1000` | from Spoolman: either the C4 summary (if H4/C4 extend it) or the display's own Spoolman-proxy fetch by `bays[i].sp.id` |
| the full step list (`steps[]` needs every planned name; `stage` gives only the current one) | H2 publishes the planned stage names when an op starts (§5.3) |
| `latest` firmware | out of scope: an update check is not part of this contract |

## 7. What HelixScreen reads

**RESOLVED (review, global): HelixScreen needs no change.** Every
field it reads keeps its shape in v2 — including `lanes`, now that the array
stays at that key and the per-FPS object moved to `lanes_by_fps` (§3, §4.2/4.2a).

HelixScreen subscribes to `oams_manager` fields `api_version`, `schema`, `ready`,
`commands`, `lanes`, `units`, `groups` (`helix:src/printer/ams_backend_openams.cpp:120-127`):

| Read | Where (`helix:src/printer/ams_backend_openams.cpp`) | In v2 |
|---|---|---|
| `api_version == 1`, `schema == "openams.manager"` | `include/openams_api.h:23-32`, `:207` | §4.1, unchanged |
| `ready` | `:358` | EXISTS-v1 |
| `commands.load/unload/cancel/reset` (one G-code word each) | `:217-231`, `:62-66` | UI-API-v1 |
| `units[].id`, `.name`, `.connected`, `.topology` | `:249-265` | UI-API-v1 |
| `units[].slots[].id`, `.bay`, `.loaded`, `.ready` | `:267-288` | UI-API-v1 |
| `groups[].name`, `.lane`, `.slots[]` | `:295-321` | UI-API-v1 |
| `lanes[].state`, `.current_slot`, `.current_group` (array) | `:327-346`, `:434-447` | **not blocked (RESOLVED): `lanes` stays the array, §4.2a** |

It sends `<commands.load> GROUP=<group> SLOT=<slot id>` (`:583-584`),
`<commands.unload>` (refused while more than one lane is loaded, `:703-707`),
`<commands.cancel>` (only while a lane reports `loading` and no load of its own is
pending, `:782-801`), and `<commands.reset>` (`:763-776`). C3 lists these commands.

## 8. Webhooks (legacy, request/response)

| Endpoint | This line (`src/oams_manager.py`) | Legacy line (`legacy:src/oams_manager.py`) |
|---|---|---|
| `openams/status` | `{status: {openams: {ready, units (count), fps: {<lane>: {value, extruder}}, lanes: {…§4.2 EXISTS keys}, filament_groups: {<g>: {lane, bays: ["<unit>-<bay>"]}}, devices}}}` (`:413-430`) | `{status: {openams: {ready, current_group, units (count), fps_value, filament_groups: {<g>: {bays (count), spools: ["oams<idx>-<bay>"]}}, api: <the UI API v1 snapshot>}}}` (`:78-91`) |
| `openams/topology` | `{topology: <§4.3>}` (`:432-434`) | absent |
| `openams/cancel_load` | cancels every `LOADING` lane: `{result: "cancel requested"}` or `{result: "no load in progress"}` (`:436-445`) | same results (`:71-76`); Moonraker does not proxy it (`legacy:docs/UI_API.md:188-190`) |

The two `openams/status` shapes collide: `filament_groups.<g>.bays` is a list
of strings here and a count there; the legacy `current_group`, `fps_value`,
`spools`, and `api` keys are absent here. **RESOLVED (review, C2
2): the webhook keeps the legacy shape, plus its `api` snapshot** — P0.3b's
implementation — not this line's current shape. The v2 `oams_manager` object
does not depend on this webhook; the design keeps it only for legacy clients
(ARCHITECTURE §5).

## 9. Open questions

The design review resolved most of
this document's open questions; each is marked **RESOLVED** in place, next to
the field or behavior it settles (§3, §4.1/4.2/4.2a, §4.5, §4.7, §4.9, §5.4,
§5.5, §5.6, §5.7, §5.9, §6.2, §6.4, §6.5, §7, §8). What remains genuinely open:

1. **Stage details** (§5.3): **RESOLVED (H2 design)**: vocabulary, plans, a 0-based `index`, and the clearing rules are in §5.3.
2. **OpenAMS 1 reader-to-bay mapping** (§2): the two MFRC522 endpoints
   (`RFID_CARD` 0/1) are not tied to bays anywhere
   (`src/mfrc522.py:295-307`). **OPEN, a hardware fact: which bays each reader covers.**
3. **AMS 2 Pro / HT tag decode:** **RESOLVED:** a unit that reads its own
   tags reports a decoded summary per bay (C1 §4.7.1), published as a
   `unit_summary` tag (§4.6).
4. **`uncertain_follower_units` publication:** **RESOLVED (C2 11):** published
   as `devices.<unit>.follower_uncertain` (§4.4).
5. **Humidity and adapter:** **RESOLVED (C2 13): accepted as written** — TO
   ADD (F3, F5); not further decided here, just confirmed as the correct
   scope.

Everything else the earlier draft listed as open (the `lanes` collision, the
`openams/status` webhook collision, setting names, spool-summary transport,
`openams_ui` naming, the size budget, AMS 1 environment source, dryer state
names, slot-id stability, the calibration/supported_actions derivations,
Spoolman-web-UI links, and fault severities) is resolved in place above —
see the section references at the top of this list.
