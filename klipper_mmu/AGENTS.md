# Contributing to klipper-mmu

`klipper_mmu/` is the MCU add-on package for OpenAMS-compatible multi-material
units. It lives inside the `openams` repository next to the Klipper host
plugin (`src/`, `moonraker/`); the two ship together and are documented
together. It is GPL-3.0-or-later, while the rest of the repository is MIT —
see the root `README.md`.

## Layout

| Path | What is in it |
|---|---|
| `mcu/` | the Klipper MCU add-on: clock, commands, drivers, pin and feeder configuration |
| `src/`, `include/` | the portable C control core, independent of Klipper and allocation-free |
| `sim/` | the simulated board and causal firmware harness |
| `tests/` | native C tests, plus host joins and optional external harnesses |
| `docs/` | the contract and design documents; `docs/architecture.md` is the map |
| `site/` | the source of the published documentation site |
| `tools/` | maintainer helpers, indexed in `tools/README.md` |
| `configs/`, `Kconfig*` | MCU-first configuration and the native menu |

## Build and test

Run from this directory.

| Command | What it does |
|---|---|
| `make test` | build and run the native C suite; stops at the first failure |
| `make test-summary` | the same, but one line per test (`ok` or `FAIL` with its first assertion) — use this while iterating |
| `make check` | the full registered gate through `tools/check.py`, with saved logs under `logs-check-*` and `summary.json`. Run it before proposing a merge. |
| `make check-openams` | the gate that also composes the real OpenAMS host runtime |
| `make check-release` | the pinned release matrix: tens of minutes, tens of GB of temp disk. Only when firmware-affecting paths changed. |
| `make test-motion`, `make test-rate`, `make test-step-segments` | individual native suites |
| `OPENAMS_SOURCE=/path/to/openams python3 -m unittest discover -s tests/klipper_ams_family_contract` | the host/family contract join against the real OpenAMS runtime |

The native tests must run the same code the MCU adapter runs, so a change to
`src/` is not accepted until a native test covers it. A new reusable helper
goes in `tools/`, gets a docstring or `--help`, derived paths (no hard-coded
personal paths), a test under `tests/<tool>/` registered in `tools/check.py`,
and one line in `tools/README.md`. Use `tools/gdb_probe.py` to answer a question
about a failing native test instead of adding prints; never leave debug output
in `src/` or `mcu/`.

## Safety rules

These are the rules that protect a user's printer. They are not style
preferences.

- **Receive-only by default.** A freshly configured board must not actuate.
  Outputs stay disabled through initialization, and a successful
  initialization never enables a motor. Enabling actuation is an explicit
  opt-in (`CONFIG_KLIPPER_MMU_ACTUATION_OPT_IN`, and the per-pin enable maps),
  not a default that a later step turns on.
- **No motion without an explicit opt-in.** Load, unload, follow and calibration
  require an established session and a passed admission gate. A command that
  arrives in the wrong state is refused (`Unavailable`, `Busy`) — never
  attempted optimistically.
- **A chip-reported value is never retried.** A wrong identity, a GSTAT reset, a
  protection flag or a configuration readback mismatch latches at once. Only a
  lost, late or corrupt *transport* reply is retried, within a fixed attempt
  bound. Do not widen that bound to make a flaky test pass.
- **Every actuating command is gated on session state.** See C1 §6.7 in
  `../docs/contracts/` for the published contract. Changing the gate means
  changing the contract and its tests together.
- **Protect bus ownership.** A sampled final stop bit is not proof the slave
  released the wire; the driver enforces post-reply hold and quiet periods.
- **Never fabricate a measurement.** A missing measurement is reported as
  missing, and an operation whose required hardware is unavailable is refused.
  Keep catalogue inclusion, implemented driver, build success, and hardware
  qualification as four separate claims.
- **No flashing and no hardware access** from a development pass, and no
  modification of an installed Klipper or printer configuration. Tests and
  builds need explicit timeouts; do not poll a job in a loop.

## Extending Klipper

This package extends Klipper itself. Reuse `klipper/src` peripheral drivers,
the scheduler, transport and shutdown services first — firmware ownership of
MMU control does not justify a second UART, PWM, ADC or board-driver stack.
When a capability is genuinely missing, add the smallest explicit driver API
extension, with disabled-build equivalence and existing-client regression
checks. A private upstream API is a reason to expose a narrow interface, not to
copy its implementation. No private-structure casts, no runtime monkey patches,
no replacement drivers. Keep upstream-source diffs explicit and reviewable;
prototypes stay in a disposable checkout until accepted.

Configuration is MCU-first through Klipper's existing target selection. Board
names are optional editable wiring presets, not the support boundary: a user
must be able to assign every signal's actual MCU pin and its polarity, pull and
electrical settings. Gate a missing peripheral capability explicitly instead of
treating one named controller board as the only supported architecture.

## Style

- Prefer less code for the same required behavior. Direct, readable control
  flow; extract shared code when it removes real duplication. No speculative
  frameworks, unnecessary layers or clever abstractions.
- Keep the common C control core allocation-free and independent of Klipper.
- Write documentation a person would want to read: concise and legible,
  friendly rather than dry. Short sentences, plain words, facts in tables and
  lists. Wit where it costs nothing; it never replaces a fact, pads the length
  or softens a safety warning. Code comments and commit messages stay plain.
- Preserve upstream attribution for adapted code. Never copy unrelated
  security, provisioning, credentials, device identities or private material.
- Every source file carries an SPDX header (`GPL-3.0-or-later`) in its own file
  type's comment syntax, after any shebang.

## Commits

- Commit each accepted, verified change with a descriptive title explaining
  the behavior or the problem addressed — not the files touched.
- Write only the files your change needs. You are not alone in the workspace:
  preserve other people's edits, and do not touch sibling projects.
- Do not commit generated builds, logs, scratch prototypes or review
  artifacts. `build/` and the scratch patterns in `.gitignore` stay out.
- Do not amend or rewrite published history, and do not push without being
  asked.
- `tools/premerge.py` merges a verified branch into local `main` without
  skipping a gate (it refuses on a dirty tree, requires the branch to contain
  `main`, runs the test-integrity check, and never pushes). Use it instead of a
  bare `git merge`.
- `tools/test_integrity.py` shows how a branch changed its own tests before you
  merge: deleted or stubbed test functions, assertion-count drops, new test
  files that assert nothing. Run it when tests are in the diff; a removed test
  needs an explicit `--allow` with the reason.

## Where things are

- `docs/architecture.md` — ownership and the contract boundaries.
- `IMPLEMENTATION_STATUS.md` — the live checkpoint. Start here.
- `../docs/contracts/` — the wire and status contracts shared with the host and
  UIs.
