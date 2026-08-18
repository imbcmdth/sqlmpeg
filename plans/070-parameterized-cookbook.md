# 070 — The cookbook becomes programs + queries/  (model: sonnet · branch
cli-vars · runs AFTER 069 lands; the harness is the referee throughout)

Maintainer directive: most recipes get parameterized so each is a
runnable program. The -v mechanism (069) and recipe 33 show the forms.

## Part 1: parameterize the cookbook
- For every recipe whose paths are ILLUSTRATIVE (film.mkv-style): sql
  fence paths become :'source' / :'dest'-style variables (pick short,
  recipe-appropriate names; two inputs -> :'main' / :'overlay' etc.),
  and the shown `$ sqlmpeg` line gains the matching `-v name=value`
  flags carrying the OLD literal values - so the pinned OUTPUT line is
  UNCHANGED (substitution reproduces it; verify per recipe through the
  harness, byte-for-byte).
- KEEP CONCRETE: recipes whose fixture paths are the contract (18,
  23-32: tests/fixtures/... exec recipes), and any recipe where a
  variable would obscure the lesson - use judgment, keep a ledger.
- Bare `:name` (raw/numeric) gets a SHOWING in exactly two or three
  natural spots (e.g. recipe 12's speed factor, a trim bound in 17) -
  not everywhere; the forms stay teachable.
- Prose lines are the orchestrator's: do NOT rewrite recipe prose. If a
  prose sentence contradicts a parameterized fence (names a literal
  path as "the query says X"), flag it in the ledger instead of editing.

## Part 2: queries/ directory
- New top-level queries/ with 6-8 ready-to-run programs distilled from
  the cookbook, each a .sql file with a short header comment: one-line
  purpose, `-- variables: name (what it is), ...`, one example
  invocation line. Candidates: transcode.sql, extract-audio.sql
  (language-selected via track rows), concat-fill.sql, pip.sql,
  tracks-to-csv.sql, remote-tracks.sql (the angel-one shape: pick
  video by resolution + audio by codec from a URL). No README.md - the
  orchestrator writes it.
- Comment style: short and factual (maintainer rule).
- Tests: a new tests/test_queries.py - parametrized over queries/*.sql,
  parse the `-- variables:` header, compile each file through cli.main
  with dummy -v values (paths like in.mp4/out.mp4), asserting exit 0.
  HERMETIC: stub probe with a rich synthetic ProbeResult (video+audio
  with language/codec/channel_layout metadata so track-row queries
  compile); registry comes from the conftest snapshot shim. A header
  test: every file HAS the variables header and an example line.

## Verify
Full cookbook harness green both tiers (offline + `-m exec`) -
byte-for-byte, zero drift on pinned outputs; test_queries.py green;
full default suite green; full -m exec tail attributed. ruff on touched
test files. No git. Report: the ledger (parameterized / kept-concrete /
prose flags), queries list with their variables.
