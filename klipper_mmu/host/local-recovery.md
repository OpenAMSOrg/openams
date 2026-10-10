# Retired local terminal recovery

After a HOST lease or connection is lost, a standalone PRELOAD or UNPRELOAD
request may remain unresolved even though its terminal is retained on the MCU.
With the opt-in managed OpenAMS unit, run
`KLIPPER_MMU_RECOVER_LOCAL MMU=<name>` explicitly. The command requires a fresh
capture of the same MCU and old HOST session, the sole exact pending local
request, and the matching queue head. It records that terminal before sending
its exact ACK. If the ACK reply is uncertain, run the same command again to
retry only the committed local terminal's ACK.

This command does not clear firmware faults, create a new lease, settle an
OpenAMS LOAD/UNLOAD waiter, enable motors, or resume printing. It leaves the old
HOST session ambiguous. Physical path and session reconciliation remain
separate steps; a PRELOAD_COMPLETE can still leave a lane-local reservation.
An empty queue, a stale connection, or a different event is not proof of
completion and cannot make this command succeed.
