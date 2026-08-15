# 014 — probe module  (model: sonnet · wave A · branch v2-streams)

Read plans/000b-interfaces-v2.md (§ probe.py) and RFC-001 (§ Probing policy).

## Deliverables
1. `sqlmpeg/probe.py` per contract: StreamMeta, ProbeResult (+by_type), probe()
   returning None on every failure mode (URL scheme, missing file, no ffprobe,
   nonzero exit, timeout 5s, bad JSON) — NEVER raises. Cache keyed
   (realpath, mtime_ns, size); clear_cache(). ffprobe cmd:
   `ffprobe -v error -print_format json -show_streams <path>` as an argv list
   (guardrail #6). Map codec_type video/audio (others ignored), per-type index
   counted in file order, tags.language/tags.title into metadata,
   width/height/avg_frame_rate (verbatim str)/sample_rate (int).
2. `tests/test_probe.py`: real ffprobe against a generated fixture (reuse
   scripts/gen_fixtures.py output; generate if missing — testsrc2 has no audio,
   so ALSO generate a small a+v fixture here via ffmpeg lavfi
   `testsrc2=...` + `sine=frequency=440:duration=2` into tests/fixtures/av.mp4
   — extend scripts/gen_fixtures.py with it, that file is yours to edit too);
   URL → None; missing file → None; cache hit (monkeypatch subprocess to
   count calls); ffprobe absent (monkeypatch which) → None.

## Verify
ruff + mypy --strict sqlmpeg/probe.py; pytest tests/test_probe.py green
(these run real ffprobe — fine locally; mark the subprocess-using ones
@pytest.mark.exec so default CI stays offline, and keep the monkeypatched ones
unmarked). Depends only on errors.py/nothing — do not import ir. No git commands.
