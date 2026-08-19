# 080 — Compile-time arithmetic, casts, f.duration  (model: opus ·
branch arithmetic · recipes 45 and 46 are the failing tests)

Read plans/rfc-015-arithmetic.md (accepted design). Build on the value
evaluator from the tag-column work in lower.py and the parser's
`_check_value_expr` typing - arithmetic and casts extend that one
grammar, never a second evaluator. sqlglot shape checks first
(Add/Sub/Mul/Div nodes, Cast, ::text spelling, precedence with
comparisons and BETWEEN).

Key semantics from the RFC: Postgres int division (truncating); float
results emit shortest-roundtrip repr; `::text` bridges numbers into the
still-strict `||`; trim bounds accept expressions (still input seeks);
call literal positions over row-table streams accept per-row
expressions feeding broadcast as ordinary literals; `f.duration` is a
probed scalar pseudo-column on input aliases (typed rejection when
unprobed); compile-time division by zero rejects; NULL propagates, a
NULL trim bound rejects naming the unprobed field.

## Tests
Unit: operator/typing matrix incl. int-division truncation and float
repr; precedence cases; ::text into ||; per-row call arguments across
broadcasting; expression trim bounds (BETWEEN, open-ended, merge rules
untouched); f.duration happy/unprobed; div-by-zero; NULL paths; the
existing strict-|| rejection now hints at ::text (update the hint).
Exec: recipe 45's compile runs end to end; a duration-arithmetic trim
runs and the output is shorter.

## Verify
ruff + mypy --strict; recipes 45-46 green through the harness
(true-bytes on trivia; STOP on semantics); full default suite; full
-m exec attributed; regen docs/system-prompt.md ONLY if the prompt's
expression-grammar text changes (it should - the value grammar section
gains arithmetic/casts; keep it terse). Report: grammar decisions,
files, tails. No git.
