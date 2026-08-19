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

## Recipe/queries sweep (the bulk of the work; every rewrite must
keep its compiled bytes identical where the output was already right)

- Wrap-in-array_agg rewrites: join+fill shapes (recipe 27 and
  friends), any `SELECT <per-row stream expr> FROM ... unnest ...`
  writing one file → `SELECT array_agg(<expr>) ...`.
- Two-level rewrites: the retag recipes (23/24, 37/38 area) — tag
  columns move into a CTE, the outer SELECT aggregates (recipe 53's
  shape, now the only spelling). Per-stream tags in a grouped branch
  stay rejected with the CTE hint (085), which is now load-bearing.
- Fan-out recipes (47/48, 55) unchanged. Single-row recipes
  unchanged. queries/*.sql swept the same way.
- The equivalence tests from 085 (sugar == explicit) become
  rejection-vs-explicit tests: the old sugar shape now errors, the
  explicit shape produces the previously pinned bytes.

## Docs (orchestrator, wave 3)

rows.md: the Grouping section becomes "Combining rows", stating rules
1-4 as the semantics; the desugar framing is deleted. README media
sections re-checked (the PiP demo is single-row CTEs — unchanged).
errors.md gains the new rejection with a captured example. prompt.py
re-taught: no implicit aggregation, always array_agg for multi-row,
with an example; system-prompt.md regenerated; test_prompt harness
must stay green (its examples may need the same rewrites).

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
