# 004 — parser.py: parse + resolve pass  (model: opus · wave 2)

Read `plans/000-interfaces.md`, the "SQL dialect (v0 surface)" and "Passes" sections
of `sqlmpeg-project.md`, and the existing `sqlmpeg/errors.py` + `sqlmpeg/ir.py`.

## Deliverables
`sqlmpeg/parser.py` with `parse()` and `resolve()` per the contract.

### parse(text)
- `sqlglot.parse_one(text, read="postgres")`; wrap any sqlglot error in
  `SqlmpegError(PARSE_ERROR)` with line/col extracted from the sqlglot error when
  available. Empty/whitespace input → PARSE_ERROR too.

### resolve(tree)
- Accept only: a `Select`, optionally with a `With` (CTEs), optionally a top-level
  `UNION ALL` of such selects (leave union handling structure intact for lower;
  resolve inputs across all branches).
- `FROM input('path') alias` and comma cross-joins of those; also CTE names as
  from-clauses. `input()` requires exactly one string literal arg. Missing alias on
  an `input()` table function → UNSUPPORTED_SQL with hint "add an alias".
- Input dedup: same path string → same ffmpeg input index (first-appearance order).
  NOTE the README example maps the SAME file to TWO indices when it appears under
  two aliases (`-i game.mp4 -i game.mp4`) — so dedup key is the alias, one index per
  distinct ALIAS, paths may repeat. CTE aliases do NOT get input indices.
- Reject with typed, line-anchored errors:
  - GROUP BY / HAVING / ORDER BY / LIMIT / OFFSET / DISTINCT / window functions /
    aggregates / subquery predicates (IN (SELECT..), EXISTS) → NO_STREAMING_EQUIVALENT
  - `UNION` without ALL → NO_STREAMING_EQUIVALENT (hint: use UNION ALL)
  - >1 expression in top-level SELECT list → SINGLE_OUTPUT_ONLY
  - `SELECT *` → UNSUPPORTED_SQL (hint: select a single frame expression)
  - unknown table/alias referenced anywhere → UNKNOWN_ALIAS
  - explicit JOIN syntax (INNER/LEFT/ON) → UNSUPPORTED_SQL (hint: use comma cross-join)
  - anything else outside the surface → UNSUPPORTED_SQL, never a crash.
- Line/col: use sqlglot node token positions when available; fall back to line 1 col 1.
- CTE selects are validated with the same rules (recursively); `ctes` returned in
  definition order. Duplicate CTE name or alias → UNSUPPORTED_SQL.
- WHERE clauses: do NOT interpret here (lower's job) but structurally validate:
  only `<alias>.t BETWEEN <num> AND <num>` conjunctions are allowed; anything else
  → UNSUPPORTED_SQL with a hint about the supported form. Unknown alias in WHERE →
  UNKNOWN_ALIAS.

## Tests
`tests/test_parser.py` — happy paths (single input, two aliases same file, CTEs,
union all) asserting Resolved fields, plus one test per rejection above asserting
`code` and that line is not None. ~20 tests.

## Verify
ruff, `mypy --strict sqlmpeg/parser.py`, pytest — green. No git commit.
