# 063 — RFC-009 close-out  (branch v4-track-rows; everything green, 1493 + 151)

Split: prompt.py (sonnet, machine contract) · docs prose + merge
(orchestrator, in-session).

## Agent half: prompt.py + tests/test_prompt.py
Add the track-rows section to the LLM contract: unnest(alias.type) rows,
the column tables per stream type (audio/video/subtitle - from
plans/rfc-009-track-rows.md § Columns, verified in parser.ROW_SCHEMAS),
WHERE/ORDER BY over rows (compile-time; NULL matches nothing), joins
between unnest tables only (INNER/LEFT/FULL + comma cross; ON grammar),
COALESCE fills per type (anullsrc / color / sqlmpeg.empty_captions with
inherit-when-omitted duration rule), row order = left side's order. One
or two worked examples mirroring cookbook recipes 23 and 26 (respell
paths to generic film.mkv-style names; prompt examples must COMPILE -
test_prompt runs them, so keep them probe-free or registry-only...
verify against test_prompt's example-compile harness and follow its
existing conventions). Regenerate docs/system-prompt.md via
scripts/gen_prompt.py. Content-keyed test updates only. ruff + mypy
--strict + full default suite green. No other files.

## Orchestrator half (mine)
docs/tracks.md (new page: the row model, columns, joins, fills, NULL,
ordering); filters.md + README cross-links; README ideas bullet +
cookbook count 28; full green; merge v4-track-rows -> main FF-ONLY;
uv version 0.11.0; annotated tag; push with --follow-tags.
