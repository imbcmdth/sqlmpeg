# 052 — The sqlmpeg.* macro namespace  (model: sonnet · branch v3-uniform ·
RFC-007 wave 3a, PARALLEL with 053a — file firewall below)

Read plans/rfc-007-uniform-calls.md § namespaces and plan 051's contract
notes (mirror the N_INPUT/ARRAY_RETURNING table pattern; macros reject
named args; the three expansions specified exactly).

## Deliverables
1. `sqlmpeg/macros.py` (new): a small frozen-dataclass table MACROS with
   exactly three entries and their expansion callables (the plan-051 notes
   give each expansion precisely):
   - blur_regions(f, x, y, w, h, sigma) -> crop -> gblur -> overlay
   - speed(f, factor) -> setpts=PTS/factor
   - delay(f, seconds) [VIDEO only] -> format=pix_fmts=yuva420p +
     tpad=start_duration=<s>:stop=1:color=black@0
   Signature checking: positional literals per the macro's OWN documented
   order (we own these signatures); named args -> typed rejection ("a
   sqlmpeg macro takes positional arguments; see docs" flavor); wrong
   arity/kinds -> UDF_ARG_TYPE with the macro's signature.
2. parser.py: reserve `sqlmpeg` alongside `ffmpeg` (alias/CTE/view names);
   the call-position Dot(Identifier(sqlmpeg), Anonymous) shape and the
   bare-column hint mirror the ffmpeg namespace exactly (probe parse
   shapes empirically for symmetry — expect identity with plan 038's
   findings).
3. lower.py: `sqlmpeg.<name>` resolution — MACROS table only; unknown ->
   UNKNOWN_FUNCTION with did-you-mean over the three + a hint that filters
   live bare or under ffmpeg. Registry NOT consulted. Macros work offline
   (no registry needed) — test with which->None.
4. Tests in NEW FILE tests/test_macros.py ONLY (firewall: 053a is
   concurrently respelling tests/test_lower.py — do not touch it, or any
   test file, golden, or doc that exists today; your surface is macros.py,
   parser.py, lower.py, tests/test_macros.py). Cover: each expansion's
   node shape; broadcasting delay over an array; ad-insert composition
   compiles (overlay(f.frame, sqlmpeg.delay(p.frame, 120), 20, 20));
   named-arg rejection; arity errors; reserved-name rejection; offline.
   Exec: the ad-insert runs (fixtures) — reuse the plan-038 pixel test
   pattern ONLY if cheap, otherwise compile+run+duration.

## Verify
ruff; mypy --strict on changed modules + tests/test_macros.py; pytest
tests/test_macros.py + your parser/lower additions green; DO NOT run the
full suite to green (053a is concurrently moving the red set) — run it
--continue-on-collection-errors and attribute only. No git commands.
