# 116 — What the language looks like: the BigQuery question

Maintainer request, 2026-08-25, from the STRUCT decision: BigQuery is
SQL that defines a map-reduce DAG, structs and arrays-of-structs are
native there, and a table and an array of structs convert into each
other freely — all of which fits this compiler better than it fits a
row store. Should the dialect align with BigQuery long term? This
document inventories both directions and recommends an answer.

## The observation that organizes everything

sqlmpeg is already two dialects wearing one syntax, split along a
clean line:

**The VALUE and ROW model is BigQuery-shaped.** `t.tags.language` is
struct field access (Postgres would force `(t).field` or json
operators). A file probes into rows whose columns are arrays of
records — `f.video`, `f.chapters` — and `unnest` is the central verb.
`STRUCT(... AS key)` just became the record literal. The whole
language compiles to a DAG, which is BigQuery's execution model, not
Postgres's.

**The STATEMENT and CALL surface is Postgres/psql-shaped.** `COPY
(query) TO 'file' WITH (...)` is the sink statement. `:'source'` is
psql variable substitution. Function bodies are dollar-quoted.
Options bind with `name => value`. `VARIADIC` spreads an array into a
pad list. Parameters declare `DEFAULT`. Casts write `::`.

The split is not an accident. The row model imitates BigQuery because
the DOMAIN is arrays of structured things; the statement surface
imitates psql because a compile-to-command tool lives in shells and
scripts, where psql's conventions are at home.

## BigQuery features we lack that would earn their keep

Ordered by value against this domain, not by novelty.

### 1. `SELECT * EXCEPT(...)` / `SELECT * REPLACE(...)`

The single highest-value borrowing available. Media work is
overwhelmingly "keep everything, change one thing":

    SELECT * REPLACE(scale(f.video[1], 1280, -2) AS video)
    SELECT * EXCEPT(subtitle)

Today both require writing every column out, which is exactly the
copy-paste this language exists to remove. `transcode`, `resize`, and
half the culled registry programs become one line. sqlglot parses
both forms under its BigQuery reader, so the borrowing is cheap the
way STRUCT was.

### 2. `ARRAY(SELECT ...)` — the missing converse

`unnest` turns an array into rows; nothing turns rows back into an
array except `array_agg` + `GROUP BY` ceremony. BigQuery's `ARRAY(
subquery)` is the inverse operator, and it completes the duality the
maintainer named: a table IS an array of structs, both directions.

    ffmpeg.concat(VARIADIC ARRAY(SELECT frame FROM shots))

reads as exactly what it does. `SELECT AS STRUCT` belongs to the same
family and matters once functions return multi-column rows that want
gathering whole.

### 3. Considered and NOT recommended

- **`WITH OFFSET` on unnest.** An earlier draft recommended it; the
  one-way rule cuts it. Rows in this language already self-describe:
  track rows carry `.index` from probe, a series row IS its number
  (`i.i`), a VALUES-shaped struct row declares whatever it needs, and
  a function row carries declared value columns. Every "number my
  rows" case has a spelling; `WITH OFFSET` would be a second one.
  Revisit only if a row source with no self-description appears.
- **`arr[OFFSET(0)]` / `arr[ORDINAL(1)]` indexing.** BigQuery makes
  the base explicit, which fits the house ethos — but `f.video[1]` is
  in every doc, recipe, published program and user query, probe order
  is 1-based everywhere it is described, and the churn buys no new
  capability. Keep 1-based subscripts.
- **`@param` variables.** Not merely redundant — they FIGHT the
  absence rule. `:'name'` is textual substitution before parse, which
  is what lets an unset variable become the keyword `NULL` with a
  source map back to its name ("':source' was not set"), feeding the
  absence machinery everywhere a NULL lands. BigQuery's `@param` is a
  RUNTIME bind parameter — a value the engine slots in at execution —
  and this compiler has no runtime; everything must be text by compile
  time. Adopting `@` would respell `:` while discarding the psql
  muscle memory of the exact audience that lives in shells, and break
  every `-v name=value` invocation ever written.
- **Procedural scripting (`DECLARE`/`BEGIN`/loops).** The language is
  declarative on purpose; loops are what `generate_series` and rows
  are for.
- **`EXPORT DATA OPTIONS(...)`.** The one case where BigQuery's
  spelling is a WORSE domain fit, not an equal one. `EXPORT DATA
  OPTIONS(uri='gs://bucket/*.csv', format='CSV')` is designed around
  cloud storage export — wildcard URIs, sharded outputs, a closed
  format enum. This language's sink options are encoder and muxer
  knobs (`video_codec`, `crf`, `frames`, `subtitle_codec`) mapping
  onto ffmpeg's own surface, and `COPY (query) TO 'file' WITH (...)`
  carries them naturally — plus the fan-out `TO (expression)`, which
  has no EXPORT DATA analog at all.
- **Window functions / `QUALIFY`.** No motivating media case yet;
  revisit when one appears (chapter gap analysis is the nearest
  candidate).

## Postgres features we depend on that BigQuery simply lacks

This list is why full alignment is off the table — each entry is
load-bearing here and has NO BigQuery equivalent to migrate to:

| feature | role here | BigQuery's answer |
| --- | --- | --- |
| `name => value` call arguments | the entire filter-option surface | none for functions |
| `VARIADIC` | array-to-pad-list spread; concat/xstack | none |
| `DEFAULT` on function parameters | optional knobs in library functions | none — BQ UDFs take fixed args |
| `COPY (query) TO ... WITH (...)` | the sink statement | `EXPORT DATA` (weaker fit) |
| `:'var'` substitution | parameterization + the absence rule | `@param` (no absence story) |
| `$$ ... $$` bodies | function definitions | `'''...'''` (cosmetic) |
| `::` casts | record casts, `::text` in TO expressions | `CAST()` only |
| `(VALUES ...) t(c)` | written row tables | none — inline data is `UNNEST([STRUCT(...)])` |

The last row is worth savoring: BigQuery's replacement for VALUES is
the struct duality itself. Once `ARRAY(...)`/struct literals are in,
`unnest(ARRAY[STRUCT(...), STRUCT(...)])` works here too — which is
not a reason to keep both spellings. It is a reason to remove one.

## What adoption removes

The standing rule: **one way to say a thing, unless the second way
carries a benefit of its own.** A spelling that merely rhymes pays
rent or leaves. Every adoption below therefore lists its deletions,
and the precedent is already set: adopting the struct map deleted
`metadata_from`, `strip_metadata`, and the whole implicit-tag
convention in one release.

- **`VALUES` goes** once `unnest(ARRAY[STRUCT(...), ...])` lands.
  Both of its uses are covered — a FROM row table, and the list a
  `chapters` option consumes — and the struct spelling is named where
  VALUES is positional (`(VALUES (1920, 20)) AS r(w, q)` says less
  than `STRUCT(1920 AS w, 20 AS q)` at every reading). No distinct
  benefit; removed.
- **`ROW(...)::chapter` / `::cue` / `::attachment` goes.** The STRUCT
  spelling shipped beside it as "the taught one", which was the
  deprecation-window thinking this project does not need pre-1.0.
  Positional record literals are exactly the rhyme this rule exists
  to delete. Small follow-up wave; the census of sites exists.
- **`GENERATE_ARRAY` is never adopted.** `generate_series` is the one
  way to say "a counted row source", it is already a FROM item rather
  than an array needing an unnest, and adopting BigQuery's spelling
  would CREATE a rhyme where none exists. The one-way rule cuts both
  directions: it prunes adoptions, not only incumbents.
- **`CAST(x AS t)` versus `::`** — sqlglot parses both, so `CAST()`
  may pass through unchecked today. Census whether it does; if so,
  either reject it with a hint naming `::` or accept it silently
  forever, but decide rather than drift. `::` is the documented
  spelling either way.
- **Kept, with the benefit named: `array_agg` beside
  `ARRAY(SELECT ...)`.** Not a rhyme — different jobs. `array_agg` is
  an aggregate and partitions under `GROUP BY` (one array per group,
  which the per-language fan-outs need); `ARRAY(subquery)` is an
  expression that gathers a whole subquery where an aggregate cannot
  stand. BigQuery itself keeps both for the same reason.
- **Kept: `unnest` beside subscripting.** `f.video[1]` picks one
  element; `unnest(f.video) v` makes rows. Different results, not two
  spellings of one thing.

## The recommendation: a hybrid with a stated rule

Not drift — a rule that decides future questions:

**The value and row model aligns with BigQuery. The statement and
call surface stays Postgres.**

Concretely: anything about what a VALUE is — records, arrays, field
access, the table/array duality, row construction — takes BigQuery's
spelling when the two differ (STRUCT over ROW-with-cast, ARRAY(
subquery), WITH OFFSET, * EXCEPT / * REPLACE). Anything about how a
STATEMENT reads or a CALL binds — sinks, variables, bodies, named
options, spread, defaults, casts — stays Postgres/psql, because that
is where Postgres is stronger for a shell-resident compiler and where
BigQuery has nothing to offer.

sqlglot makes the hybrid nearly free: it parses both dialects, the
custom dialect already subclasses one and borrows from the other
(STRUCT parsed with zero work), and each borrowing is a la carte.

The README should say this in one sentence once adopted — roughly:
"the query surface is Postgres; the data model is BigQuery's structs
and arrays, because a media file is an array of structured things."

## How the two halves cooperate

The hybrid is not a truce between competing grammars — the two
dialects occupy different STRATA of one grammar, so they compose
instead of colliding.

**BigQuery contributes expression-level grammar only**: literals and
constructors (`STRUCT`, `ARRAY(subquery)`), projection modifiers
(`* EXCEPT`, `* REPLACE`), and unnest decorations (`WITH OFFSET`).
**Postgres contributes clause- and statement-level grammar only**:
`COPY ... TO ... WITH`, `CREATE FUNCTION ... $$ ... $$`, `=>` binding,
`VARIADIC`, `DEFAULT`, `::`. An expression nests inside a clause;
a clause never appears inside an expression — so there is no token
where the two dialects could disagree, and sqlglot holds both in one
AST (the STRUCT borrowing needed zero parser work for exactly this
reason).

One query wearing both, every line annotated:

    COPY (
      SELECT * REPLACE(scale(f.video[1], :width, -2) AS video),
             -- BQ: * REPLACE        psql: :width      PG-shaped call
             f.tags || STRUCT(:'title' AS title) AS tags
             -- BQ: STRUCT, || merge          psql: :'title'
      FROM input(:'source') f, unnest(f.chapters) c WITH OFFSET i
             -- PG: FROM/alias shape          BQ: WITH OFFSET
      WHERE f.t BETWEEN c.start_t AND c.end_t
    ) TO (:'prefix' || i::text || '.mp4')
             -- PG: COPY TO expression, :: cast
      WITH (video_codec 'libx264', crf :crf)
             -- PG: sink options

(`* REPLACE` and `WITH OFFSET` are proposed borrowings, not yet
landed; the rest compiles today.)

Three seams, each with a rule:

1. **Substitution runs before parsing, so psql variables appear
   inside BigQuery constructs freely.** `STRUCT(:'title' AS title)`
   with the variable unset becomes `STRUCT(NULL AS title)` — and the
   absence rule then does what it always does (a NULL field clears
   its key). One pipeline; the halves never negotiate.
2. **Calls are where they meet**: Postgres binding applied to
   BigQuery-shaped values. `VARIADIC ARRAY(SELECT frame FROM shots)`
   is the emblem — a PG spread operator consuming a BQ array
   constructor. Same for `=>` options taking struct-typed values, and
   `::chapter` casts on STRUCT literals (already shipping).
3. **The tie-breaker for every future feature**: if the question is
   what a VALUE is — construct, destructure, reshape — take
   BigQuery's spelling. If it is how a STATEMENT binds or directs —
   sinks, variables, bodies, argument binding — take Postgres's. A
   feature that genuinely straddles the line gets decided by the
   maintainer and documented as a deviation either way; none is known
   today.

## If adopted, the order

1. `* EXCEPT` / `* REPLACE` — highest value, self-contained.
2. `ARRAY(SELECT ...)` and `SELECT AS STRUCT`, with
   `unnest(ARRAY[STRUCT(...)])` as a FROM item — completes the
   duality.
3. The removal wave: `VALUES` and the positional `ROW::record`
   literals go, and the `CAST()` census lands its decision.
4. Docs pass stating the two rules — the dialect split AND the
   one-way rule — where the dialect is described, so future feature
   questions get answered by the rules instead of by taste each time.

Each as its own small plan with recipes first, as usual.
