# 039 — Open-ended time windows  (model: sonnet · main)

`WHERE a.t >= 120` (tail of the file, no end time) and `WHERE a.t <= 60`
(head only) join BETWEEN as legal time predicates. Kills the "BETWEEN 120
AND 3600" placeholder wart from the ad-splice pattern.

## Surface rules
- Per alias, at most ONE lower bound and ONE upper bound, supplied by any of:
  `t BETWEEN a AND b` (both), `t >= x` (lower), `t <= y` (upper), or
  `t >= x AND t <= y` (both, equivalent to BETWEEN). A second bound of the
  same kind for one alias → UNSUPPORTED_SQL (mirrors today's one-BETWEEN
  rule).
- Both operand orders accepted: `a.t >= 120` and `120 <= a.t` are the same
  predicate (exact mirror, not approximation). Verify sqlglot's parse shapes
  for both orientations empirically (exp.GTE/exp.LTE arg layout).
- STRICT inequalities `>` / `<` → UNSUPPORTED_SQL with hint "use >= / <=:
  seeks are time-based, a strict bound has no frame-level meaning" (guardrail
  #3: reject, never approximate).
- Both bounds present and start >= end → UNSUPPORTED_SQL ("empty time
  window") at compile time instead of an ffmpeg runtime error.
- Bounds are numeric literals, same as BETWEEN today.

## Lowering
- Input alias: lower bound → `-ss <x>`; upper → `-to <y>`; either may be
  absent. Caption rule unchanged (any window + selected subtitle/data of
  that alias → the existing desync rejection).
- CTE name: `trim`/`atrim` with only the present bounds (`trim=start=X`,
  `trim=end=Y`, or both) + the setpts rebase as today. Video/audio only,
  as today.

## Plumbing
- ir.py: Graph.input_trims values widen to
  `tuple[float | None, float | None]` (at least one non-None). to_dict
  renders null for an open end ([120, null]); from_dict accepts. Existing
  two-bound goldens byte-identical (verify).
- emit.py: Emitted.input_trims same widening; build_ffmpeg_args renders
  -ss/-to only for present bounds.
- lower.py: _collect_trims accepts the new conjunct forms and merges bounds
  per alias; _trim (CTE path) builds args from present bounds only.
- parser.py: WHERE structural validation accepts GTE/LTE conjuncts on
  <alias>.t with a numeric literal (both orientations), rejects strict
  ops with the hint, keeps everything else rejected as today.

## Tests
- parser: accepted shapes (>=, <=, flipped, mixed with BETWEEN on other
  aliases), rejections (strict ops, double lower bound, bound + BETWEEN
  overlap on same alias, non-literal bound).
- lower/emit: tail-only ss-no-to argv; head-only to-no-ss; >= + <= merged;
  CTE open trim node args; empty-window rejection; caption rule with open
  window.
- golden: 031-trim-tail (symbolic, `WHERE a.t >= 5`, pins [5, null]).
- exec: tail trim on testsrc.mp4 (`t >= 1` → duration ≈ 1.0s re-encoded /
  range-bounded copied, same tolerances as the existing seek tests); the
  ad-splice from the README discussion now written WITHOUT the placeholder
  end (three-way UNION ALL, last branch `t >= 1`) — compile + run + duration.
- Docs: trimming.md (surface section gains the open forms), prompt.py
  time-selection text + regen, README Trims sentence if it mentions BETWEEN
  exclusively. errors.md only if a captured example drifts (check).
- Version stays 0.6.x — bump patch? No: 0.7.0 (surface change). pyproject
  version line + __init__ + README status token.

## Verify
ruff; mypy --strict changed modules; pytest tests/ -q FULLY green; pytest
-m exec -q green; git diff on pre-existing goldens empty. Baseline 989 + 73.
No git commands; do not touch pyproject beyond the version line.
