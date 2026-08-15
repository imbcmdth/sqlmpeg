# 022 — v2 polish: CLI, docs, prompt, version  (model: sonnet · wave D ·
branch v2-streams, after 021)

Read plans/000b-interfaces-v2.md, RFC-001, committed branch.

## Deliverables
1. cli.py: `--no-probe` on compile/explain/validate (maps to
   compile_sql(probe=False)); run keeps probing. Update tests.
2. Version 0.2.0 (pyproject + __init__).
3. README: streams section (the remap one-liner, reverb-all-languages
   example), BREAKING CHANGES block (implicit audio copy removed — select
   audio explicitly; [out] → out0...; IR shape), keep v0 PiP example (still
   compiles) updating its shown command output to the real v2 emission
   (run `sqlmpeg compile` and paste actual output).
4. docs/errors.md + error-schema.json: new codes (real captured JSON),
   SINGLE_OUTPUT_ONLY marked retired (kept for compat). stdlib.md +
   system-prompt.md regenerate; prompt.py dialect section updated for
   streams/broadcasting (subscripts 1-based, SELECT list = output streams,
   audio functions, splat/broadcast rules, new repair guidance for
   STREAM_NOT_FOUND / INPUT_NOT_FOUND / BROADCAST_MISMATCH) with new worked
   examples — every example must compile (existing test enforces).
5. Full gate: ruff, mypy sqlmpeg/, pytest, pytest -m exec — all green.

## Verify
Paste gate outputs + `sqlmpeg compile` of the README examples. No git commands.
