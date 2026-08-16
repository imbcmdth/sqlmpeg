# 044 — RFC-005 polish: docs, README, v0.8.0  (model: sonnet · main · final
RFC-005 wave)

Read plans/rfc-005-everyday-gaps.md and the 040-043 landed state. Close out
the release: docs coherent, README showing the new powers, version bumped.

## Deliverables
1. README (voice: earnest per the established register; NO fenced-block
   edits to existing examples; new examples get real compiled commands and
   content-keyed drift-pin tests in test_lower.py following the pattern):
   - "Streams are columns" or a new short section: one generated-source
     example (the silent-audio concat compile, or the sine one-liner — a
     query with NO -i at all is a lovely beat), drift-pinned (exec — needs
     the registry).
   - Trims/effects: one sentence + example for enable (windowed blur) and
     the centered overlay expression — the centered overlay is symbolic,
     goldenable, and drift-pinnable without exec if compiled --no-probe?
     expressions need no probe: pin it however the pattern fits best.
   - input-options sentence exists from 041; verify placement reads well.
2. docs/dynamic-filters.md: CORRECTION flagged by 043 — the collision
   census section claims `pad`'s bare spelling "was already the stdlib's";
   in practice sqlglot's PAD grammar makes bare pad(...) unreachable
   (3-arg ParseError, 4-arg arrives argless). State the truth: stdlib pad
   exists in the table but is only reachable ... IT IS NOT reachable — the
   honest text: "the stdlib `pad` entry is currently shadowed by Postgres
   grammar; use `ffmpeg.pad(...)` with named options, which loses the
   centered-by-default ergonomics — a rename of the stdlib entry is under
   consideration". Do not rename anything yourself.
3. docs/errors.md: verify captured examples against live validate --json
   (the <expr> label change touched UDF_ARG_TYPE got-lists — check that
   section's example; recapture only what drifted). enable/expr get
   mentions where the FILTER_OPTION codes are described.
4. prompt.py: confirm 042/043's additions read coherently as one document
   (Sources bullets, enable, expr) — light copyedit pass only; regen.
5. Version 0.8.0: pyproject version line, __init__, README status token.
6. Full gate, paste: pytest tests/ -q; pytest -m exec -q; ruff check .;
   mypy sqlmpeg/; the README example compile outputs.

## Do NOT
Touch lower/parser/emit/split/ir/registry/stdlib/inputs/sink source, or
goldens beyond drift-pin needs. Baseline 1211 + 92.
