# Verified follower handoff

`OpenAMSVerifiedFollowerBridge` passes controller follower state to an
OpenAMS runtime only after a separate caller has refreshed `OpenAMSDevice`.
The caller must retain `device.follower_observation()` **before** issuing a
follower command, retain the returned `FollowCommand`, and call `accept` after
a later physical capture. All steps run serialized on Klipper's reactor.

```python
before = device.follower_observation()
command = device.set_oams_follower(1, direction)
# A separately scheduled physical capture later calls device.refresh().
confirmed = verifier.accept(command, before, (device.oams_idx, bay), True)
```

`before` must be a valid, fresh controller observation; absent proof leaves the
runtime unchanged. The bridge compares controller instance, connection
generation, query order, command admission and configured device identity.
The pre-command capture must identify the same physical bay as the OpenAMS
unit supplied by the caller.
For enable it additionally requires an armed observation with the requested
direction and the exact reconciled HOST owner for the issued FOLLOW identity
and bay. Older admission results without a recorded direction cannot confirm
an enabled follower. For disable it
requires a disarmed observation and passes only a caller-supplied unit that
OpenAMS already tracks; the runtime rejects an untracked or mismatched unit.
A cancel may carry the original LOAD identity when following began after load.

The bridge does not schedule captures, ACK events, complete operation waiters,
or convert command admission to physical confirmation. It observes the MCU
follower controller state; it does not measure motor pulses or filament motion.
Calling `accept` with unknown or expired data returns false and preserves
runtime uncertainty.

The installed managed unit retains the pre-command observation and exact
admission. Its bounded service timer takes a fresh physical capture before
calling the verifier; an admission alone never updates OpenAMS follower state.
There is one follower motor per managed unit, so a newer follower command
supersedes an older unverified admission. A missing observation, stale session,
or expired pending command leaves the runtime unconfirmed.
