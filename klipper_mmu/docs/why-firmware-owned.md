# Why the MMU owns its own control loop

Happy Hare's pinned `extras/mmu_stepper.py` submits manual moves to a Klipper
trapq, and AFC's pinned `extras/AFC_stepper.py` uses Klipper's trapq and stepper
interfaces. Klipper's MCU still times the resulting step pulses. The important
difference here is **who can change the MMU trajectory after a local sensor
sample**: those integrations plan MMU moves on the host, whereas Klipper-MMU's
optional MCU service owns the MMU motor trajectory, samples its local inputs and
can retarget or stop it without waiting for another host command. The upstream
Klipper printer-motion path remains untouched.

This gives concrete capabilities, not merely another menu for the same host
workflow:

| Capability | Reason for local ownership | Current evidence and limit |
| --- | --- | --- |
| Following and rewind | DiffA/DiffB or switches can change step/PWM output on the MCU control timeline. | Shared controller, H723 adapter and causal simulator exercise this path. Real AFC Lite/BoxTurtle timing and analog calibration remain unmeasured. |
| Ramps and immediate stop | Firmware can change its own step/dir target while preserving pulse timing and stopping outputs on local faults. | Native trajectory and composed startup tests pass; no physical motor qualification yet. |
| Optional measured current | A configured current target is distinct from a sampled motor current; a local regulator can act on samples where hardware supplies them. | The portable regulator exists. The H723 BoxTurtle binding does not yet qualify a measured-current input. |
| Host interruption | The MCU enforces watchdog and sensor safety even if host communication stalls. | Native and simulator lease-loss cases pass. The correct real watchdog and stop latency must be measured on hardware. |
| Device reuse | A common operation core takes board drivers, profiles and optional sensing instead of copying a controller per MMU. | BoxTurtle, EMU and NightOwl have H723 saved-image software cycles through the shared startup and controller. The example EMU and NightOwl wiring is unqualified; catalogue entries are not support claims. |

BLDC commutation, if later implemented, likewise needs a local high-rate loop;
a slow host event or G-code exchange is not a motor-control loop. BLDC hardware
support is deferred. The architecture does not require current sensing, an
encoder, or a screen for the baseline MMU; each adds a specific observable
capability when its board path is configured and tested.

The acceptance claim is measurable: with the same host request, vary local
buffer input in the causal simulator and observe a changed MCU output before
another host command; then repeat with captured BoxTurtle timing and validate
against an unused physical run. Until that last gate, the advantage is an
implemented control architecture with software evidence, not a measured
BoxTurtle performance result. See [architecture](architecture.md),
feature map, and measurement plan.
