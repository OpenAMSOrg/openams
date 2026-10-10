# Contracts index

**Version: v1 (2026-09-29).** C5 added: klipper-mmu's MCU wire is now a public
contract, with a mapping section onto C1 and C2 and a sync test that keeps its
enumeration tables equal to `klipper_mmu/host/wire.py`. All four original
contracts updated to reflect what landed; every TO ADD marker
resolved. One DISCREPANCY reported (C2 §5.9 `environment.source`). One OPEN
hardware question active (reader coverage). One FUTURE item deferred to D6
(`spool_hint`).

The OpenAMS stack has five interface contracts, each a versioned schema
document with example payloads that double as test fixtures. This mirrors
`mmu-display/docs/ARCHITECTURE.md` §5, the design baseline these contracts
must satisfy. Work packages (F1-F6, H1-H16, D1-D9, S1-S4) label the changes
that closed each gap.

| Contract | Title | Between | Status |
|---|---|---|---|
| **C1** | [`C1_firmware_host.md`](C1_firmware_host.md) | OpenAMS units ↔ `openams` host (`src/oams.py` et al.) | public, wire-level; **v1** — the commands, responses and constants the host uses |
| **C2** | [`C2_status.md`](C2_status.md) | `openams` host → UIs (status objects: `oams_manager`, `openams_ui <unit>`, webhooks) | **v1 (2026-09-29)** — all gaps resolved; DISCREPANCY in §5.9 (`environment.source`); §4.3/§4.4.1 name the family registry (26 families); §5.6/§6.2 carry a klipper-mmu unit's own ops |
| **C3** | [`C3_commands.md`](C3_commands.md) | UIs → `openams` host (G-code commands) | **v1 (2026-09-29)** — all gaps resolved; coverage note added to `OAMS_RFID_SCAN`; §2 publishes `OAMS_PRELOAD`/`OAMS_UNPRELOAD`/`OAMS_EJECT`/`OAMS_HOME` |
| **C4** | [`C4_spool_linking.md`](C4_spool_linking.md) | Moonraker `openams_spoolman` component ↔ host ↔ Spoolman | **v1 (2026-09-28)** — all gaps resolved; `spool_hint` deferred to D6 |
| **C5** | [`C5_klipper_mmu_wire.md`](C5_klipper_mmu_wire.md) | Klipper-MMU MCU firmware ↔ its host (`klipper_mmu/host/wire.py`) | public, wire-level; **v1 (2026-09-29)** — §3 enum tables are checked against `wire.py` by `test/test_contract_c5_sync.py`; §7 maps every op, state, stage, admission, fault and result to C1/C2, and lists what has no counterpart, with the four ops C3 §2 has since mapped |

Schemas for C2 and C4 are in `schemas/`, examples in `examples/`;
`python3 docs/contracts/check_examples.py` validates them. C5 has no schema —
it is a wire contract, and its enumerations live in the tables its sync test
reads.

## Versioning rules (from `ARCHITECTURE.md` §8.3)

- **Firmware ↔ host (C1):** a capability-schema number and a protocol version
  are negotiated at connect. The host refuses to operate an unknown major
  protocol and reports the mismatch in `OAMSM_SELFTEST`. Capability schema 4
  is the floor referenced by the design (`ARCHITECTURE.md` §5); the firmware
  currently ships schema 5 (`C1_firmware_host.md` §1.3).
- **Host ↔ UIs (C2/C3):** every status object carries `schema_version`.
  Changes within a version are additive only; clients ignore unknown fields.
  A field is never re-meant — a new meaning is a new field or a new schema
  version (`ARCHITECTURE.md` §1.3).
- **Releases move together** (a G-release across the three repositories) but
  each artifact carries its own version in its status/dictionary, so a mixed
  deployment (e.g. new host, old firmware) is diagnosable rather than silently
  wrong (`ARCHITECTURE.md` §8.3).
- **Contracts are additive.** A contract document only grows new EXISTS rows
  as work packages land; existing rows are corrected for accuracy but not
  reinterpreted.

## Legend

Every claim in a contract document carries one of these three markers:

| Marker | Meaning |
|---|---|
| **EXISTS** | True today, in the code, right now. Carries file:line evidence on both sides of the boundary where both sides exist (e.g. firmware declaration and host lookup). |
| **TO ADD** | Not implemented yet. Carries the work package that will add it (e.g. F1, D6, D7) and, where one exists, the decision that reserves it (D1-D8). Never invented: if the design does not settle the shape, it is an open question instead (§9 of each contract), not a TO ADD guess. |
| **OPEN** | Neither settled by the code nor by a decision or design document. Listed in each contract's "Open questions" section for the owner to resolve. Never silently resolved by an implementer. |

A discrepancy (the host expects something the firmware does not declare, or
vice versa, or the two sides disagree on a format) is its own row, marked
**DISCREPANCY**, with file:line on both sides. Discrepancies are reported,
not fixed, by the contract documents.
