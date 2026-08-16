# 053a — The great respelling (mechanical)  (model: opus · branch
v3-uniform · RFC-007 wave 3b, PARALLEL with 052 — firewall below)

Plan 051's migration map (in its report, reproduced in the dispatch) is
the authority: 13 unchanged, 7 rename-only, 3 bare-N-input, 13 arg-order/
semantics changes, 3 to sqlmpeg.* (WHICH ARE NOT YET IMPLEMENTED — plan
052 is building them concurrently; every surface needing sqlmpeg.delay /
sqlmpeg.speed / sqlmpeg.blur_regions is DEFERRED to 053b, marked, and left
red).

## Firewall
You may edit: tests/test_lower.py, tests/test_golden.py + tests/golden/*,
tests/test_examples.py + docs/examples.md (FENCES ONLY — respell sql,
recompile commands; prose placeholders/lines stay untouched for the
orchestrator), tests/exec/test_exec.py, tests/regen_golden.py if needed.
You may NOT touch: sqlmpeg/* source, parser/lower (052 owns their deltas),
tests/test_macros.py (052's), prompt/docs beyond examples.md fences,
README.md (orchestrator's).

## Deliverables
1. Respell every red surface that does NOT need a macro, per the map:
   goldens (respell .sql, regen .ir.json, EYEBALL — arg keys move to long
   option names: width/height, out_w/out_h, start_time, duration...);
   test_lower's red sections (delete the dead-concept sections: expr-kind,
   named-extras, macro-machinery tests that 052 replaces; respell the
   rest); test_exec's stdlib spellings; examples.md fences (respell sql,
   recompile the command fence via the harness discipline — offline tier
   respells to bare filters; recipes 17b (delay) and 19a (blur_regions)
   and any speed/crossfade-of-trimmed usage needing macros: DEFER, leave
   red, list them).
2. README flagship/encoding/ladder fences: the PiP demo uses volume/amix/
   overlay/scale — per the map all unchanged EXCEPT verify amix's bare
   spelling now goes through N_INPUT identically (command should be
   byte-identical; if so the fences DON'T move and the pins stay; verify,
   don't assume). The ladder uses scale/volume — same check. If any README
   command drifts (e.g. IR key changes alter emitted args), respell the
   fence and its pinned command and SAY SO — the orchestrator rewrites
   surrounding prose.
3. Keep a precise ledger in your report: every file respelled, every
   deferred-to-053b item, every README fence verdict (moved/unmoved).

## Verify
ruff on touched test files; pytest for every file you finished (green
except the explicitly deferred macro items — list them as the ONLY
remaining red); --continue-on-collection-errors full-suite tail pasted
with attribution. No git commands.
