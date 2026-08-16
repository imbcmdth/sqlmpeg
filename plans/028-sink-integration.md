# 028 — Sink integration: goldens, docs, prompt, README  (model: sonnet ·
main · wave 3, after 026+027)

Read plans/rfc-002-sinks.md, the 026/027 reports (orchestrator relays), and
the committed code. Version bumps to 0.3.0 (pyproject + __init__).

## Deliverables
1. Goldens: `090-sink-codecs` — a COPY query (explicit subscripts, symbolic)
   whose .ir.json pins the "sink" dict; regen must leave every other golden
   byte-identical (verify with git diff). Eyeball per usual.
2. End-to-end exec test: COPY the flagship-ish query against av2/av3 TO a tmp
   mkv WITH (video_codec 'libx264', crf 28, audio_codec 'aac',
   audio_bitrate '96k') → run ffmpeg, ffprobe asserts codec_name h264/aac on
   the outputs (proves the flags actually land).
3. docs/errors.md: real captured validate --json for UNKNOWN_SINK_OPTION and
   SINK_OPTION_TYPE (025 left prose placeholders). docs/stdlib.md untouched.
4. `sqlmpeg/prompt.py`: new "Output" section — COPY ... TO ... WITH form, the
   option table rendered FROM SINK_OPTIONS (data-driven like the function
   reference), bare-SELECT default, repair guidance for the two codes. One
   worked example (plain ```sql — COPY with explicit subscripts compiles
   symbolically). Regen docs/system-prompt.md.
5. README: short "Encoding" section after the flagship — the flagship query
   wrapped in COPY ... WITH, real compiled command shown (drift-test pinned
   like the others: extend the content-keyed pattern).
6. CI needs no changes (verify).
7. Full gate: `pytest tests/ -q`, `pytest -m exec -q`, ruff, `mypy sqlmpeg/`
   — all green. Paste outputs.

## Do NOT
Touch parser/lower/emit/cli/ir/sink source — report bugs, don't fix.
