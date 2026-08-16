# 037 — CLI: SQL string is the default input  (model: sonnet · main ·
parallel with 035 — do NOT touch lower/split/emit/ir or their tests)

User direction: `sqlmpeg compile "SELECT ..."` should just work; files move
behind a flag. Nothing is released, no compat shims.

## Deliverables
1. cli.py, for compile/explain/validate/run:
   - positional `query` = SQL TEXT (optional in argparse terms, but exactly
     one of query/-f required — enforce in the handler, usage error exit 2).
   - `-f/--file PATH` reads the query from a file; `-f -` reads stdin
     (keeps the LLM repair-loop pipe). Both given or neither given → usage
     error naming the rule.
   - Muscle-memory guard: when the positional SQL fails with PARSE_ERROR and
     the string names an existing file OR ends with .sql/.SQL, append/replace
     the hint with "did you mean -f <arg>?" (CLI layer only — SqlmpegError
     from the library is not modified; wrap/augment at print time).
   - prompt subcommand unchanged (no query).
2. tests/test_cli.py: rework existing tests to the new convention (inline SQL
   where a file was incidental; -f where file-reading itself is under test);
   new tests: inline happy path per subcommand, -f path, -f - stdin, both/
   neither usage errors, the did-you-mean-f hint (positional that is an
   existing .sql file), and validate --json on an inline string.
3. prompt.py: the Repair-loop section's shown command becomes
   `sqlmpeg validate --json "<your query>"` (or -f note); regen
   docs/system-prompt.md.
4. docs/errors.md: invocation lines (`sqlmpeg validate query.sql --json`)
   updated to the new convention; captured JSON blocks unchanged (output is
   identical either way — verify one spot-check).
5. README: usage lines updated (`sqlmpeg run -f query.sql -o out.mp4` /
   inline examples where snappier). Do NOT touch the compiled-command blocks
   (drift-pinned by tests plan 035 owns).

## Verify
ruff; mypy --strict sqlmpeg/cli.py sqlmpeg/prompt.py; pytest
tests/test_cli.py tests/test_prompt.py tests/test_docs.py -q green; full
`pytest tests/ -q` green EXCEPT failures attributable to plan 035's
concurrent in-flight edits (identify by file, report). Do not touch
pyproject.toml (pending user edit). No git commands.
