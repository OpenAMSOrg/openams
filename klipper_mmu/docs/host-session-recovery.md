# HOST session recovery

Transient query failures can revoke the host-side HOST lease without changing
Klipper's MCU connection generation. Explicit recovery therefore proves
freshness with the firmware snapshot query counter, not a connection-generation
change. When the host first retains an ambiguous HOST session, it records the
snapshot query high-water mark. Recovery requires a newly captured physical
snapshot whose query is strictly greater than that mark, from the same MCU
instance and HOST session. The existing queue-head, exact ACK, pending-request,
physical STOP, and fault checks remain part of the recovery proof.

The recovery commands only retire the specific host-side ambiguity they prove.
They do not send a heartbeat, restore a lease, or grant motion permission.
An actual firmware system or motion fault remains in force and requires its
separate fault-handling path; host-session recovery does not clear it. If a
query fails again, the MCU instance or session changes, work is pending or
uncertain, or the physical/event evidence no longer matches, recovery remains
unavailable.

The identified device type and lane count survive a transient observation
failure on the same connection. They describe the firmware dictionary, not
live readiness. Disconnect/shutdown invalidates this metadata. The recovery
regression continues through a fresh physical capture and an explicitly
confirmed new-session heartbeat, so successful record cleanup cannot conceal
a permanently blocked preparation gate. A lease remains a separate action.
