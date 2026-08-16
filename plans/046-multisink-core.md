# 046 — Multi-sink core  (model: opus · main · RFC-006 wave 2)

Read plans/rfc-006-views-multisink.md and plan 045's contract notes
(embedded in the dispatch). Delete the marked temporary rejection; the ABR
ladder compiles and RUNS at the end of this wave.

## Deliverables
1. ir.py: `Graph.outputs` + `Graph.sink` -> `Graph.sinks: list[SinkUnit]`;
   `SinkUnit = {outputs: list[Output], path: str | None, options:
   dict[str, object]}` (path None only for the bare-SELECT case; options {}
   then too). to_dict: `"sinks": [{outputs, path, options}...]` (omit path
   null? no — emit "path": null explicitly; outputs shape unchanged).
   from_dict migrates. ALL goldens regen (mechanical; eyeball a spread).
2. lower.py: run() loops res.sinks (bare SELECT = one unit, path None);
   retire Resolved.select/branches reads per note 1 (parser keeps the
   fields for now — parser cleanup is NOT this wave). Delete the temporary
   rejection + its tests. Cross-sink semantics: consumers of one view from
   several sinks share nodes (the memoized CTE refs are graph-global
   already — verify with a test that the ladder's master view lowers ONCE).
3. VIEW ALIASING RELAXED (decision, RFC example is authoritative):
   `FROM master m` binds m as a branch-local name for the view/CTE ref;
   parser's "aliasing CTE" rejection is removed; the alias is branch-scoped
   (like nothing else escapes a branch) but must not collide with the flat
   namespace (reuse _reserve? NO — branch-local: two branches may both use
   m; it may not shadow a global name -> UNSUPPORTED_SQL). Update the
   pinned test test_a_view_may_not_be_aliased_in_from accordingly.
4. split.py: outputs-union consumer counting across sinks; carry `sinks`
   through reconstruction (the four-time dropped-field pattern).
5. emit.py: Emitted.groups (one per sink: maps/copy/metadata computed
   per-FILE — output stream indices restart per group); labels stay
   graph-global (out0..outN across the whole command? ffmpeg labels are
   graph-scoped: keep global uniqueness, groups reference their labels);
   build_ffmpeg_args renders: inputs once, filter_complex once, then per
   group [maps, -c per-file-index, -metadata:s per-file-index, sink
   options, path]. out_path override applies ONLY when one group (else
   ValueError contract updated). Passthrough/copy suppression per group.
6. cli.py: -o with >1 sink -> stderr error exit 2 naming sink paths; run
   executes the one command (all paths from sinks); compile prints it.
7. Tests: the LADDER end-to-end exec (view over av2, three COPYs: 2 scaled
   video+audio renditions + audio-only — run real ffmpeg once, ffprobe all
   three outputs); cross-sink share test (master lowered once, split fans);
   per-group index correctness (copy suppression + metadata in group 2);
   goldens regen + new 100-abr-ladder symbolic golden (multi-sink, explicit
   subscripts, no probe needed); CLI -o rules; bare SELECT unchanged
   behavior (regression pins).
8. Docs: none (048).

## Verify
ruff; mypy --strict changed modules; pytest tests/ -q FULLY green; -m exec
green; git diff tests/golden shows the MIGRATION ONLY (every .ir.json gains
the sinks shape; eyeball 3-4 + the two new). Baseline 1259 + 97. No git
commands; no version bump.
