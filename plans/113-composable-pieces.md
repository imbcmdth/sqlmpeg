# 113 — The registry holds what is worth installing

Maintainer, 2026-08-24, from asking what in the registry actually
composes.

A package should encode **a decision you would otherwise get wrong**.
Half the registry today is a filter call with its arguments spelled out,
which teaches nothing `sqlmpeg filters` would not. That material belongs
in the cookbook. What belongs in a package is the intricate part: the
`loop => true` that stops a still overlay dying after one frame, the
`COALESCE(a, anullsrc())` that stops a missing language silently
dropping a track, the shared view that turns three encodes into one
decode.

Two parts, in two repos. Part A is a compiler change and unblocks the
most interesting piece of part B.

---

# Part A — n-input filters stop being curated (sqlmpeg)

`lower.py:823` holds `N_INPUT`, a literal table of nine filters —
`amix`, `hstack`, `vstack`, `acrossfade`, `amerge`, `join`,
`interleave`, `ainterleave`, `ladspa` — with `concat` special-cased
beside it. The registry's pad-scope check excludes every dynamic-pad
filter; this table re-admits the chosen few. The comment above it
already concedes the shape of the problem: the ffmpeg filter set has
other N-to-one shapes, `mix` and `xstack` among them, that the table
does not carry.

ffmpeg 9.0.1 reports **34** filters with dynamic inputs. Nine are
reachable through the table, ten counting `concat`'s own path. The
other twenty-four are excluded by curation, not by any property they
have — and `VARIADIC` removed the reason for the
curation, because the pad count now comes from the array.

## What derives, and what does not

Of the 34: **23 take video and return video**, **8 take audio and
return audio**, and **3 are dynamic on both sides** — `concat`,
`streamselect`, `astreamselect`. The two selectors are also
multi-output, so they stay excluded on that ground and need no special
handling here.

For the 31 with an unambiguous stream type, four of `_NInputFilter`'s
five fields fall out of introspection:

- `stream` and `output` — from the pad notation ffmpeg already prints.
- `option` — the option whose value is the pad count, found by name
  among the filter's own options (`inputs`, or `nb_inputs` where that
  is what the filter calls it).
- `fallback` — that option's default.

**`emit_default` cannot be derived.** It is false for `acrossfade`
because that filter only grew an `inputs` option in ffmpeg 9, so
writing the defaulted count produces a command that breaks on older
builds. That is a claim about ffmpeg installations other than this one,
which no amount of local introspection can answer. It stays an
override — but an override map of two or three entries is a different
thing from an allowlist of nine.

## The change

1. **`registry.py`** stops excluding filters that are dynamic on the
   INPUT side, and marks them as n-input instead. Filters dynamic on
   the OUTPUT side (`split`, `asplit`) stay excluded, and should: the
   compiler inserts those itself.
2. **`lower.py`** derives `N_INPUT` from the registry rather than
   listing it, keeping overrides only for what introspection cannot
   see.

Which filters work becomes a fact about the ffmpeg in front of you,
which is the principle the rest of the registry already runs on.
`xstack` stops being a special request and simply appears, along with
`mix`, `xmedian`, `mergeplanes` and the rest.

## What this must not break

- `hstack(a, b)` still emits `hstack=inputs=2`, and
  `vstack(hstack(a,b), hstack(c,d))` still compiles with the splits the
  compiler inserts. Both verified working today; they are the
  regression test.
- The count/`inputs` disagreement rejection keeps naming both numbers.
- The snapshot changes, since these filters enter it. Regenerate with
  `scripts/gen_snapshot.py`; never hand-edit it. If carrying the
  n-input marking means the snapshot schema gains a field, bump its
  `format_version` with it.

---

# Part B — one package of things worth installing (sqlmpeg-registry)

## B1. Collapse to a single package

Eight packages were drawn around thirty-two programs. After the cull
there are seventeen, which is thin spread eight ways. Everything moves
to **`want/video`**.

**This costs the only cross-package test there is.** `want/video`'s
`shot` is currently the sole end-to-end proof that a call across
packages resolves — the whole point of the work that removed aliasing.
Before collapsing, either keep one deliberate second package as the
integration case, or move that coverage into sqlmpeg's own suite. Do
not let it evaporate silently.

Naming note, worth settling before it appears in every call site:
`want.video.aligned_audio(...)` reads oddly for audio work.
`want/media` or `want/av` covers the same ground. Maintainer's call.

## B2. The cull

Gone — each is one filter call with its arguments: `crop`, `resize`,
`rotate`, `fade`, `volume`, `loudnorm-all`, `extract-video`,
`extract-subtitles`, `split-channels`, `blur-region`, `extract-frames`,
`burn-subtitles`, `speed`, `clip`, `retitle`.

`blur-region` is the clearest case: it hardcodes its rectangle
(`900, 60, 320, 180, 20`) and exposes only `:track`, so it is not
parameterised at all.

Kept, each for a reason: `concat-fill`, `insert`, `crossfade` (the
language-aligned join), `duck` (sidechain over a doubly-referenced
stream), `gif`, `motion-thumbnail` (the palette pattern),
`extract-audio`, `split-chapters` (computed fan-out filenames),
`abr-ladder` (one decode, three encodes), `side-by-side` (the join on
matching dimensions), `watermark`, `pip` (the looped still),
`tracks-to-csv` (the only program producing rows, not files),
`transcode`, `thumbnail`, `mux-subtitles`, `replace-audio`.

Anything culled that is not already a cookbook entry should become one
before it is deleted.

## B3. The functions

**`aligned_audio`** — highest value. The pattern

    unnest(f.audio) a FULL OUTER JOIN unnest(g.audio) b
      ON a.tags.language = b.tags.language
    ... array_agg(COALESCE(a, ffmpeg.anullsrc()))

appears in `concat-fill`, `crossfade` and `insert`, and `insert`
repeats it three times inside itself. It has already cost one bug.

**`palette_gif(frame)`** — `paletteuse(x, palettegen(x))`, in `gif` and
`motion-thumbnail`.

**`watermarked(v, logo, scale, x, y)`** — `overlay` over a
`loop => true` still. `pip` is the same shape with a video overlay, so
one function with the loop as a parameter covers both. Verified: a
value-returning function may open its own input, and `loop => true`
lowers to `-loop 1`.

**`frames(path, count, track)` returning rows of `(n, frame)`** — n
full frames, evenly spaced.

    SELECT i.i, v
    FROM input(path) f, unnest(f.video) v, generate_series(1, count) i
    WHERE v.index = track
      AND f.t >= f.duration * (i.i - 0.5) / count

No window and no resize: a bare `f.t >=` is a seek to a point, and the
caller's `frames 1` takes one frame from there. A count of 1 is a
poster at the midpoint; a count of 12 is a storyboard.

The spacing is deliberately not the motion thumbnail's. That one uses
`(i-1)/(count-1)`, spanning 0 to 1 inclusive, which is right for clips
because the opening and closing matter. For still frames those are
exactly the frames nobody wants — black at the head, credits at the
tail. `(i - 0.5) / count` centres each frame in its own slice and never
touches either end. That difference is why these are two functions and
not one with a flag.

*Updated 2026-08-25, after value columns landed: BOTH spellings work
now. The gathered form was verified end to end earlier the same day
(four midpoint seeks into one `xstack`, output plays), and the
standalone form is the compiler's own acceptance test — `frames(path,
count, track)` returning `(n, frame)` rows, `TO (:'prefix' ||
s.n::text || '.png')`, one file per frame. Ship `frames` complete with
its `n` column, and declare `count` with `DEFAULT` now that signatures
have them.*

**`grid(streams, shape)`** — the piece that shows what composition is
for:

    SELECT ffmpeg.xstack(VARIADIC streams, grid => shape, fill => 'black')

`xstack` carries `grid` as an image size (`'3x3'`), so the layout
string is never built by hand, and `fill` covers a partial last row
rather than failing. One function, two unrelated call sites:

    grid(ARRAY[a.video[1], b.video[1], c.video[1], d.video[1]], '2x2')
    grid(array_agg(s.frame), '3x3')

The second works because `frames` returns rows that are each their own
`-i` with their own seek, so `xstack` cannot tell them from four
cameras. The abstraction holds for the reason it should, not by
accident.

Note `tile` is already callable and makes contact sheets from a single
stream — cheaper for that one case, but it cannot do the four-camera
case, so it cannot be the shared function. For a long file nine seeks
beat decoding the whole thing anyway.

## B4. Language fallout to fold into the same pass

The tag and dialect work that landed after this plan was written
changes three things here, none large:

- **`retitle` respells — and becomes its own best advertisement.** It
  is the ONLY registry program using the removed aliased-scalar tag
  spelling (verified by grep; no program uses `VALUES`, `ROW(...)`
  record literals, `metadata_from`, or `strip_metadata`). Its body
  collapses to the copy-and-override idiom:

      SELECT *, f.tags || STRUCT(:'title' AS title, :'artist' AS artist) AS tags
      FROM input(:'source') f

  with unset variables clearing nothing (NULL merges as absence).
- **A `* REPLACE` pass over the kept programs**, once that lands:
  every keep-everything-change-one-thing program (`watermark`, `pip`,
  the one-track transforms that survived the cull) shortens to
  `SELECT * REPLACE(<expr> AS video)`. Do it in the same pass as the
  cull so each program is touched once.
- **`frames` and `grid` ship as tested**, per the updated note above —
  including per-frame file output, which was the standalone function's
  whole point.

## What is still blocked

A ladder cannot become a function. `WITH (crf r.q)` is a parse error,
so encode settings must be literals — even once a `VALUES` row can key
a fan-out, the filename may vary per row but the crf may not. Those
options are output-side rather than graph-side, so this is a larger
change than it looks, and it is not in this plan.

It does not matter much yet, because `CREATE VIEW` is already the
composition seam. A view holding `watermarked(...)` feeding three
`COPY` statements compiles to **one** command — one decode, one
overlay, `split=3`, three encodes — which is strictly better than
running two programs back to back. Verified.

## Order

1. **Part A**, in sqlmpeg. `grid` cannot exist without it.
2. **B1** the collapse, with the cross-package coverage moved first.
3. **B2** the cull, promoting anything culled into the cookbook.
4. **B3** the functions. `aligned_audio`, `palette_gif` and
   `watermarked` need nothing new. `frames` needs plan 109, and `grid`
   needs part A.

`frames` also needs one thing plan 109 should say out loud: a fan-out
`TO` must be able to name files from a **table function's** returned
column, not only from `unnest` rows. That is within 109's stated
widening, but it is the difference between `frames` being usable and
not.

## House rules

- **Do not commit.** Leave the work uncommitted for review.
- Never write "fence" or "gate" in code, comments, docs or reports.
- Never cite a plan or RFC number in code, comments, docstrings or
  error messages.
- Short factual WHAT comments only.
- Recipes into `docs/examples.md` first, with pins generated by running
  the compiler. Never hand-type an ffmpeg command line.
- Run the targeted tests, not the whole suite. Report concisely.
