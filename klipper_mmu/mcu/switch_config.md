# Saved local switch settings

The MMU pin menu includes **Local switch electrical settings**. Pin names remain
in the existing pin menu. This submenu records active levels, internal pull
selection and the mechanical mapping of a switched buffer. These are separate
choices: reversing the active electrical level does not exchange the two buffer
ends. No pin is configured by saving these settings.

| Input | Active level | Pull selection | Default |
| --- | --- | --- | --- |
| Bay/preparation | One 0/1 value per lane | One -1/0/1 value per lane | BoxTurtle + AFC Lite: active low, pull-up |
| Hub | 0=low, 1=high, -1=unset | -1=down, 0=none, 1=up, -2=unset | BoxTurtle + AFC Lite: active high, pull-up |
| Local arrival | Same as hub | Same as hub | BoxTurtle + AFC Lite: active low, pull-up. Other presets: unset |
| Buffer A and B | Two 0/1 values, A then B | Two -1/0/1 values, A then B | BoxTurtle + AFC Lite: active low, pull-up, buffer A requests forward feed. Other presets: unset. Hidden for DiffA/DiffB |

List values use commas without spaces, exactly one value per configured input.
For example, bay pulls `-1,0,1,1` select down, none, up, up. Missing lists remain
unconfigured; extra/missing entries, whitespace, `+1`, `-0`, `01` and values outside
the documented range are rejected. Unused bay entries have pin `KM_PIN_UNUSED`.

The BoxTurtle defaults follow AFC's `templates/AFC_Turtle_1.cfg`: `prep: ^!TRG*`
and `switch_pin: ^HUB`, resolved through `config/mcu/AFC_Lite.cfg`. These source
settings are references, not measurements of the connected sensors. Other presets
and custom lane counts require explicit choices. Per-lane EXT/LOAD switches have separate pin and electrical lists when enabled;
EMU uses them as its virtual shared HUB. No toolhead sensor type or electrical wiring is assumed.

For a switched buffer, explicitly select **Buffer input that requests forward
feeding**. Choose the input reached when more filament must be fed forward. It
maps to normalized low position (250); the other input maps to high position
(750); neither active maps to neutral (500). Both active is invalid. The factory
swaps the complete input records, including pin, active level and pull, according
to this choice. The source aliases “advance” and “trailing” alone do not establish
a physical mapping. This menu chooses which input has each role, not new control
thresholds or a measurement calibration.

Only with [FPS arrival](../docs/fps-arrival.md) selected may the local arrival
switch be disabled. Its pin/polarity/pull choices then disappear, the factory
leaves its pin unused, and it requires no electrical settings. A fitted switch
keeps all ordinary validation and freshness requirements.

With analog DiffA/DiffB selected, the buffer switch choices are hidden and the
factory leaves both digital buffer entries unused. It does not apply a switch
pull to those analog pins. `km_config_pins()` does not validate the ADC route;
`km_config_h723_resources()` does, using the shared driver's first route.
A legacy HUB=PC4 or TRG1=PC5 conflict applies only when those pins are used;
the user remaps them. Analog calibration and ADC ownership remain with their
separate components. Existing saved overrides remain overrides across
device/board changes; changing the wiring requires reviewing them.

`km_config_switches()` returns the accepted `km_switch_gpio_config_t`. It calls
the existing complete pin factory first, then combines those pins with electrical
settings. Pin errors propagate unchanged, unset electrical choices return
`KM_ERR_UNAVAILABLE`, and malformed lists return `KM_ERR_INVALID_PARAM`. It builds
a local candidate and publishes only on success, preserving caller output on
any error. It has no GPIO access, timers, global ownership, synthetic samples or
actuator effect. Calls are serialized during setup and read the shared immutable validated pin
view, avoiding a duplicate stored pin map. This is a startup
factory, not an ISR sampling routine.

The callable board-startup composition validates the resource plan and saved
pins and attaches the local controller at READY. The hosted diagnostic startup path invokes the composition after Klipper
configuration using hardware or host-established boot identity as configured.
This factory adds no initializer of its own. Electrical suitability, scan execution
cost, and actual switch behavior still require board qualification.
`ACTUATION` stays 0 unless explicitly enabled for the qualification build.
