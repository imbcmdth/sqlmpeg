# 092 — chapters is an array column; unnest it like everything else

Maintainer question (2026-08-21): why is `chapters(f)` a table function
when the input-row model says an input is one row of array columns?
No reason - it predates the model. Make it uniform:

    SELECT c.title, c.start_t FROM input('film.mkv') f, unnest(f.chapters) c

## Semantics
- `chapters` joins the input alias's array columns (rows.md input-row
  table): an array of records (index, title, start_t, end_t) - the
  existing ROW_SCHEMAS["chapters"] schema, no stream column.
- `unnest(f.chapters) c` yields chapter rows exactly as track rows
  work; everything downstream (WHERE, fan-out TO, tag columns, table
  output, trim windows against c.start_t/c.end_t) is unchanged.
- Bare `f.chapters` is a VALUE: legal in table queries (prints as an
  array cell) and in value positions where an array of records makes
  sense; selecting it into a media query is a typed rejection (no
  streams in it), as is subscripting it for now (v1: unnest only).
- `chapters(f)` is REMOVED (one way to say it; pre-1.0 breaking). A
  call to it gets the ordinary unknown-function rejection; add a hint
  naming `unnest(f.chapters)` if cheap.
- Chapter WRITING (`WITH (chapters marks)` from a VALUES CTE,
  `chapters_from`) is unchanged.

## Mechanics
- parser.py: `_INPUT_COLUMNS`/`_UNNEST_COLUMNS` gain "chapters"; the
  table-function path for `chapters(...)` in FROM is deleted;
  `_add_track_rows` binds the chapters schema when the unnest argument
  is the chapters column.
- lower.py: the relation builder reads ProbeResult.chapters for that
  column; `_input_value` rejects bare f.chapters in stream position
  with a plain message; table cells for the bare array (086's array
  cell machinery).
- Rewrites with BYTE-IDENTICAL pins: recipes 39, 40 (if it reads
  chapters), 47 (both forms), queries/split-chapters.sql; tests in
  test_lower/test_fanout/test_parser/exec that spell chapters(f).
- Docs are the orchestrator's (rows.md, dialect.md, prompt.py).

## Checks
ruff, mypy, both suites green; every touched recipe's pinned command
unchanged.
