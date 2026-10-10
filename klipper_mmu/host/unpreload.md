# Guarded host UNPRELOAD

`KLIPPER_MMU_UNPRELOAD MMU=<name> BAY=<zero-based lane>` starts an UNLOAD from a
freshly proven lane-local LOAD reservation in the current HOST session. It is a
separate Klipper-MMU command, not an OpenAMS unload waiter. Admission is not
completion; the exact retained UNLOAD terminal records the result.

`KLIPPER_MMU_UNPRELOAD MMU=<name>` reports its pending request or last recorded
result. `KLIPPER_MMU_UNPRELOAD MMU=<name> CANCEL=1` targets only that pending
UNPRELOAD; omit `BAY`. An accepted CANCEL still waits for the retained terminal.
Stale or mismatched replies and timeouts leave the request pending for explicit
reconciliation. Neither command invents a parking distance or proves the path
clear from admission alone.
After a lost HOST lease, use [retired local terminal recovery](local-recovery.md)
for an exact retained result; this leaves motion revoked.
