# Klipper CAN patch series

Looking for a plain-language guide? See [GUIDE.md](GUIDE.md).

An opt-in, pinned set of 56 patches to Klipper's CAN bus support: the host code
(`serialhdl`, `serialqueue`, `canbus_ids`, `canbus_stats`), the MCU firmware
(`canserial`, `usb_canbus` and the bxCAN, FDCAN, lpc176x, rp2040 and can2040
drivers) and the CAN documentation. They are the owner's CAN work from
`klipper-voron350`, and only that: the same clone's webhooks, gcode, toolhead,
trapq and ADXL345 work is not in the series.

Nothing here is needed for OpenAMS to work. `install.sh` never applies the series
unless you ask for it with `--with-klipper-patches`.

License: GPL-3.0-or-later, like Klipper. See [`../COPYING`](../COPYING) and
[`../NOTICE.md`](../NOTICE.md).

## Tested base

| | |
|---|---|
| Upstream | Klipper3d/klipper |
| `BASE` | `7bc4d09465d31cd30fc0822e8d0abe02cc8c547f` (upstream `master` on 2026-09-30, "queuelogger: workaround infinite rollover printing") |
| Series | `series` lists the 56 patch files in apply order |
| Author | every patch keeps JR Lomas as author, with its own date and message |

The series applies to exactly that commit, and was built and tested there. It was
first made on `ac2a7f8b0e1b` and then moved onto `BASE`: all 56 commits applied to
the newer base without a conflict. Two upstream commits since `ac2a7f8b0e1b` touch
files the series also touches (`docs/Status_Reference.md`, `klippy/mcu.py`); they
merged cleanly. The owner's own merge of upstream conflicted only in `msgproto.py`
and `src/Kconfig`, both in work that is not part of this series.

## What it fixes and adds

**Host (Klippy):**

- `canbus_ids`: node ids are allocated per CAN interface, UUIDs are canonicalized,
  and the protocol's node-id limit is enforced.
- `serialhdl`: node ids are validated before they are encoded, SocketCAN receive
  filters are exact, assignment sends are bounded, sockets are closed after an
  assignment error, and orphaned send notifications are contained.
- `serialqueue` (the C helper): CAN frames are validated and fully initialized,
  short writes are an error, and SocketCAN frames are received and sent in batches
  (`recvmmsg`/`sendmmsg`).
- `canbus_stats`: periodic status queries are staggered, and the new queue-loss
  diagnostics are reported (documented in `docs/Status_Reference.md`).

**MCU firmware (needs a rebuild and reflash of each CAN MCU):**

- `canserial`: discovery responses are staggered, spaced by CAN frame time, varied
  between queries, retried asynchronously and coalesced; the admin queue counter
  survives rollover; buffered command blocks are dispatched in batches.
- `usb_canbus` (USB to CAN bridge): frame boundaries, payloads, gs_usb control
  setup packets, channels, flags and ambiguous identifiers are validated, the
  receive echo id is reserved, and the transmit stall deadline is scheduled.
- FDCAN (stm32, atsam, atsamd): unused frame types are rejected in hardware,
  pending receive frames are drained, bus-off recovers, transmit interrupts arm on
  demand, FIFOs are classic-sized, unrepresentable bitrates are rejected, and
  only entering bus-off is counted.
- bxCAN (stm32): pending receive frames are batched.
- lpc176x: the controller recovers from bus-off.
- rp2040 and can2040: transmit capacity is signaled only when awaited, CAN
  encoding stays outside the IRQ lock, and identifier flags and masks are
  unsigned.
- The CAN MCU drivers: bitrate validation and queue-loss diagnostics.

**Docs:** the CAN bootloader request, the CAN protocol trust model and the queue
loss status fields.

## The patches

`0001` to `0056` in `series`, in apply order. The column "Source" is the commit in
the owner's `klipper-voron350` clone the patch came from.

| # | Patch file | Source | Purpose |
|---|---|---|---|
| 0001 | `0001-klippy-requirements-Update-python-can-for-Python-3.9.patch` | `d7ed4e09` | klippy-requirements: Update python-can for Python 3.9+ (keeps the python-can requirement current for the host CAN code) |
| 0002 | `0002-chelper-Validate-parser-and-FFI-buffer-bounds.patch` | `c5eccad3` | chelper: Validate parser and FFI buffer bounds (prerequisite, not CAN work (see "Non-CAN code the series pulls in")) |
| 0003 | `0003-serialqueue-Validate-and-initialize-CAN-frames.patch` | `404b6e00` | serialqueue: Validate and initialize CAN frames |
| 0004 | `0004-msgblock-Clarify-wire-message-encoding.patch` | `60161414` | msgblock: Clarify wire message encoding (prerequisite, not CAN work (see "Non-CAN code the series pulls in")) |
| 0005 | `0005-serialqueue-Process-input-frames-without-repeated-co.patch` | `edeac5e1` | serialqueue: Process input frames without repeated copies (prerequisite, not CAN work (see "Non-CAN code the series pulls in")) |
| 0006 | `0006-serialqueue-Simplify-output-frame-construction.patch` | `4d3bd07d` | serialqueue: Simplify output frame construction (prerequisite, not CAN work (see "Non-CAN code the series pulls in")) |
| 0007 | `0007-canserial-Retry-discovery-responses-asynchronously.patch` | `a97bbf89` | canserial: Retry discovery responses asynchronously |
| 0008 | `0008-usb_canbus-Copy-only-valid-CAN-payload-bytes.patch` | `a30df221` | usb_canbus: Copy only valid CAN payload bytes |
| 0009 | `0009-usb_canbus-Validate-host-frame-boundaries.patch` | `94274cef` | usb_canbus: Validate host frame boundaries |
| 0010 | `0010-canbus-Reject-unused-FDCAN-frame-types-in-hardware.patch` | `58ad3b2c` | canbus: Reject unused FDCAN frame types in hardware |
| 0011 | `0011-canbus-Drain-pending-FDCAN-receive-frames.patch` | `ec48f426` | canbus: Drain pending FDCAN receive frames |
| 0012 | `0012-canserial-Stagger-unassigned-node-responses.patch` | `54abccbd` | canserial: Stagger unassigned node responses |
| 0013 | `0013-canbus_ids-Canonicalize-configured-UUIDs.patch` | `1ddf17e9` | canbus_ids: Canonicalize configured UUIDs |
| 0014 | `0014-canbus_ids-Allocate-node-ids-per-interface.patch` | `8f7cf3ae` | canbus_ids: Allocate node ids per interface |
| 0015 | `0015-canbus_ids-Enforce-the-protocol-node-id-limit.patch` | `aaf8ca52` | canbus_ids: Enforce the protocol node-id limit |
| 0016 | `0016-serialhdl-Validate-CAN-node-ids-before-encoding.patch` | `74f95b62` | serialhdl: Validate CAN node ids before encoding |
| 0017 | `0017-serialqueue-Require-complete-CAN-frame-writes.patch` | `3a085746` | serialqueue: Require complete CAN frame writes |
| 0018 | `0018-serialhdl-Close-CAN-sockets-after-assignment-errors.patch` | `b6841f0a` | serialhdl: Close CAN sockets after assignment errors |
| 0019 | `0019-usb_canbus-Validate-gs_usb-control-setup-packets.patch` | `d6811737` | usb_canbus: Validate gs_usb control setup packets |
| 0020 | `0020-canbus-Use-unsigned-CAN-identifier-flag-constants.patch` | `f00509f1` | canbus: Use unsigned CAN identifier flag constants |
| 0021 | `0021-usb_canbus-Reject-unsupported-host-channels.patch` | `f07a3a30` | usb_canbus: Reject unsupported host channels |
| 0022 | `0022-usb_canbus-Reject-unsupported-host-frame-flags.patch` | `8a7dfe70` | usb_canbus: Reject unsupported host frame flags |
| 0023 | `0023-usb_canbus-Reject-ambiguous-CAN-identifiers.patch` | `4037e80e` | usb_canbus: Reject ambiguous CAN identifiers |
| 0024 | `0024-canserial-Space-discovery-slots-by-CAN-frame-time.patch` | `4377baec` | canserial: Space discovery slots by CAN frame time |
| 0025 | `0025-canserial-Vary-discovery-slots-between-queries.patch` | `b2d62023` | canserial: Vary discovery slots between queries |
| 0026 | `0026-canserial-Handle-admin-queue-counter-rollover.patch` | `00063ac2` | canserial: Handle admin queue counter rollover |
| 0027 | `0027-can2040-Use-unsigned-CAN-identifier-flags.patch` | `488572e7` | can2040: Use unsigned CAN identifier flags |
| 0028 | `0028-can2040-Build-bitstream-masks-from-unsigned-values.patch` | `84ae0a02` | can2040: Build bitstream masks from unsigned values |
| 0029 | `0029-can2040-Use-volatile-reads-for-statistics-snapshots.patch` | `8c04b55b` | can2040: Use volatile reads for statistics snapshots |
| 0030 | `0030-docs-Document-the-CAN-bootloader-request.patch` | `f62682b7` | docs: Document the CAN bootloader request |
| 0031 | `0031-docs-Describe-the-CAN-protocol-trust-model.patch` | `882f3175` | docs: Describe the CAN protocol trust model |
| 0032 | `0032-canbus-Recover-FDCAN-controllers-from-bus-off.patch` | `aeb59fb6` | canbus: Recover FDCAN controllers from bus-off |
| 0033 | `0033-lpc176x-Recover-the-CAN-controller-from-bus-off.patch` | `eb9b4c45` | lpc176x: Recover the CAN controller from bus-off |
| 0034 | `0034-usb_canbus-Reserve-the-device-receive-echo-id.patch` | `9ef69574` | usb_canbus: Reserve the device receive echo id |
| 0035 | `0035-serialhdl-Use-exact-SocketCAN-receive-filters.patch` | `fcebb545` | serialhdl: Use exact SocketCAN receive filters |
| 0036 | `0036-serialhdl-Bound-CAN-assignment-sends.patch` | `40ab8ac1` | serialhdl: Bound CAN assignment sends |
| 0037 | `0037-serialhdl-Contain-orphaned-send-notifications.patch` | `3a28162a` | serialhdl: Contain orphaned send notifications |
| 0038 | `0038-canbus_stats-Stagger-periodic-status-queries.patch` | `b9019544` | canbus_stats: Stagger periodic status queries |
| 0039 | `0039-serialqueue-Batch-SocketCAN-receive-records.patch` | `c234ac7d` | serialqueue: Batch SocketCAN receive records |
| 0040 | `0040-usb_canbus-Schedule-the-transmit-stall-deadline.patch` | `5704975f` | usb_canbus: Schedule the transmit stall deadline |
| 0041 | `0041-canserial-Coalesce-queued-discovery-queries.patch` | `dbcb982c` | canserial: Coalesce queued discovery queries |
| 0042 | `0042-canserial-Dispatch-buffered-command-blocks-in-batche.patch` | `5bf93bfc` | canserial: Dispatch buffered command blocks in batches |
| 0043 | `0043-canbus-Publish-interrupt-queue-ownership-explicitly.patch` | `17e7d079` | canbus: Publish interrupt queue ownership explicitly |
| 0044 | `0044-canbus-Normalize-received-classic-CAN-lengths.patch` | `3f2c0c17` | canbus: Normalize received classic CAN lengths |
| 0045 | `0045-canbus-Arm-FDCAN-transmit-interrupts-on-demand.patch` | `f532603e` | canbus: Arm FDCAN transmit interrupts on demand |
| 0046 | `0046-rp2040-Notify-transmit-capacity-only-when-awaited.patch` | `e04e6119` | rp2040: Notify transmit capacity only when awaited |
| 0047 | `0047-canbus-Use-classic-sized-programmable-FDCAN-FIFOs.patch` | `f625e3b2` | canbus: Use classic-sized programmable FDCAN FIFOs |
| 0048 | `0048-canbus-Reject-unrepresentable-bitrates.patch` | `770b5cbb` | canbus: Reject unrepresentable bitrates |
| 0049 | `0049-canbus-Report-queue-loss-diagnostics.patch` | `71cea387` | canbus: Report queue loss diagnostics |
| 0050 | `0050-stm32-Batch-pending-bxCAN-receive-frames.patch` | `02df2f62` | stm32: Batch pending bxCAN receive frames |
| 0051 | `0051-serialqueue-Batch-SocketCAN-frame-writes.patch` | `eaf1166d` | serialqueue: Batch SocketCAN frame writes |
| 0052 | `0052-usb_canbus-Restart-interrupted-stall-deadlines.patch` | `4672edc2` | usb_canbus: Restart interrupted stall deadlines |
| 0053 | `0053-fdcan-Count-only-bus-off-entry-transitions.patch` | `a4945dd5` | fdcan: Count only bus-off entry transitions |
| 0054 | `0054-rp2040-Keep-CAN-encoding-outside-the-IRQ-lock.patch` | `94752f6d` | rp2040: Keep CAN encoding outside the IRQ lock |
| 0055 | `0055-docs-Describe-CAN-queue-loss-status-fields.patch` | `c0c7ef2a` | docs: Describe CAN queue loss status fields |
| 0056 | `0056-msgblock-Declare-resync-state-before-label.patch` | `5795163c` | msgblock: Declare resync state before label (build fix for the msgblock.c changes above) |

### Non-CAN code the series pulls in

Four patches are not CAN work. They are here because the batched SocketCAN I/O
patches are built on them, and each dependency was tried and confirmed:

- `0039` (batch SocketCAN receive records) does not apply without `0005` (input
  frame processing without repeated copies).
- `0051` (batch SocketCAN frame writes) does not apply without `0006` (output
  frame construction through `msgblock_finalize()`).
- `0006` does not build without `0004`, which introduces `msgblock_finalize()`
  and const-correct message helpers.
- `0004` does not apply without `0002` (bounds checks in the message parser and
  the FFI buffer helpers).

Together they change `klippy/chelper/msgblock.c`, `msgblock.h`, `pyhelper.c`,
`serialqueue.c`, `serialqueue.h` and `trdispatch.c`. Their commit messages state
that the wire format and the emitted bytes do not change. `0056` is a one-line
compile fix to `msgblock.c`. Leaving these out would mean leaving out the two
batching patches, `0039` and `0051`, as well.

`0001` is not code: it changes the `python-can` pin in
`scripts/klippy-requirements.txt` so the host's CAN dependency installs on current
Python. Applying it does not change an existing virtual environment; it only
matters the next time Klipper's requirements are installed.

## Excluded

Everything else in the owner's series: 30 commits, of the 86 non-merge commits
there (56 are included). The three merge commits carry no change of their own and
are not selectable.

| Commit | Subject | Why it is not in the series |
|---|---|---|
| `fd24fc14` | display: Order SD Card menu files by modification date | display menu ordering, not CAN |
| `8a93eb73` | verify_heater: Add a way to disable heater verification | heater verification, not CAN |
| `c00e8b29` | sensor_adxl345: support trigger_analog for homing/probing | ADXL345 touch probe, not CAN |
| `96104412` | sensor_adxl345: support two trigger_analog slots | ADXL345 touch probe, not CAN |
| `d37cacc0` | sensor_adxl345: add WANT_ADXL345_PROBE menuconfig option | ADXL345 touch probe, not CAN |
| `b4b4119a` | sensor_adxl345: add carrier-loss (tone) contact detector | ADXL345 touch probe, not CAN |
| `bcb1359c` | chelper: Avoid shell command construction | chelper build hardening, not CAN |
| `516feb73` | serialqueue: Always join background thread | serialqueue shutdown hardening, not CAN; the CAN series builds and applies without it |
| `7c059199` | gcode_macro: Evaluate templates in a sandbox | gcode_macro sandbox, not CAN |
| `6211543e` | gcode_macro: Bound template resource usage | gcode_macro sandbox, not CAN |
| `0a41475e` | klippy: Reject non-finite control values | general input validation, not CAN |
| `f754130f` | gcode: Bound command input length | gcode input bound, not CAN |
| `e5cc3298` | virtual_sdcard: Confine files to configured path | virtual_sdcard path confinement, not CAN |
| `e964664d` | msgproto: Validate and bound MCU protocol data | msgproto/identify validation, not CAN (it conflicts with upstream work in msgproto.py) |
| `f4431b30` | util: Harden diagnostic and IPC file handling | util/IPC file hardening, not CAN |
| `5b9986a5` | configfile: Write persistent state atomically | configfile atomic write, not CAN |
| `6b01ca07` | webhooks: Limit concurrent client connections | webhooks hardening, not CAN |
| `92a4541e` | webhooks: Bound request and log data | webhooks hardening, not CAN |
| `2bab045e` | webhooks: Bound queued response data | webhooks hardening, not CAN |
| `13c608aa` | webhooks: Bound remote method registrations | webhooks hardening, not CAN |
| `86a269c8` | msgblock: Avoid clearing unused queue storage | msgblock micro-optimization, not CAN and not needed by the series |
| `45ff3df0` | chelper: Parse CFFI declarations in one pass | chelper CFFI parsing speed-up, not CAN |
| `83546faa` | toolhead: Reduce finite move validation overhead | toolhead/gcode performance, not CAN |
| `31e8bd14` | gcode: Avoid temporary command parser slices | gcode performance, not CAN |
| `ba6b11d0` | toolhead: Avoid temporary move distance lists | toolhead performance, not CAN |
| `acfe6b1e` | corexy: Avoid tuple allocation in move checks | corexy performance, not CAN |
| `c500e0c5` | toolhead: Hand off complete lookahead queues | toolhead performance, not CAN |
| `e9198dd4` | toolhead: Fold junction limits without lists | toolhead performance, not CAN |
| `d2f07f66` | trapq: Batch host move appends | trapq performance, not CAN |
| `3b3c704c` | toolhead: Batch extra-axis trapq appends | toolhead/trapq performance, not CAN |

## Proof

Run on 2026-09-30 with git 2.43.0, Python 3.12.3, arm-none-eabi-gcc 13.2.1,
avr-gcc 7.3.0 and gcc 13.3.0. No printer was contacted.

**Applies to upstream at `BASE`.** On a fresh checkout of upstream commit
`7bc4d09465d31cd30fc0822e8d0abe02cc8c547f` (detached HEAD, clean tree):

- For each patch in `series` order, `git apply --check <patch>` and then
  `git apply <patch>`: 56 of 56 succeeded. (`git apply --check` of several patches
  that change the same file in one command does not chain, which is why the
  installer helper replays the patches one at a time on a scratch copy.)
- `git apply <all 56 patches>` in one command succeeded, and the result is identical
  to the cherry-picked series commits (`git diff` empty).
- `tools/klipper_patches.py check`, `apply`, `status` and `remove` on the same
  checkout: apply made the 27-file change above, `remove` restored a clean tree,
  and a second `apply` or `remove` was harmless.
- `python3 -m compileall -q klippy`: exit 0.

**Firmware builds.** With the series applied, Klipper's own Kconfig and Makefile,
`make OUT=<dir>/ KCONFIG_CONFIG=<config> olddefconfig` then `make -j2`. The first
config is Klipper's own `test/configs` file, unmodified (it is the only CAN one
upstream has); the others are a `test/configs` base plus the CAN option lines
shown:

| Config | Built from | CAN path exercised | Result |
|---|---|---|---|
| `test/configs/n32g45x-canbus.config` | unmodified | stm32 bxCAN (`src/stm32/can.c`), PA11/PA12 | pass |
| `test/configs/rp2040.config` | + `CONFIG_RPXXXX_CANBUS=y` | rp2040 can2040 (GPIO 4/5) | pass |
| `test/configs/stm32f405.config` | + `CONFIG_LOW_LEVEL_OPTIONS=y`, `CONFIG_STM32_MMENU_CANBUS_PB8_PB9=y` | stm32 bxCAN | pass |
| `test/configs/stm32g0b1.config` | + `CONFIG_STM32_MMENU_CANBUS_PB0_PB1=y` | stm32 FDCAN | pass |
| `test/configs/stm32h723.config` | + `CONFIG_LOW_LEVEL_OPTIONS=y`, `CONFIG_STM32_MMENU_CANBUS_PB5_PB6=y` | stm32 FDCAN (H7) | pass |
| `test/configs/stm32g0b1.config` | + `CONFIG_STM32_USBCANBUS_PA11_PA12=y`, `CONFIG_STM32_CMENU_CANBUS_PB0_PB1=y` | `usb_canbus` USB to CAN bridge | pass |
| `test/configs/lpc176x.config` | + `CONFIG_LPC_MMENU_CANBUS_P0_0_P0_1=y` | lpc176x CAN | pass |
| `test/configs/same70q20b.config` | + `CONFIG_ATSAM_MMENU_CANBUS_PC12_PD12=y` | atsam MCAN (`src/atsam/fdcan.c`) | pass |
| SAME54P20 (`test/configs/samd51p20.config` has no CAN, so `CONFIG_MACH_SAME54P20=y` replaces its MCU line) | + `CONFIG_ATSAMD_MMENU_CANBUS_PB13_PB12=y` | atsamd CAN (`src/atsamd/fdcan.c`) | pass |

Each resulting `.config` was checked to have `CONFIG_CANBUS=y` (or
`CONFIG_USBCANBUS=y`) with the intended pins. All nine linked a `klipper.elf`. The
only compiler warning was `-Warray-bounds` in `src/generic/armcm_reset.c`, a file
the series does not touch.

**Klipper's own test runner.** `scripts/test_klippy.py` with a virtual environment
of `scripts/klippy-requirements.txt`, numpy and scipy, and MCU dictionaries built
with the patched tree from 25 of Klipper's `test/configs` (every test config whose
toolchain is installed here: arm-none-eabi, avr and the host compiler): 36 of 36
test cases passed. `test/klippy/printers.test` was not run: it needs the `pru` and
`hc32f460` dictionaries, whose toolchains are not installed. The run also compiles
the patched `klippy/chelper` (including `serialqueue.c`) with `cffi`. The suite
has no CAN test of its own, so this shows the host code still loads and runs
with the series, not that CAN behaves correctly; that needs hardware (below).

## Apply and remove

Through the installer, from the OpenAMS checkout:

```sh
bash install.sh --with-klipper-patches      # apply to ~/klipper (or -k <path>)
bash install.sh --remove-klipper-patches    # reverse it; nothing else is touched
bash install.sh -u                          # the full uninstall also removes it if applied
```

`--with-klipper-patches` is part of a normal install run, like `--with-mcu-addon`:
it stops Klipper, applies the series, and starts Klipper again. With
`--remove-klipper-patches` the installer only removes the patches (and restarts
Klipper); it installs and removes nothing else. The two flags cannot be combined
with each other or, for `--with-klipper-patches`, with `-u`.

The helper behind it can also be run directly:

```sh
python3 tools/klipper_patches.py check  ~/klipper    # would it apply? changes nothing
python3 tools/klipper_patches.py apply  ~/klipper
python3 tools/klipper_patches.py status ~/klipper    # exit 0 applied, 10 not applied, 1 problem
python3 tools/klipper_patches.py remove ~/klipper
```

What the helper does:

1. Verifies `SHA256SUMS` for `BASE`, `series` and every patch, and refuses on any
   mismatch or any patch that is not listed.
2. Requires a Git checkout of Klipper whose `HEAD` is `BASE` or a newer commit
   that descends from it and left every file the patches touch unchanged (see
   below). A Klipper that is older, unrelated, shallow enough to lack `BASE`, or
   newer with a patched file changed is refused, with what to do about it.
3. Refuses if any file the patches change has local changes.
4. Replays the patches on a scratch copy of those files (`git apply --check`, then
   apply, one patch at a time). Only if all of them work does it touch your tree.
5. Applies the series to the working tree with `git apply`, in one all-or-nothing
   command. It makes no commit and does not touch the index or any branch.
6. Records what it applied inside the checkout's git directory
   (`.git/openams-klipper-patches/`: a state file with the file hashes, and a copy
   of the patches). Applying again says "already applied" and changes nothing.
   `status` and `remove` use that record, so they stay exact even after OpenAMS is
   updated to a different series. `remove` refuses, and says why, if a patched file
   was edited after the apply; if the files were reset to Klipper's own content it
   just clears the record.

A newer Klipper is accepted when it descends from `BASE` and none of the files the
patches touch changed since `BASE`: the patches then apply to exactly the files they
were made for, and the rest of Klipper moves on as upstream intends. The series was
tested on `BASE` itself; to accept only that, use
`python3 tools/klipper_patches.py apply --exact-base ~/klipper` (with the installer,
`OPENAMS_KLIPPER_PATCHES_EXACT_BASE=1 bash install.sh --with-klipper-patches`).

After applying: restart Klipper (the installer does), and **rebuild and reflash each
MCU** whose firmware should have the fixes. The `src/` changes reach an MCU only
through a new firmware. Klippy rebuilds its C helper (`c_helper.so`) itself on the
next start; the host needs a C compiler, as Klipper already does.

## Effect on Moonraker's Klipper updates

The patches are changes to files Git tracks, and they are not committed, so the
Klipper checkout is **dirty**. Moonraker's update manager reports a dirty
repository, and will not update it cleanly: do not use its update or recovery
actions on a patched Klipper. A recovery that resets the working tree throws the
patches away (run `bash install.sh --remove-klipper-patches` afterwards, which clears
the stale record).

To update Klipper while using the series:

1. `bash install.sh --remove-klipper-patches`
2. Update Klipper (Moonraker's update manager, or `git pull`).
3. `bash install.sh --with-klipper-patches`

Step 3 works when the new Klipper is still at `BASE` or a descendant that left the
patched files alone. Otherwise it refuses and says so, and the printer simply runs upstream
Klipper until OpenAMS ships a series rebased onto the new base. The OpenAMS update
manager entry (`[update_manager openams]`) is unaffected: it tracks the OpenAMS
repository, not Klipper.

## What to check on a real printer

The automated checks above cannot exercise CAN hardware. On a printer, after
applying the series and reflashing:

- Every CAN MCU (toolhead board, and any USB to CAN bridge) connects at startup
  and after a `FIRMWARE_RESTART`.
- Adding a new, unassigned CAN node: `canbus_query.py` lists it, and assigning
  it works.
- Two or more unassigned nodes on one bus are all discovered (this exercises the
  staggered and spaced discovery responses).
- The `canbus_stats` fields, including the new queue-loss ones, appear in
  Klipper's status and stay at zero on a healthy bus.
- Unplugging the CAN cable for a few seconds and plugging it back in recovers the
  node (bus-off recovery): Klipper either reconnects or reports the MCU lost and
  `FIRMWARE_RESTART` brings it back.
- A long print with full toolhead activity has no "Timer too close" or CAN
  retransmit bursts in `klippy.log`.
- On a USB to CAN bridge, the host interface (`can0`) comes up and passes traffic
  at the configured bitrate.
- Moonraker shows the Klipper repository as dirty while the series is applied,
  and clean again after `--remove-klipper-patches`.

## Regenerating the series

For the maintainer, to move the series to a new upstream base `NEW`:

```sh
git clone https://github.com/Klipper3d/klipper.git work && cd work
git checkout -b can-series NEW
git cherry-pick <the source commits, in the order of the table above>
git format-patch -N --no-signature --zero-commit --no-stat -o /path/to/openams/klipper_patches/can NEW..can-series
cd /path/to/openams/klipper_patches/can
ls 0*.patch > series
echo NEW > BASE                          # the full 40-character sha
sha256sum BASE series 0*.patch > SHA256SUMS
```

Then repeat the proof above, update this file, and run the repository tests
(`python3 -m unittest discover -s test`).
