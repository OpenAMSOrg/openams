# CyberBrick Time-lapse Kit

The Bambu Lab CyberBrick Time-lapse Kit triggers a camera (a servo, Bluetooth
or a jack) from an AMS's 6-pin bus port. With Cortex Bridge firmware 40 or
later, OpenAMS fires it from Klipper. Nothing needs setting up: with the kit
plugged in and the bridge relay running, its light shows solid green while it
is linked, and it fires; without it nothing changes.

## Firing it

| G-code | What it does |
|---|---|
| `CYBERBRICK_SHOT` | Fires the kit once (`P=` the M1004 S5 P value, 1 by default). It answers on the console; `QUIET=1` says nothing |
| `M1004 S5 P1` | The same, as Bambu Studio writes it in a sliced file's time-lapse G-code. Any other `M1004` does nothing, and `M1004 S5` without `P` sends nothing, as on a Bambu printer. A printer config that defines its own `M1004` keeps it, and `CYBERBRICK_SHOT` still works |

A shot fires when Klipper reads the command, which is ahead of the moves
queued before it. Put `M400` before it where the head must be still: Bambu's
time-lapse G-code does, and moonraker-timelapse does when it parks.

Shots closer together than `cyberbrick_min_interval` are not sent;
`printer.cyberbrick.rate_limited` counts them.

## moonraker-timelapse

```
[oams_manager]
cyberbrick_timelapse_frames: True
```

Each moonraker-timelapse frame then fires the kit too, right after its own
`_TIMELAPSE_NEW_FRAME` macro: once the head is parked, or straight away when
it does not park. moonraker-timelapse's files stay as they are. Without
moonraker-timelapse the option does nothing.

## Status

`printer.cyberbrick` (Moonraker, macros, panels):

| Field | Meaning |
|---|---|
| `available` | a Cortex Bridge relay runs, with production firmware 40 or later |
| `present` | the kit is linked to the bridge |
| `shots` | shots this Klipper session sent that the bridge took |
| `kit_shots` | the bridge's own shot count |
| `last_shot_time` | when the last shot was taken (Unix time), or null |
| `last_shot_result` | `queued`, `refused`, `not-permitted`, `rate-limited`, `unavailable`, or the relay's error |
| `rate_limited` | shots not sent because they came too soon |

## Options

In `[oams_manager]`:

| Option | Default | Meaning |
|---|---|---|
| `cyberbrick:` | `True` | the kit support at all; `False` turns the kit off once when Klipper starts and registers no command and no status |
| `cyberbrick_min_interval:` | `0.5` | the shortest gap between shots, in seconds (0.5 or more) |
| `cyberbrick_timelapse_frames:` | `False` | moonraker-timelapse frames fire the kit |

## When it is not there

No kit, no relay, a bridge before firmware 40, lab firmware, or the bridge
not connected: OpenAMS sends nothing, pauses nothing and logs nothing.
`CYBERBRICK_SHOT` says why it did not fire; `M1004` stays silent. The log
says when the kit comes and when it goes, once each.

From a shell, `cortex-bridge.py kit` shows the kit's status,
`cortex-bridge.py kit shot` fires it, and `cortex-bridge.py kit on` or `off`
turns it on or off.
