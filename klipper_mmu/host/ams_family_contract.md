# OpenAMS ams-family runtime contract

The sibling `openams/src/oams_runtime.py` is the current
OpenAMS effect executor. Its `Runtime.request(fps, action)` creates a reactor
completion and dispatches the pure `oams_state` reducer. The reducer emits
`StartLoad(unit, fps, gen)`, `StartUnload(unit, fps, gen)`,
`StartCalibrate(unit, kind, fps, gen)`, `CancelLoad(unit)`,
`SetFollower(unit, enable, direction)`, `ArmDeadline(fps, seconds)`,
`CancelDeadline(fps)`, `Pause(fps, reason)`, and `Settle(fps, result)`.
The runtime executes these through its configured OAMS unit, reactor, and
G-code objects. The driver must echo the exact `gen` on
`OpCompleted(fps, code, value, gen)`; the reducer ignores stale generations.
The `Runtime.request` return value settles on the reducer's `Settle` effect,
not on an outgoing start call or an accepted admission response.

`host.requests.Requests` pairs a full MCU identity with the FPS waiter and
OpenAMS generation token. `host.event_delivery.EventDelivery` retains an event
until its consumer accepts it, then builds the exact event ACK. The contract
tests compose these objects with the **actual sibling runtime**, rather than a
copy of its reducer. They cover successful load, failed load and retry, stale
identity after replacement, and the fact that start/admission do not settle a
waiter. The retained-event consumer checks the request completion after
`Runtime.dispatch`: that method catches reducer/effect errors and can return
normally while leaving the waiter pending. In that case the consumer raises,
preserving the event for retry without preparing its ACK.

An already settled waiter can have a different outcome, such as a host timeout.
The installed adapter must reconcile that outcome with the retained MCU result;
`completion.test()` alone does not prove this event caused the settlement.

Run the tests with:

```sh
OPENAMS_SOURCE=/path/to/openams \
  python3 -m unittest discover -s tests/klipper_ams_family_contract -v
```

The tests deliberately do not pin a SHA of a locally edited sibling checkout.
The caller supplies the repository under test through `OPENAMS_SOURCE`; record
its revision and worktree state alongside any release qualification result.

The device adapter is still needed to establish and reconcile the MCU
instance/session, translate OpenAMS `Start*` and follower effects into commands,
capture the connection generation before queuing callbacks, route retained
terminals through `Requests` to `Runtime.accept_terminal(...)`, and ACK
only after successful handling. It must also reconcile physical filament
ownership and uncertain operations across reconnects. A replacement request
while OpenAMS still considers a lane busy is rejected by its reducer; an
adapter cannot turn that rejection into a new active motion operation. These
tests exercise the callback contract but do not grant motion permission or
claim an installed printer integration.

`Runtime.request()` returns its completion to the G-code caller only after it
synchronously dispatches the `Start*` effect; the device's `start_*` method
receives a bay and generation but not that completion. `Runtime.dispatch()` also
catches reducer/effect exceptions and can return without settling. The public
`Runtime.accept_terminal()` hook, committed in the ams-family checkout as
`927a454` and covered there by `443a5c5`, now checks the active generation and
pending completion, dispatches `OpCompleted`, and reports success only when that
same completion settles with the matching outcome. The product-side
`openams_terminal_consumer.py` uses this hook without touching private runtime
state. `event_cycle.py` composes one explicit PEEK, consumer and ACK; it does not
schedule itself or install a device. Runout auto-reload has no G-code waiter and
needs a separate acceptance contract. The installed MMU device adapter remains
separate integration work; the hook and one-shot cycle do not enable motion.
