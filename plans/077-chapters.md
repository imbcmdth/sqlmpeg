# 077 — Chapters: read table and VALUES-defined writing  (model: sonnet ·
branch metadata-chapters · runs AFTER 076; cookbook recipes 39 and 40
are the failing tests)

Read plans/rfc-012-metadata.md § chapters. Settled design:

## Read: `chapters(<alias>)` in FROM
- probe.py: parse `ffprobe -show_chapters` (add the flag to the
  invocation); ProbeResult grows `chapters: list[ChapterMeta]`
  (start_t/end_t floats in seconds, title from tags, 1-based index).
  Opportunistic like everything probed.
- parser/lower: `chapters(f) c` binds a compile-time row table with
  columns `index` (int), `title` (text), `start_t`, `end_t` (numbers).
  Same machinery as unnest rows: WHERE/ORDER BY, table/CSV output.
  No `track` column - selecting chapters into a MEDIA query is a typed
  rejection (chapters are not streams). Unprobeable input rejects like
  unnest. Recipe 39 pins the table rendering (floats print as Python
  str(float): "0.0").
- gen_fixtures.py: new `av-chapters.mkv` - av.mp4 remuxed with an
  ffmetadata chapters input: Intro 0-1, Credits 1-2 (matching recipe
  39's pin exactly).

## Write: `chapters <cte>` sink option + `chapters_from <alias>`
- Parser: VALUES-list CTEs (`WITH marks(a, b, c) AS (VALUES ...)`)
  become compile-time row tables (columns named by the CTE column list,
  types inferred from the literals; sqlglot shape checks first).
  Reachable ONLY by the chapters sink option in v1 - selecting FROM a
  VALUES CTE stays rejected (scope control; note the rejection).
- sink.py: `chapters` option whose value is a CTE NAME (identifier);
  the CTE must have start_t/end_t/title-compatible columns (number,
  number, text - match by name). `chapters_from` takes an input alias:
  emits `-map_chapters <input index>`. Both set: reject.
- Emission for `chapters <cte>`: one extra input
  `-f ffmetadata -i 'data:text/plain;base64,<b64 of ;FFMETADATA1 +
  [CHAPTER] blocks, TIMEBASE=1/1, integer-or-float START/END, title>'`
  plus `-map_chapters <that input's index>`. Recipe 40's pin holds the
  exact base64 for its VALUES rows - byte-match it (the orchestrator
  computed it from TIMEBASE=1/1, START=0/END=60/title=Intro,
  START=60/END=300/title=Act One, trailing newline). The extra input
  is compiler-minted like empty_captions (internal, not user-spellable).
- Exec test: write chapters to a real mkv, ffprobe -show_chapters reads
  them back (titles + times).

## Verify
ruff + mypy --strict; recipes 39-40 green through the harness (true-
bytes reports on trivia; STOP on semantic divergence); full default
suite; full -m exec attributed. Report: parse shapes, ChapterMeta
fields, the VALUES fence, tails. No git.
