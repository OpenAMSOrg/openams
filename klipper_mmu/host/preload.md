# Guarded host PRELOAD

`KLIPPER_MMU_PRELOAD MMU=<name> BAY=<zero-based lane>` is registered only when
`openams_motion: True` configures the managed OpenAMS unit. It requires the same
current controller instance, reconciled HOST session, diagnostic readiness,
fresh fault-free physical capture, and confirmed unexpired lease as LOAD.
The command returns a START admission, not proof of filament position.

`KLIPPER_MMU_PRELOAD MMU=<name>` reports the pending request or its last
recorded result. The retained PRELOAD_COMPLETE, PRELOAD_CANCELLED, and
PRELOAD_FAILED events (13–15) settle only an exact full-identity PRELOAD match.
The event is acknowledged after that result is recorded. A second PRELOAD is
refused while one is unresolved. A lost connection retains the exact request
for reconciliation; it cannot be replaced with a fresh request by reconnecting.

`KLIPPER_MMU_PRELOAD MMU=<name> CANCEL=1` sends CANCEL for that exact pending
PRELOAD in the current HOST session; omit `BAY`. An accepted cancel reply means
the terminal is still pending. Query the command without `BAY` to inspect its
retained PRELOAD_CANCELLED or failure result. A stale connection, unmatched
reply, or timeout leaves the pending request for reconciliation.
After a lost HOST lease, use the separate
[retired local terminal recovery](local-recovery.md) command to inspect and
acknowledge an exact retained result without restoring motion.

This is a Klipper-MMU command, not an OpenAMS LOAD operation. OpenAMS currently
has no PRELOAD reducer action or terminal, so neither admission nor completion
is sent to an OpenAMS load waiter. Firmware owns motion and path occupancy;
this host command adds no parking-distance inference.
