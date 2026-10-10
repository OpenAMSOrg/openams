# H723 MMU resource plan

`km_config_h723_resources()` expands the saved pin configuration into MMU-side
hardware claims before any peripheral is initialized. It validates the same
canonical GPIO assignments and adds the resources used by the currently
supported physical bindings:

- Powered rewind resolves both directions of every lane through the accepted
  shared PWM query and local-pair bridge. Pure pair validation rejects
  unsupported timers and channel aliases before I/O. Different pins that alias
  one timer channel conflict. Each used timer is claimed exclusively. A lane
  may mix supported timers, including two directions on distinct channels of
  one timer. Cross-lane sharing of a timer is rejected because the driver
  updates the whole timer. Hardware qualification of those updates stays open.
- Analog DiffA/DiffB claims the unit and two channels from the shared driver's
  first selected route. Pins must be external and distinct, on one unit, with
  different channels. ADC1, ADC2, and ADC3 are accepted. There is no copied
  route table. Both `CONFIG_WANT_ADC` and `CONFIG_WANT_ADC_LOCAL` are required;
  without the local API the plan returns `KM_ERR_UNAVAILABLE` before I/O. The
  shared driver excludes ordinary clients from that whole unit until reset. A
  legacy HUB=PC4 or TRG1=PC5 conflict applies only when those pins are used;
  the user remaps them.
- Passive rewind adds no PWM claims. Switched feedback adds no ADC claims.
- GPIO STEP/DIR and software UART use the Klipper scheduler, so this plan does
  not invent dedicated STM32 timer units for them. Their interrupt budget is a
  separate qualification requirement.

The route lookup calls the optional shared `gpio_pwm_get_route()` interface in
Klipper's actual STM32 driver. Its first-match routing table is the only routing
source. The MMU maps the returned opaque timer identity to a numeric resource
claim; it does not inspect timer registers, initialize clocks or select GPIO
alternate functions. The latter belong to the driver, so the resource plan now
contains only timer and channel. Default AFC Lite pairs use
TIM2, TIM3, TIM12 and TIM4, channels 2/1 respectively.

The accepted `WANT_HARD_PWM_QUERY` and `WANT_HARD_PWM_LOCAL_PAIR` extensions
are required for powered-rewind resource planning. If either is missing, the
plan returns `KM_ERR_UNAVAILABLE` before I/O. The route helper leaves its
output unchanged when the query is unavailable. Passive
rewind remains available without it. Unsupported pins, oversized pin values,
unknown timer identities and invalid channels never become valid claims. A
successful query does not claim hardware availability or reserve a timer.

Encoder and measured-current acquisition currently return `KM_ERR_NOT_SUPPORTED`
from this factory. Their pin/menu selection and common control behavior do not
yet establish a physical acquisition backend or complete resource plan. They
remain additive features to implement, not silently emulated measurements.

The output includes canonical resource claims and per-lane PWM routes. Callers
must use persistent output storage on the MCU and serialize setup calls. Fixed
scratch storage avoids large automatic pin-plan copies on Klipper's default
512-byte stack. Success publishes `count`; every failure sets `count=0` and other
output bytes are unspecified. A disabled pin menu returns unavailable. Detailed
conflict indices refer to the would-be claim array, with pin claims first.

This function does **not** reserve hardware or discover another Klipper
component's usage. Before setup, the board initializer must merge every other
MCU user's claims, with distinct owner IDs, and run `km_resource_plan_validate()`.
It must also prevent later conflicting configuration. Reserving just the chosen
GPIO pins is insufficient: another pin can select an already-owned PWM channel
or ADC unit. Whole timer claims deliberately prohibit external PWM sharing, even
at the same frequency. The shared ADC driver excludes ordinary clients from a locally owned unit until reset.

The callable board-startup composition invokes this plan and saved pins and
attaches the local controller at READY. An automatic startup caller, verified
boot identity, and host-command attachment remain pending. Saved PWM choices
are available through `km_config_rewind_outputs()`. The driver implements timer
updates; hardware qualification stays open. There is no automatic output setup.
`ACTUATION=0` is unchanged.
