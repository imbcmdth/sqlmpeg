# 043 — Timeline enable + expression param kinds  (model: opus · main ·
RFC-005 wave 3)

Read plans/rfc-005-everyday-gaps.md §2 §3 and plan 042's contract notes,
reproduced here (verified by that author):

ENABLE:
- Every named-argument path funnels through `_Lowerer._check_named_args`
  (lower.py ~2270): tier-2 bare calls, ffmpeg.<name> calls, tier-1 named
  extras, and generated sources. Admit `enable` BEFORE the options-dict
  lookup (it is framework-level, never in -help output), gated on the
  target's DynamicFilter.timeline flag (landed in plan 040).
- The callee cannot look filters up; pass the timeline flag in (parameter
  defaulting False). Call sites that know the registry entry:
  _lower_dynamic_call, _add_source (sources: SourceFilter has NO timeline
  field — enable on a source rejects unconditionally), and the tier-1
  named-extras path (_lower_stdlib_call resolves impl.named_target, a
  filter name — needs registry.get(target).timeline).
- Value type: str (an ffmpeg expression). Non-T target -> a
  timeline-flavored UNKNOWN_FILTER_OPTION ("gblur supports enable; scale
  does not" style — use the real flag). Docs give the vocabulary (t, n,
  pos) and say content is ffmpeg-runtime-checked.

EXPR KINDS — LANDMINE FIRST:
- lower._EXPR_KIND = "expr" is ALREADY the _classify fallback label for
  un-lowerable expressions (1+2, NULL, TRUE), compared by equality in
  _match_variant. A new ParamKind "expr" matched by equality would silently
  accept those. RESOLVE by renaming the internal fallback label (e.g.
  "<expr>" or "unsupported") in lower.py — it never leaks into ParamKind —
  and THEN adding ParamKind "expr" meaning "num literal OR str expression".
- _match_variant: expr param matches classify results "num" AND "str"
  (explicit membership, not equality).
- _lower_arguments: third branch — expr params accept _number OR _string,
  passed through verbatim (num stays num for IR cleanliness; str passes as
  the expression string).
- _stream_positions unaffected (video/audio filter) — re-verify.

## Deliverables
1. enable per the notes above; exec tests: blur(f, 5, enable =>
   'between(t,0.5,1.5)') compiles AND runs (real ffmpeg, verify rc=0; a
   pixel check that blur is absent at t<0.5 and present at t=1 mirrors the
   plan-038 transparency test pattern — do it, the fixtures support it);
   ffmpeg.drawbox(..., enable => ...) tier-2 path; enable on scale (non-T,
   verify scale's real flag first) rejected; enable on a source rejected;
   enable under --portable rejected (named-arg policy).
2. ParamKind "expr" migrations in stdlib.py: overlay x/y; pad x/y/w/h (all
   variants); crop x/y/w/h; scale w/h (3-arg variant; factor stays num);
   text x/y (+fontsize only if the live registry types drawtext's fontsize
   as str — check and follow); draw_box x/y/w/h; rotate stays num (we own
   the degrees mapping). Docs lines updated where the kind is user-visible
   (gen_docs renders kinds — stdlib.md will change; regen).
3. FAITHFULNESS exec test: for every FuncSpec param of kind "expr", resolve
   named_target via the live registry and assert the corresponding option
   is str-typed. (Mapping param name -> option name needs a small
   declarative table where they differ — crop's x maps to crop's x (str),
   scale's w to w, etc.; keep it in the test, derived from the expand args
   if practical.)
4. The centering case end-to-end: overlay(f.frame, p.frame, '(W-w)/2',
   '(H-h)/2') — compile (symbolic golden? kinds are stdlib-only, no
   registry needed for compile: YES symbolic — add golden
   098-centered-overlay) + exec run.
5. Broadcast interplay: expr args are scalar literals — verify zip/element
   paths unaffected (test with a broadcast + expr position).
6. prompt.py: Arguments section documents expr params ("a number, or a
   quoted ffmpeg expression like '(W-w)/2'; variables are per-filter and
   checked by ffmpeg at run time") + enable paragraph; regen. docs:
   dynamic-filters.md enable section; trimming.md untouched.

## Verify
ruff; mypy --strict changed modules; pytest tests/ -q FULLY green; -m exec
green; git diff pre-existing goldens EMPTY (098 new; if any existing golden
changes from kind renames, STOP and report — kinds should not appear in IR).
Baseline 1181 + 86. No git commands; no version bump (044).
