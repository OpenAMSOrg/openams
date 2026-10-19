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
`cortex-bridge.py support` writes a report to attach to a support request.

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
| WARN | `W_FOREIGN_MMU`, `W_TOOL_MACRO` |
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
| `OpenAMS motion unavailable for <name>` | The unit lost its host session, went silent, or its firmware does not allow motion | Run `OAMSM_SELFTEST` for the reason it gives; after a lost session, `FIRMWARE_RESTART` |
| `OpenAMS hardware snapshot unavailable for <name>` | The unit has not reported its sensors yet | Wait; then check the wiring and `OAMSM_SELFTEST` |
| `Unknown FPS '<name>'` | A `FPS=` value names no lane | Use the `[fps ...]` section's short name |
| `Multiple FPS present; specify FPS=<name>` | Several lanes and no `FPS=` given | Add `FPS=` |
| `Unknown OpenAMS setting '<name>'` | A bad `OAMSM_SET_OPTION` name | Use one of the six policy switches |
| `Unknown OAMS '<name>'.` | `OAMS=` on a group or spool-link command names no unit | Use the unit's name (`oams1`), not its index |
| `Refusing to unload: <bay> is loaded on this OAMS, not <bay>` | `SPOOL=` names a bay other than the loaded one | Drop `SPOOL=`, or name the loaded bay |
| `OpenAMS cannot heat <extruder>: minimum_extrude_temperature <a> is above the temperature ceiling <b>. ...` | The flow has nowhere legal to heat: the floor is above `max_temp - 5` or above `max_operation_temperature` | Lower `minimum_extrude_temperature`, raise `max_temp` or `max_operation_temperature`, or check the `[extruder]` name |
| `Filament not detected at toolhead extruder inlet` | The inlet switch did not trigger | Clear the jam, then retry; this raised because the request was `STRICT=1` |
| `AMS load reached the toolhead, but <MACRO> failed: <reason>. Filament remains loaded and forward following remains armed.` | The extruder callback failed after a successful AMS load | Fix the callback. Do not force an unload before knowing the toolhead state |

## Runout and calibration

| Symptom | Likely cause | Fix |
|---|---|---|
| `ptfe_length is not calibrated (0); cannot auto-load the next spool. Run OAMS_CALIBRATE_PTFE_LENGTH.` | `ptfe_length` is still 0 | Run `OAMS_CALIBRATE_PTFE_LENGTH OAMS=<n> SPOOL=<bay>` |
| `filament runout on group <name> and no spare spool available` | Every other bay in the group is empty or not ready | Insert filament, or add a bay to the group |
| `filament runout on group <name> and the spare spool is not calibrated (<bay>); run OAMS_CALIBRATE_PTFE_LENGTH SPOOL=<n>.` | The spare bay has no calibrated path length | Calibrate that bay |
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