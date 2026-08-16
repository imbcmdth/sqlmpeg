# 033 — RFC-004 foundation: stream types, input_trims, fixtures
(model: sonnet · main · wave 1)

Read plans/rfc-004-star-subtitles.md (design, including the input-seek
amendment). This wave is ADDITIVE — repo stays fully green (816 + 56 exec);
nothing produces subtitle/data refs or input_trims until wave 2, so emit/
split/lower need no changes here (do not touch them).

## Deliverables
1. `sqlmpeg/ir.py`:
   - `StreamType = Literal["video", "audio", "subtitle", "data"]`.
   - src ref grammar gains markers `s` / `d` ("src:<alias>:s:0"): update
     `_parse_type_marker` + the module-docstring grammar (note subtitle/data
     refs are passthrough-only, never filtergraph pads — wave 2/3 enforce).
   - `Graph.input_trims: dict[str, tuple[float, float]] = field(default_factory=dict)`
     (alias -> (start, end) seconds). to_dict emits `"input_trims"` ONLY when
     non-empty, as `{alias: [start, end]}`; from_dict tolerates absence
     (same golden-compat pattern as sink).
2. `sqlmpeg/probe.py`: map codec_type "subtitle" -> "subtitle" and "data" ->
   "data" (currently skipped); attachments still skipped. StreamMeta
   unchanged (the new types leave width/height/fps/sample_rate None; language
   tags on subtitle streams flow into metadata as they do for audio).
3. `sqlmpeg/sink.py`: `subtitle_codec` entry (scope widens: OptionScope gains
   "subtitle"; str, flag "-c", per_stream=True, doc mentions mov_text/webvtt/
   srt). NOTE emit renders per-stream options by OutputMap.type == scope —
   verify that just works for a hand-built subtitle output in ONE emit test
   (allowed exception to "don't touch emit": test only, no source change —
   if emit source turns out to need a change to render it, STOP and report
   instead).
4. Fixtures (scripts/gen_fixtures.py):
   - `tests/fixtures/subs.en.vtt` — write a small valid WEBVTT file directly
     (3 cues over ~2s; plain text write, no ffmpeg needed; idempotent).
   - `tests/fixtures/avs.mkv` — mux av.mp4 + subs.en.vtt into an mkv with
     `-c copy -c:s srt` + `-metadata:s:s:0 language=eng` (idempotent,
     ffmpeg argv-list). Verify via ffprobe in an exec test: 3 streams,
     codec_type subtitle present, language eng.
   - Data-stream fixture: synthesizing tmcd/gpmd is not practical — SKIP;
     data-type behavior is covered by monkeypatched probes in later waves.
     State this in the plan report.
5. Tests: ir round-trips (subtitle/data src refs, input_trims present/absent
   + no-key-when-empty), probe mapping (offline monkeypatched JSON with
   subtitle+data streams; exec against avs.mkv), sink table entry, the one
   emit rendering test, fixture generation exec test.

## Verify
ruff; mypy --strict on changed modules; `pytest tests/ -q` FULLY green;
`pytest -m exec -q` green; `git diff --stat tests/golden` empty. No git
commands. Report contract notes for wave 2 (parser/lower).
