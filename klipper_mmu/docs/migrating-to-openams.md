# Migrating from the separate klipper-mmu repository

klipper-mmu now ships inside the OpenAMS repository, in `klipper_mmu/`. This
page is for a printer that installed klipper-mmu from its own checkout, using
that checkout's `tools/install.py`. Nothing about the printer's behavior
changes. What moves is where the code lives and which installer owns it.

[install.md](install.md) documents the install itself; this page is only the
move, in the order you should make it.

You will need three paths: your Klipper **source checkout** (the directory with
`src/`, `klippy/` and a top-level `Makefile`), your old **klipper-mmu
checkout**, and where you will clone **openams**.

## What stays the same

- **`[klipper_mmu NAME]` config sections.** The host extra is the same
  `klipper_mmu/host` package under the same extra name, so your `printer.cfg`
  and any included MMU config are unchanged. The section is loaded by
  `load_config_prefix` in `klipper_mmu/host/__init__.py`, and Klipper finds it
  at `klippy/extras/klipper_mmu` exactly as before.
- **`KLIPPER_MMU_*` commands.** Same host code, same command names, same
  `MMU=<name>` parameter, same replies. See
  [G-code commands](g-code-commands.md); `KLIPPER_MMU_QUERY` is still
  read-only and START permission is still false.
- **The MCU firmware build.** The same ordinary Klipper `make menuconfig`, the
  same `# BEGIN klipper-mmu add-on` / `# END klipper-mmu add-on` hook block in
  `src/Kconfig` and `src/Makefile`, the same menus under
  **Configure and reserve MMU pins**. Only the directory the `src/klipper_mmu`
  symlink points at is different, so the same commands rebuild it.
- **Saved firmware settings and presets.** No `.config` is read, rewritten or
  converted by any step below. The `Kconfig` symbol names, the device
  catalogue and the control-profile presets are unchanged, so a saved
  `klipper-mmu.config` loads as it is. See
  [saved firmware settings](../mcu/config.md).

## What changes

- **Where the code lives.** `<old klipper-mmu checkout>/…` becomes
  `<openams checkout>/klipper_mmu/…`. After the migration both Klipper
  symlinks, `src/klipper_mmu` and `klippy/extras/klipper_mmu`, resolve inside
  the openams checkout. Any script of yours that `cd`s into the old checkout
  must be repointed.
- **One installer.** openams's `install.sh` installs the host extra on every
  run, and the MCU add-on when you pass `--with-mcu-addon`. You no longer run
  `tools/install.py` yourself to keep klipper-mmu installed; the installer
  still exists, and openams calls it for every klipper-mmu rule.
- **One Moonraker update entry.** A single `[update_manager openams]` entry
  covers the whole repository, `klipper_mmu/` included, because both are in
  one Git checkout. Its `path` is your openams checkout and its `origin` is
  `https://github.com/OpenAMSOrg/openams.git`. An update pulls new add-on
  sources through the symlinks; rebuild the MMU firmware afterwards.
  If your `moonraker.conf` still carries a klipper-mmu update entry of its
  own, from the old repository's instructions, delete that section: the
  klipper-mmu directory is no longer a separate repository, and a second
  entry can only fail to find it. `install.sh` adds the `openams` entry if it
  is missing; it never adds one for klipper-mmu.
- **No separate OpenAMS fork.** The klipper-mmu host code is in
  `klipper_mmu/host/` and the OpenAMS plugin it binds to is in this same
  repository's `src/`, so there is no external OpenAMS revision to track or
  install. See [OpenAMS integration](../integration/openams/README.md).

## The steps

### 1. Remove the old links, with the old checkout's own tool

The symlinks point into the old checkout, so they have to be removed by the
tool in that checkout. Run all three from the old klipper-mmu directory:

```sh
cd <old klipper-mmu checkout>
python3 tools/install.py remove-host   <klipper path>
python3 tools/install.py remove-driver-api <klipper path>
python3 tools/install.py remove        <klipper path>
```

- `remove-host` deletes the `klippy/extras/klipper_mmu` symlink.
- `remove-driver-api` restores the shared Klipper driver files, but only if
  you installed the driver extensions with `install-driver-api`. If you never
  did, it reports `absent` and changes nothing, which is the expected result.
- `remove` deletes the two marked hook blocks in `src/Kconfig` and
  `src/Makefile` and the `src/klipper_mmu` symlink. It leaves the rest of
  your Klipper checkout, your `.config` and any build output alone.

Each action is a no-op when its work is already absent, so a repeated run is
harmless. `check` and `check-host` from the same tool tell you what is still
installed if you are unsure.

Do not skip this step. The installer deliberately refuses to replace a link
that belongs to another location, so with the old symlinks still in place
`install.sh` would report the host extra as a conflict and leave the old
checkout serving the printer.

### 2. Clone openams and run its install.sh

```sh
git clone https://github.com/OpenAMSOrg/openams.git <openams checkout>
cd <openams checkout>
./install.sh -k <klipper path> -c <moonraker config dir>
```

Add `--with-mcu-addon` to that same command if step 1's `remove` deleted the
add-on hooks, which is the case whenever you had installed the MCU add-on from
the old checkout:

```sh
./install.sh -k <klipper path> -c <moonraker config dir> --with-mcu-addon
```

`install.sh` links the OpenAMS modules and scripts, the Moonraker
`openams_spoolman` component, the klipper-mmu host extra, adds or migrates the
single `[update_manager openams]` entry and includes `oams.cfg` in
`printer.cfg`. `-k` defaults to `~/klipper` and `-c` to
`~/printer_data/config`; see the [root README](../../README.md#install)
for the full option list. If you never had OpenAMS installed, this one run
sets up both halves.

### 3. Rebuild the MMU firmware

Skip this step if you never installed the MCU add-on. If you did, the
`src/klipper_mmu` symlink now points somewhere else, so the old firmware image
was built against the old sources. Drop the previous MMU output and build
again, in the same Klipper checkout, with the same saved configuration:

```sh
cd <klipper path>
make KCONFIG_CONFIG="$PWD/klipper-mmu.config" OUT=out-mmu/ clean
make KCONFIG_CONFIG="$PWD/klipper-mmu.config" OUT=out-mmu/ menuconfig
make KCONFIG_CONFIG="$PWD/klipper-mmu.config" OUT=out-mmu/ olddefconfig
make KCONFIG_CONFIG="$PWD/klipper-mmu.config" OUT=out-mmu/ -j4
```

Pass `KCONFIG_CONFIG` on the make command line on every invocation; an
environment-only setting is not enough. `menuconfig` shows the same menus and
the same saved values as before, and `olddefconfig` accepts the saved
`klipper-mmu.config` unchanged. Do not use `distclean` for this: it deletes
`.config` regardless. Then flash the MMU firmware as usual.

### 4. Restart Klipper

`install.sh` stops Klipper at the start of the run and starts it again when
the run finishes, so a successful run leaves a restarted Klipper. If you ran
it in a way that skipped the service actions, restart it yourself:

```sh
sudo systemctl restart klipper
```

## Check that it worked

Run the check actions from the openams checkout, against the same Klipper
checkout:

```sh
cd <openams checkout>
python3 klipper_mmu/tools/install.py check      <klipper path>
python3 klipper_mmu/tools/install.py check-host <klipper path>
```

Both print `installed` and exit 0. Exit 1 means the work is absent, which
means step 1 or step 2 did not do what this page says; exit 2 means a
conflict, an incomplete installation or an error, and the message names the
file to look at.

Then:

- `readlink <klipper path>/src/klipper_mmu` prints your openams checkout's
  `klipper_mmu`, not the old one, and
  `readlink <klipper path>/klippy/extras/klipper_mmu` prints its `host`.
- `grep -c '\[update_manager openams\]' <moonraker config dir>/moonraker.conf`
  is 1, and no other update entry names a klipper-mmu repository.
- The Klipper log (`journalctl -u klipper -n 200`, or Moonraker's log panel)
  loads the extra without a config error, and a `KLIPPER_MMU_QUERY MMU=<name>`
  command answers. The extra is loaded only when a `[klipper_mmu ...]` section
  names it, so a printer with no MMU section shows nothing at all, which is
  correct.

## How to go back

Remove what the migration installed, then install from the old checkout again:

```sh
cd <openams checkout>
./install.sh -u -k <klipper path> -c <moonraker config dir>
```

That removes the OpenAMS links, the klipper-mmu host extra symlink and the
MCU add-on hooks with the `src/klipper_mmu` link, where they are present. It
does not remove the shared driver extensions: if you ever ran
`install-driver-api`, run `remove-driver-api` yourself first, from whichever
checkout holds the tool:

```sh
python3 klipper_mmu/tools/install.py remove-driver-api <klipper path>
```

Then, from the old klipper-mmu checkout, re-run its `install-host` and
`install` actions, rebuild the MMU firmware as in step 3, and restart Klipper.
Going back is a return to the previous arrangement, not a downgrade of any
file: the Klipper checkout and your config are unchanged by either direction.
