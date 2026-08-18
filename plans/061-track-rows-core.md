# 061 — Track rows: parse empirics + row model  (model: opus · branch
v4-track-rows · RFC-009 wave 2; recipes 23-24 are YOUR red targets)

Read plans/rfc-009-track-rows.md first. Goal state: cookbook recipes 23
(unnest audio + WHERE language) and 24 (unnest subtitle + WHERE) compile
to their pinned commands; 25-28 (joins) stay red for wave 062.

## STOP gate: sqlglot empirics FIRST
Probe (sqlglot 30.17, read="postgres") and WRITE DOWN in your report the
exact parse shapes for: `unnest(f.audio) t` in FROM alongside
`input(...) f` comma-sources (implicit-LATERAL function call); `JOIN
unnest(...) b ON a.x = b.x` (join node kinds/sides for INNER/LEFT/FULL
OUTER — needed by 062, capture now while you're in there); COALESCE in a
SELECT list; ORDER BY over row columns; row-column references
(`t.language`) vs stream columns (`t.track`). If any shape is
unparseable or ambiguous under guardrail #2, STOP and report.

## Deliverables
1. parser.py/resolver: `unnest(<alias>.<type>)` FROM items become track-
   row bindings (mandatory alias; the argument must be a bare array of a
   COMMA-visible input alias; typed rejections otherwise). Row schema
   per stream type from RFC-009 § Columns (needs 060's probe fields —
   coordinate: 060 runs concurrently; code against the enriched
   StreamMeta signature in its plan, and if probe.py lacks a field yet,
   rebase your expectations when it lands — your exec verification runs
   after both).
2. Compile-time row semantics: WHERE over row columns (predicate
   evaluator: =, !=, <, >, BETWEEN, AND/OR, IS NULL; NULL matches
   nothing); ORDER BY over row columns admitted ONLY for track-row
   queries (NO_STREAMING_EQUIVALENT everywhere else — pin that fence
   stays with a test); unprobeable input under unnest → typed rejection.
3. Lowering: selecting `t.track` over N surviving rows yields an
   N-element `_Value` array in row order, each element carrying its
   row's StreamMeta as provenance (passthrough refs — recipes 23/24 are
   pure remaps, `-c:0 copy`). Selecting a metadata column as an output
   is a typed rejection (streams are the only outputs).
4. Tests: new test_lower/test_parser sections (synthetic probes for unit
   coverage: WHERE hits/misses/NULL, ORDER BY, rejections, subtitle
   rows, CTE interplay if it falls out — note what doesn't). NO golden
   changes, NO docs/examples.md changes (the recipes are the pins).

## Verify
ruff + mypy --strict on changed modules; full default suite green;
`pytest tests/test_examples.py -m exec`: recipes 23 and 24 GREEN, 25-28
the only red. If a pinned command disagrees with your (correct)
compilation only on trivia (node ids), report the true command — the
orchestrator refines pins. Report parse-shape findings for 062. No git.
