# 097 — `IN (literals)`, which the docs already promise

Found while writing functions (096): `docs/dialect.md` lists
`IN (literals)` in the predicate grammar and `docs/rows.md` shows a
worked example using it -

    CASE WHEN t.tags.language IN ('en', 'english') THEN 'eng' ...

- and neither compiles. `WHERE t.tags.language IN ('eng','fra')` is
`UNSUPPORTED_SQL: unsupported WHERE predicate`. The docs are wrong,
but their instinct is right: `IN` is standard SQL, it is the natural
spelling for matching one of several languages (the most common
track-row predicate there is), and it desugars to an OR chain with no
new evaluation machinery. Implement rather than retract.

Not in the LLM prompt, so no generated query depends on it yet.

## Scope

- `<row column or input scalar> IN (<literal>, ...)` and `NOT IN`,
  wherever the compile-time predicate grammar is accepted: `WHERE`,
  join `ON`, `CASE WHEN`, and the assertion form over a subscript.
- Desugar at parse/validate time to the equivalent `=`/`OR` chain so
  exactly one evaluator stays in play; nothing new in lower.
- Type-check every element against the column's type, as `=` does; a
  mixed list is a typed rejection naming the offending element.
- Empty list `IN ()` is a parse error from sqlglot; `IN (SELECT ...)`
  stays rejected as a subquery predicate (do not weaken that).
- NULL semantics: `IN` is `= a OR = b`, so a NULL column matches
  nothing, matching the existing `=` behavior. Say so in the docs.

## Checks

The rows.md example compiles; `WHERE t.tags.language IN ('eng','fra')`
selects both tracks; `NOT IN` excludes; a number list works over
`t.channels`; a mixed list rejects; `IN (SELECT ...)` still rejects.
Every pinned command byte-identical (this adds a spelling, changes no
output). `scripts/hunt.py` gains `IN` in its predicate generator.

## Docs

dialect.md's grammar line is already right. rows.md's example becomes
true. Add `IN`/`NOT IN` to the prompt's predicate list so the LLM
surface gains it too.
