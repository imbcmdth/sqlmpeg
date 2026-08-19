# Track rows

`unnest` turns a track array into rows with the probed metadata as columns; `WHERE`, `ORDER BY`, and `JOIN` then select, sort, and align tracks by what they are instead of by index.

Everything on this page evaluates at **compile time**. The columns are ffprobe results, so no join, filter, or sort reaches the ffmpeg command - only the wiring they decided. Inputs must be probeable (typed rejection otherwise).

## The shape of the model

An `input()` alias is one row with four array columns - `video`, `audio`, `subtitle`, `data` - the shape of a container file. `unnest` goes from that to rows. No re-nesting on output: the sink serializes the result set into a flat stream list (a container) or lines (CSV).

## Rows and columns

`unnest(<alias>.audio)` (or `.video`, `.subtitle`, `.data`) in `FROM` yields one row per track. Alias mandatory; the argument is a bare array of an input declared earlier in the same FROM list:

```sql
SELECT t.track
FROM input('film.mkv') f, unnest(f.audio) t
WHERE t.language = 'eng' AND t.channel_layout = 'stereo'
```

| column | audio | video | subtitle/data |
| --- | --- | --- | --- |
| `track` | the stream | the stream | the stream (passthrough-only) |
| `index` | 1-based, agrees with `f.audio[1]` | same | same |
| `language`, `title` | tags | tags | tags |
| `codec` | yes | yes | yes |
| `channels`, `channel_layout`, `sample_rate` | yes | - | - |
| `width`, `height`, `fps`, `color_transfer` | - | yes | - |
| `bitrate`, `duration` | yes | yes | - |

Unreported fields are NULL with SQL semantics: NULL matches nothing (`=` and `!=` both fail); use `IS [NOT] NULL`.

Subscripts reach the same columns without unnest: `f.audio[1].language` (or the strict-Postgres `(f.audio[1]).language`; `f.audio[1]` is sugar for `f.audio[1].track`). In a `WHERE` this is an **assertion** - the subscript names one track, so a false predicate refuses to compile. See [recipe 29](examples.md#29-assert-what-youre-shipping).

`ORDER BY` over row columns re-sorts tracks (multi-key, Postgres NULL placement). Allowed only on row queries; frames never sort. Without it, rows keep file order - track order is player-visible and is never changed implicitly.

## Joins

```sql
SELECT amix(a.track, b.track)
FROM input('film.mkv') f, input('commentary.mkv') g,
     unnest(f.audio) a JOIN unnest(g.audio) b ON a.language = b.language
```

- `INNER`, `LEFT`, `FULL OUTER` between unnest tables; comma between them is a cross join. Joins anywhere else stay rejected.
- Result order: the left side's track order; a FULL join appends unmatched right rows after, in their order.
- Real join multiplicity: one row matching two pairs with both. To pair a 5.1 and a stereo English track separately, widen the key: `ON a.language = b.language AND a.channel_layout = b.channel_layout`.
- ON takes the same compile-time grammar as WHERE (`=`, `!=`, `<`, `>`, `<=`, `>=`, `BETWEEN`, `IS [NOT] NULL`, `AND`/`OR`/`NOT`), column vs column or literal, statically type-checked.

## Fills

An outer join's gap side has a NULL `track`. Selecting it bare is a typed rejection; fill it with `COALESCE`, by stream type:

- **Audio: `ffmpeg.anullsrc(...)`** - silence. `duration` inherits from the paired track when omitted; no probed or written duration anywhere is a rejection (an unbounded generator would hang concat). Nothing else is injected.
- **Video: `ffmpeg.color(...)`** - black by default; `size`, `rate`, `duration` inherit from the paired row when omitted.
- **Captions: `sqlmpeg.empty_captions()`** - a zero-cue subtitle track, emitted as one extra self-contained `data:`-URI input. No cues are generated.

Fills carry the paired row's tags, so a silence-filled French slot still emits `-metadata:s:N language=fra`.

To inspect rows before acting, use a table query (SELECT with no COPY) or CSV - [recipes 30-32](examples.md#30-look-at-a-files-tracks-as-a-table). The concat-with-fill pattern is [recipe 27](examples.md#27-concatenate-files-with-different-track-counts); recipes 23-28 cover the rest of this surface.

## Fences

Row tables need probeable inputs. Metadata columns as outputs are legal only in table/CSV queries, not media queries. `track` is not usable inside ON. Aggregates and `GROUP BY` stay rejected.
