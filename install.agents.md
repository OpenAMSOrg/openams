# Installing and bringing up OpenAMS: instructions for AI agents

This file is for an AI agent with shell access to a Klipper printer host.
Follow it to install OpenAMS, fit it into the printer's existing
configuration, prove that every layer comes up, and report what you did. Humans
should read [README.md](README.md) instead.

It is written so that a small model can follow it literally. Where judgment is
needed, the file says so, and says what to decide between.

---

## 0. How to use this file

- **Order.** Do the sections in order, 1 to 10. Do not skip a step. Do not
  reorder steps.
- **Every step has three parts:**
  - **Run:** the exact command.
  - **Expect:** what success looks like.
  - **If not:** what to do instead.
- **Run as the Klipper user** (usually `pi`), never as root, in a normal shell
  on the printer host. Commands assume the default paths below; section 1 tells
  you when they differ.
- **Keep a log.** Append every command you run and its full output to
  `~/openams-agent.log`. You hand it to the human at the end.
- **Words with a fixed meaning:**
  - **STOP:** stop working. Send the human the STOP report from section 10 and
    wait. Do not try workarounds.
  - **ASK:** ask the human the exact question given, and wait for the answer.
    Do not guess the answer.
  - **MUST / NEVER:** no exceptions.
- **Anything this file does not cover:** STOP. Never improvise a change to
  Klipper, Moonraker or the printer's configuration.

### The rules (never break them)

1. **NEVER act while the printer is printing or paused.** Check the print
   state (section 1, `PRINT_STATE`) before any step that restarts Klipper or
   changes configuration. If it is `printing` or `paused`: STOP.
2. **NEVER run as root.** NEVER run `install.sh` with `sudo`; it calls `sudo`
   itself where it must.
3. **NEVER make the printer move, heat or cut.** That means `G28`, `G1`, `T0`
   to `T19`, `M104`, `M109`, `OAMSM_LOAD_*`, `OAMSM_UNLOAD_*`, `OPENAMS_LOAD`,
   `OPENAMS_UNLOAD`, `SAFE_UNLOAD_FILAMENT`, `CUT_FILAMENT`, `OAMS_CALIBRATE_*`,
   `OAMS_*_TORTURE_TEST`, `OAMS_FOLLOWER*`, `OAMS_PID_AUTOTUNE`, or any macro you
   have not read.
   - The only printer command you may send is `OAMSM_SELFTEST`, which is
     read-only.
   - Motion needs a human watching the machine (section 9).
4. **NEVER flash firmware or change the CAN bus.** No `make flash`, Katapult,
   `flashtool.py`, `dfu-util` or `stm32flash`. Never change a bitrate or an
   interface. On a Cortex Bridge printer, never run `cortex-bridge.py update`
   unless the human explicitly asks for a bridge firmware update.
5. **NEVER invent a value.**
   - A `canbus_uuid` comes from the bus query (section 6.2).
   - A serial path comes from `/dev/serial/by-id/`.
   - The Bambu tag `key` comes from the human only. NEVER search for it.
   - Cutter coordinates come from the human or from the printer's existing
     config.
6. **Back up before any change** (section 2). NEVER delete a file. To disable
   a config section, comment it out and add `# openams-agent: <why>` above it.
7. **Edit only the files a step names:** `oams.cfg` and, in section 6.5 only,
   `printer.cfg`.
   - NEVER edit Klipper's or Moonraker's source.
   - NEVER run `git reset`, `git checkout`, `git clean`, `git stash` or
     `git commit` in any repository except `git pull --ff-only` in `~/openams`.
8. **NEVER pass `--with-klipper-patches` or `--with-mcu-addon`** unless the
   human asked for that flag by name.
9. **One change at a time.** After each change, run the checks the step names
   before the next change.
10. **Two tries, then STOP.** If the same step fails twice after a fix, STOP.

---

## The words you need

| Word | What it is | Where |
|---|---|---|
| Klipper | the printer firmware's host side (Python), a systemd service, usually `klipper` | `~/klipper`, log `~/printer_data/logs/klippy.log` |
| Moonraker | Klipper's web API (port 7125); every check here goes through it | `~/moonraker`, `~/printer_data/config/moonraker.conf`, log `~/printer_data/logs/moonraker.log` |
| config dir | Klipper's configuration files | `~/printer_data/config` (old installs: `~/klipper_config`) |
| `[include X]` | pulls file X into `printer.cfg`; a section defined twice is merged and the later option wins, **silently** | any `.cfg` |
| extras | Klipper's plugin directory; OpenAMS installs itself there as symlinks | `~/klipper/klippy/extras/` |
| MCU | a microcontroller Klipper talks to: the printer board, a toolhead board, each AMS unit, the FPS board | `[mcu NAME]` sections |
| CAN, `canbus_uuid` | the bus most OpenAMS boards use, and each board's 12-hex-digit id on it | `can0`, `[mcu NAME] canbus_uuid:` |
| Cortex Bridge | a USB board that connects **stock** Bambu Lab AMS units (no OpenAMS boards, no CAN). Its relay service `cortex-bridge` gives each AMS a serial port `/dev/oams/<uid>` | `lsusb` shows `1a86:fe11`; `[mcu NAME] serial: /dev/oams/<uid>` |
| AMS unit | one AMS as Klipper sees it: an MCU (an OpenAMS mainboard, or one of a Cortex Bridge's per-AMS ports) plus up to 4 bays (AMS HT: 1). In config: `[mcu oams_mcuN]` and `[oams oamsN]` | `oams.cfg` |
| FPS | the filament pressure sensor between AMS and toolhead; reads 0..1 | `[fps]` or `[fps NAME]` |
| lane | one FPS and the extruder it feeds; units on the same lane can back each other up | `[oams] fps:` |
| bay | a spool slot, named `<unit>-<index>`, for example `oams1-0` | |
| filament group | a named list of bays; the slicer's tool `T0` loads group `T0` | `[filament_group T0] group: oams1-0` |
| `T0`..`T3` macros | the toolchange commands; `oams_macros.cfg` defines `T0`-`T3`, more need adding (section 6.4) | `oams_macros.cfg` |
| `oams_manager` | OpenAMS's coordinator; its status object says whether OpenAMS is ready | `[oams_manager]` |
| Spoolman | a spool database server (port 7912); optional, the installer sets it up by default | `~/Spoolman` |
| gco-routines | an optional Klipper extra the installer also clones; installing it changes no behavior. Enabling it works only on a Klipper it was tested against (`GCO_BASELINE`); on any other, Klipper stops at startup | `~/gco-routines` |

### What `install.sh` changes

Everything else on the printer is untouched.
- **Links:**
  - one symlink per `~/openams/src/*.py` into `~/klipper/klippy/extras/`;
  - `extras/klipper_mmu` pointing to `~/openams/klipper_mmu/host`;
  - `~/klipper/scripts/<name>.py` for each `~/openams/scripts/*.py`;
  - `~/moonraker/moonraker/components/openams_spoolman`.
- **Copied only if absent:** `oams.cfg` (from `oams_sample.cfg`) into the config dir.
- **Symlinked:** `oams_macros.cfg` and `oams_macros_ordered.cfg` in the config dir point at the checkout, so updates take effect. An existing unmodified copy (any version OpenAMS shipped) is replaced by the link; a customized one is moved to `<name>.bak-<YYYYmmdd-HHMMSS>` first and the installer prints a message. Never edit these files.
- **`printer.cfg`:** one line `[include oams.cfg]`, above `[printer]`. Only if absent.
- **`moonraker.conf`:** `[update_manager openams]`, `[spoolman]` and `[openams_spoolman]`, each only if absent.
- **Spoolman:** if there is no Spoolman server, it installs one in `~/Spoolman` as the `Spoolman` service.
- **gco-routines:** it clones `~/gco-routines` and links it as `extras/gco_routines`.
- **Services:** it stops Klipper while it works and starts it at the end. It restarts Moonraker.

> **After a fresh install, Klipper reports an error. This is expected.** The new
> `oams.cfg` still holds placeholders like `<your_unique_FPS_UUID>`. Section 6
> fills them.

---

## 1. Get the code and look at the printer

### 1.1 Get or update the checkout

**Run:**
```bash
if [ -d ~/openams/.git ]; then
  git -C ~/openams status --short && git -C ~/openams pull --ff-only
else
  git clone https://github.com/OpenAMSOrg/openams.git ~/openams
fi
```
**Expect:** a clone, or `Already up to date.` / a fast-forward.
**If not:**
- `git status` lists modified files in `~/openams`: ASK "~/openams has local
  changes (list them). May I keep them and skip the update?" Never discard them.
- The clone fails: check the network with `curl -sI https://github.com`. If it
  fails, STOP.

### 1.2 Preflight (read-only)

**Run:** `bash ~/openams/tools/agent/preflight.sh | tee ~/openams-agent-preflight.txt`

**Expect:** `KEY=value` lines ending with `VERDICT=OK`, `VERDICT=ASK` or
`VERDICT=STOP`.

**If not:**
- `VERDICT=STOP`: STOP. Quote every `STOP_REASON=` line.
- `VERDICT=ASK`: ask the human every `ASK=` line, one at a time, then continue.

Read these keys. They decide later steps:

| Key | Value | What it means and what to do |
|---|---|---|
| `PRINT_STATE` | `printing`, `paused` | STOP (rule 1). `standby`, `complete`, `cancelled`, `error`: go on |
| `KLIPPER_SERVICES` | more than one | several printers on one host. ASK which one; then use `-s`, `-k`, `-c` in section 3 |
| `KLIPPER_PATH`, `CONFIG_DIR` | not `~/klipper`, `~/printer_data/config` | pass `-k KLIPPER_PATH -c CONFIG_DIR` to `install.sh` |
| `KLIPPER_FLAVOR` | `kalico` | supported; nothing changes. gco-routines may not be: read `GCO_BASELINE` |
| `KLIPPY_ENV_PYTHON` | below 3.9 | gco-routines will be skipped; add `--skip-gco-routines` |
| `GCO_BASELINE` | `no` | gco-routines does not accept this Klipper. Install as usual: installing it changes nothing. It stays off (section 6.4); say so in the report |
| `GCO_BASELINE` | `unknown` | `~/gco-routines` is not cloned yet. The installer prints the answer (section 3.2) |
| `GCO_ACTIVE` | `yes (<file>)` with `GCO_BASELINE=no` | gco-routines is enabled on a Klipper it refuses, so Klipper cannot start. The preflight asks; on yes, comment out `[gco_routines]` and `[include oams_macros_ordered.cfg]` (rule 6) |
| `OAMS_EXTRA` | `none` | **fresh install**: sections 2 to 9 |
| `OAMS_EXTRA` | `openams (...)` | **update**: sections 2, 3, 5, 7, 8, 10. Skip section 6 unless the audit (section 5) shows errors |
| `OAMS_EXTRA` | `broken (...)` | OpenAMS's link is there but its target is gone (the checkout moved or was deleted). Treat it as a fresh install: `install.sh` relinks |
| `OAMS_EXTRA` | `klipper_openams (...)` | **migration** from the old project. `install.sh` migrates links and the update entry by itself; the existing `oams.cfg` and macros are kept. Sections 2 to 10; in section 6, change only what the audit flags |
| `FOREIGN_MMU` | not empty (`AFC`, `HappyHare`, `ERCF`) | another MMU system. ASK "This printer has <value> configured. OpenAMS must not run beside another MMU system on the same toolhead. Should I stop here?" Default: STOP |
| `NETWORK_GITHUB` | `no` | add `--skip-gco-routines --no-spoolman` in section 3 |
| `SUDO_NOPASSWD` | `no` | ASK the human to run section 3's command themselves, then continue at section 4 |
| `CAN_INTERFACES` | empty | no CAN interface. If `lsusb` shows `1a86:fe11`, this is a Cortex Bridge printer: no CAN is needed; continue, and read section 6.2. Otherwise, if the AMS units connect over CAN (they normally do): STOP ("CAN is not set up on this host") |
| `CAN_<if>_STATE` | not `UP`/`UNKNOWN` | STOP: the CAN interface is down |
| `DISK_FREE_MB` | below 500 | STOP |
| `HAS_OAMS_CFG` | `yes` with `OAMS_EXTRA=none` | a leftover `oams.cfg`. ASK "There is an oams.cfg but OpenAMS is not installed. Keep it (it will be used) or should I move it aside?" |

---

## 2. Back up

**Run:**
```bash
TS=$(date +%Y%m%d-%H%M%S); CFG=$(grep '^CONFIG_DIR=' ~/openams-agent-preflight.txt | cut -d= -f2-)
tar -C "$CFG" -czf ~/openams-agent-backup-$TS.tgz . && \
ls -la ~/klipper/klippy/extras > ~/openams-agent-backup-$TS-extras.txt && \
ls -la ~/openams-agent-backup-$TS.tgz
```
**Expect:** a `.tgz` larger than 0 bytes. Write its path in your log; the report
needs it.

**If not:** STOP. Never continue without a backup.

---

## 3. Run the installer

### 3.1 Choose the flags

Start from `./install.sh` and add:

| Condition (from section 1) | Add |
|---|---|
| non-default paths or service | `-k <KLIPPER_PATH> -c <CONFIG_DIR> -s <service>` |
| `NETWORK_GITHUB=no` | `--skip-gco-routines --no-spoolman` |
| the human said "no Spoolman" | `--no-spoolman` |
| the human gave a Spoolman address elsewhere | `--spoolman-url http://HOST:7912` |
| `KLIPPY_ENV_PYTHON` below 3.9 | `--skip-gco-routines` |
| anything else | nothing |

### 3.2 Run it

**Check first:** `PRINT_STATE` is not `printing`/`paused`. Run section 1.2's
preflight again if more than a few minutes have passed.

**Run:**
```bash
cd ~/openams && ./install.sh <flags> 2>&1 | tee ~/openams-agent-install.log; echo "EXIT=${PIPESTATUS[0]}"
```
**Expect:** `EXIT=0`, and near the end `Starting Klipper... [OK]`.

**If not:** find the first `[ERROR]` or `[WARNING]` line in the log.

| Line starts with | Cause | Do |
|---|---|---|
| `[ERROR] Klipper service with name` | wrong service name | rerun with `-s <name from KLIPPER_SERVICES>` |
| `[ERROR] Klipper installation not found in directory` | wrong path | rerun with `-k <KLIPPER_PATH>` |
| `[ERROR] Moonraker configuration not found` | wrong config dir | rerun with `-c <CONFIG_DIR>` |
| `[ERROR] This script must not run as root` | you used sudo/root | rerun as the Klipper user |
| `[ERROR] ... klipper_mmu` (name clash) | a foreign `klipper_mmu.py` in extras | STOP |
| `[ERROR] --spoolman-url needs a URL` / `cannot be combined` | your flags | fix the flags, rerun |
| `[WARNING] gco-routines: this Klipper is not a version it accepts` | installed, and must stay off | note it for the report and go on. Never enable it |
| any other `[WARNING]` about gco-routines | optional part skipped | note it for the report and go on |
| `[WARNING] Could not download the latest Spoolman release` / Spoolman install failed | network or apt | note it and go on (OpenAMS works without Spoolman) |
| `Installation failed; attempting to restore the Klipper service.` | a step failed | STOP. Attach `~/openams-agent-install.log` |

The installer is safe to run again. It skips what is already done.

---

## 4. Wait for Klipper and read its state

Use this after every restart.

**Run:**
```bash
for i in $(seq 1 30); do
  S=$(curl -s http://127.0.0.1:7125/printer/info | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["state"])' 2>/dev/null)
  echo "klipper: ${S:-no answer}"; case "$S" in ready|error|shutdown) break;; esac; sleep 2
done
curl -s http://127.0.0.1:7125/printer/info | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["state_message"])'
```
**Expect:** `klipper: ready`.

**If not:**
- `error` right after a fresh install is expected. Go to section 5.
- `no answer` for 60 s: Moonraker is down. Run `systemctl status moonraker`
  and `tail -n 50 ~/printer_data/logs/moonraker.log`. You may run
  `sudo systemctl restart moonraker` once. If it still does not answer: STOP.

---

## 5. Audit the configuration (read-only)

Do this before changing any configuration, and again after every change.

**Run:**
```bash
CFG=$(grep '^CONFIG_DIR=' ~/openams-agent-preflight.txt | cut -d= -f2-)
curl -s http://127.0.0.1:7125/printer/gcode/help > /tmp/oams-help.json
if python3 -c 'import json; json.load(open("/tmp/oams-help.json"))["result"]' 2>/dev/null; then
  python3 ~/openams/tools/agent/config_audit.py --config-dir "$CFG" --commands /tmp/oams-help.json
else
  python3 ~/openams/tools/agent/config_audit.py --config-dir "$CFG"
fi | tee ~/openams-agent-audit.txt
```
- **Output:** one finding per line, `LEVEL CODE file:line message`, then
  `AUDIT errors=N warnings=N manual=N`.
- **Exit code:** 0 when there are no errors, 1 when there are errors.
- **`--commands`:** it is used only when Klipper answers. Without it, macro calls
  are reported as `M_MACRO_UNVERIFIED` instead of checked.

**Work through every ERROR, then every WARN.** Collect every MANUAL item for the report.

| Code | Meaning | What you do |
|---|---|---|
| `E_PLACEHOLDER` | a value still reads like `<your_unique_FPS_UUID>` | fill it in section 6 (UUIDs: 6.2; tag key: 6.3) |
| `E_INCLUDE_MISSING` | `[include X]` names a missing file | ASK "printer.cfg includes X, which does not exist. Was it removed on purpose?" Do not create it |
| `E_INCLUDE_CYCLE` | files include each other | STOP |
| `E_MCU_TRANSPORT` | `[mcu]` without `serial`/`canbus_uuid`, or a bad UUID | section 6.2 |
| `E_OAMS_MCU` | `[oams]` names an `[mcu]` that does not exist | fix the `mcu:` name to the `[mcu NAME]` you created in 6.2 |
| `E_UNIT_IDX_DUP` | two units share `oams_idx` | give each unit its own number, 1, 2, ... in `[oams]` order |
| `E_DEVICE_KIND` | bad `device_kind` | set `device_kind: auto` |
| `E_FPS_SOURCE` | an `[fps]` needs exactly one of `pin:` / `source_oams:` | keep `pin:` for a wired FPS board, `source_oams:` for a Cortex Bridge |
| `E_FPS_LANE` | an `[oams] fps:` names no lane, or several lanes and a unit without `fps:` | ASK which lane each unit feeds |
| `E_GROUP_MEMBER` | a group names a unit or bay that does not exist | fix it to `oams<N>-<bay>` with bay 0-3 (AMS HT: 0) |
| `E_GROUP_BAY_DUP` | one bay in two groups | ASK which group the bay belongs to |
| `E_GROUP_LANE_MIX` | a group has bays on different lanes | ASK which bays belong together |
| `E_GROUP_NAME_DUP` | `[filament_group X]` defined twice | merge into one section, keeping the bays in the order the human wants (ASK if they differ) |
| `E_TAG_KEY` | `[bambu_lab_tag_processor] key` is not 32 hex digits | section 6.3 |
| `E_HDC1080` | `sensor_type: HDC1080` without `[hdc1080]` | add an empty `[hdc1080]` section to `oams.cfg` |
| `E_PAUSE_RESUME` | OpenAMS macros without `[pause_resume]` | add an empty `[pause_resume]` section to `oams.cfg` |
| `E_MACRO_COLLISION` | a macro in `oams_macros.cfg` is also defined elsewhere, with the same body: an old copy, which silently replaces parts of the other | section 6.5 |
| `E_MACRO_CUSTOMIZED` | a macro in `oams_macros.cfg` is also defined elsewhere with a **different** body: an edit that an update will throw away | section 6.5 |
| `E_MACRO_MISSING` | an OpenAMS macro calls a command nothing defines (usually `CLEAN_NOZZLE`) | section 6.5 |
| `W_FOREIGN_MMU` | another MMU system is configured | same as `FOREIGN_MMU` in section 1.2 |
| `W_TOOL_MACRO` | a `T<n>` macro is defined outside OpenAMS's files | section 6.5 |
| `M_MACRO_UNVERIFIED` | no `--commands` (Klipper not ready): a call that could not be checked | run section 5 again once Klipper is `ready` |
| `M_CUTTER`, `M_PTFE_ZERO`, `M_FPS_ORIENTATION`, `M_RFID_FIRMWARE` | only a human can confirm these | do not change them; list them in the report (section 9) |
| `I_*` | information | copy the `I_SUMMARY` lines into the report |

---

## 6. Fit OpenAMS into this printer's configuration

**Fresh install only:** do 6.1 to 6.6.
**Update or migration:** do only what the audit flags.

### 6.1 Read before you write

Read these files in full before editing anything:
- `oams.cfg`
- `oams_macros.cfg` (only to know its macro names; never edit it)
- `printer.cfg`
- every file `printer.cfg` includes

Note:
- every `[mcu ...]` and its `canbus_uuid`/`serial`;
- every `[gcode_macro ...]` whose name is `T0`-`T19`, `_TX`, `CUT_FILAMENT`,
  `SAFE_UNLOAD_FILAMENT`, `CLEAN_NOZZLE`, `PAUSE` or `RESUME`;
- `[pause_resume]`;
- any `[filament_switch_sensor]`.

### 6.2 Find the boards' CAN UUIDs (or serial paths)

**Cortex Bridge printer** (`lsusb` shows `1a86:fe11`): skip the CAN query
below. The installer already wrote one `[mcu]`, `[oams]` and the
`[filament_group]` sections per AMS into `oams.cfg`, between the
`Cortex Bridge units` markers, plus `[fps] source_oams:`. Check them:

**Run:** `python3 ~/openams/tools/cortex-bridge.py status`
**Expect:** the last line is `Everything is connected.`
**If not:** the line names the fix. For `... AMS online but not in your config`
run `python3 ~/openams/tools/cortex-bridge.py config --write --yes` (it stops
and restarts Klipper and only adds missing units). Anything about cables,
LEDs or power: STOP and ask the human.

Each AMS's `ptfe_length` comes from the human's measurement, converted with
`cortex-bridge.py ptfe <millimetres>`. NEVER guess it. Then continue at 6.3.

**OpenAMS boards on CAN:** you need one `canbus_uuid` per AMS unit
(`[mcu oams_mcuN]`), plus one for the FPS board (`[mcu fps]`) unless the FPS
connects over USB.

**Run** (Klipper is in `error` after a fresh install, so it holds no boards):
```bash
~/klippy-env/bin/python ~/klipper/scripts/canbus_query.py can0
```
**Expect:** lines like `Found canbus_uuid=0a1b2c3d4e5f, Application: Klipper`.

Work it out:
1. Remove every UUID that is already in a `[mcu ...]` section. What is left
   are the candidates.
2. A candidate with `Application: Katapult` or `CanBoot` is a board waiting in
   its bootloader: STOP ("board <uuid> is in its bootloader; it needs firmware
   from a human").
3. **Count check:** the candidates must equal the number of AMS units plus one
   FPS board on CAN. ASK the human how many AMS units there are if you do not
   know. More or fewer candidates: ASK, listing them.
4. **Which candidate is which:** you cannot tell from this list. Put the
   candidates into the sections in any order, do 6.6, then read each board's
   firmware version:
   ```bash
   curl -s "http://127.0.0.1:7125/printer/objects/query?mcu%20fps=mcu_version&mcu%20oams_mcu1=mcu_version"
   ```
   - An FPS board runs plain Klipper: its version starts with `v0.`, like
     `v0.12.0-158-g36f9b26e`.
   - An OpenAMS unit's version does not, like `oams_2.0.26.1-beta-9d0bfd0`.
   - If `fps` shows an OpenAMS version, swap the two UUIDs and repeat 6.6.
   - If both look the same, or a section reports `Unable to connect`: ASK the
     human.

**USB FPS board:** use `serial: /dev/serial/by-id/<name>` instead of
`canbus_uuid:`. Run `ls -l /dev/serial/by-id/`. Pick the entry that is not
already used by another `[mcu]`. If there is more than one: ASK.

### 6.3 The tag key and RFID

- **`[bambu_lab_tag_processor] key: <32_HEX_CHARACTERS>`:** ASK "Do you have
  the Bambu Lab RFID master key to put in oams.cfg? Without it, tags are read but
  not decoded." If the human gives it, set it. Otherwise comment the whole
  section out with `# openams-agent: no key provided`. NEVER search for the key.
- **`[mfrc522 openams]`:** this section drives the RFID readers of the
  STM32F072 OpenAMS mainboard, and needs its firmware 2.0.25 or later.
  - After 6.6, read `mcu oams_mcu1`'s `mcu_version` (6.2).
  - If the version starts with `oams_2.` and is 2.0.25 or later, keep the
    section.
  - If it starts with `oams_2.` but is older, comment the section out with
    `# openams-agent: firmware older than 2.0.25`.
  - If it does not start with `oams_2.` (another board family), ASK "Does this
    AMS use the OpenAMS mainboard's MFRC522 readers?" Comment the section out
    unless the answer is yes.

### 6.4 Units, lanes and groups

- **One `[mcu oams_mcuN]` + `[oams oamsN]` per AMS unit**, N = 1, 2, ...
  - In each `[oams]`: `mcu: oams_mcuN`, `oams_idx: N`, `device_kind: auto`.
  - Copy the other options from `[oams oams1]` of the sample unchanged.
- **One `[fps]` per toolhead.** A single toolhead keeps the sample's bare
  `[fps]` with `pin: fps:PA2`. Several toolheads: ASK which unit feeds which
  toolhead, then use named `[fps NAME]` sections and `fps: NAME` in each
  `[oams]`. See `oams_sample.cfg`.
- **Groups, fresh install:** one group per bay, in unit order.
  - Unit 1 bays 0-3 are `T0`-`T3`, unit 2 bays 0-3 are `T4`-`T7`, and so on.
  - `oams_macros.cfg` defines only `T0`-`T3`. For each extra group `Tn`, add to
    the end of `oams.cfg`:
    ```
    [gcode_macro Tn]
    gcode:
      _TX GROUP=Tn
    ```
- **Groups, update or migration:** never change existing groups.
- **`[temperature_sensor oams]`:** this is the HDC1080 sensor on the STM32F072
  OpenAMS mainboard. Keep it for unit 1.
  - For each further unit, ASK "Does AMS unit N have its own HDC1080
    temperature sensor?"
  - On yes, add a copy named `oams<N>` with `i2c_mcu: oams_mcuN`.
- **Never change:**
  - `ptfe_length` (0 is the safe uncalibrated value);
  - `fps_is_reversed`;
  - `[fps] reversed`;
  - `hub_hes_on`, `f1s_hes_on`;
  - the PID values.
  
  A human calibrates these (section 9).
- **gco-routines stays off.** Never add `[gco_routines]` or uncomment
  `[include oams_macros_ordered.cfg]`. Enabling it is the human's decision, and
  works only when `GCO_BASELINE=yes` or the installer printed
  `gco-routines: this Klipper is a version it accepts`. On any other Klipper it
  stops Klipper at startup.

### 6.5 Macros: collisions and what OpenAMS needs from the printer

- **`E_MACRO_COLLISION`:** another file defines a macro OpenAMS ships (for
  example an old `T0`, or `CUT_FILAMENT` from a previous MMU setup) with the
  **same body**. Klipper merges the two silently, and the toolchange then runs
  a mix of both.
  - ASK: "printer.cfg/<file> defines [gcode_macro X], which OpenAMS also
    defines. Should I comment out the old one (in <file>:<line>)?"
  - On yes, comment out that whole section with
    `# openams-agent: replaced by oams_macros.cfg`.
  - Never edit `oams_macros.cfg`.
- **`E_MACRO_CUSTOMIZED`:** the same thing, but the copy has a **different
  body**: the owner changed the macro. It works today and is gone at the next
  update, because `oams_macros.cfg` is replaced by the checkout's version (it is a link; an old customized copy was moved to `oams_macros.cfg.bak-<date>` by the installer, and its changes belong in the owner's own file).
  1. ASK which lines the owner added, and what they were for. Do not guess.
  2. Move those lines into the override or hook that replaces the macro, in the
     owner's own file:
     - the cutter moves go into `[gcode_macro OAMS_CUT]` (called by
       `CUT_FILAMENT` in place of its own moves; the step is still `cut`);
     - anything the toolchange or unload should do at a step boundary goes
       into a `[gcode_macro _OAMS_HOOK_<point>]` in their own file: one of
       `BEFORE_HEAT`, `AFTER_CUT`, `AFTER_RETRACT` (a swap only) or
       `AFTER_FEED`. Give it `variable_step: "<step>"` and
       `variable_label: "<text>"` (both quoted) when the action deserves a step
       of its own on the display; leave them out when it should just run inside
       the step around it. The point's position is the display's: a wipe
       belongs to `AFTER_CUT`, a purge to `AFTER_FEED`.
     - nozzle cleaning is already the owner's: `CLEAN_NOZZLE`.
  3. Comment out the old definition, with
     `# openams-agent: moved to <the macro you wrote>`.
  - **Worked example.** The owner copied `CUT_FILAMENT` and added a wipe of the
    nozzle after the cut. The finding names `OAMS_CUT` for the cutter moves and
    the added wipe is its own action, so their file ends up with the cut
    override and a hook that has a step of its own:
    ```ini
    [gcode_macro OAMS_CUT]
    gcode:
        {% set v = printer["gcode_macro _oams_macro_variables"] %}
        G90
        G0 X{v.pre_cut_x} Y{v.pre_cut_y} F{v.pre_cut_speed}
        G0 X{v.cut_x} Y{v.cut_y} F{v.cut_speed}
        G0 X{v.post_cut_x} Y{v.post_cut_y} F{v.pre_cut_speed}
        M400

    [gcode_macro _OAMS_HOOK_AFTER_CUT]
    variable_step: "wipe"
    variable_label: "Wiping the nozzle"
    gcode:
        WIPE_NOZZLE
    ```
    and the `CUT_FILAMENT` copy they had is commented out. The plan now reads
    `cut, wipe, retract, ...`, so the displays show the wipe.
- **`W_TOOL_MACRO`:** a `T<n>` macro exists for a group OpenAMS's file does not
  define. Leave it, and list it in the report.
- **`CLEAN_NOZZLE`** is called by every toolchange, after the load. It is the
  most common `E_MACRO_MISSING`. ASK: "OpenAMS calls CLEAN_NOZZLE after each
  load. Does this printer have a nozzle wiper macro I should use, or should I add
  one that does nothing?" On "nothing", add to `oams.cfg`:
  ```
  [gcode_macro CLEAN_NOZZLE]
  # openams-agent: no nozzle cleaner on this printer
  gcode:
    RESPOND MSG="CLEAN_NOZZLE: no nozzle cleaner configured"
  ```
- **`[pause_resume]`** must exist (`E_PAUSE_RESUME`).
- **Do not change** `PRINT_START`, `PRINT_END`, `PAUSE`, `RESUME`,
  `CANCEL_PRINT` or the slicer. The report tells the human what the slicer needs.
- **The cutter and toolhead lengths** are in `[gcode_macro _oams_macro_variables]`
  in `oams.cfg` (`variable_pre_cut_*`, `variable_cut_*`, `variable_post_cut_*`,
  `variable_extrusion_*`). On a migration, keep the old values. On a fresh
  install, ASK: "What are this printer's filament cutter coordinates (pre-cut
  X/Y, cut X/Y, post-cut X/Y)? Until they are set, toolchanges are unsafe."
  Write only what the human gives.

### 6.6 Apply and check

1. Run the audit (section 5) until it reports `errors=0`.
2. Restart Klipper so it reads the config:
   `curl -s -X POST http://127.0.0.1:7125/printer/restart`
3. Wait (section 4) until `klipper: ready`.
4. If it is `error`, read `state_message` and use the table in section 7, L1.
5. Then run the audit again **with** `--commands` (Klipper is ready now).

---

## 7. The bring-up ladder

**Run:**
```bash
python3 ~/openams/tools/agent/bringup_check.py --selftest | tee ~/openams-agent-bringup.txt; echo "EXIT=${PIPESTATUS[0]}"
```
- **Output:** one line per level, `L<n> <NAME> <PASS|FAIL|WARN|SKIP> <detail>`, with
  indented detail lines, then `BRINGUP pass=N fail=N warn=N skip=N`.
- **Exit code:** 0 when nothing failed.
- **What it sends:** it moves nothing. `--selftest` sends only `OAMSM_SELFTEST`,
  which is read-only.

**Expect:** every level `PASS` or `WARN`, and `EXIT=0`.

**If not:** fix the **lowest** failing level first, then run the ladder again.

| Level | FAIL / WARN means | Where to look | What you may do |
|---|---|---|---|
| L0 MOONRAKER FAIL | Moonraker is down | `systemctl status moonraker`, `~/printer_data/logs/moonraker.log` | `sudo systemctl restart moonraker` once; else STOP |
| L0 WARN `openams_spoolman` missing | the component is not loaded (spool editing is off in every UI) | `grep -n openams_spoolman ~/printer_data/logs/moonraker.log`; `[openams_spoolman]` in `moonraker.conf`; the link `~/moonraker/moonraker/components/openams_spoolman` | if the link and the section exist, `sudo systemctl restart moonraker` once (Moonraker loads components only at start). If either is missing, run `install.sh` again (section 3). Report it if it persists |
| L1 KLIPPY FAIL | Klipper is not ready | the detail lines (`state_message`), then `tail -n 80 ~/printer_data/logs/klippy.log` | match the message below |
| L2 IDLE FAIL | printing or paused | | STOP |
| L3 MCUS FAIL | an MCU has no version | the MCU's name in the detail | see `Unable to connect` below |
| L3 WARN bus state or errors | CAN errors (wiring, termination) | the `canbus_stats` numbers | report them; ASK the human to check the CAN cabling |
| L4 OPENAMS FAIL `oams_manager is absent` / not ready | OpenAMS did not load | `klippy.log`, search `oams` | run `install.sh` again; else STOP |
| L4 FAIL a unit not connected | the unit's MCU is not talking OpenAMS | L7's self-test lines (`NOT CONNECTED`, `REFUSED: <reason>`) | a `REFUSED` reason is about firmware: STOP. Otherwise recheck its UUID (6.2) |
| L5 FAIL `fps_value` outside 0..1 | the FPS reading is broken | `[fps]` pin/source | STOP |
| L5 WARN FPS above 0.8 on an unloaded lane | possibly reversed FPS orientation | the values in the detail | do not change anything; report it (section 9) |
| L5 WARN reader chip does not answer | an RFID reader is missing or dead | the error text | report it (hardware) |
| L5 WARN temperature out of range | a sensor is wrong or disconnected | the sensor name | report it |
| L6 MACROS FAIL | a group has no `T<n>` macro, or a required macro is missing | the names in the detail | add `T<n>` macros (6.4); check that `oams.cfg` includes `oams_macros.cfg` |
| L6 WARN `CLEAN_NOZZLE` missing | | | section 6.5 |
| L7 SELFTEST FAIL | the self-test found a problem | its lines: `NOT CONNECTED`, `REFUSED`, warnings | fix what it names (L3/L4 rows); else STOP |
| L8 SPOOLMAN WARN | Spoolman offline or absent | `systemctl status Spoolman`; `curl -s localhost:7912/api/v1/info` | report it (optional part) |
| L9 UPDATES WARN | Moonraker cannot update OpenAMS (no remote, dirty, detached) | the detail | report it. Never `git reset` |

**Klipper's messages** (L1). Find the first one that matches `state_message` or
the end of `klippy.log`:

| Message contains | Cause | Do |
|---|---|---|
| `Invalid canbus_uuid` | a placeholder or typo in `canbus_uuid` | section 6.2 |
| `Duplicate canbus_uuid` | the same UUID in two `[mcu]` sections | section 6.2 |
| `mcu 'host': Unable to connect` (an `[mcu]` whose `serial:` is `/tmp/klipper_host_mcu`) | the Pi's own Linux MCU process | run `systemctl is-active klipper-mcu`, `ls -l /tmp/klipper_host_mcu` and `id -nG`. The Klipper user must be in group `tty` to open that pty. Do not fix it yourself: STOP and report the three outputs (the fix is `sudo usermod -a -G tty <user>` and a Klipper restart, the human's call) |
| `mcu '<name>': Unable to connect` or `Timeout on connect` | wrong UUID, board off, CAN down | check `CAN_can0_STATE`; recheck the UUID (6.2). Still failing: ASK the human to check the board's power and cable |
| `Option '<x>' in section '<y>' must be specified` | a required option is missing | add it from `oams_sample.cfg`, same section |
| `Section '<x>' is not a valid config section` | an extra is not installed (OpenAMS links missing) | run `install.sh` again (section 3) |
| `bambu_lab_tag_processor key must contain exactly 16 hexadecimal bytes` | the tag key | section 6.3 |
| `Duplicate` / `already` about a `filament_group` | a group defined twice | the audit's `E_GROUP_NAME_DUP` |
| `Unknown command` while a macro runs | a macro calls something undefined | audit with `--commands` (section 5) |
| `MCU '<name>' shutdown` | an MCU shut down (heater, timing) | STOP; never use `FIRMWARE_RESTART` to hide it |
| anything else | | STOP; quote the message |

When every level is `PASS` or `WARN`, OpenAMS is up. Continue to section 8.

---

## 8. Final checks

1. **Audit:** run section 5 again (with `--commands`). It must report
   `errors=0`.
2. **Ladder:** run section 7 again. It must exit 0.
3. **Nothing is left over:**
   - `grep -n "openams-agent" "$CFG"/*.cfg` lists only the changes you made on
     purpose.
   - Nothing you added still holds a `<placeholder>`.
4. **The panel:** if the printer uses Mainsail or Fluidd, the OpenAMS panel is a
   separate install. It is not part of this guide; mention it in the report.

---

## 9. What only a human may do

List these in the report, with the commands. Never run them yourself.

- **FPS orientation:** with no filament loaded, the FPS should read near 0.
  - If it reads near 1, flip `[fps] reversed` (and see `fps_is_reversed` in
    `oams_sample.cfg`).
  - Value now: from L5.
- **Cutter coordinates and toolhead lengths:** in `_oams_macro_variables`
  (`M_CUTTER`).
- **Hub sensor calibration:** `OAMS_CALIBRATE_HUB_HES OAMS=<idx> SPOOL=<bay>`
  (STM32F072 mainboard only), with filament in that bay.
- **PTFE length:** `OAMS_CALIBRATE_PTFE_LENGTH OAMS=<idx> SPOOL=<bay>`. Then set
  `ptfe_length` (`M_PTFE_ZERO`). Until then, a runout pauses instead of
  reloading.
- **First load and unload, with someone watching:**
  `OAMSM_LOAD_TO_TOOLHEAD GROUP=T0`, then `OAMSM_UNLOAD_FROM_TOOLHEAD`.
- **The slicer:** emit `T<n>` at each toolchange (the group name).
- **RFID:** a reader whose chip does not answer (L5) is wiring or hardware.

---

## 10. Reports

### The final report (when section 8 passes)

Send exactly this, filled in:

```
OpenAMS install report
- Host: <USER>@<hostname>, <OS>, Klipper <KLIPPER_COMMIT> (<KLIPPER_FLAVOR>)
- Case: fresh | update | migration from klipper_openams
- install.sh flags: <flags>   exit: <0>
- Backup: <path to .tgz>
- Changes I made (file:line, what, why):
  - ...
- Units: <I_SUMMARY units line>   Groups: <I_SUMMARY groups line>
- gco-routines: installed and off | skipped (<why>); accepts this Klipper: <GCO_BASELINE, or the installer's answer>
- Audit: AUDIT errors=0 warnings=<n> manual=<n>
- Bring-up: <the BRINGUP line>; WARN levels: <each L line that warned, verbatim>
- Self-test: <RESULT line>
- For you to do (section 9): <each MANUAL item and WARN, one per line, with the command>
- Questions I asked and your answers: <list>
- Logs: ~/openams-agent.log, ~/openams-agent-install.log, ~/openams-agent-audit.txt, ~/openams-agent-bringup.txt
```

### A STOP report (whenever you STOP)

```
OpenAMS install STOPPED at section <n>, step <name>
- Why: <the STOP reason or the failing check, verbatim>
- What I ran last: <command>
- Its output (last 30 lines): <...>
- Relevant log lines: <tail of klippy.log or moonraker.log, 30 lines max>
- State now: Klipper <state>, print <PRINT_STATE>, changes made so far: <list or none>
- Backup: <path>   How to undo my changes: section 11
- What I need from you: <one clear question or action>
```

---

## 11. Updating, removing, undoing

- **Update OpenAMS:**
  1. `git -C ~/openams pull --ff-only`.
  2. `./install.sh` with the same flags as before.
  3. Sections 4, 5 and 7.
- **Remove OpenAMS, keeping the configs:**
  `cd ~/openams && printf 'n\n' | ./install.sh -u`.
  - The `n` answers the installer's "Remove oams.cfg ...?" prompt.
  - It removes the links, the `[include oams.cfg]` line, `[update_manager openams]` and
    `[openams_spoolman]`.
  - It leaves Spoolman and its data, gco-routines and every `oams*.cfg` alone.
- **Undo everything you did:**
  1. Remove OpenAMS as above.
  2. Restore the backup:
     `tar -C "$CFG" -xzf ~/openams-agent-backup-<TS>.tgz`.
  3. `curl -s -X POST http://127.0.0.1:7125/printer/restart`, then section 4.
  4. Klipper should return to the state it had before you started.

---

## Appendix: files, services, logs

| What | Where |
|---|---|
| OpenAMS checkout | `~/openams` (tools: `~/openams/tools/agent/`) |
| Klipper config | `~/printer_data/config/` (`printer.cfg`, `oams.cfg`, `oams_macros.cfg`, `moonraker.conf`) |
| Logs | `~/printer_data/logs/klippy.log`, `moonraker.log`; `journalctl -u klipper -n 100` |
| Services | `klipper`, `moonraker`, `Spoolman` (optional), `klipper-mcu` (if the host is an MCU) |
| Moonraker API | `http://127.0.0.1:7125` (`/printer/info`, `/printer/objects/query?<object>=<fields>`, `/printer/gcode/help`, `/server/gcode_store`) |
| Spoolman | `http://127.0.0.1:7912/api/v1/info` |
| CAN query | `~/klippy-env/bin/python ~/klipper/scripts/canbus_query.py can0` |
| Read-only self-test | `OAMSM_SELFTEST` (via `bringup_check.py --selftest`) |
