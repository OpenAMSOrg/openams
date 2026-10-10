# Klipper CAN patches: user guide

A plain-language guide to the optional CAN patch series that OpenAMS can apply to
Klipper. The [README](README.md) is the reference (every patch, what was tested,
how the series is regenerated). This guide covers what the series does for you
and how to use it.

You do not need this series to run OpenAMS. Nothing applies it unless you ask.

The same CAN work is also kept merged with upstream Klipper in the public fork
[OpenAMSOrg/klipper-optional-improvements](https://github.com/OpenAMSOrg/klipper-optional-improvements)
("CAN bus robustness and diagnostics, input hardening, and host performance"). This
series is the CAN-only subset; the fork is the place to look for anything wider.

## What it fixes

Every item below is something a patch in the series does, taken from the patch
messages. None of it was measured on your printer; see
[Check that it works](#check-that-it-works).

### Boards that drop off the bus

| What you might see | What the series changes |
|---|---|
| A CAN board stays dead after a wiring or termination fault, even when the cable is fixed | The STM32, SAME70, SAMC21/SAME5x (FDCAN) and LPC176x controllers now start the hardware's bus-off recovery on their own. Before, they sat in bus-off until a reset. The ISO recovery delay is kept. |
| A USB to CAN bridge stays blocked when the CAN bus is stalled | A USB to CAN bridge now enforces its 50 ms transmit stall timeout with a timer, also after a firmware shutdown. Before, the timeout was only checked when other traffic happened to wake the task, so a stuck controller could block host messages indefinitely. |
| An unassigned board hangs its own MCU when the bus is stuck | Discovery responses are retried from the normal transmit task, not in a tight loop that starved the watchdog. |
| A node stops accepting assignments after a long uptime | The admin queue test no longer misreads an empty queue as full when its 32-bit counter rolls over. |

### Boards that never show up (discovery and assignment)

| What you might see | What the series changes |
|---|---|
| `canbus_query.py` misses a board when two or more new boards are on the bus | Unassigned boards answer inside a window of about 1.5 seconds, each in its own slot spaced by the time of a CAN frame at your bitrate. The slot changes with every query, so two boards that collide once do not collide forever. |
| The same board gets different node ids, or a bad UUID fails late | UUIDs are normalized on config load, so `0A1B...` and `0a1b...` are one device. A bad UUID is a config error. |
| Many CAN buses, or many MCUs | Node ids are allocated per CAN interface, starting at 4. More than 255 on one interface is a clear config error instead of a crash inside python-can. |
| Klipper hangs while assigning a node id | The assignment frame has a one-second send timeout. A failed attempt closes its socket before the retry, so it no longer leaks one socket per attempt. |
| A discovery storm pushes real commands out | Repeated discovery queries are coalesced, so they cannot fill the small admin queue and drop a node assignment. |

### Garbage frames and malformed input

| What you might see | What the series changes |
|---|---|
| Odd bytes or a crash from a corrupted message | The host message parser and the C helper's buffer functions check lengths before reading or copying. |
| Stale or uninitialized bytes on the wire | Short SocketCAN reads are rejected; outbound frames are zeroed first; a short write stops the transport instead of silently corrupting the stream. |
| A USB to CAN bridge forwards junk, or Linux detaches the `can0` interface | The bridge rejects short or oversized host records, unsupported channels and flags, ambiguous or error identifiers, and the reserved receive echo id. It copies only valid payload bytes. It also validates gs_usb control requests, and stalls bad ones instead of wedging endpoint zero. |
| Frames you never use consume receive buffers | FDCAN hardware now drops unused extended and remote frames, so they never reach the CPU. USB bridges keep unfiltered forwarding. |
| Impossible lengths (DLC 9 to 15) | They are clamped to 8 when received. |
| A wrong bitrate silently programmed | If the clock cannot hit your `canbus_frequency` exactly, the MCU now shuts down with `Invalid CAN bitrate`. Before, it ran at a nearby rate and still reported the one you asked for. |

### A busy host or a loaded MCU

| What you might see | What the series changes |
|---|---|
| High CPU on the host with several CAN MCUs | SocketCAN frames are read and written in batches of up to 16 (`recvmmsg`, `sendmmsg`). Receive filters are exact, so the kernel does less work per node. |
| Request and response bursts once per second | Status queries are staggered by a per-MCU phase. |
| MCU interrupt load | FDCAN drains every pending frame per interrupt. bxCAN takes the frames that were pending on entry. FDCAN transmit interrupts fire only when a queue is full. RP2040 notifies only when awaited. On STM32H7, ATSAM and ATSAMD the FDCAN FIFOs hold 16 classic-size entries per direction instead of 3. |
| Slow command dispatch on the MCU | Up to eight complete command blocks are handled per pass. |

### Diagnostics

A new optional `canbus_stats` query reports lost frames and bus-off episodes. See
[below](#check-that-it-works).

The series also fixes undefined C behavior in the identifier flags and can2040 code.
It adds documentation too: the CAN bootloader request, the status fields and a
trust model. The trust model says CAN has no authentication: a device on your bus
can send commands. Treat the bus as part of the machine's trusted wiring.

## Requirements

| Need | Detail |
|---|---|
| Klipper | A git clone. The series is tested on upstream commit `7bc4d09465d31cd30fc0822e8d0abe02cc8c547f` (the `BASE` file). A newer Klipper is accepted only if it descends from that commit and none of the files the series touches changed. |
| Clean files | The files the patches change must have no local changes. Other files may. |
| Host | Python 3.7 or newer and `git`. A C compiler, which Klipper already needs, because Klippy rebuilds its C helper on start. |
| MCU firmware | Rebuild and reflash every CAN MCU to get the firmware-side fixes. |

Host only (take effect when Klipper restarts): the `canbus_ids` and `serialhdl`
checks, the `serialqueue` C helper changes (parser and FFI bounds, frame validation,
batching), staggered status queries, and the diagnostics display.

Firmware (need a reflash): `canserial` discovery and queue changes, `usb_canbus`
bridge validation and timers, FDCAN, bxCAN, LPC176x, RP2040 and can2040 changes,
bitrate validation, and the diagnostic counters themselves.

The documentation patches change docs only.

## Install

### Through OpenAMS

From the OpenAMS checkout:

```sh
bash install.sh --with-klipper-patches
```

Use `-k <path>` if Klipper is not in `~/klipper`. The installer stops Klipper,
applies the series, and starts Klipper again. The helper checks the series
checksums, the Klipper commit and the touched files first. If any check fails,
nothing is changed. It edits the working tree only. It makes no commit and does
not touch the index or any branch.

To accept only the tested commit, not a newer descendant:

```sh
OPENAMS_KLIPPER_PATCHES_EXACT_BASE=1 bash install.sh --with-klipper-patches
```

### By hand

```sh
python3 tools/klipper_patches.py check  ~/klipper   # would it apply? changes nothing
python3 tools/klipper_patches.py apply  ~/klipper
python3 tools/klipper_patches.py status ~/klipper   # exit 0 applied, 10 not applied, 1 problem
python3 tools/klipper_patches.py remove ~/klipper
```

`apply` and `check` also take `--exact-base`. Restart Klipper after `apply`. The
helper keeps a record and a copy of the patches inside the checkout's `.git`
directory, so `status` and `remove` stay exact even after you update OpenAMS.
Running `apply` twice is harmless.

### Moonraker's update manager

The patches are uncommitted changes, so the Klipper checkout is dirty and
Moonraker's update manager reports it so. Do not use its update or recovery
actions on a patched Klipper. A recovery that resets the tree throws the patches
away; run `bash install.sh --remove-klipper-patches` afterward to clear the stale
record. The OpenAMS update manager entry is not affected.

### Updating Klipper

1. `bash install.sh --remove-klipper-patches`
2. Update Klipper (Moonraker's update manager, or `git pull`).
3. `bash install.sh --with-klipper-patches`

If the new Klipper changed a patched file, step 3 refuses. Your printer then runs
plain upstream Klipper until OpenAMS ships a series rebased onto it.

## Rebuild and flash the MCU firmware

This part is generic. It does not replace your board's own flashing instructions.

```sh
cd ~/klipper
make menuconfig     # keep your existing settings; the CAN options are unchanged
make clean
make -j2
```

Then flash each CAN MCU the way you normally do (for example Katapult over CAN,
`make flash`, or a UF2 or DFU copy). Klipper's `docs/Bootloaders.md` and your
board's documentation cover the details. Notes:

- A board that was not rebuilt keeps running its old firmware. That works, and its
  diagnostics fields stay empty.
- The new MCU build refuses a bitrate it cannot hit exactly. Keep `canbus_frequency`
  and the MCU build option the same as before.
- If you use a USB to CAN bridge, flash it with the same care as the toolhead
  board. The `can0` link drops while it restarts.

## Check that it works

**Is the series applied?**

```sh
python3 tools/klipper_patches.py status ~/klipper
git -C ~/klipper status --short
```

`status` prints `applied: 56 patches ...` and exits 0. `git status` lists the
modified files.

**Do the MCUs have the new firmware?** Klipper makes one `canbus_stats` object
per CAN MCU (named like `canbus_stats <mcu name>`; the main MCU is just `mcu`).
With new firmware, `klippy.log` stats lines gain four fields:

```
canstat_<name>: bus_state=active rx_error=0 tx_error=0 tx_retries=0 rx_overflows=0 tx_discards=0 usb_invalid=0 bus_off=0
```

The same fields are in the printer status. They are `None` until the first
response, and stay `None` on old firmware. The diagnostics are queried about every
five seconds.

| Field | Meaning | On a healthy bus |
|---|---|---|
| `rx_overflows` | Receive queue overflows (hardware FIFO, command buffer, admin queue, USB bridge queue). A hardware event can cover several frames, so this is a lower bound. | 0 |
| `tx_discards` | Frames a USB to CAN bridge threw away after its transmit queue stalled. Bridges only. | 0 |
| `usb_invalid` | Malformed or unsupported USB records the bridge discarded. Bridges only. | 0 |
| `bus_off` | Times the controller entered bus-off. Always 0 on RP2040 and RP2350. | 0 |

What to look for:

- `rx_overflows` climbing while `rx_error` stays 0 points to an overloaded MCU or
  host, not wiring. The existing error counters cannot see this kind of loss.
- `bus_off` above 0 means the node fell off the bus and came back. Check
  termination, wiring and bitrate.
- `tx_discards` above 0 means the host sent frames the bus could not take for
  more than 50 ms.
- `usb_invalid` above 0 means the bridge received bad records from the host side and
  discarded them.
- Nonzero `rx_error` or `tx_error` with clean queue counters usually means a
  signal-integrity problem.

On a healthy bus every field stays at zero. A healthy bus is the dullest thing
in your printer, which is the goal.

## Troubleshooting

The first rows are the helper's own refusals; the helper prints `refused:` and
exits 1. The last block is general CAN advice, not specific to this series.

| Symptom | Likely cause | Fix |
|---|---|---|
| `checksum mismatch for <file> ... the series was modified or damaged` | A patch, `series`, or `BASE` file was edited or corrupted. | `git -C <openams checkout> checkout -- klipper_patches`, or fetch OpenAMS again. Never apply a series that fails its checksums. |
| `this Klipper checkout does not contain the tested base commit ...` | Shallow clone, or a fork that diverged. | Fetch upstream history, then `git checkout` the base. The message gives both commands. |
| `Klipper is at <sha>, which is older than or unrelated to the tested base` | Klipper is too old, or is a different line. | Update Klipper, or check out the base shown in the message. |
| `Klipper is newer than the tested base and these patched files changed upstream since it: ...` | Upstream changed a file the series touches. | Check out the base, or wait for a rebased series. |
| `newer than the tested base ... --exact-base ... accepts only the base` | You asked for exact-base. | Check out the base, or drop the option. |
| `these files the patches change have local changes: ...` | You edited a touched file, or it is untracked. | Commit, stash (`git -C ~/klipper stash`) or discard the changes, then retry. |
| `"<path>" is not a Git checkout` | The path is a copy of Klipper, not a clone. | Point at your git clone. |
| `a different OpenAMS Klipper patch series is already applied` | An earlier series is on the tree. | Run `remove` first, then `apply`. |
| `the OpenAMS Klipper patches are recorded as applied, but these files changed since: ...` | A patched file was edited after the apply. | Back up your edits, restore with the `git checkout --` command in the message, then run `remove`. |
| `--with-klipper-patches and --remove-klipper-patches cannot be used together` (or `-u`) | Conflicting installer flags. | Use one. `-u` already removes the patches. |
| `MCU '<name>' shutdown: Invalid CAN bitrate` after reflashing | The MCU clock cannot make your bitrate exactly. | Choose a bitrate the board supports. The firmware no longer rounds. |
| General: `Timeout on connect` or MCU never found | Bitrate differs between host and MCUs, no termination, wrong UUID or interface. | Check `canbus_query.py can0`, bitrate on both sides, and that the bus has 120 ohm termination at each end (about 60 ohm across CAN_H and CAN_L, power off). |
| General: lost frames or retransmits under load | Host `can0` transmit queue too short. | Set `txqueuelen` to 128 or more on `can0` (`sudo ip link set can0 txqueuelen 128`, or in your interface config). |
| General: many `rx_error` or `tx_error` counts | Bad wiring, a stub that is too long, or a missing common ground. | Fix the wiring, then watch the counts. |

CAN troubleshooting has a way of ending at the same two places: the cable and the
terminator.

## Remove it

```sh
bash install.sh --remove-klipper-patches
```

This reverses exactly what was applied, restarts Klipper and touches nothing
else. The full uninstall (`bash install.sh -u`) also removes the series if it is
applied. After removal, rebuild and reflash each MCU whose firmware was built with
the series if you want it back on stock firmware. Removal refuses if a patched file
was edited after the apply; the message says how to recover.

## FAQ

**Does OpenAMS need this?** No. The series is independent of OpenAMS features.

**Does it change the protocol?** The patch messages say identifiers, frame layouts
and the Klipper message format are unchanged. Discovery replies are timed
differently. The diagnostics query is built for mixed old and new firmware.

**Can I flash only some boards?** Yes. Host fixes work with any firmware. Each
board gets the firmware fixes only after its own reflash.

**Is it GPL?** Yes. It is GPL-3.0-or-later like Klipper; see
[`../NOTICE.md`](../NOTICE.md) and [`../COPYING`](../COPYING).

**Why is the tested commit so exact?** The patches were built, and their
firmware compiled for nine boards, on that commit. The README lists what was
tested, and also what was not: no CAN hardware was in the loop.

**Does it help my CAN bus's wiring?** No. It makes the software tolerate faults and
report them. It cannot add a terminator.
