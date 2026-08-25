# 115 — Tags become explicit, and every other column becomes a value

Maintainer, 2026-08-25, rewritten after the census and the design
conversation. Supersedes the first draft's tag-relocation rule.

Pre-1.0, so this breaks cleanly in one release: **the implicit
aliased-scalar-tag convention is removed and the explicit form arrives
in the same change.** No deprecation window, no dual meaning, no
release where a scalar column means two things.

## The two rules, complete

1. **Metadata is written by a column named `tags`, holding a struct
   expression.** Everything else in a SELECT list that is not a stream
   (or one of the existing special columns — `chapters`,
   `disposition`, `attachments`) is a **value column**: plain data
   traveling with the row, never metadata.

2. **`STRUCT(expr AS key, ...)` is the struct literal.** Borrowed from
   BigQuery, documented as a deviation like the dialect's others, and
   already parsed by the current parser (verified: `exp.Struct` with
   `PropertyEQ` fields, and `f.tags || STRUCT(...)` as `DPipe`).

The write surface:

    SELECT *, STRUCT('roo' AS title, :'artist' AS artist) AS tags ...
    SELECT *, f.tags || STRUCT('roo' AS title) AS tags ...      -- copy, override
    SELECT *, f.tags || STRUCT(NULL AS title) AS tags ...       -- NULL clears a key
    SELECT t, t.tags || STRUCT('eng' AS language) AS tags
    FROM input(:'source') f, unnest(f.audio) t WHERE t.index = 1

Scoping is today's rule, respelled: over input rows the `tags` column
is the container's map; over track rows it is that stream's; in a CTE
body it rides the body's streams (which is how recipe 53's layering
survives — respelled, not killed). `||` merges right-side-wins; a NULL
field clears its key (the absence rule — no delete operator). All of
it evaluates at compile time to `-metadata` flags, as today.

Read and write are finally the same shape: `f.tags.title` in,
`STRUCT(... AS title)` out.

## What is removed

- An aliased scalar column is no longer a tag, anywhere. At a media
  sink, a scalar column that is not `tags` (or another special column)
  is a typed rejection whose hint shows the `STRUCT(...) AS tags`
  spelling. In a table query, scalars keep printing as data.
- The census (in the transcript of the standing agent, harness at the
  session scratchpad's `cen.py`) maps every current tag behavior — the
  A-set is the respell list, and the "tag takes two different values"
  error family disappears entirely.

## Value columns — what the removal buys

With tags explicit, a scalar column has exactly one meaning, so CTE
and table-function rows can finally carry values:

- A CTE alias exposes its scalar columns beside its streams (today it
  exposes streams only — verified, there is NO existing outer-read
  behavior to preserve). Readable wherever row columns read today:
  `WHERE`, a fan-out `TO` expression, `GROUP BY`, another CTE, table
  output.
- A table function's `RETURNS TABLE(n number, ...)` declaration is
  ENFORCED — the census found declared types are currently decorative
  — and its declared scalar columns are value columns.
- Every value is compile-time evaluated by the row evaluator that
  already computes predicates and seek bounds. Nothing new is known
  only at run time; a column that is not compile-time evaluable is a
  typed rejection naming it.
- A fan-out `TO` reading a CTE/function value column pins one row per
  command, the machinery every other row-keyed fan-out uses; per-row
  windows are bound inside the body where the input binds, so a pinned
  row arrives with its input already seeked. `docs/known_gaps.md`'s
  "CTE-keyed group cannot fan out" entry closes; the
  `ROW_COUNT_MISMATCH` hint that names an unreachable body alias
  becomes true or is reworded.

## The acceptance test

    CREATE FUNCTION frames(path text, count number, track number)
    RETURNS TABLE(n number, frame video_stream) AS $$
      SELECT i.i, v
      FROM input(path) f, unnest(f.video) v, generate_series(1, count) i
      WHERE v.index = track AND f.t >= f.duration * (i.i - 0.5) / count
    $$ LANGUAGE sql;

    COPY (SELECT s.frame FROM frames(:'source', :count, 1) s)
    TO (:'prefix' || s.n::text || '.png') WITH (video_codec 'png', frames 1)

`:count` commands, each seeking its midpoint, each writing its own
file named from `s.n`. Second acceptance: per-file titles under a
fan-out — `STRUCT('shot ' || s.n::text AS title) AS tags`. Third: the
gathered spelling keeps compiling unchanged (its tests exist).

## STRUCT beyond tags

Chapters and cues gain the named-field spelling beside the positional
one — `STRUCT('Intro' AS title, 0 AS start_t, 60 AS end_t)` is
accepted wherever `ROW('Intro', 0, 60)::chapter` is. The `ROW::cast`
form stays valid; STRUCT is the taught one. **Every recipe and doc
example using `ROW(...)::chapter` / `::cue` is rewritten to STRUCT**,
and the tag recipes (52, 53, 55, retitle-shaped ones) are respelled to
the `tags` column.

## Consequential retirements, same change

`metadata_from <alias>` is `f.tags AS tags`; `strip_metadata true` is
`STRUCT() AS tags`. Both sink options are removed; their rejections
name the replacement spelling. The language gets smaller.

## Ripples to carry, not forget

- `sqlmpeg/prompt.py`: the tag section shrinks to the one rule; STRUCT
  documented; an LLM never learns the dead spelling.
- `docs/rows.md` Tags section, `docs/dialect.md`'s tag-column grammar
  and the value grammar, `docs/errors.md` if error codes change.
- The registry's programs (`want/*`) use `AS title` and must be
  respelled when registry work resumes — noted here so the release
  that ships this is followed promptly by the registry update.
- Two-pass (`loudnorm2`), `explain`, and MCP surfaces should need
  nothing, but verify tags still flow through `SinkUnit.tags`.

## Order

1. STRUCT literal as a value expression + the `tags` column, with the
   implicit convention removed and every test/recipe/doc respelled in
   the same motion (the census's A-set is the checklist).
2. Value columns: CTE and table-function rows, enforcement of declared
   types, reads in WHERE/TO/GROUP BY/table output, the fan-out
   pinning. The frames acceptance.
3. Chapters/cues STRUCT spelling + recipe rewrites; the two sink
   option retirements.

One agent may carry all three in sequence; the boundary between them
is where a review could land if it must.

## House rules

- **Do not commit.** Leave the work uncommitted for review.
- Never write the two banned words in code, comments, docs or reports.
- Never cite a plan or RFC number in code, comments, docstrings or
  error messages.
- Short factual WHAT comments only.
- Recipes and pins generated by running the compiler, never hand-typed.
- Run the targeted tests, not the whole suite. Report concisely.
