# 091 — Remove -o: the query names its destination

Maintainer direction (2026-08-20): `-o` is already unadvertised
(README swept, help suppressed); this plan removes it. One way to name
a destination: `COPY ... TO`, parameterized with `-v` variables when
it should vary. The payoff is a simpler CLI contract: a query without
`COPY ... TO` IS a table query, always — no flag can reinterpret it.

## The simplified contract

- `run`: COPY with a media destination → compile and execute; no COPY
  (or COPY TO STDOUT) → print the table/CSV. Nothing else.
- `compile`: requires a media COPY; a table query keeps its existing
  refusal. The `out.mp4` placeholder dies with the flag
  (`_DEFAULT_OUT`, cli.py:87) — compile never invents a destination.
- An unknown `-o` argument then fails as argparse's standard
  unrecognized-argument error (exit 2). Acceptable; no bespoke
  migration message (pre-1.0, and the flag was already hidden).

## Code (sqlmpeg/cli.py only — the compiler never saw -o)

- Both suppressed `add_argument("-o", ...)` blocks (~163, ~186).
- `_DEFAULT_OUT` and every `args.output` consumer: the
  `_reject_output_override` multi-COPY usage error (~321) becomes
  dead — delete; run's "no output path given: pass -o, or use COPY
  ... TO" (~543) reduces to the table branch (a sink-less query IS a
  table query — the error may become unreachable; if so, delete it);
  the table-vs-media dispatch (~415, ~489, ~513, ~520) loses its
  `args.output` terms.
- Module docstring: compile/run output-path prose (~20-36).

## Tests

- tests/test_cli.py: 16 `"-o"` sites — override/precedence/usage-error
  tests deleted or inverted (the multi-sink -o usage error test
  becomes an unrecognized-argument assertion if kept at all);
  table-dispatch tests lose the -o arms.
- tests/test_fanout.py: 1 site; tests/exec: 2 sites — rewrite to COPY
  TO forms.

## The cookbook: 37 recipes show -o

Every `$ sqlmpeg [compile] -f query.sql -o <path>` recipe rewrites to
the COPY form:

- The query fence gains `COPY ( ... ) TO :'dest'` (or a literal path
  in fixture-bound recipes) — for bare-SELECT recipes this is purely
  additive wrapping; WHERE/trim/UNION shapes wrap unchanged.
- The shown command drops `-o` and gains `-v dest=<path>` where the
  recipe is parameterized.
- THE PINNED ffmpeg BYTES DO NOT CHANGE (same destination, same
  graph) — every rewrite is verified byte-for-byte against the
  existing pin; a changed pin means the rewrite is wrong.
- Recipe 4's first variant (trim, `-o cut.mp4`) and friends: the
  prose sentence mentioning -o (if any) updates; recipe 33 (the
  variables mechanism) checked for -o mentions.

## queries/ and other docs

- queries/*.sql: 0 files use -o (already COPY TO) — headers'
  example lines re-checked anyway.
- docs/filters.md, trimming.md, rows.md, errors.md: no -o mentions
  found; sweep to confirm at implementation time.
- README: already clean. prompt.py: never documented the CLI; sweep
  to confirm.

## Waves

1. Orchestrator: this plan committed (recipes change SQL + shown
   commands but keep pinned bytes, so the red-first ritual does not
   apply cleanly — the wave rewrites and verifies against the
   UNCHANGED pins instead).
2. Implementation (sonnet): cli.py + tests + the 37-recipe sweep +
   confirmation sweeps. ruff/mypy/full suites green; every touched
   recipe's pinned bytes identical.
3. Orchestrator: README CLI-reference sanity check (the usage string
   is already -o-free), release per procedure — breaking, so a MINOR
   bump (0.24.0).
