# EMU host composition

The host now recognizes the opt-in `emu` OpenAMS family as firmware device ID
7. Motion admission still requires an exact match between the configured family
and lane count and the attached firmware dictionary: EMU is ID 7 with five
lanes. A mismatched identity or topology remains diagnostic-only. The mapping
is additive; BoxTurtle (1), NightOwl (2), and Custom (3) retain their existing
checks.

EMU's AFC implementation uses a virtual hub pin for lane routing. Consequently,
the shared HUB input may remain clear while filament is staged locally or while
it is being fed. The host keeps the ordinary retained status/sensor capture and
session reconciliation path: per-lane BAY and LOAD readings must belong to the
same fresh capture, and a local PRELOAD reserves only its proven lane. An
unknown, stale, or contradictory LOAD reading blocks readiness; an inactive
shared HUB input alone does not prove the route empty or loaded.

A fresh all-clear capture can establish a cold session. A quiescent PRELOAD can
establish a lane-local session only when exactly one lane's BAY and LOAD sensors
agree. A physically occupied shared route still needs shared HUB or toolhead
evidence under the existing ownership rules; lane-local LOAD alone cannot
establish occupied-route ownership. The host deliberately refuses that case
when the EMU virtual-HUB arrangement leaves both shared signals clear, rather
than guessing the occupied state.

After reconciliation, LOAD, FOLLOW, UNLOAD, terminal event delivery, and exact
ACK use the shared HOST request/session and event-delivery services. There is no
EMU-specific controller or alternate session protocol. The five-lane saved
H723 EMU image publishes firmware ID 7, lane count 5, and enables the five LOAD
inputs; native image/runtime fixtures exercise those firmware contracts.

The focused host tests verify exact family/lane admission and refusal of wrong
ID or lane counts, plus five-lane virtual-hub projection including cold session,
lane-local preload, occupied-route refusal, empty state, and stale LOAD
rejection. Existing generic host
operation/event tests continue to cover HOST LOAD/UNLOAD and exact terminal ACK.
These are software tests; they do not establish physical EMU wiring or motion
qualification. AFC's `AFC_EMU` implementation is the source for the virtual-pin
behavior; the upstream `openams` capability catalogue has no
EMU hardware family. This mapping therefore describes the add-on's named EMU
profile and does not claim that upstream AMS family detection recognizes AFC
EMU as an AMS product.

Root integration passes all 62 focused host cases and the production EMU
hosted runtime against the retained five-lane dictionary and autoconf. Evidence:
`build/review-emu-host.log`, `build/review-emu-hosted-final.log`. The fixture
now waits for actual switch qualification and retains a separate diagnostic
query challenge for the hardware-identity and host-epoch startup paths.
