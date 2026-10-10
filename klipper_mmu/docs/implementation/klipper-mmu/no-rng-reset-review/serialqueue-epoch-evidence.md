# Host-provisioned reset epoch: pinned Klipper transport review

Date: 2026-09-23  
Klipper source: `build/release-1998024-20260923/klipper`

## Finding

For Klipper's ordinary disconnect/reconnect path, serialqueue does not carry its queued or retransmitted application command blocks into the next session. A host-generated epoch is therefore a viable additive alternative for no-RNG targets if it is generated and sent dynamically after the new session's identify/config exchange, rather than baked into the static init command list. This conclusion covers Klipper-managed reconnect, not arbitrary bytes injected onto an active link or a target reset that is invisible to Klipper and leaves the serial session operating as if nothing happened.

The current product implementation is not this design: `host/__init__.py` registers the no-argument `km_diagnostic_start` as `is_init=True`, and the MCU ignores its args. The init command is replayed on matching-CRC host restart and after full config. Config CRC covers config commands only, not init commands (`klippy/mcu.py` `_finalize_config`, `_connect`). A token embedded in that list would not rotate safely for an MCU-only reset on the same helper.

## Queue and parser boundary

- Every serial session allocates a fresh `serialqueue` in `klippy/serialhdl.py::_start_session` (lines 86-104). `serialqueue.c::serialqueue_alloc` zeroes the structure, sets `send_seq=1`, `receive_seq=1`, and initializes empty ready/sent/receive/notify queues (lines 708-760).
- `SerialReader.disconnect()` calls `serialqueue_exit`, joins the background thread, clears the queue pointer, closes the device, and completes pending notifications with failure (`serialhdl.py:217-226`). The prior serialqueue's sent and ready messages are freed by `serialqueue_free` (`serialqueue.c:798-812`). Its retransmit thread therefore cannot resend the old identity-establishment block through a later serialqueue.
- On MCU reset, Klipper command parser state is also reset: `src/command.c`'s static `next_sequence` starts at `MESSAGE_DEST` (line 16), and a valid incoming frame must match it before dispatch; parser advances it only after accepting the frame (`src/command.c:282-306`). Klipper's sequence field is four bits (`MESSAGE_SEQ_MASK=0x0f`). New serialqueue starts at outbound sequence 1. `_start_session` first fetches identify data using `identify offset=...` queries before the MCU config helper runs (`serialhdl.py:69-105`).
- Init/config commands use `serial.send` and are not application-level retried by `SerialRetryCommand`; that retry wrapper is for response queries (`serialhdl.py:315-345`). Reliable delivery and command ordering are handled by serialqueue.
- The old 4-bit sequence alone would not be a durable reset discriminator. The key safety boundary is teardown: queued and unacknowledged frames are discarded when Klipper disconnects the old session; the replacement queue starts from empty and the identify/config phase completes before post-init callbacks execute.

## Lifecycle decision

Use a fresh host CSPRNG epoch for every MCU link/config lifecycle and send it from a dynamic `register_post_init_callback` callback. Pinned `MCUConfigHelper._connect()` calls post-init callbacks only after sending config/init commands, confirming `get_config`, and checking the config CRC (`klippy/mcu.py:1050-1080`). This gives a fresh identity when a reset MCU is configured. If only Klippy restarts while the MCU remains READY, its fresh epoch command must be ignored by the already-attached MCU; current one-shot startup behavior already makes start a no-op outside IDLE. The host then observes the existing MCU instance. Keep the transport observation challenge independent.

For a real reconnect with the same Python object, freshness requires that the dynamic callback run again for each call to `_connect`; a token generated once in `KlipperMMU.__init__` is not acceptable. The normal serial reconnect path is a new serialqueue and identify exchange. Pinned Klipper's MCU helper is registered on `klippy:connect`, not on every serial packet; an invisible MCU reset that does not lead to disconnect/reconnect is outside this handshake's guarantee and must cause a fail-closed link/session error if the transport can detect it.

This is adequate for Klipper-managed fault recovery and does not require target RNG. It is not a cryptographic authentication protocol: the token protects epoch freshness and stale command separation, not malicious host/link impersonation.

## Minimum protocol and tests

1. Replace the static `km_diagnostic_start` init command with a dynamic post-init startup command carrying a validated nonzero 64-bit epoch (prefer two uint32 wire words if Klipper command encoding constraints warrant it). MCU accepts it once only in IDLE/unbound, freezes it as controller `instance`, and rejects malformed/zero values. Preserve READY idempotence: later host restart cannot change a live identity.
2. Have the host retain the current observed instance only after a valid post-start response. Never reuse transport `challenge` as epoch.
3. Exercise the actual pinned `MCUConfigHelper` fixture for full-config (`is_config=0`), matching-CRC reconnect (`is_config=1`), post-init ordering, and repeated `_connect` on the same helper with a distinct generated epoch.
4. Add MCU/native lifecycle cases: old operational command, heartbeat, event peek/ACK all reject before attach and after reattach; reset to IDLE plus fresh epoch changes instance; repeated start while READY preserves instance; malformed and zero epoch fail closed.
5. Preserve existing host stale-generation, exact event-head ACK, reconnect/session reconciliation tests. Include a host-restart/MCU-still-READY case that sends but cannot apply the fresh host token.

## Persistence alternative

Pinned Klipper MCU core contains STM32 flash setup and flashing tools, but no application flash-write, EEPROM, or backup-register service surfaced by the source search. A target-owned monotonic reset counter would thus be a new board-family-specific persistence feature, with atomic write/journal, torn-write recovery, and endurance policy. It is not the smaller next slice than a post-init host epoch under Klipper's managed reconnect boundary.
