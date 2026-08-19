# 083 — Container tags: read `i.title`, write `-metadata` globals

Completes the CASE story: read container tags as columns on the input
alias, write them as tag columns, so
`CASE WHEN i.title IS NULL THEN 'Untitled' ELSE i.title END AS title`
round-trips. Version 0.21.0.

## Read: container tag columns on the input alias

- Curated keys, typed `text`: `title, artist, album, album_artist,
  date, genre, comment, composer, track, copyright, encoder,
  description`. Absent tag = NULL (this is what makes CASE fill work).
  Unprobed input = INPUT_NOT_FOUND, exactly like `f.duration`
  (lower.py:3429-3450).
- probe.py: `ProbeResult.tags: dict[str, str]` (default_factory), read
  from `format.tags` at probe.py:281-285, keys lowercased. `_tags()`
  (probe.py:353) whitelists language/title for streams — do NOT reuse
  it; capture the full dict (curation happens at resolution, so future
  free-form read needs no probe change).
- parser.py: extend the static gate — `_INPUT_COLUMNS`
  (parser.py:259-261) grows the tag keys (or a sibling frozenset
  `_INPUT_TAG_COLUMNS` consulted next to it at parser.py:3131-3138);
  hint text must keep listing only the seven structural columns plus
  "and container tags (title, artist, ...)" — don't dump 12 keys into
  every unknown-column hint verbatim if it reads badly; judgment.
  `_row_operand` (parser.py:3820-3870) types them `"text"`, sibling to
  the `_is_input_duration` case at 3842.
- lower.py: `_row_value_of` (lower.py:3140-3143) dispatches
  `_InputBinding` + tag key → `probes[alias].tags.get(key)`;
  `_input_value` (lower.py:4479-4538) rejects tag columns in stream
  position with "is a text tag, not a stream" (mirror the `duration`
  rejection at 4495) and its hint at 4525 updates too.
- Table queries: `_table_projection` (lower.py:5904-5924) — extend the
  input-scalar predicate so `select i.title, i.duration from input(...)
  i` renders (cardinality already broadcasts, lower.py:5936-5937).

## Write: container tags from tag columns (no-row-table branches)

- In a COPY branch with NO row table (`env.relation is None`), an
  aliased non-stream SELECT column (same `_is_tag_column` shapes,
  lower.py:6069-6084) becomes a CONTAINER tag. Row-table branches keep
  RFC-012 per-stream semantics unchanged — zero breakage.
- Relax the two rejection sites: lower.py:3853-3864 (value-expr hint)
  and 3868-3875 (generic tail) — in a no-row branch with an alias,
  collect instead of reject. Unaliased value exprs keep the "give it an
  alias" hint, now without the "over unnest'd track rows" clause.
- IR: new `SinkUnit.tags: dict[str, str | None]` (ir.py:180-202).
  NULL means CLEAR and — unlike the per-stream pop at
  lower.py:6057-6058 — must EMIT `-metadata key=` (ffmpeg copies input
  globals by default; clearing must be explicit).
- emit.py: render at emit.py:657 next to `_render_sink_options`, keys
  sorted: `-metadata key=value`, NULL → `-metadata key=`. Both
  build_ffmpeg_args and build_ffmpeg_commands paths.
- REMOVE the `title`/`comment` sink options (sink.py:293-310) —
  maintainer directive: one way to say it; tag columns subsume them
  (breaking, pre-1.0). Sweep: tests/test_sink.py (parity dict ~50-51,
  render test 157-163, validate-happy 275+), test_emit.py:1223,
  docs/system-prompt.md regen (its sink table is generated from
  sink.py), docs/tracks.md:73 prose, recipe 42 (already rewritten to
  the tag-column form). No conflict rule needed.
- Emission order: container tags render BEFORE the remaining sink
  options (so recipe 42's printed bytes are unchanged: `-metadata
  'title=Director Cut' -map_metadata 0`). `strip_metadata`/
  `metadata_from` do NOT conflict with tag columns: ffmpeg applies
  `-metadata` after `-map_metadata` regardless of argv order, tag
  columns win — document, mirroring sink.py:329-330.
- `disposition` as a container key: reject (it is per-stream only).
- Fan-out `TO (...)`: out of scope v1 — a tag column in a fan-out
  branch keeps whatever it does today; per-row container tags (chapter
  title → file title) is a stated follow-up.

## TDD (recipes land red first)

- Recipe 51 (offline): remap film.mkv keeping v+a, set
  `'Director''s Cut' AS title` and clear artist with `NULL AS artist`.
- Recipe 52 (exec, fixture-bound): tests/fixtures/tagged.mp4 (NEW —
  fixtures are gitignored and generated: add `_generate_tagged()` to
  scripts/gen_fixtures.py, testsrc+sine 2s with `-metadata` title
  "Angel One", artist "Docs Dept", date 2026; note mp4 always adds an
  `encoder` tag whose value varies by ffmpeg version — don't pin it) —
  CASE-fill `comment` from `i.comment`, pass `i.title || ' (restored)'
  AS title`; exec assertion probes the written tags.
- queries/: `retitle.sql` (set title/artist from -v variables) joins
  the catalog + README row.

## Waves

1. Orchestrator: fixture + this plan + recipes 51/52 red. Committed.
2. Implementation (opus): everything above + tests — parser gate,
   typing, read dispatch, table branch, write collection, IR/emit,
   conflicts, NULL-clear, stream-position rejection; probe tags unit
   test against tagged.mp4; hermetic CLI tests (probe stubbed, the 067
   lesson). ruff + mypy --strict; full default + exec green including
   both recipes (report true bytes if my pins are off — do not edit
   pins, report).
3. Orchestrator: repin if needed, docs (README one line, tracks.md
   container-tag section, errors.md if hints changed), queries/
   retitle.sql + README row, 0.21.0, tag, push, CI watched green.
