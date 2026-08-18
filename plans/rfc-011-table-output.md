# RFC-011 — Table output: SELECT prints rows, COPY writes CSV

Status: draft 2026-08-18, rolling into 0.12.0 with RFC-010. The row
model (RFC-009) made every metadata value compile-time; this RFC makes
them VISIBLE. A metadata query needs no execution at all — the compiler
holds every cell the moment compilation ends — so sqlmpeg becomes an
ffprobe front-end that speaks joins, and intermediate row tables
(unnest, joins) become inspectable in plain language.

## The contract change

The SELECT list is the result set; **COPY is what makes it a file**.

- A statement set with NO media destination (no media COPY, no `-o`) is
  a **table query**: `run` prints its result set as a psql-style table
  and executes nothing. ffmpeg runs only for a media destination.
- `-o` stays as the implicit media COPY it always morally was — today's
  `run -f q.sql -o out.mp4` workflow is unchanged.
- `compile` remains "show me the ffmpeg command": on a table query it is
  a typed usage message pointing at `run` (there is no command to show).
  `validate` unchanged. `explain` unchanged (IR dump; a table query's
  graph is just small).

## Table mode

- **Metadata columns become legal SELECT outputs in table mode** (the
  existing "streams are the only outputs" rejection now applies only to
  media queries). Stream-valued cells print a REF-BEARING placeholder —
  `<video 0:v:0>`, `<audio n2>` — because the ref tells you what would
  have been wired, which is the join-debugging view. NULL cells print
  empty, like psql.
- Any sink-less SELECT prints, streams-as-placeholders included:
  `SELECT a.track, b.track FROM … FULL OUTER JOIN …` as a table IS the
  join inspector.
- Format: psql homage — aligned columns, header, `(N rows)` footer.
  Exact bytes pinned by the TDD recipes.
- Column headers: the SELECT alias when given, else the column
  expression's natural name (`language`, `track`, …).

## CSV

Stock Postgres COPY, nothing invented (guardrail #2):

    COPY (SELECT t.language, t.codec FROM …) TO STDOUT WITH (FORMAT csv, HEADER true)
    COPY (…) TO 'tracks.csv' WITH (FORMAT csv, HEADER true)

- `FORMAT csv` is REQUIRED to make a COPY a table sink (PG's own rule —
  its default format is text, so a bare `.csv` path without the option
  gets a hint, not an inference).
- `HEADER true|false` supported; default false (PG's default).
- A csv COPY takes table-mode columns (metadata legal, streams as
  placeholders); media sink options in a csv COPY are a typed rejection
  and vice versa (a separate small option table, not SINK_OPTIONS).
- TO STDOUT prints; TO '<path>' writes the file (`run` does the
  writing; `compile` has nothing to show, same as table mode).
- STOP-gate empirics: sqlglot parse shapes for TO STDOUT and for
  WITH (FORMAT csv, HEADER true) under read="postgres" BEFORE building.

## Fences (deliberate refusals)

- Aggregates, GROUP BY, COUNT(*) stay rejected — row tables make them
  computable, and we decline the slope toward being a database. The
  fence line: sqlmpeg answers "what tracks are there", not "how many".
- A MEDIA copy selecting metadata columns stays rejected (existing).
- Table mode never executes ffmpeg, never decodes, never probes deeper
  than compilation already did.

## Waves (inside 0.12.0, after RFC-010's 065 lands)

- TDD first: cookbook recipes — inspect a file's tracks (bare SELECT,
  metadata + placeholder columns), the join-inspector table, CSV to
  stdout. Pins are the exact table/CSV bytes.
- 067 (agent): STOP-gate empirics; parser/sink surface (STDOUT, csv
  option table); the table renderer (own module, e.g. sqlmpeg/table.py);
  CLI rework per the contract; tests.
- 068 (orchestrator): docs for RFC-010 + RFC-011 together (README
  install story + the "run prints tables" story, tracks.md, errors.md
  touch-ups), full green, 0.12.0, tag, push.

## Non-goals

Interactive REPL/pager; JSON output format (explain exists; add only on
demand); aggregates (above); table output for `compile` (it shows
commands); Parquet and friends (it's a video tool, not a warehouse).
