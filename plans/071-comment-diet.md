# 071 — The comment diet  (model: opus · branch comment-diet ·
maintainer directive: comments are "a bit much")

## The style rule
Short and factual. Say WHAT. Say why/how only when necessary. Prefer
self-documenting code (a good name beats a comment restating it).
Delete: narrative blocks restating the code, plan/RFC archaeology that
git history already records ("plan 051 did X", "an earlier draft..."),
comments explaining a change's correctness to a reviewer, restated
docstring content, section banners that a blank line serves as well.
SHARPENED (maintainer, mid-wave): EVERY RFC/plan-number citation goes -
"stop mentioning RFCs in comments that no one will read" - module
docstrings included. Keep the fact, drop the citation.

## Preservation guardrails (NON-NEGOTIABLE)
- MEASURED FACTS stay: every empirically-pinned behavior record - the
  tpad stop=-1 hang, caption retiming under input seeks, the dedup
  consecutive-run rule, positional-binding fidelity notes, the sqlglot
  parse-shape tables (parser.py docstring), snapshot order-preservation
  warnings, the data:-URI capture, ffmpeg-9 acrossfade variance.
  CONDENSE the telling, KEEP the fact and its "measured, not guessed"
  status - these prevent regressions no test can express.
- Guardrail references (#2 valid-Postgres, #4 tables-are-data, #7
  no-panics) stay where they justify a non-obvious shape.
- Module docstrings may shrink but keep their CONTRACT statements (what
  the module promises, not how it got there).
- Behavior changes: NONE. Test changes: comment/docstring text only.
- When in doubt whether a comment is load-bearing, keep it and list it
  in the report for orchestrator review.

## Scope
sqlmpeg/*.py first (the shipped surface); tests/ only where comments
are egregious narrative (fixture docstrings that tell wave stories).
plans/ and docs/ untouched (docs are prose by design).

## Verify
ruff + mypy --strict across sqlmpeg/; FULL default suite green; full
-m exec green (nothing behavioral can move, so any test failure is a
STOP - you deleted something load-bearing). Report: per-file
before/after comment-line counts, the kept-but-flagged list, tails.
No git.
