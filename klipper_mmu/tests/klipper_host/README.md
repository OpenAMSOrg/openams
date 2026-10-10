# Host observer and snapshot checks

Pure Python, synchronous review of the package host observer and its explicit snapshot command against pinned Klipper public APIs. One suite runs 39 methods: 18 observer groups and 21 snapshot groups, including 120 part-order permutations. This command does not install anything, open a serial port, start a service, or touch hardware. `KLIPPER_MMU_SNAPSHOT` is explicit; the fixture does not poll in the background.

From the package root:

```
timeout 60s python3 tests/klipper_host/run_connection.py --upstream PATH_TO_KLIPPER --dictionary PATH_TO_DICTIONARY_JSON --results PATH_TO_NEW_RESULTS_DIR
```

`--package` is optional. It defaults to the package root, two directories above this test file (`Path(__file__).resolve().parents[2]`). Pass `--package PATH_TO_PACKAGE` to override that default.

## Inputs

- `--upstream` (required). Klipper tree whose `klippy/mcu.py`, `klippy/serialhdl.py`, and `klippy/msgproto.py` match the pinned APIs of commit `ce7002bedf37e938bb483572949f3703ac6476cb`. These exact SHA256 values are required before any upstream file is loaded or compiled:
  - `klippy/mcu.py`: `36c47e1f910abfdd1670b323af752fa4a44afdf8ea21a936664f067b1e35c23b`
  - `klippy/serialhdl.py`: `1502e97ae0f941b79b3209f2bf518ead330e30ca8cfbbdf5fa7f6b9858575133`
  - `klippy/msgproto.py`: `c205bb7d70d968b5fbe8bbb86247a125994eb5d7acde16660afb2471672d6278`
  A mismatch exits nonzero and those sources are not loaded. The same pinned `klippy/mcu.py` supplies the unchanged public classes `CommandQueryWrapper`, `CommandWrapper`, `AsyncResponseWrapper`, `MCUConfigHelper`, and `MCU`, plus `get_printer_mcu`. `SerialRetryCommand` and `MessageParser` come from the other two pinned files.
- `--dictionary` (required). The generated message-dictionary JSON from an MMU-enabled build, with a compatible transport query and response and the version and actuation constants this observer checks. The real `MessageParser` consumes it. An arbitrary unrelated Klipper dictionary is not sufficient.
- `--results` (required). A directory that does not already exist. The script creates it and writes `tests.log` and `results.json`. An existing directory is left untouched.
- Package files under `--package`: `host/__init__.py` is the extra under test. `host/protocol.py` is hashed into the report. `results.json` records the three accepted upstream hashes, both package hashes, the dictionary hash, the test outcome, and the fixture limitations.

The runner is in-process `unittest` with one suite of `HostChecks` and `SnapshotChecks`. `results.json` records `tests` from that run (39 methods). A behavioral failure exits nonzero. This script starts no subprocess and needs no external compiler. Time moves only when the modeled reactor is advanced. The script does not sleep or arm a real timer. The snapshot deadline is five seconds on that modeled clock.

## Eighteen observer groups

1. `test_actual_connect_order_crc_and_cached_status` — first connect orders move-queue readiness before the transport query, caches status, keeps CRC and config commands stable across a status read and a query, and issues challenges 1 then 2.
2. `test_all_observed_states_and_full_widths` — unbound, attached, shutdown, and clock_unavailable, including the full 64-bit instance width and last-query width, never report motion as available.
3. `test_file_output_is_not_live` — debug file output is not a live connection and sends no observation.
4. `test_versions_and_actuation_validation` — missing or wrong protocol version, wrong integration version, and actuation values 7, missing, true, and the string `0` raise before any observation.
5. `test_actuation_metadata_never_authorizes_motion` — firmware actuation metadata of integer 1 is reported and still leaves motion unavailable.
6. `test_duplicate_mcu_refused_before_registration` — a second observer on the same MCU raises before events or gcode commands change.
7. `test_separate_mcus_allowed` — observers on different MCUs stay distinct.
8. `test_wrong_challenge_and_inconsistent_state_invalidate` — a mismatched challenge or an inconsistent state invalidates the cached instance and last query and records an error.
9. `test_timeout_and_wrapper_error` — an omitted response or a synthetic transport failure surfaces as a command error, clears the instance, records an error, and stays within the retry bound of 7 observations.
10. `test_disconnect_and_shutdown_during_send` — disconnect or shutdown during send drops the connection and clears the instance.
11. `test_reentrant_query_rejected_before_send` — a re-entrant query is rejected and does not add a second send.
12. `test_reconnect_preserves_challenge_counter_and_retained_report` — reconnect after disconnect does not repeat config commands, continues the challenge counter at 2, and keeps last query `0xfffffffe`.
13. `test_exhaustion_does_not_send_or_wrap` — after challenge `0xffffffff`, the next query raises without another send and clears the instance.
14. `test_old_send_cannot_downgrade_new_generation` — a send still in flight cannot downgrade status after a failed reconnect moves the generation forward.
15. `test_reconnect_uses_current_dictionary` — after a synthetic dictionary change, reconnect observes through the current parser and reports unbound.
16. `test_lookup_failure_is_reported_and_explicit_retry_can_recover` — a dictionary without the transport query fails connect, records the error, and sends nothing; restoring the parser lets an explicit query recover.
17. `test_inflight_invalidation_releases_guard_after_unwind` — disconnect or shutdown while a send is pending invalidates the generation; after the outstanding call unwinds, callbacks are clear, reconnect succeeds, and challenges continue 1 through 4.
18. `test_busy_reconnect_recovers_after_original_send_unwinds` — a reconnect attempted while the original send is still pending is rejected; after that call unwinds, callbacks are clear and a later query recovers the connection with challenges 1 through 3.

## Twenty-one snapshot groups

`SnapshotReactor` and `SnapshotSerial` model completion, callback order, and the endpoint. They are not a hardware concurrency test. Command, query, and response wrappers stay the pinned upstream classes. Allowed sends are `km_transport_query` and `km_snapshot_part`.

1. `test_all_part_orders_and_cached_copy` — 120 part orders assemble one cached snapshot and a later status read does not resend.
2. `test_inactive_file_output_and_unbound_do_not_request_parts` — inactive, file output, unbound, shutdown, and clock-unavailable send no parts.
3. `test_incomplete_uses_one_timeout_then_can_recover` — one five-second timeout, then a later complete snapshot uses a newer query id.
4. `test_wrong_identity_or_query_never_assembles` — a mismatched instance or query does not publish.
5. `test_duplicate_and_conflicting_fragments` — identical duplicate parts can succeed; conflicting duplicates publish nothing.
6. `test_matching_error_with_changed_instance_is_immediate` — matching nonzero command results finish without the five-second wait.
7. `test_admin_ok_does_not_complete_snapshot` — command result 0 does not complete the snapshot.
8. `test_highwater_and_exhaustion_survive_reconnect` — full-width high-water survives reconnect; exhaustion sends no further part.
9. `test_registration_and_send_failure_cleanup` — partial registration, missing lookup, or a failed send cleans up and can recover.
10. `test_disconnect_and_shutdown_during_pending_snapshot` — disconnect or shutdown while pending clears the snapshot; a later read can succeed.
11. `test_reentrant_query_and_snapshot_do_not_send` — re-entrant query and snapshot add no send.
12. `test_delayed_old_callbacks_cannot_replace_new_snapshot` — an older generation cannot replace the new snapshot.
13. `test_malformed_matching_part_completes_with_error` — a matching part with a bad field fails the snapshot.
14. `test_explicit_transport_observation_clears_snapshot` — an explicit transport query drops the cached snapshot.
15. `test_unrelated_rejection_is_ignored_before_semantic_decode` — a different command or query is ignored.
16. `test_callback_queued_before_send_error_is_closed_on_unwind` — a callback queued before a send error does not complete the closed transaction.
17. `test_deadline_late_parts_never_publish` — parts after the deadline never publish.
18. `test_decoder_assembly_happens_on_reactor` — assembly runs on the modeled reactor phase.
19. `test_busy_reconnect_snapshot_recovers_after_unwind` — a reconnect during a pending snapshot is rejected; after unwind a new snapshot succeeds.
20. `test_matching_malformed_admin_reply_remains_strict` — matching result 255 publishes nothing.
21. `test_boolean_matching_fields_are_ignored` — a boolean query field on a command result is ignored.

## Limits

These checks execute actual upstream public lookup, command encoders, the message decoder, retry, command and response wrappers, and the first-connection config helper. Endpoint, reactor, and printer objects in the fixture only model that glue. Selected upstream classes and methods are compiled unchanged from their AST nodes. `SnapshotReactor` and `SnapshotSerial` are modeled glue; phase checks are not real concurrent hardware tests.

The normal first connection follows config ordering. A subsequent reconnect is observer-only on the model and does not rerun a finalized `MCUConfigHelper`. A changed dictionary is synthetic. There is no serial port, service, installation, or hardware. The run is not proof of entropy, source linking, OpenAMS lifecycle, motion readiness, boot initialization, startup freshness, hardware behavior, or boot identity. If invalidation occurs while a send is pending, the query guard must be clear after that outstanding call unwinds so a later connect or query can recover.

The extra is read-only. It may send `km_transport_query` and, for an explicit snapshot, `km_snapshot_part`. It must not send heartbeat, motion, acknowledgement, or session commands, and it does not poll in the background. Actuation must be exactly the integer 0 or 1, and the query wrapper must be connection-scoped. Those requirements stay strict in this fixture.

Snapshot checks cover partial registration cleanup, one five-second timeout, full-width ids, high-water and exhaustion, background-to-reactor dispatch, no stale callback completion after error, matching versus unrelated command results, duplicate and conflicting parts, and generation recovery.

The modeled reactor rejects observer polling timers. No background job is started. This does not install anything.

## Diagnostic startup

`DiagnosticChecks` covers opt-in `diagnostic_startup` in this same runner. The first modeled connection uses the real `MCUConfigHelper`: `km_diagnostic_start` is sent once as an init command after `finalize_config` and before the confirmation `get_config`. A later observer-only connect does not rerun that helper. A matching-CRC case uses a fresh fixture presented as already configured. `km_diagnostic_query` and signed `km_diagnostic_state` go through the real query wrapper, `SerialRetryCommand`, and `MessageParser`. Boolean and other values `%c`/`%i` cannot preserve are checked on `_decode_startup` directly. A valid startup response that also carries extra metadata is accepted. Attached rows use a nonzero full-width instance and unbound rows use instance 0; the resulting state and instance are asserted. If `raw_send_wait_ack` raises, `SerialRetryCommand` leaves the `km_diagnostic_state` callback registered. The host still clears the query guard and invalidates the observation and snapshot. Timeout unregisters that callback before raising. The next explicit query recovers through the real wrapper, clears the retained callback, does not send another `km_diagnostic_start`, and continues increasing challenges. Disconnect or shutdown inside the in-flight diagnostic query invalidates immediately and leaves the query guard set. A re-entrant query before that event is rejected without a send. After invalidation a user query returns cached status and does not send. Reconnect advances the generation even while its observation is refused, with no send or challenge change. The stale reply cannot publish into that newer generation. After unwind the guard and callbacks are clear, data stays disconnected, and an observer connect plus an explicit query recover challenges 1 through 4. The MCU owns firmware latch idempotence. Motion stays unauthorized. No serial port, sleep, or hardware.
