# 109 — Wave A: any countable row may bound a seek

Maintainer, 2026-08-24, revised after checking the claims against the
code. Section 3a of plan 108, as an executable wave. Independent of
wave B — different machinery, neither waits on the other.

## What is true today — verified, not assumed

Series and VALUES rows are already ordinary rows. `_add_series_rows`
(`lower.py:4401`) and the VALUES binder (`lower.py:4391`) both produce
`_RowBinding`s whose rows join the branch relation exactly as track
rows do, and resolve's scope already marks their aliases `"row"`. The
series column is read back qualified: `generate_series(1, 3) i` gives
`i.i`, never bare `i` — the cookbook's series recipes show this, and
every example in this plan writes it that way.

Two distinct walls remain, one per half of the feature. Both were hit
empirically:

**The fan-out half.** A series-keyed window under a fan-out `TO`
passes resolve completely — the mixed-conjunct carve-out at
`parser.py:4809` admits a row-bounded time window whenever `fanout` is
set, for any row alias. It then dies in lower with "a trim bound may
reference track-row columns only under a fan-out TO" — **even though a
fan-out TO is right there**. The cause: fan-out recognition at
`lower.py:2584` asks `references_row_alias(raw.path_expr,
set(self.res.track_rows))`, and `res.track_rows` holds **unnest
aliases only** (`parser.py:3653` is its single producer). A TO
referencing `i.i` is not recognised as a fan-out, `fanout_expr` stays
None, and the rejection at `lower.py:4656` fires with a message that
is wrong for this case. The parser makes the same unnest-only decision
wherever it consults `self.track_rows` (e.g. `parser.py:2444`,
`:1267`); every such site needs the same widening.

**The gather half.** With no fan-out `TO`, resolve itself rejects the
window: the carve-out at `parser.py:4809` requires `fanout=True`, so
`WHERE f.t >= i.i - 1` is "a WHERE predicate cannot mix track-row
columns (i) with other aliases (f)" before lower ever sees it. This is
where the new machinery lives.

## What changes

1. **Fan-out recognition widens from unnest aliases to all row
   aliases.** Series, VALUES and CTE-carried rows key a fan-out `TO`
   on the same terms as track and chapter rows. Downstream, the
   per-row pinning and `Graph.input_trims` machinery
   (`lower.py:5425`, `:5545`) is not track-specific; expect small
   fixes where a pinned row is assumed to carry a stream (series and
   VALUES rows are `_STREAMLESS_ROW`), not a rework.

2. **A row-bounded window no longer requires a fan-out `TO`.** Resolve
   admits the shape unconditionally (the `fanout` condition on the
   carve-out goes); lower decides what it means. Under a fan-out `TO`
   it means what it means today: one command per row. Without one, the
   rows must be **gathered** — consumed by a `VARIADIC` aggregate —
   and each surviving row mints its own `-i` of the same path with its
   own `-ss`/`-to`, all in **one** graph, the per-row streams flowing
   into the aggregate. N windows reaching a single sink that neither
   gathers nor fans out is still a rejection; its message names both
   ways forward.

3. **Seek semantics are unchanged.** Per-input `-ss`/`-to`, ffmpeg
   skipping what it discards. No PTS handling is needed here and none
   should be added: each window is its own input, so its timestamps
   already start at zero. That is wave B's problem, not this one's.

4. **The rejection at `lower.py:4656` gets an accurate message.** Once
   both halves exist it fires only for the un-gathered, un-fanned
   case, and its hint names both spellings.

## The shape already exists

The motion thumbnail compiles today to **five `-i` of the same file,
each with its own `-ss`/`-to`, feeding one graph** — because it is
written as five hand-copied branches through `shot`. The gather half
does not invent that shape; it lets five ROWS produce it. That hands
the wave its sharpest test: the row-driven query must compile to
**byte-identical argv** against the hand-written form, the same
standard table functions were held to.

## One case the fan-out half must cover

A fan-out `TO` must be able to name its files from a **table
function's** returned column, not only from a `FROM`-level row source
— a function whose body is `input(...) f, generate_series(...) i`
returning `(i.i, stream)` rows, with the caller writing
`TO (:'prefix' || s.n::text || '.png')`. Expansion inlines the body,
so this should follow from (1) plus hygiene, but it is the consuming
case for the registry's planned `frames()` function and must be
tested, not presumed.

## Recipes first

Into `docs/examples.md`, before any implementation:

1. **N clips from a parameter, into N files** — a series-keyed window
   under a fan-out `TO`.
2. **N clips gathered into one** — the same windows reaching a single
   `ffmpeg.concat(VARIADIC array_agg(...))` sink. This is the recipe
   the wave exists for.

Both as ordinary `pgsql` blocks with their compiled command beneath,
generated by running the compiler. Never hand-type an ffmpeg command
line. Series columns are written qualified (`i.i`) throughout.

## Verification

- The gathered form's argv is byte-identical to the hand-written
  motion thumbnail's for the same count and length.
- A series-keyed window with no gathering and no fan-out `TO` is still
  a rejection, and its message names both ways forward.
- A descending or single-row series still behaves as before.
- The table-function fan-out case above compiles and names its files
  from the returned column.
- `ruff`, `mypy --strict`, and the unit tier stay green.

## House rules

- **Do not commit.** Leave the work uncommitted for review.
- Never write "fence" or "gate" in code, comments, docs or reports.
- Never cite a plan or RFC number in code, comments, docstrings or
  error messages.
- Short factual WHAT comments only.
- `docs/*.md` is terse reference prose, not backstory.
- Run the targeted tests, not the whole suite; the full run happens at
  review. Report concisely.
