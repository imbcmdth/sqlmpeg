# 088 — CTE references are row sources

Maintainer decision (2026-08-19, design discussion): the table output
is the ground truth; COPY serializes exactly the relation the table
shows. A CTE reference must therefore behave as SQL says: its body's
rows. The current implementation re-nests a CTE's rows into an array
at the boundary (media side), while table mode gives it row semantics
(0.21.2) — the two disagree, and mixed-count queries fall in the
crack. This plan lands the row model; plan 089 then removes the
implicit aggregation entirely. 088 + 089 release together as 0.22.0.

## Semantics

- `FROM <cte>` contributes the CTE body's rows. A CTE's columns are
  its body's named columns (streams; tag columns keep riding the
  streams). One body row = one outer row.
- Comma between row sources (CTEs, unnest tables, chapters) is a
  cross join, real multiplicity: `FROM vid, aud` with 1 x 2 rows is a
  2-row relation with vid's value repeated. Table mode prints exactly
  that (the 087 count-mismatch rejection is deleted — reality replaces
  it).
- `GROUP BY` and `array_agg` work over CTE-sourced rows, media and
  table alike, with 085's validity rules unchanged: `SELECT vid.track,
  array_agg(aud.track) FROM vid, aud GROUP BY vid.track` is the
  canonical "one video, all matching audio" spelling. Grouped fan-out
  over CTE rows follows the same rules as over unnest rows.
- A CTE whose body is a single row (the PiP shape, any body over just
  an input alias) is a 1-row source: cross joins with it are shape
  no-ops. Array-valued columns inside a row (c.audio selected as a
  column) stay array VALUES — this plan changes row identity, not the
  value model.
- JOIN ... ON between CTE references: out of scope v1 (comma cross
  join + WHERE covers the shapes at hand; unnest-to-unnest joins
  unchanged). A row-metadata column surface for CTEs (aud.language in
  the OUTER query) is also out of scope: CTE columns are what the
  body named, as today.

## Interim note on the media side

Until 089 lands, the media path still implicitly aggregates. Under
this plan that desugar applies to the honest relation: an ungrouped
`SELECT vid.track, aud.track FROM vid, aud` COPY maps the cross join
(vid's stream twice). That is intended — the table preview shows the
duplication before any encode — and 089 turns the same shape into a
typed error. Do not preserve the old re-nest behavior anywhere.

## Mechanics (starting anchors; explore before building)

- lower.py `_CteBinding` (~1370) stores columns as `_Value`s — needs
  row identity: the body's per-row values (the 087 `splat` flag on
  `_Column` marks which arrays are row sets). The relation machinery
  (`_RowRelation`/`_RowBinding`, ~1439-1487) currently only wraps
  unnest track rows; CTE sources must join it (a row whose columns
  are streams).
- `_lower_branch` env setup: FROM items that are CTE references
  populate env.relation (cross join with any unnest tables present).
- Grouping: 085's `_Env.grouped`/partitioning and 086's table
  grouping consume the relation generically once CTE rows are in it.
- parser.py: `_has_unnest`-style admission for GROUP BY/ORDER BY must
  also fire when FROM references a CTE; the 087 mixed-count rejection
  and its tests come out.
- Tag flow: 084's cte_tags carry-over is unaffected (id(StreamMeta)
  keyed).

## TDD (recipes land red first, with 089's)

Recipe 57: the maintainer's query — two CTEs (filtered video, filtered
audio), `SELECT vid.track, array_agg(aud.track) FROM vid, aud GROUP BY
vid.track`, shown both as a table (one row, array cell) and as a COPY
(video once, both audios). Uses av-2eng.mp4.

## Waves

1. Orchestrator: plans 088+089 committed, recipes 57 (+ 089's
   rewrites) red.
2. Implementation (opus): the row model + tests. Full suites green
   except recipes that 089 owns.
3. 089 follows immediately; single 0.22.0 release after both.
