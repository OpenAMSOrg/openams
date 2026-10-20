# Toolhead cut distances

Starting values for the toolhead settings in `[gcode_macro _oams_macro_variables]`
(`retract_length`, `extrusion_unload_length`, `extrusion_reload_length`,
`hotend_meltzone_compensation`), one entry per toolhead + extruder + cutter +
hotend. Each entry is measured in that combination's published CAD, not taken
from someone's printer.

| File | What it is |
|---|---|
| [`cut_defaults.json`](cut_defaults.json) | The database: geometry, starting values, sources and checks for each combination, plus the values other MMU systems ship as references. |
| [`TABLE.md`](TABLE.md) | The same combinations as a table. |
| [`../tools/toolhead_defaults.py`](../tools/toolhead_defaults.py) | Looks a combination up and prints its settings, ready to paste. |

```
python3 tools/toolhead_defaults.py list
python3 tools/toolhead_defaults.py show --toolhead Stealthburner --extruder "Clockwork 2" \
    --cutter FilamATrix --hotend "Dragon HF"
python3 tools/toolhead_defaults.py community Stealthburner
```

These are starting points. The reload test on the printer has the last word:
raise `extrusion_reload_length` until the nozzle just oozes during the reload,
then back off 1 mm. The cutter coordinates (`pre_cut_x`, `cut_x`, `post_cut_x`
and their `_y`) depend on the printer's frame and are not in the database.

## What is measured

Each toolhead CAD is cut with a plane through the filament axis, square to the
extruder gear axes. Heights are measured along the filament axis, in mm above
the nozzle tip:

- `nip`: the drive gear axis, where the gears grip the filament
- `blade`: the blade plane
- `meltzone_top`: the top of the heater block (the heater core on a Revo)
- `heatsink_top`, `heatbreak_top`, `tube_seat`: the hotend's top, the
  heatbreak's top, and where the PTFE tube bottoms out at the top of the
  toolhead

The checks run on the CAD are listed with each entry. They cover:

- the gears gripping a 1.75 mm filament
- the blade sitting between the gears and the melt zone, with nothing else in
  its path
- the channel staying open for the filament
- parts that overlap

`narrow_passages` lists the places where the channel is less than 2.6 mm wide.
A cut end that isn't clean, or a formed tip, catches there on its way up.

## How the settings follow from the geometry

| Setting | Starting value |
|---|---|
| `retract_length` | top of the melt zone + 2.5 mm, rounded up to 0.5 mm: the tip leaves the melt zone before the cut |
| `extrusion_unload_length` | gear nip → blade + 5 mm, rounded up to 1 mm: the cut end clears the gears |
| `extrusion_reload_length` | gear nip → blade, rounded to 0.5 mm: from the gears (where the AMS stops) to the stub the cut left |
| `hotend_meltzone_compensation` | 0: add a little only if the nozzle drips while parked |

An entry without a melt zone has no starting values. That happens when the
hotend is a single solid in the CAD and no published drawing gives its heater
position. Its distances are still measured.

## Community values

`community` holds what Happy Hare and AFC ship for these toolheads (Happy
Hare's installer presets and wiki table, AFC's hotend values page), with the
source of every number. Their parameters measure different things from the
OpenAMS ones. For example, Happy Hare's `retract_length` pulls the tip to just
below the blade, and AFC's `tool_stn` already subtracts the filament left in
the hotend. So they are kept as references to compare against, not copied
across.
