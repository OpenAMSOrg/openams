# Saved rewind bridge limits

On H723, enable the pin menu and powered DC rewind, then open **Rewind bridge
limits and outputs** in `make menuconfig`. Save a motor/bridge duty ceiling, a required
continuous off interval before reversal, and a maximum local direction-command
age. These limits apply to every configured powered-rewind lane. They are
separate from the operation profile's requested rewind duty, which may be lower
but must not exceed the bridge ceiling.

All three settings initially remain unset. No board-qualified reversal delay or
motor limit is invented for BoxTurtle, NightOwl or Custom. The direction-command
age includes callback work; it is not a promised protection response time. The
software reversal interval does not replace transistor switching dead time.

`km_config_rewind()` publishes one `km_pwm_bridge_config_t`, using the same pure
`km_pwm_bridge_config_validate()` as portable driver initialization. The factory
returns `KM_ERR_UNAVAILABLE` for a disabled pin menu, passive rewind or an unset
limit; `KM_ERR_CONFLICT` if the operation duty exceeds the bridge ceiling; and
`KM_ERR_INVALID_PARAM` for a null output or invalid driver limits. Failure leaves
the caller's output untouched. It does no I/O, starts no driver, and establishes
no current measurement or readiness. The backend still starts disabled.

The same submenu also saves a requested PWM period in microseconds and three
per-lane active-level lists for ENABLE, forward PWM and reverse PWM. All remain
unset initially. Lists require exactly one `0` or `1` per configured lane, with
commas and no spaces. A logical zero duty maps to the chosen inactive electrical
level. One period applies to all lanes to keep shared timer periods compatible;
the adapter must still decide whether sharing a timer is supported at all.

`km_config_rewind_outputs()` publishes these electrical choices separately from
the portable bridge limits. Missing settings, disabled pin configuration or
passive rewind return unavailable; malformed lane lists return invalid parameter.
Every lane validates before the output array is written, and unused lanes are
zero. A requested period in the menu's 1..1,000,000 us range is not proof that a
specific timer can produce it accurately. The [H723 binding](pwm_bridge_gpio.md)
uses Klipper's shared paired-PWM driver for period conversion, polarity and
acknowledged compare updates.

Channel routes remain in the resource factory; current-sense calibration is
separate. Full external resource enforcement, production startup and board
qualification remain required. The strict add-on
continues to advertise `ACTUATION=0`.
