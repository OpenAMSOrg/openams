# Local drafts of the command reference

`tools/local_doc.py` drafts `docs/g-code-commands.md` with a local model and
checks any version of it against the code. The draft is a starting point: a
person still reviews what each command does.

```sh
# Draft three versions and keep the one that passes the most checks.
python3 tools/local_doc.py draft --out build/local-doc/g-code-commands.md
# Check a document (any author) against the code; exit 1 on failures.
python3 tools/local_doc.py check docs/g-code-commands.md
```

`draft` needs an OpenAI-compatible server, by default llama-server at
`http://127.0.0.1:18080/v1/chat/completions` (override with `LOCAL_DOC_URL`).
It was tuned on a local 35B model. It never replaces an existing file unless
`--force` is given, and writes a `.report` next to the draft.

## How a draft is made

1. **Facts come from the code, not from the model.** For each registered
   `KLIPPER_MMU_*` command the script collects the handler and the private
   helpers it calls, the `if` around its registration (which printer.cfg option
   enables it), every error message the handler raises (its parameter rules),
   the `host/wire.py` values behind `CLIENT` and `OPERATION`, the managed unit's
   range checks, the docs that mention it, and the exact list of commands. The
   model fills any missing fact by inventing one, so every fact the document
   needs must be in the prompt.
2. **One request per document**, in the model's own formatting, thinking off.
3. **Several drafts in parallel** (`--runs`, default 3); drafts of the same
   prompt differ a lot, so the one with the fewest check failures is kept.
4. **Code fixes only mechanical slips:** a doubled command name, or `MMU=`
   missing from an invocation.
5. **Plain-language pass:** each line that uses an internal term is rewritten
   by the model, one line per request, with a glossary of what the term means.
   A rewrite is kept only if the term is gone and every backticked item and
   command name survives.

## What `check` verifies

Derived from the code on every run, so it follows code changes: only the
registered commands, all of them; every runnable example has `MMU=`, known
parameters, values inside literal ranges and no unquoted spaces; each
conditionally registered command names its option; required parameters are
not called optional; `MMU` is never listed as optional; `CLIENT`/`OPERATION`
values match `host/wire.py`; no internal terms. `make check` runs it against
the committed reference (`tests/local_doc`).

It cannot verify meaning: what a command does, caveats left out, or how
parameters combine beyond the handler's error messages. Review those.

## Evidence and what did not help

On this reference, two blind reviews by another vendor's model found 4–5
errors in a draft made this way and 9 in one written by the same model as a
free agent . Tried and dropped: thinking (slower and worse; unbounded
it spent 4,000–7,500 tokens per answer), low temperature (no change), voting
over samples (its mistakes repeat), asking the model to fix flagged errors (it
regenerates instead), and yes/no claim checks with token probabilities (no
separation between true and false claims).
