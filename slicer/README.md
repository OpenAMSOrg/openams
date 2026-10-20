# Slicer profiles for OpenAMS

OpenAMS runs the toolchange in Klipper, so the slicer must only write `T0`, `T1`, …
and purge after each one. OrcaSlicer's and PrusaSlicer's multi-material defaults
were made for the Prusa MMU: they ram, cool, park and reload the filament around
every `T`, which fights the OpenAMS macros. These files and settings turn that off.

The easiest way to apply them is the slicer setup page, https://openams.si-forge.com/en/slicer-setup. It makes
OpenAMS copies of your own Orca or PrusaSlicer profiles and checks a sliced file.

## Files

- `openams_slicer.json`: every setting OpenAMS needs, per slicer and per preset
  type, with its value and the reason. The page, the ready-made files and the
  tables below are made from it.
- Ready-made OrcaSlicer profiles for Orca's own Voron printers (0.4 mm nozzle).
  Each holds the printer and its six process profiles, as presets that inherit
  Orca's own and change only these settings. Import with File → Import → Import
  Configs:
- `orca/Voron 0.1 0.4 nozzle OpenAMS.orca_printer`
- `orca/Voron 2.4 250 0.4 nozzle OpenAMS.orca_printer`
- `orca/Voron 2.4 300 0.4 nozzle OpenAMS.orca_printer`
- `orca/Voron 2.4 350 0.4 nozzle OpenAMS.orca_printer`
- `orca/Voron Switchwire 250 0.4 nozzle OpenAMS.orca_printer`
- `orca/Voron Trident 250 0.4 nozzle OpenAMS.orca_printer`
- `orca/Voron Trident 300 0.4 nozzle OpenAMS.orca_printer`
- `orca/Voron Trident 350 0.4 nozzle OpenAMS.orca_printer`

PrusaSlicer has no ready-made files: an imported PrusaSlicer preset doesn't take
its values from the preset it inherits, so it has to be complete, and a complete
printer preset would replace your bed, offsets and start G-code. The page makes the
copies from your own export instead.

## Settings

Checked by slicing a two-colour print with OrcaSlicer 2.4.1 and 2.4.2 and
PrusaSlicer 2.8.1 (2.9.5, 2.9.6 read in the source).

### OrcaSlicer: printer

| Setting | Where | OpenAMS | Why |
|---|---|---|---|
| Single Extruder Multi Material | Printer settings → Multimaterial | `on` | One nozzle shared by every filament: changes and purges go through the prime tower. |
| Enable filament ramming | Printer settings → Multimaterial → Wipe tower | `off` | On, every change writes ramming, about 94 mm of retracts, cooling moves, a parking move and a 90 mm load around the T, and sets pressure advance to 0 for the rest of the print. OpenAMS cuts and reloads by itself. |
| Purge in prime tower | Printer settings → Multimaterial → Wipe tower | `off` | Off, every change purges the process's Prime volume, so the purge never drops below what the cut leaves in the hotend. On, it follows the flushing volumes, which can be much less. |
| Retraction when switching material → Length | Printer settings → Extruder | `0` | The toolchange retracts by itself. A slicer retract before the T is added to it, and pushed back after. |
| Change filament G-code | Printer settings → Machine G-code | `T[next_extruder]` | OpenAMS's T macros do the whole change. Nothing else belongs here. |
| Wipe tower type | Printer settings → Multimaterial → Wipe tower | `type2` | Type 2 takes the purge from Prime volume, which a profile can carry. Type 1 takes it only from the project's flushing volumes. |
| Manual Filament Change | Printer settings → Multimaterial | `off` | On, the T commands disappear from the file. |
| High extruder current on filament swap | Printer settings → Multimaterial → Single extruder multi-material parameters | `off` | Meant for the Prusa MMU. Klipper ignores it; keep it off. |

### OrcaSlicer: process

| Setting | Where | OpenAMS | Why |
|---|---|---|---|
| Prime tower → Enable | Process → Multimaterial → Prime tower | `on` | Without a tower the T is written at the part and nothing is purged. |
| Prime volume | Process → Multimaterial → Prime tower | `140` | The purge after each change, in mm³. It has to cover the old filament the cut leaves in the hotend (about 100 mm³ on a Stealthburner with a FilamATrix) plus colour mixing. |
| Prime all printing extruders | Process → Multimaterial → Prime tower | `off` | On, the print starts by loading every filament in turn. |

### PrusaSlicer: printer

| Setting | Where | OpenAMS | Why |
|---|---|---|---|
| Single Extruder Multi Material, with Extruders = your number of filaments | Printer Settings → General → Capabilities | `on` | One nozzle shared by every filament. PrusaSlicer builds a wipe tower only with more than one extruder. |
| Cooling tube position | Printer Settings → Single extruder MM setup | `off` | Pulls the filament up to a cooling tube in four retracts. OpenAMS unloads by itself. |
| Cooling tube length | Printer Settings → Single extruder MM setup | `off` | Sets the cooling moves' stroke. At 0 with cooling moves still on, PrusaSlicer writes G1 F-2147483648, which stops a Klipper print: set the filaments' cooling moves to 0 too. |
| Filament parking position | Printer Settings → Single extruder MM setup | `off` | Parks the filament with one more retract, and sets how far the slicer reloads after the T. |
| Extra loading distance | Printer Settings → Single extruder MM setup | `off` | With the parking position, the slicer's load after the T. OpenAMS reloads by itself. |
| High extruder current on filament swap | Printer Settings → Single extruder MM setup | `off` | Meant for the Prusa MMU. Klipper ignores it; keep it off. |
| Retraction when tool is disabled → Length (every extruder) | Printer Settings → Extruder 1, 2, … | `0` | Defaults to 10 mm: G1 E-10 before the T and E+12 after. OpenAMS retracts and reloads by itself. |
| Retraction when tool is disabled → Extra length on restart (every extruder) | Printer Settings → Extruder 1, 2, … | `0` | Extra filament pushed after a change. |
| Purging volume | Printer Settings → Single extruder MM setup | `140` | The purge after each change, in mm³. It has to cover the old filament the cut leaves in the hotend (about 100 mm³ on a Stealthburner with a FilamATrix) plus colour mixing. |
| Tool change G-code | Printer Settings → Custom G-code | `T[next_extruder]` | OpenAMS's T macros do the whole change. Nothing else belongs here. |

### PrusaSlicer: print

| Setting | Where | OpenAMS | Why |
|---|---|---|---|
| Wipe tower → Enable | Print Settings → Multiple Extruders | `on` | Off by default: then the T is written at the part and nothing is purged. |
| Prime all printing extruders | Print Settings → Multiple Extruders → Wipe tower | `off` | On by default: the print starts by loading, ramming and unloading every filament in turn. |
| Ooze prevention | Print Settings → Multiple Extruders | `off` | For printers with several hotends; not with one nozzle. |
| No sparse layers | Print Settings → Multiple Extruders → Wipe tower | `off` | On, the nozzle dives into a tower that is only printed on change layers. |
| G-code substitutions (three rules, added to yours) | Print Settings → Output options | `three rules, added to yours` | Deletes three lines the wipe tower writes even with the settings above: a pressure-advance reset it never undoes, an invalid feed rate, and a speed-factor reset. |

### PrusaSlicer: filament

| Setting | Where | OpenAMS | Why |
|---|---|---|---|
| Ramming settings (no ramming lines) | Filament Settings → Advanced → Toolchange parameters with single extruder MM printers | `120 100` | PrusaSlicer has no switch to turn ramming off: an empty ramming graph removes its lines. |
| Number of cooling moves | Filament Settings → Advanced → Toolchange parameters with single extruder MM printers | `0` | Shapes a tip by moving the filament up and down in a cooling tube. OpenAMS cuts instead. |
| Delay after unloading | Filament Settings → Advanced → Toolchange parameters with single extruder MM printers | `0` | A pause the toolchange doesn't need. |
| Stamping distance | Filament Settings → Advanced → Toolchange parameters with single extruder MM printers | `0` | Tip-forming strokes between cooling moves. |
