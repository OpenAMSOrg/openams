# What changed in oams_macros.cfg

These notes were in the README. They matter when you update from an older
OpenAMS that had a copied (not linked) `oams_macros.cfg`, or when you enable
concurrent toolchanges with gco-routines.

## Concurrent toolchanges with gco-routines

`oams_macros.cfg` in this repository already contains the concurrent workflow.
If you keep a customized copy, merge the updated macros first (the installer
never replaces it). Then add two configuration lines and restart Klipper while
the printer is idle:

1. In `printer.cfg`, put `[gco_routines]` above `[include oams.cfg]` and
   before any other `[gcode_macro ...]` section or include that defines macros.
2. In `oams.cfg`, uncomment `[include oams_macros_ordered.cfg]`, directly after
   `[include oams_macros.cfg]`. This file only sets `render_mode: ordered` on
   `_TX`.

What changes when both are present:

- The cut, toolhead retraction and OpenAMS unload still finish first. The
  OpenAMS load (`OAMSM_LOAD_FILAMENT`) then runs in a background routine while
  `CLEAN_NOZZLE` runs, so the nozzle is cleaned during loading.
- `M400` finishes the cleaning moves, and `WAIT` joins the load before the
  load result and inlet sensor are checked and before the toolhead reload
  extrusion.
- A toolchange that starts while the printer is paused runs serially. A pause
  during the overlap follows the extension's rules (no new routine starts while
  paused); the toolchange then stops before the toolhead reload.
- Call `T0`-`T19` and `OPENAMS_LOAD` from ordinary G-code only, never inside
  another `START` block: they own their background routine.

On stock Klipper, or without the overlay, the same `oams_macros.cfg` runs every
step serially (load, inlet check, `CLEAN_NOZZLE`, reload) and never renders
`START`, `END` or `WAIT`. Stock Klipper rejects `render_mode`, so include
`oams_macros_ordered.cfg` only together with `[gco_routines]`. To turn the
feature off, comment out both lines again.

## Changes from the previous oams_macros.cfg

These apply with or without gco-routines. Since the installer now links
`oams_macros.cfg`, you get them on update; a customized copy you had is kept as
`oams_macros.cfg.bak-<date>`, and what it changed belongs in your own file
through the hooks. The changes:

- New `_OAMS_*` helper macros (`_OAMS_FINISH_UNLOAD`, `_OAMS_CONFIRM_UNLOADED`,
  `_OAMS_CONTINUE_AFTER_UNLOAD`, `_OAMS_CONTINUE_AFTER_LOAD`,
  `_OAMS_CONTINUE_AFTER_INLET`, `_OAMS_FINISH_TOOLCHANGE`,
  `_OAMS_FAIL_TOOLCHANGE`, `_OAMS_ABORT`, `_OAMS_RAISE`). Stock Klipper renders
  a whole macro before running it; each helper renders fresh, so every safety
  decision sees the result of the step before it. `_TX` and
  `SAFE_UNLOAD_FILAMENT` call them, so copy all of them.
- Failure handling: on a failed load, failed unload, enabled sensor failure, or
  a pause arriving mid-toolchange, `T0`-`T19` and `SAFE_UNLOAD_FILAMENT` turn
  the follower off, `PAUSE`, and raise a G-code error. In an SD print the print
  therefore ends through `on_error_gcode` instead of continuing. UI (`STRICT`)
  requests (`OPENAMS_LOAD`, `OPENAMS_UNLOAD`) raise without `PAUSE`.
- `CLEAN_NOZZLE` runs after the OpenAMS load and before the toolhead reload
  (previously after the reload). There is no `G0 Z15` before it, so
  `CLEAN_NOZZLE` must provide its own Z clearance.
- The toolchange is wrapped in one `SAVE_GCODE_STATE` / `RESTORE_GCODE_STATE`
  (`NAME=oams_toolchange`). On success it returns to the starting position at
  100 mm/s and restores the positioning and extrusion modes.
- Homing and heating are steps, not refusals. A flow homes (`G28`) when the
  axes are not homed and heats the extruder to the filament's printing
  temperature before it cuts or extrudes; both appear in the announced plan as
  `home` and `heat`, `home` first. A hot-enough extruder has no `heat` step, an
  unknown material heats to `unknown_material_temperature`, a swap heats to the
  higher of the two filaments, and `TEMP=<°C>` overrides all of it. The
  temperature is clamped to the heater's `max_temp - 5` and
  `max_operation_temperature` and raised to `minimum_extrude_temperature`; a
  floor above that ceiling is a configuration error and fails before any motion.
  A running print with its own hot target is never heated. On success an idle
  flow puts the target back (a failed one leaves it on, so a retry starts hot).
  For `T0`-`T19`, an unknown filament group still fails before any motion.
- `SAFE_UNLOAD_FILAMENT` sets `M83` before the first retract and retracts at
  `unload_speed`. `CUT_FILAMENT` issues `G90` before the cutter moves.
- Four new variables in `[gcode_macro _oams_macro_variables]`, with these
  defaults when absent: `unload_speed: 1000`,
  `extrusion_unload_additional_length: 250`, `additional_unload_speed: 5000`,
  `minimum_extrude_temperature: 0`.
- The macros require `[pause_resume]` and read the `extruder` and `configfile`
  status objects.


## Where to go next

- [Install](install.md#gco-routines) — installing gco-routines.
- [Customizing the toolchange](configure.md#customizing-the-toolchange) — where your own changes belong.
