# Saved MMU pins and Klipper reservations

Select the MCU in Klipper `make menuconfig`, then enable **klipper-mmu** pin
configuration (default off). Choose the mechanism, optionally an editable
wiring preset, and assign every supported signal role. Manual pin mapping is
the default. AFC Lite is only an STM32H723 preset that seeds editable
defaults; a full saved config keeps an explicit AFC preset and pin overrides.
Device, control and pin choices stay separate. Nothing in this menu enables
an output.

Lane lists contain exactly one canonical pin per configured lane, in order
(`PD4,PC12,PE2,PD15`). Shared inputs take one name. Write uppercase names
such as `PA1` or `PA2`: no spaces, connector aliases, leading zeroes, MCU
names, or `!`/`^`/`~` prefixes. Edit a local switch pin by setting its
canonical field to `PA1` or `PA2` the same way. Polarity and pulls are
separate electrical strings and do not use those prefixes; see
[local switch electrical settings](switch_config.md) and the feeder submenu.

Accepted ports are those the selected MCU's STM32 headers define, GPIOA
through GPIOI, matching upstream's enumeration. A defined port is not a pad
bonded on the board or package.

Callable H723 STEP/DIR, switch, PWM, and paired-ADC bindings are implemented.
Reservations do not run motors. ACTUATION stays 0 unless explicitly opted in.
The callable board-startup composition applies saved pins and the resource
plan and attaches the local controller at READY. The hosted diagnostic path
starts it after Klipper configuration; physical qualification remains open.

The AFC Lite defaults come from AFC's `config/mcu/AFC_Lite.cfg`. Its BoxTurtle
preset uses TRG1..4 as BAY inputs and EXT1..4 (SW7–SW10/PE8–PE11) as separate
LOAD inputs, with the template's active levels and pull-ups. The local
toolhead-arrival input is assigned to SW6/PE7 and must actually be wired
there for switch arrival; the stock AFC Lite template leaves that printer-side
sensor elsewhere. With explicit calibrated [FPS arrival](../docs/fps-arrival.md),
the switch may be disabled. Its menu fields, pin group and Klipper reservation
then disappear rather than assigning a dummy GPIO.
For DiffA/DiffB on SW1/PC4 and SW2/PC5, the preset moves HUB to SW11/PE12
and BAY0 to SW12/PE13. These two connectors must be disconnected from the
former switched Turtle Neck buffer. See the [candidate wiring and build
record](../tests/release_matrix/README.md). Encoder and current-sense pins
are not invented. A missing required assignment is a build error naming the
role.
An eight-lane composition needs eight explicit assignments; a four-port
preset does not add another MCU or copy its pins.

With analog feedback selected, `km_config_pins()` accepts canonical BUFFER_A
and BUFFER_B names on STM32H723 and returns `KM_ERR_NOT_SUPPORTED` for that
analog profile on any other STM32. It does not validate ADC routing. SW1/PC4
and SW2/PC5 are optional editable presets. A legacy HUB=PC4 or TRG1=PC5
conflict exists only when those pins are used; the Diff preset remaps them,
and user overrides can choose a different verified wiring. The factory
rejects duplicate pins.
Saved overrides stay overrides when the device, board or feedback mode
changes, so update buffer A/B when the wiring changes.

`km_config_pins_view()` creates one immutable, static-lifetime map of
canonical pin IDs and a role-ordered claim list for serialized startup
consumers. The view is shared read-only, avoiding multiple 1.6 KiB copies in
small MCU images. Callers that require an independent value can use
`km_config_pins()`, which copies the same validated view to caller storage.
Both APIs reject malformed names, missing or extra lane entries, duplicate
pins and a non-H723 analog profile. Failed calls preserve the caller's output.
Disabled pin configuration returns `KM_ERR_UNAVAILABLE`. Diagnostic indices
identify conflicting claims or the next malformed list entry. Claim owner IDs
match `enum km_pin_role`; unused entries are `KM_PIN_UNUSED`.

Enabled pin groups also emit `RESERVE_PINS_klipper_mmu_<role>` constants.
Klipper's MCU identification reads them, and the existing pin resolver
rejects ordinary host pin commands, including aliases, that would claim an
MMU pin. There is no host-core patch or dummy host stepper. Existing
incompatible reservations are errors. This covers the normal Klipper path.

This is the pin portion of board configuration. `km_config_h723_resources()`
adds timer, channel, and ADC-unit claims from the shared drivers and validates
that plan. Pin reservations alone do not reserve a whole peripheral. The
shared ADC driver excludes ordinary use of every channel on a locally owned
unit until reset. Callers still merge other MCU users and prevent later
conflicts. Accepted PWM query and local-pair APIs reject unsupported timers
and channel aliases before I/O. A lane may mix supported timers; cross-lane
whole-timer sharing is rejected. The driver applies timer updates. Hardware
qualification stays open. Hosted diagnostic startup uses verified boot
identity and remains a separate opt-in from actuation permission.

Native configuration tests compile the production factory with sanitizers
against saved upstream Kconfig choices. Full upstream builds check emitted
constants through the unmodified host pin resolver. The interactive menu
check changes and saves arrival and hub pins and checks that
`make olddefconfig` preserves them. Synthetic names such as PF1 are test
inputs, not wiring advice. See
review evidence.

## Feeder electrical levels and timing

**Feeder electrical levels and timing** stores ENABLE, STEP and forward DIR
for the saved STM32 electrical settings, one value per lane, commas and no
spaces. Enter `0` for low or `1` for high (`0,1,0,1` alternates forward
level). Active-low ENABLE is held high while disabled. Inactive STEP is the
opposite of the configured active level. These strings are not pin names and
do not use `!` or `^`.

Four-lane AFC Lite ENABLE/STEP defaults and the BoxTurtle DIR default follow
the AFC Turtle template (`!M*_EN`, `M*_STEP`, `!M*_DIR` in that file). Other
mechanisms need an explicit direction. Custom boards and other lane counts
need explicit lists. Defaults are references, not measured wiring.

Pulse active and inactive minima, DIR setup and hold, ENABLE setup, maximum
callback lateness and the driver speed ceiling apply to every feeder lane.
Defaults are development starting values, not measured interrupt budgets.
`km_config_feeders()` returns the existing per-lane driver configuration,
using the same validator as driver initialization and the GPIO adapter. It
rejects malformed level lists, a speed ceiling that cannot fit the minimum
pulse cycle, and feed or retract settings above that ceiling. All lanes are
validated before any write; failure preserves the caller's array, and unused
lanes are zero on success.

The factory does no I/O. The callable board-startup composition applies saved
pin and feeder configuration with the resource plan and attaches the local
controller at READY. An automatic startup caller remains pending.
[Input bias](switch_config.md), [TMC register settings](tmc_config.md), rewind
timing, and physical qualification stay separate. The TMC menu saves register
settings; local UART attachment still depends on the existing shared API
(`WANT_TMCUART` and `WANT_TMCUART_LOCAL`). The
[H723 resource factory](h723_resources.md) rejects unsupported PWM timer and
channel aliases before I/O and records whole-timer and paired-ADC claims.

Pin-factory calls are serialized during setup. The candidate plan uses fixed
scratch storage and preserves caller output on error without a large
automatic stack object. Persist caller plans on the MCU as well.
