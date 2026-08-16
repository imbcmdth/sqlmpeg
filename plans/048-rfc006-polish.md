# 048 — RFC-006 polish: docs, README, v0.9.0  (model: sonnet · main · final
RFC-006 wave)

Read plans/rfc-006-views-multisink.md and the 045-047 landed state. Punch
list accumulated from those waves' reports:

1. README (earnest voice, drift-pin pattern, no existing fenced-block
   edits): a new "Views and multiple outputs" section with the ABR ladder —
   view + three COPYs, real compiled command (symbolic-compilable: golden
   100-abr-ladder's query is the model; pin via the content-keyed pattern,
   offline since no probe/registry is needed for explicit subscripts).
   A channelsplit round-trip one-liner in the "Any ffmpeg filter" area
   (exec-pinned; needs registry).
2. prompt.py — the big one this wave:
   - FIX the now-false bullets: "Never alias it (FROM pip p is rejected)"
     and "aliasing a CTE" in the Rejected list (aliasing is legal since
     046; branch-local, may not shadow).
   - New "Scripts, views and multiple outputs" section: CREATE VIEW ... ;
     COPY...;+ semantics, one command/N files, views-before-COPYs, unused
     views rejected, view bodies are full queries.
   - Array-returning calls paragraph (the three, namespace-only, count from
     options, results splat/subscript via CTE columns).
   - Worked example: the ladder (plain ```sql — symbolic). Regen
     docs/system-prompt.md.
3. docs/dynamic-filters.md: the fence bullet 047 added likely wants one
   more sentence + the worked channelsplit example; verify the enable and
   census sections still read correctly against 045-047.
4. docs/errors.md: re-verify captured examples against live output (the
   Resolved/sinks churn may have shifted a line/col — check ALL, recapture
   drifted ones). New script-rule rejections (unused view, bare SELECT in
   script, views-after-COPY) get documented under UNSUPPORTED_SQL with one
   captured example.
5. Version 0.9.0: pyproject version line, __init__, README status token.
6. Full gate, paste: pytest tests/ -q; pytest -m exec -q; ruff check .;
   mypy sqlmpeg/; version print; the README ladder compile output.

## Do NOT
Touch compiler source or goldens (beyond drift-pin tests in
tests/test_lower.py). Baseline 1333 + 103. The extractplanes flags-typed
limitation stays documented as-is (047's wording) — the registry change is
a future batch, not this plan.
