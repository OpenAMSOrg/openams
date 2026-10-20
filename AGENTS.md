# OpenAMS: context for AI agents

This file is for an AI agent working in this repository, or helping someone
whose OpenAMS printer doesn't load, unload or change tools. It explains why
things work or fail on a real machine, so that a fix goes to the actual cause.

- Installing OpenAMS on a printer: follow [install.agents.md](install.agents.md).
- Working in `klipper_mmu/`: read [klipper_mmu/AGENTS.md](klipper_mmu/AGENTS.md).
- Error text matched to a fix: [docs/guide/troubleshooting.md](docs/guide/troubleshooting.md).
- The customer-facing guide built from the same support history:
  <https://openams.si-forge.com/en/running-openams>.

---

## The repository

| Path | What is in it |
|---|---|
| `src/` | The Klipper host plugin (extras): `oams.py` (one unit), `oams_manager.py` (lanes, groups, commands), `oams_runtime.py` (load/unload/runout monitors), `fps.py`, `filament_group.py` |
| `moonraker/` | The `openams_spoolman` Moonraker component |
| `oams_macros.cfg`, `oams_sample.cfg` | The shipped toolchange macros (linked into the user's config, replaced on update) and the template for the user's own `oams.cfg` |
| `tools/agent/` | Read-only checks: `bringup_check.py`, `config_audit.py`, `preflight.sh` |
| `cortex_bridge/`, `tools/cortex-bridge.py` | Stock AMS units behind a Cortex Bridge |
| `klipper_mmu/` | The klipper-mmu MCU add-on (GPL-3.0-or-later), with its own `AGENTS.md` |
| `klipper_patches/can/` | The optional Klipper CAN patch series |
| `docs/guide/` | The user guide; `docs/contracts/` holds the C1–C5 interface contracts |
| `test/` | The host plugin's tests |

Run `make test-openams` (that is, `python3 -m unittest discover -s test`) for
the host plugin, and `make test` for everything, klipper-mmu included.

---

## How the machine works

Most wrong fixes come from a wrong picture of this.

**First, find out which hardware the printer has.** The AMS mechanics and
everything printer-side are the same on both, but the electronics, the setup
and half the troubleshooting are not.

| | OpenAMS 1 | 0AMS (Cortex Bridge) |
|---|---|---|
| Inside the AMS | An OpenAMS replacement mainboard (STM32F072) with its own mainboard firmware | The stock Bambu Lab board and firmware; nothing is opened or flashed |
| Buffer | The OpenAMS FPS board and its slide, with the kit's spring | Bambu Lab's own filament buffer, with the stronger spring that ships with the bridge |
| Connection | CAN, with the FPS board usually as the USB-to-CAN bridge; `[mcu …] canbus_uuid:` | A Cortex Bridge on USB; the `cortex-bridge` relay gives each AMS a serial port, `[mcu …] serial: /dev/oams/<uid>` |
| Buffer reading | Wired to the FPS board: `[fps] pin: fps:PA2` | Relayed by the AMS: `[fps] source_oams: oams1`, with arrival confirmed by the host at `arrival_threshold` (default 0.65) |
| `ptfe_length` | Calibrated: `OAMS_CALIBRATE_PTFE_LENGTH OAMS=<n> SPOOL=<bay>` | Measured and converted: `cortex-bridge.py ptfe <mm>` (encoder clicks, 1.14 per mm) |
| First check | `bringup_check.py --selftest`, then the CAN bus | `python3 ~/openams/tools/cortex-bridge.py status`, then the ladder |
| Setup guide | [install](docs/guide/install.md), [configure](docs/guide/configure.md), [docs/stm32f072.md](docs/stm32f072.md) | [Set up a Cortex Bridge printer](docs/guide/cortex-bridge-setup.md) |

A third kind of unit, an MMU running klipper-mmu (BoxTurtle, ERCF and others),
looks the same from the host; see `klipper_mmu/`.

- **The AMS is simple.** It picks a spool and pushes the filament down the
  tube until the filament presses against the extruder's gears, then it holds
  it there. Everything else in a toolchange is the printer's job.
- **The FPS is the AMS's endstop.** The hub motor is a BLDC, and a BLDC has
  no sense of distance. The AMS knows the filament has arrived only because
  the FPS slide is pushed home: its reading goes from about 0 (relaxed) to
  about 1 (compressed).
- **`ptfe_length` sets when to listen.** The unit feeds fast until it is
  100 mm short of `ptfe_length`, then slows down and stops once the buffer
  reads arrival (`fps_upper_threshold` on OpenAMS 1, the host's
  `arrival_threshold` on a Cortex Bridge). Before that point the reading is
  ignored, so a slide that compresses early only matters if the load stops
  there. While `ptfe_length` is 0, a Cortex Bridge AMS refuses to load, and
  on OpenAMS 1 a runout pauses instead of reloading.
- **The stall guard.** The unit's encoder counts the filament as it moves. If
  the motor runs and the count stops, `src/oams_runtime.py` fails the
  operation, lights the bay LED red, and pauses with `loading speed too low`
  or `unloading speed too low`. That is the AMS protecting itself and the
  filament. The fault is wherever the filament got stuck.
- **The toolchange (`_TX` in `oams_macros.cfg`):**
  1. heat, and home if needed;
  2. cut: `CUT_FILAMENT`, or the user's `OAMS_CUT`;
  3. retract: `extrusion_unload_length`, then `extrusion_unload_additional_length`
     (default 250) as **one** `G1` move;
  4. the AMS rewinds;
  5. the AMS loads the new bay;
  6. `CLEAN_NOZZLE`;
  7. reload: `extrusion_reload_length`;
  8. the optional sensor checks.

  Steps 2, 3 and 7, and the `CLEAN_NOZZLE` macro, belong to the printer.
  On failure the macro turns the follower off, pauses, and raises.
- **There are two "reversed" settings.**
  - `fps_is_reversed:` in `[oams …]` is the unit's own reading. That is the
    one the load stops on.
  - `reversed:` in `[fps]` is the host's copy, and only changes what the UIs
    draw.

---

## Where failures actually come from

These numbers come from the OpenAMS Discord support channels, December 2024 to
October 2026: 333 reports, of which 249 had a fault behind them. A language
model extracted them, and the OpenAMS-side tags were checked by hand. Treat the
shares as approximate. Nearly all of these reports come from OpenAMS 1
printers, because the Cortex Bridge is newer. The CAN and flashing share
doesn't apply to a Cortex Bridge, but the filament path and printer shares do.

| Root cause | Share of faults |
|---|---:|
| Installing and configuring: CAN, flashing, config files | 46% |
| The filament path: tube ID, fittings, springs, the FPS case | 20% |
| The printer: extruder grip, cutter, toolhead switches, macros, slicer, host power | 15% |
| Wear and dust inside the AMS | 8% |
| An OpenAMS software or firmware bug (most fixed by an update) | 8% |
| A faulty OpenAMS board | 3% |

**The prior that follows:** when a load, an unload or a toolchange fails on a
machine that passes the bring-up ladder, the cause is almost always in the
filament path or the printer, not in the AMS or this code. Look there first,
and say so to the user.

---

## Field facts

Each of these settled many support threads. They're consistent with
`oams_sample.cfg` and the guide.

- **Tube:** 3 mm ID / 4 mm OD PTFE for the whole path, ideally one piece.
  - 2 mm ID tube, and Bambu Lab's 2.5 mm ID tube, add enough friction that
    the FPS compresses before the gears, and the load stops short.
  - Many PC4-M6 fittings are made for 2 mm ID tube, and leave a 1 mm step
    inside that catches the filament tip; PC4-M10 fittings don't.
  - Extruders that take only 2 mm ID tube need a smooth, tapered 3-to-2 mm
    transition, such as the printed PTFE coupler v5
    (`hardware/ptfe-coupler/ptfe_coupler_v5.stl`). Bambu couplers aren't
    suitable.
- **Buffer spring:** use the one that came with the hardware.
  - OpenAMS 1: the FPS kit's spring (0.6 mm stainless 304 wire, 30 mm long,
    12 mm OD, 6 coils).
  - Cortex Bridge: the stronger spring that ships with the bridge, fitted into
    Bambu's buffer.

  Both are stiffer than Bambu's stock spring, on purpose, to allow for a
  Klipper printer's longer tube.
  - A spring stiffer than that lets the BLDC push against a snag until it
    grinds a flat spot into the filament.
  - The flat spot then fails at the feeder next time, and the ground plastic
    jams the hub gears and the encoder.
  - A stepper-driven MMU may tolerate a stiffer spring. The AMS doesn't.
- **No filament switch in front of the extruder gears.** It adds resistance
  exactly where the FPS has to read arrival, and it is redundant: the FPS is
  the endstop. After the gears is fine (`fs_extruder_out`).
- **A cutter is required.** Tip forming (`oams_tip_forming.cfg`) is
  undocumented and use-at-your-own-risk. Never offer it as a fix.
- **The extruder must grip and let go.** On a Clockwork 2, check the
  anti-squish screw and the grub screw on the 50T gear shaft. If 250 mm of
  extra retract didn't free the filament, more won't: either the cut didn't
  go through or the extruder isn't gripping.
- **`max_extrude_only_distance`** in `[extruder]` must be at least
  `extrusion_unload_additional_length`. Klipper's default is 50 mm, and the
  250 mm retract then fails with `Extrude only move too long`.
  **`config_audit.py` doesn't check this yet.**
- **`[exclude_object]`** must exist, because `_TX` reads
  `printer.exclude_object`. Without it, every toolchange fails with a jinja
  `UndefinedError`. **`config_audit.py` doesn't check this yet.**
- **After any grinding,** cut back past the flat spots and blow out the AMS
  hub with compressed air. Dust in the hub is the most common reason a bay
  reads as empty or a feeder runs on and on.
- **CAN (OpenAMS 1 only):**
  - one USB-to-CAN bridge per bus, and exactly two 120 Ω terminations;
  - after a hang or a flash, a full power-off, not `FIRMWARE_RESTART`;
  - `canbus_query.py` lists only boards Klipper hasn't claimed, so stop
    Klipper and power-cycle first to see every UUID;
  - an undervolted Raspberry Pi drops its USB devices, the bridge included.
- **Cortex Bridge (0AMS only):**
  - `cortex-bridge.py status` names the first thing to fix, and
    `cortex-bridge.py support` writes the report to attach.
  - A load that stops just short of the toolhead, with a message that the
    reading stayed below `arrival_threshold`, needs that threshold lowered
    under the peak the message names. Look at the path first if the peak is
    far below.
  - The bridge stops an AMS whenever Klipper goes quiet. After a Klipper stop,
    `Can not update MCU … as it is shutdown` needs `FIRMWARE_RESTART`, and
    nothing is wrong.

---

## Compensations: never recommend them, never build them

A compensation makes one symptom go away and moves the damage somewhere else,
usually into the AMS.

| Compensation | What it actually does |
|---|---|
| A stiffer FPS spring, so the slide stops compressing early | The BLDC grinds the filament at the snag |
| An offset added to `ptfe_length`, so the FPS is ignored for longer | A few millimetres of encoder error and the unit reaches the gears at full speed; it slams the buffer and eventually breaks the slide |
| Running the AMS together with the extruder, to make up for an extruder with no grip | Filament chewed on every rewind; the extruder is still weak |
| A toolhead switch as the endstop "to be sure" | One more snag point in front of the gears |
| Retract lengths raised again and again | Hides a cut that doesn't go through, or an extruder that doesn't grip |
| A higher stall timeout, or a looser encoder threshold | The guard stops protecting the filament and the hub gears |

**For code changes, the same rule.** Don't add options or defaults that:

- relax the stall guard;
- ignore the FPS for longer;
- drive the AMS against the extruder to get past a jam;
- or otherwise let a bad path "work".

The FPS and the stall guard are messengers. A request to soften them almost
always comes from a printer-side problem. Answer that problem instead, and
point to the test that finds it.

---

## Diagnosing a printer, in order

1. **Read-only checks.** On a Cortex Bridge, start with
   `python3 ~/openams/tools/cortex-bridge.py status`. Then, on either
   hardware, fix the lowest failing level first:
   ```bash
   python3 ~/openams/tools/agent/bringup_check.py --selftest
   python3 ~/openams/tools/agent/config_audit.py --config-dir ~/printer_data/config
   bash ~/openams/tools/agent/preflight.sh
   ```
   Then check the two gaps the audit misses: `max_extrude_only_distance` and
   `[exclude_object]`.
2. **The FPS reading.** It should be about 0 with the slide relaxed and about
   1 pushed home. The values come from L5 of the ladder, or from `OAMSM_SELFTEST`.
3. **The hand tests.** A human does these; an agent never moves the printer:
   - **The path:** unhook the tube at the toolhead and push fresh filament by
     hand through the whole path. It must not catch anywhere.
   - **The extruder:** without the AMS, extrude and retract about 100 mm,
     then again with light resistance on the filament.
   - **The cutter:** run `CUT_FILAMENT` alone, then pull the filament out by
     hand. It must come free, with a clean cut.
   - **The unload:** after an unload, unhook the tube and pull. The filament
     must already be free of the extruder.
4. **Only then** a watched load and unload (`OAMSM_LOAD_TO_TOOLHEAD GROUP=T0`,
   `OAMSM_UNLOAD_FROM_TOOLHEAD`), then `T1` and `T0`.

**What to ask for:**

- `klippy.log` from the failing attempt;
- `oams.cfg`, and whether `oams_macros.cfg` was edited;
- photos of the whole path, and the tube's ID;
- the toolhead, extruder and cutter;
- Klipper or Kalico.

---

## Talking to the user

- **Be kind.** Many are on their first multi-material printer and don't yet
  know what a toolchange does. Point them to the customer guide, which
  explains it from the start, instead of teaching it one question at a time.
- **Be plain about where the problem is.** OpenAMS is a replacement board,
  firmware and a plugin; the toolhead, cutter, tube and slicer are theirs.
  When the evidence points at the printer, say so, with the test that proves
  it.
- **Fix the cause, not the symptom** (see compensations above). If you can't
  find the cause, say what you checked and what the human should check next.
- **Never name other users** from the support history.
