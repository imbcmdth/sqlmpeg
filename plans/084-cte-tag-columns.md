# 084 — Tag columns in CTE bodies

The two-level tagging shape: per-stream tags inside a `WITH` (row
scope), container tags in the outer SELECT (file scope, shipped in
083). Today a tag column in a CTE body is rejected; the fix is small
because CTE bodies already run the sink's validation path.

## Semantics

- A CTE body with track rows may carry aliased scalar columns — same
  shapes `_is_tag_column` accepts (lower.py:6166-6184). They tag the
  row's streams (per-stream, the 083-era row rule). NULL clears
  provenance, as it does in sink branches.
- The tags ride the CTE's streams into whatever output finally maps
  them — including through subscripts, splats, and filters (provenance
  threading is already id(StreamMeta)-keyed and identity-stable,
  lower.py:2362-2390).
- A CTE body with NO track rows and a scalar column keeps its
  rejection (a CTE has no container to tag); sharpen the hint to say
  container tags belong in the outer query.
- If the sink branch re-tags the same track's same key, the sink wins
  (inner-then-outer layering, no error). Different sinks may still
  disagree with each other, as today.

## Mechanics (anchors verified)

- The gate is the `tags` keyword on `_lower_query`
  (lower.py:2114-2131) — sink passes True (1776, 1864), the CTE
  entries pass nothing (run() 1768-1771, run_table() 5918-5919).
  Don't just flip it: `tags=True` would also enable 083's container
  path when `env.relation is None` (the 2277-2281 dispatch). Use a
  mode ("sink" vs "rows-only") so CTE bodies collect ONLY per-stream
  tags.
- THE BLOCKER: `_lower_query` resets `self.tags` at entry
  (lower.py:2119-2120), and each sink resets again before `_outputs`
  reads it (1906). CTE-collected tags must survive: harvest into a
  separate `self.cte_tags` after each CTE body lowers, and merge in
  `_metadata` (lower.py:6124-6158) as
  `{**cte_overrides, **sink_overrides}` per stream. A plain
  "don't reset" is wrong — two COPYs may legitimately tag one track
  differently (comment at 1725-1727).
- `_record_tag`'s same-key-disagreement check (2381) stays
  branch-local; the CTE/sink boundary is layering, not disagreement.

## TDD (red first)

Recipe 53 (exec): the two-level query over av2.mp4 — CTE tags each
audio track `'Audio (' || a.language || ')' AS title`, outer SELECT
adds `'Director Cut' AS title` as the container tag.

## Tests (wave)

- Pin the currently-working baseline first: CTE body with unnest +
  WHERE (no test exists — tests/test_parser.py:1696 is the only
  unnest-in-CTE test).
- CTE tag flows to output metadata; survives subscript
  (test_lower.py:1204/1251 are the analogues); NULL clears; sink
  override wins; two sinks over one tagged CTE each get the tag;
  no-row CTE scalar still rejects (golden 910 variant); table queries
  over a tagged CTE unaffected.

## Waves

1. Recipes red + both plans committed (orchestrator).
2. Implementation (opus): mode flag + cte_tags carry-over + merge +
   tests. Full default + exec green incl. recipe 53 byte-for-byte.
3. Docs with 085's (orchestrator): tracks.md two-level pattern,
   prompt.py bullet, regen system-prompt.md.
