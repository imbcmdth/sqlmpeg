# 051 — Uniform resolution: the de-tiering core  (model: opus ·
branch v3-uniform · RFC-007 wave 2)

Read plans/rfc-007-uniform-calls.md (the rule, the namespaces, the error
consolidation) and plan 050's contract notes (Registry.source
live|reference; snapshot fallback: load() then load_reference() when
unavailable; --reference forces the snapshot; snapshot_of/generated for
explain annotations).

MIGRATION BRANCH RULES (the v2-streams precedent): each wave is green on
its OWN scope; stdlib-spelling tests/goldens are EXPECTED RED until wave
053 migrates them. State the red set precisely in your report.

## Deliverables
1. lower.py: bare-name and ffmpeg.<name> calls resolve ONLY in the registry
   (identical semantics; the namespace remains the collision-proof
   spelling). The stdlib lookup, FUNCTIONS import, named_target machinery,
   tier-1 named extras, expr-kind branches: DELETED. stdlib.py itself is
   deleted (052 introduces sqlmpeg/macros.py fresh; do not pre-build it).
2. POSITIONAL OPTIONS — the heart. After the stream inputs, positional
   literal args bind to the filter's options in registry order. CRITICAL
   empirical work first: our registry DEDUPES option aliases (keeps the
   longest name, e.g. width over w) — verify the deduped, insertion-ordered
   option list matches ffmpeg's own positional binding order on a sample
   (scale=640:480 -> w,h; gblur=5 -> sigma; crop=100:50 -> out_w,out_h;
   xfade positional -> transition first). Compile positionally, RUN against
   fixtures, confirm behavior matches the equivalent hand-written
   positional filtergraph. If dedup order diverges anywhere from ffmpeg's
   binding order, STOP and report before building on it.
   - A positional binds/validates as the option it lands on (type/range/
     enum via the existing option validator) — option errors are
     UNKNOWN_FILTER_OPTION / FILTER_OPTION_TYPE uniformly.
   - Mixing: positionals first, then named; a positional after a named ->
     UNSUPPORTED_SQL (existing kwarg-order rule); a named that collides
     with an option already bound positionally -> FILTER_OPTION_TYPE-class
     "already set" (mirror the old conflict wording).
   - More positionals than options -> UDF_ARG_TYPE-style arity message
     naming the filter's option count.
   - Stream-count/type errors stay UDF_ARG_TYPE (against the pad
     signature).
3. Snapshot fallback in compiler.py: registry = load(); if not available,
   load_reference(). portable parameter DELETED; new
   compile_sql(..., reference=False) forcing load_reference().
   cli.py: --portable removed everywhere; --reference added to
   compile/explain/validate (help: "compile against the bundled reference
   snapshot of ffmpeg <version> instead of the installed binary").
   explain: when Registry.source == "reference", annotate the IR dump
   (top-level "registry" key? IR purity — prefer an explain-layer
   annotation, NOT a Graph field; decide and document).
4. Sources/enable/array-returning/broadcasting: unchanged semantics, now
   reachable offline via the snapshot — prove with tests (a source compile
   and an enable compile with which->None).
5. errors.py: nothing added; UDF_ARG_TYPE narrows by usage not by enum.
   prompt.py: DO NOT touch beyond what import breakage forces (053/054 own
   it; if prompt.py imports FUNCTIONS it will break — apply the MINIMAL
   stub to keep the module importable and its tests EXPECTED RED, noted).
6. Own tests green: new test_lower sections for positional binding (incl.
   the empirical fidelity runs, exec), mixing rules, conflicts, arity,
   snapshot fallback, --reference CLI, offline-everything. EXPECTED RED:
   everything referencing stdlib spellings (test_stdlib deleted with its
   module; goldens; cookbook harness; prompt/docs tests; many test_lower
   sections — migrate ONLY the ones your own deliverables touch, list the
   rest).

## Verify
ruff + mypy --strict on changed modules; your own test list green; full
pytest with --continue-on-collection-errors reported and attributed. No git
commands; no version bump. Report: the positional-fidelity findings, the
red set, contract notes for 052 (macro mechanism expectations) and 053
(the spelling-migration map: old stdlib call -> new uniform spelling for
all 39, including arg-order changes like crop).
