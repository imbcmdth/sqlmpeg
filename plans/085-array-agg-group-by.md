# 085 — array_agg + GROUP BY: the sugar, spelled explicitly

Maintainer directive: the implicit aggregation must have an explicit
form. The model (agreed in discussion): a COPY branch's destination is
its GROUP BY key; non-aggregated input-level columns are implicitly
grouping columns; row-referencing stream columns are implicitly
array_agg'd in row order; one group = one container. This plan makes
that spelling legal, byte-identical to the sugar.

## Semantics

- `array_agg(<expr>)` where `<expr>` lowers to a stream in row context
  (`a.track`, `volume(a.track, 0.5)`, ...): collapses the branch's
  surviving rows into the ordered stream list the bare splat produces.
  Byte-identity with the sugar is the acceptance test.
- `ORDER BY` inside array_agg: typed rejection v1 ("row order is
  preserved; reorder with a WHERE/join shape instead") — sqlglot parses
  it as ArrayAgg(this=Order(...)), so detection is one isinstance.
- `GROUP BY <exprs>`:
  - keys referencing only the input alias or constants → one group;
    the fusion shape. `SELECT f.video, array_agg(a.track) ... GROUP BY
    f.video` == the sugar, byte for byte.
  - keys referencing row columns → the tuples partition by evaluated
    key (first-appearance order); requires a fan-out `TO (expr)`, and
    the TO expression may reference only group keys and input scalars.
    One command per GROUP, all the group's rows aggregated into that
    file. This turns today's duplicate-destination error into a legal,
    asked-for shape; the error stays for the UNgrouped fan-out
    (typo-guard), with its hint mentioning GROUP BY.
  - multi-key GROUP BY: allowed, tuple key.
- Grouping validity (Postgres's rule, enforced): in a grouped branch
  (GROUP BY present or any aggregate in the SELECT), every
  non-aggregated, non-constant expression must match a GROUP BY
  expression syntactically (sqlglot .sql() equality). This is what
  makes tag scope DERIVED: a group-level scalar is provably
  group-constant → container tag of the group's file (a.language AS
  title in the partitioned fan-out = per-file title).
- Aggregates/GROUP BY in table queries, UNION ALL branches, or CTE
  bodies: typed rejection v1 (branch-local feature of a single-branch
  COPY). Per-stream tag columns in a grouped branch: rejection with a
  hint pointing at the 084 CTE shape.

## Mechanics (anchors verified)

- Parser gate 1: `_SELECT_ALLOWED` (parser.py:242) enforced at
  2171-2172, which already admits "order" only when `_has_unnest` —
  admit "group" the same way. Gate 2: the blanket exp.AggFunc
  rejection at parser.py:2236-2244 — narrow to allow exp.ArrayAgg in
  the legal position. The `_STREAMING_CLAUSES` GROUP BY rejection
  (parser.py:227-240, 1281-1303) narrows accordingly; golden
  900-group-by.error.json updates (the non-unnest case keeps a typed
  rejection).
- Grouping validity: net-new check in `_validate_select`
  (parser.py:2159-2202) next to `_check_columns`.
- Lowering: intercept exp.ArrayAgg in `_lower_expr`
  (lower.py:3922-3961) AHEAD of the `_call_parts` arm (948-949 would
  otherwise send it to the registry as an unknown filter). The
  aggregate lowers its argument per tuple exactly like the splat
  (`_row_value`, lower.py:4046-4101; per-row step `_row_stream`
  4114-4166) and returns the same `_Value` array — that is what makes
  byte-identity fall out.
- Tag scope in a grouped branch: at the 2277-2281 dispatch, a grouped
  branch routes scalars to `_collect_container_tag` (they are
  group-constants by the validity check). `_is_tag_column`
  (6166-6184) must not claim an ArrayAgg.
- Partitioned fan-out: the current machinery pins one ROW per command
  (`_pin_fanout_row` 3058-3089, one _Lowerer per row in
  `lower_commands` 6545-6572, TO evaluated against the pinned row in
  `_sink_path` 1944-2010). Grouped fan-out pins one GROUP (a tuple
  subset) per command; `_check_distinct_paths` (6585-6603) then checks
  distinct GROUPS, not rows.

## TDD (red first)

- Recipe 54 (exec): the explicit identity — `SELECT f.video,
  array_agg(a.track) ... GROUP BY f.video` over av2.mp4, pinned to the
  sugar's exact bytes.
- Recipe 55 (exec): partitioned fan-out — new fixture av-2eng.mp4
  (gen_fixtures: video + sine 440 eng + sine 660 eng + sine 880 fra):
  `SELECT array_agg(a.track), a.language AS title ... GROUP BY
  a.language) TO (a.language || '.mka')` → two commands, the eng file
  carrying BOTH eng tracks.

## Tests (wave)

Equivalence property (sugar vs explicit, several shapes, assert same
argv); grouped fan-out multi-row group; validity rejections (ungrouped
non-constant scalar, ORDER BY in agg, agg in table/CTE/UNION, grouped
per-stream tag hint); ungrouped collision error keeps firing; goldens.

## Waves

1. (shared with 084) recipes red + plans committed.
2. Implementation (opus), after 084 lands: parser gates + validity +
   ArrayAgg lowering + grouped fan-out + tests. Full suites green
   incl. recipes 54-55 byte-for-byte.
3. Orchestrator docs: tracks.md "the explicit form" section stating
   the desugaring rule; prompt.py; queries/ candidate
   (per-language-file split); known_gaps sweep; release with 083+084
   (single minor, on maintainer's go).
