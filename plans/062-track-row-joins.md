# 062 — Track-row joins + fills  (model: opus · branch v4-track-rows ·
RFC-009 wave 3; cookbook recipes 25-28 are the red targets — ALL of them
green is this wave's definition of done)

Read plans/rfc-009-track-rows.md and plan 061's landed code + its parse
findings (the JOIN arg matrix lives in parser.py's module docstring:
side='FULL' arrives with AND without kind='OUTER'; comma sources and
explicit joins share Select.joins). 061 left two extension points shaped
for you: `_split_where`'s two-row-table rejection (becomes the ON path)
and `_eval_row`'s single-binding signature (generalize to alias→row).

## Deliverables
1. Joins between unnest tables: INNER, LEFT, FULL OUTER (accept FULL
   with/without kind), plus comma-between-unnests as the bounded cross
   join. ON predicates reuse 061's evaluator (=, !=, <, >, <=, >=,
   BETWEEN, IS [NOT] NULL, AND/OR/NOT; 3VL — NULL matches nothing).
   Joining an unnest table to a stream-level source, or JOIN syntax
   anywhere else, keeps the existing rejection. Result row order: left
   side's order, then (FULL) unmatched right rows in their order —
   RFC-009's rule, no sorting. Multiplicity is real join semantics (a
   row may pair twice); document in the report, test it.
2. NULL track columns: selecting one bare (outer join gap) is a typed
   rejection naming the missing side/key; COALESCE(track, <fill>) is
   the accepted spelling.
3. Fills, by type:
   - audio: a generated-source call as COALESCE fallback (recipe 26/27
     pin `ffmpeg.anullsrc(duration => 2)` → an ordinary zero-input
     anullsrc node). Inherit-when-omitted: duration ONLY in v1 (from the
     paired row's duration column; typed rejection when neither an
     explicit duration nor a probed one exists). Do NOT auto-inject
     sample_rate — the pins show exactly what was written.
   - video: same mechanism, ffmpeg.color() — inherit size (from paired
     width/height), rate (fps), duration when omitted. Unit-tested (no
     recipe pins it).
   - subtitle: sqlmpeg.empty_captions() — a NEW input-minting macro:
     lowers to an extra INPUT `-f webvtt -i
     "data:text/vtt;base64,V0VCVlRUCgo="` (measured working 2026-08-17),
     stream ref to its one subtitle pad, provenance from the paired row
     so the language tag emits. Needs an internal per-input format
     option flowing through Graph.input_options → emit (NOT exposed in
     the user-facing INPUT_OPTIONS table). Passthrough-only rules hold.
   - a fill spelling of the wrong type for the column → UDF_ARG_TYPE.
4. The fill's provenance = the paired (non-NULL side's counterpart) row
   metadata restricted to tags (language/title) — recipe 26 pins fra on
   the silence-filled mix; recipe 27 pins it through concat.
5. Pins are the spec, node-id trivia aside: if your correct compilation
   differs from a pin ONLY on node numbering/whitespace, report the true
   command for the orchestrator to repin — do not edit docs/examples.md
   yourself. Any SEMANTIC divergence from a pin: STOP and report.

## Surface
sqlmpeg/parser.py, sqlmpeg/lower.py, sqlmpeg/macros.py (empty_captions),
sqlmpeg/emit.py + compiler.py ONLY as far as the internal input-format
option requires, tests/test_parser.py, tests/test_lower.py,
tests/test_macros.py. No docs edits; goldens only if a NEW golden is
warranted (do not respell existing ones).

## Verify
ruff + mypy --strict on changed modules; new tests green; full default
suite green; `pytest tests/test_examples.py -m exec -q` — recipes 25-28
green (modulo reported repin trivia), NOTHING else red; full `-m exec`
tail attributed. Report: join implementation notes, fill inheritance
behavior, the empty-captions input mechanics, repin list. No git.
