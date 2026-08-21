# 095 — The type system (spec for review)

Status: PROPOSED. Review, amend, then implement. After implementation
this document moves to docs/types.md as the reference.

Why: the compiler carries its shapes as ad-hoc frozensets that must
agree by hand; 092 had to split one because "array column" and "array
of streams" were the same set by accident. Functions (096) and record
literals (094) need declared types to stand on. One source of truth,
everything else derived - hints, docs, the LLM prompt.

Syntax is Postgres's `CREATE TYPE ... AS (...)`. The types are
BUILT-IN and documented; users do not declare them. (Verified:
sqlglot parses these declarations and `::audio_stream[]` casts today.)

## 1. Scalars

    text      -- SQL text
    number    -- integer or float; Postgres typing (int/int truncates,
              -- any float operand gives float)
    boolean   -- predicates only; not a column type

## 2. Streams ARE the records

There is no separate handle type at the surface. A stream value is a
`video_stream` / `audio_stream` / `subtitle_stream` / `data_stream`
record: filters take them and return them, `-map` maps them,
`SELECT a` selects one, `array_agg(a)` gathers them. The graph node
behind a record (what the emitter turns into a `-map` or a pad label)
is internal and has no name in the language. A filter output is a
record whose facts were never probed; it has no readable fields (R3),
and its W fields ride through to whatever maps it (how tags already
survive filters). Subtitle and
data streams are passthrough-only (no filter accepts them).

## 3. Stream records - what a row about a stream carries

    CREATE TYPE video_stream AS (
        index          number,     -- RO  1-based, agrees with f.video[k]
        tags           tag[],      -- W   language, title, any key
        disposition    flag[],     -- W   default, forced, ... (closed set)
        codec          text,       -- RO
        width          number,     -- RO
        height         number,     -- RO
        fps            text,       -- RO  verbatim, e.g. 30000/1001
        color_transfer text,       -- RO
        bitrate        number,     -- RO
        duration       number);    -- RO

    CREATE TYPE audio_stream AS (
        index          number,     -- RO
        tags           tag[],      -- W   language, title, any key
        disposition    flag[],     -- W
        codec          text,       -- RO
        channels       number,     -- RO
        sample_rate    number,     -- RO
        channel_layout text,       -- RO
        bitrate        number,     -- RO
        duration       number);    -- RO

    CREATE TYPE subtitle_stream AS (
        index number, tags tag[], disposition flag[], codec text);
    CREATE TYPE data_stream AS (     -- the landmine bucket: anything
        index number, tags tag[],    -- ffprobe cannot classify; never
        disposition flag[], codec text); -- attachments, never cues
    -- index/codec RO; tags/disposition W

    CREATE TYPE tag  AS (key text, value text);   -- free-form keys
    CREATE TYPE flag AS (key text, set boolean);  -- disposition: the
        -- closed set ffmpeg knows (default, forced, dub, original,
        -- comment, lyrics, karaoke, hearing_impaired, visual_impaired,
        -- clean_effects, attached_pic, captions, descriptions,
        -- metadata, dependent, still_image, ...); an unknown key is a
        -- typed rejection. Read: `a.disposition.forced` (boolean).
        -- Write: emitted as `-disposition:N default+forced` from the
        -- set flags.

W = writable: an assertion the query may make; emitted as that
stream's tag (`-metadata:s:N`, `-disposition:N`). RO = read-only: a
probed fact; a constructed record that sets one is a typed rejection.
Every W field is nullable; NULL clears.

The fields are ABOUT the stream; the stream itself is the record's
identity, not a field. Two records are the same stream or they are
not - identity is nominal, never field-by-field (two filter outputs
with all-NULL facts are two different streams).

## 4. Non-stream records

    CREATE TYPE chapter AS (
        index   number,   -- RO  ffprobe's order, 1-based
        title   text,     -- W
        start_t number,   -- W   seconds
        end_t   number);  -- W   seconds

    CREATE TYPE attachment AS (    -- 094
        filename text,    -- W
        mimetype text,    -- W
        path     text);   -- W   source file when constructing;
                          --     NULL when read from a container

    CREATE TYPE cue AS (           -- 094
        index   number,   -- RO
        start_t number,   -- W   seconds
        end_t   number,   -- W   seconds
        text    text);    -- W

A `cue[]` in a subtitle position IS a WebVTT subtitle stream: reading
one (`unnest(v.cues)`) and writing one (`ARRAY[ROW(...)::cue, ...]`,
or `array_agg(ROW(...)::cue)` over rows) are symmetric with chapters,
and chapters<->cues converts both ways. Emission reuses the `data:`
WebVTT input mechanism `sqlmpeg.empty_captions()` already uses.

Constraints checked at compile time for constructed chapters:
start_t < end_t, ascending, non-overlapping.

## 5. The container - the type of an input row

    CREATE TYPE container AS (
        video       video_stream[],
        audio       audio_stream[],
        subtitle    subtitle_stream[],
        data        data_stream[],
        chapters    chapter[],
        attachments attachment[],      -- 094
        duration    number,            -- RO
        tags        tag[]);            -- W   container tags

Tags are an array, not a set of named columns: no collision between a
tag key and a column name (`audio`, `track`, `duration` are plausible
keys), and free-form keys are the only kind - reading and writing
share one shape. Stream records carry `tags tag[]` the same way
(`language`, `title` live there, not as fields). Access is by path,
`f.tags.title` / `a.tags.language` (parses today as a dotted column):
the key after `tags.` names the entry, NULL when absent. No bare
`f.title` sugar (DECIDED: O6). Writing: `'eng' AS tags.language`? No -
a tag is written as a tag-column the way it is today, `'eng' AS
language`, which is construction of the `tags` entry by key (R4);
`NULL AS language` clears it.

`input('path') f` is a table of ONE `container` row. `f.t` is not a
field: it is the seek handle, legal only in `WHERE` trim windows, and
takes no part in `SELECT *`. `f.frame` is REMOVED (a holdover from
before track rows; it was only ever `f.video[1]`).

A generated source (`ffmpeg.sine(...) s`) is a `container` whose one
array holds one record with every fact NULL; the other arrays are
empty.

## 6. Rules

R1. `unnest(f.<array>) a` turns `T[]` into rows of `T`. Works for the
    four stream arrays, `chapters`, `attachments`.
R2. `SELECT *`: over a container, its array columns (stream arrays
    in v/a/s/d order, then `chapters`, `attachments`) - the remux
    shape, never the scalars. Over unnest rows, the record's fields -
    the metadata table. `SELECT a` over unnest rows is the stream.
R3. Field access: `f.audio[1].codec`, `a.index`, `(f.audio[1]).index`
    read a field; `a.tags.language` / `f.tags.title` read a tag.
    Field access on a FILTER OUTPUT is a typed rejection: the output
    pin is connected to nothing, so the node never exists, and a
    stream nobody mapped has nothing to report - `scale(v, 640,
    -2).width` cannot be known without running it, and `volume(a,
    0.2).language` is just `a.language` spelled twice. The hint
    names the input-side read.
R4. Construction is "a stream plus W-field columns": in a function
    body, CTE, or SELECT list, `SELECT a.audio[1], 'eng' AS language`
    yields that stream with `language` overridden - the existing tag-
    column shape, now the only constructor for stream records.
    Narrow non-stream records (chapter, attachment) construct
    positionally: `ROW(...)::chapter`, `ARRAY[ROW(...)::chapter, ...]`.
    Setting an RO field anywhere is a typed rejection.
R5. Tags ARE writable fields. Today's "tag column" is exactly R4's
    alias construction applied to a row - the two stories merge. A
    record reaching an output emits its non-NULL W fields.
R6. The OUTPUT row is positional streams + container scalars +
    record arrays (`chapters`, `attachments`). It is NOT a `container`:
    named stream arrays (`... AS audio`) are rejected on output -
    streams are positional, one way to say it.
R7. Nullability is SQL's: an outer join's gap side is a NULL record;
    `COALESCE(b, ffmpeg.anullsrc(...))` produces a stream.
R8. CTE/view rows have the body's column types (an anonymous row
    type), as in SQL. A function's `RETURNS` names one of the types
    above, an array of one, or `TABLE(...)` of them.
R9. Homonyms: type names and column/alias names are separate
    namespaces (SQL's rule); `audio_stream` the type and `f.audio` the
    column never collide.

## 7. What the implementation derives from this

- `sqlmpeg/types.py`: the declarations as data. `ROW_SCHEMAS`,
  `_INPUT_COLUMNS`, `_UNNEST_COLUMNS`, `_STREAM_ARRAY_COLUMNS`,
  `INPUT_TAG_COLUMNS`, the tag-key handling, and the per-field
  writability all become views over it.
- Filter pad checks = handle-type checks; UDF_ARG_TYPE speaks
  "expected audio, got video".
- Error hints, docs/rows.md tables, and the LLM prompt's column
  sections render from the same data.
- BREAKING: `f.frame` disappears (`f.video[1]` everywhere: ~24
  cookbook lines, 13 queries, the README). Pinned bytes unchanged.
- BREAKING: `.track` disappears. `SELECT a` / `array_agg(a)` /
  `GROUP BY a` / `scale(a, ...)` replace `a.track` everywhere
  (~20 recipes, the queries, tests); `a.track` becomes a typed
  rejection with the hint "the row is the stream: use a". Every
  pinned ffmpeg command stays byte-identical.
- BREAKING: container and stream tags become `tags tag[]`; the
  twelve named container columns and `language`/`title` fields are
  accessor sugar over it (O6).
- Other behavior changes: `disposition` readable (new), RO-field
  construction rejected (new), `SELECT *` defined per R2, cues
  writable, field access on a filter output rejected.

## 8. Decisions (maintainer, 2026-08-21)

- O1 `disposition`: a closed-key boolean map (`flag[]`), read by path,
  written from set flags. No longer a reserved tag key.
- O2 constructed chapters must set `start_t` and `end_t`; `title` may
  be NULL.
- O3 resolved by `tags tag[]`: free-form everywhere.
- O4 attachments populate `f.attachments` only. `f.data` is reserved
  for what ffprobe cannot classify - the landmine area - and nothing
  well-typed is ever routed there.
- O6 no accessor sugar: `f.tags.title`, `a.tags.language`, required.
- R3 field access on a filter output is a typed rejection.


## 9. Waves (dependency-ordered; each is its own agent + review)

A. FOUNDATION - `sqlmpeg/types.py` holds the declarations as data
   (types, fields, field kind stream/text/number/array, W vs RO,
   which array unnests to which record). `ROW_SCHEMAS`, `_ROW_COMMON`,
   `_INPUT_COLUMNS`, `_UNNEST_COLUMNS`, `_STREAM_ARRAY_COLUMNS`,
   `INPUT_TAG_COLUMNS`, `ROW_STREAM_COLUMN`, `CHAPTERS_COLUMN`,
   `INPUT_DURATION_COLUMN` become views over it and stop being
   independent sources of truth. ZERO behavior change: every existing
   test passes untouched. Acceptance: 2256 default + 213 exec green
   with no test file edited.

B. RENAMES - `.track` and `f.frame` leave the language. `a.track` ->
   `a` (the row IS the stream), `f.frame` -> `f.video[1]`. Both become
   typed rejections with hints naming the replacement. Sweep: ~38 + 30
   cookbook references, 18 query files, ~850 test references. Every
   pinned ffmpeg command byte-identical.

C. MAPS - `tags tag[]` replaces the twelve container tag columns and
   the per-stream `language`/`title` fields; read by path
   (`f.tags.title`, `a.tags.language`), written as today's tag columns
   (`'eng' AS language` constructs the entry, `NULL` clears).
   `disposition flag[]` with the closed ffmpeg key set, read
   `a.disposition.forced`, written from set flags; the reserved
   tag-key special case disappears.

D. RULES - `SELECT *` per R2 (container: array columns; row: fields),
   field access on a filter output rejected (R3), RO-field
   construction rejected (R4), constructed-chapter constraints (O2),
   attachments never routed to `f.data` (O4).

Then: orchestrator docs - this file becomes docs/types.md, rows.md
shrinks to point at it, dialect.md and the prompt follow, release.

## After this lands
094 (literals, output columns, cues), then 096 (functions: scalar and
table-returning, with RETURNS over these types).
