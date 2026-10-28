# mock_moonraker

Serves a recorded or hand-made OpenAMS fixture over Moonraker's WebSocket
JSON-RPC API, so the ESP32 display and its tests can develop against OpenAMS
status without a printer. Every request a client sends is recorded.

```sh
cd tools && python3 -m mock_moonraker --fixture mock_moonraker/fixtures/idle.json \
    --port 7125 --speed 1.0 --gcode-mode simulate --record calls.jsonl
# --speed 0 applies every event on connect; --loop restarts the timeline
# python3 mock_moonraker/build_fixtures.py   # re-derive fixtures/ from the C2 examples
```

The fixture format is a recorded event log:
`{"scenario", "seed", "family_map", "events"}`, where each event is
`{"t", "kind", "unit", "payload"}` and `kind` is one of `openams_ui`,
`component_status`, `c2_status`, `action_log`, `spoolman_call`, `gcode_script`.
Events at `t` are applied `t / speed` seconds after the first client connects.
An optional `"spoolman_responses"` map (`"METHOD path"` keys) answers
`server.spoolman.proxy`; `fixtures/` holds one fixture per C2 scenario.

## Spoolman

`spoolman.py` holds an in-memory Spoolman (the component's own
`api.FakeSpoolman`) loaded with the fixture: the `GET` **lists** in
`spoolman_responses` (`/v1/spool`, `/v1/vendor`, `/v1/filament` — what the
component pre-warms with, C4 §6), then one spool per bay the fixture's
`component_status` links. `server.spoolman.proxy` answers `GET /v1/spool`,
`GET /v1/vendor` (with `?name=`) and `GET /v1/spool/<id>` from it, and falls
back to the fixture's own `spoolman_responses` for anything else, so a
hand-made fixture keeps working. Everything a UI writes shows up in the next
listing.

## `server.openams_spoolman.edit` (C4 §9)

`POST /server/openams_spoolman/edit` validates with the real component's
`edit.parse()` and runs the real `edit.apply()` against that in-memory
Spoolman, so the errors are the component's and the writes are Spoolman's:

```sh
# an unlinked bay that holds a spool: vendor, filament and spool are created
# and the host is sent OAMSM_SET_BAY_SPOOL, which links the bay in openams_ui
{"jsonrpc": "2.0", "id": 1, "method": "server.openams_spoolman.edit",
 "params": {"unit": "oams1", "bay": 2, "material": "ABS", "vendor": "Polymaker",
            "color_hex": "102030", "initial_weight": 500, "remaining_weight": 500}}
-> {"spool_id": 16, "created": true,
    "bay": {"state": "linked", "spool_id": 16, "source": "manual", ...}}
```

A linked bay is patched (a material, color or vendor change re-points the
spool at a matching or new filament, never a shared one). The bay's
`bays.<unit>-<bay>` entry of the component status is rebuilt with
`ops.spool_summary()`, the `openams_ui` bay's `sp` is updated, and both
`notify_openams_spoolman_status` and the `openams_ui` status diff are pushed.
An error is a JSON-RPC error with the component's message and status: an empty
bay is 409 `no spool in oams1 Spool 1`, an unknown unit or bay 404, a bad value
400 (`flow_k must be between 0 and 2`).

## `--taglib <path>`

Serves the `openams_taglib` component's routes over plain HTTP, from a local
checkout of the Bambu Lab RFID library, so the display's tag writer can be
developed without a printer. The routes are the component's own
(`/server/openams/taglib/` `status`, `materials`, `colors`, `pick`, `cloned`,
`add`, `update`), answered by the component's `library.py`; the database is in
memory, and nothing is cloned (a missing `<path>` reports state `missing`;
`update` re-reads the checkout). Results come wrapped as Moonraker does, `200`
with `{"result": ...}`, and an error as its HTTP status with
`{"error": {"code": 404, "message": "..."}}`:

```sh
cd tools && python3 -m mock_moonraker --fixture mock_moonraker/fixtures/idle.json \
    --host 0.0.0.0 --port 7130 --taglib ~/Bambu-Lab-RFID-Library
curl 'http://localhost:7130/server/openams/taglib/colors?material=ABS'
```

## `--gcode-mode`

| Mode | `printer.gcode.script` |
|---|---|
| `ok` (default) | every script answers `"ok"` |
| `refuse` | `OPENAMS_LOAD …`, `OPENAMS_UNLOAD` and `T<n>` answer a JSON-RPC error, code 400, as the macros refuse before any motion on a configuration they cannot satisfy: *OpenAMS cannot heat extruder: minimum_extrude_temperature 260.0 is above the temperature ceiling 240.0. Check minimum_extrude_temperature, max_temp and max_operation_temperature*. (A cold extruder is no longer refused: the macros heat it.) |
| `simulate` | `OPENAMS_LOAD GROUP=<g> SLOT=<n>` and `OPENAMS_UNLOAD` answer `"ok"`, then play about 3 s on that slot's unit lane: `op` `LOADING`/`UNLOADING`, `stage` through the C2 §5.3 plan names, then `LOADED`/`UNLOADED` with `group` and `bay` set, pushing the status diffs |

Every script is recorded in all three modes.
