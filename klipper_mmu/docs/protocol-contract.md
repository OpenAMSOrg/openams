# Portable core contract

This describes the implemented native core, not a finished transport or hardware
adapter. All calls into a core and its trajectory must be serialized by the adapter;
ISRs/tasks cannot mutate the same state concurrently without a bounded critical
section. Outputs must remain disabled through initialization and error paths.

## Motion and timing

Motion commands specify a velocity or absolute point target with local limits.
The MCU computes its own acceleration, cruise, braking and next-edge deadlines.
It never requires host-generated movement chunks. At most four analytic phases
are traversed in an update. Integer absolute positions are bounded to ±10^12 steps,
velocity to 200,000 steps/s and acceleration/braking to 2,000,000 steps/s².
Double-precision arithmetic represents relative phase travel; MCU runtime cost
and achievable pulse rate require independent board qualification.

Commands have monotonic microsecond timestamps. Modular ordering is valid within
half the 32-bit timer horizon. The adapter must service edges independently of
its slower control update. The model-only update function does not emit pulses.
Service emits one actual edge at the current time, never a burst of backdated
pulses. Whole physical position advances after the falling edge; a disrupted
pulse makes position uncertain. Late output, driver failure and emergency stop
latch the trajectory and disable outputs. Recovery requires a reconciled position
and disabled, fault-free driver; it never enables the driver.

Operation requests and recovery carry a coherent pair of local millisecond and
microsecond observations. This preserves fractional command time and the coarse
epoch through fine-counter wrap. Periodic service remains required while idle
or faulted; see the [operation time contract](operation-time.md) for initialization,
serialization, stale rejection and explicit recovery behavior.

Point targets inside stopping distance or opposite the current velocity are
rejected without altering the active plan. Gracefully stop before such a retarget.
Per-move limits do not overwrite configured limits. Timestamped limit updates
preserve ongoing velocity and apply new ramp limits.

## Identity, admission and events

Identity consists of client, nonzero 32-bit session, nonzero 32-bit request,
operation type and lane. Sessions and request numbers increase without wrapping;
use a newer session before request exhaustion. Active ownership prevents another
client/session taking over. A completed recent identity returns
`KM_REQ_ALREADY_COMPLETED`; query its terminal with `km_core_find_terminal`.
An active identical request returns `KM_REQ_ALREADY_IN_PROGRESS`. Neither restarts
motion. The eight-terminal history is bounded; older reused IDs remain stale.

The preferred `km_core_request_cancel_target` requires the complete owning
identity, including operation and lane; wire commands use this entry point.
Legacy entry points retain their client/session/request behavior. Zero and sessionless
cancels are rejected. Ownership and event reservation remain until velocity is
zero and all physical pulse endings are complete. Cancellation during an unload
or calibration always produces that operation's cancelled result, including when
success was being settled. Occupancy remains conservative after interruption.

Admitted operations reserve event capacity in the 16-entry ring. Load reserves
both its completion and a subsequent autonomous-follow stop/fault. Queue pressure
rejects new work before output. No push operation evicts an unacknowledged event.
Transports peek/retransmit, then acknowledge the exact head sequence. `pop` is only
a synchronous convenience. Sequence zero is excluded. An unsolicited fault at a
full queue is separately latched; new work/recovery is blocked until it is queued.
Repeated observation of a latched fault does not create repeated terminal events.

The `protocol_wire` helper maps legacy OpenAMS action/result values only.
Its legacy encoding rejects internal request IDs above 255 rather than silently
truncating them. The full-width [firmware command boundary](../mcu/commands.md)
and `host/requests.py` matcher instead retain complete identities and explicitly
associate them with the existing OpenAMS compatibility token.
Telemetry has no generation-tagged terminal identity and cannot complete a wait.

## Wire enumerations

Firmware exports each value a host reads as a number as a Klipper
`DECL_ENUMERATION` in `mcu/enumerations.c`: operation state, stage,
operation type, client, admission, fault, event type, event flag bits, OpenAMS
result code, administrative command kind and result, transport binding,
follower state, direction, diagnostic startup state and `km_result_t`. Each
entry name is its C enumerator suffix (`km_op_state LOADED` is
`KM_OP_STATE_LOADED`), so an entry cannot name one constant and carry
another. The `km_*` dictionary names match no message parameter or `_name`
suffix, so Klipper still parses and encodes every field as an integer; the
export adds dictionary entries and changes no message format.

`host/wire.py` is the host's one copy of the message formats and these
values. At connect the host requires every name it knows to be declared with
the same value; firmware may declare more, and unknown values stay rejected
where they appear. A missing or different entry leaves the MMU unavailable
while the printer continues ([optional connect](optional-connect.md)). A
dictionary that declares none of them comes from an image built before the
export. The version check already limits those to protocol 1 and integration
1, whose values have only been appended since the first release, so the host
reads them with its own table. `KLIPPER_MMU_PROTOCOL_VERSION` stays 1 because
the export is additive. Never renumber a value: add a new name in firmware and
the host table together; `tests/test_wire_enumerations.py` requires the
exported set to equal the host table.

## Reconnection observations

`km_core_get_snapshot` returns a read-only copy. It now exposes the full active
operation identity, whether its terminal is still pending, path ownership and
occupancy separately, pulse-position uncertainty, per-client admitted session
and request counters, and queued-event head/count plus any pending unsolicited
fault. It neither acknowledges events nor clears faults, refreshes the host
lease, changes request counters or resumes motion. Snapshot time is the last
accepted controller time; the transport must separately timestamp acquisition.
The adapter must serialize snapshot capture with updates and physical servicing.

Load completion can coexist with continuing ownership and automatic following.
Thus `terminal_pending=false` is not a released path, and an occupied but unowned
path after stop/recovery is not an empty machine. Owner/occupant lane fields are
meaningful only when their corresponding flags are true. An occupied path can report
`occupant_lane=255` (`KM_LANE_UNKNOWN`) when local presence is observed without
an established source. Bay switches alone cannot resolve that source; admission
remains blocked until explicit path reconciliation. A later absence or invalid
reading does not clear retained occupancy. Active identity fields
are not a terminal record; after ordinary settlement some retained identity
numbers remain, while `active_client=NONE` and `lane_active=false`. The retained
terminal API or an actual terminal event establishes a request's result.

Position uncertainty reports disrupted pulse accounting; its absence is not
physical homing or independent filament-position measurement. `motor_ready`
exposes the core's global admission flag, not proof that every configured lane
or optional backend is initialized. Existing driver, sensor and per-operation
admission checks still apply.

The host may use admitted counters to avoid reusing an old identity, but must
first reconcile an outstanding request and path ownership. Sessions cannot
increase while another session still owns the path. Counters at `UINT32_MAX`
must not wrap. Event head sequence is only a delivery cursor; acknowledge the
exact event after processing it, and query retained terminal history by full
identity if an acknowledgement has already removed it from the queue. History
eviction is an unknown/stale result, never inferred success.

These observations are implemented in the portable core and callable Klipper
command/response boundary. The optional H723 diagnostic path now attaches with
a one-shot MCU boot identity, while installed OpenAMS session and physical-path
reconciliation remain pending. The host records same/changed attached instances
within one host-object lifetime, but cannot infer full ownership from that
comparison. RAM counters and event sequences do not identify a firmware boot.
Replies queued from an earlier
connection must not settle a new connection's waiter. The existing eight-bit
OpenAMS generation must be explicitly mapped to a full firmware identity rather
than truncated or used as a session number. Snapshot queries increase for the
entire controller instance, including reconnects; replaying an existing query
returns its original capture. `km_transport_query` reports the high-water mark
but cannot establish controller identity by itself.

## Lifecycle and recovery

Load feeds to hub, then to the configured physical arrival switch. It transitions
locally into forward following and emits load completion while retaining path
ownership. A failed feed is retried locally, up to three attempts within 240 s,
with no intermediate event ([LOAD retries](fps-arrival.md#load-retries)).
Buffer pressure alone is never arrival evidence. The host will run
printer capture/prime macros and await queued printer motion at this handoff.

Explicit following can change direction through a new owned intent. Before
printer retraction/cutting, the host will select reverse following; after printer
motion completes it requests autonomous unload. Unload retracts until hub and
arrival signals clear, then brakes and stops. Powered rewind is optional and is
not engaged against remaining forward feeder motion. Its outputs stop immediately
on cancellation/fault. Ordinary unload does not wait for the spool's bay sensor
to clear. Extra profile-specific parking geometry and full eject are pending.

Calibration requires a configured valid encoder and physical hub/arrival sensing.
It measures travel to arrival, reverses locally, waits for clearance and settles.
Probe and retract phases have separate deadlines and progress supervision. This
is not yet a complete persisted/homed geometry calibration workflow.

The local host watchdog requires heartbeat/ready updates within the configured
lease while a host owns motion. Sensor acquisition has separate freshness limits.
Communication loss, driver failure, invalid configured sensors and emergency
stop disable outputs. Reconnection only refreshes communication. Explicit recovery
reconciles known position/path after the underlying fault is resolved and keeps
outputs off. Runtime state never automatically authorizes resuming a print.

## Sensors and optional control

Frontends publish acquisition time and validity through timestamped input APIs.
Configured invalid/stale sensors fault active operations; absent optional sensors
are not fabricated. Invalid buffer feedback suppresses the follower command
immediately, then latches a persistent signal fault after the debounce period.
Analog profiles use a proportional speed law around the buffer midpoint with
configured gain/deadband; switched profiles retain hysteresis. Both feed velocity
intent into the same local acceleration planner. The portable follower has an
optional current-setpoint output, but the shipped TMC feeder fixes run/hold
current at initialization and does not advertise runtime current changes. Its
readback verifies those fixed settings; an unexpected change faults the feeder.
The operation core also uses the configured run current as its ceiling, so
encoder-based stall current escalation is not available in the current firmware.
Adding it requires a bounded current-change/confirmation lifecycle and an
explicit limit. Driver register settings are not current measurements.

DiffA/DiffB uses explicit monotonic paired calibration along the user's two
half-ranges around 1.65 V. Physical channel order is configurable. Synthetic tables
in tests are never installed as defaults. Paired-path and configured voltage
bounds reject inconsistent samples; neither establishes universal open/short
coverage. Valid endpoints may legitimately be 0 V or 3.3 V.

Measured-current rewind control is optional. Each sample carries its acquisition
time; cached readings cannot renew freshness or integrate repeatedly. PI gains
use elapsed sample time with bounded integral/output. Over-limit readings cut
output during qualification; severe readings trip immediately. Faults remain
latched. A zero command never becomes a minimum nonzero PWM command. Actual bridge
reversal dead time and current acquisition belong to the physical backend.
