# Pinned Klipper source fixtures

Unmodified source files from Klipper revision
`ce7002bedf37e938bb483572949f3703ac6476cb`, retained only for repeatable installer
preservation tests. They are not compiled as a second driver or shipped into a
printer. Original attribution is retained in tmcuart.c. Klipper is GPLv3;
see https://github.com/Klipper3d/klipper/blob/ce7002bedf37e938bb483572949f3703ac6476cb/COPYING.
The reviewed before hashes are in `upstream/source-hashes.json`, except
`src/Kconfig`. It is no longer a driver target; installer tests use it for the
add-on hook and the previous bundle (`PREVIOUS_KCONFIG_HASHES` in
`tools/install.py`).
