# 035 — RFC-004 wave 3: input-level -ss/-to as the primary trim
(model: opus · main)

Read plans/rfc-004-star-subtitles.md (§ "Input-level seeking is the primary
trim") and plan 034's contract notes, reproduced here — they are precise and
verified by that author:

1. split.py's insert_splits Graph reconstruction DROPS input_trims (passes
   sink= but not input_trims=) — the sink bug pattern again; fix first.
2. _Env.trims is per-branch; Graph.input_trims is per-graph. Aliases are
   globally unique so merging is safe, but the write is explicit: when
   _collect_trims records a window for an _InputBinding alias, also record
   graph.input_trims[alias].
3. _access/_trim is the split point: input aliases stop splicing filter
   nodes (the ref stays as-is → passthrough now possible under WHERE);
   _CteBinding keeps the filter trim path unchanged; _Env.trimmed memo
   becomes CTE-only.
4. The caption rejection in _access keys on value.type in _PASSTHROUGH_ONLY —
   LIFT it for input aliases (captions now trimmed coherently by the seek);
   KEEP it for CTE bindings. Tests flipping negative→positive:
   test_where_over_a_consumed_subtitle_stream_is_rejected,
   test_where_over_a_consumed_data_stream_is_rejected,
   test_star_plus_where_over_a_captioned_input_is_rejected. Staying negative:
   test_where_over_a_cte_carrying_a_subtitle_column_is_rejected.
5. Emitted needs input-trim carriage: emit() resolves alias-keyed
   g.input_trims against g.sources into `input_trims: list[tuple[float,
   float] | None]` parallel to Emitted.inputs; build_ffmpeg_args renders
   ["-ss", str(start), "-to", str(end)] immediately before the owning -i.
6. Goldens/tests that regen or update: 030-trim-where (trim/setpts nodes →
   input_trims key); test_lower's "WHERE -> typed trim" section (~line 900)
   pins filter-node shapes for INPUT aliases — those rewrite to assert
   input_trims + unchanged refs; CTE-trim tests stay; plus
   test_where_that_does_not_touch_the_caption_alias_still_trims and
   test_a_captioned_input_may_still_be_trimmed_when_captions_are_not_selected
   (assert input_trims now, not ["trim","setpts"]).

## Additional requirements
- Float rendering: -ss/-to values render via the same scalar rendering rules
  emit uses elsewhere (12.5 -> "12.5", ints without decimal point).
- WHERE on an input alias applies to the WHOLE input: document in lower's
  docstring that all streams of that alias — including subtitle/data and
  UNSELECTED ones — are seeked (harmless: unselected streams aren't mapped).
- NEW capability tests: trimmed passthrough (SELECT a.video[1] ... WHERE →
  src ref survives, -ss/-to + -c:0 copy in argv, EXEC: run it, ffprobe
  duration with GOP-tolerant bounds per the RFC accuracy contract — testsrc
  fixtures have sparse keyframes, assert duration <= untrimmed and >= exact
  window, document the tolerance); trimmed captions (avs.mkv WHERE + SELECT *
  → runs, subtitle stream present); accurate re-encoded trim (existing
  tests/exec duration test — verify it still passes: input seek + re-encode
  is frame-accurate).
- Split/emit consume-once etc. unchanged beyond note 1 and note 5.
- UNION ALL branches with per-alias windows: each alias's window lands on its
  own -i; concat unchanged. Add one test.
- compile with probe=False: input trims are probe-independent (pure numbers
  from the SQL) — work symbolically; test.

## Verify
ruff; mypy --strict on changed modules; pytest tests/ -q FULLY green;
pytest -m exec -q green; `git diff --stat tests/golden` shows ONLY
030-trim-where (eyeball it: no trim nodes, input_trims present, refs
unchanged). Do not touch pyproject.toml (user edit pending). No git
commands. Report: the Emitted shape shipped, accuracy test tolerances
chosen, judgment calls, notes for plan 036 (docs/prompt/README polish +
v0.5.0 — including the stale "SELECT * rejected" text in prompt.py:295 and
docs/errors.md:205,224,231 that 034 flagged).
