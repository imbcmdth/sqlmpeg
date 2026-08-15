# 016 — parser v2  (model: opus · wave B · branch v2-streams)

Read plans/000b-interfaces-v2.md (§ parser.py), RFC-001, current parser.py.

## Deliverables
Update `sqlmpeg/parser.py` + `tests/test_parser.py`:
- Multiple projections legal (drop SINGLE_OUTPUT_ONLY raise; keep the
  no-projection check).
- Projections/args may contain: `alias.video[<int>]` / `alias.audio[<int>]`
  (verify sqlglot's parse shape for subscripts empirically — likely
  exp.Bracket; also check `a.video [ 1 ]` whitespace and chained `[1][2]`,
  reject chains), bare `alias.video` / `alias.audio`, `alias.frame` (legal,
  sugar resolved in lower). Subscript literal must be integer >= 1 →
  UNSUPPORTED_SQL "stream subscripts are 1-based" otherwise (0, negative,
  float, string, expression).
- Column-name whitelist: frame|video|audio|t on INPUT aliases. For CTE-alias
  qualifiers, ANY column name is structurally legal (checked in lower);
  parser only verifies the qualifier is in scope.
- `t` rules unchanged. Existing rejections all preserved — keep every current
  test passing unless it asserts SINGLE_OUTPUT_ONLY (rework those: two
  projections is now valid; two projections where one is a literal is still
  rejected — but by lower, so drop from parser tests).

## Verify
ruff + mypy --strict sqlmpeg/parser.py; pytest tests/test_parser.py
tests/test_ir.py green. EXPECTED RED: lower/golden/cli/prompt (later plans).
No git commands.
