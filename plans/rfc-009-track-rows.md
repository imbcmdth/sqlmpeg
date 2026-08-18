# RFC-009 — Track rows: unnest, metadata columns, compile-time joins

Status: draft 2026-08-17. SUPERSEDES RFC-008's macro surface: the join is
the composable primitive ("I'd rather things that compose"), and once it
exists the macros fail their own admission bar. RFC-008's plumbing that
survives: probe enrichment (expanded here) and the untrimmed same-path
`-i` dedup.

## The idea

`unnest(<alias>.audio)` in FROM turns a track array into a compile-time
TABLE — one row per track, one column for the stream itself plus a column
per piece of probed metadata. Metadata become real columns: usable as
join keys (multi-key!), in WHERE, in ORDER BY. Stock Postgres all the way
down (guardrail #2): `unnest`, implicit-LATERAL function calls in FROM,
`JOIN ... ON`, `COALESCE`, `ORDER BY`.

The joins never reach ffmpeg. Every column is probed metadata, known at
compile time, so a join evaluates during lowering into static routing —
the way `WHERE t BETWEEN` vanishes into `-ss`/`-to`. What flows out of a
row query is aligned stream arrays, which the existing broadcast/zip
machinery already consumes; downstream (split/emit) is untouched.

## Columns

Audio rows: `track` (the stream), `index`, `language`, `title`, `codec`,
`channels`, `channel_layout`, `sample_rate`, `bitrate`, `duration`.
Video rows: `track`, `index`, `language`, `title`, `codec`, `width`,
`height`, `fps`, `bitrate`, `duration`, `color_transfer` (the HDR
discriminator; a derived `hdr` boolean can come later if it earns it).
All verified present in `ffprobe -show_streams` (bit_rate,
channel_layout, per-stream duration included — measured 2026-08-17).

Unprobed input or absent field -> NULL. Standard SQL three-valued logic:
NULL never equals anything, so a join on missing metadata simply doesn't
match, and WHERE drops the row. Honest, and no new rules to learn.

## What it buys, in queries

Track selection by metadata (today: guess the subscript):

    SELECT t.track FROM input('film.mkv') f, unnest(f.audio) t
    WHERE t.language = 'eng' AND t.channel_layout = 'stereo'

The two-English case — 5.1 with 5.1, stereo with stereo — is just a
wider key:

    SELECT amix(a.track, b.track)
    FROM input('film.mkv') f, input('commentary.mkv') p,
         unnest(f.audio) a JOIN unnest(p.audio) b
           ON a.language = b.language AND a.channel_layout = b.channel_layout

Union-with-silence-fill is a FULL OUTER JOIN plus COALESCE; the fill
inherits the paired track's duration (the only correct default; an
explicit `duration =>` on the source call wins):

    SELECT a.track, COALESCE(b.track, ffmpeg.anullsrc())
    FROM ..., unnest(f.audio) a
         FULL OUTER JOIN unnest(p.audio) b ON a.language = b.language

## Semantics

- **Row order is deterministic without ORDER BY**: the left side's track
  order, then (FULL only) unmatched right rows in their own order — the
  RFC-008 canonical-source rule, inherited. Track order is
  player-visible surface; nothing resorts it implicitly. `ORDER BY` over
  row columns re-sorts explicitly (compile-time; the
  NO_STREAMING_EQUIVALENT fence gets a carve-out ONLY for track-row
  queries — frames still never sort).
- **Output multiplicity**: a stream column selected over N rows yields N
  output streams in row order — exactly an array. Row queries and the
  array model are one mechanism; CTE columns, splats, subscripts,
  broadcasting all follow.
- **Join multiplicity is real join semantics**: a row matching two rows
  on the other side pairs with both. That REPLACES RFC-008's
  duplicate-tag rejection — two English tracks against one is not an
  error, it is two pairs, and the fix (when unwanted) is a wider key.
  Documented loudly.
- **Consume-once**: only stream columns actually selected wire into the
  graph. Unmatched and unselected rows' streams are never decoded (the
  template-only rule, now falling out of ordinary column selection).
- **Fences**: unnest needs a probeable input (same rule as bare arrays,
  typed rejection otherwise). JOIN syntax is admitted between unnest
  tables ONLY — input-level FROM stays comma-cross-join. Predicates are
  compile-time expressions over row columns and literals (=, !=, <, >,
  BETWEEN, AND/OR). INNER / LEFT / FULL OUTER with ON; comma between
  unnest tables is the (bounded, compile-time) cross join.
- **NULL track columns**: selecting a nullable track column (outer join)
  without COALESCE is a typed rejection naming the row that was NULL —
  never a silently missing output.

## Plumbing

1. probe.py: StreamMeta grows codec, channels, channel_layout, bitrate,
   duration, color_transfer (audio fields None on video and vice versa);
   ProbeResult grows container duration. Opportunistic as ever.
2. parser.py: sqlglot parse-shape empirics FIRST (STOP gate): unnest in
   FROM with and without comma-siblings, JOIN nodes and ON expressions,
   FULL OUTER spelling, COALESCE, ORDER BY placement — under
   read="postgres", the shapes drive the resolver design.
3. resolver: track-row table bindings (alias -> row schema), compile-time
   predicate evaluator, join evaluation, row-order rules.
4. lower.py: row queries resolve to aligned `_Value` arrays with per-row
   provenance (each row's StreamMeta IS its provenance); COALESCE fill
   mints anullsrc nodes (duration from the paired track). Downstream
   untouched.
5. Carried from RFC-008: `-i` dedup for identical untrimmed inputs.

## Waves

- 060 (sonnet): probe enrichment + `-i` dedup. Independent, useful now.
- 061 (opus): parse-shape empirics (STOP gate) + resolver row model +
  WHERE/ORDER BY over rows. Ends green with SELECT-from-unnest working.
- 062 (opus): joins (INNER/LEFT/FULL), COALESCE fill, multiplicity,
  errors. The concat and pairwise-mix stories end-to-end, exec-tested.
- 063: docs (mine: filters.md or a new docs/tracks.md, cookbook recipes,
  README bullet), prompt.py section (agent-eligible).

## Every stream type unnests; only the fill differs

`unnest` accepts audio, video, subtitle and data arrays alike — the row
model, WHERE, joins and ORDER BY are type-agnostic. What differs per
type is the COALESCE fill for an outer join's gaps:

- **audio**: `ffmpeg.anullsrc(...)` — sample_rate and duration inherit
  from the paired row's columns; explicit options win.
- **video**: `ffmpeg.color(...)` (black default) — size, rate and
  duration inherit from the paired row's width/height/fps/duration the
  same way.
- **subtitle/data**: NO fill exists (cues cannot be generated). A
  COALESCE fallback on a caption column is a typed rejection; a NULL
  caption column selected bare is the ordinary NULL-track rejection. The
  caption idiom is INNER or LEFT join — and caption rows still buy
  selection by metadata (`WHERE s.language = 'eng'`), which replaces
  subscript guessing. Caption track columns stay passthrough-only,
  exactly like subtitle streams everywhere else in the dialect.

Subtitle row columns: `track`, `index`, `language`, `title`, `codec`.

## Non-goals

Frame-level joins (never — nothing here decodes); GROUP BY/aggregates
over rows (count of tracks etc. — maybe later, the fence stays put);
joining unnest tables against real SQL VALUES lists;
disposition/default-track flags as columns (probe has them; add when a
recipe needs them).
