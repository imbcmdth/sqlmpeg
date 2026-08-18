# 064 — Subscript metadata accessors  (model: sonnet · branch
subscript-meta · RFC-009 addendum; cookbook recipe 29 is the red target)

Measured parse shapes (sqlglot 30.17, read='postgres'): both
`f.audio[1].language` and `(f.audio[1]).language` arrive as
Dot(Bracket(Column)) / Dot(Paren(Bracket(Column))) — accept BOTH,
identical semantics (the paren form is the strictly-Postgres spelling;
document nothing here, docs are the orchestrator's).

## Deliverables
1. WHERE conjuncts over subscript metadata: `<alias>.<type>[k].<column>`
   compares like a row column (061's evaluator + 3VL NULL + static
   literal typing, reused not duplicated). The subscript is 1-based,
   bounds-checked like any subscript; the column set is
   parser.ROW_SCHEMAS for that stream type. Works for input aliases;
   decide CTE-column behavior empirically (if rows aren't available
   there, reject with a clear hint rather than half-working).
2. `.track` sugar: `f.audio[1].track` ≡ `f.audio[1]` anywhere the latter
   is legal (stream position). Metadata accessors as SELECT outputs →
   typed rejection (streams are the only outputs). Bare-array access
   (`f.audio.language`, no subscript) → typed rejection hinting unnest
   or a subscript.
3. Predicate placement: these accessors join the compile-time WHERE
   half; a conjunct mixing them with the time window (`f.t`) keeps the
   existing mixed-conjunct rejection. Unprobed input + metadata
   accessor → the probe-required rejection (same as unnest's).
4. Tests: new test_parser/test_lower sections — both parse spellings,
   assertion pass/fail, NULL tag (3VL), bounds, .track sugar in stream
   and WHERE positions, output-position rejection, bare-array rejection,
   mixed-conjunct rejection, static literal typing, subtitle/video
   accessors, unprobed rejection.

## Surface
sqlmpeg/parser.py, sqlmpeg/lower.py, tests/test_parser.py,
tests/test_lower.py. No docs edits (recipe 29 is the pin - report the
true command if it differs on trivia); no goldens.

## Verify
ruff + mypy --strict; full default suite green; `pytest
tests/test_examples.py -m exec -q` fully green including recipe 29;
full `-m exec` tail attributed. No git.
