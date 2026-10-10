<!-- SPDX-License-Identifier: GPL-3.0-or-later -->
# Operation time contract

Operation requests and explicit recovery take `km_core_timepoint_t { ms, us }`.
Both fields describe one sampled MCU-local command dispatch instant. For a
conceptual unwrapped microsecond total `T`, `ms = floor(T / 1000) mod 2^32` and
`us = T mod 2^32`. Dividing the wrapped `us` field by 1000 is incorrect after
about 71 minutes. The pair records the adapter's observation, not a guarantee
of exact physical acquisition or output time.

The adapter must serialize requests, core updates and trajectory service. Obtain
the pair coherently inside that serialized dispatch boundary; do not forward a
host clock or reuse an earlier reception timestamp after newer local service.
Exact `us` reaches motion retargeting, braking and immediate/delayed rewinder
direction changes. Local `ms` supplies operation watchdogs and event timestamps.

## Validation and retained time

A pair is coherent when unsigned `us - ms * 1000` is below 1000. The core compares
its coarse elapsed milliseconds and fine remainder with its last accepted pair,
requiring `0 <= elapsed_us < 2^31`. This detects a whole fine-counter wrap gap
that comparing low microseconds alone would miss. A newer, valid non-faulted
trajectory service time is a separate lower bound. Missing physical output
deadlines still faults; accepting a new timestamp cannot erase overdue edges.

The controller clock has its own validity flag. A first coherent observation
may occur at any epoch, including zero. It does not depend on whether motion has
initialized its clock: a first follow command or an update while faulted also
establishes controller time.

New admissions commit time after validation and event-capacity reservation,
before driver work. Cancellation commits after identity and state checks;
successful explicit recovery commits after the position/trajectory checks.
Periodic updates commit valid time. Rejected work does not advance this clock.
Ordinary duplicate admission replies inspect retained work without renewing it.
Cancellation during braking may replace a pending success with cancellation and
retains its validated timestamp, while ownership remains until settlement.

Stale/incoherent requests return `KM_REQ_REJECTED_STALE` before changing identity,
event reservations, ownership or output. Invalid recovery returns
`KM_ERR_INVALID_PARAM`. An invalid/backward periodic update immediately faults
and disables outputs before further actuator work; its event uses the last
validated coarse time, or zero if no coherent observation has yet been accepted.

## Service and recovery

The adapter must service the core with individual intervals strictly below
`2^31` microseconds even while idle or faulted. Normal motion/feedback deadlines
are much shorter and still apply. The modular contract cannot reconstruct an
arbitrarily long unobserved interval from two wrapping counters.

Healthy, fully stopped trajectories advance their clock through the normal model
update without enabling outputs or resetting position. A faulted trajectory
remains disabled and uncertain while periodic service maintains controller time.
Explicit recovery after physical reconciliation may then establish a new
trajectory epoch; the obsolete faulted trajectory timestamp does not block it.
Recovery retains the pending-event, driver-fault, disabled-output and known
position/path checks. It never enables a motor or automatically resumes work.

## Sensors and API migration

Physical sensor frontends publish explicit local millisecond acquisition time
through the `_at` input APIs; stale/invalid samples remain meaningful. Convenience
sensor/encoder setters and the motor-not-ready fault path use the retained coarse
controller observation. They do not sample a hardware clock and are suitable only
for serialized synchronous use at that observation. Leases and events also use
local time. No sensor hardware accuracy is inferred from the timestamp type.

The unreleased request API previously took only `now_ms`; recovery took `now_us`.
Their last parameter is now the typed pair. `km_core_update` keeps its two scalar
arguments but requires the same coherent pair. Cancellation names and arities
are retained; sessionless cancellation remains unsupported. No wrapper invents
fine time from coarse time. Host and MCU transport integration remains separate.
