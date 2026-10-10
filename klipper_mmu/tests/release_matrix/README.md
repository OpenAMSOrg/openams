# Pinned release integration check

Run from the Klipper-MMU repository root:

```sh
python3 -B tests/release_matrix/run.py
```

The runner requires the SHA256-pinned Klipper `ce7002bedf37e938bb483572949f3703ac6476cb` source archive at `build/klipper-ce7002bedf37e938bb483572949f3703ac6476cb.tar.gz`. It refuses a pre-existing output directory; use `--build-dir` for another disposable run. Results stay under `klipper-mmu/build/release-matrix/`. Each retained image includes `manifest.json` with the upstream and product commits, product dirty state, transport, actuation gate and hashes of the firmware binary, ELF, dictionary and configuration. Use that record when matching a bench result to a build; a dirty product checkout is a development artifact, not a reproducible release candidate. The runner does not access hardware, installed printer services, or sibling repositories.

The check extracts upstream into that directory, verifies every source hash in the reviewed driver manifest, then runs the product installer's separate driver, MCU hook, and host-extra actions. The driver action must leave `src/Kconfig` unchanged. Before the hook is installed, a stock image is built for each selected H723/F407 target, with the package-owned driver options undefined. Each matching default-off row with the hook must give the patched drivers the same preprocessed compiler input, as must one stock build with the package link pointing at a missing path (the optional includes). Klipper's own Kconfig engine saves 46 configurations: STM32H723 default-off; BoxTurtle with the AFC Lite switched-buffer preset; four analog DiffA/DiffB BoxTurtle images on PC4/PC5 that end LOAD on FPS arrival without a toolhead switch (`boxturtle-h723-{usb,can}[-safe]`: USB or CAN, each diagnostic-only or actuator-permitted); STM32F207, F405, F407, F429, F765, G431, G474, H743 and H750 default-off, core-only Custom, and safe and actuator-permitted hosted Custom images with a manual one-lane pin map; H723 USB images for five-lane EMU; and H723, F207, F405, F407, F429, F765, G431, G474, H743 and H750 USB images for four-lane NightOwl, each diagnostic-only and actuator-permitted. `make olddefconfig` must preserve each selection, including the transport, boot offset, 25 MHz crystal, analog flag, ADC deadlines and sampling period where selected, hosted startup, actuator permission where selected, and every chosen physical pin. H743/H750 use upstream’s default 8 MHz crystal, default 400/480 MHz core clock, USB PA11/PA12, and no bootloader offset. The check rejects duplicate pin assignments before retaining an artifact. All 64 configurations must link an ELF with a clean build between them. Each retained image has its matching `klipper.dict` and `autoconf.h` beside the ELF, binary, and `.config`. After installing and removing the add-on, the matrix checks host command encoding and response parsing against the retained image dictionaries. It exercises Klipper's configuration and reconnect path against the four BoxTurtle analog dictionaries, including probe init in safe images. It also compiles and runs the native F407 TMC-UART ownership regression against the just-installed pinned upstream sources. The matrix then compiles and runs the native H723 analog BoxTurtle startup fixture against both USB image configurations with no test-only Kconfig overrides. The diagnostic image must sample sensors while rejecting START and keeping outputs idle; the actuation image must complete a mocked LOAD/FOLLOW/UNLOAD cycle, with LOAD ended by FPS arrival at the image's own threshold, hold and minimum travel. The composed FPS arrival checks run over the USB actuation header. A separate GPIO fixture reads the exact actuation image pins and polarities to check all four feeders' STEP/DIR/ENABLE ordering, isolation and safe-off. The joined host fixture queries production status and sensor replies through the image dictionary. Upstream driver files, both source hook files, and an unrelated host-extra sentinel must retain their original bytes.

The EMU images use five lane-local LOAD switches as a virtual HUB boundary and
have no separate HUB GPIO. Their PD/PE/PC pin map and explicitly selected
electrical levels, pulls, TMC addresses and current limits are illustrative,
not known wiring for a fitted controller. The matrix checks the USB entry,
diagnostic-start symbol, saved configuration, exact-image host wire formats,
and production profile, pin, switch, feeder, TMC and resource factories
compiled against each saved image configuration. The actuator-permitted image is solely for software tests and is not authorized for flashing or motion. A separate native board
startup fixture checks five passive lanes, delayed readiness of lane five,
and safe shutdown with simulated factories and drivers. The matrix also runs a
native runtime fixture against the saved actuation image's `autoconf.h` and
`klipper.dict`. It enters production hosted diagnostic startup, samples the
production switch service with a synthetic pin source, proves the selected
lane's physical LOAD input becomes the virtual HUB reading, then dispatches
production LOAD/FOLLOW/UNLOAD commands and checks retained terminal lookup,
wrong-sequence rejection, exact ACK, lane isolation, and safe-off. This remains
a software-only test: it does not qualify real wiring, GPIO timing, motors,
current, or a mechanical cycle.

The matrix also runs a virtual TMC fixture with the exact actuation image's
four feeder configurations, checking current confirmation, startup readiness
and fault isolation. These virtual devices do not measure electrical current.

To rerun the wire check separately against one retained image dictionary:

```sh
KM_WIRE_KLIPPER_SOURCE="$PWD/build/release-matrix/klipper" \
KM_WIRE_DICTIONARY="$PWD/build/release-matrix/artifacts/boxturtle-h723-usb/klipper.dict" \
python3 -B tests/klipper_host_wire_pinned/run_tests.py
```

This verifies wire formats and field widths with Klipper's actual parser. The
matrix also drives one LOAD from the host wrapper through the compiled shared
MCU command handlers, then carries the C-produced terminal through PEEK,
consumer acceptance, and exact ACK. That native boundary test uses a simulated
sensor timeline and mock drivers. It does not exercise the H723 peripherals or
prove physical motion on a BoxTurtle.
The separate H723 native fixture uses the release image's real Kconfig values
and production calibration and diagnostic-start commands, but still replaces
GPIO, ADC, timer and motor peripherals with test shims.
The matrix also runs one joined host-to-H723 LOAD/FOLLOW/UNLOAD cycle against
the USB actuation image and its exact dictionary, with toolhead-switch arrival
restored over its header by `shim/switch_arrival_autoconf.h`. It sends Klipper-encoded
commands into a fresh native startup process for each of four lanes, drives
synthetic lane-local BAY/LOAD, HUB/toolhead and Diff observations, decodes
retained terminals, rejects a wrong-sequence ACK, ACKs each exact head, and
checks selected feeder motion, other lanes still, and outputs off. It does not
exercise physical GPIO/ADC edges, mechanics, or a live OpenAMS print.

The H723, F207, F405, F407, F429, F765, G431, G474, H743 and H750 NightOwl images use the same passive-rewind, switched-buffer profile,
with separate physical HUB, BAY, LOAD, and toolhead inputs. Both illustrative Custom pin
maps, TMC UART addresses, requested currents, input levels, and pulls are
illustrative test values, not verified NightOwl wiring. The safe image reaches
hosted diagnostic READY but rejects START with all feeders idle. The actuation
image exercises synthetic switch transitions through production
LOAD/FOLLOW/UNLOAD commands, retained terminal lookup and ACK, lane isolation,
and safe-off. This tests reuse of the shared startup and control path across the admitted MCUs,
including host-lease-loss safe-off during an active LOAD. It does not establish
NightOwl hardware support or qualify pin routing.

The one-lane Custom/F207, Custom/F405, Custom/F407, Custom/F429, Custom/F765, Custom/G431, Custom/G474, Custom/H743 and Custom/H750 USB pairs use the shared local GPIO and TMC UART
paths with a guarded boot identity and hosted diagnostic startup. Its safe
image reaches READY but denies START and leaves the mock feeder idle; its
separate actuator-permitted image runs a synthetic physical-HUB
LOAD/FOLLOW/UNLOAD cycle with terminal ACKs. The release matrix verifies each
saved configuration, dictionary and artifact hashes and runs both hosted modes.
No F207, F405, F407 or F765 wiring, electrical response or physical motion has been qualified.

The [AFC Lite manufacturer's manual](https://raw.githubusercontent.com/xbst/AFC-Lite/refs/heads/master/Docs/AFC-Lite_Manual.pdf) specifies STM32H723 with a 25 MHz crystal. Its USB path uses PA11/PA12 and **no bootloader offset**; its CAN path uses PB8/PB9 and a **128 KiB offset**. These transport settings are selected in upstream Klipper's Kconfig, not in a separate MMU menu. The matrix checks the actual ELF entry address as well as the saved setting. An older matrix accidentally combined USB with the CAN offset and left the clock at 8 MHz. Its archived images are not board candidates.

The analog BoxTurtle candidate uses the pinned AFC Lite connector aliases (`config/mcu/AFC_Lite.cfg`) and four-lane Turtle template (`templates/AFC_Turtle_1.cfg`) from the [pinned AFC archive](../../references/AFC-Klipper-Add-On-5ea84ea6fab75b9b49db090f309154d1e52272bb.tar.gz). Wire the local inputs as follows, replacing the old switched Turtle Neck buffer on SW11/SW12:

| Connector | MCU pin | Analog candidate role |
| --- | --- | --- |
| SW1 / SW2 | PC4 / PC5 | OpenAMS DiffA / DiffB |
| SW3 / SW4 / SW5 | PB0 / PB1 / PB2 | Bay 1 / 2 / 3 |
| SW6 | PE7 | Unused: FPS arrival (optional toolhead switch in the menu) |
| SW7 / SW8 / SW9 / SW10 | PE8 / PE9 / PE10 / PE11 | Load 0 / 1 / 2 / 3 |
| SW11 | PE12 | HUB switch |
| SW12 | PE13 | Bay 0 switch |

The feeder STEP pins remain PD4,PC12,PE2,PD15; PA1 is the lane-0 rewind-forward output and cannot also be STEP. SW11/SW12 must be disconnected from the former switched buffer before reuse. The ADC limits in these candidates are bounded provisional values, not measured board timings. The 1 ms inter-pair delay prevents continuous conversion-retry interrupts between pairs; conversion deadlines remain active during each pair. Each `boxturtle-h723-{usb,can}-safe` artifact compiles diagnostic startup with actuator START disabled and a receive-only raw DiffA/DiffB probe for calibration capture after unpowered pin/electrical verification. Probe mode and calibrated diagnostic startup use separate host configurations and require an MCU reset between them. Its matching image without `-safe` separately compiles experimental actuator permission and omits the probe; do not flash or operate it until the diagnostic image has established measured Diff calibration, motor-current setup, safe-off, and timing on the exact hardware. All remain **software-only candidates** pending that qualification. The F405 and F407 pins are illustrative software-test assignments, not verified board wiring. The matrix checks installed build composition and reversibility; it cannot establish physical operation.

Record the staged physical checks against the [BoxTurtle H723 bench acceptance](../../docs/boxturtle-h723-acceptance.md).

The G431/G474 passive images select 170 MHz upstream Klipper USB on PA11/PA12.
Their default-off boot identity uses the RNG with Klipper's existing HSI48
clock; it must preserve the shared USB clock and reject an unexpected clock
route or previously owned RNG. The native provider test checks clock routing,
health errors, ownership, deadlines and shutdown, and cross-compiles against
both pinned CMSIS headers. A 25 MHz PLL reference is excluded because the
pinned G4 clock setup requires an exact 4 MHz PLL input divider. These images
retain the same manually configured passive Custom and NightOwl behavior;
they do not enable the H723-only ADC or PWM extensions.
