# 086 — Grouped table queries; arrays as table cells

Maintainer report: `SELECT f.video, array_agg(a.track) FROM ... GROUP
BY (f.video)` without COPY rejects, but a bare SELECT is the same
relation printed — grouping must print the resulting shape for
inspection. Investigating exposed that arrays have NO table-cell
representation, with three symptoms:

1. Rowless table query, bare input array: `SELECT f.audio FROM
   input(av2) f` prints only `<audio 0:a:0>` — silently drops the
   second element.
2. Bare input array in a table query WITH a row relation
   (`SELECT f.video, a.language ... unnest ...`): INTERNAL IndexError
   panic (guardrail #7 violation).
3. GROUP BY / array_agg in a table query: typed rejection (085's v1
   scope cut, now reversed by the maintainer).

## Fix

- Array cell: Postgres array-literal style over the existing stream
  cell text — `{<audio 0:a:0>,<audio 0:a:1>}`. A bare input array
  column renders as one array cell (all elements, braces even for one
  element); subscripted (`f.video[1]`) and row (`a.track`) stream
  columns keep their plain `<video 0:v:0>` cells. With a row relation
  present, the input-array cell broadcasts per row (same value each
  row), like `f.video[1]` does today. Same text in CSV fields.
- Grouped table query: GROUP BY and array_agg become legal in table
  mode with 085's exact validity rules (grouping rule enforced;
  array_agg a whole SELECT column; ORDER BY inside it rejected). One
  printed row per group, groups in first-appearance order; array_agg
  cell is an array cell of the group's tracks in row order; group keys
  print as plain cells. Column name for an unaliased array_agg:
  `array_agg` (Postgres's convention); alias wins as usual.
- 085's rejections for UNION ALL branches / CTE bodies stay. The
  media-side "row-column GROUP BY requires a fan-out TO" rule does NOT
  apply in table mode — a grouped table query needs no destination;
  every group is just a printed row (this is the inspection story:
  preview the fan-out's partitions before writing files).

## Anchors (from 085's wave, may have drifted slightly)

- The table rejection: `_check_aggregate_context` in parser.py (085
  added it) — drop the table-query arm, keep UNION/CTE/view arms.
- Table lowering: `_table_projection` (lower.py ~5904) + `_value_cells`
  / `_row_metadata_cells`; the crash is in the stream arm hitting an
  input array with a relation present — find and fix the IndexError
  regardless of the rest.
- Cells: table.py `CellValue` includes `StreamCell`; add the array
  rendering there (or a cell type wrapping a list of StreamCells).
- Grouped partitioning: 085's `_fanout_groups`/`_Env.grouped` in
  lower.py — table mode reuses the partition, not the fan-out sink.

## TDD (red first)

Recipe 56 (exec, av2.mp4): the maintainer's inspection query pinned as
a table — and a second fence in the same recipe showing the grouped
preview of the per-language fan-out over av-2eng.mp4 (`SELECT
a.language, array_agg(a.track) ... GROUP BY a.language`), two rows.

## Tests (wave)

- Regressions: the rowless bare-array truncation (now full `{...}`),
  the with-relation INTERNAL crash (now renders), CSV variants.
- Grouped: one group (input-level key), multi-group (row key,
  first-appearance order), aliased and unaliased array_agg headers,
  validity rejections still firing in table mode (ungrouped row
  scalar, ORDER BY inside agg), UNION/CTE rejections unchanged.
- Hermetic via the synthetic-probe patterns; one exec pass through the
  cookbook harness for recipe 56.

## Waves

1. Orchestrator: plan + recipe 56 red.
2. Implementation (sonnet): all of the above; ruff/mypy/full suites
   green; recipe 56 byte-for-byte or true bytes reported.
3. Orchestrator: repin if needed; rows.md "Inspecting" section update
   (grouped preview, array cells); errors.md if messages changed.
