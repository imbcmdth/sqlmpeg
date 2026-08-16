# 042 — Generated sources in FROM  (model: opus · main · RFC-005 wave 2)

Read plans/rfc-005-everyday-gaps.md §1, plan 040's landed registry API
(Registry.get_source(name) -> SourceFilter(name, output: StreamType, doc);
source_names(); options(name) works for sources; get() stays None for them —
call get_source, not get), and the current parser.py FROM handling
(_add_table/_add_input, the ffmpeg-namespace Dot detection from plan 038's
call path) + lower.py (input bindings, env, zero context for sources yet) +
emit.py (chain rendering — verify zero-input nodes render as a chain head
with no input labels; plan 007-era code should already allow it, confirm).

## Deliverables
1. parser.py: FROM accepts `ffmpeg.<name>(<named options>) alias` — the
   exp.Dot(Identifier(ffmpeg), Anonymous) shape INSIDE a Table node (probe
   empirically how `FROM ffmpeg.testsrc(duration => 2) t` parses — likely
   Table(this=Dot(...)); also `FROM ffmpeg.testsrc() t` and no-parens
   `FROM ffmpeg.testsrc t`, which should reject with a hint). Alias
   mandatory (same rule as input()); positional args -> UNSUPPORTED_SQL
   ("sources have no stream inputs; options are named"); option pairs
   collected raw (positions per the coarse-anchor rule) into a
   Resolved-side source-binding record. Sources do NOT get input indices
   (no -i!) — they are not in input_paths/sources; new Resolved field.
2. lower.py: source aliases bind via registry.get_source: unknown ->
   UNKNOWN_FUNCTION flavored for sources (did-you-mean over
   source_names()); excluded/multi-output names that appear in the raw
   -filters but not get_source -> the fence-flavored UNSUPPORTED_SQL; no
   registry/--portable -> the standard tier-2 unavailability errors.
   Options validated via registry.options(name) (same two FILTER_OPTION
   codes). The alias exposes ONE stream of SourceFilter.output type:
   `.video[1]`/`.frame` or `.audio[1]` by type, `.video`/`.audio` bare
   array = length-1 (static; splat/broadcast fine), star = the one column,
   wrong-type column or out-of-range subscript -> STREAM_NOT_FOUND
   (static message: "ffmpeg.testsrc produces 1 video stream"). The source
   lowers to a Node(filter=<name>, args=<validated options>, inputs=[],
   outputs=[type]) created ONCE per alias on first use (memoized;
   fan-out handled by the split pass as usual).
   WHERE t on a source alias -> UNSUPPORTED_SQL, hint "sources take a
   duration => option instead". Sources legal in CTE bodies and UNION ALL
   branches (the silent-audio headline). Provenance: none (no probe).
3. emit.py: verify (and test) zero-input node chains render correctly
   (`testsrc=duration=2[out0]` / chain-merged `sine=...,volume=...[out1]`);
   fix narrowly if not. Consume-once/topo checks must accept inputs=[].
4. Reserved-name interplay: `ffmpeg` is already rejected as an alias name;
   confirm FROM-position uses can't confuse the namespace (test).
5. Tests: offline via fake registries (extend the fixture-registry pattern
   in test_lower.py — it needs source entries; check how plan 040 shaped
   the offline fixtures in test_registry.py and mirror). parser shapes;
   binding/column rules; option validation; UNION ALL av2 + silent-audio
   branch signature match; CTE-carried source. Exec: the headline —
   `SELECT f.video[1], f.audio[1] FROM input(av.mp4) f UNION ALL
   SELECT t.video[1], s.audio[1] FROM ffmpeg.testsrc2(duration => 1,
   size => '320x240', rate => 15) t, ffmpeg.anullsrc(duration => 1) s`
   — compile + RUN + ffprobe (duration ≈ 3, 1 video + 1 audio out); a
   color-matte pad composite; a sine tone compile+run.
6. Docs: dynamic-filters.md gains a short Sources section (namespace-only,
   the fence, WHERE rejection, silent-audio example); prompt.py Sources
   bullet + regen. README untouched (044 decides placement).

## Verify
ruff; mypy --strict changed modules; pytest tests/ -q FULLY green; -m exec
green; git diff pre-existing goldens empty (no new goldens — sources are
registry-dependent). Baseline 1123 + 83. No git commands; no version bump.
Report parse shapes + contract notes for 043.
