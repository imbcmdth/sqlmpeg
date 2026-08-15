# 020 — broadcasting  (model: opus · wave C · branch v2-streams)

Read plans/000b-interfaces-v2.md (§ lower.py broadcasting), RFC-001
(§ Broadcasting), and the committed 019 lower.py. Extend, don't rewrite.

## Deliverables
Extend `sqlmpeg/lower.py` (+ tests):
- Expression values become (type, scalar-ref | list-of-refs). Bare
  `a.video`/`a.audio`: probed → list of refs (file order); unprobeable →
  INPUT_NOT_FOUND with the "cannot enumerate streams" message.
- Broadcasting at call sites: any stream-kind arg that is a list → expand the
  call elementwise (fresh nodes per element); multiple lists zip →
  BROADCAST_MISMATCH (message names both aliases/exprs and lengths); scalars
  repeat. Result is a list. Nested broadcasts compose naturally.
- SELECT-list splat: a list-valued column expands to consecutive Outputs.
  Provenance: element derived 1:1 from source stream k carries that stream's
  metadata (thread provenance through single-stream-input chains; multi-
  stream-input functions (amix, overlay) break provenance → empty metadata).
- CTE columns may now be arrays: record (name, type, refs list, provenance);
  `cte.<name>` splats/broadcasts; `cte.<name>[k]` subscripts (1-based,
  bounds-checked against the KNOWN length — this is static, no probe needed).
- WHERE trim composes with arrays (trim applies per element, shared nodes).
- UNION ALL with array columns: equal lengths or CONCAT_MISMATCH.
- Tests: reverb-all-languages (probed av fixture with 2+ audio tracks — extend
  scripts/gen_fixtures.py with a 2-audio-track file, e.g. sine 440 + sine 880,
  language metadata eng/fra set via -metadata:s:a:N; mark probe-dependent
  tests exec), zip mismatch, scalar broadcast, CTE array splat + subscript,
  provenance metadata carried / dropped through amix, symbolic INPUT_NOT_FOUND.

## Verify
Same gate as 019 (all listed suites green, incl. `-m exec` for the probed
ones). No git commands.
