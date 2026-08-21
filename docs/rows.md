# Row shapes

Every FROM item is a compile-time table with a fixed shape. This page lists each shape, its columns, and what each column is for. Column values are ffprobe results, so inputs must be probeable wherever a metadata column is read (typed rejection otherwise); everything here evaluates at compile time - no filter, join, or sort reaches the ffmpeg command, only the wiring they decided.

Column types: **stream** is selectable as output; **text** and **number** are values - usable in `WHERE`/`ON`, compile-time expressions (`CASE`, `||`, `::text`, `+ - * /`), tag columns, and table/CSV output, never as streams. Unreported values are NULL with SQL semantics: `=` and `!=` both fail against NULL; use `IS [NOT] NULL`.

## Input rows - `input('film.mkv') f`

One row per input: the shape of a container file. Arrays of streams plus the container's own scalars.

| column | type | notes |
| --- | --- | --- |
| `video`, `audio`, `subtitle`, `data` | stream array | splat (`f.audio` = every track), subscript (`f.audio[1]`, 1-based), or `unnest` into track rows. `subtitle`/`data` are passthrough-only |
| `chapters` | record array | `unnest` into chapter rows; no splat, no subscript. Bare, it prints as one array cell |
| `t` | timeline | only in `WHERE` trim windows: `f.t BETWEEN 5 AND 60`, either bound alone, or against chapter bounds |
| `duration` | number | probed container duration in seconds |
| `title`, `artist`, `album`, `album_artist`, `date`, `genre`, `comment`, `composer`, `track`, `copyright`, `encoder`, `description` | text | the container's tags; NULL when the file doesn't carry the key |

Subscripts reach track-row columns without unnest: `f.audio[1].language` (strict-Postgres `(f.audio[1]).language` also parses). In a `WHERE` this is an **assertion** - the subscript names one track, so a false predicate refuses to compile ([recipe 29](examples.md#29-assert-what-youre-shipping)).

## Track rows - `unnest(f.audio) t`

One row per track. The argument is an array column of an input declared earlier in the same FROM list; alias mandatory. All five array columns unnest - the four stream arrays here, and `chapters` below. The schema varies by stream type:

The row IS the stream: a bare `t` where a stream is expected selects it, filters it, or gathers it. The columns below are the metadata ABOUT it.

| column | type | audio | video | subtitle | data |
| --- | --- | --- | --- | --- | --- |
| `index` | number | yes | yes | yes | yes |
| `language`, `title`, `codec` | text | yes | yes | yes | yes |
| `channels`, `sample_rate` | number | yes | - | - | - |
| `channel_layout` | text | yes | - | - | - |
| `width`, `height` | number | - | yes | - | - |
| `fps` | text | - | verbatim, e.g. `30000/1001` | - | - |
| `color_transfer` | text | - | yes | - | - |
| `bitrate`, `duration` | number | yes | yes | - | - |

`index` is 1-based and agrees with the subscript: `WHERE t.index = 1` and `f.audio[1]` name the same track.

`WHERE` over row columns filters tracks; `ORDER BY` re-sorts them (multi-key, Postgres NULL placement) - without it, rows keep file order, which is player-visible and never changed implicitly. Both take the compile-time predicate grammar: `=`, `!=`, `<`, `<=`, `>`, `>=`, `BETWEEN`, `IS [NOT] NULL`, `AND`/`OR`/`NOT`, statically type-checked.

## Chapter rows - `unnest(f.chapters) c`

The same shape as track rows, over the container's chapter list. A chapter is not a stream, so a bare `c` selects nothing and nothing here reaches a media query; the columns feed trim windows (`WHERE f.t BETWEEN c.start_t AND c.end_t`), fan-out destinations, tag columns, and table/CSV output. Chapter rows cross join with track rows like any other pair of sources.

| column | type | notes |
| --- | --- | --- |
| `index` | number | ffprobe's chapter order, 1-based |
| `title` | text | |
| `start_t`, `end_t` | number | seconds |

Writing chapters is the reverse shape: a `VALUES` CTE with exactly these columns handed to the sink - `WITH marks(start_t, end_t, title) AS (VALUES ...) ... WITH (chapters marks)` - compiles to one extra self-contained input carrying the list; `chapters_from <alias>` copies an input's chapters through instead. Recipes [39-40](examples.md#39-list-a-files-chapters).

## CTE rows - `WITH x AS (...)`

A CTE exposes whatever its body named with `AS`, and referencing it in FROM contributes its body's ROWS - a two-row CTE is a two-row source, and comma between sources is a cross join with real multiplicity, exactly as SQL says. Tag columns in the body ride on its streams (see Tags below). No other columns exist on a CTE alias; there is no natural naming from a bare `a`. Views referenced in FROM follow the same rules.

## Joins

```sql
SELECT array_agg(amix(a, b))
FROM input('film.mkv') f, input('commentary.mkv') g,
     unnest(f.audio) a JOIN unnest(g.audio) b ON a.language = b.language
```

- `INNER`, `LEFT`, `FULL OUTER` between unnest tables; comma between them is a cross join. Joins anywhere else stay rejected.
- Result order: the left side's track order; a FULL join appends unmatched right rows after, in their order.
- Real join multiplicity: one row matching two pairs with both. To pair a 5.1 and a stereo English track separately, widen the key: `ON a.language = b.language AND a.channel_layout = b.channel_layout`.
- `ON` takes the same grammar as `WHERE`, column vs column or literal. A bare row alias is a stream, not a value to compare, so it is not usable inside `ON`.

An outer join's gap side is a NULL row. Selecting it bare is a typed rejection; fill it with `COALESCE`, by stream type: **audio** `ffmpeg.anullsrc(...)` (silence; `duration` inherits from the paired track when omitted, and no duration anywhere is a rejection), **video** `ffmpeg.color(...)` (black by default; `size`/`rate`/`duration` inherit), **captions** `sqlmpeg.empty_captions()` (a zero-cue subtitle track as one extra `data:`-URI input). Fills carry the paired row's tags, so a silence-filled French slot still emits `-metadata:s:N language=fra`. The pattern is [recipe 27](examples.md#27-concatenate-files-with-different-track-counts).

## Tags

An aliased non-stream SELECT column is a metadata tag; the alias is the key (free-form; quoted identifiers for unusual keys), the value any compile-time expression, `NULL` clears the key. The scope is the row shape it sits over:

- **Over track rows**: tags that row's stream(s) - `-metadata:s:N`. `CASE WHEN t.language IN ('en', 'english') THEN 'eng' ELSE t.language END AS language` retags a library in one expression. Unselected tags pass through unchanged. Recipes [37-38](examples.md#37-retitle-tracks-from-their-own-metadata).
- **Over input rows only** (no track rows in the branch): tags the container - `-metadata`. The input's own tag columns feed the expressions, so `CASE WHEN f.title IS NULL THEN 'Untitled' ELSE f.title END AS title` fills a missing title. [Recipe 52](examples.md#52-read-the-containers-tags-rewrite-them-with-case).
- **Both in one query**: layer with a CTE - tag columns in the body are per-stream, in the outer SELECT container-level, and the outer SELECT gathers the CTE's rows (`array_agg` + `GROUP BY`, see Combining rows); the outer value wins on a shared key. [Recipe 53](examples.md#53-tag-the-tracks-and-the-container-in-one-query).

`disposition` is a reserved key: its value is ffmpeg's disposition spec (`'default'`, `'forced'`, `'default+forced'`, `'0'` clears) and it emits `-disposition:N` instead - [recipe 41](examples.md#41-flag-the-default-track). `metadata_from <alias>` copies an input's global tags, `strip_metadata true` drops them; a tag column overrides either for its key. The same columns in a table/CSV query print as plain data, which previews what a retag will write.

## Combining rows

Four rules, no exceptions:

1. A query produces a relation. A bare SELECT prints it; COPY serializes it. Same relation.
2. A single destination takes exactly ONE row - any container, manifests included. Rows combine only when written: `array_agg` gathers a column's streams in row order, `GROUP BY` names what stays constant (an aggregate with no `GROUP BY` is one group, Postgres's own rule).
3. `TO (expression over row columns)` writes one file per row - rule 2, applied N times.
4. A multi-row relation into a single path is a compile error (`ROW_COUNT_MISMATCH`) naming the row count, the destination, and both ways out.

```sql
COPY (
  SELECT f.video, array_agg(a)
  FROM input('film.mkv') f, unnest(f.audio) a
  GROUP BY f.video
) TO 'out.mp4'
```

The row count is the RESOLVED count against the actual file: a `WHERE` that narrows a row table to one row needs no aggregate. Queries with only input aliases in FROM are one row - arrays are values inside it, so splats, subscripts, and `SELECT *` never need gathering.

`array_agg` takes any per-row stream expression (`array_agg(volume(a, 0.5))`) and must be a whole SELECT column; row order is the aggregation order (`ORDER BY` before the aggregate reorders it; `ORDER BY` inside `array_agg` is rejected). Postgres's grouping rule is enforced: outside an aggregate, a row-varying expression must match a `GROUP BY` key. Group keys may be streams (`GROUP BY vid`, `GROUP BY f.video[1]`).

`GROUP BY` a row column partitions the rows, one output file per group - this requires a fan-out `TO (expression over the group keys)` (N groups are N rows; rule 2). Group keys are group-constants, so they double as container tag columns. [Recipe 55](examples.md#55-one-file-per-language-all-its-tracks-inside) writes one file per language with all of that language's tracks inside, titled by its key; [recipe 57](examples.md#57-combine-tracks-selected-by-separate-ctes) gathers across CTE boundaries.

## Inspecting

Any of these shapes prints as a table with a bare SELECT (no COPY), or as CSV with `COPY ... TO STDOUT WITH (format 'csv')` - [recipes 30-32](examples.md#30-look-at-a-files-tracks-as-a-table). A bare input array column (`f.audio`, not subscripted) prints as one cell, Postgres array-literal style - `{<audio 0:a:0>,<audio 0:a:1>}`, braces even for one element; a subscript (`f.audio[1]`) or a bare track row (`t`) still prints its plain `<audio 0:a:0>` placeholder. `f.chapters` prints the same way, its records parenthesized in schema order: `{(1,Intro,0.0,1.0),(2,Credits,1.0,2.0)}`.

`GROUP BY` and `array_agg` are legal here too - table mode has no destination to fan out over, so every group just prints as one row, in first-appearance order, `array_agg` an array cell of the group's tracks. It is how you preview a fan-out COPY's partitions before writing any file - [recipe 56](examples.md#56-preview-a-grouped-shape-as-a-table).
