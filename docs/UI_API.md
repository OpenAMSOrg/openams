# OpenAMS UI API v1

OpenAMS exposes a small, versioned status and command contract for native user
interfaces. It is intentionally independent of AFC, a particular OpenAMS
controller family, and the manager's internal state-machine implementation.

## Upgrading existing installations

An ordinary repository update followed by a Klipper restart while the printer
is idle keeps existing installations working. The API is implemented entirely
inside the already-installed `oams_manager.py`: it adds no Python dependencies,
extra-module symlinks, mandatory configuration, or MCU firmware requirements.
Rerunning the installer is **not required for this change**.

Existing `T0`/`T1`/`_TX GROUP=...` macros, automatic runout handling, and
untargeted `OAMSM_LOAD_FILAMENT` / `OAMSM_UNLOAD_FILAMENT` keep their existing
behavior, including informational replies on hardware failure. Strict error
handling is opt-in: a load with `SLOT=...`, or an unload with `STRICT=1`, raises
a G-code error on failure so the new UI sequence cannot continue after it.

To enable **HelixScreen** load/unload controls, manually merge the updated
sections from `oams_macros.cfg` into your printer's customized macro file:
`OPENAMS_LOAD`, `OPENAMS_UNLOAD`, `_TX`, `SAFE_UNLOAD_FILAMENT`, `CUT_FILAMENT`,
`_LOAD_FS_IN`, `_LOAD_FS_OUT`, `_UNLOAD_FS_OUT`, and every `_OAMS_...` helper
macro they call. Preserve your calibrated variables and printer-specific
cutter, parking, and nozzle-cleaning changes. The updated macros also change
untargeted toolchanges (`T0`...) and `SAFE_UNLOAD_FILAMENT`: on a failed load,
failed unload, enabled sensor failure, or a pause arriving mid-toolchange they
turn the follower off, pause, and raise an error, which ends an SD print. UI
(`STRICT`) requests raise without pausing. See
[Changes from the previous oams_macros.cfg](guide/macro-changes.md#changes-from-the-previous-oams_macroscfg)
for the complete list. Restart Klipper after reviewing the merged macros. The
installer deliberately does not replace an existing macro file, even when rerun.

Until those macros are present, status remains available but `commands` omits
the unavailable load/unload operations. Clients must disable an operation if
its advertised command is missing, and only that operation: HelixScreen keeps
showing status and offering the advertised operations, and refuses load (and
tool changes) or unload until the matching command appears.

HelixScreen implements this API natively from v1.1.0-beta.1
([prestonbrown/helixscreen#1691](https://github.com/prestonbrown/helixscreen/pull/1691)).
Its `HELIX_MOCK_AMS=openams` test mode, proposed in
[prestonbrown/helixscreen#1753](https://github.com/prestonbrown/helixscreen/pull/1753),
runs HelixScreen's real OpenAMS backend against a mock `oams_manager`. The API
does not add a KlipperScreen plugin or an AFC dependency.

## Discovery and subscription

`oams_manager` in `printer.objects.list` is not enough to identify this API:
a manager from before it registers the same object and publishes only
`current_group`. A client therefore queries `oams_manager` for `api_version`
and `schema` and treats OpenAMS as absent unless both are supported. It then
subscribes to the `oams_manager` object.

AFC drives OpenAMS hardware through its own objects and does not use
`oams_manager`, so a leftover `[oams_manager]` section can coexist with a
working AFC setup. A client that supports both must leave the printer to AFC
whenever the `AFC` object is present. HelixScreen does.

The status object has this shape:

```json
{
  "api_version": 1,
  "schema": "openams.manager",
  "ready": true,
  "commands": {
    "load": "OAMSM_LOAD_TO_TOOLHEAD",
    "unload": "OAMSM_UNLOAD_FROM_TOOLHEAD",
    "cancel": "OAMSM_LOAD_FILAMENT_CANCEL",
    "reset": "OAMSM_CLEAR_ERRORS"
  },
  "lanes": [
    {
      "id": "fps",
      "state": "loaded",
      "current_group": "T0",
      "current_slot": 0,
      "following": false,
      "direction": 0,
      "message": null,
      "pressure": 0.48,
      "set_point": 0.5
    }
  ],
  "units": [
    {
      "id": "1",
      "name": "unit1",
      "kind": "oams",
      "topology": "hub",
      "lane": "fps",
      "connected": true,
      "slots": [
        {"id": 0, "bay": 0, "ready": true, "loaded": true}
      ]
    }
  ],
  "groups": [
    {"name": "T0", "lane": "fps", "slots": [0]}
  ],
  "current_group": "T0"
}
```

`current_group` remains at the top level for compatibility with existing
macros. New clients should otherwise consume only the versioned fields above.

The legacy `openams/status` webhook remains available. Its original fields are
unchanged and the v1 snapshot is available below its `api` key, complete and
with `lanes` as the array shown above.

On the unified host, `oams_manager` additionally publishes `lanes_by_fps`: the
same status keyed by FPS name instead of indexed by array position (e.g.
`lanes_by_fps.fps.current_slot`), with every field `lanes` has plus the
pre-existing, non-versioned fields the manager already reported per lane
(`op`, `group`, `unit`, `runout`, `since`, `message`). This is additive and
outside the UI API v1 contract: a client should use `lanes` (this document's
contract); `lanes_by_fps` exists for internal/host-side consumers (the ported
`oams_macros.cfg` toolchange macros use `lanes_by_fps.fps.current_slot`, since
Klipper G-code macros address a known lane by name more naturally than by
array position).

## Model

- A **lane** is one independently operated filament path/FPS. Current master
  publishes one lane named `fps`. `pressure` is the lane's FPS reading, from
  0.0 (no pressure) to 1.0 (fully compressed); the FPS measures compression
  only, never tension. It is rounded to two decimals and republished only
  once it moves by 0.02, so sensor noise does not resend the lanes on every
  poll. `set_point` is the compression the feeding unit's hub motor regulates
  to (its `fps_target`), or `null` when no unit reports one.
  `lanes_by_fps.<l>.pressure_band` is `[low, high]`, the band that same
  unit's regulator keeps the pressure in (its `fps_lower_threshold` and
  `fps_upper_threshold`), or `null` when no unit reports a valid one. It is
  informational: a reading outside the band is not a fault by itself.
  A lane whose follower state is unknown (`following` null, e.g. right after
  a Klipper restart) is not `motion_settled`; the `oams_manager` status lane
  carries the additive `motion_blocked_reason` string explaining why (C2
  §4.2). The manager re-establishes the forward follower itself once the lane
  is idle and healthy; a refusal first leaves the lane stopped (`following`
  false, `motion_settled` true) and is retried a bounded number of times. A
  failed unload/load flow leaves its follower stopped, the lane as it is. A
  loaded bay is not shown unloaded for a hub-sensor flicker while a flow runs
  on its lane.
- A **unit** is a physical feeder attached to one lane. `kind` is a stable
  machine identifier; `topology` is the hardware-neutral rendering/operation
  shape (`hub`, `linear`, `parallel`, or `mixed`). Clients must branch on
  `topology`, not on a family name. A client that meets a `topology` value it
  does not know may draw that unit as a hub: slots are addressed by id, so
  operations are unaffected. A unit contains one or more slots.
- A **slot** has a manager-assigned integer `id` that is unique within the
  snapshot. Commands address this id; clients must not derive command
  addresses from unit names or array positions.
- A **group** names the slots eligible to satisfy a tool/material request and
  belongs to one lane.

This nesting is the forward-compatibility boundary for multi-FPS and
multi-family support. A future manager may publish several lanes and mix unit
kinds without changing the v1 meanings. Existing fields are additive within
v1; a meaning change requires a new `api_version`.

### Showing the FPS

`pressure` is a one-sided reading. Draw it as a gauge from 0.0 (no pressure)
to 1.0 (fully compressed), with `set_point` marked when it is not `null`. Do
not present it as a two-sided tension/compression meter, and do not convert it
into Happy Hare's `sync_feedback_bias`: the FPS has a single spring, so a
reading below `set_point` is less compression, not tension.

The hub motor keeps the pressure near `set_point` while the extruder pulls, so
movement around the target is normal regulation, not a fault. Describe it
relative to the target ("at", "above" or "below target") rather than as tight
or loose. The readings worth flagging sit at the rails: pressure held near full
compression means filament is not moving into the toolhead (a jam or a clog
downstream), and pressure near zero while the lane is loaded means the feed is
not engaging. Where those warnings start is the client's choice.

`lanes_by_fps.<l>.pressure_band` may be drawn as the regulator's band, but it
is outside v1 and is not a fault boundary.

Everything the gauge needs is already in v1: `pressure`, `set_point` and the
lane's `state`.

## Commands

Clients must use the command names advertised in `commands` rather than
hard-coding lower-level device commands.

### Unified UI command contract

Every OpenAMS plugin accepts the same arguments for the same actions, so a UI
runs the same calls against any of them:

- Load: run `commands.load` with `GROUP=<group> SLOT=<slot id>`, where the slot
  id is a global `units[].slots[].id` (the same ids `groups[].slots` lists).
- Unload: run `commands.unload` with `FPS=<lane id>`, where the lane id is a
  `lanes[].id`.

The advertised command names differ by plugin:

| Plugin | `commands.load` | `commands.unload` |
|--------|-----------------|-------------------|
| `openams` (unified host) | `OAMSM_LOAD_TO_TOOLHEAD` | `OAMSM_UNLOAD_FROM_TOOLHEAD` |
| `klipper_openams` | `OPENAMS_LOAD` | `OPENAMS_UNLOAD` |

A UI must follow the map and never hard-code these names. `cancel` and `reset`
are advertised as `OAMSM_LOAD_FILAMENT_CANCEL` and `OAMSM_CLEAR_ERRORS` by both.

### Load a selected slot

```gcode
OAMSM_LOAD_TO_TOOLHEAD GROUP=T0 SLOT=0
```

To serve a tool or material, a client picks the slot from the group: the
member already loaded if there is one, otherwise a member whose slot reports
`ready`. HelixScreen does not send a load for a group with no ready member.

`OAMSM_LOAD_TO_TOOLHEAD` runs `OAMSM_LOAD_FILAMENT` for the group (and the
exact slot when `SLOT` is given), then the configured toolhead load macro once
the filament has settled at the toolhead. `OAMSM_VALIDATE_LOAD` rejects a
malformed, unknown, unavailable, or incorrectly grouped slot before any
motion. If the toolhead macro fails, the AMS load is not unwound: the filament
stays loaded with its forward follower armed. Temperatures, cutter positions,
and motion distances remain the responsibility of the printer's configured
macros. The `OPENAMS_LOAD` toolchange macro, when merged from
`oams_macros.cfg`, is not advertised.

### Unload

```gcode
OAMSM_UNLOAD_FROM_TOOLHEAD FPS=fps
```

`OAMSM_UNLOAD_FROM_TOOLHEAD` reverse-follows while the configured toolhead
unload macro retracts the filament, then rewinds the AMS with
`OAMSM_UNLOAD_FILAMENT`. `FPS` selects the lane and is optional with a single
lane. When the manager reports no loaded slot, this command does not move the
printer.

### Cancel or reset

```gcode
OAMSM_LOAD_FILAMENT_CANCEL
OAMSM_CLEAR_ERRORS
```

Cancel requests cancellation of an active hardware load. It does not promise
that already queued physical toolhead moves have stopped. Reset clears OpenAMS
errors and re-determines state.

`OAMSM_LOAD_FILAMENT_CANCEL` is ordinary G-code, so it runs only once Klipper's
G-code queue is free. It can reach a load the manager runs on its own, such as
a runout reload. It cannot interrupt a load started with the advertised load
command, because the load command holds the queue until the load completes or
fails; by then there is nothing left to cancel. Clients must not offer cancel
for a load they started. HelixScreen refuses it as busy and leaves its own
record of the load in place until the command returns. The Klippy-side
`openams/cancel_load` webhook can interrupt a load, but Moonraker does not
proxy it to clients.

## Cortex Bridge firmware

On a printer with a Cortex Bridge, the `openams_bridge` Moonraker component
(installed with the bridge relay) lets a UI show the bridge and install its
firmware. It is separate from `oams_manager`: a client discovers it by calling
the status method and treating a "method not found" error as "no bridge".

`server.openams.bridge.status` (HTTP `GET /server/openams/bridge/status`):

```json
{
  "relay": {
    "state": "running",
    "detail": "1 AMS online",
    "bridge": {"serial": "CB0123456789AB", "image": "production"},
    "units": [{"uid": "0a1b2c3d4e5f", "online": true,
               "path": "/dev/oams/0a1b2c3d4e5f"}],
    "update": null
  },
  "bundled": [{"version": 3, "released": "2026-11-01",
               "notes": "...", "ok": true, "problem": ""}],
  "latest": {"version": 3, "released": "2026-11-01", "notes": "...",
             "ok": true, "problem": ""},
  "update_running": false,
  "last_result": null
}
```

`relay` is `null` when the relay is not running. `relay.state` is one of
`starting`, `waiting`, `connecting`, `running`, `updating` or `stopped`, and
`relay.reason` names why it is waiting (`no-bridge`, `bridge-in-update-mode`,
`bridge-fault`, `no-roster`, ...). `latest` is `null` when no package is
bundled. The installed firmware version is not reported yet.

`server.openams.bridge.update` (HTTP `POST /server/openams/bridge/update`,
optional `version`) installs a bundled package, the newest when `version` is
absent, and returns `{"started": true, "version": 3}` at once. It answers 409
while printing, paused or already updating, 404 when no such package is
bundled, and 503 when the relay is not running. Progress arrives as the
`notify_openams_bridge_update` notification, one object per step:

```json
{"event": "accepted", "version": 3}
{"event": "progress", "phase": "writing", "done": 41, "total": 100}
{"event": "result", "ok": true, "version": 3}
```

`phase` is `entering`, `waiting-receiver`, `authenticating`, `writing`,
`restarting` or `done`. A failed result has `ok: false` with `error` (for
example `UpdateModeRefused` when the AMS is busy) and a `message` to show as it
is. After a successful update the component waits for the AMS to come back
and sends `FIRMWARE_RESTART`; a client needs to do nothing more. Confirm with
the user before calling update: Klipper loses the AMS for about a minute.

## Compatibility rules

Clients must fail closed when `api_version` is absent or unsupported: they
treat OpenAMS as absent rather than reporting an error. They must ignore
unknown fields and check each command's availability separately. While `ready`
is false, clients show status but send no command. A client may display all
lanes, but it must not silently combine independent lanes into a single
current-slot/action value. With several lanes loaded, an unload sent without
`FPS` cannot say which lane it empties, so clients always send `FPS`.

Current master has a single FPS and therefore one active lane. The same API is
designed for the future manager as follows:

- publish one lane entry per FPS;
- attach every unit to its owning lane;
- keep slot ids unique for the snapshot;
- attach each group to exactly one lane;
- route a targeted load by slot id through that lane's runtime;
- use the same advertised high-level commands so a UI never depends on a
  reducer class or hardware-family-specific G-code.

## Regression verification

The regular suite checks the status contract, legacy webhook fields, slot
selection, legacy failure behavior, and importing through an old installation's
individual extras symlinks without installing any new module.

The optional integration suite uses the G-code dispatcher and macro engine from
Klipper commit `57a018ad7bbacde28c35305fd79cb9488bd201db`, with fake hardware.
It compares command/motion traces and final state against klipper_openams master commit
`01de61b6ccf406b8bd17e4ff8a5e1250ec942ac1`, using both unchanged old macros and
the new macros. It also checks validation before motion, within-group slot
changes, error propagation, and sensor failures:

```bash
OPENAMS_KLIPPER_DIR=/path/to/klipper python3 -m pytest -q
```

The checkout must contain the pinned commit. Tests read its committed source
without changing that checkout or accessing a printer. These checks exercise
the real parser/template engine with fake devices; they are not a physical
printer or MCU validation.
