# 012 — Portable LLM system prompt  (model: opus · wave 5)

Replaces the spec's T4 `sqlmpeg ai` subcommand (decision 2026-08-15): instead of
calling any API ourselves, ship a first-class system prompt so users can bring
their own AI. Success criterion unchanged: an LLM given this prompt should
one-shot ≥8/10 natural-language tasks or repair within 2 validate-loop rounds.

## Deliverables
1. `sqlmpeg/prompt.py` — `build_system_prompt() -> str`. Deterministic, pure;
   function reference section generated from `stdlib.FUNCTIONS` (guardrail #4).
   Content, in order:
   - Role: you translate natural-language video-edit requests into sqlmpeg SQL.
     Output ONLY the SQL query, no prose, no code fences unless asked.
   - Dialect rules (exact, from the implemented surface): FROM input('path') alias
     (alias required); comma cross-joins; CTEs via WITH (no nesting, no forward
     refs, referenced by bare name, reuse is automatic — never re-alias);
     single frame expression in SELECT; <alias>.frame is the only frame column;
     WHERE <alias>.t BETWEEN a AND b (seconds) is the only predicate, one per
     alias, AND-joined; UNION ALL = concatenation (matching fps/resolution);
     video-only, audio copied from first input.
   - What is REJECTED (so the model doesn't try): GROUP BY/aggregates/ORDER BY/
     LIMIT/window functions/subqueries/JOIN ... ON/SELECT */casts/arithmetic in
     args (literals only).
   - Function reference: generated per function from variants — signature with
     param names+kinds, doc line. Note nesting composes filters.
   - 8 worked examples, NL request → SQL, covering: simple scale; crop+scale
     chain; trim; PiP overlay via CTE (the README one); blur_regions;
     text+draw_box; UNION ALL concat; speed+fades. Keep them short and correct —
     VERIFY each compiles with compile_sql before shipping (write a test).
   - Repair loop: run `sqlmpeg validate --json q.sql`; the JSON error contract
     (line/col/code/message/hint); per-code repair guidance in 2-3 lines each.
2. `sqlmpeg/cli.py` — add `sqlmpeg prompt` subcommand printing
   build_system_prompt() to stdout (no args). Update the module docstring list.
3. `scripts/gen_prompt.py` — writes docs/system-prompt.md (newline="\n") with a
   generated-file header naming the regen command. Idempotent.
4. `docs/system-prompt.md` — the committed output.
5. `tests/test_prompt.py` — (a) every SQL example embedded in the prompt compiles
   (extract via a marker convention you define, e.g. sql fences); (b) every
   FUNCTIONS name appears in the prompt; (c) docs/system-prompt.md is fresh
   (regen == committed bytes); (d) `main(["prompt"])` prints it, exit 0.
6. README: short "Use with an AI" section — pipe `sqlmpeg prompt` as the system
   prompt, then loop generate → `validate --json` → repair.

## Do NOT touch
pyproject.toml, scripts/gen_fixtures.py, scripts/gen_docs.py, tests/exec/,
tests/test_fuzz.py, tests/test_docs.py, docs/errors.md, docs/stdlib.md,
.github/ — other agents own these right now.

## Verify
ruff, `mypy --strict sqlmpeg/prompt.py sqlmpeg/cli.py`, pytest tests/test_prompt.py
tests/test_cli.py — green. No git commit.
