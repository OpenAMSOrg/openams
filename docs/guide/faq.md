# OpenAMS FAQ

## What this page is

Short answers to the questions the other pages do not answer in one place. Each
answer links to the page that has the detail.

## Scope

**Which printers does it work with?**
Any Klipper printer with Moonraker. The host plugin drives AMS units over
Klipper's serial protocol, on CAN or USB-serial. See [install](install.md).

**Do I need klipper-mmu?**
No. It is a separate, opt-in MCU add-on for an MMU that has its own
microcontroller (BoxTurtle, ERCF, NightOwl and others). A plain AMS unit never
enables it. The installer links its host extra anyway, and that stays dormant
until a config section names it. See [install](install.md#klipper-mmu).

**Can it run beside AFC, Happy Hare or another MMU system?**
Not on the same toolhead. `preflight.sh` reports `FOREIGN_MMU` and the config
audit raises `W_FOREIGN_MMU` when it finds one. See
[troubleshooting](troubleshooting.md#the-three-read-only-checks).

**Do I need Spoolman?**
No. It is part of the default install so a printer shows spool colors and
materials, and `./install.sh --no-spoolman` skips it. Your spools will survive
being anonymous. See
[install](install.md#spoolman).

## Tools and groups

**How many tools can I have?**
A classic unit has four bays, and a group names the bays that serve one tool.
`oams_macros.cfg` defines `T0` to `T3`; `T4` to `T19` are commented out until you
add groups for them. See [configure](configure.md#filament-groups).

**Can I run two AMS units?**
Yes. Name them `oams1`, `oams2` and so on, give each a unique `oams_idx`, and
list bays as `oams1-0, oams2-1`. They can be different families, but every bay in
one group must share an FPS lane.

**Can one tool use several spools?**
Yes, that is what a group is for. Two spools of the same color in one group let
runout reload the spare without a color change. See
[using](using.md#groups-and-runout).

**Does it work with a toolchanger or IDEX printer?**
Give each toolhead its own named `[fps ...]` lane and set `fps:` on each unit.
The macros assume the toolchanger activates the tool before `T<n>` runs. See
[configure](configure.md#fps).

## Config and files

**Where do my group edits and calibration results go?**
Into `oams.cfg`, written in place. `SAVE_CONFIG` cannot edit an included file, so
OpenAMS does not use it. If your sections live in another file, set
`openams_config_path:` under `[oams_manager]`.

**Will an update overwrite my config or macros?**
Not `oams.cfg`: that file is yours and the installer never replaces it.
`oams_macros.cfg` and `oams_macros_ordered.cfg` are links to the files in the
checkout, so an update brings the new macros. Your own changes go in your own
file, through the hooks ([customizing the toolchange](configure.md#customizing-the-toolchange)).
See [install](install.md#update).

**What is the tag key?**
The Bambu Lab master key that derives each tag's MIFARE sector keys. OpenAMS
ships a placeholder and never a real key, and without one Bambu tags do not
decode (the audit reports `I_NO_TAG_KEY`). The key lives in your own config. See
[configure](configure.md#rfid-readers-and-the-tag-key).

**Why does Klipper say `Invalid canbus_uuid`?**
A placeholder is still in `oams.cfg`, which is the normal state after a fresh
install. Replace each with a UUID from `canbus_query.py`. See
[configure](configure.md#mcus-and-can-uuids).

## Running it

**Why did a toolchange heat the nozzle?**
That is the flow doing the work itself. Before it cuts or extrudes it heats to
the filament's printing temperature, waits, and shows it as the `heat` step.
The temperature comes from what your spool manager pushed for the bay, or from
`unknown_material_temperature` (235 °C) when nothing is known. A print that is
running keeps its own temperature: the slicer owns that one. See
[reference](reference.md#gcode_macro-_oams_macro_variables).

**The nozzle is cold again after a toolchange. Is that a bug?**
No. An idle printer cools the nozzle once the flow succeeds, exactly as
Klipper would after a manual command. A flow that failed leaves the heater on,
so the retry starts hot.

**Why does a runout pause instead of reloading?**
Usually `ptfe_length` is still 0. Run `OAMS_CALIBRATE_PTFE_LENGTH` once per bay.
A group with no spare spool, or `auto_reload_on_runout` set to `False`, also
pauses. The messages are in
[troubleshooting](troubleshooting.md#runout-and-calibration).

**Do the check tools change anything?**
No. `bringup_check.py`, `config_audit.py` and `preflight.sh` are read-only, and
`--selftest` sends only `OAMSM_SELFTEST`. The installer is another matter: it
restarts Klipper, so never run it during a print.

**Why is the pressure bar backwards on my touchscreen?**
The host and the unit read the same buffer through different pins, and yours are
inverses. Flip `reversed:` in `[fps]`. See
[configure](configure.md#fps-orientation).

## Front ends and tooling

**How does a touchscreen or Mainsail panel talk to it?**
Through the `openams/status` webhook and the `oams_manager` status object, with
commands such as `OPENAMS_LOAD` and `OAMSM_LOAD_TO_TOOLHEAD`. The contract is in
the [UI API](../UI_API.md).

**Can an AI agent install it?**
Yes. [install.agents.md](../../install.agents.md) is the same install written for
an agent, with read-only checks in [`tools/agent/`](../../tools/agent/) that tell
it when to stop and ask.

**What does the installer download?**
Two optional things: gco-routines from GitHub (skip it with
`--skip-gco-routines`) and, if no Spoolman is found, Spoolman's release and its
prerequisites (skip it with `--no-spoolman`).

**Can I undo the CAN patches?**
Yes: `./install.sh --remove-klipper-patches`, or `-u`, which reverses them if
applied. See the [CAN patches guide](../../klipper_patches/can/GUIDE.md).

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `[ERROR] This script must not run as root. Exiting.` | Installer run with `sudo` | Run it as the Klipper user |
| Klipper dirty in Moonraker after install | The optional CAN patches edit the working tree | Expected; `--remove-klipper-patches` undoes it |
| An answer you cannot find here | It is in a longer page | Start from the [guide index](README.md) |

## Where to go next

- [Troubleshooting](troubleshooting.md) — every error, matched to a cause.
- [Configure](configure.md) — the options you have to decide.
- [Using OpenAMS](using.md) — toolchange, runout and calibration.
- [Guide index](README.md) — every page.
