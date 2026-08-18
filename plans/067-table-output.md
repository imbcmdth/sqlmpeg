# 067 — Table output + CSV + default run  (model: sonnet · branch
ffmpeg-required · RFC-011; cookbook recipes 30-32 are the red targets)

Read plans/rfc-011-table-output.md (authoritative, as amended: run is
the DEFAULT subcommand unconditionally; ASCII psql format).

## STOP gate: sqlglot empirics FIRST
Parse shapes (read="postgres") for: `COPY (...) TO STDOUT WITH (format
'csv', header true)` — the STDOUT target's AST shape vs a quoted path,
and how the format/header options arrive vs the existing sink options.
Also `SELECT t.language ...` metadata-column outputs (currently a typed
rejection — find where, relax for table mode only). Write findings down.

## Deliverables
1. Query classification: a statement set with no media COPY and no -o
   is a TABLE QUERY; COPY TO STDOUT/'.csv-with-format-csv' is a CSV
   sink; anything with a media destination stays a media query
   (including -o injection). Metadata columns legal as outputs in
   table/csv queries ONLY; the media-query rejection is unchanged.
2. sqlmpeg/table.py (new): the renderer. Format is PINNED by recipes
   30/31 byte-for-byte: one leading space per cell, cells ljust to
   max(header, values) width, " | "-joined, dashed rule with "+" at
   separators (width+2 dashes per column), "(N rows)"/"(1 row)" footer,
   EVERY LINE RSTRIPPED (an empty last cell ends at its "|"). Stream
   cells render "<video 0:v:0>"-style: type + the ffmpeg stream spec
   for source passthroughs, node ref (n2) for filtered streams. NULL
   cells empty. CSV per the csv module's defaults + header option;
   quoting only when needed (csv.writer default).
3. cli.py: run prints the table / writes-or-prints CSV for table
   queries (NO ffmpeg execution, no ffmpeg needed at all); run on media
   queries unchanged. `run` becomes the DEFAULT subcommand: argv whose
   first token is not a known subcommand dispatches to run verbatim
   (no gating - a typo dies in the SQL parser as PARSE_ERROR).
   compile/validate on table queries: compile -> typed usage message
   pointing at run; validate unchanged (compiles = valid). explain
   unchanged.
4. Fences: aggregates/GROUP BY etc. stay rejected everywhere (add a
   test proving COUNT(*) over rows is still NO_STREAMING_EQUIVALENT);
   media sink options in a csv COPY and csv options in a media COPY
   are typed rejections (separate small option table, NOT SINK_OPTIONS).
5. Tests: test_cli (default dispatch incl. flag-first argv, table
   printing, compile-on-table-query message), test_table.py (renderer
   unit: widths, rstrip, empty cells, 1-row footer, placeholder forms),
   parser tests for classification/relaxation/rejections, csv sink
   tests (stdout + file + header false + option cross-rejections).

## Surface
sqlmpeg/{parser,lower,emit,cli}.py as needed, sqlmpeg/table.py +
sqlmpeg/sink.py or a new csv option table, tests/*. NOT docs/examples.md
(pins are the spec - report true bytes if your correct render differs on
trivia; semantic divergence = STOP), not README/docs prose (068's).

## Verify
ruff + mypy --strict; full default suite green; `pytest
tests/test_examples.py -m exec -q` fully green including 30-32; full
`-m exec` tail attributed. Report: parse findings, classification
design, renderer notes, repin list if any. No git.
