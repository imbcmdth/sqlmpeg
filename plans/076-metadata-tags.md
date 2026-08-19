# 076 — Metadata tag columns + CASE/|| expressions  (model: opus ·
branch metadata-chapters · cookbook recipes 37 and 38 are the failing
tests; 39-40 are the NEXT wave's - leave them red, do not touch)

Read plans/rfc-012-metadata.md § metadata editing. Settled semantics:
- In a media query over track rows, a non-stream SELECT column sets a
  tag on that row's output stream(s). ROW-SCOPED: the tag applies to
  every stream column the row produces. Alias = tag key (free-form;
  quoted identifiers allowed). Value = compile-time expression over the
  row: literals, row columns, CASE, `||`, NULL (clears the key).
  Unselected tags pass through from provenance unchanged.
- CASE (searched and simple forms) and `||` join the compile-time
  evaluator, and become legal in WHERE and ON too (same grammar; add
  coverage). Type rules: `||` takes text/NULL (numbers reject with a
  hint to quote them? DECIDE: casting ints implicitly is more useful -
  Postgres would require ::text; keep strict: reject non-text operands
  with a hint. State the choice.)
- Emission: tags land as provenance overrides -> existing
  `-metadata:s:<N> key=value`. Zero filter nodes. Override REPLACES the
  provenance value for that key; NULL removes it.
- The media-query rejection narrows: non-stream columns are legal in a
  media query when the query has row tables and every such column is a
  tag column; a media query with no unnest keeps the old rejection.
- Table/CSV queries: unchanged (columns stay plain data).

sqlglot empirics first for CASE/||/quoted-alias shapes, as always.

## Tests
Unit: evaluator CASE/|| matrix (3VL through CASE, NULL propagation in
||); tag override/clear/passthrough; row-scoped multi-stream rows (a
video + audio row both tagged); cross-join value borrowing
(a.track tagged from b's column); free-form and quoted keys; the
narrowed rejection still fires without row tables; WHERE/ON accept the
new grammar. Exec: one real run writes a tag and ffprobe reads it back.
Recipes 37-38 green (report true bytes on wrap/order trivia; pins were
hand-authored).

## Surface
parser.py, lower.py, tests. No docs edits; no probe/emit changes
expected (emission path exists) - report if that assumption breaks.

## Verify
ruff + mypy --strict; full default suite green EXCEPT recipes 39-40
(next wave's, chapters); full -m exec similarly attributed. Report:
grammar decisions (|| typing), files, tails. No git.
