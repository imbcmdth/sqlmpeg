# 053b — Macro-dependent respell + prompt rewrite  (model: sonnet ·
branch v3-uniform · RFC-007 wave 4; 052+053a landed, macros exist)

## Deliverables
1. Respell the 053a-deferred red set to sqlmpeg.* spellings:
   - tests/golden/096-ad-insert.sql (sqlmpeg.delay), 060-blur-regions-
     macro.sql (sqlmpeg.blur_regions); regen + eyeball.
   - tests/test_lower.py deferred sections: respell tests that assert
     DISTINCT behavior (broadcasting delay over arrays, ad-insert
     end-to-end, enable-on-macro rejection); DELETE tests that
     tests/test_macros.py now duplicates (single-expansion shape checks).
     State which went which way.
   - tests/exec/test_exec.py: 2 deferred end-to-end tests respelled.
   - docs/examples.md recipes 12 (sqlmpeg.speed), 17b (sqlmpeg.delay),
     19a (sqlmpeg.blur_regions): sql fence respell + command fence
     recompile ONLY, prose untouched.
2. sqlmpeg/prompt.py rewrite (the 051 stub dies): a much shorter machine
   contract — the calling convention rule (streams first, positionals in
   ffmpeg declared order, named =>, enable), the three namespaces with the
   macro trio's exact signatures, sinks/COPY + input-trim essentials kept
   from the old text, and the filter reference rendered from the live
   registry when available. The base/--dynamic distinction collapses: ONE
   prompt; without ffmpeg it emits the contract plus a note that filter
   names/options resolve against the installed ffmpeg. Keep the
   validate --json loop guidance (the portable-prompt story). cli.py
   `prompt` subcommand flags: drop --dynamic (keep parsing? NO — delete;
   pre-1.0). tests/test_prompt.py rewritten to match (content-keyed, not
   full-text pins).
3. FIREWALL: do not touch README.md, docs/*.md prose (examples.md fences
   only), docs/stdlib.md, docs/dynamic-filters.md, tests/test_docs.py —
   orchestrator owns those this wave.

## Verify
ruff + mypy --strict on prompt.py; pytest tests/test_prompt.py,
test_lower.py, test_golden.py, test_examples.py, exec -m exec; full suite
--continue-on-collection-errors tail attributed (remaining red should be
ONLY test_docs/stdlib.md surfaces, which are the orchestrator's). No git.
