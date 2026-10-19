# Installing OpenAMS

## What this does, and why

`install.sh` links the OpenAMS plugin into Klipper's `extras/` directory, puts
three config files in place if they are not there yet, adds one `[include oams.cfg]` line
to `printer.cfg`, adds Moonraker update and Spoolman sections to
`moonraker.conf`, and restarts the services. It never overwrites a config file
you already have. A Klipper extra is a Python file in `klippy/extras/`, so
"installing" means symlinking.

## Requirements

- Klipper and Moonraker installed, and a `printer_data/config` directory holding
  `printer.cfg` and `moonraker.conf`.
- An OpenAMS unit: a stock AMS on a Cortex Bridge (USB; see
  [Set up a Cortex Bridge printer](cortex-bridge-setup.md)), an AMS fitted with
  an OpenAMS 1 mainboard (STM32F072, usually on CAN), or an MMU running
  klipper-mmu.
- `git`, `curl`, `sudo`. `unzip` if you want the installer to set up Spoolman.
- Python 3.9 or newer in Klipper's environment, only for gco-routines.
- The printer idle. The installer stops and starts Klipper.

Run it as the user that owns Klipper, never with `sudo`; it refuses with
`[ERROR] This script must not run as root. Exiting.`

## Install

```bash
git clone https://github.com/OpenAMSOrg/openams.git ~/openams
cd ~/openams
./install.sh
```

### Flags

| Flag | Meaning | Default |
|---|---|---|
| `-k <path>` | Klipper checkout | `~/klipper` |
| `-s <name>` | Klipper systemd service name | `klipper` |
| `-c <path>` | Klipper config directory | `~/printer_data/config`, falling back to `~/klipper_config` |
| `-r <path>` | gco-routines checkout | `~/gco-routines` |
| `-u` | Uninstall: remove links, the include line and the Moonraker sections | off |
| `--with-mcu-addon` | Also add the klipper-mmu MCU add-on to Klipper's sources | off |
| `--skip-gco-routines` | Skip the gco-routines step, with no network access | off |
| `--require-gco-routines` | Any gco-routines problem stops the install | off |
| `--with-klipper-patches` | Apply the optional Klipper CAN patch series | off |
| `--remove-klipper-patches` | Reverse that patch series | off |
| `--spoolman-url <url>` | Use a Spoolman elsewhere; installs nothing | — |
| `--no-spoolman` | Skip Spoolman entirely | off |
| `--cortex-bridge` | Install the Cortex Bridge relay even with no bridge connected | when a bridge is connected |
| `--no-cortex-bridge` | Do not install the Cortex Bridge relay | — |

Three pairs cannot be combined: `--skip-gco-routines` with
`--require-gco-routines`, `--spoolman-url` with `--no-spoolman`, and
`--with-klipper-patches` with `--remove-klipper-patches`. Neither patch flag
combines with `-u`.

To install against a non-default layout:

```bash
OPENAMS_NO_SERVICE=1 ./install.sh -k /path/to/klipper -c /path/to/printer_data/config
```

`OPENAMS_NO_SERVICE=1` skips every `sudo systemctl` action and prints each as
skipped. It is a test switch for trees with no systemd. For the Cortex Bridge
step, `OPENAMS_SYSTEM_ROOT=<dir>` writes its `/etc` files under `<dir>` and runs
no `sudo`, `OPENAMS_SYSFS_USB=<dir>` is the USB device tree it looks for a
bridge in, and `OPENAMS_BRIDGE_PYTHON` is the Python its relay runs with.
`--cortex-bridge` and `--no-cortex-bridge` cannot be combined.

### What the installer touches

| What | Change |
|---|---|
| `~/klipper/klippy/extras/` | One symlink per `src/*.py` |
| `~/klipper/klippy/extras/klipper_mmu` | Symlink to `klipper_mmu/host` |
| `~/klipper/scripts/` | One symlink per `scripts/*.py` |
| `~/moonraker/moonraker/components/openams_spoolman` | Symlink to `moonraker/openams_spoolman` |
| Config directory | `oams.cfg` (from [`oams_sample.cfg`](../../oams_sample.cfg)) — **only if absent**; `oams_macros.cfg` and `oams_macros_ordered.cfg` — **symlinks** to the checkout |
| `printer.cfg` | `[include oams.cfg]` above `[printer]` — only if absent |
| `moonraker.conf` | `[update_manager openams]`, `[spoolman]`, `[openams_spoolman]` — each only if absent |
| Cortex Bridge only | See [Cortex Bridge](#cortex-bridge) below |

Running the installer twice changes nothing.

### Cortex Bridge

When a Cortex Bridge is connected (running, or waiting for firmware), when its
relay is already installed, or with `--cortex-bridge`, the installer also sets
up the bridge relay. [Set up a Cortex Bridge printer](cortex-bridge-setup.md)
is the customer guide; this is what it changes:

| What | Change |
|---|---|
| `/etc/systemd/system/cortex-bridge.service` | The relay service, run as you from the OpenAMS checkout with Klipper's Python; enabled and started |
| `/etc/systemd/system/klipper.service.d/cortex-bridge.conf` | Starts Klipper after the relay (`Wants=`, so Klipper still starts without it) |
| `/etc/udev/rules.d/99-cortex-bridge.rules` | Keeps ModemManager away from the bridge and its update receiver |
| `/etc/tmpfiles.d/cortex-bridge.conf` | Creates `/dev/oams`, where the relay links one port per AMS |
| `dialout` group | Adds you to it, if you are not already a member |
| `~/moonraker/moonraker/components/openams_bridge` | Symlink to `moonraker/openams_bridge`, plus `[openams_bridge]` in `moonraker.conf` |
| `moonraker.asvc` | Allows Moonraker to restart `cortex-bridge` |
| `[update_manager openams]` | `managed_services` gains `cortex-bridge`, so an update restarts the relay |
| `oams.cfg` | On a printer without one: copied from [`oams_cortex_bridge_sample.cfg`](../../oams_cortex_bridge_sample.cfg), then the connected AMS units are added by `tools/cortex-bridge.py config --write` |

The relay's Python is the one Klipper runs with, which already has pyserial;
without it the installer installs `python3-serial` and uses the system Python.
If another relay service such as `cortex-gateway` is installed, the installer
warns: only one service can hold the bridge.

### klipper-mmu

The host extra is always linked and is harmless on a printer with no MMU:
Klipper loads an extra only when a config section names it. The **MCU add-on is
opt-in**, because it edits two files of your Klipper checkout:

```bash
./install.sh --with-mcu-addon
```

That adds `osource "src/klipper_mmu/mcu/Kconfig"` to `src/Kconfig` and
`-include src/klipper_mmu/mcu/Makefile` to `src/Makefile`, each between
`# BEGIN klipper-mmu add-on` and `# END klipper-mmu add-on` markers, plus a
`src/klipper_mmu` symlink. Both includes are optional, so firmware for other
boards still builds if the package is moved or removed. **Undo** with
`./install.sh -u` or `python3 klipper_mmu/tools/install.py remove <klipper path>`.

An MMU section does nothing until you build and flash its firmware; see the
[klipper-mmu install guide](../../klipper_mmu/docs/install.md). The installer
refuses to run if `klippy/extras/klipper_mmu.py` exists, because the two would
compete for the name.

### Spoolman

Spoolman is part of the default install, so a printer shows spool colors and
materials. The installer uses the first server it finds, in this order:

1. a `[spoolman]` section with `server:` already in `moonraker.conf`;
2. a Spoolman answering on this host at `http://localhost:7912/api/v1/info`;
3. otherwise, a new one installed here from Spoolman's own release installer.

Option 3 needs `curl`, `unzip` and `sudo`, and downloads from GitHub. A failure
prints a `[WARNING]` and the install continues: OpenAMS works without spool
colors.

```bash
./install.sh --spoolman-url http://<server>:7912   # use a server elsewhere
./install.sh --no-spoolman                         # skip all of it
```

Run `./install.sh` again with either flag to change your mind later. Uninstall
(`-u`) removes only the `[openams_spoolman]` section. Spoolman, its data and
`[spoolman]` hold your spool database and may serve other tools.

### gco-routines

gco-routines is an optional Klipper extra that renders macros in ordered chunks.
The installer clones it to `~/gco-routines` and links it as
`klippy/extras/gco_routines`. **Installing it changes nothing**: no Klipper
source is patched and no dependency is upgraded. Concurrent toolchanges are a
separate, manual step, and only for a Klipper gco-routines accepts (see below):

1. In `printer.cfg`, put `[gco_routines]` **above** `[include oams.cfg]` and
   before any section or include that defines macros.
2. In `oams.cfg`, uncomment `[include oams_macros_ordered.cfg]`, directly after
   `[include oams_macros.cfg]`.

The OpenAMS load then runs in a background routine while `CLEAN_NOZZLE` runs,
and `WAIT` joins it before the toolhead reload. Stock Klipper rejects the
`render_mode` option the ordered file sets, so never include that file without
`[gco_routines]`.

It needs Python 3.9 or newer in Klipper's environment, and it works only on a
Klipper it was tested against. It identifies one by the SHA-256 of
`klippy/gcode.py`; on any other, `[gco_routines]` stops Klipper at startup with
`Unsupported Klipper GCodeDispatch baseline`. Kalico, vendor forks and Klipper
checkouts older than the tested one are usually refused. The installer says
which yours is: `gco-routines: this Klipper is a version it accepts`, or a
`[WARNING]` that it is not. After a Klipper update, check again before you
restart:

```bash
python3 ~/openams/install_helpers/gco_baseline.py --klipper ~/klipper --gco-routines ~/gco-routines
```

It prints `GCO_BASELINE=yes`, `no`, or `unknown` (no gco-routines checkout).
OpenAMS works fully without gco-routines; only the overlapped toolchange needs
it.

An existing non-Git directory, a modified checkout, another branch or a
foreign `gco_routines` path is never overwritten: the installer prints one
warning and still installs OpenAMS. `--skip-gco-routines` skips the
step with no network access; `--require-gco-routines` makes any problem stop the
install. `-u` leaves the checkout alone. To remove the extra, first remove the
activation lines, then:

```bash
python3 ~/gco-routines/tools/install_gco_routines.py --klipper ~/klipper --uninstall
```

### Klipper CAN patches (optional)

[`klipper_patches/can/`](../../klipper_patches/can/README.md) is a pinned series
of 56 patches to Klipper's CAN bus code, tested on one upstream commit. Read the
[CAN patches guide](../../klipper_patches/can/GUIDE.md) first. Nothing applies the
series unless you ask:

```bash
./install.sh --with-klipper-patches      # apply to the Klipper working tree
./install.sh --remove-klipper-patches    # reverse it
```

The installer checks series checksums, that Klipper sits at the tested commit,
and that the touched files have no local changes; it refuses and says what to do
otherwise. It edits the working tree and makes no commit, so Moonraker's update
manager then reports Klipper as dirty. MCU-side fixes need a firmware rebuild
and reflash. `-u` removes the series if applied.

## Moving from klipper_openams

Run `./install.sh` from the new checkout. The installer also rewrites any
`[update_manager openams]` section that still references `klipper_openams`,
removes stale symlinks in `klippy/extras` and `scripts` with no matching file in
`src/` or `scripts/`, and re-points the ones that still exist.

Your `oams.cfg` is never overwritten. The macro files become links to the checkout (see [update](#update)). The old `klipper_openams` checkout stays;
delete it once the new installation works. Every `klipper_openams` G-code command
still exists with the same arguments. The OpenAMS display and Mainsail panel
([openams-ui](https://github.com/OpenAMSOrg/openams-ui)) need this OpenAMS:
with `klipper_openams` they connect but show no units.

To remove `klipper_openams` once everything works, first check that nothing
still points at it (both print nothing):

```bash
grep -rn klipper_openams ~/printer_data/config/
ls -la ~/klipper/klippy/extras ~/klipper/scripts | grep klipper_openams
```

A match is usually an `[include ...]` line of your own; point it at the
OpenAMS checkout. Then move the old checkout aside, run **FIRMWARE RESTART**,
and delete it after a print or two:

```bash
mv ~/klipper_openams ~/klipper_openams.old
rm -rf ~/klipper_openams.old    # later, when all is well
```

On the STM32F072 mainboard (OpenAMS 1), two settings may need attention. Both
are optional, and most printers need neither.

**`openams_config_path`** goes under `[oams_manager]`. It is the file OpenAMS
writes back into when you edit groups, calibrate or link Spoolman spools. The
default is `oams.cfg` in the same folder as `printer.cfg` (on most printers
`~/printer_data/config/oams.cfg`). Set it only if your `[oams]` and
`[filament_group]` sections live in a file with another name, and give the
full path:

```ini
[oams_manager]
openams_config_path: ~/printer_data/config/my_ams.cfg
```

**`environment_sensor`** goes under each `[oams ...]` section. It names the
temperature and humidity sensor of that unit: the name after
`temperature_sensor` in the sensor's section. With exactly one HDC1080 sensor
(as in `oams_sample.cfg`) it is found on its own and you need not set it. With
two or more, set it on every unit:

```ini
[temperature_sensor oams]
sensor_type: HDC1080
# ... the rest as in oams_sample.cfg

[oams oams1]
environment_sensor: oams
```

See [docs/stm32f072.md](../stm32f072.md) for every config difference.

## Check that it works

After the installer restarts Klipper, the stock sample config still has
placeholders, so **an error on the first start is expected**. Replace the UUIDs
and the tag key ([configure](configure.md)), then run the bring-up ladder:

```bash
python3 ~/openams/tools/agent/bringup_check.py --selftest
```

It walks ten levels, from Moonraker up to the Moonraker update entry. Success is
every level `PASS` or `WARN`, `fail=0` in the final `BRINGUP` line, and exit
status 0. It moves no filament: `--selftest` sends only `OAMSM_SELFTEST`, which
is read-only. The level table is in
[troubleshooting](troubleshooting.md#the-three-read-only-checks), with two more
read-only checks, `config_audit.py` and `preflight.sh`.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `[ERROR] This script must not run as root. Exiting.` | Run with `sudo` | Run it as the Klipper user |
| `[ERROR] Klipper service with name "klipper" not found ...` | Different service name | Pass `-s <name>` |
| `[ERROR] Klipper installation not found in directory ...` | Klipper is not in `~/klipper` | Pass `-k <path>` |
| `[ERROR] Moonraker configuration not found in directory ...` | Config directory is elsewhere | Pass `-c <path>` |
| `... klipper_mmu.py exists and would compete with the klipper_mmu Klipper extra` | An older MMU install owns the name | Find what installed it, then remove it |
| `[WARNING] ...` about gco-routines | A non-Git or diverged checkout | Left alone on purpose; OpenAMS still installs |

More installer messages are in [troubleshooting](troubleshooting.md#installer-errors).

## Update

```bash
git -C ~/openams pull --ff-only
cd ~/openams && ./install.sh
```

Then run the bring-up ladder again. The installer added an
`[update_manager openams]` entry, so Moonraker's update button does the same
`git pull`. Run `./install.sh` afterward to link any files the release added.
On a Cortex Bridge printer the update restarts the bridge relay too; new
bridge firmware arrives with it and is installed separately
([update the Cortex Bridge firmware](cortex-bridge-update.md)).

`oams_macros.cfg` and `oams_macros_ordered.cfg` in your config directory are
links to the checkout, so the pull is enough to update them. Running the
installer on an older install replaces an unmodified copy with the link. A copy
you edited is kept as `oams_macros.cfg.bak-<date>` and the installer says so;
move what you changed into the hook macros in your own file
([customizing the toolchange](configure.md#customizing-the-toolchange)).
[What changed in oams_macros.cfg](macro-changes.md) lists the behaviour that
changed since copied macros were in use.

## Uninstall

```bash
cd ~/openams && ./install.sh -u
```

It removes the OpenAMS links (including the Moonraker component link), the
`klipper_mmu` host symlink, the MCU add-on hooks when present, the
`[include oams.cfg]` line, `[update_manager openams]` and `[openams_spoolman]`.
On a Cortex Bridge printer it also stops and removes the relay service and its
`/etc` files, the `openams_bridge` component and section, and its
`moonraker.asvc` line. It reverses the Klipper patch series if applied, and
asks whether to remove your `oams*.cfg` files; answering `n` keeps them.

Spoolman, its data, the `[spoolman]` section and the gco-routines checkout stay.

## Gotchas

- **Never run it as root.** It calls `sudo` itself where it must.
- **Never run it during a print.** It stops and starts Klipper.
- `oams_macros.cfg` and `oams_macros_ordered.cfg` in your config directory are
  links to the checkout, so do not edit them: an edit would change the
  checkout. An older install had copies; the installer replaces an unmodified
  copy with the link and keeps a customized one as `<name>.bak-<date>`. Put your
  changes in your own file, through the hooks
  ([customizing the toolchange](configure.md#customizing-the-toolchange)).

## Where to go next

- [Configure](configure.md) — edit `oams.cfg`.
- [Using OpenAMS](using.md) — load, unload, toolchange, calibrate.
- [Troubleshooting](troubleshooting.md) — match an error to a fix.
- [oams_sample.cfg](../../oams_sample.cfg) — the annotated reference config.
- [install.agents.md](../../install.agents.md) — the same install for an AI agent.