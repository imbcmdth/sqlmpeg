# Types

Every column, value and stream in the dialect has a declared type, and
error messages name them. This page is the vocabulary; the per-shape
column tables are in [rows.md](rows.md).

## The kinds

| kind | types | what it is |
| --- | --- | --- |
| scalar | `text`, `number`, `boolean` | a compile-time value. `number` follows Postgres typing (int/int truncates); `boolean` comes from a flag map and stands alone as a predicate |
| stream record | `video_stream`, `audio_stream`, `subtitle_stream`, `data_stream` | one track: **the record IS the stream**, plus the metadata about it |
| record | `chapter` (`attachment`, `cue` coming) | data the container carries that is not a stream |
| map | `tag`, `flag` | key/value pairs read by path, never unnested |
| container | `container` | one input file: its stream arrays, its chapter list, its scalars |
| array | `T[]` | `unnest` turns it into rows of `T` |

`input('film.mkv') f` is a table of ONE `container` row.
`unnest(f.audio) a` is rows of `audio_stream`.

## The record is the stream

A stream record is what filters take and return, what `-map` maps, and
what `SELECT a` selects. The graph node behind it has no name in the
language. Two records are the same stream or they are not: identity is
which stream, never a field-by-field comparison, so `GROUP BY a` keeps
two tracks apart even when every probed column agrees.

A filter's output is a stream whose fields were never probed, so it has
none to read: `scale(v, 640, -2).width` is a typed rejection, not a
NULL. Read the field on what goes in.

## Writable and read-only fields

Every field is one or the other, and the distinction is enforced:

- **Writable** — an assertion your query may make: a stream's `tags`
  and `disposition`, a container's `tags`, a chapter's `title`,
  `start_t`, `end_t`. Written with a tag column (`'eng' AS language`);
  `NULL` clears.
- **Read-only** — a probed fact: `index`, `codec`, `width`, `height`,
  `fps`, `channels`, `sample_rate`, `channel_layout`, `bitrate`,
  `duration`, `color_transfer`. Setting one is a typed rejection
  naming the field as probed.

The reserved names are the read-only fields **of the record the column
sits over**, so `1920 AS width` is rejected over a video row and is an
ordinary tag over an audio row, which has no `width`. Every other alias
is a free-form tag key.

## Maps: tags and disposition

`tags` (free-form keys) and `disposition` (ffmpeg's closed flag set)
are maps, read by path:

```sql
SELECT t.tags.language, t.tags.title, t.disposition.forced
FROM input('film.mkv') f, unnest(f.audio) t
```

An absent key reads NULL. A disposition key outside ffmpeg's set is a
typed rejection with a did-you-mean. Naming a map without a key
(`t.tags`) is a value-position rejection, though it prints as one cell
of `(key,value)` records in a table query - handy for seeing every tag
a file carries.

Writing keeps the tag-column spelling: `'eng' AS language` sets that
entry, `'default+forced' AS disposition` sets the whole flag map (a
relative spec like `'+forced'` is rejected - the column sets the map,
so there is nothing to adjust), and `NULL` clears either.

Tags that ride: only `language` and `title` follow a stream through a
filter to the output. The rest describe the source.

## `SELECT *`

- Over a container: its stream arrays, video/audio/subtitle/data - the
  remux shape. In a table query the chapter list joins them.
- Over rows: the record's scalar fields, the metadata table. Map
  columns are excluded (a disposition cell is 250 characters wide);
  name them when you want them.
- Over rows in a **media** query: a typed rejection. A row's star means
  its fields, and fields are not output streams - select the row
  itself.
