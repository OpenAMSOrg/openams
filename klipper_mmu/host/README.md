# Host response boundary

`protocol.py` decodes parameters supplied by Klipper's existing message parser.
`mcu/messages.c` emits the corresponding typed messages with `sendf`. `wire.py`
holds the host's only copy of each message format and of the enumerated values
that firmware exports and connect compares. All three belong
to the optional add-on. There is no separate framing, checksum, binary parser or
transport library.

Admission, retained events and status are different response types. An accepted,
already-in-progress or already-completed admission cannot finish a host operation.
Completion requires the matching retained terminal result, processed before its
exact sequence is acknowledged. These codecs do not acknowledge anything or
complete OpenAMS waiters. A successful emitter return means a send was attempted,
not that Klipper delivered it; the caller must retain data for retry.

All identity-bearing responses include an opaque nonzero 64-bit controller
instance. Request identity retains client, 32-bit session, 32-bit request,
operation and lane. Invalid identities on rejected requests and unsolicited
faults are preserved; a decoder is not an admission or matching decision.
The optional H723 diagnostic startup now attaches the core using a one-shot
MCU boot identity. The host still needs reconnect reconciliation before it can
admit operations or deliver retained events. An instance must stay stable across
reconnects to a live MCU and change after an actual firmware restart; a timestamp
is not a substitute. An unqualified host-first-claim latch cannot ensure this:
replaying the old claim could restore the old identity. The earlier provider
requirements and command-guard counterexample are recorded in
`host-instance-preparation/decision.md`.

The caller captures one private status snapshot and its separate acquisition
time under controller serialization, then serves one part per host request from
that retained copy in task context. Bursting all five into upstream's 96-byte
serial TX buffer could drop responses. All five responses carry the same instance
and nonzero query ID. They separate identity,
motion, time/current, client sessions and client requests to fit Klipper's frame
limit at maximum integer widths. The host waits for every part, accepts identical
replays, rejects conflicting duplicates and ignores other instances/queries.
A completed snapshot is delivered once. Query IDs must not be reused for a
controller instance, including reconnects; retransmissions refer to the original
capture. The caller owns
query timeout and must discard callbacks from old connection generations.

Signed position is explicitly reconstructed from two unsigned 32-bit words;
velocity and buffer position each use one unsigned word with explicit host sign
conversion. This also handles the extreme negative 32-bit values that did not
round-trip through the pinned upstream C encoder/Python `%i` parser in review.
Validity/availability flags remain independent of numeric payloads. An old
measurement value with availability false remains unavailable. Retained control
time and transport acquisition time remain distinct. Status never synthesizes
terminal success, clears a fault or releases physical ownership.

## MCU exchanges

`command_transport.exchange` is the host's one request/reply loop. It
registers the reply formats, sends, waits once with a deadline, and
unregisters on every exit. Serial callbacks only copy parameters onto the
reactor. A reply handler runs only while its exchange is open and the
caller's `is_current` (generation, instance, session) still holds; it ignores
replies for another query, instance or request, and a `ValueError` it raises
fails the exchange. The command, event, terminal, sensor, physical-state,
ACK, path-reconciliation and snapshot reads all use it and keep their own
validation.

Every exchange runs under the observer's one `reactor.mutex()`. A concurrent
caller waits, bounded by the holder's deadlines, instead of failing. A
transaction holds it from its fresh capture through its last send; it is
released only while OpenAMS runtime code runs inside an event transaction.
See [connection.md](connection.md#generation-and-challenge).

Run the response checks with a read-only upstream Klipper checkout:

```sh
KLIPPER_SOURCE=/path/to/klipper timeout 60s python3 -m unittest discover \
  -s tests -p test_messages.py -v
```

They compile the production C emitters with sanitizers, capture their arguments,
and encode/frame/decode them using upstream `klippy/msgproto.py`. This checks
full widths, frame size, signed positions, missing/conflicting/stale fragments
and all 120 fragment orders. The separate registered-command checks and upstream
C-parser experiment now exercise MCU dispatch; see `../mcu/commands.md`. An
installed OpenAMS adapter, instance negotiation, event delivery and printer
handoffs remain integration work.

## Matching OpenAMS operation tokens

`requests.py` keeps the existing OpenAMS runtime's one-byte compatibility token
associated with a full firmware identity. It needs an already verified controller
instance, host session and reconciled request high-water mark. It is not a device
adapter, session negotiator or transport. The waiter key is the OpenAMS FPS path
name, so changing physical bays cannot leave a stale match for the same waiter.

On the reactor thread, settle any superseded runtime waiter, call `issue`, and
send the returned identity without truncation. Following consumes an identity
without creating a load/unload/calibration waiter. Request exhaustion raises;
it never wraps. `cancel_target` returns the pending operation's exact identity.
After load completion, disabling automatic following instead needs the retained
owner identity from reconciled firmware state.

Capture `generation` before queuing a response callback. Decode the reply, pass
that captured generation to `admission` or `event`, then pass the returned match
to `deliver`. The delivery callback receives `(waiter, code, value, token)` for
the existing runtime's `OpCompleted` event. Delivery checks the identity and
connection again, so callbacks already matched before replacement or reconnect
cannot finish a newer waiter. A callback exception preserves the match for retry;
the callback must not report failure after committing its result.

Only a matching rejection or operation-specific terminal settles a waiter.
Accepted, already-running and already-completed admissions require the terminal
event. Unmatched events still need the device's fault/ownership handling; a false
delivery result is not permission to discard or acknowledge them. Event sequence
acknowledgement remains the adapter's responsibility after processing succeeds.

Call `disconnect` as soon as a connection becomes invalid. `resume` accepts a
verified coherent snapshot from the same controller and refreshes request counters;
it does not complete pending operations, clear faults or authorize motion. A new
controller or newer host session leaves pending outcomes unknown for explicit
reconciliation. A fresh tracker must not replace this ownership check.

```sh
OPENAMS_SOURCE=/path/to/openams timeout 60s python3 \
  -m unittest discover -s tests -p test_requests.py -v
```

Seventeen checks cover full identities, reconnect callbacks, admission codes
against the production C helper, and actual OpenAMS runtime waiters. The runtime
test drives 257 loads to reproduce token reuse across different bays. Its device
is an intent-recording fixture. The separate exact-image H723 join now connects
the managed unit, OpenAMS FPS mirror, manager and runtime through LOAD/UNLOAD;
its peripherals and filament travel are simulated, and printer macros and
physical hardware remain unqualified.

The `openams` checkout supplies the identity-aware runtime
used by the tests below. Its `OpCompleted(fps, code, value, gen=token)` action
is the intended waiter boundary. The separate main `klipper_openams` checkout
still has a shared `action_status` field, and its hardware service publishes
`spool_loaded` when a command is sent. Neither of those main-checkout signals
proves the MCU finished a particular request; do not feed them retained
terminals. `event_delivery.py` checks a retained queue head and prepares its
exact ACK only after the full consumer boundary accepts the event. The optional
managed host extra uses this path. After lease loss, an explicit ACK-only retry
requires a new same-instance capture proving the old HOST session, a full
queue-head match, and a confirmed ACK reply. It does not restore motion or
settle other old-session waiters.

`decode_transport_state` and `decode_command_result` decode the command boundary's
administrative replies. Neither is an admission or terminal event. Match a fresh
transport challenge and local connection generation before using its instance or
snapshot high-water mark. Snapshot queries must increase across reconnections to
the same instance, not restart at one. The transport observation does not establish
a controller instance or authorize motion. See `../mcu/commands.md`.

The `KLIPPER_MMU_CONFIG_*` dictionary constants describe saved firmware choices
for powered rewind, encoder, analog buffer and measured current. They come from
the same choices used by `mcu/config.c`; they are not acquired measurements or
runtime capability/readiness evidence. In particular, they do not override
ACTUATION=0 or an unbound controller. See `../mcu/config.md`.
