# Optional Klipper source integration

For a BoxTurtle/AFC Lite/OpenAMS board specifically, see the
[BoxTurtle quick start](boxturtle-quick-start.md) for one condensed build,
flash, host-config and commissioning path; this page covers the installer
in full generality.

The installer adds the separately maintained package to an explicitly selected
Klipper source checkout. Development and validation currently use disposable
checkouts. The MCU module supplies clock diagnostics, callable command/sensor
services and saved control settings. Source installation does not make the MMU operational or qualify
it for hardware use.

## One installer: the openams checkout

This package now lives inside the openams repository as `klipper_mmu/`, and
openams's own `install.sh` drives it. There is no separate install step, and
openams does not reimplement any rule here: it calls this tool.

From an openams checkout, with `-k` pointing at the Klipper checkout:

```sh
./install.sh                       # links klippy/extras/klipper_mmu (host extra)
./install.sh --with-mcu-addon      # also installs the MCU source hooks
```

Those two commands are exactly:

```sh
python3 klipper_mmu/tools/install.py install-host <klipper dir>
python3 klipper_mmu/tools/install.py install <klipper dir>
```

- The host-extra link is created on every run and is inert until a
  `[klipper_mmu ...]` section names it, so printers without an MMU are
  unaffected.
- The MCU add-on is opt-in because it is the only step that edits Klipper's own
  `src/Kconfig` and `src/Makefile`. The installer prints both edited paths and
  the undo command before it runs.
- `./install.sh -u` removes all of it: the host-extra symlink, and the marked
  Kconfig/Makefile blocks plus the `src/klipper_mmu` symlink when they are
  present. A checkout that never had the add-on is reported as absent, not as
  an error.
- `install.sh` refuses to run when `klippy/extras/klipper_mmu.py` exists,
  because that file would compete with the extra name, and it never links a
  module named `klipper_mmu` from openams's own `src/`.
- Moonraker's one `[update_manager openams]` entry covers this directory: an
  update pulls the new sources through the symlinks. Rebuild the MMU firmware
  afterwards; the MCU module is compiled into it.

Everything below still applies and describes each action in full generality.

From the `klipper-mmu` package directory:

```sh
python3 tools/install.py install /path/to/disposable/klipper
python3 tools/install.py check /path/to/disposable/klipper
```

Ordinary hook installation changes only these paths in that checkout:

- `src/klipper_mmu`: a symlink to this entire package.
- `src/Kconfig`: one marked optional include, `osource "src/klipper_mmu/mcu/Kconfig"`.
- `src/Makefile`: one marked optional include, `-include src/klipper_mmu/mcu/Makefile`.

Both includes are optional (Klipper's kconfiglib supports `osource`). If the
package is moved or deleted, `make menuconfig` and `make` still work for other
firmware built from that checkout, such as the printer mainboard. An MMU
configuration then builds without the add-on, so run `check` before building MMU
firmware. Earlier packages wrote mandatory `source`/`include` lines inside the
same markers. `check` reports those as needing an upgrade (status 2), `install`
replaces them in place, and `remove` deletes either form.

`install` does not create `klippy/extras/klipper_mmu`. The host extra has its own
`install-host` action.

The module's default-off option appears in the ordinary Klipper
`make menuconfig` on STM32 targets with at least 128 KiB flash and the host
simulator. Klipper's upstream menu selects the MCU; new STM32 MMU configurations
default to manual wiring. Optional presets fill editable values. Device selection describes the
configured composition; the implemented module must separately advertise actual
runtime capabilities. Unsupported hardware features must not become available
merely because a preset was selected. The mechanism, optional-feedback, motion
and protection menus now feed the common control-profile factory; see
[saved firmware settings](../mcu/config.md). STM32 targets also have an optional
[pin configuration menu](../mcu/pin_config.md) and ordinary Klipper pin reservations.
Open **Device, board and lanes** for the composition and controller defaults
(see [selectors](selectors.md) for selector machines).
Under **Configure and reserve MMU pins**, wiring is grouped with the corresponding
feeder/TMC, presence/buffer, and rewind electrical settings. Pins such as PA1 or
PA2 are editable independently of the preset; polarity and pulls use separate
fields. The catalogue includes all planned devices and variants, with unimplemented
compositions identified. GPIO/UART supports the accepted targets in the
rollout map; local ADC and paired PWM still
have H723-specific capability gates.
Existing `.config`
symbol names and saved overrides remain compatible; no separate configuration
program is required.
For an AFC Lite STM32H723, use the manufacturer's 25 MHz crystal setting in
that upstream menu. Its USB path uses PA11/PA12 with **no bootloader offset**;
its CAN path uses PB8/PB9 with a **128 KiB offset**. The
[release matrix](../tests/release_matrix/README.md) saves and checks both
choices. Transport and bootloader settings are not MMU device-profile presets.
Its feeder submenu also saves lane polarity and pulse timing; validation rejects
incompatible speed settings before board initialization.
The [TMC submenu](../mcu/tmc_config.md) saves UART addresses and driver settings,
requiring an explicit motor/board current ceiling before its factory is available.
The [switch submenu](../mcu/switch_config.md) saves polarity, pulls and buffer
direction. Local arrival wiring and the switched-buffer mapping must be explicit;
analog DiffA/DiffB does not inherit digital switch settings.
The [rewind submenu](../mcu/rewind_config.md) requires explicit bridge duty and
reversal limits; the operation profile cannot exceed the bridge duty ceiling.
The H723 analog candidate has hosted diagnostic startup and firmware command
attachment after measured calibration is delivered. It still needs electrical,
timing, current, safe-off, and motion qualification on the actual BoxTurtle;
other board and device choices are not covered by that candidate.

Ordinary installation does not edit `.config`, printer configuration, services,
or upstream motion code. It does not build, flash, or restart anything, and it
does not link the host extra. The separate host action below only adds that
symlink.
It refuses an incomplete source package, foreign destination, altered integration
block, unmanaged include or the earlier experimental integration. Repeated
installation is a no-op when the hooks and link already match.

## Keep MMU firmware configuration separate

Use a dedicated persistent Klipper source checkout for qualification builds;
do not install or update driver extensions in a running printer checkout. Within
that checkout, use Klipper's native config/output overrides consistently:

```sh
make KCONFIG_CONFIG="$PWD/klipper-mmu.config" OUT=out-mmu/ menuconfig
make KCONFIG_CONFIG="$PWD/klipper-mmu.config" OUT=out-mmu/ olddefconfig
make KCONFIG_CONFIG="$PWD/klipper-mmu.config" OUT=out-mmu/ -j4
```

These are ordinary Klipper targets, not a second configuration interface.
Pass `KCONFIG_CONFIG` on the make command line: the pinned upstream Makefile
assigns it directly, so an environment-only setting is insufficient. Save and
back up `klipper-mmu.config`; the dedicated output contains its own generated
header, firmware and dictionary. A normal printer `.config` and `out/` are not
rewritten by these commands. Repeat both overrides on every MMU invocation.

Use `make KCONFIG_CONFIG="$PWD/klipper-mmu.config" OUT=out-mmu/ clean` to remove
only that MMU output. Do not use upstream `distclean` for isolation: its pinned
recipe explicitly deletes `.config` and `.config.old` regardless of the
config override. Check installation hooks before loading saved MMU settings;
unknown symbols can be lost if the add-on is absent.

The source-dependent regression in
`tests/klipper_config_isolation/run_tests.py` copies a supplied Klipper tree,
installs there, and verifies native configuration/header generation, round trip
and isolated clean while preserving printer configuration/output byte for byte.
It does not modify the input tree or link/flash a firmware image.

## Updates and removal

The link reads the package's current sources. Updating this package changes what
the next Klipper build reads; copying files or reinstalling unchanged hooks is
unnecessary. Keep the package in the same location. If moving it, remove the
integration using the original package first, then install from the new location.
The tool deliberately refuses to replace a link belonging to another location.

After upstream updates, inspect the hooks with `check`; run `install` to restore
missing unaltered hooks. Resolve upstream merge conflicts normally. The installer
does not overwrite edited integration blocks. Save and review your build settings
with Klipper's `make menuconfig` or `make olddefconfig`, and use a clean build after
changing firmware sources or removing the add-on.

```sh
python3 tools/install.py remove /path/to/disposable/klipper
```

Removal deletes only the exact marked blocks and the matching source link. It
preserves text added elsewhere, including edits made after installation. It
also preserves the source files' existing extended attributes through atomic
hook replacement; if those attributes cannot be copied, replacement fails. It
leaves build output and `.config` untouched. A later upstream configuration pass
can discard options no longer defined by Kconfig. Repeated removal is harmless.

`check` returns exit status 0 for installed, 1 for absent, and 2 for a conflict,
incomplete installation or error. Successful `install` and `remove` return 0;
errors return 2.

## Failure handling and checks

The Linux installer locks the source directory against other installer instances,
validates both hooks before mutation, and atomically replaces each source file
while retaining its permission mode. It attempts rollback after ordinary write
failures. If a file changed meanwhile, rollback preserves that edit and reports
the incomplete rollback. Do not edit these hooks concurrently with installation.
The three-path operation is not a power-failure transaction; after an interrupted
operation use `check`, then repair with `install` or remove the owned hooks.

The bounded test suite uses temporary checkouts and synthetic write failures:

```sh
timeout 35s python3 -B -m unittest discover -s tests -p 'test*install.py' -v
```

The [current release matrix](../tests/release_matrix/README.md) builds 34
upstream Klipper configurations, including the H723 analog BoxTurtle candidate,
and checks clean add-on removal independently of the installer's preservation
tests.
No installed printer configuration or physical device is involved in these checks.

## Explicit host extra

The host extra is a separate optional action. `install`, `remove`, and `check` still change only the MCU source hooks. `install-driver-api` remains the driver-API action. Host installation does not add those hooks or driver APIs, and those actions do not create the host extra. openams's `install.sh` runs `install-host` on every install, and `install.sh -u` runs `remove-host`; that is the whole difference from the direct command below.

From the `klipper-mmu` package directory, on a disposable checkout:

```sh
python3 tools/install.py install-host /path/to/disposable/klipper
python3 tools/install.py check-host /path/to/disposable/klipper
python3 tools/install.py remove-host /path/to/disposable/klipper
```

The only checkout path this action changes is `klippy/extras/klipper_mmu`, a symlink to this package's `host` directory. The package must contain files `host/__init__.py`, `host/protocol.py`, and `host/requests.py` before `install-host` creates the link. `check-host` on an installed link requires those same files. A missing package does not prevent `check-host` from reporting an absent link. Removing an owned link is allowed when the package is incomplete or the link target is gone. `check-host` treats a dangling owned link as an incomplete installation; `remove-host` still removes that owned link.

Repeated `install-host` or `remove-host` is a no-op when the link is already in the requested state, and a repeated install leaves the existing owned symlink in place. Updates to the package `host` sources are visible through the link; the installer does not copy them. Removal deletes only that owned symlink and leaves the package sources in place.

The checkout must already contain a top-level `Makefile` and real `klippy` and `klippy/extras` directories. Symlinked `klippy` or `klippy/extras` parents are refused, and missing parents are not created. A foreign file, directory, or symlink at `klippy/extras/klipper_mmu`, including a dangling foreign symlink, is left unchanged. An existing `klippy/extras/klipper_mmu.py` file, directory, or link, including a dangling link, is also left unchanged so it cannot compete with the extra name. Unknown actions are rejected before any change.

The installer takes an exclusive non-blocking lock on the `klippy/extras` directory for the call and closes that descriptor when it returns. One symlink create or remove has no multi-file rollback. The same concurrency limits as the original installer apply; this is not a general filesystem transaction.

`check-host` uses the same exit codes as `check`: 0 installed, 1 absent, and 2 for a conflict, incomplete installation, or error. Successful `install-host` and `remove-host` return 0; errors return 2.

Klipper loads this extra for a section of the existing form below. `mcu: mcu` selects the existing printer MCU object. `diagnostic_startup` and `diff_calibration_file` are separate opt-ins. The latter names an absolute path to a measured DiffA/DiffB JSON record in the format described by [host calibration](../host/calibration.md). The host validates and encodes the file, sends one ordinary OID calibration config command before finalization, and includes it in the config CRC. With both options enabled, the init START command follows configuration. This example is for a disposable test checkout and intentionally has neither opt-in set. `KLIPPER_MMU_QUERY` is read-only; START permission remains false. No installed-printer operation or hardware qualification is claimed.

```
[klipper_mmu NAME]
mcu: mcu
```

`feeder_rotation_distances` is another separate opt-in: a comma list of one
rotation distance per lane, in millimetres (Happy Hare's
`variable_rotation_distances`, AFC's per-lane `rotation_distance`), overriding
the firmware's own `KLIPPER_MMU_FEEDER_LANE_ROTATION_DISTANCES_UM` Kconfig list
entirely, one lane's `config_km_feeder_rotation` command at a time. The list
must have exactly one entry per lane and every entry must be positive; a
restart is needed after changing it, exactly like Klipper's own
`rotation_distance`. See [per-lane rotation distance](../site/calibration.html)
for the calibration procedure.

For live OpenAMS motion, the separately enabled `openams_motion` option also
needs OpenAMS's runtime hooks, which this repository's OpenAMS provides, as described in
[OpenAMS integration](../integration/openams/README.md), plus the host watchdog
and lease-age settings in [the connection contract](../host/connection.md).
For a BoxTurtle whose DiffA/DiffB pair is owned by the MMU firmware, use
`source_oams: klipper_mmu NAME` in its `[fps]` section; do not assign those ADC
pins to a second host reader. Installing this Klipper host symlink alone does
not change OpenAMS or grant motion. The analog H723 candidate additionally needs diagnostic startup
and an absolute path to a measured DiffA/DiffB calibration record.

## Explicit shared driver extensions

The explicit driver action adds local TMC UART access in Klipper's existing
`src/tmcuart.c`, a read-only route query and a local paired-output API in
`src/stm32/hard_pwm.c`, exclusive sequential ADC-pair ownership in
`src/stm32/stm32h7_adc.c`, and a local GPIO batch reservation in
`src/stm32/stm32h7_gpio.c`, `src/stm32/gpioperiph.c` and `src/stm32/internal.h`.
These extend the existing drivers. The single reviewed
patch, `upstream/drivers-local.patch`, also creates `src/tmcuart.h`,
`src/stm32/pwm.h`, `src/stm32/adc_local.h` and `src/stm32/gpio_local.h`. It
does not change `src/Kconfig`. This package declares the driver options
(`WANT_TMCUART_LOCAL`, `TMCUART_LOCAL_MAX_CHANNELS`, `WANT_GPIO_LOCAL`,
`WANT_HARD_PWM_QUERY`, `WANT_HARD_PWM_LOCAL_PAIR`, `HARD_PWM_LOCAL_MAX_PAIRS`
and `WANT_ADC_LOCAL`) in `mcu/Kconfig.driver_api`, with the earlier names,
types, dependencies, ranges, defaults and help. They appear in
`make menuconfig` only through the ordinary add-on hook. Without the hook they
are undefined; the patched drivers test them only with `#if`, so that build is
the same as with them off. With the hook but without the driver API, enabling
one of them stops `make` (not `menuconfig` or `clean`) with a message naming
`install-driver-api`. The ordinary `install` command still installs only the
add-on hooks and link. Each driver API is default-off and selected independently.

In a disposable checkout with the reviewed upstream source bytes:

```sh
python3 tools/install.py install-driver-api /path/to/disposable/klipper
python3 tools/install.py check-driver-api /path/to/disposable/klipper
```

The supported source hashes come from Klipper revision
`ce7002bedf37e938bb483572949f3703ac6476cb`. This checks the paths listed in `upstream/source-hashes.json` (currently thirteen),
not the Git revision or every file in a checkout. A checkout whose other
files, including `src/Kconfig`, differ from that revision is accepted when
those thirteen paths match. The thirteen files are still modified in place: before
a Klipper update that touches them, run `remove-driver-api`, update, and
reinstall only if they still match; other bytes are refused until a reviewed
bundle for them exists. The tool also verifies the
packaged patch and hash manifest against reviewed digests, applies the patch in
a temporary directory, and verifies its outputs before changing the checkout.
It needs the standard `patch` utility; each invocation has a ten-second timeout.

Driver actions only read `src/Kconfig`, except to upgrade or remove the
previous bundle below, and work before or after ordinary hook installation.
`src/Kconfig` must still be a single-link regular file with an intact or absent
add-on hook, and must not itself declare one of the package-owned options.
Edited driver bytes, foreign headers, symlinks, hardlinks, damaged hooks,
modified assets and partial states are refused. Both `src` and `src/stm32` must be real directories; parent checks
are repeated before writes and rollback. Unrelated files and `.config` are untouched. Repeated installation or
removal in the matching complete state is a no-op.

The UART local API is default-off and supports the targets listed in the
rollout map. Select an eligible STM32 target
in Klipper's `make menuconfig`, then enable `WANT_TMCUART_LOCAL` (local MCU
access to the existing TMC UART driver). In the Klipper-MMU pin/feeder
configuration, select `KLIPPER_MMU_TMC_UART_SHARED`
to compile the local binding and composed feeder. Both choices must be enabled;
installing the API alone does not compile the binding. The common configuration
validator remains available without the driver extension.

For powered rewind, select both `WANT_HARD_PWM_QUERY` (expose hardware PWM routes
to local MCU modules) and `WANT_HARD_PWM_LOCAL_PAIR`. They use Klipper's existing
STM32 hardware-PWM driver. The H723
resource plan now queries that driver instead of carrying a second pin table.
With either API missing or disabled, powered-rewind planning reports unavailable;
passive rewind requires neither. The pure pair validator rejects unsupported
timers and aliases of the same timer/channel before hardware setup. Distinct
supported timers within one lane remain valid. Validation does not reserve a
timer or check its runtime availability.

The optional H723 `WANT_HARD_PWM_LOCAL_PAIR` choice adds paired outputs inside
the existing PWM driver. `HARD_PWM_LOCAL_MAX_PAIRS` bounds allocated handles
(1..8); usable pairs are also limited by available supported timers. The current
H723 backend supports TIM2/3/4/5/12, so eight independently owned powered lanes
are unavailable with this backend. Each pair
exclusively claims its timer or timers, requires an external raw ENABLE-off
callback, and confirms compare updates before reporting success. Reversals need
a separately confirmed zero; the MMU consumer supplies the off interval.
With the H723 MMU add-on enabled, this option also compiles its
[rewind binding](../mcu/pwm_bridge_gpio.md). That binding is already accepted.
It checks saved configuration before setup. With `WANT_GPIO_LOCAL`, `NULL` and count 0 is configured admission; a supplied plan is still validated. With GPIO local off, the caller still supplies the complete external plan. Selecting this PWM option alone does not perform board bring-up, host attachment, or enable START. The default build advertises `ACTUATION=0`; the separate experimental diagnostic-startup opt-in can advertise 1 only after its additional configuration gates. Outputs start inactive, and hardware qualification is absent.

The binding's permanent native regressions run from the package directory:

```sh
python3 tests/klipper_pwm_binding/run_fake.py
python3 tests/klipper_pwm_binding/run_composed.py
```

The composed test installs the pinned extension in a temporary fixture and checks
actual shared-driver compare counts and separate ENABLE transitions. Neither test
accesses hardware.

For local sequential ADC pairs, native `make menuconfig` exposes
`WANT_ADC_LOCAL` on STM32H723 with ADC support. It defaults off. The shared
driver provides a pure route query and `gpio_adc_setup_local_pair`, with
boot-lifetime ownership of one complete ADC unit (ADC1, ADC2, or ADC3). An
ordinary ADC client and a local pair cannot share that unit, even on different
channels. Pins are the canonical GPIO values from `make menuconfig`; AFC Lite
SW1/PC4 and SW2/PC5 are optional editable presets. H723 pins must be external,
distinct, and on the driver's first-selected unit with different channels.
Samples are sequential. This is not synchronized sampling, current sensing, or
BLDC control. The MMU reader uses that local-pair call exclusively. Saved
BUFFER_A and BUFFER_B pins are what startup passes in. Without the local API,
the analog profile is rejected before hardware mutation and switch feedback
remains usable. Selecting the option adds no host command and no boot-time
start, and the default build remains at `ACTUATION=0`. The reader and saved-pin integration are
accepted, including all 56 saved-configuration tests. See
`plan/status-history.md`.

The optional native `WANT_GPIO_LOCAL` choice defaults off and covers the
accepted targets in the rollout map.
Ordinary and local source paths remain the shared Klipper drivers. ADC and
paired PWM still have narrower H723 capability gates.
The complete-batch caller reserves every intended local pin first with
`gpio_local_reserve` (nonempty distinct pins; no clock or MMIO change).
`tmcuart_local_setup`, `gpio_adc_setup_local_pair`, and `gpio_pwm_pair_setup`
consume those prior claims and never reserve pins themselves. A missing claim
returns NULL from UART setup and BUSY from ADC or PWM setup, before slot, unit,
or timer ownership and before physical mutation. Read-only PWM clock-frequency
calculations may occur before that check. UART permits one already-reserved
single-wire pin. ADC remains a sequential external same-unit pair; ordered-pair
idempotence and unit exclusion remain, and `ADC_TEMPERATURE` is not an external
GPIO. A PWM pair retains timer exclusion, inactive GPIO before alternate
function, and the existing update and shutdown behavior. `gpio_pin_is_local` is
a pure query. Claims persist until reboot, with no release. Ordinary pin
sharing remains allowed. Ordinary setup refuses a local claim before peripheral
mutation, including when that peripheral's local API option is off. UART-level
exclusion still blocks reuse of a local UART pin across local UART clients.
With `WANT_GPIO_LOCAL` off, prior caller preconditions remain, including a complete external ownership list (`NULL`, 0 is invalid), and there is no generic GPIO enforcement. With it enabled, `NULL` and count 0 selects configured admission: startup still runs the saved factories (the H723 factory validates the internal MMU plan) and skips only the missing external list. A supplied array stays a caller completeness assertion and keeps plan validation plus fieldwise inclusion. Runtime reset, read, write, and safe-off are unchanged. Explicitly called `board_startup` reserves exactly the expected-plan `KM_RESOURCE_PIN` entries once through `gpio_local_reserve` before component setup. The batch is all-or-none, omits non-PIN claims, and is not released. Trusted Klipper configuration checks `RESERVE_PINS` (MMU, transport, crystal), aliases, and `BUS_PINS` before deployment. Firmware GPIO/ADC/PWM registries cover participating driver APIs; direct MMIO is outside that guarantee. The GPIO option alone does not attach commands or start a board. The separate diagnostic-startup build option supplies an explicit startup path, and its experimental actuation opt-in gates START. Outputs are initialized inactive. Hardware qualification is absent. Some adapters may still be migrating; local setup still requires pins already reserved.

The previous fourteen-path bundle differs from this one only by its `src/Kconfig`
declarations. It is recognized by the exact pinned `src/Kconfig` hash (with an
old or current hook block at the end), using the reviewed hunk kept as
`upstream/previous-kconfig.patch`. `check-driver-api` refuses it with status 2
and names both repairs: `install-driver-api` removes only those declarations,
and `remove-driver-api` restores all fourteen pristine files. Until then the
package and `src/Kconfig` both declare the options; Klipper merges the identical
declarations, but menuconfig lists each option twice. An edited copy of those
declarations is refused rather than guessed.

Earlier driver bundles whose exact hashes differ are deliberately refused as
mixed states, including older UART-only, UART-plus-route, five-target and
seven-target bundles. Use the matching earlier package's `remove-driver-api`
action to restore its pristine sources before installing this bundle. Do not
manually mix patch versions.

Installing or selecting these driver options alone does not enable motion or
configure a board. The default build advertises `ACTUATION=0`; see
[`diagnostic_startup.md`](../mcu/diagnostic_startup.md) for the separate
experimental startup and actuation gates.
Existing host UART commands remain available. Remove the extension explicitly:

```sh
python3 tools/install.py remove-driver-api /path/to/disposable/klipper
```

Removal restores the original driver bytes (and, for the previous bundle, the
original `src/Kconfig`) and deletes only the matching owned headers. It preserves existing permission modes, including a custom header
mode if removal fails and rolls back. Driver `check` uses the same exit codes as
ordinary `check`. The directory lock coordinates installer instances; other
editors must not write the targets concurrently. Observed concurrent changes are
preserved during rollback, but this is not a filesystem transaction against
arbitrary concurrent writers or power loss. A mixed state requires inspection
and manual recovery from the reviewed sources; the tool will not guess a repair.

Installer and native driver tests retain unmodified upstream source fixtures, so they run
without a network connection or a downloaded checkout. Actual firmware build
checks use a full disposable upstream checkout separately.

The permanent PWM route regression can be run from this package directory:

```sh
timeout 175s make -f tests/klipper_pwm_routes/Makefile test
```

It stages this exact bundle through the public installer, then compiles the actual
PWM source with an inert register model. Saved pin/configuration tests require
`KLIPPER_SOURCE` to name a full disposable checkout with this extension installed;
they also accept an existing ordinary add-on hook without duplicating its menu.

The paired-output regression also stages the installed driver and uses an inert
register model, with one fresh process per case:

```sh
timeout 180s python3 tests/klipper_pwm_pair/run_tests.py /tmp/klipper-mmu-pair-results
```
