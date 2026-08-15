# 011 — Error docs + stdlib docs  (model: sonnet · wave 5)

Read spec "Error contract" section, sqlmpeg/errors.py, sqlmpeg/stdlib.py.

## Deliverables
1. `docs/errors.md` — one section per ErrorCode: meaning, when it fires, one example
   query that triggers it, the JSON error it produces (real output from
   `validate --json`, not invented). CONCAT_MISMATCH documented as reserved (v1
   probing); INTERNAL documented as a bug (report it).
2. `docs/error-schema.json` — JSON Schema (draft 2020-12) for the error object:
   line (int|null), col (int|null), code (enum of the real ErrorCode values),
   message (string), hint (string|null). All five keys required.
3. `docs/stdlib.md` — GENERATED, not hand-written: `scripts/gen_docs.py` renders it
   from `stdlib.FUNCTIONS` (guardrail #4: table drives docs). Per function: SQL
   signature(s) built from variants (param names + kinds), the doc line, ffmpeg
   filter(s) used. Header notes it is generated and how to regenerate. Idempotent.
4. `tests/test_docs.py` — (a) docs/stdlib.md is up to date (regenerating produces
   identical bytes); (b) every ErrorCode value appears as a heading in errors.md;
   (c) error-schema.json validates the example dicts from SqlmpegError.to_dict()
   (hand-roll the check or skip jsonschema — it is not a dep; simple key/type
   assertions are fine).

## Verify
ruff on new .py files, pytest — green. No git commit.
