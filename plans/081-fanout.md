# 081 — Set-driven output fan-out  (model: opus · branch fanout ·
recipes 47 and 48 are the failing tests)

Read plans/rfc-014-output-fanout.md (accepted). The rule: in a media
COPY over a compile-time row table, a TO EXPRESSION referencing that
table's columns means one command per surviving row; a constant TO
keeps today's semantics byte-for-byte (the full suite is the proof).

Machinery to build on: the value grammar (||, ::text, arithmetic - all
shipped), the command-sequence emission from two-pass (a list of
commands; wrap/&&/run-loop all exist), chapters(f) and unnest row
tables, per-row value evaluation from plan 080's option binder.

Key semantics:
- Per-row world: each command binds ITS row - trim bounds over row
  columns (WHERE f.t BETWEEN c.start_t AND c.end_t -> that row's
  -ss/-to), the path expression, tag columns, per-row call arguments.
  Chapters rows in a media query become LEGAL exactly and only under a
  fan-out TO (they still carry no streams; their columns feed bounds,
  paths, tags).
- Path expression: text-typed value expression; must reference at least
  one row column (else it is a constant and today's rules apply);
  rejections: path separators or '..' in a COMPUTED segment; duplicate
  resulting paths across rows (name the collision); zero surviving
  rows; NULL path (unprobed column) naming the field.
- Per-command output indices restart at 0 (each command is its own
  file): recipe 48 pins -c:0/-metadata:s:0 in BOTH commands.
- v1 rejects: fan-out + two_pass; fan-out in multi-COPY scripts;
  fan-out + chapters/chapters_from/metadata_from sink options (sink
  identifiers resolve per input, fine, but keep the matrix small -
  reject and note); STDOUT/csv fan-out (table queries unaffected).
- WITH options apply to every command identically.

## Tests
Unit: the rule's dispatch (constant TO unchanged - pin an existing
golden shape recompiled identically); per-row bounds/paths/tags; all
rejections above; cross-product fan-out (chapters x tracks) command
count. Exec: recipe 47 RUNS (two chapter files exist, durations ~1s
each); recipe 48 runs (eng.m4a/fra.m4a exist, tags read back).
queries/: add split-chapters.sql (source; chapters drive everything)
and extract-languages.sql (source) with headers; extend
tests/test_queries.py dummies as needed.

## Verify
ruff + mypy --strict; recipes 47-48 green (true-bytes on trivia - the
-ss 0.0 rendering and provenance tokens in the pins are my best guess;
STOP on semantics); full default suite; full -m exec attributed; regen
docs/system-prompt.md (the prompt should state the fan-out rule in one
short block). Report: dispatch design, rejection wordings, tails.
No git.
