# Tools for maintainers

Check this index before writing a new helper script. When you build a
reusable tool, put it here: a `--help` or docstring that says what it is for
and when to use it, no hard-coded personal paths (derive them, or take
arguments or environment variables), tests under `tests/<tool>/` registered in
`tools/check.py`, and one line below. One-off scratch scripts belong in the
disposable `build/` area, not here.

## Build and test

| Tool | Use it to |
|---|---|
| `make test` | Run the native C test suite (stops at the first failure). |
| `make test-summary` | Iterate on a change: builds everything, prints compile errors, then one line per native test (`ok` or `FAIL` with its first assertion). Short output for agents. |
| `tools/check.py` (`make check`) | Run the full registered gate (native, Python, Klipper builds, release images) with saved logs. Run before merging. |
| `tools/masked_reach.py ELF ROOT...` | Measure what an interrupt root reaches in a firmware image: instructions, float/libm and VFP sites, 64-bit divides, indirect calls, longest chain. Evidence for the R1 task/interrupt split (`docs/controller-task-boundary-audit.md`). |
| `tools/thumb_cost.py --driver FILE.c --src a.c -I include [--target m0plus\|m7\|both]` | Measure what a `void bench(void)` driver costs in instructions, estimated cycles and microseconds on Cortex-M0+ and Cortex-M7, per function and per libgcc helper, by building with `arm-none-eabi-gcc` and running the ELF in Unicorn. Use it to check a periodic job against its tick budget; the cycle figure is a CPI estimate, not a cycle-accurate simulation. |
| `tools/gdb_probe.py MODE ... -- PROGRAM` | Answer a question about a native test in one gdb batch call instead of adding printf and rebuilding: `why` (first failure, assertion text, repository backtrace, locals, `--print`), `trace` (values at every hit of a location), `stop` (stop at the K-th hit), `inspect` (static look at an ELF, including ARM firmware). `why`/`trace`/`stop` take `--follow-child` to follow a `fork()`ed child (run_isolated-style scenarios) instead of the parent. |
| `tools/test_integrity.py BASE [HEAD] [--repo DIR] [--allow NAME]... [--json]` | See how a branch changed its own tests before merging: deleted test functions, stubbed ones (C bodies left with no assertion call or only `printf("PASS: ...")`, Python bodies left as pass/print/skip), per-file assertion-count drops and new test files that assert nothing. Exit 1 unless `--allow` names the deliberate removals (`--allow test_removed_api`, or a file path for a whole file). |
| `tools/premerge.py BRANCH -m MESSAGE [--allow NAME]... [--repo DIR] [--dry-run] [--release]` | Merge one verified branch into local main without skipping a gate: refuses unless main is checked out and clean, BRANCH contains main, the test-integrity check passes and the gate passes in a disposable worktree of BRANCH, then merges with `--no-ff` using the `-m` message exactly as given (no trailers). Never pushes. It scopes the gate to what changed: BRANCH's changed paths against main are checked against a firmware-affecting list (`mcu/`, `src/`, `include/`, `upstream/`, `configs/`, `tests/config/`, any `Kconfig*`, the top-level `Makefile`, `tools/install.py`, `tools/driver_api_bundle.py`, `tools/check.py`, `tools/premerge.py`, `tests/release_matrix/`, and the `tests/<suite>/` directories `tools/check.py` only builds under `--upstream`/`--openams`/a saved-artifact gate, e.g. `tests/host_h723_command_join/`, `tests/klipper_board_startup/`, `tests/klipper_tmc_uart/` -- derived from `check.py`'s own registries, not hand-copied); a hit runs `tools/check.py --release` (the pinned release matrix, tens of minutes and tens of GB of temp disk), otherwise the much cheaper `tools/check.py`. `--release` always forces the full gate. Prints the decision and, when paths triggered it, which ones. On a failed gate it keeps only that run's `logs/` and `summary.json` under `logs-check-*`, never its build trees. |
| `tools/site_browser.mjs URL [--size WxH] [--dark] [--timeout MS] [steps...]` | Drive a real headless Chrome (dependency-free CDP over Node's global WebSocket) to check the docs site: run ordered `--scroll`/`--click`/`--key`/`--wait`/`--eval`/`--shot` steps against a page, with console errors and uncaught exceptions reported. No npm install needed. |
| `tools/site_package.py [--check-only] [--site DIR] [--out ZIP]` | Check every internal link, `#anchor` and nav chapter in the docs site, then zip `site/` for Netlify (Drop or `netlify deploy --dir`), default `build/netlify/klipper-mmu-docs.zip`. Exit 1 lists each broken reference. |
| Publishing the docs site | The docs site's source is `klipper_mmu/site/`, published to GitHub Pages (https://openamsorg.github.io/klipper-mmu-docs/). After a docs merge to main: `tools/site_package.py --check-only`, then zip `site/` and publish the result (`netlify deploy --dir`, or the zip via the GitHub Pages workflow). Only built site files go public, never source or other docs. |
| `make -C tests/hh_harness test` | Run the real core (`src/*.c`, unmodified) behind Happy Hare's own PRELOAD/UNLOAD seam on its `boxturtle` fixture, with HH's `FilamentPath` as the plant (`tests/hh_harness/README.md`); optional, needs the pinned HH archive extracted and a Python with greenlet+jinja2 (`tools/check.py` OPTIONAL_MAKEFILES omits it by default). |
| `tools/upstream_dossier.py trace\|md\|check` (`make -C tests/upstream_dossier test`) | Boot a device in the pinned Happy Hare test harness, run PRELOAD/SELECT/LOAD/UNLOAD/EJECT on gate 0, and reduce the recording to a committed `docs/devices/<device>.json` dossier plus a Markdown page (`the upstream dossier plan` D1/D2): device facts, rendered hardware/parameters, one move table per operation with the sensor it ended on, and the AFC/Happy Hare/klipper-mmu concept-mapping table. `check` regenerates every committed dossier and diffs both the JSON and the Markdown. Same optional gate as `hh_harness` (needs the extracted archive and greenlet+jinja2); `md` and `check`'s diff logic run under plain system Python. |
| `tools/afc_facts.py facts TEMPLATE [--mcu ALIAS.cfg] [--afc-cfg AFC.cfg]` / `compare DOSSIER.json TEMPLATE ...` | Read one AFC template as plain-INI JSON facts (lanes, pins with polarity and, given `--mcu`, the alias resolved to a bare MCU pin; hub, buffer, speeds), or join those facts with a Happy Hare dossier (`tools/upstream_dossier.py` schema 1) by a fixed 13-concept list and print an agree/differ/afc-only/hh-only Markdown table. `compare` exits 1 if a `differ` row has no matching entry in the dossier's `decisions` list. No AFC or Klipper code runs. |

## Install and configuration

| Tool | Use it to |
|---|---|
| `tools/install.py` | Install, inspect or remove the Klipper source hooks, host extra and driver APIs (`docs/install.md`). |
| `tools/klipper_src_fixture.py TARGET_DIR [--package DIR]` | (Re)create TARGET_DIR as a patched Klipper checkout (WANT_STEPPER_LOCAL applied) from `tests/fixtures/shared_tmcuart`, so a Makefile-driven test can compile against the real, patched `stepper.c` without a hand-written fake. Used by `tests/klipper_controller/Makefile` and `tests/klipper_stepdir/Makefile`; `tests/klipper_stepper_local/run_tests.py` does the same thing itself (it predates this helper). |
| `tools/driver_api_bundle.py` | Rebuild or verify the reviewed Klipper driver API bundle: `rebuild --pinned ARCHIVE --edited TREE [--add PATH]... [--write-install]` regenerates `upstream/drivers-local.patch` and `upstream/source-hashes.json`; `check --pinned ARCHIVE` reapplies them and verifies every hash and both `tools/install.py` pins. |
| `tools/pin_plan_check.py KCONFIG_CONFIG KLIPPER_SRC` | Refuse a `CONFIG_KLIPPER_MMU_PINS_*` map that startup would reject, at build time (`mcu/Makefile` runs it before compiling): one pin per lane or shared, no pin twice, and on the H723 respooler pairs on TIM2/3/4/5/12 (a timer per lane) and DiffA/DiffB on one ADC, read from Klipper's own `hard_pwm.c`/`stm32h7_adc.c` tables. |
| `tools/device_index.py [--check] [--dir DIR]` | Regenerate (or check) `docs/devices/README.md`, the one-table index of every device dossier. |
| `tools/report_migration.py` | Read local Klipper `.cfg` files and print a conservative migration report (`docs/migration.md`). |

## Local model helpers

These talk to any local OpenAI-compatible server (llama-server on
`127.0.0.1:18080` by default). They are optional, and each one has a usable
non-model path.

| Tool | Use it to |
|---|---|
| `tools/mcp_server.py` | Serve the tools above to any MCP client as typed tools: `test_summary`, `run_suite`, `run_test`, `gdb_why/trace/stop/inspect`, `masked_reach`. Stdio; run it from the checkout to serve. |
| `tools/context_pack.py [--no-model] [--with-code] path:symbol\|path:START-END ...` | Print the exact source of the requested C functions, Python methods or line ranges — location, signature and (with `--with-code`) body — with a two-line summary, as a ready context pack so you can edit without first surveying the code. Unavailable model summaries are reported inline; `--no-model` skips them. |
| `tools/local_doc.py` | Draft or check `docs/g-code-commands.md` with the local model, grounded in facts extracted from the code (`docs/local-doc.md`). `check` runs in `make check`. |

Setup these depend on (per machine, not in the repository):
- An OpenAI-compatible local server, e.g. llama-server on `127.0.0.1:18080`.
- `bwrap` for the sandbox; `arm-none-eabi-objdump` for `masked_reach.py`.

## Keeping long runs bounded

Long tasks run in chunks, and a stop is reported rather than retried blindly: a
small concrete fix is taken up directly; a clear but narrower task is rewritten
as a fresh brief; an uncertain design or root cause gets one bounded
consultation with the evidence summary (never the full transcript); a scope that
was too big is split.
