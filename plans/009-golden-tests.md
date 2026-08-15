# 009 — Golden test harness  (model: sonnet · wave 4)

Read `plans/000-interfaces.md`, spec "Testing" + guardrail #5, existing compiler.

## Deliverables
1. `tests/golden/` — fixture pairs `NNN-name.sql` + `NNN-name.ir.json` (expected
   `Graph.to_dict()`), or `NNN-name.sql` + `NNN-name.error.json` (expected
   SqlmpegError.to_dict(); compare code + line only, not message text).
   Write ≥10 cases:
   - 010-readme-pip (the README example, verbatim)
   - 020-scale-only, 021-crop-scale-chain, 030-trim-where, 040-union-concat,
     050-cte-reuse-split (CTE referenced twice → split visible in IR),
     060-blur-regions-macro, 070-overlay-pip
   - errors: 900-group-by (NO_STREAMING_EQUIVALENT), 910-two-columns
     (SINGLE_OUTPUT_ONLY), 920-unknown-func (UNKNOWN_FUNCTION), 930-bad-arity
     (UDF_ARG_TYPE)
2. `tests/test_golden.py` — pytest parametrized over the folder; compiles each .sql,
   compares `to_dict()` deep-equal against .ir.json (or error code/line against
   .error.json). Missing expectation file → fail with instruction.
3. `tests/conftest.py` (if needed) + a regen helper: `python -m tests.regen_golden`
   rewrites all .ir.json from current compiler output (document in a comment that
   regen output must be reviewed in git diff).
4. One smoke test asserting the README example's emitted `filter_complex` contains
   `crop=600:200:1200:50` and an `overlay` (NOT byte-exact full-string).
5. A dialect test: every `tests/golden/*.sql` parses under
   `sqlglot.parse_one(text, read="postgres")` (guardrail #2).

IMPORTANT: generate .ir.json via the regen helper, then EYEBALL each one against the
spec semantics before reporting done; call out anything that looks wrong in your
final report instead of blessing it.

## Verify
ruff, pytest — green. No git commit.
