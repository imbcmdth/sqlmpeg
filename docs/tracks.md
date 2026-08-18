# Track rows

Every real library has the file with the tracks in the wrong order, the one with two English audios, and the pair that should concatenate except one of them never got a French dub. Fixing those by counting streams in ffprobe output is exactly the bookkeeping SQL was built to delete, so sqlmpeg gives you the real SQL tools: `unnest` turns a track array into rows, the probed metadata becomes columns, and selection, ordering, and alignment become `WHERE`, `ORDER BY`, and `JOIN`.

One thing to hold onto: **everything on this page happens at compile time.** The columns are ffprobe results, so every predicate is decidable while compiling, and no join, filter, or sort survives into the ffmpeg command. What ffmpeg receives is the wiring these decisions produced, the same way a `WHERE t BETWEEN` window vanishes into `-ss`/`-to`. Nothing here decodes a frame, and none of it works on an input ffprobe cannot read (that is a typed rejection, not a guess).

## Rows and columns

`unnest(<alias>.audio)` (or `.video`, `.subtitle`, `.data`) in `FROM` makes a table with one row per track. It needs an alias, like every table in the dialect, and its argument must be a bare array of an input declared earlier in the same FROM list - stock Postgres scoping, where a function call in FROM may reference the tables before it:

```sql
SELECT t.track
FROM input('film.mkv') f, unnest(f.audio) t
WHERE t.language = 'eng' AND t.channel_layout = 'stereo'
```

| column | audio | video | subtitle/data |
| --- | --- | --- | --- |
| `track` | the stream itself | the stream itself | the stream itself (passthrough-only, as captions are everywhere) |
| `index` | 1-based, agrees with `f.audio[1]` | same | same |
| `language`, `title` | tags | tags | tags |
| `codec` | codec name | codec name | codec name |
| `channels`, `channel_layout`, `sample_rate` | yes | - | - |
| `width`, `height`, `fps`, `color_transfer` | - | yes | - |
| `bitrate`, `duration` | yes | yes | - |

A field ffprobe didn't report is NULL, and NULL behaves the way SQL says it behaves: it equals nothing, it compares to nothing, and `WHERE` drops the row. An untagged track simply never matches `t.language = 'eng'` - and never matches `t.language != 'eng'` either. `IS NULL` / `IS NOT NULL` ask the question directly.

`ORDER BY` over row columns re-sorts the tracks (multi-key, `NULLS FIRST`/`LAST` honored, Postgres defaults). It is allowed only here, where the rows are compile-time metadata - frames still never sort, and the `NO_STREAMING_EQUIVALENT` fence on everything else hasn't moved. Without an `ORDER BY`, rows keep the file's own track order. That default is deliberate: track order is player-visible surface (players open the first audio track), so nothing reorders it behind your back.

## Joins: aligning tracks across files

Two multi-language files, every track mixed with its counterpart, whatever order each file stores them in - that is an inner join, written the way Postgres writes it:

```sql
SELECT amix(a.track, b.track)
FROM input('film.mkv') f, input('commentary.mkv') g,
     unnest(f.audio) a JOIN unnest(g.audio) b ON a.language = b.language
```

Result rows follow the LEFT side's track order (unmatched right rows, in a `FULL` join, append after in their own order), so the output track order is `f`'s. When one file carries two English tracks - a 5.1 and a stereo - that is not an error: real join semantics pair the one against both, and the fix, when you didn't want that, is a wider key:

```sql
ON a.language = b.language AND a.channel_layout = b.channel_layout
```

`INNER`, `LEFT`, and `FULL OUTER` joins are supported between unnest tables (a comma between them is the cross join, bounded and occasionally useful). Joins anywhere else in the dialect remain rejected; input-level FROM stays the comma cross-join it has always been. ON predicates take the same compile-time grammar WHERE does: `=`, `!=`, `<`, `>`, `<=`, `>=`, `BETWEEN`, `IS [NOT] NULL`, `AND`/`OR`/`NOT`, column against column or column against literal, type-checked statically (`a.language = b.channels` is rejected as never-matchable, not silently false).

## Fills: what stands in for a missing track

An outer join keeps rows only one side has, and the gap side's `track` is NULL. Selecting it bare is a typed rejection naming the row and the key that failed to match; `COALESCE` is the accepted spelling, and what you coalesce WITH depends on the stream type:

- **Audio: `ffmpeg.anullsrc(...)`** - silence. When you omit `duration`, it inherits the paired track's probed duration, because a stand-in for a 2-second track should be 2 seconds long; an unbounded generator with no duration anywhere is rejected rather than hanging your concat. Nothing else is injected - what you write is what compiles.
- **Video: `ffmpeg.color(...)`** - a canvas, black by default, inheriting `size`, `rate`, and `duration` from the paired row the same way.
- **Captions: `sqlmpeg.empty_captions()`** - an EMPTY subtitle track: it exists, it takes the paired row's language tag, and it contains zero cues, because nobody generates your subtitles for you. It costs one extra input in the command, a self-contained `data:` URI holding a bare WEBVTT header - no file on disk.

A fill carries the paired row's tags as provenance, so a silence-filled French slot still emits `-metadata:s:N language=fra` and players still see a French track.

The founding use case ties it together - concatenating two files where one lacks a track. `concat` demands identical segment shapes, so each `UNION ALL` branch runs the same outer join and selects its own side, fills included. The worked version, byte-checked against the real compiler, is [cookbook recipe 27](examples.md#27-concatenate-files-with-different-track-counts); recipes 23 through 28 cover the rest of this page's surface the same way.

## The fences, briefly

Row tables need probeable inputs. Metadata columns are not selectable as outputs (streams are the only outputs) and `track` is not usable inside ON (keys are metadata, not streams). Aggregates, `GROUP BY`, and friends stay rejected. And nothing on this page changes what reaches ffmpeg: by the time the command exists, every row, join, and fill has already been decided.
