# 095 — A declared type system

Maintainer directive (2026-08-21): stop relying on ad-hoc shapes;
declare the types once and derive everything from them. Must land
BEFORE 094 (chapter/attachment literals) and before the function work,
or each invents type machinery locally.

Evidence the ad-hoc shapes cost real money: three registries must
agree by hand (`_INPUT_COLUMNS`, `_UNNEST_COLUMNS`, `ROW_SCHEMAS`),
and 092 had to split `_UNNEST_COLUMNS` because "array column" and
"array of streams" were one frozenset by accident.

## The vocabulary (built-in, documented; users never declare them)

Handles - opaque references to a graph node, typed by media kind, the
things filters consume:

    video, audio, subtitle, data

Records - what a row of each kind carries:

    video_stream(track video, index number, language text,
                 title text, codec text, width number, height number,
                 fps text, color_transfer text, bitrate number,
                 duration number)
    audio_stream(track audio, index number, language text,
                 title text, codec text, channels number,
                 sample_rate number, channel_layout text,
                 bitrate number, duration number)
    subtitle_stream(track subtitle, index, language, title, codec)
    data_stream(track data, index, language, title, codec)
    chapter(index number, title text, start_t number, end_t number)
    attachment(...)   -- 094
    cue(...)          -- 094

The container - the type of an INPUT ROW:

    container(video video_stream[], audio audio_stream[],
              subtitle subtitle_stream[], data data_stream[],
              chapters chapter[], attachments attachment[],
              frame video, duration number,
              title text, artist text, album text, album_artist text,
              date text, genre text, comment text, composer text,
              track text, copyright text, encoder text,
              description text)

`t` stays a pseudo-column (a seek handle, not a field).

## Rules

- `input(...) f` is a table of ONE `container` row. `unnest(f.audio)`
  is `audio_stream[]` -> `audio_stream` rows. Everything in rows.md
  becomes a consequence of the declarations, not prose.
- ONE implicit coercion, declared deliberately: a RECORD in stream
  position means its `.track` handle. That is what keeps
  `SELECT f.audio` splatting into maps. No other implicit conversion.
- Filter pad checking becomes type checking against handle types;
  UDF_ARG_TYPE messages speak the same vocabulary as every other
  error ("expected audio, got video").
- Casts to these type names parse today (verified: `::audio_stream`,
  `::audio_stream[]`, `CREATE TYPE ... AS (...)`), so 094's literals
  and 095's function RETURNS clauses drop straight in.

## Implementation shape

- One `sqlmpeg/types.py`: the declarations as data (name -> fields,
  field -> type), plus the handle/record/array relationships.
  `ROW_SCHEMAS`, `_INPUT_COLUMNS`, `_UNNEST_COLUMNS`,
  `_STREAM_ARRAY_COLUMNS`, `INPUT_TAG_COLUMNS` all become views over
  it - deleted as separate sources of truth.
- parser/lower consult the registry instead of frozensets; error
  hints render from it (so a new column is documented automatically).
- The LLM prompt renders the type tables from the same registry, the
  way it already renders sink/input options.
- No behavior change is intended. Error MESSAGE text will move where
  it names shapes; that is the whole point, but every message change
  must be deliberate and the goldens regenerated with eyes on the
  diff.

## Checks
ruff, mypy, both suites; every pinned recipe byte-identical (this is a
refactor, not a feature). Goldens regenerated only for messages whose
wording intentionally moved.

## After this lands
094 (chapter/attachment/cue literals + output columns), then the
function work (scalar and table-returning), which gets
`RETURNS audio_stream[]` for free.
