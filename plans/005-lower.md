# 005 — lower.py + compiler.py  (model: opus · wave 3)

Read `plans/000-interfaces.md`, `sqlmpeg-project.md` "Passes", and the ACTUAL code of
`sqlmpeg/parser.py`, `sqlmpeg/stdlib.py`, `sqlmpeg/ir.py`, `sqlmpeg/split.py` — they
are done; conform to what exists.

## Deliverables
1. `sqlmpeg/lower.py` — `lower(res: Resolved) -> Graph`:
   - Walk the single SELECT expression bottom-up. Column refs `alias.frame` (or bare
     CTE name / `alias.frame`) → FrameRef: `"src:<alias>"` for inputs, the CTE's
     output ref for CTEs. `<alias>.t` outside WHERE → UNSUPPORTED_SQL.
   - Function calls: look up in `stdlib.FUNCTIONS` (case-insensitive match, but
     store lowercase). Unknown → UNKNOWN_FUNCTION with did-you-mean hint via
     `difflib.get_close_matches`. Check arity+kinds against variants; mismatch →
     UDF_ARG_TYPE listing expected vs got (use `stdlib.signatures()`).
     Numeric literals → int/float; string literals → str; frame-kind args must be
     frame-typed subexpressions. Then call `spec.expand(ctx, args)`.
   - `ExpandCtx` impl: fresh ids `n1, n2, ...` in creation order; registers into the
     Graph's node dict.
   - WHERE `a.t BETWEEN x AND y` → prepend `trim` node (`{"start": x, "end": y}`) +
     `setpts` node (`{"expr": "PTS-STARTPTS"}`) onto that alias's source — i.e.
     rewrite the FrameRef for that alias to the setpts node id so every consumer
     of the alias sees the trimmed stream.
   - CTEs lowered first, in order, into the same Graph; their output refs recorded.
     Each CTE's SELECT must also be a single frame expression (else SINGLE_OUTPUT_ONLY).
   - UNION ALL branches each lower to a ref; then one `concat` node
     (`{"n": <count>, "v": 1, "a": 0}`, inputs = branch refs).
   - Output: `graph.output` = final ref. A bare `SELECT a.frame FROM input(..) a`
     (no functions) is legal — output is `"src:a"`.
2. `sqlmpeg/compiler.py` — `compile_sql(text: str) -> Graph`:
   parse → resolve → lower → `split.insert_splits`. Wrap any non-SqlmpegError
   exception as `SqlmpegError(INTERNAL, ...)` — guardrail: no panics on user input.
3. `tests/test_lower.py` — the README pip example lowers to the expected node set
   (assert on Graph.to_dict()); WHERE→trim+setpts; unknown function hint contains
   a suggestion; arity error message contains both signatures; union-all → concat;
   nested calls chain correctly. ~15 tests.

## Verify
ruff, `mypy --strict sqlmpeg/lower.py sqlmpeg/compiler.py`, full pytest — green.
No git commit.
