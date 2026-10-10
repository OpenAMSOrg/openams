# G-code commands reference

Every `KLIPPER_MMU_*` command this add-on registers, for a BoxTurtle running
klipper-mmu. Every command takes `MMU=<name>` to pick which MMU config
section it talks to; for a BoxTurtle set up as `[klipper_mmu box]`, that's
`MMU=box`, which every example below uses.

For background, see [BoxTurtle quick start](boxturtle-quick-start.md),
[diagnostics](diagnostics.md), and [selectors](selectors.md).

---

## Everyday commands

Commands you'll reach for during normal use — checking on the MMU, loading
or ejecting a spool, and working through a fault:

- `KLIPPER_MMU_QUERY` — is the MMU connected?
- `KLIPPER_MMU_SENSORS` — what do the switches read right now?
- `KLIPPER_MMU_DIAGNOSE` — what's wrong, in plain language?
- `KLIPPER_MMU_PRELOAD` — feed a spool into a bay and hold it ready
- `KLIPPER_MMU_UNPRELOAD` — back a spool's filament out of its bay
- `KLIPPER_MMU_EJECT` — fully withdraw a spool's filament from its bay
- `KLIPPER_MMU_ACK_FAULT` — acknowledge a fault after fixing its cause
- `KLIPPER_MMU_RESTART_DRIVER` — restart one lane's (or the selector's) motor driver after a driver fault
- `KLIPPER_MMU_RECONCILE_PATH` — tell the MMU the shared path is empty, or which lane owns it
- `KLIPPER_MMU_RECOVER_SESSION` — release the old connection record after a disconnect

Everything else below is for setup, calibration, or less common recovery.
Every command's heading below is marked **Everyday** or **Advanced**.

---

## Status commands

These just read the MMU's current state. None of them move anything, so
they're safe to run at any time, including mid-print.

| Command | What it shows | When to use it |
| --- | --- | --- |
| `KLIPPER_MMU_QUERY MMU=box` (Everyday) | Whether Klipper can currently see the MMU board, and — if `diagnostic_startup` is on — whether board startup finished. | Right after a restart, or as your first check before troubleshooting anything else. |
| `KLIPPER_MMU_SENSORS MMU=box` (Everyday) | The current state of every configured BAY, LOAD, HUB and toolhead switch. | When a lane won't load, to check whether the board is actually seeing filament at that switch. |
| `KLIPPER_MMU_DIAGNOSE MMU=box` (Everyday) | A plain-language explanation of anything wrong right now, or `no problems found`. | After any fault, or whenever something seems off — it's read-only, so it's always safe to run. |
| `KLIPPER_MMU_SNAPSHOT MMU=box` (Advanced) | A complete, point-in-time dump of the firmware's internal state: position, faults, sessions and pending requests. | When you need the full picture at once, for example to include in a bug report. |
| `KLIPPER_MMU_PHYSICAL MMU=box` (Advanced) | Every sensor reading, plus which lane (if any) the MMU believes currently occupies the shared path between the hub and the extruder. | Before deciding whether to `CLEAR` or `ADOPT` the shared path with `KLIPPER_MMU_RECONCILE_PATH`. |
| `KLIPPER_MMU_EVENT MMU=box` (Advanced) | The next event waiting to be delivered, without acknowledging it. | While debugging a stuck fault or request, to see what the MMU is about to report. |
| `KLIPPER_MMU_STATS MMU=box` (Advanced) | How many loads, unloads, calibrations, preloads and ejects have completed, failed, or been cancelled since Klipper started, by lane. | To check whether one lane is failing more often than the others. |
| `KLIPPER_MMU_SWITCH_DELAY MMU=box` (Advanced) | The software delay, in microseconds, between sampling the switches and the firmware acting on them (not the physical switch latency). | If you suspect GPIO sampling is lagging behind real switch changes, e.g. diagnosing intermittent misreads. |

`KLIPPER_MMU_QUERY`'s `startup_state`/`startup_result` fields only appear
when `diagnostic_startup` is configured. `KLIPPER_MMU_DIAGNOSE`'s
Bowden-drift check needs `bowden_estimate_file` configured, and its
switch-bounce, idle-edge and TMC checks are silently skipped unless the
firmware exports the diagnostic counters.

### `KLIPPER_MMU_TERMINAL` — Advanced

**Purpose:** Look up one specific past LOAD/UNLOAD/FOLLOW/CALIBRATE result by its exact identity.

**Parameters:**
- `CLIENT`: integer, 1–3 (1=HOST, 2=LOCAL_UI, 3=INTERNAL).
- `SESSION`: integer, 1–0xffffffff.
- `REQUEST`: integer, 1–0xffffffff.
- `OPERATION`: integer, 1–4 (1=LOAD, 2=UNLOAD, 3=FOLLOW, 4=CALIBRATE).
- `LANE`: integer, 0–7.

**Example:** `KLIPPER_MMU_TERMINAL MMU=box CLIENT=1 SESSION=1 REQUEST=1 OPERATION=1 LANE=0`

**Behavior:** Looks up the retained history for that exact combination of client, session, request, operation and lane. Prints `history_miss=unknown` if nothing matches that identity (including if it has aged out of history), or the event type, result code, value and flags if it does. Use this when you have an exact identity to check — for example copied from a log line — and want to know what actually happened to that specific request; day to day, `KLIPPER_MMU_DIAGNOSE` is the easier way to find out what's wrong.

**Motor movement:** No.

**Configuration:** `mcu`.

---

## Loading and ejecting filament

These start or check on a firmware PRELOAD, UNPRELOAD or EJECT: feeding a
spool into a bay, backing it out, or fully withdrawing it. All three need
`openams_motion: True` and the OpenAMS lease options configured (see
[OpenAMS integration](../integration/openams/README.md)).

### `KLIPPER_MMU_PRELOAD` — Everyday

**Purpose:** Feed a spool into a bay and hold it ready, or check on a PRELOAD already running.

**Parameters:**
- `BAY`: integer, 0–(bay count − 1), optional.
- `CANCEL`: integer, 0 or 1, optional (not together with `BAY`).

**Example:** `KLIPPER_MMU_PRELOAD MMU=box BAY=0`

**Behavior:** With `BAY=n`, starts PRELOAD on bay `n` and reports whether it was accepted, or the result if it already finished. With no `BAY`, reports whatever PRELOAD is currently pending, or the last recorded result. With `CANCEL=1`, cancels the pending PRELOAD request and reports the cancellation. Use this after inserting a new spool into an empty bay, to have the MMU pull it in and confirm it can grip and see the filament before you need it for a print.

**Motor movement:** Yes, via firmware PRELOAD (requires `openams_motion`).

**Configuration:** `openams_motion` with the OpenAMS topology and lease options ([OpenAMS integration](../integration/openams/README.md)).

### `KLIPPER_MMU_UNPRELOAD` — Everyday

**Purpose:** Back a spool's filament out of a bay it was fed into, or check on an UNPRELOAD already running.

**Parameters:**
- `BAY`: integer, 0–(bay count − 1), optional.
- `CANCEL`: integer, 0 or 1, optional (not together with `BAY`).

**Example:** `KLIPPER_MMU_UNPRELOAD MMU=box BAY=0`

**Behavior:** With `BAY=n`, starts UNPRELOAD on bay `n` and reports whether it was accepted, or the result. With no `BAY`, reports the pending request or the last recorded result. With `CANCEL=1`, cancels the pending UNPRELOAD request. Use this before removing a spool you'd preloaded, to withdraw its filament out of the bay's feeder first.

**Motor movement:** Yes, via firmware UNPRELOAD (requires `openams_motion`).

**Configuration:** `openams_motion` with the OpenAMS topology and lease options ([OpenAMS integration](../integration/openams/README.md)).

### `KLIPPER_MMU_EJECT` — Everyday

**Purpose:** Fully withdraw a spool's filament from its bay once the shared path has been unloaded, or check on an EJECT already running.

**Parameters:**
- `BAY`: integer, 0–(bay count − 1), optional.
- `CANCEL`: integer, 0 or 1, optional (not together with `BAY`).

**Example:** `KLIPPER_MMU_EJECT MMU=box BAY=0`

**Behavior:** With `BAY=n`, starts a full firmware EJECT on bay `n` and reports whether it was accepted, or the result. With no `BAY`, reports the pending request or the last recorded result. With `CANCEL=1`, cancels the pending EJECT request. Use this after a tool change's UNLOAD has cleared the shared path, when you want that bay's filament wound all the way back so you can pull the spool. On a unit without BAY switches (Angry Beaver, 3MS, QuattroBox), EJECT backs the lane out by a set distance instead, as Happy Hare does (`KLIPPER_MMU_EJECT_FINAL_DISTANCE_MM`: 20 mm, or 1500 mm on 3MS), and marks the gate empty.

**Motor movement:** Yes, via firmware EJECT (requires `openams_motion`).

**Configuration:** `openams_motion` with the OpenAMS topology and lease options ([OpenAMS integration](../integration/openams/README.md)).

### `KLIPPER_MMU_HOME` — Everyday

**Purpose:** Home the selector of a selector machine (ERCF, Tradrack, MMX6, 3D Chameleon, HTLF, MMX), or check on a HOME already running.

**Parameters:**
- `CANCEL`: integer, 0 or 1, optional.

**Example:** `KLIPPER_MMU_HOME MMU=box`

**Behavior:** Starts a firmware HOME and reports whether it was accepted; if one is already pending, reports that request instead. With `CANCEL=1`, cancels the pending HOME. You rarely need it: the first selection after boot or a selector fault homes on its own. A selector resting on its endstop backs off first. Refused on firmware built without a selector. See [selectors](selectors.md).

**Motor movement:** Yes, the selector axis only, via firmware HOME (requires `openams_motion`).

**Configuration:** `openams_motion` with the OpenAMS topology and lease options ([OpenAMS integration](../integration/openams/README.md)).

### `KLIPPER_MMU_MOTORS_OFF` — Advanced

**Purpose:** Release the selector's own motor (and its shared gear) so you can move the selector by hand, the same idea as Happy Hare's MMU_MOTORS_OFF.

**Parameters:** None.

**Example:** `KLIPPER_MMU_MOTORS_OFF MMU=box`

**Behavior:** Shuts the selector driver down without faulting anything, and forgets that the selector is homed or sitting at a gate; the next selection homes it again from scratch. Refused while any operation, homing or selection is in progress, and on firmware built without a selector. Run this before sliding, turning, or swinging the selector by hand to line it up with a gate for `KLIPPER_MMU_CALIBRATE_SELECTOR`.

**Motor movement:** No -- it turns a motor's holding torque off, never on.

**Configuration:** `openams_motion` with the OpenAMS topology and lease options ([OpenAMS integration](../integration/openams/README.md)).

### `KLIPPER_MMU_CALIBRATE_SELECTOR` — Advanced

**Purpose:** Measure or set one selector gate's calibrated position (LINEAR/ROTARY selectors) or angle (the SERVO selector kind), the same idea as Happy Hare's MMU_CALIBRATE_SELECTOR.

**Parameters:**
- `GATE`: integer, 0–254, required.
- `ANGLE`: integer, degrees, optional; giving it switches to the SERVO-kind flow below instead of homing.
- `SPACING`: integer, degrees, optional, only used together with `ANGLE`.
- `SAVE`: integer, 0 or 1, optional, default 1.

**Example:** `KLIPPER_MMU_CALIBRATE_SELECTOR MMU=box GATE=0`

**Behavior:** Refused on the INDEXED selector kind (BTT ViViD): it has no per-gate position or angle to measure or save -- every gate is found fresh by its own index switch on each selection (`docs/selectors.md`).

**Behavior:** Without `ANGLE` (LINEAR/ROTARY selectors): run `KLIPPER_MMU_MOTORS_OFF`, position the selector by hand at gate `GATE`, then run this. One call: it starts a firmware HOME, waits for it to finish, reads the measured travel to the switch, and saves it as gate `GATE`'s position -- a `HOME_FAILED` or a timeout reports clearly and saves nothing. Once both gate 0 and the last gate have been measured this way, the rest are spread evenly between them, Happy Hare's own rule. With `ANGLE=a` (the SERVO selector kind): no homing here -- this just saves the angle you already found with `KLIPPER_MMU_SERVO`. Add `SPACING=s` to also fill every other gate `s` degrees from this one, Happy Hare's MMU_CALIBRATE_SERVO_SELECTOR `SPACING`. `SAVE=0` measures or moves and reports, without saving. Saving requires `selector_calibration_file`; see [selectors](selectors.md#calibrating-a-selector-machine). Refused on firmware built without the matching selector kind.

**Motor movement:** Yes, the selector axis only, via firmware HOME (LINEAR/ROTARY selectors; requires `openams_motion`); with `ANGLE=`, only the selection servo moves.

**Configuration:** `openams_motion` with the OpenAMS topology and lease options ([OpenAMS integration](../integration/openams/README.md)); saving needs `selector_calibration_file`.

### `KLIPPER_MMU_SERVO` — Advanced

**Purpose:** Jog the SERVO selector kind's selection servo to a test angle, to find each gate's angle before saving one with `KLIPPER_MMU_CALIBRATE_SELECTOR`.

**Parameters:**
- `ANGLE`: integer, degrees, required.

**Example:** `KLIPPER_MMU_SERVO MMU=box ANGLE=83`

**Behavior:** Moves the selection servo straight to `ANGLE` degrees and reports it; nothing is selected, and no saved calibration changes. Use this to sweep through angles and note down the one that lines up with each gate, then save it with `KLIPPER_MMU_CALIBRATE_SELECTOR MMU=box GATE=n ANGLE=a`. Refused unless the MMU is otherwise idle, and on firmware built without the SERVO selector kind.

**Motor movement:** Yes, the selection servo only.

**Configuration:** `openams_motion` with the OpenAMS topology and lease options ([OpenAMS integration](../integration/openams/README.md)).

---

## Recovering from a fault

After the MMU stops for a fault, work through these in order — see
[path reconciliation](path-reconciliation.md#from-a-stopped-fault) for the
full sequence.

### `KLIPPER_MMU_ACK_FAULT` — Everyday

**Purpose:** Acknowledge a fault the MMU has reported, once you've confirmed the lane actually stopped.

**Parameters:** None.

**Example:** `KLIPPER_MMU_ACK_FAULT MMU=box`

**Behavior:** Acknowledges the retained `FAULT_ENTERED` or `FOLLOW_FAULT` event, but only after confirming the affected lane has actually stopped moving. This does not clear the firmware fault itself or allow motion again — that's what `KLIPPER_MMU_RECONCILE_PATH` is for. Use this after a fault pauses a print, once `KLIPPER_MMU_DIAGNOSE` has told you what happened and you have fixed the physical cause, and before restarting a driver or reconciling the path.

**Motor movement:** No.

**Configuration:** `openams_motion` with the OpenAMS topology and lease options ([OpenAMS integration](../integration/openams/README.md)).

### `KLIPPER_MMU_RESTART_DRIVER` — Everyday

**Purpose:** Restart one lane's — or the selector's — motor driver and clear its fault latch.

**Parameters:**
- `LANE`: integer, 0–7, required unless `SELECTOR=1`.
- `SELECTOR`: integer, 0 or 1; restart the selector's own TMC2209 instead of a lane, required unless `LANE` is given. Exactly one of `LANE` or `SELECTOR` must be given.

**Example:** `KLIPPER_MMU_RESTART_DRIVER MMU=box LANE=2` (or `MMU=box SELECTOR=1` to restart the selector's own driver instead)

**Behavior:** Restarts the TMC driver for lane `n` — or, with `SELECTOR=1`, the selector's own TMC2209 — and clears its STEP/DIR fault latch, reporting ready once the driver reconfirms. It never enables an output by itself — a stopped operation fault still needs `KLIPPER_MMU_RECONCILE_PATH` afterward. Use this after a driver fault (for example a static-discharge reset, or the motor power was off), once you've fixed the physical cause and need that lane's — or the selector's — driver working again. `SELECTOR=1` is refused on a build with no selector, or one whose selector has no TMC2209 of its own (a plain STEP/DIR axis).

**Motor movement:** No.

**Configuration:** `driver_restart`.

### `KLIPPER_MMU_RECONCILE_PATH` — Everyday

**Purpose:** Tell the MMU the shared path between the hub and the extruder is empty, or which lane's filament is in it.

**Parameters:**
- `ACTION`: string, `CLEAR` or `ADOPT`.
- `LANE`: integer, 0–7, required when `ACTION=ADOPT`.

**Example:** `KLIPPER_MMU_RECONCILE_PATH MMU=box ACTION=CLEAR` (or `ACTION=ADOPT LANE=2` if lane 2's filament is still in the path)

**Behavior:** `ACTION=CLEAR` states that the shared path is physically empty — fresh sensor readings must agree. `ACTION=ADOPT LANE=n` states that lane `n`'s filament occupies it, checked the same way. If the MMU was stopped on a fault, either action also clears that fault once every event has been acknowledged and the drivers are healthy, and then frees the lane on the OpenAMS side when the fork you're running supports it (otherwise restart Klipper before using that lane). This command never moves anything, turns anything on, or authorizes a print to continue by itself. Use this whenever the MMU reports the shared path as occupied by an unknown lane — for example right after a boot with filament already loaded, or once you've fixed a fault and checked by eye whether the path is empty. See [path reconciliation](path-reconciliation.md).

**Motor movement:** No.

**Configuration:** `path_reconciliation`.

### `KLIPPER_MMU_RECOVER_SESSION` — Everyday

**Purpose:** Release the printer's old connection record after a disconnect, so a new one can be prepared.

**Parameters:** None.

**Example:** `KLIPPER_MMU_RECOVER_SESSION MMU=box`

**Behavior:** Releases a disconnected host session, but only once there's no fault acknowledgment or request still outstanding on it. Requires the board to be at diagnostic READY, the same old session counter it had before, and a fresh capture proving either an empty path or one quiescent, already-identified lane. This does not restore motion by itself; it just clears the way for the next print to prepare a fresh session. Use this last in the fault-recovery sequence, once you've acknowledged the fault, restarted any faulted driver, and reconciled the path.

**Motor movement:** No.

**Configuration:** `openams_motion` with the OpenAMS topology and lease options ([OpenAMS integration](../integration/openams/README.md)).

---

## Recovering after a dropped connection

These are for the narrower case where the host briefly lost its connection
to the board while a PRELOAD/UNPRELOAD/EJECT (or a fault) was still being
handled, leaving a result or an acknowledgment stranded. Most disconnects
are resolved by the ordinary fault-recovery commands above; reach for these
only when one of them says there's nothing to do but you know a result is
still outstanding.

### `KLIPPER_MMU_RECOVER_LOCAL` — Advanced

**Purpose:** Finish acknowledging a PRELOAD/UNPRELOAD/EJECT result that was left hanging when the host lost its lease on the board, whether the connection dropped or Klipper simply stalled.

**Parameters:** None.

**Example:** `KLIPPER_MMU_RECOVER_LOCAL MMU=box`

**Behavior:** Either retries the acknowledgment that was already accepted but never confirmed, or reconciles the leftover result from before the disconnect — whichever applies. It does not send a heartbeat or restore motion. Use this after your host briefly lost the board while a PRELOAD, UNPRELOAD or EJECT was still pending, to clear that leftover result: a USB replug, or Klipper stalling long enough (over the 2 s host lease) that the board stopped the motion on its own. `KLIPPER_MMU_DIAGNOSE` names the case; follow it with `KLIPPER_MMU_RECONCILE_PATH` and `KLIPPER_MMU_RECOVER_SESSION` as it suggests.

**Motor movement:** No.

**Configuration:** `openams_motion` with the OpenAMS topology and lease options ([OpenAMS integration](../integration/openams/README.md)).

### `KLIPPER_MMU_RECOVER_WAITER` — Advanced

**Purpose:** Finish acknowledging a PRELOAD/UNPRELOAD/EJECT request that was still waiting for an outcome when the connection to the board dropped.

**Parameters:** None.

**Example:** `KLIPPER_MMU_RECOVER_WAITER MMU=box`

**Behavior:** Either retries the acknowledgment that was already accepted but never confirmed, or reconciles the leftover request from before the disconnect. It does not send a heartbeat or restore motion. Use this in the same kind of disconnect as `KLIPPER_MMU_RECOVER_LOCAL`, when it was a request still waiting for its outcome — rather than an already-recorded result — that was left stranded.

**Motor movement:** No.

**Configuration:** `openams_motion` with the OpenAMS topology and lease options ([OpenAMS integration](../integration/openams/README.md)).

---

## Calibration and commissioning

One-time or occasional setup tasks, not part of day-to-day printing.

### `KLIPPER_MMU_DIFF_PROBE` — Advanced

**Purpose:** Capture one raw FPS buffer sensor reading pair, for building a calibration file.

**Parameters:** None.

**Example:** `KLIPPER_MMU_DIFF_PROBE MMU=box`

**Behavior:** Reads the raw DiffA/DiffB pair and its timing, and reports `state`, `result`, `a_raw`, `b_raw`, `a_attempt_us`, `b_ready_us`, and `observed_us`. Use this while building the FPS buffer's calibration file — at each known buffer position you record, this is how you capture the raw reading for it. Requires `diff_probe: True`.

**Motor movement:** No.

**Configuration:** `diff_probe`.

### `KLIPPER_MMU_CALIBRATE` — Advanced

**Purpose:** Measure a bay's actual hub-to-extruder path length instead of guessing it.

**Parameters:**
- `BAY`: integer, 0–(bay count − 1).
- `SAVE`: 0 or 1, default 1.

**Example:** `KLIPPER_MMU_CALIBRATE MMU=box BAY=0`

**Behavior:** Feeds bay `n` through an empty path and reports the measured hub-to-extruder length in feeder microsteps and, if `KLIPPER_MMU_FEEDER_STEPS_PER_M` is exported, also in millimetres. With `SAVE=1` (the default), saves the result to `bowden_estimate_file`, which narrows the FPS arrival window used by later LOADs starting at the next restart. Use this once the MMU is running, instead of guessing `openams_path_length_mm`, and again any time you change the Bowden path, the DiffA/DiffB calibration, or the firmware. Requires `openams_motion`, `bowden_estimate_file`, and an OpenAMS runtime.

`calibrated_arrival_steps` is one distance shared by every lane, not per-lane like a selector's gate table, so measuring a *different* bay from the one already saved would otherwise leave every lane aiming at that one-off measurement. `SAVE=0` still measures and briefly lets the firmware use the result, but then puts back whatever `bowden_estimate_file` already held, on both the file and the running firmware — nothing is saved and the previously calibrated bay's LOADs are undisturbed. With a stored estimate to compare against, the report also adds a line: bay `n` measured against the stored length, and — if the two bays share the same physical path — the rotation-distance scale factor to apply to bay `n` in [per-lane rotation distance](../site/calibration.html). Use this to check another lane's real gear against a reference lane you already trust, without recalibrating the shared distance.

**Motor movement:** Yes, via firmware motion (requires `openams_motion`).

**Configuration:** `openams_motion` with the OpenAMS topology and lease options, and `bowden_estimate_file`.

### `KLIPPER_MMU_COMMISSION` — Advanced

**Purpose:** Walk through checking every switch, lane and the FPS buffer before trusting a newly built or rewired MMU.

**Parameters:**
- `STEP`: string, `switches`, `lane`, or `fps`.
- `LANE`: integer, 0–(bay count − 1), required when `STEP=lane`.
- `ANSWER`: string, `yes` or `no`, optional when `STEP=lane`.

**Example:** `KLIPPER_MMU_COMMISSION MMU=box STEP=switches`

**Behavior:** `STEP=switches` reads every switch and its bounce/idle-edge counts. `STEP=fps` samples the FPS buffer position for about 10 s. `STEP=lane LANE=n` waits for you to insert filament into lane `n`, then runs PRELOAD and EJECT so you can watch it happen; call it again with `ANSWER=yes` or `ANSWER=no` (no motion) to record whether the spool wound the filament back. `STEP=switches` and `STEP=fps` need no motion and work on a `-safe` (`ACTUATION=0`) image, so you can check switches and the FPS before any motor can move; only `STEP=lane` needs `openams_motion`. Use this once, right after wiring a new BoxTurtle, or any time you touch a switch or a lane and want to check it before trusting it again.

**Motor movement:** Yes, via firmware for `STEP=lane` (requires `openams_motion`).

**Configuration:** `mcu`; `STEP=lane` needs `openams_motion`. Bounce and idle-edge counts need firmware that exports them.

---

## Metadata and tool mapping

These record information you choose to track yourself — the MMU doesn't
infer any of it from sensors — and are entirely optional.

### `KLIPPER_MMU_SET_GATE_METADATA` — Advanced

**Purpose:** Record what's loaded in a gate (name, material, color), shown in the MMU's Klipper status.

**Parameters:**
- `GATE`: integer, 0–(gate count − 1).
- `NAME`: string, optional.
- `MATERIAL`: string, optional.
- `COLOR`: string, optional.
- `AVAILABLE`: string, `0` or `1`, optional.

**Example:** `KLIPPER_MMU_SET_GATE_METADATA MMU=box GATE=0 NAME=PLA MATERIAL=PLA COLOR=FFFFFF AVAILABLE=1`

**Behavior:** Saves whichever fields you give for gate `n`; any field you leave out keeps its previously saved value. At least one field is required. Use this to label a gate right after loading a new spool, so the MMU's status shows the right name, material and color for it. `AVAILABLE` is your own note that a spool is there. On a device with a real per-lane BAY switch it never checks or reports physical filament presence — the switch already answers that. On a device with no BAY switch at all, `AVAILABLE` doubles as Happy Hare's own per-gate status (`GATE_UNKNOWN`/`GATE_EMPTY`/`GATE_AVAILABLE`): OpenAMS refuses to attempt a LOAD only on a gate you or a previous LOAD marked empty, same as Happy Hare's own tool-change rule of only refusing a confirmed-empty gate. A LOAD that reaches the toolhead marks its gate available; one that never gets the lane past the shared HUB marks it empty; your own `AVAILABLE=` setting always overrides the last learned value until the next LOAD outcome changes it again. Requires `gate_metadata_file` and `gate_metadata_gates` (or `openams_bays`).

**Motor movement:** No.

**Configuration:** `gate_metadata_file`, `gate_metadata_gates`, `openams_bays` (if using OpenAMS).

### `KLIPPER_MMU_TOOL_MAP` — Advanced

**Purpose:** Tell the MMU which physical gate a slicer's logical tool number should use.

**Parameters:**
- `TOOL`: integer, 0–(mapping count − 1).
- `GATE`: integer, 0–(mapping count − 1), optional.
- `CLEAR`: integer, 1, optional (mutually exclusive with `GATE`).

**Example:** `KLIPPER_MMU_TOOL_MAP MMU=box TOOL=0 GATE=1`

**Behavior:** With `GATE=n`, maps tool `t` to gate `n`. With `CLEAR=1`, removes that tool's mapping. With neither, reports the gate tool `t` currently resolves to. Use this when you rewire which bay is which, or want a slicer's `T0` to point at a different physical gate than usual. Requires `tool_mapping_file` and `tool_mapping_gates` (or `openams_bays`).

**Motor movement:** No.

**Configuration:** `tool_mapping_file`, `tool_mapping_gates`, `openams_bays` (if using OpenAMS).

### `KLIPPER_MMU_MAINTENANCE` — Advanced

**Purpose:** Read or update your own manual wear counters.

**Parameters:**
- `COUNTER`: string, required for edits.
- `INCR`: integer, optional (increment).
- `RESET`: `1`, optional.
- `LIMIT`: integer, optional.
- `WARNING`: string, optional.

**Example:** `KLIPPER_MMU_MAINTENANCE MMU=box COUNTER=blade INCR=1`

**Behavior:** With no arguments, reports every maintenance counter. With `COUNTER=name`, edits that counter: `INCR` adds to it, `RESET=1` sets it back to zero, `LIMIT` sets a threshold, and `WARNING` sets the message shown once it's passed. Use this to track wear items yourself — for example counting how many cuts a blade has made — and get a warning once it's due for replacement. Requires `maintenance_counters_file`.

**Motor movement:** No.

**Configuration:** `maintenance_counters_file`.
