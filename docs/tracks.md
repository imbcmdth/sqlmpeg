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

## Editing tags

In a media query over track rows, a non-stream column sets a tag on that row's output stream(s). The alias is the tag key (free-form; quoted identifiers for unusual keys), the value is any compile-time expression over the row - literals, row columns, `CASE`, `||` concatenation - and `NULL` clears the key. Unselected tags pass through unchanged. Row-scoped: the tag applies to every stream the row carries.

```sql
SELECT t.track,
       CASE WHEN t.language IN ('en', 'english') THEN 'eng' ELSE t.language END AS language,
       'Audio (' || t.language || ')' AS title
FROM input('film.mkv') f, unnest(f.audio) t
```

Compiles to `-metadata:s:N` flags only - no filter nodes. The same columns in a table/CSV query print as plain data, which previews what a retag will write. `disposition` is a reserved key: its value is ffmpeg's disposition spec (`'default'`, `'forced'`, `'default+forced'`, `'0'` clears) and it emits `-disposition:N` instead - [recipe 41](examples.md#41-flag-the-default-track). Container-level tags are not per-row: in a query with no track rows the same aliased columns tag the CONTAINER (`'Remastered' AS title`, `NULL AS artist` clears), and the input's own tags are readable as columns on its alias - `f.title`, `f.artist`, `f.comment` and the other common keys, NULL when the file lacks them. `metadata_from <alias>` copies an input's global tags, `strip_metadata true` drops them; a tag column overrides either for its key. Recipes [37-38](examples.md#37-retitle-tracks-from-their-own-metadata) are the worked versions.

To do both in one query, layer them with a CTE: tag columns in the CTE body are per-stream (its rows are tracks), tag columns in the outer SELECT are container-level (the CTE's output is streams). If the outer query re-tags a key a CTE already set, the outer value wins. [Recipe 53](examples.md#53-tag-the-tracks-and-the-container-in-one-query).

## Grouping: the aggregation made explicit

A multi-row media query writing one file implicitly aggregates: the destination is the group key, row streams are gathered in row order. Both halves can be written out - `GROUP BY` and `array_agg` are legal over track rows, and the explicit form compiles to the same bytes as the sugar:

```sql
COPY (
  SELECT f.video, array_agg(a.track)
  FROM input('film.mkv') f, unnest(f.audio) a
  GROUP BY f.video
) TO 'out.mp4'
```

`array_agg` takes any per-row stream expression (`array_agg(volume(a.track, 0.5))`) and must be a whole SELECT column; row order is the aggregation order (`ORDER BY` before the aggregate reorders it; `ORDER BY` inside `array_agg` is rejected). Postgres's grouping rule is enforced: outside an aggregate, a row-referencing expression must match a `GROUP BY` key.

`GROUP BY` a row column partitions the rows, and each group is one output file - this requires a fan-out `TO (expression over the group keys)` and is how rows *share* a destination (the ungrouped fan-out still rejects two rows naming one file). Group keys are group-constants, so they double as container tag columns. [Recipe 55](examples.md#55-one-file-per-language-all-its-tracks-inside) writes one file per language with all of that language's tracks inside, titled by its key.

## Chapters

`chapters(f)` in FROM is a row table - `index`, `title`, `start_t`, `end_t` per chapter, from the container. It composes with WHERE/ORDER BY and table/CSV output; chapters are not streams, so selecting them into a media query is rejected.

Writing: define chapters with a `VALUES` CTE and hand it to the sink - `WITH marks(start_t, end_t, title) AS (VALUES ...) COPY (...) TO 'out.mkv' WITH (chapters marks)`. Compiles to one extra self-contained input carrying the chapter list; `chapters_from <alias>` copies an input's chapters through instead. Recipes [39-40](examples.md#39-list-a-files-chapters).

## Fences

Row tables need probeable inputs. Metadata columns as outputs are legal in table/CSV queries and as tag columns in row-table media queries; a media query with no row tables keeps the old rejection. `track` is not usable inside ON. Aggregates and `GROUP BY` stay rejected.
