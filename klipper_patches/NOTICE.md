# Source and licensing boundary

Everything under `klipper_patches/` is licensed GPL-3.0-or-later; see
`COPYING`. The patches are changes to Klipper (Klipper3d/klipper, GPL-3.0), so
they are derivative works of Klipper and carry Klipper's license, not the MIT
license of the rest of this repository. The same rule applies to
`klipper_mmu/`.

The patch files are `git format-patch` output and keep their original author,
date and message. They need no SPDX header: the license is the one of the file
each patch changes.

The installer helper that applies the series, `tools/klipper_patches.py`, is a
separate program that does not include Klipper code. It lives outside this
directory and is MIT licensed like the rest of the host plugin.

A Klipper checkout that has the series applied is a GPL-3.0 work made of
Klipper plus these patches. Nothing here changes the MIT license of the
OpenAMS host plugin (`src/`, `moonraker/`).
