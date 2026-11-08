# Set up a Cortex Bridge printer

## What this page does

It takes you from a Cortex Bridge board in its bag to a Klipper printer that
loads filament from a stock Bambu Lab AMS. Follow the sections in order. Each
one ends with a check, so you know it worked before you move on.

Nothing on the AMS is opened or flashed. It keeps its Bambu Lab firmware.

| Step | What you do | Time |
|---|---|---|
| [1. Gather the parts](#1-gather-the-parts) | Check you have everything | 5 min |
| [2. Prepare the printer](#2-prepare-the-printer) | Back up, update, fit the buffer spring, lay the tube | 20 min |
| [3. Wire the bridge](#3-wire-the-bridge) | Case, USB, 24 V, buffer and AMS cables | 20 min |
| [4. Power on](#4-power-on) | Read the three LEDs | 1 min |
| [5. Install OpenAMS](#5-install-openams) | One command; it also writes your AMS config | 5 min |
| [6. Check the bridge](#6-check-the-bridge) | See your AMS on the Klipper computer | 1 min |
| [7. Finish the config](#7-finish-the-config) | Path length, cutter, toolhead macros | 15 min |
| [8. Add the Mainsail panel](#8-add-the-mainsail-panel) | See and drive the AMS from Mainsail | 2 min |
| [9. First load](#9-first-load) | Load, unload and swap once, watching | 15 min |

Later: [keep it up to date](#keep-it-up-to-date) and, when something is
wrong, [troubleshooting](#troubleshooting).

## 1. Gather the parts

- A **Cortex Bridge** board, Rev A4. The revision is printed on the underside.
- A **USB data cable** from the bridge's USB-C port to your Klipper computer.
  A charge-only cable powers the bridge but it never appears in `lsusb`.
- **24 V from the printer's power supply:** two wires from spare **+24 V** and
  **0 V** terminals on the printer's 24 V supply to the bridge's screw
  terminal, rated for at least 5 A (0.75 mm², 18 AWG, or thicker). The supply must have spare capacity for the AMS units, which draw
  the most while drying. A separate 24 V supply also works.
- The **stronger buffer spring** that comes with the Cortex Bridge. It replaces
  the spring in the Bambu Lab buffer (step 2).
- The **case**: printed base and lid, three **M2 × 10** screws, and
  **double-sided foam tape** to stick it to the printer's rear panel. A DIN
  rail clip is optional. [The case](cortex-bridge-hardware.md#the-case) lists
  the parts.
- An original Bambu Lab **AMS filament buffer** with its **4-pin buffer
  cable**, which runs from the bridge's BUFFER connector to the buffer. Both
  come with the AMS1; for an AMS 2 Pro, Bambu Lab sells the filament buffer
  with its cable as an addition.
- Optional: a **BIQU AMS Lite cable extension, 4-pin, 1 m**
  ([BIQU](https://biqu.equipment/products/biqu-ams-upgrades-multi-material?variant=41897374351458),
  [Amazon](https://www.amazon.com/clp/B0F93RMF2X)), if
  Bambu Lab's short 4-pin cable does not reach. It plugs in between that cable
  and the bridge, so the bridge can sit further from the buffer, for example
  on its DIN clip under the printer, with a single 4-pin run. A 0.5 m version
  is also sold.
- The stock **6-pin cable** from the AMS to the buffer.
- One **AMS1, AMS 2 Pro or AMS HT**, still running its Bambu Lab firmware.
- A Klipper printer that already prints, with **Moonraker** and **Mainsail**,
  and SSH access to its computer (a Raspberry Pi or similar Linux computer).
- A **filament cutter** at the toolhead. OpenAMS cuts the filament before every
  swap; tip forming is not supported.
- **PTFE tube, 3 mm inside and 4 mm outside diameter (3 mm ID / 4 mm OD)**, for
  the filament path: AMS to buffer, and buffer to the coupler below, and
  normally on to the toolhead. This size is critical. Narrower tube, such as
  the common 2 mm ID tube, adds so much friction that the AMS cannot push
  filament to the toolhead reliably. The one exception is the last tube, from
  the coupler into the toolhead, when your extruder only takes 2 mm ID tube.
- A printed **PTFE tube coupler v5**
  ([`hardware/ptfe-coupler/ptfe_coupler_v5.stl`](../../hardware/ptfe-coupler/ptfe_coupler_v5.stl))
  with two **ECAS04 push-fit collets**, joining the buffer's outlet tube to the
  tube that runs to the toolhead. Its **3** end takes the 3 mm ID tube from the
  buffer. Its **2** end takes either 3 mm ID or 2 mm ID tube to the toolhead,
  and tapers smoothly between the two sizes. The arrows on its body point the
  way the filament travels.

  ![PTFE tube coupler v5 with an ECAS04 collet in each end: 3 mm ID tube in the 3 end at the top, 2 mm ID tube in the 2 end below](../images/ptfe-coupler-v5.png)
- Only with more than one AMS: a printed **PTFE splitter**
  ([2 to 5 way PTFE splitter, MakerWorld](https://makerworld.com/en/models/816201-2-to-5-way-ptfe-splitter#profileId-2452271))
  that merges the AMS units' filament tubes into one before the buffer.

Start with one AMS. Add more after the first one loads and unloads cleanly; the
bridge takes up to four, daisy-chained.

## 2. Prepare the printer

1. Print something small and check the printer works **without** the AMS. Any
   problem you have now will still be there with the AMS attached.
2. In Mainsail, open **Machine** and download a backup of your configuration
   folder. You can restore it if anything goes wrong.
3. Update Klipper and Moonraker from Mainsail's **Machine → Update Manager**.
4. **Fit the stronger buffer spring.** Open the Bambu Lab filament buffer the
   way Bambu Lab's
   [buffer slider and spring steps](https://wiki.bambulab.com/en/ams/filament-buffer-connection-board-replacement-guide#step-1-remove-the-buffer-slider-and-spring)
   show, take out its spring,
   put in the stronger spring that came with the Cortex Bridge, and close the
   buffer again. The stronger spring lets the buffer push back against the
   longer PTFE tube of a Klipper printer without reporting the filament as
   arrived too early. Check that the slider moves freely and springs back.

   ![Inside the opened Bambu Lab filament buffer: the slider's coil spring, marked with a red arrow, is the spring to replace](../images/buffer-spring-replace.jpg)

   *Photo: [Bambu Lab Wiki](https://wiki.bambulab.com/ams/filament-buffer-connection-board/slide-spring-install.jpg), arrow added.*
5. Lay the PTFE tube from the buffer to the toolhead with gentle bends, joined
   just before the printer with the printed PTFE tube coupler v5. Push the
   tube from the buffer into the coupler's **3** end and the tube to the
   toolhead into its **2** end.
   - Use **3 mm ID / 4 mm OD** tube everywhere up to the coupler, and from the
     coupler to the toolhead too if your extruder takes it.
   - If your extruder's entrance only takes 2 mm ID tube (the Orbiter, for
     example), use 2 mm ID tube from the coupler to the toolhead. Do not join
     3 mm and 2 mm tube any other way: where 3 mm tube meets 2 mm tube
     directly, the 1 mm step catches the filament tip. The coupler's taper
     guides the filament past it.

   The filament must slide through the whole path by hand without catching.

**Check:** the printer prints, you have a configuration backup, and the
buffer has the stronger spring.

## 3. Wire the bridge

Do all the wiring with the printer **switched off at the mains** and the
bridge's USB unplugged.

First put the board in its case and mount it: three M2 × 10 screws from below
through the base and the board into the lid, then double-sided foam tape on
the base to stick the case to the printer's rear panel, within reach of the
printer's 24 V supply and of the buffer cable. The optional DIN clip mounts it
on a rail instead, for example under the printer, with the BIQU 4-pin extension
if the buffer's cable does not reach. [The case](cortex-bridge-hardware.md#the-case) has the
parts and the order.

![Wiring: Klipper computer, Cortex Bridge, printer's 24 V supply, AMS buffer and daisy-chained AMS units](../images/cortex-bridge-wiring.svg)

```text
Klipper computer ── USB-C ── Cortex Bridge ── 4-pin ── AMS buffer ── 6-pin ── AMS ── 6-pin ── next AMS
                                  │
                 printer's 24 V power supply (PWR IN)
```

1. **24 V to PWR IN.** Run two wires from spare terminals on the printer's
   24 V power supply: **+24 V** (often marked `V+`) and **0 V** (often `V−` or
   `COM`). Loosen the two screws on the bridge's green terminal, put the
   **+24 V** wire in the terminal marked **+** and the **0 V** wire in the one
   marked **−**, then tighten both and tug each wire. If you are not sure which
   supply terminals are spare, check the supply's label or the printer's
   wiring diagram before connecting anything.
2. **BUFFER to the buffer.** Plug the 4-pin cable into the bridge's BUFFER
   connector until the latch clicks. Plug its other end into the 4-pin
   connector on the AMS buffer. If the bridge sits further away, put the
   BIQU 4-pin 1 m extension ([BIQU](https://biqu.equipment/products/biqu-ams-upgrades-multi-material?variant=41897374351458),
   [Amazon](https://www.amazon.com/clp/B0F93RMF2X)) between Bambu Lab's cable and the bridge.
3. **AMS to the buffer.** Plug the AMS's stock 6-pin cable into the AMS and into
   the buffer, the same way it connects on a Bambu Lab printer. More AMS
   units are daisy-chained: each one's 6-pin cable goes to the free 6-pin
   port on the AMS before it, never back to the buffer.
4. **USB.** Connect the bridge's USB-C port to a USB port on the Klipper
   computer. Use a port on the computer itself, not a hub with its own power
   switch.
5. Leave the small **PRGRM** connector and the **5-pin header** empty.

See [The Cortex Bridge board](cortex-bridge-hardware.md) for pinouts and
pictures.

**Check:** every plug is fully seated, and the + wire is in the + terminal.

## 4. Power on

Switch the printer on. USB is already connected. Three LEDs light:

| LED | Colour | Means |
|---|---|---|
| D9 | White | USB power |
| D10 | Green | The bridge is running |
| D8 | Red | 24 V reaches the buffer and the AMS |

The AMS starts up as it does on a Bambu Lab printer.

**Check:** all three LEDs are on. If one is not, see
[Status LEDs](cortex-bridge-hardware.md#status-leds) and fix it before going on.

## 5. Install OpenAMS

Log in to the Klipper computer over SSH as the user that runs Klipper (on most
printers, `pi`). Keep the bridge and the AMS powered, then run:

```bash
git clone https://github.com/OpenAMSOrg/openams.git ~/openams
cd ~/openams
./install.sh
```

Do not put `sudo` in front of `./install.sh`; it asks for your password itself
when it needs it.

The installer sees the bridge on USB and does everything for it:

- installs the bridge relay, the `cortex-bridge` service that gives Klipper
  one serial port per AMS;
- adds you to the `dialout` group and sets up `/dev/oams`;
- writes `oams.cfg` from the Cortex Bridge sample and adds your AMS units to
  it, after asking each one what it is;
- adds OpenAMS to Mainsail's Update Manager.

The end of its output looks like this:

```text
Installing the Cortex Bridge relay...
  relay Python: /home/pi/klippy-env/bin/python
...
Cortex Bridge relay installed [OK]
...
Looking for AMS units behind the bridge (up to 45 s)...
  0a1b2c3d4e5f  AMS 2 Pro, 4 bays
Added ams2 to /home/pi/printer_data/config/oams.cfg.
Next: once Klipper is ready, let each AMS measure its filament path, with someone watching: `OAMS_CALIBRATE_PTFE_LENGTH OAMS=<n> SPOOL=<bay>` for each bay you use (oams_idx is in oams.cfg, bays count from 0).
...
Starting Klipper... [OK]
Waiting for Klipper (up to 60 s)...
Klipper is ready.
```

On a second run, Klipper can come up with an AMS the bridge stopped while
Klipper was stopped; the installer then sends **FIRMWARE RESTART** by itself
and says so.

**Check:** you see `Cortex Bridge relay installed [OK]` and one line per AMS.

- `No Cortex Bridge connected; skipping its relay` means the computer cannot
  see the bridge. Check the USB cable and the white LED, then run
  `./install.sh` again. It is safe to run as often as you like.
- `No AMS was added to oams.cfg yet` means the bridge is fine but no AMS
  answered. Update the bridge firmware first (below). If still no AMS
  answers, check the red LED and that the BUFFER cable and the AMS cable are
  fully seated at both ends, then run
  `python3 ~/openams/tools/cortex-bridge.py config --write`.
- `oams.cfg was left unchanged: it still describes OpenAMS 1 hardware` means
  this printer ran OpenAMS 1 before. The installer doesn't add the bridge's
  AMS next to the old unit, because the two setups would mix. To move to the
  bridge, run:

  ```bash
  python3 ~/openams/tools/cortex-bridge.py config --write --replace-openams1
  ```

  It comments out the OpenAMS 1 unit, its board's `[mcu]`, the sections that
  use that board (its temperature sensor and RFID readers), its filament
  groups and the `[fps]` section with a `pin:`, each marked
  `# cortex_bridge config:`. Then it adds the bridge's AMS, with groups `T0`
  onwards and an `[fps]` that reads the buffer through the AMS. Your cutter
  values, macros and other sections stay as they are. Groups start again at
  one bay each, so set up any spare-spool groups again afterwards. If the old
  FPS board is still connected, for example as the USB-to-CAN bridge for your
  toolhead board, keep its `[mcu]`; if it's gone, comment that out too.

> [!IMPORTANT]
> **Update the bridge firmware now, before anything else.** A bridge can
> reach you with older firmware than your OpenAMS bundles, and an older bridge
> may not recognise a newer AMS: the AMS lights up but never answers. With the
> printer idle and no filament loaded, run:
>
> ```bash
> python3 ~/openams/tools/cortex-bridge.py update
> ```
>
> Answer `y`. It takes about a minute, keep USB and 24 V connected, and it ends
> with `The bridge now runs version <n>.` `Nothing to do: the bridge is up to
> date.` means there was nothing to install. If the installer added no AMS,
> run `python3 ~/openams/tools/cortex-bridge.py config --write` after the
> update, then **FIRMWARE RESTART**.
> [Update the Cortex Bridge firmware](cortex-bridge-update.md) has the details.

## 6. Check the bridge

One command shows what is connected and the next thing to do:

```bash
python3 ~/openams/tools/cortex-bridge.py status
```

```text
Cortex Bridge
  USB:         running, serial CB0123456789AB, production firmware on /dev/ttyACM0
  Relay:       active (1 AMS online)
  AMS units:
    0a1b2c3d4e5f  online   /dev/oams/0a1b2c3d4e5f  configured
  Firmware:    version 7, up to date
  Klipper:     active

Everything is connected.
```

**Check:** the last line says `Everything is connected.` Anything else names
the problem and its fix; [troubleshooting](#troubleshooting) has the full list.

The `Firmware:` line is the bridge's own firmware: the version it runs, and
whether a newer one came with OpenAMS. `version 4; version 7 is available`
means an update is waiting. During setup, install it now, as in step 5;
later, [update the bridge firmware](cortex-bridge-update.md) when it suits
you.

## 7. Finish the config

Open `oams.cfg` in Mainsail (**Machine**, `config` folder). At the end, between
the `Cortex Bridge units` markers, is one `[mcu]` and one `[oams]` section per
AMS, a `[filament_group]` (`T0`, `T1`, ...) per bay, and the `[fps]` section
that reads the buffer through the AMS. Three things need your printer's values.

### Filament path length

OpenAMS refuses to load a bay whose `ptfe_length` is 0. With Cortex Bridge
firmware 8 or later the AMS measures it for you; with older firmware you
measure it by hand. `python3 ~/openams/tools/cortex-bridge.py version` shows
which firmware the bridge runs, and [the update guide](cortex-bridge-update.md)
installs a newer one.

**Let the AMS measure it (firmware 8 or later).** Leave `ptfe_length: 0` for
now and finish the cutter and the toolhead macros below. After the
**FIRMWARE RESTART** at the end of this step, put filament in each bay you will
use, stay with the printer, and run this in the Mainsail console, once per bay:

```gcode
OAMS_CALIBRATE_PTFE_LENGTH OAMS=1 SPOOL=0
```

`OAMS=` is the AMS's `oams_idx` (1 for the first), `SPOOL=` the bay, counted
from 0. The hotend can stay cold: the extruder doesn't move. The AMS feeds the
filament at full speed until the buffer compresses at the extruder gears,
takes the distance there, and rewinds it to the bay:

```text
Calibrating ams1 Spool 1: the AMS feeds the filament to the toolhead (at most 4000 mm), measures the path at the buffer, and rewinds it. Stay with the printer.
Calibrated ams1 Spool 1: 2034 mm of filament path, ptfe_length_0: 2319 (saved to /home/pi/printer_data/config/oams.cfg).
```

The result is saved as that bay's `ptfe_length_<bay>` and used at once. The
feed stops after at most 4 m (plus 200 mm), so a disconnected tube never gets
more than that; for a longer path, add `MAX_LENGTH=<millimetres>`.

After that, every load reports the path it measured on the way, in the
console, the first time for a bay and whenever it differs from `ptfe_length`
by more than 100 mm, with the command that updates it:

```text
ams1 Spool 2 reached the toolhead after 2030 mm of filament path (ptfe_length: 2500 mm). To use the measured length, run OAMS_CALIBRATE_PTFE_LENGTH OAMS=1 SPOOL=1, or set ptfe_length_1: 2314 under [oams ams1].
```

**Measure it by hand (older firmware).** Measure the filament path from the
AMS outlet, through the buffer, to the extruder gears, in millimetres: the
length of the tube from the AMS to the buffer, plus the tube from the buffer
to the toolhead, plus the path inside the toolhead to the gears. Then convert
it:

```bash
python3 ~/openams/tools/cortex-bridge.py ptfe 2050
```

```text
2050 mm of filament path is ptfe_length: 2337
```

Put that number in the AMS's `[oams]` section:

```ini
ptfe_length: 2337
```

The value is in the AMS's encoder clicks (1.14 per millimetre), which is why
the command converts it. If one bay's path is longer than the others, give
that bay its own value with `ptfe_length_<bay>:` (bays count from 0).

Either way, the length matters:

- **What it does.** A load runs at full speed until the buffer starts to
  compress, then slows down and stops when the buffer reads arrival. The
  length limits the full-speed part: the bridge stops a load that has fed
  about 200 mm more than `ptfe_length` without the buffer compressing, so a
  broken or unplugged tube never gets metres of filament.
- **Too short** stops every load before the toolhead. Firmware 8 or later
  says `Spool loading failed (the filament fed <n> mm (ptfe_length <n> mm +
  200 mm) without the toolhead buffer reaching arrival_threshold: ...)`; older
  firmware says `the AMS stopped feeding before the toolhead buffer reached
  arrival_threshold`, and the AMS reports `motion_timeout`. Clear the fault
  with `OAMS_CLEAR_FAULT OAMS=<n>`, then rewind the bay with
  `OAMSM_UNLOAD_FILAMENT` (the filament stopped short of the extruder, so no
  cut is needed), then calibrate the bay. A calibration refuses to start while
  that filament is still out. On older firmware, measure how far the tip
  stopped from the extruder gears instead: the path is about your old length,
  plus 200 mm, plus that gap.
- **Too long** is no fix either. When a spool runs out during a print, the
  reload of the next spool starts from this length, so a length longer than
  the path starts it late.

### The cutter

OpenAMS cuts the filament at the toolhead before every swap. In
`[gcode_macro _oams_macro_variables]`, at the top of `oams.cfg`, set
`pre_cut_x`/`pre_cut_y`, `cut_x`/`cut_y` and `post_cut_x`/`post_cut_y` to your
printer's cutter positions. The values in the file are an example for one
printer, not yours. [Configure](configure.md#gcode_macro-_oams_macro_variables)
explains each value, and how to replace the moves with your own `OAMS_CUT`
macro.

### The toolhead macros

OpenAMS asks your printer to grab the filament after a load
(`toolhead_load_macro`, default `LOAD_FILAMENT`) and to eject it before an
unload (`toolhead_unload_macro`, preset to `_OAMS_TOOLHEAD_EJECT`, which ships
with OpenAMS and ejects the filament past the extruder gears). Your printer
needs a `LOAD_FILAMENT` macro that only moves the extruder. Most printers
already have one in `printer.cfg`; check what yours does.

*Why `LOAD_FILAMENT` is needed.* The AMS pushes the filament only as far as
the extruder gears: there the buffer compresses and the AMS stops. The last
stretch, from the gears through the hotend to the nozzle, is up to the
extruder, and only your printer knows how long it is. So once the AMS has
stopped, OpenAMS runs your `LOAD_FILAMENT`, and the AMS feeds along behind as
the extruder pulls. It does this in `OAMSM_LOAD_TO_TOOLHEAD`, the load without
the cutter that you use for the first load. Without the macro, that load fails
with the filament left at the gears.

*Why only extruder moves.* OpenAMS calls the macro in the middle of
`OAMSM_LOAD_TO_TOOLHEAD` and waits for its moves to finish. All it needs there
is the pull, so plain `G1 E` moves are enough. Everything else is done
elsewhere: you heat the hotend before this load, and the toolchanges a print
runs (`T0`, `T1`, ...) do their own cut, load and purge and never call
`LOAD_FILAMENT`. A macro that heats to its own temperature, homes, parks or
purges does work this load does not need; one that calls an `OAMSM_` command
starts another AMS move in the middle of this one. A typical `LOAD_FILAMENT`:

```ini
[gcode_macro LOAD_FILAMENT]
gcode:
  M83            ; relative extrusion
  G1 E30 F300    ; pull the tip from the gears down to the nozzle
  G1 E15 F150    ; prime the nozzle, slower
  M82            ; back to absolute extrusion
```

The lengths depend on your toolhead: the first move brings the tip from the
gears to the nozzle, the second, slower one primes it. The hotend must be hot:
if it is cold, Klipper refuses the move and OpenAMS stops with
`AMS load reached the toolhead, but LOAD_FILAMENT failed`, leaving the
filament at the gears. If your `LOAD_FILAMENT` does more than move the
extruder, add an extruder-only macro under another name and set
`toolhead_load_macro:` in `[oams_manager]` to it; see
[configure](configure.md#oams_manager).

When all three are set, restart Klipper (**FIRMWARE RESTART** in Mainsail).
Use **FIRMWARE RESTART** after every change to `oams.cfg`, not **RESTART**:
the bridge stops an AMS as soon as Klipper goes quiet, so after a plain
restart Klipper reports `Can not update MCU '...' config as it is shutdown`.

**Check:** Klipper is **Ready**, and the console command `OAMSM_SELFTEST`
reports each AMS without errors.

### Adding another AMS later

The bridge takes up to four AMS units. Switch the printer off. The AMS units
are daisy-chained: only the first one connects to the buffer. Plug the new
AMS's 6-pin cable into the free 6-pin port on the last AMS in the chain. Bring
every AMS's filament tube into a printed
[PTFE splitter](https://makerworld.com/en/models/816201-2-to-5-way-ptfe-splitter#profileId-2452271),
whose single outlet tube goes into the buffer. Switch the printer back on, then
run:

```bash
python3 ~/openams/tools/cortex-bridge.py config --write
```

It adds only the new AMS, after the existing ones, and never changes the
sections you have already set. It stops Klipper for a few seconds while it
identifies the AMS, and starts it again. Set the new AMS's `ptfe_length`, then
restart Klipper.

## 8. Add the Mainsail panel

The OpenAMS panel shows each AMS, its bays and spools on Mainsail's dashboard,
and loads, unloads, edits groups and starts drying from there. It comes in the
OpenAMS build of Mainsail: Mainsail itself, with the panel added. Install it
with:

```bash
~/openams/install.sh --mainsail-panel
```

It downloads the latest OpenAMS Mainsail, keeps your Mainsail settings, keeps
the Mainsail you had as `~/mainsail.stock`, and points Mainsail's
**Machine → Update Manager** entry `mainsail` at the OpenAMS build, so
Mainsail's own update button keeps the panel. Reload Mainsail with
**Ctrl+Shift+R** (Cmd+Shift+R on a Mac): a plain reload can show the old
Mainsail from the browser's cache. The panel is added at the end of the
dashboard's first column; move it under **Settings → Dashboard**.

**Check:** the dashboard has an **OpenAMS** panel showing your AMS and its
bays.

A Mainsail that is a link or a git checkout (your own build) is left alone.
The panel is built from [openams-ui](https://github.com/OpenAMSOrg/openams-ui);
the build is [OpenAMSOrg/mainsail](https://github.com/OpenAMSOrg/mainsail).

## 9. First load

Do this with the printer idle and stay with it. The AMS does the feeding;
your job is to watch the filament path.

1. Insert filament into the first bay of the AMS, as on a Bambu Lab printer,
   until the AMS takes it.
2. Heat the hotend to that filament's temperature, for example `M109 S220`.
3. In the Mainsail console, load it to the toolhead:

   ```gcode
   OAMSM_LOAD_TO_TOOLHEAD GROUP=T0
   ```

   The filament travels to the buffer, then to the toolhead. When it reaches
   the extruder the buffer compresses, the AMS stops, and your
   `LOAD_FILAMENT` pulls it in.
4. Unload it again:

   ```gcode
   OAMSM_UNLOAD_FROM_TOOLHEAD
   ```

   The toolhead ejects the filament past its gears and the AMS rewinds it to
   the bay.
5. With filament in two bays and the cutter set, run a real swap: `T0`, then
   `T1`, then `T0`. Watch the cutter, the retract and the purge. A screen's
   load and unload buttons run this same toolchange, so use them only once the
   cutter is set.

**Check:** both loads reached the toolhead and both unloads returned the
filament to its bay. You are ready to print: slice with one tool per
`[filament_group]` (`T0`, `T1`, ...). [Using OpenAMS](using.md) covers
toolchanges, runout, groups and the rest.

If a load stops just short of the toolhead with a message that the reading
stayed below `arrival_threshold`, lower `arrival_threshold:` in `[fps]` under
the value the message names; see
[configure](configure.md#host-confirmed-arrival-from-a-relayed-fps).

## Drying

An AMS 2 Pro or AMS HT dries from Mainsail's panel or with
`OAMS_DRYER_START OAMS=<n> TARGET=<C> DURATION=<s>`. The AMS HT needs its own
power adapter for the heater.

With older bridge firmware the vents stay closed for the whole dry, so the
moisture the filament gives off has nowhere to go. With bridge firmware 10 or
later, the bridge opens them. It does this from the moisture in the chamber,
the absolute humidity it works out from the AMS's humidity and temperature
readings:

- Once the chamber is at temperature and the moisture has risen and stopped
  rising, the bridge opens the vents. If that hasn't happened within an hour,
  it opens them anyway.
- The AMS then keeps them open until the dry ends, as it does on a Bambu
  printer. Closing them in between comes with a later bridge firmware.
- When the dry time is up, the heater stops and the vents close until the
  chamber is below 45 °C.

The bridge does this by itself, so a dry finishes the same way when Klipper
restarts. Mainsail's panel shows the humidity. `VENT=unit` on
`OAMS_DRYER_START` leaves the vents to the AMS: closed for the whole dry. The
numbers can be changed with the `dryer_vent_*` options in `[oams <name>]`; see
[the reference](reference.md#oams-name).

## Keep it up to date

- **OpenAMS**, including the bridge relay, updates from Mainsail's
  **Machine → Update Manager**, entry `openams`. It restarts the relay and
  Klipper for you.
- **The bridge's own firmware** comes as signed packages with OpenAMS
  updates. After updating OpenAMS, `python3 ~/openams/tools/cortex-bridge.py
  version` says whether the bridge is up to date.
  [Update the Cortex Bridge firmware](cortex-bridge-update.md) explains when
  and how to install a new version.

## Troubleshooting

Start with `python3 ~/openams/tools/cortex-bridge.py status`; its last line
names the first thing to fix.

| You see | What it means | What to do |
|---|---|---|
| No LEDs at all | No USB power and no 24 V | Check both cables, and that the printer is on. |
| White LED off | No USB power | Use a data cable in a port on the computer itself. |
| Red LED off | No 24 V at the BUFFER connector | Check the wires from the printer's 24 V supply, their polarity at PWR IN, and the 5 A fuse next to it. |
| `The bridge is not on USB` | `lsusb` does not list `1a86:fe11` | A charge-only cable, a bad port, or a USB hub. Try another cable and port. |
| `The bridge relay is not installed` | OpenAMS was installed without the bridge connected | Connect it and run `~/openams/install.sh` again. |
| `Start the relay` | The `cortex-bridge` service is stopped or failed | `sudo systemctl start cortex-bridge`, then `status` again. |
| `No AMS answers` | The bridge is running but sees no AMS | Update the bridge firmware (`cortex-bridge.py update`): an older bridge may not recognise a newer AMS. Then check the red LED, and that the 4-pin BUFFER cable and the 6-pin AMS cable are fully seated at both ends. Power the AMS off and on. |
| `... AMS online but not in your config` | A new AMS was connected | `cortex-bridge.py config --write` |
| `An AMS (...) keeps announcing itself, but the bridge has not offered it an index` (its LED blinks red) | Bridge firmware 9 can take up to 36 minutes to give a newly connected AMS its index once the bridge has been running a while | Switch the printer off (the bridge's 24 V and USB) for 10 seconds, then run **FIRMWARE RESTART** once Klipper is up, and `cortex-bridge.py config --write` if the AMS is new. Or wait: it is added by itself within 36 minutes. |
| `The bridge is waiting for firmware` | A firmware update was interrupted | `cortex-bridge.py update`; see [the update guide](cortex-bridge-update.md). |
| `Firmware: installed version unknown: ...` | The bridge has not said which version it runs; the rest of the line says why | See [the update guide](cortex-bridge-update.md#when-the-version-is-unknown). |
| `The bridge reports a fault` | The bridge's own self-test failed | Unplug USB for 10 s; if it stays, write a support report (below). |
| Klipper: `Unable to open serial port '/dev/oams/...'` | Klipper started before the AMS was up | Power the AMS, wait for `status` to show it online, then **FIRMWARE RESTART**. |
| Klipper: `Can not update MCU '...' config as it is shutdown` | Klipper was stopped for more than a few seconds. The bridge stops an AMS whenever Klipper goes quiet, so nothing keeps running unattended | **FIRMWARE RESTART**. |
| Klipper: `<ams> Spool N has no PTFE length` | `ptfe_length` is 0 for that bay | Run the command the message names: `OAMS_CALIBRATE_PTFE_LENGTH OAMS=<n> SPOOL=<bay>` on firmware 8 or later, or [measure and set it](#filament-path-length) on older firmware. |
| `PTFE calibration of <ams> Spool N failed: the filament fed <n> mm (MAX_LENGTH <n> mm + 200 mm) without reaching the toolhead buffer` | The path is longer than `MAX_LENGTH`, or the filament isn't reaching the toolhead | Look where the tip stopped. Running freely: run it again with a larger `MAX_LENGTH=`. Stuck: fix the path. |
| `PTFE calibration ... failed: the filament stopped moving for 4 s` | A snag in the path, at a joint, a bend or the hub | Push filament by hand through the whole path; fix where it catches. |
| A load stops before the toolhead: `the filament fed <n> mm (ptfe_length ... + 200 mm)` (firmware 8 or later) or `the AMS stopped feeding before the toolhead buffer reached arrival_threshold` (older), fault `motion_timeout` | `ptfe_length` is shorter than the filament path, or the filament snagged (`stopped moving for 4 s` on firmware 8) | Clear the fault with `OAMS_CLEAR_FAULT OAMS=<n>`, then `OAMSM_CLEAR_ERRORS`. If the filament runs freely, the length is too short: [calibrate the bay](#filament-path-length), or set the old length plus 200 mm plus the gap left at the gears. |
| Loads stall, or the buffer reports arrival before the filament reaches the toolhead | Too much friction in the filament path | Check that every tube up to the coupler is 3 mm ID / 4 mm OD (not 2 mm ID), any 2 mm ID tube starts at the coupler's 2 end, the bends are gentle, the coupler and splitter joints are smooth, and the stronger buffer spring is fitted. |
| A load stops just short of the toolhead | The buffer reading did not reach `arrival_threshold` | Lower it as the message suggests ([details](configure.md#host-confirmed-arrival-from-a-relayed-fps)). |
| An unload ends in a unit fault | The filament tip stayed in the extruder gears | Use `toolhead_unload_macro: _OAMS_TOOLHEAD_EJECT`. |
| No OpenAMS panel in Mainsail after step 8 | The browser shows its cached Mainsail, or the panel is at the bottom of the dashboard | Reload with **Ctrl+Shift+R** and scroll down; more in [troubleshooting](troubleshooting.md#the-mainsail-panel-and-spoolman). |
| `[WARNING] Spoolman is installed but does not answer` | Spoolman's own install failed part-way | Run `./install.sh` again; `sudo journalctl -u Spoolman -n 50 --no-pager` shows why. |

The relay keeps a short support log, `cortex-bridge.jsonl`, under Mainsail's
**Machine → Log files**. To send everything support needs in one file:

```bash
python3 ~/openams/tools/cortex-bridge.py support
```

It writes `cortex-bridge-support-<date>.txt` next to the other logs: the
bridge's state and firmware, the relay, Klipper's state, the OpenAMS sections
of your config, the installer's last output and the end of the logs.
Passwords, keys, addresses, names, AMS serial numbers, tag data and spool
details are removed before it is written, and writing it sends nothing.
[The support report](support-report.md) lists what is in it and how to send
it.

For OpenAMS itself, [troubleshooting](troubleshooting.md) matches Klipper's
error messages to their causes.
