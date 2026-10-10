# Host connection observation

This slice is a source and library implementation for independent review. It is not a hardware qualification, and it does not claim that an installed printer observes or accepts a Klipper MMU.

## Native extra layout

Klipper loads a prefixed extra from `klippy/extras/<module>/__init__.py`. For this add-on, that module is `klipper_mmu`, and the entry points are `load_config_prefix(config)` and `KlipperMMU`. A section uses this shape:

```
[klipper_mmu NAME]
mcu: mcu
diagnostic_startup: False
```

`NAME` is the `MMU` mux value for the observer's explicit read commands, including `KLIPPER_MMU_QUERY`, `KLIPPER_MMU_SNAPSHOT`, `KLIPPER_MMU_SENSORS`, `KLIPPER_MMU_TERMINAL`, and `KLIPPER_MMU_STATS`. `mcu` selects the printer MCU object (`mcu`, or a secondary MCU name resolved with `get_printer_mcu`). The default is `mcu`. `diagnostic_startup` is optional and defaults to false, so the observer stays read-only. Native MCU and menu configuration remains the firmware configuration system. Only an explicit true registers one public MCU config callback from the constructor after `get_printer_mcu`. File output returns from that callback with no command. Otherwise the constants must be integer `KLIPPER_MMU_PROTOCOL_VERSION` 1, integer `KLIPPER_MMU_INTEGRATION_VERSION` 1, and integer `KLIPPER_MMU_DIAGNOSTIC_STARTUP` 1. Boolean true is not that gate, and `KLIPPER_MMU_ACTUATION` does not grant it. Only then the callback adds `km_diagnostic_start` with `is_init=True`. A missing or malformed gate raises `config_error` before that init command.

Klipper finalizes those config callbacks, resolves the config, restart, and init lists (including aliases and reserved pins), sends full configuration plus init or a CRC-matched restart plus init, then confirms with get_config. This extra relies on that public order. The callback does not mean the board is ready. Firmware waits for its clock and verified identity, initializes its own board, runs its own 1 ms startup and deadline (default total timeout 1 s), and attaches diagnostic commands with start left disabled. A duplicate init while waiting, ready, or faulted is a firmware no-op. Analog startup additionally requires an opt-in measured calibration object. There is no host realtime loop. Setting `diagnostic_startup` false on a later host restart does not undo an MCU that already latched this init, and the flag does not shut outputs down. Reset and reconciliation are separate. This is not a hardware qualification.

A disposable Klipper checkout can link this directory with the separate `install-host`, `check-host`, and `remove-host` actions in `tools/install.py` (see [install](../docs/install.md)). That action creates only `klippy/extras/klipper_mmu` as a symlink to `host` and does not install MCU hooks or driver APIs. The ordinary `install` action still installs only MCU source hooks. The observer and opt-in OpenAMS unit have native and synthetic Klipper tests; installed-printer observation and hardware qualification remain pending. Nothing in this document is an instruction to edit an installed printer or run motion.

## One observer

Each MCU has one `klipper_mmu` observer. Construction resolves the MCU before registering `klippy:connect`, then refuses a second object whose `get_mcu()` is that same MCU. That keeps a single transport subscription for the MCU.

The object registers `klippy:connect`, `klippy:disconnect`, and `klippy:shutdown`, plus the mux commands `KLIPPER_MMU_QUERY`, `KLIPPER_MMU_SNAPSHOT`, `KLIPPER_MMU_SENSORS`, `KLIPPER_MMU_TERMINAL`, `KLIPPER_MMU_EVENT`, and `KLIPPER_MMU_STATS`. With `diagnostic_startup` true it also registers the MCU config callback described above. Connect, gcode, snapshot, and error paths do not send `km_diagnostic_start`. The default observer uses no timer, poll loop, background retry, direct serial object, private connection helper, or monkey patch. The separately enabled OpenAMS command unit schedules a bounded reactor lease/event service. Reactor services come from `printer.get_reactor()`. Snapshot, sensor, terminal, and event responses use public `register_serial_response` only for the duration of one explicit command and are unregistered before that command returns. Their transmits use public `lookup_command`. Neither wrapper is cached on the object.

## Generation and challenge

Every connect, disconnect, and shutdown increments a local generation and drops the observation, including any cached snapshot. Instance, last-query, and snapshot values from an older generation are never left current. The same invalidation drops the cached transport query wrapper and the cached diagnostic-state query wrapper. Both wrappers are connection-scoped: the next normal connection calls public `lookup_query_command` again and encodes with the message dictionary identified for that connection. Observed `startup_state` and `startup_result` clear with the transport observation. The configured `diagnostic_startup` preference stays. There is no direct serial lookup and no private message-dictionary helper. Invalidation does not release the MCU mutex held by an in-flight exchange and does not reset the snapshot query high-water counter.

The per-object challenge counter is separate from that generation. Its initial value is `secrets.randbelow(0xffffffff)` (0 through 0xfffffffe). The counter advances before each challenge query and is never reset. Values are nonzero: 0xffffffff is followed by 1, so a long-lived object never runs out. A value repeats only after 2^32 - 1 further queries. The mutex below admits one exchange at a time and Klipper delivers replies in order, so the next exchange that receives any reply consumes or rejects every older reply. A reply from before a wrap could therefore match only after 2^32 - 1 consecutive exchanges had received no reply at all. The exact echo and generation checks below still apply to every reply.

The challenge is captured with the generation before `send`. `send` may yield to the reactor. A reply is stored only when `decode_transport_state` accepts it, the echoed challenge is exactly the captured value, and the generation is still that connected generation. When `diagnostic_startup` is true, that same challenge is sent once more as `km_diagnostic_query challenge=%u` through public `lookup_query_command`, under the same mutex. Neither observation is published unless `km_diagnostic_state challenge=%u state=%c result=%i` echoes it and the generation still matches. That query does not send `km_diagnostic_start` and does not wait until ready. A wrong, malformed, or stale reply does not replace current state. The in-flight call's error path does not clear a newer generation and does not replace a wrapper installed for a newer connection. The exchange holding the mutex releases it only after it unwinds, even when the connection generation changed while `send` was waiting; a connect or query that waited then observes the new generation. That release does not automatically retry, establish firmware identity, or authorize motion. This challenge is a host response token, not a controller boot identity.

When that reply says `faulted`, the same challenge goes out once more as `km_diagnostic_detail_query` (skipped on firmware whose dictionary lacks it), and `KLIPPER_MMU_QUERY` and `KLIPPER_MMU_DIAGNOSE` turn the reply into a plain-language finding: the startup step, the lane, the real cause and any offending pins by name, with what to check. A faulted startup never attaches, so DIAGNOSE explains it instead of refusing for want of a connection.

Every MCU exchange runs under one Klipper `reactor.mutex()` owned by the observer (`_mcu_lock`). A concurrent caller (gcode command, connect, OpenAMS command, lease or event service, physical refresh) waits for it, bounded by the holder's own deadlines, instead of failing. Each entry point takes the mutex once for its whole transaction: `KLIPPER_MMU_SNAPSHOT` holds it across its fresh observation and snapshot, and session preparation, lease renewal and recovery hold it from their fresh capture through their heartbeat, peek or ACK. The mutex is released only while OpenAMS runtime code runs inside an event transaction (the terminal consumer and the firmware-fault reducer), because that code may issue a follower STOP or PAUSE that needs the MCU; the event-transaction latch stays set, and the transaction re-acquires the mutex before its ACK. Retired-waiter recovery keeps the mutex across the runtime's `accept_retired_terminal`, which never issues a follower command. Disconnect and shutdown never wait for the mutex. Response registrations are removed before the mutex is released, including on error, timeout, and when disconnect or reconnect changed the generation during the wait. Invalidation does not retire or cancel an outstanding transport wrapper request; clearing the cached reference only forces the next connection to look up again. Public query wrappers share one response name, so overlapping sends are unsafe. There is no extra retry loop; the existing transport query wrapper's bounded retry is the only retry. Snapshot part commands are not retried by this extra.

## What a connection does

File-output and debug mode does not look up or send `km_transport_query`, `km_diagnostic_query`, or `km_snapshot_part`, and the opt-in callback does not add `km_diagnostic_start`. It does not treat a dummy response as a live MCU. Status is `file_output`, `connected` false, and instance, last query, startup observations, and snapshot are null. That record is not acceptance evidence. `KLIPPER_MMU_SNAPSHOT` in that state, or while the observer is inactive or disconnected, sends nothing and raises `command_error`.

A normal connect reads the public MCU constants before any query. `KLIPPER_MMU_PROTOCOL_VERSION` and `KLIPPER_MMU_INTEGRATION_VERSION` must both be integer 1. `KLIPPER_MMU_ACTUATION` must be an integer 0 or 1 (`type(value) is int` and `value in (0, 1)`). A missing constant, a boolean, a string (including `'0'`), and any other integer are invalid. Otherwise connect stores that reason in `last_error` and raises `config_error` the same way an unsupported version does, and sends no query. Connect then compares the dictionary's `km_*` enumerations with `host/wire.py`: a missing or different value leaves the MMU `unavailable` and inactive with the difference in `last_error`, sends nothing and lets printer startup finish; a dictionary with none of them is a protocol 1 image built before the export and uses the host's values ([wire enumerations](../docs/protocol-contract.md#wire-enumerations)). Integer 0 or 1 is preserved as `firmware_actuation` metadata only. `motion_available` is always false, including when a later snapshot reports `motor_ready`, when actuation is 1, or when diagnostic startup reports `ready`. Startup `ready` means that one-shot diagnostic attach finished. Transport `attached` is not startup `ready`. This observer does not claim motion support.

Every message format the host uses is written once, in `host/wire.py`. The transport lookup formats are `km_transport_query challenge=%u` and `km_transport_state challenge=%u instance_hi=%u instance_lo=%u last_query=%u state=%c`. Lookup goes through public `lookup_query_command` after the constants check, once per connection, because that constructor captures the encoder from the current identified dictionary. The transport query is sent once on a successful normal connect and again only from `KLIPPER_MMU_QUERY` or from the fresh observation that begins an explicit snapshot or terminal query. With `diagnostic_startup` true, each of those also sends one `km_diagnostic_query` after a valid transport reply. The default false path never looks up or sends that query. Connect does not send a snapshot. `km_diagnostic_start` is only the opt-in MCU init command, not a connect or gcode action.

Observed binding values are `unbound` (0), `attached` (1), `shutdown` (2), and `clock_unavailable` (3). `attached` does not mean a motor is ready. Opt-in startup state is separate: `idle` (0) and `waiting` (1) require result `-3`, `ready` (2) requires result `0`, and `faulted` (3) requires a known negative result from `-1` through `-10` except `-3`. Challenge, state, and result must satisfy `type(value) is int` (booleans fail), and the challenge must equal the captured uint32. A faulted reply is stored with the transport observation. A missing or inconsistent startup reply fails that whole observation with `command_error` and publishes neither half. Instance 0 and last query 0 are valid while unbound. The instance is the 64-bit value in the reply, with no high-bit mask and no invented identity. The host does not establish firmware identity, attach the core, negotiate a session, acknowledge events, or resume after reconnect.

The optional H723 diagnostic path attaches after its one-shot boot-identity provider succeeds. Its hardware qualification and OpenAMS admission remain pending. Normal connect and gcode paths do not send event ACKs, heartbeat or motion commands.

## Explicit HOST session preparation

The installed `KlipperMMU.prepare_cold_host_session(expected_bays)` method is an explicit entry point used by the opt-in OpenAMS unit during its first physical refresh; it is not a gcode command. It requires the opt-in actuation image (`KLIPPER_MMU_ACTUATION=1`), opt-in diagnostic startup observed as `ready` with result 0, an attached same-instance connection, and a fresh coherent status/sensor capture. All configured required sensors must prove an empty path. The snapshot must show no active operation, retained event, pending terminal, motion, fault, or occupied path. The method uses the existing cold-session helper to choose a new HOST session and confirm one exact heartbeat reply. It returns fresh request and event-delivery helpers plus the capture query only on success; the observer's `motion_available` field remains false.

The connection generation and instance are checked before and after the heartbeat. A disconnect or reconnect invalidates a prepared session on the extra. Once a heartbeat send may have occurred, timeout, rejection, or connection change latches this extra against another preparation attempt until explicit reconciliation. Physical preflight failure sends no heartbeat and leaves the method retryable. The caller must hold the MCU mutex around the helper's exchanges and settle any abandoned OpenAMS waiters before using a returned helper. The ordinary OpenAMS unit remains read-only. `openams_motion: True` additionally requires `openams_host_watchdog_ms` and `openams_lease_max_age_ms` (positive integers with maximum age below watchdog), and installs one guarded command unit. That unit starts physical-only, then confirms the HOST lease, binds the existing command bridges and retained event cycle, and schedules one bounded reactor service at a time. It advertises motion only while the exact session, diagnostic READY, a fresh fault-free board capture, and the confirmed unexpired lease all agree. A failed service retains the old ACK and waiters and stops scheduling. Direct fault clearing and error LED control are still unavailable.

For a completed lane-local PRELOAD after a host restart,
`prepare_preloaded_host_session(expected_bays)` requires a fresh quiescent
`PRELOADED` capture, one physically identified parked lane with its LOAD switch
asserted, and clear shared-hub and toolhead switches. It chooses a new HOST
session and confirms one exact heartbeat. The managed unit binds that session
only if the newly captured lane still matches its local reservation; a lease
must then be confirmed before commands are available. It cannot resolve old
waiters or ACKs. A virtual-hub PRELOAD ends with LOAD clear, and a profile
without LOAD switches cannot supply this proof; both remain unavailable for
restart reattachment. A same-process disconnect uses the separate retained
HOST recovery path below.

`KLIPPER_MMU_RECOVER_SESSION MMU=NAME` exposes the managed unit's
`reconcile_clean_host_recovery(expected_bays)` for an explicit, same-process
release of a disconnected HOST session with no pending ACK or request waiter.
The old session must be retired: by a newer connection generation to the
same MCU instance, or on the same connection by the fault that revoked its
lease, which leaves it disconnected. It also requires diagnostic READY, the
exact old HOST session counter, and a fresh coherent status/sensor capture
proving an empty path, one quiescent, physically identified lane-local
PRELOAD with its LOAD switch asserted and the shared path clear, or one
quiescent LOADED lane with its BAY and the shared path present, as
`KLIPPER_MMU_RECONCILE_PATH ACTION=ADOPT` leaves it. It sends no heartbeat
and grants no motion. The managed unit's next refresh prepares a cold,
preloaded or occupied session from its own fresh capture and confirms its
heartbeat. Pending ACKs or waiters, a changed instance or session, or an
unknown physical path keep recovery latched.

`retry_retained_host_ack()` is a separate, explicit ACK-only recovery for an already accepted terminal after a lost lease. It requires an eligible retained event, a newer same-instance physical capture with the old HOST session, an exact fresh queue-head match, and a successful ACK reply. The managed unit's `retry_retired_ack()` also clears its own retired ACK only after that observer proof succeeds. Failure leaves both records unchanged. Success leaves the old HOST session ambiguous and any other request waiters pending; it neither heartbeats nor authorizes motion.

`KLIPPER_MMU_EJECT MMU=NAME BAY=N` starts a distinct full firmware EJECT after UNLOAD has cleared the shared path. The command is available only with `openams_motion`; it requires a current physical snapshot and motion permission. With no `BAY`, it reports the pending or retained result. `CANCEL=1` cancels only the exact pending EJECT request. A retained EJECT terminal (events 16–18) and its exact ACK settle the result; an UNLOAD terminal cannot do so. EJECT creates no OpenAMS waiter or spool mapping. If the HOST connection retires, `KLIPPER_MMU_RECOVER_LOCAL` reconciles its exact queue head and ACK before motion can be reenabled.

`KLIPPER_MMU_STATS MMU=NAME` reads process-local counts of successful, cancelled and failed LOAD, UNLOAD, CALIBRATE, PRELOAD and EJECT terminals by lane. A terminal contributes only after its exact retained event receives a confirmed ACK; an explicit ACK recovery also contributes once. The counts are diagnostic observations in memory, not firmware counters or persisted print history. Reading them sends no MCU command.

`KLIPPER_MMU_SWITCH_DELAY MMU=NAME` explicitly queries the most recent accepted GPIO switch batch's software delay to the clock observation immediately before the controller update. The reply echoes a fresh challenge and the exact MCU instance; the host rejects changed generations, mismatched instances and malformed values; an overlapping query waits for the MCU mutex. `valid=False` means the latest controller cycle had no accepted switch batch. `accepted_since_attach` is a saturated count and `max_delay_us` is the maximum accepted interval since controller attachment; both remain historical through later invalid cycles and shutdown. The query does not sample GPIO, change the status snapshot, or start a timer. It does not measure physical switch-edge latency.

The managed unit's explicit `retry_retained_fault_ack()` handles a diagnosed `FAULT_ENTERED` or `FOLLOW_FAULT` only when a fresh capture proves the unchanged fault, the exact queue head is still retained, the OpenAMS runtime has the separately installed strict fault hook, and the MCU confirms the ACK. The hook refuses an active or uncertain follower. Firmware disarms the follower on every fault, so when OpenAMS still records the lane as following, the host first reports the proven stop through `accept_verified_follower`. A physical lane without a mapped OpenAMS FPS remains retained. This call does not clear the firmware fault or old HOST session, grant motion, or resolve unrelated request waiters. The diagnostic peek itself still never ACKs.

With `openams_motion: True`, `KLIPPER_MMU_ACK_FAULT MMU=NAME` explicitly invokes that same recovery. It reports success only after the MCU confirms the ACK. A refusal or timeout retains the event for a later exact retry; the command does not clear the firmware fault or HOST recovery latch.

`retry_retained_fault_ack()` keeps the exact `(lane, identity)` it accepted into OpenAMS. A later `KLIPPER_MMU_RECONCILE_PATH` that clears a stopped firmware fault (see [path reconciliation](../docs/path-reconciliation.md#from-a-stopped-fault)) reuses that pair against a fresh post-reconcile capture proving the firmware left FAULT, and calls the runtime's `accept_firmware_fault_cleared(lane, identity)` when the OpenAMS source provides it, removing the OpenAMS latch only on an exact identity match. The hook is optional: an older OpenAMS keeps the latch, and the reconcile response says to restart Klipper before using that lane.

## Explicit snapshot

`KLIPPER_MMU_SNAPSHOT` is manual. There is no snapshot timer, connect-time snapshot, or automatic retry.

The command first repeats the bounded transport observation, including the one opt-in startup query when that preference is true. It then requires this generation to be connected, `state == attached`, and a nonzero full-width instance. `unbound`, `shutdown`, and `clock_unavailable` raise `command_error` and do not send `km_snapshot_part`. The transport observation from that attempt remains the cached diagnostic.

Each object keeps a snapshot query high-water counter, initially 0. It is not cleared on disconnect, reconnect, failure, or MCU identity change. After the fresh observation, the command allocates `max(local counter, firmware last_query) + 1`, requires the result to fit in `1..0xffffffff`, stores it as the new high-water, and only then may send. Exhaustion raises `command_error` before a snapshot send and does not wrap. A new object still starts from the firmware high-water observed on that attempt, so it does not reuse a query this firmware has already issued. Generation, instance, and the allocated query are captured together.

The command registers the five `km_status_*` formats and `km_command_result instance_hi=%u instance_lo=%u command=%c query=%u result=%c` through `register_serial_response` before any snapshot send. It looks up `km_snapshot_part instance_hi=%u instance_lo=%u query=%u part=%c` through `lookup_command` and sends parts `0` through `4` once each (identity, motion, time, sessions, requests). Those five messages are sequential reads of one retained firmware capture: a newer query id asks the firmware to retain a capture, and the other parts of that same id return that capture. They are not five independent live samples. The host does not reuse a query id in this object, because a repeated id would retransmit the retained capture rather than a new one.

Serial callbacks only copy parameters and schedule `reactor.register_async_callback`. Decoding, `SnapshotAssembler` updates, generation checks, completion, and status publication run on the reactor thread. Callback state is local to that transaction, so a delayed callback cannot assemble or complete a later snapshot. A closed, already completed, or stale-generation callback has no side effects and must not publish or settle another query.

One `reactor.completion()` and one absolute five-second deadline cover all five parts. The deadline starts when the snapshot transaction is armed, after the transport observation, and `completion.wait(deadline)` is called once. There is no per-part wait and no sleep or poll loop. Fragments whose full-width instance or query do not match are ignored. Unrelated command results are ignored. An administrative `km_command_result` with `result == 0` is not a snapshot and never completes one. A result with `command == 8` and the allocated query, including a nonzero failure whose instance differs or is 0 after reset, fails the transaction immediately. Matching duplicate parts may be ignored. A conflicting duplicate or a malformed fragment that matches this instance and query fails the transaction. Decoder exceptions are completed as `command_error` and do not escape the reactor callback. The command checks the deadline again before publishing; a late callback cannot turn a timeout into a stored snapshot.

On full assembly the cache stores one fresh dictionary: `instance`, `query`, and every field from `Snapshot.values` (booleans, signed position and velocity integers, and tuples of client counters). Raw response buffers are not stored. `last_query` becomes the consumed id and `last_error` is cleared. `motion_available` stays false. Failure and invalidation clear the cached snapshot. A snapshot error uses the generation guard in `_fail`, so it does not replace a newer connection's state. Disconnect during the wait still unregisters wrappers and releases the mutex after the bounded wait; this extra does not add a cancellation framework.

The gcode response prints the cached snapshot fields. It does not invent binding state or convert units. This remains diagnostic observation, not an OpenAMS device adapter and not readiness for motion. No installed board is claimed to have passed this path.

## Explicit physical sensor read

`KLIPPER_MMU_SENSORS MMU=NAME` is one manual, read-only query. It first refreshes
the transport state, then allocates from the same monotonic query high-water as
`KLIPPER_MMU_SNAPSHOT` under the MCU mutex. Firmware captures status and
physical sensors together under its core lock. Three bounded Klipper responses
carry configured, present, valid and fresh bits plus original sample times for
each bay and the shared hub and toolhead inputs. A repeated query returns that
retained capture. The host requires all three matching parts; it unregisters
temporary callbacks on success, rejection, disconnect or timeout. The command
prints `unknown` for invalid or stale readings and `unconfigured` for missing
inputs. It does not map one shared hub sensor into per-bay loaded state or infer
filament arrival from buffer position. It neither sends motion nor marks OpenAMS
hardware ready.

`KLIPPER_MMU_PHYSICAL MMU=NAME` reads all five status parts and all three sensor
parts for one retained MCU query and acquisition time. It prints each input's
configured, valid, fresh, and presence state. Shared-path ownership is reported
as one known bay only when logical status and physical presence agree, as proven
empty only when both agree on absence, and as unknown otherwise. This is a
manual diagnostic; it does not start motion, acknowledge events, or make the
OpenAMS device ready. Invalid or stale inputs remain unknown.

## Status and the query command

`get_status(eventtime)` returns a new dictionary of cached, JSON-compatible diagnostics and does no I/O and no query allocation:

- `connected`
- `state`
- `instance`
- `last_attached_instance` (the last nonzero attached instance observed by this host object)
- `boot_transition` (`None` until comparable, then `same` or `changed` for the latest attached observation)
- `boot_instance_changed` (sticky true after this host object observes a different attached instance)
- `last_query`
- `firmware_actuation`
- `diagnostic_startup` (the configured boolean; false by default)
- `startup_state` (`None`, or `idle` / `waiting` / `ready` / `faulted`)
- `startup_result` (`None`, or the signed code from the startup reply)
- `motion_available` (always false)
- `last_error`
- `snapshot` (`None`, or a new dictionary as described above)

Invalid, disconnected, and file-output observations expose null `instance`, `last_query`, `startup_state`, `startup_result`, and `snapshot`. `diagnostic_startup` still reports the configured preference. `state` is `disconnected`, `file_output`, `unavailable`, or one of the four observed binding names above. Those labels are not interchangeable.

Boot-instance comparison is an in-memory diagnostic only. A disconnect, failed
query, unbound state, or stale response does not replace the last attached
instance; a fresh host object has no prior comparison. A changed identity
remains visible through `boot_instance_changed` even after subsequent same-boot
queries. Neither equality nor change establishes session ownership or grants
motion permission.

`KLIPPER_MMU_QUERY` sends only while this connection is active, then reports the transport diagnostic with `respond_info`, including `diagnostic_startup`, `startup_state`, and `startup_result`. It does not send a snapshot or `km_diagnostic_start`. A fresh observation clears the cached snapshot so an older capture is not shown as current. In file-output or when disconnected it reports the cached diagnostic and does not send. A transport reply is not reported as ready, usable, or actuation authorization. Query, lookup, and decoder failures leave the current observation invalid, keep `last_error`, and raise `command_error` to the explicit caller. They do not retry automatically or reset an instance.

## Manual maintenance counters

Set `maintenance_counters_file` to an absolute JSON path to opt into manual,
persistent maintenance counts. A missing file starts empty. The bounded file
contains `{"counters":{"blade":{"value":12,"limit":100,"warning":"Replace cutter blade"}}}`;
names use lowercase letters, digits, and underscores (up to 32 characters),
with up to 32 counters. Counts and limits are integers from 0 to 1,000,000,000.
`KLIPPER_MMU_MAINTENANCE MMU=NAME` lists counters; `COUNTER=blade` reads one;
`INCR=1` increments, `RESET=1` resets to zero, and
`LIMIT=100 WARNING="Replace cutter blade"` sets a threshold and optional
printable warning text (up to 160 characters; empty clears it). `due` is true
only when the count exceeds a positive limit, regardless of warning text.
These are explicit user edits, never inferred from operations;
they send no MCU command. `get_status` returns a copy under
`maintenance_counters` (null when disabled). The file rejects duplicate keys
and is replaced atomically after a valid edit.

## Manual gate metadata

Set `gate_metadata_file` to an absolute JSON path to enable host-owned gate
annotations. With OpenAMS topology, its bay count sets the valid gate range;
an optional `gate_metadata_gates` must match it. Without OpenAMS topology,
`gate_metadata_gates` is required and must be 1 through 8. A missing file starts
with an empty map. The file contains `{"gates":{"0":{"name":"PLA",
"material":"PLA","color":"FFFFFF","available":true}}}`. Reads are bounded
to 8192 bytes and reject duplicate keys, unknown fields, and invalid indices.

`KLIPPER_MMU_SET_GATE_METADATA MMU=NAME GATE=0 NAME=PLA MATERIAL=PLA
COLOR=FFFFFF AVAILABLE=1` saves supplied fields as an explicit manual edit.
`AVAILABLE` accepts 0 or 1 and is only a user's assertion; it does not indicate
physical filament presence, grant ownership, or trigger transport or motion.
`get_status` returns a copy under `gate_metadata` (or null when disabled).

## Logical tool mapping

Set `tool_mapping_file` to an absolute JSON path to enable persistent tool-to-gate
assignments. OpenAMS bay count sets the range when configured; otherwise set
`tool_mapping_gates` to 1 through 8. An optional `tool_mapping_gates` with OpenAMS
must match its bay count. A missing file starts empty. The file format is
`{"tools":{"0":0,"1":0}}`; several tools may explicitly alias one gate.
Duplicate keys, malformed JSON, invalid indices, and files over 1024 bytes
cause configuration to fail. Valid edits replace the file atomically.

`KLIPPER_MMU_TOOL_MAP MMU=NAME TOOL=0 GATE=1` saves an assignment.
`CLEAR=1` removes that tool's assignment. `GATE` and `CLEAR` together are
rejected. Omit both to query; an absent assignment reports `gate=unmapped`.
`get_status` exposes a copy under `tool_mapping` (or null when disabled).
This is a logical resolver only. It neither asserts filament availability nor
changes motor or printer state. A caller must independently validate physical
conditions before any later operation uses the result.

## Step-estimated Bowden length

Set `bowden_estimate_file` to an absolute JSON path to keep the last
step-estimated PTFE calibration. Without an encoder, firmware reports it as
completed feeder microsteps from HUB to qualified FPS arrival, never as encoder
clicks. A missing file starts empty. After a flagged `PTFE_CAL_COMPLETE` ACK is
confirmed, the file is atomically replaced with
`{"estimate":{"kind":"step_estimate","unit":"feeder_microsteps",
"span":"hub_to_fps_arrival","steps":12345,"lane":0,"mmu":"box",
"firmware":{...},"diff_calibration_sha256":...}}`.
`firmware` contains every integer `KLIPPER_MMU_*` constant from the connected
dictionary. The hash is null without a `diff_calibration_file`.

Loading rejects files over 8192 bytes, duplicate keys and missing, unknown or
relabeled fields. A failed write is logged and set in `last_error`; the ACK
stands, so calibrate again. `get_status` returns a copy under `bowden_estimate`
(null when disabled or empty). An encoder result is not stored here. OpenAMS
receives 0, its uncalibrated PTFE value, for an estimate.

When the diff calibration is configured and the stored estimate matches this
MMU, the connected dictionary's constants and the calibration hash, and it
falls within the dictionary's `KLIPPER_MMU_FPS_MIN_STEPS`/`MAX_STEPS`, the
next configuration sends `config_km_fps_steps` after the calibration object,
restoring the calibrated HUB-to-arrival distance so LOAD does not need a
fresh CALIBRATE every session. Firmware accepts arrival within that distance
plus or minus its own configured window (`KLIPPER_MMU_LOAD_WINDOW_MM`); the
outer travel bounds never narrow. Otherwise nothing is sent, and LOAD stays
refused until CALIBRATE measures a distance in that session. See
[FPS arrival](../docs/fps-arrival.md#defaults-and-per-printer-bounds). The
LOAD attempt report below also states travel past HUB as a share of it.

## LOAD attempt report

After the confirmed ACK of a LOAD terminal, the observer sends one
`km_load_attempts_query` for that identity, bounded to 0.2 s under the same
MCU mutex, and explains each attempt of the
[firmware's retries](../docs/fps-arrival.md#load-retries): segment (lane to
HUB, HUB to extruder), cause, distance and the next tactic. Distances are in
millimetres when the dictionary exports a nonzero
`KLIPPER_MMU_FEEDER_STEPS_PER_M`, otherwise in microsteps; past HUB they are
also a share of this MMU's stored Bowden estimate. The report is always logged;
a retried or unsuccessful LOAD is also shown with `respond_info`. `get_status`
and the managed OpenAMS unit's webhook status expose the latest as `last_load`
(`lane`, `request`, `summary`, `attempts`). It is diagnostic only: firmware
without the query, a timeout or a changed connection logs a warning and never
affects the ACK, OpenAMS or statistics. A LOAD admitted before the query
replaces the records, which then read as none.

With `openams_motion: True`, `KLIPPER_MMU_CALIBRATE MMU=NAME BAY=n` starts
that calibration on an empty path. It requires `bowden_estimate_file` and the
bound OpenAMS runtime, and dispatches that runtime's own `Calibrate` action
(from the module the runtime was loaded with; OpenAMS is not imported here),
so OpenAMS's reducer and waiter run it as `OAMS_CALIBRATE_PTFE_LENGTH` does.
After the waiter settles, the command waits for the event transaction's ACK
and reports the newly stored microsteps, the file, and that the narrowed
window applies at the next restart. When the connected dictionary exports a
nonzero `KLIPPER_MMU_FEEDER_STEPS_PER_M` (BoxTurtle 727272 from its
menu mechanics), it also reports millimetres (`steps * 1000 / steps_per_m`,
one decimal) and says this is the value to use for `openams_path_length_mm`;
otherwise only the microstep count is reported. A failed calibration, or a
success that stored no estimate (an encoder result or a failed write), raises
an error.

## Retained terminal diagnostic

`KLIPPER_MMU_TERMINAL MMU=NAME CLIENT=1 SESSION=<id> REQUEST=<id> OPERATION=<1..4> LANE=<0..7>` is an explicit read-only lookup. It first makes a fresh transport observation, then requires an attached, nonzero instance. Its five-second bounded query uses Klipper's ordinary `km_terminal_query` command and temporary response registrations. The full controller and request identity must match before it reports a retained event. A stale or evicted history record prints `history_miss=unknown`, never success. Disconnect and stale response checks prevent publication across connection generations. The command does not acknowledge the event, complete an OpenAMS waiter, start motion, or establish a host session.

`KLIPPER_MMU_EVENT MMU=NAME` reads the retained queue head after a fresh transport observation. It requires an attached, nonzero instance and holds the MCU mutex. A matching head prints its full identity, sequence, timestamp, type, result, value, and flags; firmware's empty result prints `queue_empty=true`. A disconnect, malformed response, rejection, or timeout raises an error and publishes no event. Reading never acknowledges the head or completes an OpenAMS waiter.

## Diagnostics and guided commissioning

`KLIPPER_MMU_DIAGNOSE MMU=NAME` explains the current physical capture, the
retained fault, the LOAD attempt report and the stored Bowden estimate in
plain language, and the same rules run once after every ACKed operation
terminal. `KLIPPER_MMU_COMMISSION MMU=NAME STEP=switches|lane|fps` guides a
builder through per-switch, per-lane and FPS checks, driving motion only
through the existing PRELOAD/EJECT paths above. See
[diagnostics](../docs/diagnostics.md).
