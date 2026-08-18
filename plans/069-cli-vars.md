# 069 — CLI variables: psql-style -v / :'name'  (model: sonnet · branch
cli-vars · recipe 33 is the red target)

Decisions (maintainer-approved plan, C:\Users\imbcm\.claude\plans\
polished-giggling-shamir.md has the full text): psql convention exactly.
`-v/--set NAME=VALUE` repeatable; `:'name'` -> escaped single-quoted
literal ('' doubling); `:"name"` -> double-quoted identifier; bare
`:name` -> raw text; `::` casts and lone `:` untouched; substitution
skips '...' strings, "..." identifiers, -- and /* */ comments.
Undefined variable referenced -> SqlmpegError(UNSUPPORTED_SQL) with the
reference's real line:col, message naming it, hint listing defined
names (or "define it with -v name=value"). Duplicate -v: last wins.
Unused -v: silent. Malformed -v (no '=', name not
[A-Za-z_][A-Za-z0-9_]*): hand-rolled usage error, exit 2 (mirror
cli.py:201-205 style).

## Deliverables
1. sqlmpeg/vars.py (new): `substitute(text, variables) -> str`, the
   quote/comment-aware scanner above, offset->line:col tracking for the
   error. Comment style: short, factual, WHAT-focused (maintainer rule;
   no narrative blocks).
2. cli.py: `-v/--set` in `_add_query_arguments` (cli.py:125) - covers
   all four query subcommands and naked dispatch for free; parse pairs
   + substitute inside `_resolve_query` (cli.py:190-218) so every
   handler inherits it; SqlmpegError from substitution renders via the
   existing _print_error path, exit 1 (validate --json emits the error
   object as usual). Nothing below cli.py changes.
3. Tests (hermetic - no fixtures, no probing, per the 067 lesson):
   tests/test_vars.py for the scanner (three forms; quote-doubling;
   ::cast; :var inside string/line-comment/block-comment untouched;
   lone colon; adjacent forms; empty value; missing-var line:col).
   tests/test_cli.py: end-to-end compile with -v; --set alias;
   duplicate last-wins; malformed -v exit 2; missing-var through
   compile (exit 1, error: prefix) AND validate --json (object on
   stdout); naked dispatch `sqlmpeg -f q.sql -v a=b`; unused -v silent;
   -v on explain and run accepted.

## Verify
ruff + mypy --strict on vars.py and cli.py; new tests green; full
default suite green; `pytest tests/test_examples.py -m exec -q` green
INCLUDING recipe 33 (offline tier - report the true command if the pin
drifts on trivia; my pin mirrors recipe 1's codec-flag shape). Full
`-m exec` tail attributed. No git. Report: files, scanner edge notes,
verification tails.
