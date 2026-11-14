# Troubleshooting OpenAMS

## What it is, and why it exists

OpenAMS reports most faults as a sentence naming what happened and what to do.
This page is those sentences, indexed by where you meet them: Klipper's startup
log, the console during a print, or the installer's own output.

Start with the three read-only checks below. They answer most questions without
you touching the printer.

**With a Cortex Bridge**, start with `python3 ~/openams/tools/cortex-bridge.py
status` instead: it checks USB, the relay and each AMS, and names the first
thing to fix. The bridge's own table of symptoms and fixes is in
[Set up a Cortex Bridge printer](cortex-bridge-setup.md#troubleshooting), and
`cortex-bridge.py support` writes a report to attach to a support request,
with private details removed ([the support report](support-report.md)).

## The three read-only checks

None of these move filament, and none of them write to the config. Run them from
the OpenAMS checkout.

### 1. `bringup_check.py` — what works, bottom up

```bash
python3 ~/openams/tools/agent/bringup_check.py --selftest
```

Ten levels, from the web API up to the printer's filament switching. Each prints
`L<n> <NAME> <PASS|FAIL|WARN|SKIP> <detail>` with indented detail lines, then a
final `BRINGUP pass=N fail=N warn=N skip=N`. Exit 0 when nothing failed.

`--selftest` sends exactly one command, `OAMSM_SELFTEST`, which is read-only.
Other flags: `--url` (default `http://127.0.0.1:7125`), `--api-key`,
`--fixture-dir` to replay recorded answers instead of using HTTP, `--json`.

Fix the **lowest** failing level first, then run it again. The upper levels
cannot pass while a lower one is broken.

| Level | What it checks | A FAIL means |
|---|---|---|
| L0 MOONRAKER | Moonraker answers; `openams_spoolman` loaded | Moonraker is down, or the component link is missing |
| L1 KLIPPY | Klipper reports `ready` | Read the `state_message` detail |
| L2 IDLE | No print in progress | Stop and wait for the human |
| L3 MCUS | Every MCU reports a firmware version; CAN state | A board is not talking, or CAN has errors |
| L4 OPENAMS | `oams_manager` exists, is ready, units connected | OpenAMS did not load, or a unit is not connected |
| L5 SENSORS | `fps_value` inside 0..1; readers and temperatures in range | The FPS reading, a reader chip, or a temperature sensor |
| L6 MACROS | Every group has a `T<n>` macro; required macros present | A group has no toolchange macro |
| L7 SELFTEST | `OAMSM_SELFTEST` reports `RESULT: PASS` | The self-test names what it found |
| L8 SPOOLMAN | Spoolman reachable, bays linked | Optional; OpenAMS works without it |
| L9 UPDATES | Moonraker can see the OpenAMS update entry | Moonraker cannot update it (no remote, dirty, detached) |

A WARN is usually information: L5 warns about an FPS above 0.8 on an unloaded
lane, which usually means the reading is reversed.

### 2. `config_audit.py` — what is wrong in the config

```bash
python3 ~/openams/tools/agent/config_audit.py --config-dir ~/printer_data/config
```

Findings print one per line as `LEVEL CODE file:line message`, then a summary.
Exit 0 with no errors, 1 with errors, 2 on a usage or I/O problem. `--json`
prints the same findings as JSON; `--commands <file>` cross-checks the macros
against the commands a running Klipper registers.

The codes it uses:

| Level | Codes |
|---|---|
| ERROR | `E_INCLUDE_CYCLE`, `E_INCLUDE_MISSING`, `E_PLACEHOLDER`, `E_MCU_TRANSPORT`, `E_FPS_LANE`, `E_OAMS_MCU`, `E_DEVICE_KIND`, `E_UNIT_IDX_DUP`, `E_FPS_SOURCE`, `E_GROUP_MEMBER`, `E_GROUP_BAY_DUP`, `E_GROUP_LANE_MIX`, `E_GROUP_NAME_DUP`, `E_TAG_KEY`, `E_HDC1080`, `E_PAUSE_RESUME`, `E_MACRO_COLLISION`, `E_MACRO_CUSTOMIZED`, `E_MACRO_MISSING` |
| WARN | `W_FOREIGN_MMU`, `W_TOOL_MACRO`, `W_TOOLHEAD_MACRO` (needs `--commands`) |
| MANUAL | `M_RFID_FIRMWARE`, `M_PTFE_ZERO`, `M_FPS_ORIENTATION`, `M_CUTTER`, `M_MACRO_UNVERIFIED` |
| INFO | `I_NO_TAG_KEY`, `I_SUMMARY` |

An `E_PLACEHOLDER` finding means a `<your_unique_..._UUID>` placeholder is still
in the file, which is the normal state right after a fresh install.

`E_MACRO_COLLISION` and `E_MACRO_CUSTOMIZED` are the same problem at two
severities: another file defines a macro OpenAMS ships. The first is a copy
with the same body — comment it out. The second is one the owner edited, so it
works until the next update, which replaces `oams_macros.cfg`; the added
lines have to move into `OAMS_CUT` or a hook in the owner's own file
([customizing the toolchange](configure.md#customizing-the-toolchange)).

### 3. `preflight.sh` — what is on this host

```bash
bash ~/openams/tools/agent/preflight.sh
```

Prints one `KEY=value` per line, then any `STOP_REASON=` and `ASK=` lines, then
`VERDICT=OK`, `VERDICT=ASK` or `VERDICT=STOP`. It reads files, runs
`systemctl show`, `ip` and `git`, and makes HTTP GETs. It changes nothing.

| Key | Meaning |
|---|---|
| `IS_ROOT` | Must be `no`; the installer refuses to run as root |
| `KLIPPER_SERVICES` | How many Klipper services; more than one needs `-s` |
| `KLIPPER_PATH`, `KLIPPER_COMMIT`, `KLIPPER_DIRTY_FILES` | Which checkout and how modified |
| `KLIPPY_ENV_PYTHON` | Klipper's Python version |
| `GCO_BASELINE`, `GCODE_PY_SHA256` | Whether gco-routines accepts this Klipper: `yes`, `no`, or `unknown` before `~/gco-routines` is cloned |
| `GCO_ACTIVE` | Whether a config file has `[gco_routines]`; with `GCO_BASELINE=no`, Klipper cannot start |
| `KLIPPER_MMU_NAME_CLASH` | `klippy/extras/klipper_mmu.py` exists; the installer refuses |
| `CONFIG_DIR`, `PRINTER_CFG`, `MOONRAKER_CONF` | Whether the config directory exists and holds both files |
| `PRINT_STATE` | `printing` or `paused` is a STOP |
| `OAMS_EXTRA` | `openams`, `klipper_openams`, `broken`, or `none` |
| `HAS_OAMS_CFG`, `HAS_OAMS_MACROS_CFG`, `HAS_OAMS_MACROS_ORDERED_CFG` | Which config files exist |
| `MOONRAKER_HAS_*` | Which Moonraker sections exist |
| `FOREIGN_MMU` | Another MMU system (AFC, Happy Hare, ERCF) is installed |
| `CAN_<iface>_STATE`, `CAN_<iface>_BITRATE`, `CAN_<iface>_QLEN` | CAN bus health |

## Klipper startup errors

| Symptom | Likely cause | Fix |
|---|---|---|
| `Invalid canbus_uuid` | A placeholder or typo in `canbus_uuid` | Read the real UUIDs with `canbus_query.py` |
| `Duplicate canbus_uuid` | The same UUID in two `[mcu]` sections | Give each board its own section |
| `mcu '<name>': Unable to connect` / `Timeout on connect` | Wrong UUID, board unpowered, CAN down | Check `CAN_can0_STATE`, then the UUID, power and cable. A bus that stays flaky: [CAN patches guide](../../klipper_patches/can/GUIDE.md) |
| `mcu 'host': Unable to connect` | The Klipper user is not in group `tty`, so it cannot open `/tmp/klipper_host_mcu` | `sudo usermod -a -G tty <user>` and restart Klipper |
| `Section '<x>' is not a valid config section` | The extra is not installed | Run `./install.sh` again |
| `Unsupported Klipper GCodeDispatch baseline` | `[gco_routines]` is enabled on a Klipper gco-routines was not tested against, often right after a Klipper update | Comment out `[gco_routines]` and `[include oams_macros_ordered.cfg]`, or bring Klipper to a version it accepts ([gco-routines](install.md#gco-routines)) |
| `Internal error during connect: 'MCU' object has no attribute 'register_serial_response'` | Kalico, or mainline Klipper from before 2026-02-26, with an OpenAMS from before 2026-11-08 | Update OpenAMS (`git -C ~/openams pull --ff-only`, then `./install.sh`), then `FIRMWARE_RESTART` |
| `Unknown config object 'heaters'` | An OpenAMS from before 2026-10-18 reads `[hdc1080]` before any heater section, as it is when `[include oams.cfg]` sits above `[printer]` | Update OpenAMS; the include can stay where the installer put it |
| `Option '<x>' in section '<y>' must be specified` | A required option is missing | Add it from [`oams_sample.cfg`](../../oams_sample.cfg) |
| `bambu_lab_tag_processor key must contain exactly 16 hexadecimal bytes` | The tag key is missing or not 32 hex characters | Set a real key locally, never from a public repository |
| `both OpenAMS RFID readers must use one MCU` | The two reader pin sets point at different MCUs | Put both readers on one unit's MCU |
| `Duplicate filament_group '<name>'; names must be unique.` | The same `[filament_group]` section twice | Remove or rename one |
| `bay <unit>-<n> is in both '<A>' and '<B>'; a bay may belong to only one filament_group.` | A bay is in two groups | Remove it from one |
| `filament_group '<name>' spans FPS lanes <a> and <b>; a group's bays must share one FPS lane.` | A group mixes lanes | Split the group, or fix the unit's `fps:` |
| `[<kind> <name>] must set 'fps:' when several [fps] lanes are defined.` | More than one `[fps]` and no `fps:` on the unit | Add `fps:` |
| `[oams <name>] references unknown fps '<lane>'.` | `fps:` names a lane that does not exist | Fix the name |
| `unit index <n> is shared by '<a>' and '<b>'; oams_idx/unit_idx values must be unique across every [oams] and [follower].` | Two units claim the same index | Renumber |
| `No [fps] section found; OpenAMS requires at least one FPS lane.` | `[fps]` missing from `oams.cfg` | Add it |
| `f1s_hes_on requires one value (single-bay AMS HT) or four comma-separated values` | A threshold list has the wrong length | Use one or four values |
| `Invalid filament_group bay '<x>' in [filament_group <n>]; expected '<oams_name>-<bay_index>' (e.g. oams1-0)` | A malformed `group:` entry | Use `<unit>-<bay>` |
| `unknown device_kind '<x>' (expected auto, one of the AMS families, or one of the klipper-mmu devices: …)` | A typo in `device_kind:` | Use `auto`, `ams1`, `ams_ht` or `ams2` |
| `[<name>] microsteps must be a power of two <= 256` | Bad follower `microsteps:` | Use 1, 2, 4, 8, 16, 32, 64, 128 or 256 |

## Runtime errors

| Symptom | Likely cause | Fix |
|---|---|---|
| `OpenAMS is not ready` | A command ran before the manager finished its startup resync | Wait for Klipper to settle; check `OAMSM_SELFTEST` |
| `Group <name> does not exist` | No `[filament_group <name>]` section | Add it, or fix the name |
| `Group <name> has no bays assigned` | The group's `group:` list is empty | Add bays with `OAMSM_ASSIGN_BAY` or in the config |
| `OpenAMS slot <n> does not exist` | The bay index is out of range for that unit | Check the bay count; followers have one bay |
| `<bay> is not assigned to group <name>` | The slot does not belong to the group you named | Pick a slot from that group |
| `OpenAMS motion unavailable for <name>` | The unit lost its host session, went silent, or its firmware does not allow motion | Run `OAMSM_SELFTEST` for the reason it gives. After a lost session an AMS on a Cortex Bridge reconnects by itself within seconds; an OpenAMS mainboard needs `FIRMWARE_RESTART` |
| `host communication lost: the unit stopped; OpenAMS is reconnecting it`, then `OpenAMS: <unit> is connected again after the host stalled; RESUME continues the print` | Klipper was busy for longer than the AMS's 4 s host watchdog, so the AMS stopped and the print paused, as designed. OpenAMS configures it again by itself; nothing moves until you resume | Press **RESUME**. The loaded lane's follower comes back by itself: while paused, or with the print once it runs. To find what held Klipper, see the next row |
| `OAMS: <unit>: the follower could not be re-armed; RESUME retries` (the print pauses) | The print needs the lane's forward follower, and the unit refused it for about a minute (a Cortex Bridge refuses it while a fault of the unit is latched) | Clear what the unit reports (**Clear errors**, `OAMSM_CLEAR_ERRORS`), then press **RESUME**. OpenAMS keeps trying while paused, and RESUME tries again |
| `OpenAMS: the host stalled for <n> s; a unit's host watchdog may have fired` in `klippy.log` | Something held Klipper's main loop that long. The `Reactor ... busy for <n> with: ...` line just before it names it. One known cause: an older gco-routines checked a large print file on the main loop when the print started | Update the extra that the busy line names (for gco-routines, `git -C ~/gco-routines pull`, then restart Klipper) |
| `host communication lost: the unit stopped and needs FIRMWARE_RESTART` | The same stall, on a unit that cannot be reconnected without a restart (an OpenAMS mainboard) | Press **FIRMWARE RESTART**, then resume or restart the print |
| `OpenAMS hardware snapshot unavailable for <name>` | The unit has not reported its sensors yet | Wait; then check the wiring and `OAMSM_SELFTEST` |
| `Unknown FPS '<name>'` | A `FPS=` value names no lane | Use the `[fps ...]` section's short name |
| `Multiple FPS present; specify FPS=<name>` | Several lanes and no `FPS=` given | Add `FPS=` |
| `OpenAMS: the toolchange to <group> failed at the <step> step: <reason>. The print is paused` | A toolchange in a print failed; the print paused instead of ending | Fix what the reason names, then press **RESUME**: it finishes the toolchange from where it stopped, then the print goes on ([how](macro-sequence.md#when-something-fails)) |
| `OpenAMS could not finish the toolchange to <group> ...: <reason>. The print stays paused` | RESUME retried and it failed again | Fix the cause and press **RESUME** again; to give up, cancel the print |
| `... the retry ended with nothing is loaded on lane <lane>, not <group>` (or `<other> loaded`) | The retry ran without an error but the lane does not hold the group it was for (a `_TX` of your own that moved nothing, say), so it does not count | Find out why nothing was loaded, then press **RESUME** again |
| `OpenAMS could not pause the print for the toolchange to <group>, so its error ends the print` | A toolchange failed in a print and nothing could pause it: no `[pause_resume]`, or a `PAUSE` macro that does not pause | Add `[pause_resume]` (Mainsail's `mainsail.cfg` has it) or fix the `PAUSE` macro. The print ended on purpose: printing on without the filament it asked for would ruin it |
| `OpenAMS: this printer has no [pause_resume] section ...` at startup | `[pause_resume]` is missing, so a failed toolchange cannot pause a print and ends it (the shipped macros also need it) | Add `[pause_resume]`, or include Mainsail's `mainsail.cfg` |
| `OAMSM_CLEAR_ERRORS: a fault stayed latched after the clear on <unit>` | The unit refused or did not recover within its 15 s recovery window (a fault can look cleared, then latch again) | Check the unit (filament path, its LEDs), then Reset again; a power cycle of the AMS clears what it cannot |
| `OAMSM_CLEAR_ERRORS: the toolchange to <group> on lane <lane> is not finished` | Reset keeps an unfinished toolchange on purpose | In a print, **RESUME** finishes it; from the panel, **Retry** finishes it. A panel Load or Unload that failed before it started is cleared instead (`had not started; cleared`) |
| `OpenAMS cannot retry the failed toolchange: the printer lost its position` | The motors were turned off while the print waited (the idle timeout), so RESUME, or `OAMSM_RETRY` of any failed toolchange, would have to home with the part on the bed | The print cannot be resumed safely: cancel it. `pause_idle_timeout` in `[oams_manager]` (48 hours by default) is how long a paused print keeps its motors and heaters on |
| `OpenAMS will not run the toolchange to <group> while the print is paused: the printer lost its position` (or `the unload`) | A `T<n>`, the panel's Load or Unload while a print is paused and its motors were turned off: it would home with the print on the bed | Nothing was moved. Cancel the print first; then load or unload |
| `OpenAMS: the print needs <group> loaded on lane <lane>` | RESUME found the toolhead empty, or holding another group than the print was using | Load that group (`T<n>`, or the panel), then press **RESUME** |
| `OAMSM_RETRY: a print is printing` | A retry was sent while the print runs | Pause the print first, or wait for it to end |
| RESUME says `Resume aborted` (no filament detected) after a failed toolchange | Mainsail's or Fluidd's `RESUME` macro has a `runout_sensor` and stops before Klipper's resume, so the retry never runs | Run `OAMSM_RETRY` or press the panel's **Retry**, then **RESUME** |
| `OpenAMS could not find Klipper's resume command` at startup, or `RESUME cannot retry it on this Klipper` in a pause message | OpenAMS could not put its retry in front of Klipper's resume | After a failed toolchange pauses the print, run `OAMSM_RETRY`, then **RESUME** |
| `Unknown OpenAMS setting '<name>'` | A bad `OAMSM_SET_OPTION` name | Use one of the six policy switches |
| `Unknown OAMS '<name>'.` | `OAMS=` on a group or spool-link command names no unit | Use the unit's name (`oams1`), not its index |
| `Refusing to unload: <bay> is loaded on this OAMS, not <bay>` | `SPOOL=` names a bay other than the loaded one | Drop `SPOOL=`, or name the loaded bay |
| `OpenAMS cannot heat <extruder>: minimum_extrude_temperature <a> is above the temperature ceiling <b>. ...` | The flow has nowhere legal to heat: the floor is above `max_temp - 5` or above `max_operation_temperature` | Lower `minimum_extrude_temperature`, raise `max_temp` or `max_operation_temperature`, or check the `[extruder]` name |
| `Filament not detected at toolhead extruder inlet` | The inlet switch did not trigger | Clear the jam, then retry; this raised because the request was `STRICT=1` |
| `<unit> Spool <n> has no PTFE length; ...` | `ptfe_length` (or `ptfe_length_<bay>`) is 0 for that bay, so the load was refused before anything moved | Do what the message says: `OAMS_CALIBRATE_PTFE_LENGTH OAMS=<n> SPOOL=<bay>` (`SPOOL` counts from 0) on OpenAMS 1 and on a Cortex Bridge with firmware 8 or later; older bridge firmware can't, so measure the path and set it ([details](cortex-bridge-setup.md#filament-path-length)) |
| `<unit> Spool <n> reached the toolhead after <n> mm of filament path (ptfe_length: <n> mm). To use the measured length, ...` | Information: a Cortex Bridge AMS (firmware 8 or later) measured the path on this load, and it differs from the length in force (`ptfe_length`, or the bay's own `ptfe_length_<bay>`, which the message names then) | Run the command it names, or set the value it names; a length far from the path stops loads short or reloads late. When it also says `and remove ptfe_length_<bay>`, the AMS config still has a bay's own length from an older calibration, which overrides `ptfe_length`: remove those lines too, or the bay keeps its old length (the calibration command removes them itself) |
| `PTFE calibration of <unit> Spool <n> failed: <reason>` | A Cortex Bridge AMS (firmware 8 or later) stopped the calibration and says why | `fed <n> mm (MAX_LENGTH ...)`: the path is longer, or the filament isn't reaching the toolhead; run again with a larger `MAX_LENGTH=` once the path is clear. `stopped moving for 4 s`: a snag. `toolhead buffer never reached arrival_threshold`: see [configure](configure.md#host-confirmed-arrival-from-a-relayed-fps) |
| `Cannot calibrate <unit> Spool <n>: filament is still out past a hub on this lane; rewind it first with OAMSM_UNLOAD_FILAMENT.` | A load or calibration stopped short and left a bay's filament out | `OAMSM_UNLOAD_FILAMENT` rewinds it (it never reached the extruder), then calibrate again |
| `Cannot calibrate <unit> Spool <n>: a spool is loaded on this lane; unload it first (with the cut, as a toolchange does).` | The lane holds a loaded spool; a calibration feeds from an empty path | Unload it the normal way, through the cut (a toolchange's unload, or the screen's unload) |
| `Spool loading failed (the AMS stopped feeding before the toolhead buffer reached arrival_threshold: ...)` | A Cortex Bridge AMS stops a load that feeds about 200 mm past `ptfe_length` without the buffer compressing, or whose filament stops moving | Look where the tip stopped. Running freely but short of the extruder gears: `ptfe_length` is too short ([set it](cortex-bridge-setup.md#filament-path-length)). Stuck at a joint or bend: fix the path. Then press **Clear errors** (`OAMSM_CLEAR_ERRORS`) |
| `Spool loading failed (the toolhead buffer never reached arrival_threshold; ... motion_timeout ...)` although the filament reached the toolhead and the buffer is compressed | OpenAMS before 2026-11-08 (evening): the first load after a Reset (`OAMSM_CLEAR_ERRORS`, or the Reset a retry runs) could reuse a generation, and its arrival was never confirmed; the AMS slows to a creep at the toolhead and times out | Update OpenAMS. Meanwhile an unload before the load avoids it. Then press **Clear errors** (`OAMSM_CLEAR_ERRORS`) |
| The toolhead drives into a corner and hits the frame when a toolchange fails, and later cuts miss the cutter | OpenAMS before 2026-11-08 (evening): a toolchange that had to home first moved back to the position from before homing (0, 0, 0 after a restart) | Update OpenAMS, then home again (`G28`): the crash cost the toolhead its position |
| `Spool loading failed (the filament fed <n> mm (ptfe_length <n> mm + 200 mm) without the toolhead buffer reaching arrival_threshold: ...)` | Cortex Bridge firmware 8 or later: the load ran past `ptfe_length` without arriving | If the filament ran freely and stopped short of the gears, `ptfe_length` is too short: `OAMS_CALIBRATE_PTFE_LENGTH OAMS=<n> SPOOL=<bay>`. Then press **Clear errors** (`OAMSM_CLEAR_ERRORS`) |
| `Spool unloading failed (the filament did not move back: ...)` | Cortex Bridge firmware 14 or later: after the cut, the AMS's odometer stood still while it pulled, so the bridge stopped the AMS at once. Almost always the cut did not sever the filament, or the cut end is caught in the toolhead | Check the cutter (its blade, and its travel: `cut_x`/`cut_y` in `_oams_macro_variables`). Then Retry: it cuts again before the AMS pulls. A filament sensor between the extruder and the hotend (`fs_extruder_out`) catches a failed cut before the AMS pulls at all. Firmware 12 and older pulled for 60 s first and said `no filament progress (jam, dead motor, or missing sensor)` |
| `OpenAMS stopped the unload: the unit's reverse follower stopped (...)` | The AMS was pulling the filament back as the extruder retracted (its reverse follower) and stopped: with Cortex Bridge firmware 15, `the AMS could not keep pulling the filament back while the extruder retracted` means its pull-backs did not bring the toolhead buffer down; otherwise the AMS reported its own error or stopped answering. OpenAMS stops before the next retract, so the extruder does not push against an AMS that no longer takes the filament | Check the path between the AMS and the toolhead for a snag or a kink, and that the spool turns freely. Then Retry |
| `the unit did not confirm the reverse follower within 4.5 s` | The AMS never answered the request to pull back before the extruder retracts (or its link dropped meanwhile); nothing retracted | Check the AMS's connection and power (`support report` shows the link), then Retry |
| `... the AMS reported a jam (error 0x17) ...` | The AMS's own feeder stalled (on a Bambu printer this is its jam dialog): a spool that turns hard, such as a cardboard spool or a tangle, or filament caught near the AMS | Free the spool or the filament, then Retry. Bambu recommends an adapter ring for cardboard spools in the AMS |
| `... the AMS reported a feeder motor fault (error 0x12) ...` | A feeder motor of the AMS could not turn. Seen on an AMS 2 Pro pulling filament back while its end was held at the toolhead; filament wound tight on the spool can do the same | Free the filament at the toolhead or the spool, then Retry |
| `nothing loaded` from `OAMSM_UNLOAD_FILAMENT`, or `filament still detected in a hub` from a load, after a load or calibration that stopped short | The bay's filament is still out past its hub, but the lane counts as unloaded | On a Cortex Bridge AMS, `OAMSM_UNLOAD_FILAMENT` rewinds that bay; the filament never reached the extruder, so no cut is needed. Otherwise pull it back by hand |
| `AMS load reached the toolhead, but <MACRO> failed: <reason>. Filament remains loaded and forward following remains armed.` | The extruder callback failed after a successful AMS load | Fix the callback. Do not force an unload before knowing the toolhead state |

## Runout and calibration

| Symptom | Likely cause | Fix |
|---|---|---|
| `ptfe_length is not calibrated (0); cannot auto-load the next spool. To fix it, ...` | `ptfe_length` is still 0 | Do what the message says: `OAMS_CALIBRATE_PTFE_LENGTH OAMS=<n> SPOOL=<bay>` on OpenAMS 1; on a Cortex Bridge, [measure and set it](cortex-bridge-setup.md#filament-path-length) |
| `filament runout on group <name> and no spare spool available` | Every other bay in the group is empty or not ready | Insert filament, or add a bay to the group |
| `filament runout on group <name> and the spare spool is not calibrated (<bay>); ...` | The spare bay has no path length | Set that bay's length as the message says |
| `filament runout on group <name> (auto reload is off)` | `auto_reload_on_runout` is False | `OAMSM_SET_OPTION NAME=auto_reload_on_runout VALUE=True` |
| `Cannot calibrate PTFE length: filament is already loaded at the hub or follower mode is active. Run OAMS_UNLOAD_SPOOL OAMS=<n> before retrying.` | The bay is loaded | `OAMS_UNLOAD_SPOOL OAMS=<n>`, then retry |
| `Cannot calibrate PTFE length: no filament was detected in <bay>. Insert filament in that spool position and retry.` | The named bay is empty | Insert filament |
| `Cannot calibrate PTFE length: OAMS is busy with another operation.` | Another command owns the unit | Wait, then retry |
| `PTFE calibration timed out in firmware after <n> s … The filament did not reach the expected sensor state.` | The filament stopped short: a kink, a jam, or friction in the PTFE path | Fix the path, unload, then retry |
| `Hub HES calibration is unsupported by this unit` | The firmware does not offer it | Skip it; set `hub_hes_on` in the config |
| `Calibration of HES <n> failed` | The bay could not be calibrated | Put filament in that bay and retry |
| `follower <name> path_length is 0 (runout will pause instead of auto-reloading)` | The inline follower is uncalibrated | Measure and set `path_length` |

## Sensors and hardware

| Symptom | Likely cause | Fix |
|---|---|---|
| `fps_value outside 0..1` | The FPS pin is wrong or the board is unpowered | Check the `[fps]` `pin:` and the wiring |
| FPS above 0.8 on an unloaded lane | The reading is reversed | Toggle `reversed:` in `[fps]`; the manager warns once it has seen ten such samples |
| `NOT CONNECTED` for a unit in `OAMSM_SELFTEST` | The unit's MCU is not answering OpenAMS commands | Check its UUID, power and CAN cabling |
| `REFUSED: <reason>` for a unit | The firmware published a connect-time refusal | Read the reason. It names the contract break; fix the firmware, not the host |
| `mixed OAMS firmware protocol versions [...]` | Units run different protocol versions | Bring every unit to the same release: [update the Cortex Bridge](cortex-bridge-update.md), or flash the same mainboard firmware on every OpenAMS 1 board |
| A reader chip does not answer (L5 WARN) | Wiring, or a dead reader | Check `cs_pin_*`/`reset_pin_*`; the mainboard needs firmware 2.0.25 or newer |
| `No RFID reader configured for OAMS=<n> RFID_CARD=<0\|1>` | No `[mfrc522]` section, or `oams:` does not match | Fix `oams:` or add the section |
| `[<name>]: extruder '<x>' not found; check the 'extruder:' option` | `extruder:` names a nonexistent extruder | Fix the name |
| `[<name>]: source_oams '<x>' was not found` | `source_oams:` names no unit | Fix the name, or use `pin:` |
| A pause with `motion_timeout`, `motion_stalled`, `follower_buffer_invalid`, `follower_filament_lost`, `follower_starved` or `follower_stalled` | The lane's follower lost the filament | Clear the path, then `OAMSM_CLEAR_ERRORS` |
| `<unit>: cleared the motion_timeout fault left by an earlier failure; the unload that just finished worked` | An unload that works shows the AMS moves again, so OpenAMS clears the motion fault the AMS latched for the failure before it (it would refuse the next load). A dryer or system fault is never cleared this way | Nothing. If it says `could not clear`, press **Clear errors** (`OAMSM_CLEAR_ERRORS`) |
| Mainsail still says to FIRMWARE_RESTART after a bridge or AMS firmware update | Klipper was restarted while an AMS was still coming back | Press **FIRMWARE RESTART** in Mainsail. It is expected now and then, and harmless |
| `T0 ran out; no spare spool is left` (or another group) with plenty of filament on the spool, or an AMS that doesn't push while printing, on the first print after an AMS or the Cortex Bridge restarted with filament loaded (a power cycle, a firmware update) | The Cortex Bridge forgot which spool was loaded while it or the AMS restarted. Bridge firmware 28 picks it up again by itself, and OpenAMS now reads the AMS HT's own sensor | [Update the Cortex Bridge](cortex-bridge-update.md) to firmware 28 or later and update OpenAMS. Until then, unload and load that spool again before printing |
| `Update failed: unknown method 'ams_update'` from `cortex-bridge.py ams-firmware` | The bridge relay still runs the OpenAMS code from before an update: an update by `git pull`, or a `[update_manager openams]` entry without `managed_services`, does not restart it | Run the command again: it offers to restart the relay. Or `sudo systemctl restart cortex-bridge`, then **FIRMWARE RESTART** in Mainsail. Run `./install.sh` once to add the relay to the update entry |
| `<unit> is already drying (timed_dry, <n> min left); to change the temperature, the time or Rotate, stop it first (OAMS_DRYER_STOP)` | A dry already runs on that AMS, for example when Rotate is switched on during a print | Nothing, if it should go on. To change it: `OAMS_DRYER_STOP OAMS=<n>`, then start again |
| `<unit> cannot turn its spools during a print; start the dryer without Rotate (ROTATE=0)` | Spools never turn while a print runs or is paused | Start the dry without Rotate. An AMS HT on firmware 04.x or later dries while you print |
| `<unit> is drying with its spools turning, so it cannot feed; stop the dryer (OAMS_DRYER_STOP), or start it again without Rotate, before loading <group>` | A turning spool is free, with its filament back out of the AMS's first stage | Stop the dryer, or restart it without Rotate, then load |
| `<unit> is drying and does not feed; stop the dryer (OAMS_DRYER_STOP) before loading <group>` | This AMS does not feed while it dries (an AMS 2 Pro, or an AMS HT on firmware 03.x) | Stop the dryer, or wait for it to finish, then load |
| A fault says `Clear errors did not clear it: power the AMS off and on`, or Clear errors answers `... still latched after the clear` | The AMS keeps its own error, or (Cortex Bridge firmware 20 and older) it still reports moving and refuses the clear | Power the AMS off and on (an AMS HT or AMS 2 Pro also needs its power adapter unplugged), then press **Clear errors** again. Cortex Bridge firmware 21 stops a unit that still reports moving before it clears |
| A failed Load or Unload from the panel stays as `not finished` after Clear errors | Clear errors keeps it so Retry can finish it from where it stopped | **Retry** finishes it, **Unload** backs it out, **Abandon** (`OAMSM_ABANDON`) forgets it and leaves the filament where it stopped |

## Installer errors

| Symptom | Likely cause | Fix |
|---|---|---|
| `This script must not run as root. Exiting.` | Run with `sudo` | Run it as the Klipper user; it calls `sudo` itself |
| `[ERROR] --spoolman-url and --no-spoolman cannot be combined.` | Two conflicting flags | Pick one |
| `[ERROR] --skip-gco-routines and --require-gco-routines cannot be combined.` | Two conflicting flags | Pick one |
| `[ERROR] --with-klipper-patches and --remove-klipper-patches cannot be used together.` | Two conflicting flags | Pick one |
| `[ERROR] -u already removes the Klipper patches if they are applied; drop --remove-klipper-patches.` | `-u` with that flag | Drop the flag |
| `<path>/klippy/extras/klipper_mmu.py exists and would compete with the klipper_mmu Klipper extra; remove it first.` | A previous MMU install owns the name | Ask what installed it, then remove it |
| `[WARNING] the openams_spoolman component is not linked into Moonraker (no checkout at "…")` | No Moonraker checkout found | Spool editing stays off; run the installer again with `-c` set |
| `[WARNING] gco-routines: this Klipper is not a version it accepts` | gco-routines was not tested against this Klipper's `klippy/gcode.py` | Nothing is wrong. Leave `[gco_routines]` out; OpenAMS works fully without it |
| `[WARNING] …` about a gco-routines directory | A non-Git or diverged checkout | Left alone on purpose; migrate it yourself |
| `[skipped: OPENAMS_NO_SERVICE=1] systemctl …` | The test switch is set | Unset `OPENAMS_NO_SERVICE` |
| `[WARNING] Spoolman is installed but does not answer at …` | Spoolman's own installer failed part-way (it still prints "installed successfully") | `sudo journalctl -u Spoolman -n 50 --no-pager` shows why; run `./install.sh` again, which installs this host's Spoolman again when it does not answer |
| `Installing the OpenAMS Mainsail … [SKIPPED] … is a link or a git checkout` | Your Mainsail is your own build | Build the panel into it ([openams-ui web panel guide](https://github.com/OpenAMSOrg/openams-ui/blob/main/docs/guide/web-panel.md)), or replace it with a plain Mainsail and run `--mainsail-panel` again |

## The Mainsail panel and Spoolman

| Symptom | Likely cause | Fix |
|---|---|---|
| No OpenAMS panel after `./install.sh --mainsail-panel` | The browser still shows the cached old Mainsail, or the panel sits at the end of the dashboard's first column | Reload with **Ctrl+Shift+R** (Cmd+Shift+R on a Mac) and scroll down; move it under **Settings → Dashboard**. `cat ~/mainsail/release_info.json` must name `OpenAMSOrg`; if it names `mainsail-crew`, run `--mainsail-panel` again |
| The panel says "No OpenAMS units are connected" | The printer runs `klipper_openams`, or OpenAMS did not load | Install this OpenAMS ([moving from klipper_openams](install.md#moving-from-klipper_openams)), then **FIRMWARE RESTART** |
| No spool colors or materials; Spoolman shows as offline | Spoolman is not running, or Moonraker does not know it | `curl -s localhost:7912/api/v1/info` must print a line; if not, see the Spoolman installer row above. Then restart Moonraker from Mainsail |
| After a Klipper restart a tagged bay shows "Tag not read yet" instead of "Tag seen, slot not confirmed" | The tag cache could not be written or read | `openams_tag_cache.json` lives next to `oams.cfg`, or next to `printer.cfg` when there is no `oams.cfg`; that folder must be writable by Klipper. The Spoolman link stays; the tag is read again at the bay's next load |
| A bay shows "Two spools answered", "A different spool's tag appeared here" or "This tag is also in slot …" | Two tags moved while one spool turned, or one tag seems to be in two bays | Load the spool, or press **It is this slot** (`OAMS_RFID_SCAN OAMS=<n> BAY=<bay> ASSERT=1`) while only this spool's tag faces the reader. A Re-read settles it only once the reads no longer disagree, for example when the other bay no longer holds the tag |
| A Bambu spool's tag shows as an unknown tag and Spoolman links it only by its UID | Its decode failed three times (the tag rested at the edge of the reader's field, or the spool was moving) | Press **Re-read**, or take the spool out and put it back: both try the decode again, and so does the tag coming back into the field |

## Update and uninstall

Update with `git -C ~/openams pull --ff-only` and then `./install.sh`, then re-run
the ladder. Uninstall with `./install.sh -u`, which removes the links, the
include line and the Moonraker sections, and asks before deleting your
`oams*.cfg` files. Spoolman, its data and gco-routines stay. The full procedures
are in [install](install.md#update).

## Gotchas

- `OAMSM_SELFTEST` is the fastest first move. Everything else is a guess until
  you have seen its output.
- `FIRMWARE_RESTART` is a recovery tool, not a silencer. Find out why the MCU
  shut down first.
- `SAVE_CONFIG` cannot edit an included subfile, which is why group edits and
  calibration write in place. Set `openams_config_path:` if they land somewhere
  unexpected.

## Where to go next

- [install.agents.md](../../install.agents.md) — the same checks as a scripted run.
- [docs/contracts/](../contracts/) — the interface contracts behind these errors.
- [Architecture](../../ARCHITECTURE.md) — how the pieces fit.
- [install](install.md), [configure](configure.md), [using](using.md), [FAQ](faq.md).