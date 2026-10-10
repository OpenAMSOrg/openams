# HOST lease bounds and recovery

The firmware's configured HOST watchdog is `CONFIG_KLIPPER_MMU_HOST_LEASE`,
whose menu range is 1–60,000 ms. The firmware dictionary now publishes that
effective build value as `KLIPPER_MMU_HOST_LEASE_MS`. An `openams_motion` host
must configure `openams_host_watchdog_ms` to the exact advertised value and
`openams_lease_max_age_ms` to a positive, smaller interval. The host rejects a
missing, malformed, or incompatible firmware bound before sending the initial
lease heartbeat. Matching these values makes the host's local expiry agree
with the firmware's actual watchdog; the host max age remains the stricter
command-permission cutoff.

Lease renewal stays serialized with the ordinary MCU command channel. If that
channel is temporarily occupied by a physical query, the OpenAMS service may
defer renewal only while its last exact heartbeat is still within max age. Once
that age expires, motion permission closes. A heartbeat timeout or ambiguous
reply disconnects the lease and retains exact pending requests and ACK state;
the same lease cannot be retried as if the reply had not arrived.

`KLIPPER_MMU_RECOVER_SESSION` is a separate explicit reconciliation path. It
does not heartbeat or grant motion. A fresh, newer capture must identify the
same MCU instance and old HOST session, show diagnostic READY and live board
readiness, have no active operation, queued/retained events, pending requests,
uncertain position, or system/motion/follower faults, and prove an empty path,
one quiescent local PRELOAD, or one quiescent LOADED lane (as ADOPT leaves it).
The command only releases that clean old record. The normal session
preparation and exact heartbeat confirmation must run afterward before a new
lease can be established. A pending terminal/ACK or request waiter requires
its dedicated exact recovery path and blocks clean session release.

These are host and firmware-contract tests; they do not measure a physical USB
round-trip or hardware watchdog timing margin.

Older firmware dictionaries without `KLIPPER_MMU_HOST_LEASE_MS` remain usable
for observation, but cannot establish a managed motion lease. Rebuild matching
firmware before using that path; there is no guessed watchdog fallback. Root
passes 72 focused lease/recovery/managed-unit tests. The candidate portable
aggregate passes, and 56 actual Klipper connection tests cover the legacy
read-only dictionary. The rebuilt hosted-image release is a separate gate.
