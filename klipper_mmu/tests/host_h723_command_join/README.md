# Pinned host to BoxTurtle H723 command join

This joins the pinned Klipper message wrappers and `host.command_transport`
with the existing native H723 board fixture. The fixture receives the actual
host-produced Diff calibration bytes, runs production calibration delivery and
diagnostic startup, then dispatches encoded host heartbeats and
LOAD/FOLLOW/UNLOAD commands to production firmware handlers. Each of the four
lanes gets a fresh native process. The fixture checks its BAY and LOAD input
route, accepted command identities, selected-feeder motion, other feeders
staying still, retained terminal delivery and exact ACK, and outputs off after
UNLOAD. The pinned host `read_physical_state` adapter also sends all five
snapshot and sensor queries through the exact image dictionary. Their replies
come from the production `command_km_snapshot_part` and
`command_km_sensor_part` handlers and production `sendf` message encoders; the
fixture JSON carries no status or sensor values. The host asserts owner lane,
stage, BAY/LOAD selection, shared HUB/toolhead presence, and inactive outputs
at lifecycle points for every lane.

Lane 0 also uses the pinned AFC BoxTurtle reference geometry to delay switch
transitions until the production feeder has generated enough steps: up to
155 mm to HUB and 1725 mm from HUB to toolhead on LOAD, then the reverse Bowden
distance and the configured nominal 65 mm post-HUB clearance on UNLOAD. Host
heartbeats continue throughout these long moves. The 20 mm toolhead-clear
transition during UNLOAD is a synthetic fixture choice, not a measurement.
This catches motion and deadline defaults that could pass a millisecond-scale
switch script but fail the reference travel budget.
An independent exact-image case withholds heartbeats during LOAD. It requires
the firmware to stop its physical STEP output at lease expiry, retain the
LOAD_FAILED terminal and fault state, and stay off after heartbeats resume.

Run against one matching release-matrix analog actuation image and dictionary:

```sh
make IMAGE_AUTOCONF=/absolute/path/to/boxturtle-h723-can/autoconf.h \
  KLIPPER_DICTIONARY=/absolute/path/to/boxturtle-h723-can/klipper.dict \
  EXTRA_CONFIG=$PWD/../klipper_board_startup/shim/switch_arrival_autoconf.h
```

The analog release images end LOAD on FPS arrival without a toolhead switch.
This join drives the toolhead switch, so `EXTRA_CONFIG` restores switch
arrival on SW6/PE7 over the image header, as the release matrix does.

## FPS mode

`MODE=fps` joins the same actuation image as built, with no `EXTRA_CONFIG`:
FPS arrival, no toolhead switch. The host's own configuration commands reach
the fixture first: the `diff_calibration_file` bytes and, when a step estimate
is stored, `config_km_fps_steps` (the calibrated HUB-to-arrival distance;
without one, LOAD is refused until this session's own CALIBRATE measures it).
The fixture models a filament tip that moves
with the feeder's model position. HUB asserts past the 155 mm reference, and
the DiffA/DiffB pair, through the calibration, reads 0 (uncompressed) except
while following (held at 500) or while LOAD or CALIBRATE pushes the tip past
the 1725 mm reference Bowden (800, above the 700 threshold). One timeline:

1. `KLIPPER_MMU_CALIBRATE MMU=box BAY=0` stores the step estimate (the
   reference Bowden within sampling) in `bowden_estimate_file`; firmware
   adopts the measured distance at once, so LOAD needs no restart to use it.
2. LOAD lane 0 ends by FPS arrival at the reference distance, FOLLOW holds the
   buffer at mid-range, and UNLOAD rewinds with the buffer at 0 and clears HUB
   by the configured 65 mm. Each retained terminal is consumed and ACKed.
3. Tool change: a spool is seated in bay 1, then LOAD and UNLOAD lane 1, with
   lane 0's feeder still.
4. A new MCU process and host objects (a restart): the host restores the
   calibrated distance from `bowden_estimate_file` and the firmware profile
   uses it for the next LOAD; the outer travel limits never narrow.
5. The DiffA/DiffB pair fails during FOLLOW. The host retains the
   `FOLLOW_FAULT`; `KLIPPER_MMU_ACK_FAULT` ACKs it; once the pair is repaired,
   `KLIPPER_MMU_RECONCILE_PATH ACTION=ADOPT LANE=0` clears the fault and
   `KLIPPER_MMU_RECOVER_SESSION` releases the retired session. The managed
   unit's next refresh prepares a new session for the adopted lane
   (`choose_occupied_host_session`), and UNLOAD succeeds.

Without `OPENAMS_SOURCE`, a small runtime stand-in starts each operation on
the managed unit and settles its waiter. With it, the actual OpenAMS runtime
runs `LoadBay`, `Unload` and `Calibrate` and its monitor refreshes the unit.
OpenAMS then pauses on the accepted fault and latches that lane; it has no
verified clear, so step 5 restarts the host (not the MCU), whose connect
prepares the adopted lane's session before UNLOAD.
The release matrix runs this mode over its `boxturtle-h723-usb` image, and
again through OpenAMS when `OPENAMS_SOURCE` is set:

```sh
make MODE=fps IMAGE_AUTOCONF=/absolute/path/to/boxturtle-h723-usb/autoconf.h \
  KLIPPER_DICTIONARY=/absolute/path/to/boxturtle-h723-usb/klipper.dict
```

The tip model's edges are quantized to 200 ms fixture steps and follow the
reference geometry exactly; they are synthetic, not measured. As in the other
modes, the fixture calls the core update every millisecond; the production
controller timer is exercised by the
[composed checks](../../docs/fps-composed-checks.md).

Set `KM_WIRE_KLIPPER_SOURCE` to the pinned Klipper checkout if it is not at
the test suite's default location. The build lives under this directory's
`build/` tree. No serial device or hardware is opened.

Set `OPENAMS_SOURCE` to an OpenAMS ams-family checkout to run the managed
lane-0 LOAD/UNLOAD case through its actual FPS source, manager topology,
group selection, hardware-world builder, monitor, `oams_state` reducer, and
`oams_runtime` waiter. The OpenAMS checkout is this repository's root. Without `OPENAMS_SOURCE`, the case uses a terminal-consumer stub so
the pinned release matrix remains self-contained. Both modes use the same
H723 image, synthetic sensor edges, reference-distance STEP counts, and
retained-event ACK checks. The optional mode reads the external source but
does not install or modify it. It preserves the full direct-runtime LOAD/UNLOAD
timeline through the actual manager and runtime. A separate bounded fixture
run exercises the registered `OAMSM_UNLOAD_FROM_TOOLHEAD` route and asserts that
its configured unload macro runs only after the managed unit confirms reverse
FOLLOW for the loaded unit. That probe stubs the subsequent autonomous-unload
continuation because its printer macro is not installed in the native fixture.
It also checks retained terminal ACK and host-lease renewal through the
preceding managed LOAD. The registered `OAMSM_LOAD_TO_TOOLHEAD` handler is
exercised only through its unknown-group validation path; the full blocking
load lifecycle remains covered through the manager's runtime in the existing
timeline.

This is a native integration check: the real H723 startup, ADC/sensor,
calibration, command, message, and operation code runs in one process per lane.
GPIO, ADC values, timer/clock services, and motor-driver electrical effects
still come from the board fixture shims. It validates a complete native
host-to-firmware lifecycle, but does not qualify physical timing, wiring,
current control, or a mechanical LOAD/FOLLOW/UNLOAD cycle. The optional
OpenAMS-source mode joins the actual manager/runtime to the managed host unit
and exercises the registered command handoff against the synthetic MCU fixture,
but does not execute a real printer macro or an installed Klipper/OpenAMS session.
