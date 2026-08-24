# 108 — Loop-shaped queries: VARIADIC, a series, and rows that carry windows

Maintainer, 2026-08-23, from trying to write the motion thumbnail
without five copy-pasted branches.

Three features that only matter together, and together they are the
thing hand-written ffmpeg cannot do: **a query whose SHAPE follows a
parameter or a file, rather than the text.** Five clips or nine, every
chapter however many there are, every track a file happens to carry.

Everything here stays compile-time countable. Nothing becomes known
only at run time; the arity simply stops being spelled out in the query
and starts being computed from something the compiler already knows.

## 1. `VARIADIC`: an array IS the argument list

Today an N-input filter takes N WRITTEN arguments — `amix(a, b, c)` is
three — and `concat` is not callable at all, being a variable-pad
filter. So a query cannot mix "however many tracks this file has" or
join "however many pieces the last step produced".

The obvious move, passing `array_agg(...)` as one argument, is the
wrong shape: an array value is not an argument list, and making it one
implicitly would be magic. It would also be AMBIGUOUS, because a bare
array in an argument position already means something — **broadcast**,
one node per element. `atempo(v.audio, 1.25)` fans out per track.

SQL already has the marker, and it means exactly this:

    concat(VARIADIC array_agg(v))
    amix(VARIADIC f.audio)

Postgres's `VARIADIC` says "this array is the argument list, expanded".
sqlglot parses it and round-trips it, so the syntax costs nothing.

Rules:

- **Bare array keeps meaning broadcast; `VARIADIC` means spread.** Two
  readings, two spellings, no ambiguity.
- **Both an aggregate and a plain array are accepted.**
  `array_agg(...)` over a compile-time-countable relation has a known
  length, and so does `f.audio`. `amix(VARIADIC f.audio)` needs no
  `GROUP BY` for the common case.
- **Positional streams may precede it**, as in Postgres: `concat(intro,
  VARIADIC array_agg(v))`. `VARIADIC` is last and there is at most one.
- **The pad count comes from the array.** An explicit count option that
  disagrees (`amix(VARIADIC xs, inputs => 2)`) is a rejection naming
  both.
- **An empty array is a rejection** — a filter with no inputs is not a
  filter call. It names the relation that produced nothing.
- **`concat` joins the callable set.** It was excluded for having a
  variable pad count; under `VARIADIC` the count is determined, so it
  is callable on the same terms as the rest. Called without `VARIADIC`
  it stays rejected, with a hint saying so.

This also explains a restriction that will look arbitrary otherwise:
`array_agg()` is "only supported as a whole SELECT column" because an
array in a value position had nowhere to go. `VARIADIC` gives it
exactly one place.

**What it unlocks on its own**, before anything else here — stitching a
file's own chapters, which is unwritable today:

    COPY (
      WITH parts AS (
        SELECT unsharp(f.video, luma_amount => 1.5) AS v
        FROM input(:'source') f, unnest(f.chapters) c
        WHERE f.t >= c.start_t AND f.t <= c.end_t
      )
      SELECT ffmpeg.concat(VARIADIC array_agg(parts.v))
      FROM parts
    ) TO :'dest'

The rows already work. Only the join was missing.

## 2. `generate_series(start, stop)` as a FROM item

A row source that is a count rather than a file:

    FROM input(:'source') f, generate_series(1, :count) i

- Bounds must be integer literals **after substitution**, which is when
  the compiler sees them, so `:count` is fine and a column reference is
  a rejection. That is what keeps it countable: the row count is
  `stop - start + 1`, known before anything runs.
- An optional third argument is the step, same rule.
- The alias names both the relation and its one column, as in Postgres:
  `generate_series(1, 5) i` gives `i`.
- A descending or empty range is a rejection rather than zero rows: a
  query that produces nothing is a mistake worth naming.

`FROM` currently admits `input()`, CTE names, source filters and
table-returning functions, and its rejection says "only input('path')
and CTE names", which is already stale. That message wants fixing with
this.

## 3. Any compile-time row may key a fan-out and bound a trim

Today a trim bound may reference row columns **only under a fan-out**,
and only for track and chapter rows — `known_gaps.md` records that a
CTE-keyed group cannot fan out. So the rows from a `VALUES` list or a
series cannot carry a window, which is what makes them useless for
slicing.

Extend both to any compile-time-countable row source: `VALUES` CTEs,
`generate_series`, and whatever comes later. Nothing about the
machinery is track-specific — `split-chapters` already proves per-row
windows work; the restriction is on which rows may key them.

Then N clips into N files, positions from a parameter:

    COPY (
      SELECT f.video, f.audio
      FROM input(:'source') f, generate_series(1, :count) i
      WHERE f.t >= (f.duration - :len) * (i - 1) / (:count - 1)
        AND f.t <= (f.duration - :len) * (i - 1) / (:count - 1) + :len
    ) TO (:'prefix' || i::text || '.mp4')

## All three: the motion thumbnail, one branch

    COPY (
      WITH shots AS (
        SELECT f.video AS frame
        FROM input(:'source') f, generate_series(1, :count) i
        WHERE f.t >= (f.duration - :len) * (i - 1) / (:count - 1)
          AND f.t <= (f.duration - :len) * (i - 1) / (:count - 1) + :len
      ),
      small AS (
        SELECT fps(scale(ffmpeg.concat(VARIADIC array_agg(shots.frame)),
                         :width, -2), :fps) AS frame
        FROM shots
      )
      SELECT paletteuse(small.frame, palettegen(small.frame))
      FROM small
    ) TO :'dest'

`(i - 1) / (:count - 1)` runs 0 to 1 across whatever count is passed —
the `shot` function's fraction, generalised — so the head and tail clips
still fall out of the same arithmetic and five hand-written branches
become one.

## Order

1. **`VARIADIC`.** Independent, the largest win, and the only one that
   unlocks queries no rearrangement can express today. `concat` becomes
   callable with it.
2. **`generate_series`.** Independent of 1; useless without 3 for
   slicing, but immediately useful for anything that wants N of a thing.
3. **Rows keying fan-outs and bounding trims.** Needs 2 to be worth
   much, and closes a gap `known_gaps.md` already records.
4. Rewrite `imbcmdth/images`' motion thumbnail against all three, and
   retire `imbcmdth/video`'s `shot` if nothing else wants it.

Each lands with its recipes in `docs/examples.md` first, as usual.
