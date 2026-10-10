# Optional MMU connect handling

## Problem

`KlipperMMU._connect` runs as a `klippy:connect` handler. Pinned upstream
`Printer._connect` (`klippy/klippy.py`) sorts connect-time exceptions into
kinds. `config_error` and `pins.error` produce the configuration message plus
the RESTART hint. `msgproto.error` produces "Protocol error", and `mcu.error`
produces "MCU error during connect". Any other exception, including
`gcode.CommandError`, produces "Internal error during connect". Each of these
kinds halts printer startup.

The MMU raised `command_error` when its firmware constants were incompatible
and when its first transport observation failed. So a wrong firmware build was
shown as an internal error. A lost reply from this optional device also halted
the whole printer.

## Behavior

| Connect-time condition | Result |
|---|---|
| File output | State `file_output`; nothing is sent. |
| Protocol or integration version is not the integer 1 | `printer.config_error`; `last_error` holds the same message. |
| `KLIPPER_MMU_ACTUATION` or the optional host-epoch constant is not the integer 0 or 1 | `printer.config_error`; `last_error` holds the same message. |
| A `km_*` dictionary enumeration the host knows is missing or has a different value ([wire enumerations](protocol-contract.md#wire-enumerations)) | Connect completes and sends nothing, not even a host epoch. The MMU is `unavailable`, `connected=False` and inactive, so `KLIPPER_MMU_QUERY` only reports. `last_error` names each entry and a warning is logged. Flash matching firmware, then restart. |
| The dictionary declares none of those enumerations (image built before they were exported) | Accepted as protocol 1 with the host's values; an info line is logged. |
| Configuration callback gates (`diagnostic_startup`, calibration, `diff_probe`) | Unchanged `printer.config_error` raised from the MCU configuration callback. |
| First observation raises `command_error` (timeout, wrapper error, malformed or unechoed reply, diagnostic query failure, missing query command in the dictionary) | Connect completes. The MMU is `unavailable`, `connected=False`, and has no instance, last query, startup state or snapshot. `last_error` keeps the message and a warning is logged. |
| An invalidation (disconnect/shutdown) during that observation | The newer generation's state is kept. The stale error does not overwrite it. |

The handler catches only `printer.command_error`. `_observe` raises that type
for every observation outcome after it calls `_fail`. Any other exception
still reaches the upstream wrapper. The handler never sets `connected` or a
state itself. It calls `_fail` with the generation it captured before
observing, so the stale-generation guard in `_fail` still applies. Before
observing, `_invalidate` has already bumped the generation, cleared the
snapshot and any prepared host session, and kept the host recovery record.
`motion_available` stays false.

`_active` stays true after a failed first observation. Recovery is always an
explicit action:

- `KLIPPER_MMU_QUERY` observes again. A failure is still reported as a
  command error. Success restores `connected`, state, instance and the
  startup diagnostics.
- Identified firmware composition remains valid within the same MCU
  connection. A failed observation clears live state and any prepared session,
  but retains this dictionary metadata. Explicit recovery and a fresh physical
  capture can therefore prepare a new session after communication recovers.
  Disconnect/shutdown clears the composition; a new connect must identify it
  again. No query automatically restores a lease or motion permission.

A dictionary that lacks `km_transport_query` is classified by `_observe` as an
observation failure, so it gives an unavailable MMU and not a configuration
error. The message names the missing command.

## Verification

`tests/klipper_host/run_connection.py` runs the pinned upstream
`Printer._connect` and `_set_state`, extracted by AST from
`klippy/klippy.py` and hash-pinned. They run against the existing
fixture, which includes the real `MCUConfigHelper`, `CommandQueryWrapper`,
`SerialRetryCommand` and `MessageParser`. The runner checks these cases:

- Bad version, actuation and host-epoch constants end in the upstream configuration error
  state (the message, then the RESTART hint), never "Internal error". No
  observation is sent.
- Transport timeouts, transport wrapper errors, and diagnostic query timeouts
  and errors each let the printer reach its ready state, with the MMU
  unavailable and `last_error` set. A manual query while the fault persists
  raises a command error. A manual query after the fault clears recovers,
  using a fresh challenge.
- A disconnect or shutdown during the connect-time observation leaves the
  newer `disconnected` state with no stale error, and a reconnect succeeds.
- A reconnect while an older send is still in flight waits for the MCU
  mutex instead of failing. The old send unwinds as stale and cannot
  downgrade the new generation; the reconnect then observes it. This holds
  for query, snapshot and diagnostic startup paths.
