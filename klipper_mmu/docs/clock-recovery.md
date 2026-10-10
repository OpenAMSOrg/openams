<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# MCU clock discontinuity and recovery

The scheduler clock adapter services the 32-bit hardware counter every 1 ms.
Ordinary counter wrap is safe: the elapsed unsigned tick delta is small, so the
clock advances monotonically across `UINT32_MAX` and the maintenance timer is
scheduled again from the fresh raw count. A delayed callback also remains
interpretable when the missed interval is below `2^31` ticks. This is a
modular-time limit, not a recommended scheduler delay; a long service stall can
already make motion or sensor data unsafe before the clock reaches it.

For an observed unsigned delta of `2^31` ticks or more, a single counter value cannot distinguish elapsed
time from a backward/discontinuous counter. The adapter fails closed: it
latches `FAULTED`, invokes Klipper shutdown, and does not reschedule maintenance
or accept later timestamps. A stall spanning an entire counter cycle cannot be
identified from that counter alone; timely maintenance remains a requirement.
The next diagnostic query can report the latched
fault. Clearing Klipper shutdown does not restore this adapter or its timer.

This behavior intentionally requires a fresh MCU startup. Reinitializing at
zero while retaining the attached controller, dated sensors, trajectories, or
host-visible operation state would mix two local time epochs; it could make
stale work appear current. The clock module has no stopped-state rearm protocol
that reconciles those consumers, so it does not clear a fault automatically
or resume motion. Recovery of the MCU clock requires resetting the MCU and
re-establishing the complete startup epoch.

The `tests/klipper_clock` adapter test dispatches the production maintenance
callback after a maximum legal interval that crosses raw wrap, checks a
diagnostic query and a subsequent deadline conversion, then verifies that an
exactly half-range interval faults and that later deadline commands preserve
their outputs while returning `KM_ERR_FAULTED`.
