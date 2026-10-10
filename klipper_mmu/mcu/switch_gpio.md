# Local switch inputs

`km_switch_gpio_setup()` binds one initialized static core to local bay, hub and
toolhead inputs when fitted. Only an explicitly enabled calibrated FPS arrival
profile may omit the toolhead input; it then claims no pin and publishes no
positive toolhead presence. Switched-buffer inputs are included only for a switched profile;
DiffA/DiffB remain exclusively with the paired ADC frontend. Call from ready task
context before controller attachment, after validating the complete resource
plan. The binding validates all used canonical pins for the admitted STM32 target, pulls (-1/0/1) and
internal duplicates before touching GPIO. With `CONFIG_WANT_GPIO_LOCAL`, every
pin this mode actually uses (bays, hub, toolhead, and buffer low/high only for
switched feedback) must already be locally reserved. Analog feedback does not
consult buffer switch fields. A missing, ordinary or partial claim returns
`KM_ERR_CONFLICT` with the core and binding unchanged and retryable. The binding
does not reserve pins. The caller still owes full resource admission, including
a genuinely complete MCU plan and host, transport, crystal, and other external
owners. Callable [`km_board_startup_begin`](board_startup.md), when
`CONFIG_WANT_GPIO_LOCAL` is enabled, reserves precisely the expected MMU plan
PIN entries in one `gpio_local_reserve` batch after validation and core init and
before this setup. It does not reserve external complete-plan entries or non-PIN
timer/ADC claims. The hosted diagnostic startup path invokes this composition after Klipper
configuration and attaches registered commands only after readiness. The batch
is not global inventory discovery and does not by itself admit shared timers,
ADC, or transport; the startup caller validates the full resource plan. `ACTUATION=0` stays unchanged. This page does not
hardware-qualify arbitrary boards or profiles. It copies explicit settings,
claims them permanently and configures input mode only. It owns no timer or
output.

Supply `km_switch_gpio_sample` and a NULL context to the local controller, or
invoke it from a bounded composite sampling callback alongside the ADC frontend.
Each scan takes one observation before reading at most twenty pins (eight BAY, eight LOAD, HUB, toolhead,
and two switched-buffer inputs) in the controller's control task, with
interrupts enabled. That observation is a conservative earliest
acquisition timestamp for the batch; GPIO execution time cannot make earlier
readings appear newer. Controller time is observed again after the callback.
Readings are sequential rather than electrically simultaneous. Each input requires 5 ms of stable samples before its initial value or a change
is published. Ordinary bounce retains the last accepted level; an acquisition
gap invalidates it until requalified. The switched-buffer decoder rejects both
ends active. This GPIO binding measures neither encoder motion nor current.
Optional [FPS arrival](../docs/fps-arrival.md) uses the separate calibrated ADC
frontend and shared operation core.

After a successful GPIO batch is published to the core (including a valid
switched-buffer reading when configured), the sampler passes its original
`acquisition.us` to the controller. The controller records the wrap-safe elapsed
microseconds to its existing clock observation immediately before
`km_core_update`. `km_controller_get_switch_sample_delay()` exposes the latest
interval with a validity flag; an invalid batch or a controller cycle without
an accepted switch sample clears validity. This is software sample-to-update
delay, not physical switch-edge latency. It does not add a host reply or work to
the motor deadline path.

The common `km_core_set_presence_at()` entry point updates all bay readings and
the shared path once. When a path is first observed occupied during an owned
operation, it retains that operation's lane. Otherwise it reports
`KM_LANE_UNKNOWN` (255), even when exactly one bay switch is active. It preserves
previously established occupancy/source through subsequent absence or invalid
samples. Explicit reconciliation is necessary before admitting work against an
unknown occupied path. The older single-lane setter retains its compatibility
behavior for callers that already supply a source lane; physical scans use the
batch entry point.

Each input also keeps a saturating `uint16` "bounce" count of rejected
debounce candidates since this binding's setup (docs/diagnostics.md): a raw
level change that returned to the still-qualified level, or that was itself
replaced by a further raw change, before the 5 ms qualification. A clean
transition, and an acquisition-gap reset, are never counted.
`km_switch_gpio_get_bounce()` is a read-only task-context query (no GPIO
read, no state change) returning zero for every switch when unbound; the
diagnostic `km_diag_part` command (mcu/commands.md) exports it, alongside
`src/operation_core.c`'s per-lane BAY/LOAD and HUB idle-edge counters.

Shutdown invalidates only this binding's inputs without GPIO writes, timers or
clock reads. It cannot resume after `clear_shutdown`, including a failed partial
setup. Wrong callback identity/context, changed lane count/buffer mode or an
unavailable clock triggers Klipper shutdown. Callable
[`km_board_startup_begin`](board_startup.md) sets up this binding when invoked
and attaches the controller only after readiness. The hosted diagnostic startup
caller supplies the external resource plan and command attachment.
`ACTUATION=0` still denies actuator START.

Saved switch polarity/bias and buffer mapping now come from
[`km_config_switches()`](switch_config.md). See
[`board_startup.md`](board_startup.md) for the callable composition. The caller
must still supply a genuinely complete MCU plan and enforce external ownership.
Hosted startup is available only on the admitted configurations; it does not
make arbitrary profiles or peripherals supported. Physical electrical and
execution-cost qualification remain separate. A
GPIO read cannot diagnose every open/short failure. Input configuration does not
establish that a board connector has the required electrical characteristics.
