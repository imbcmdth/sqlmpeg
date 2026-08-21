# 094 — chapters is an output column: literals, copies, and WebVTT cues

Runs after 092 (chapters as an input array column). The output row
takes the input row's shape: stream arrays, scalar tags, and a
`chapters` record array. A column named `chapters` holding an array of
chapter records IS the file's chapter list. The sink options
`chapters` and `chapters_from` are removed - one way to say it.

## The `chapter` composite type
- `chapter(title text, start_t number, end_t number)` - the same
  schema `unnest(f.chapters)` exposes. Defined by the dialect; valid
  Postgres syntax given such a type, so `ROW('Intro', 0, 15)::chapter`
  parses today (sqlglot: Cast(Tuple/Row)).
- Literal form: `ARRAY[ROW('Intro', 0, 15)::chapter, ROW('Chapter 1',
  15, 25)::chapter] AS chapters`. `{...}` braces are NOT Postgres
  array syntax outside a string literal and stay rejected.
- Compile-time validation, typed rejections: field count and types;
  start < end; windows non-overlapping and ascending; `title` text.
  Values take the value grammar (variables, `||`, arithmetic,
  `f.duration` for an open last chapter).

## Three sources for the column
1. Literal `ARRAY[ROW(...)::chapter, ...]`.
2. Copy-through: `g.chapters AS chapters` from another input (replaces
   `chapters_from`). Plain `f.chapters` from the same input is the
   default behavior of ffmpeg's chapter passthrough - document what
   selecting it explicitly vs omitting it means (ffmpeg copies
   chapters from the first input by default; an explicit `NULL AS
   chapters` clears, mirroring tag columns).
3. From rows: `array_agg(ROW(m.title, m.start_t, m.end_t)::chapter) AS
   chapters` over any row source - a VALUES CTE (the old writing
   shape, now consumed relationally), track rows, or cue rows.

## Cue rows: WebVTT as a row source
- `unnest(v.cues) c` over an input whose stream is WebVTT (or a
  sidecar .vtt input) yields rows `index, start_t, end_t, text`.
  ffprobe does not enumerate cues; sqlmpeg parses the VTT text itself
  (it already reads and writes VTT for the empty-captions fill). v1:
  WebVTT only; SRT is a follow-up.
- The canonical conversion: WebVTT is HLS's chapter-metadata format,
  so `array_agg(ROW(c.text, c.start_t, c.end_t)::chapter) AS chapters
  FROM input('chapters.vtt') v, unnest(v.cues) c` is the first import
  recipe. Cue rows are also plain table-query material (caption
  timing inspection).

## Emission
Unchanged mechanism: the chapter list becomes the one extra
ffmetadata `data:` input with `-map_chapters`, exactly what the sink
option emits today - only the SQL spelling moves. Pinned bytes of the
existing chapter-writing recipe (40) must be byte-identical after its
rewrite.

## Removals and sweep
- Sink options `chapters`, `chapters_from` deleted (tests, prompt
  table regen, errors.md if a captured example names them).
- Recipe 40 rewritten to the column form; a new recipe for the VTT
  import; rows.md gains the `chapter` type and cue rows; dialect.md
  gains `ARRAY[ROW(...)::type]` in the value grammar.

## Waves
1. Recipes red first (40 rewritten, new VTT-import recipe, a literal
   recipe), plan committed.
2. Implementation (opus): type + literal parsing/validation, the
   output column, cue rows, sink-option removal, tests.
3. Orchestrator docs, release.
