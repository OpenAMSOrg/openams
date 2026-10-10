# Occupied HOST session preparation

`KlipperMMU.prepare_occupied_host_session(expected_bays)` is an explicit
reactor-thread entry point for an installed adapter. It reads one coherent
physical status and sensor capture. The reconciliation gate requires a known
occupied lane in LOADED and STOPPED state, with no active owner, operation,
events, fault, motion, or position uncertainty. A fresh HOST session is chosen
from the captured counter and confirmed by an exact OK heartbeat reply.

The result contains `prepared` (`PreparedHostSession`) and the physically
proven `lane`. The prepared helpers can enter the existing lease flow; neither
the heartbeat nor the lane proof grants motion permission. Firmware admission
must still be checked when an operation starts.

Ineligible captures send no heartbeat. Once a heartbeat is attempted, timeout,
rejection, or connection change leaves the observer's ambiguity latch set and
prevents an implicit retry. Recovery must reconcile that outcome explicitly.

The opt-in managed OpenAMS unit uses this path only when its fresh physical
view identifies one occupied lane. It compares that lane with the independent
bootstrap capture before starting the existing lease and command binding.
An empty physical view keeps the cold-session path. An unknown or conflicting
occupied path cannot bind motion commands. This supports a completed,
quiescent load across a host adapter restart; it does not clear a watchdog
fault, resume interrupted motion, or settle old waiters or events.
