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
- The alias names both the relation and its one column, and the
  column reads back qualified: `generate_series(1, 5) i` gives `i.i`.
- A descending or empty range is a rejection rather than zero rows: a
  query that produces nothing is a mistake worth naming.

`FROM` currently admits `input()`, CTE names, source filters and
table-returning functions, and its rejection says "only input('path')
and CTE names", which is already stale. That message wants fixing with
this.

## 3. Two ways to slice, chosen by the query

Section 3 as first written aimed at the wrong constraint. `-ss`/`-to`
are **per-input** options, so no row-bounded trim can slice one `-i`
many ways — the fan-out is not a restriction to be lifted, it is what a
seek IS. And the in-graph route, `ffmpeg.trim`, refuses a computed bound
outright:

    FILTER_OPTION_TYPE: option 'starti' of filter 'trim' expects a
    string, got a COLUMN expression

because ffmpeg reports duration options as `type=str`.

So there are two slicing semantics, they are genuinely different, and
the query should say which it wants. Both spellings already exist; each
needs one restriction lifted.

### 3a. A row-derived bound mints one input per row

`WHERE f.t >= …` keeps meaning **seek**: N windows become N `-i` of the
same file, each with its own `-ss`/`-to`. ffmpeg skips what it discards,
so this is the cheap form for a few short windows over a long file.

What changes is only **which rows may key it**. Today that is tracks and
chapters; `known_gaps.md` records that a CTE-keyed group cannot fan out.
Extend it to any compile-time-countable row source — `VALUES`,
`generate_series`, whatever comes later. Nothing in the machinery is
track-specific; `split-chapters` already proves per-row windows work.

A second restriction goes with it: such a fan-out no longer requires a
fan-out `TO`. It was constrained to one because N inputs had nowhere to
go but N files. Under `VARIADIC` they gather into one:

    SELECT ffmpeg.concat(VARIADIC array_agg(shots.frame)) FROM shots

### 3b. A computed value may bound a filter

`ffmpeg.trim(v, starti => expr)` is a **filter**: one decode, with
everything outside the window decoded and discarded. That is the right
form when windows are dense, when the source is short enough that
seeking buys nothing, or when the slices must stay inside one graph.

Duration-typed options accept any compile-time-countable numeric
expression, formatted as seconds. The `type=str` a duration option
reports is an ffmpeg reporting detail, not a claim that only strings
are meaningful there.

### 3c. The compiler resets PTS after a trim

`trim` preserves source timestamps. Concatenate two trims without
`setpts=PTS-STARTPTS` and the result has a gap where the first clip's
timestamps ended; write one to a file alone and it carries a lead-in of
however far into the source it began. That is a silent wrong answer
rather than an error, which makes it exactly what the compiler should
not leave to the author.

So: **after every `trim`/`atrim` whose result is used, the compiler
inserts a PTS reset**, unless that path already carries one the author
wrote. `explain` shows the inserted node, as it already shows inserted
`split`s — the graph stays inspectable and the insertion stays
overridable.

The 3a form needs none of this: each window is its own `-i`, whose
timestamps already start at zero. That asymmetry is the sharpest
statement of what the two forms actually are.

## 4. A filter name dispatches on its input type

`setpts` and `asetpts` are one operation over two mediums, and 3c would
otherwise have to pick between them by hand. It should not have to, and
neither should anyone else: the compiler knows every stream's type.

**When a call names `X` and `aX` also exists, the input picks which.**
Video in, `X`; audio in, `aX`.

This holds for all 29 such pairs in the reference registry — every `X`
takes video-only inputs, every `aX` audio-only — so the dispatch is
never ambiguous. Three pairs disagree on OUTPUT type (`drawgraph`,
`graphmonitor` and `histogram` return video from audio), which costs
nothing: dispatch reads the input, and misuse of the result is an
ordinary type error downstream.

- **Raw names are untouched.** `ffmpeg.setpts` and `ffmpeg.asetpts`
  stay exactly what ffmpeg has. Dispatch is a convenience over the
  curated surface, never a reinterpretation of an explicit raw call.
- **One behaviour change**: `fade(a)` on an audio stream is a type
  error today and means `afade` afterwards. Someone handing audio to
  `fade` wants their audio faded, so the trade is worth taking — and it
  is the only way an existing query can be affected.
- 3c then needs no special case of its own. It inserts a node named
  `setpts`, and the rule that serves authors resolves it on an audio
  path.

## All of it: the motion thumbnail, one branch

    COPY (
      WITH shots AS (
        SELECT f.video AS frame
        FROM input(:'source') f, generate_series(1, :count) i
        WHERE f.t >= (f.duration - :len) * (i.i - 1) / (:count - 1)
          AND f.t <= (f.duration - :len) * (i.i - 1) / (:count - 1) + :len
      ),
      small AS (
        SELECT fps(scale(ffmpeg.concat(VARIADIC array_agg(shots.frame)),
                         :width, -2), :fps) AS frame
        FROM shots
      )
      SELECT paletteuse(small.frame, palettegen(small.frame))
      FROM small
    ) TO :'dest'

`(i.i - 1) / (:count - 1)` runs 0 to 1 across whatever count is passed —
the `shot` function's fraction, generalised — so the head and tail clips
still fall out of the same arithmetic and five hand-written branches
become one.

## Order

1. **`VARIADIC`.** Done. Independent, the largest win, and the only one
   that unlocks queries no rearrangement can express today. `concat`
   became callable with it.
2. **`generate_series`.** Done. Independent of 1; useless without 3a for
   slicing, but immediately useful for anything wanting N of a thing.
3. **3a — rows keying fan-outs and bounding trims**, and gathering
   without a fan-out `TO`. Needs 2 to be worth much, and closes a gap
   `known_gaps.md` already records.
4. **3b — computed values in duration-typed options.** Independent of
   3a; this is the other slicing semantics, and without it `trim` stays
   a filter nobody can drive.
5. **4 — name dispatch on input type.** Independent of everything else,
   but 3c leans on it, so it lands first of the two.
6. **3c — the PTS reset.** Needs 4 to be one mechanism rather than two,
   and needs 3b to have anything worth resetting.
7. Rewrite `want/images`' motion thumbnail against all of it, and retire
   `want/video`'s `shot` if nothing else wants it.

Each lands with its recipes in `docs/examples.md` first, as usual.
