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

## 2. Handles - a reference to a stream in the graph

    video, audio, subtitle, data

A handle is what filters consume and produce, and what a `-map` maps.
It carries its riding tags (provenance: language/title that survive
through filters) but no probed facts. A filter call's type is the
handle type of its output pad: `volume(x) : audio`, `overlay(v, w) :
video`. Subtitle and data handles are passthrough-only (no filter
accepts them).

## 3. Stream records - what a row about a stream carries

    CREATE TYPE video_stream AS (
        track          video,      -- the handle
        index          number,     -- RO  1-based, agrees with f.video[k]
        language       text,       -- W   tag
        title          text,       -- W   tag
        disposition    text,       -- W   ffmpeg disposition spec
        codec          text,       -- RO
        width          number,     -- RO
        height         number,     -- RO
        fps            text,       -- RO  verbatim, e.g. 30000/1001
        color_transfer text,       -- RO
        bitrate        number,     -- RO
        duration       number);    -- RO

    CREATE TYPE audio_stream AS (
        track          audio,
        index          number,     -- RO
        language       text,       -- W
        title          text,       -- W
        disposition    text,       -- W
        codec          text,       -- RO
        channels       number,     -- RO
        sample_rate    number,     -- RO
        channel_layout text,       -- RO
        bitrate        number,     -- RO
        duration       number);    -- RO

    CREATE TYPE subtitle_stream AS (
        track subtitle, index number, language text, title text,
        disposition text, codec text);   -- index/codec RO

    CREATE TYPE data_stream AS (
        track data, index number, language text, title text,
        disposition text, codec text);   -- index/codec RO

W = writable: an assertion the query may make; emitted as that
stream's tag (`-metadata:s:N`, `-disposition:N`). RO = read-only: a
probed fact; a constructed record that sets one is a typed rejection.
Every W field is nullable; NULL clears.

DECISION (maintainer): `disposition` joins the record as a W field
(today it is a reserved tag key with no read side). Reading it back
from ffprobe's disposition flags is cheap and makes the record
symmetric.

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
        start_t number,   -- RO
        end_t   number,   -- RO
        text    text);    -- RO   (cues are read, not written, v1)

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
        frame       video,             -- sugar: video[1].track
        duration    number,            -- RO
        title       text,              -- W   container tags:
        artist      text,              -- W
        album       text,              -- W
        album_artist text,             -- W
        date        text,              -- W
        genre       text,              -- W
        comment     text,              -- W
        composer    text,              -- W
        track       text,              -- W
        copyright   text,              -- W
        encoder     text,              -- W
        description text);             -- W

`input('path') f` is a table of ONE `container` row. `t` is not a
field: it is the seek handle, legal only in `WHERE` trim windows.

A generated source (`ffmpeg.sine(...) s`) is a `container` whose one
array holds one record: the handle set, every fact NULL, the other
arrays empty.

## 6. Rules

R1. `unnest(f.<array>) a` turns `T[]` into rows of `T`. Works for the
    four stream arrays, `chapters`, `attachments`.
R2. Stream records are ACCEPTED wherever a stream is expected
    (subsumption, one direction). Why the rule exists: `f.audio` must
    be `audio_stream[]` or `unnest(f.audio)` has no metadata, so by
    SQL's array rule `f.audio[1]` is a record - yet `SELECT
    f.audio[1]`, `scale(f.audio[1], ...)` and the splat `SELECT
    f.audio` all need a stream, and SQL has no way to map `.track`
    over an array. The alternatives are worse: two parallel arrays
    per kind, or `.track` everywhere with no splat.
    The rule, exactly:
    - Where the checker expects a stream (a media COPY's SELECT
      columns, filter arguments, array_agg's argument, COALESCE
      branches, UNION ALL branch columns, CTE columns consumed as
      streams), a stream record or an array of them is accepted; its
      handle is used, elementwise for arrays.
    - Nothing is invented: the handle is a field of the record.
    - Nothing is lost: the record's non-NULL W fields stay attached
      to the handle as riding tags, which is how tags already survive
      filters. `SELECT a` and `SELECT a.track` produce identical
      output, tags included.
    - RO facts are not consulted; they describe the source, and a
      consumer of streams has no use for them.
    - It NEVER applies in value positions: WHERE, CASE, `||`, tag
      expressions, fan-out TO, GROUP BY keys. A record where a value
      is expected is a typed rejection, never a silent `.track`.
    - The converse is false: a handle is not a record. A filter
      output or `f.frame` has no fields (R3).
R3. Field access: `f.audio[1].language`, `a.language`,
    `(f.audio[1]).language` all read the record field. On a handle
    (filter output, `f.frame`) there are no fields - typed rejection.
R4. Construction:
    - narrow records (chapter, attachment): positional
      `ROW(...)::chapter`, or `ARRAY[ROW(...)::chapter, ...]`.
    - stream records: by SELECT aliases in a function body or CTE -
      `SELECT a.audio[1] AS track, 'eng' AS language` builds an
      `audio_stream` with `track` and `language` set and every other
      field NULL. Positional ROW() for stream records is a typed
      rejection (too wide to be safe).
    - Setting an RO field in any construction is a typed rejection.
R5. Tags ARE writable fields. Today's "tag column" is exactly R4's
    alias construction applied to a row - the two stories merge. A
    record reaching an output emits its non-NULL W fields.
R6. The OUTPUT row is positional streams + container scalars +
    record arrays (`chapters`, `attachments`). It is NOT a `container`:
    named stream arrays (`... AS audio`) are rejected on output -
    streams are positional, one way to say it.
R7. Nullability is SQL's: an outer join's gap side is a NULL record;
    `COALESCE(b.track, ffmpeg.anullsrc(...))` produces a handle.
R8. CTE/view rows have the body's column types (an anonymous row
    type), as in SQL. A function's `RETURNS` names one of the types
    above, an array of one, or `TABLE(...)` of them.
R9. Homonyms: type names and column/alias names are separate
    namespaces (SQL's rule). `f.audio` is an `audio_stream[]`, not an
    `audio`. Type names are legal as aliases but discouraged in docs.

## 7. What the implementation derives from this

- `sqlmpeg/types.py`: the declarations as data. `ROW_SCHEMAS`,
  `_INPUT_COLUMNS`, `_UNNEST_COLUMNS`, `_STREAM_ARRAY_COLUMNS`,
  `INPUT_TAG_COLUMNS`, the tag-key handling, and the per-field
  writability all become views over it.
- Filter pad checks = handle-type checks; UDF_ARG_TYPE speaks
  "expected audio, got video".
- Error hints, docs/rows.md tables, and the LLM prompt's column
  sections render from the same data.
- No behavior change intended beyond: `disposition` readable (new),
  RO-field construction rejected (new), positional ROW for stream
  records rejected (new). Every pinned recipe byte-identical.

## 8. Open for the maintainer

O1. `disposition` as a readable W field (section 3) - yes/no.
O2. Record-by-alias construction requiring `track` to be set, or may
    a record be all-NULL (a "placeholder")? Proposed: `track` required
    for stream records; chapters need start_t/end_t.
O3. Container W fields: the twelve tags listed, or any free-form key?
    Today tag columns accept free-form keys on output. Proposed:
    free-form stays for OUTPUT (ffmpeg accepts any key), the twelve
    are the READABLE ones.
O4. Attachment read side: ffprobe reports attachments as streams
    (codec_type attachment, filename/mimetype tags). Proposed: they
    populate `f.attachments`, never `f.data`.

## After this lands
094 (literals, output columns, cues), then 096 (functions: scalar and
table-returning, with RETURNS over these types).
