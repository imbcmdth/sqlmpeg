# 087 — CTE array columns in table queries print every row

Maintainer report: a CTE whose body yields two track rows prints ONE
row when referenced from an outer table query —

    WITH aud AS (SELECT a.track AS track FROM input(:'src') i2,
                 unnest(i2.audio) a WHERE a.language = 'en')
    SELECT aud.track FROM aud

prints `<audio 0:a:0>` and stops. Reproduced on av-2eng.mp4 (direct
query: 2 rows; through the CTE: 1). The media COPY of the same query
maps both streams correctly — table mode only. 086 routed bare input
arrays (ArrayCell) and array_agg columns, but a CTE-bound array column
takes neither path and truncates to its first element, the exact
defect class 086 fixed elsewhere.

## Rule

A CTE alias in FROM contributes its rows: an array-valued CTE stream
column in a table query renders ONE ROW PER ELEMENT, like a row
alias's `.track` (the array is the row set) — NOT an ArrayCell and not
the first element. It's `FROM aud`: a two-row table prints two rows.

- Columns from the same CTE align by construction (one body, one row
  count).
- A subscripted CTE column (`aud.track[1]`-style, if the body stored
  an array) stays a single broadcast cell, as today.
- Input-level scalars and bare input arrays beside the CTE column
  broadcast per row, as they do beside a row relation.
- Array-valued columns from two different sources with disagreeing
  row counts in one table SELECT (two CTEs, or CTE next to an unnest
  relation): typed rejection with a plain message (select them in
  separate queries). Do not invent a cross join.
- CSV path identical.

## Anchors

086's routing in lower.py: `_array_cell_broadcast` (input arrays /
array_agg) vs the per-element splat in `_value_to_cells` (row-alias
track columns); `_cte_value` (~4731) returns the CTE's stored `_Value`
— its array case needs the per-element path with a row count. Check
`_lower_table_branch`'s row-count logic (relation tuples or 1) — a
CTE-only FROM has no relation, so the count must come from the widest
CTE array column.

## Tests (wave)

Regression: the exact report shape (CTE + WHERE, two elements → two
rows) table AND csv; a CTE column next to an input scalar (broadcast);
subscripted CTE column single cell; two same-CTE columns align;
disagreeing-count rejection; media COPY of the same query unchanged
(both maps); tagged-CTE column in a table query still prints plain
(084's no-table-effect holds).

## Waves

1. Plan committed (this file; regression is test-level, no recipe —
   bug fix, not a feature).
2. Implementation (sonnet): fix + tests; ruff/mypy/full suites green.
3. Orchestrator: release 0.21.2 per the release procedure.
