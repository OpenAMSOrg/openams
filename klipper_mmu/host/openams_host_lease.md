# HOST lease renewal

`HostLease` accepts a `PreparedHostSession` after cold-session reconciliation. The
Klipper reactor owner explicitly calls `renew()` while holding exclusive access
to the MCU command response channel. It has no timer or background retry.

The caller supplies the configured firmware `host_watchdog_ms` and a
`max_age_ms` strictly below it, allowing for transport and scheduling margin.
The helper never guesses either value. Motion checks stay false until a fresh,
exact HOST heartbeat reply is confirmed for the same controller instance,
session, and connection generation. Every motion check also calls a supplied
live board/actuation readiness predicate. This helper does not send START or
grant physical motion by itself.

Heartbeat timeout, rejection, connection change, or lease expiry latches the
lease unavailable and disconnects both `Requests` and `EventDelivery`. An
attempted renewal at or after the previous confirmed lease's deadline is
rejected before sending another heartbeat. The first renewal after preparation
is allowed without a previous local heartbeat timestamp. An
accepted reply dates the lease from before sending the heartbeat; a reply that
arrives at or after that deadline closes the lease even if firmware accepted
the command. This conservative timestamp accounts for transport delay. An
ambiguous heartbeat must be reconciled using a new coherent snapshot and a new
prepared session before another heartbeat is attempted. Existing in-flight
waiters remain unknown until terminal events or explicit recovery resolve them.
