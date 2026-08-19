# 089 — No implicit aggregation

Maintainer decision (2026-08-19): the implicit row-squish is removed,
and the row rule is absolute — a single destination needs exactly one
row, NO exceptions, manifests included. If DASH/HLS output ever
lands, its renditions are array columns on that one row (`array_agg`
of the video ladder, `array_agg` of the audio set), which mirrors the
input model: a container is one row with array columns, coming or
going. No container-dependent carve-outs, so the check is one check.
Releases with 088 as 0.22.0.

## The rules (these become THE documented semantics)

1. A query produces a relation; bare SELECT prints it, COPY
   serializes it. Same relation.
2. One row = one file. A single-path TO requires a single-row
   relation. Rows combine only when written: `array_agg` (+ GROUP BY
   when keyed; an aggregate with no GROUP BY is one group).
3. `TO (expression over row columns)` = one file per row (rule 2, N
   times). Grouped fan-out unchanged.
4. Multi-row relation + single-path TO = typed error, line-anchored on
   the TO, naming the row count and destination. Hint offers both
   exits: gather with array_agg/GROUP BY, or one file per row with a
   TO expression. Message in plain language.

Single-row queries are untouched: arrays are VALUES inside the input
row (`SELECT f.video, f.audio FROM input(...) f` is one row; splats,
`SELECT *`, subscripts all keep working). Only multi-row relations
(unnest/joins/chapters/CTE sources) writing one file change.

## Rulings (from the full-repo audit, 2026-08-19)

- The check uses the RESOLVED row count, not the static shape: a
  WHERE/join that narrows a row table to one row on the actual file is
  legal. This is the ground-truth principle applied: if the table
  prints one row, the COPY of that relation works. Recipes 23/24/28/
  37/45 (single-row on their fixtures) therefore stay as they are.
- Only FROM-referenced relations count. A VALUES CTE consumed by
  `WITH (chapters marks)` is not a row source (recipe 40 unaffected).
- Views follow the same rule as CTEs: a multi-row view body
  referenced in FROM is a row source.
- Generated sources (ffmpeg.sine/color/testsrc2/anullsrc) are one row.
- Must verify in tests: tag inheritance survives array_agg (a
  COALESCE fill still carries the paired row's language tag), and a
  subscript works as a GROUP BY key (`GROUP BY f.video[1]` — two
  rewrites need it; add support if missing).

## The sweep (12 rewrites; every rewrite must keep its compiled bytes
identical where the old output was already right)

- docs/examples.md (6): 25, 26 → wrap the mixed column in array_agg
  (26 keeps the COALESCE fill inside); 27 → per-branch array_agg +
  GROUP BY on the video column; 38, 41 → CTE form (tags in the body,
  array_agg outside); 53 → outer SELECT gains array_agg + GROUP BY
  g.video.
- queries/ (4): extract-audio → array_agg(t.track); remote-tracks →
  array_agg per column; side-by-side → array_agg(hstack(...));
  concat-fill → per-branch array_agg + GROUP BY (its stated purpose
  guarantees multi-row on real files — today it only passes the
  harness because the synthetic probe has one track per type).
- prompt.py: the two sql-probed teaching examples showing the old
  shape (unnest+WHERE track select; the FULL OUTER JOIN mix) get
  array_agg; regen system-prompt.md.
- The 085 equivalence tests become rejection-vs-explicit tests: the
  old implicit shape errors, the explicit shape produces the
  previously pinned bytes.
- New error code or message needs: errors.md heading with a captured
  example, error-schema.json enum entry, prompt.py _REPAIR entry (a
  test asserts every ErrorCode has both) — OR reuse UNSUPPORTED_SQL
  and skip the schema churn; implementer's call, stated in the
  report.

## Docs (orchestrator, wave 3)

rows.md: the Grouping section becomes "Combining rows", stating rules
1-4 as the semantics; the desugar framing is deleted; the join
illustration (~line 60) and the two-scopes tag bullet (~line 78) get
the aggregated spellings. README media sections re-checked (PiP demo
single-row — unchanged; the "Tracks are rows" bullet at ~135 must
mention aggregation; the stale "forty real tasks" count at ~117 fixed
in passing). errors.md gains the new rejection with a captured
example. prompt.py re-taught: aggregation is mandatory for multi-row,
with an example; system-prompt.md regenerated; test_prompt harness
must stay green.

## Mechanics

- The check sits where the branch's relation cardinality meets the
  sink: multi-row relation, single-path TO, no grouping → the rule-4
  error. Fan-out and grouped paths bypass it by construction.
- Remove the implicit array_agg wrapping in the media lowering (the
  splat-of-rows into one output's stream list for ungrouped multi-row
  branches). The explicit paths (085/086/088) already produce the
  same bytes; nothing else changes in emission.
- UNION ALL concat is branch-level, not row-level: untouched.

## Waves

1. (shared with 088) plans + rewritten recipes red.
2. Implementation (opus, after 088 lands): the rule-4 error + sugar
   removal + test sweep. Every rewritten recipe byte-identical to its
   old pin unless the old behavior was the bug.
3. Orchestrator: docs, prompt regen, queries sweep verification,
   0.22.0 per the release procedure.
