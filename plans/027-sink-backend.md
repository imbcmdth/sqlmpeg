# 027 — Sink backend: emit + CLI  (model: sonnet · main · wave 2,
parallel with 026 — do NOT touch parser.py/lower.py/compiler.py)

Read plans/rfc-002-sinks.md, committed sqlmpeg/sink.py + ir.Sink (plan 025),
current emit.py + cli.py. Build against ir/sink only — hand-build Graphs with
`sink=Sink(...)` in tests; the frontend (plan 026) is being written in
parallel.

## Deliverables
1. `sqlmpeg/emit.py`:
   - `Emitted` gains `sink: Sink | None = None`; `emit(g)` copies `g.sink`.
   - `build_ffmpeg_args(e, out_path: str | None = None)`: path = out_path if
     given else e.sink.path if set else raise SqlmpegError(INTERNAL? no —
     ValueError is wrong too; use SqlmpegError(UNSUPPORTED_SQL? no...). Use a
     plain `ValueError("no output path")` — this is a programmer/CLI contract,
     not user SQL; the CLI guarantees one is present. Document it.)
   - Option rendering from SINK_OPTIONS table data only (no per-option code):
     for each output index i whose type matches the spec scope, per_stream
     specs render [f"{flag}:{i}", rendered_value]; container specs render
     [flag, rendered_value] once, after the map/metadata block; faststart
     (bool + value_template "+faststart") renders ["-movflags", "+faststart"]
     only when the value is true. Ordering: options in Sink.options insertion
     order, appended after all -map/-metadata/-c-copy args, before out_path.
   - Copy suppression: when a codec option (flag "-c") covers a stream type,
     passthrough outputs of that type drop their `-c:<i> copy` (the explicit
     codec re-encodes them, RFC "Passthrough interplay").
2. `sqlmpeg/cli.py`: `-o` becomes optional where it wasn't and overrides the
   sink path; resolution order per command: compile → -o, else sink path,
   else "out.mp4" placeholder (today's default); run → -o, else sink path,
   else usage error on stderr exit 2. explain unchanged (sink shows in the
   IR JSON). Update --help text minimally.
3. Tests (test_emit.py + test_cli.py): hand-built Graph with sink — codec/crf
   per-video-index rendering; audio_bitrate per-audio-index; format/faststart
   container-level; copy suppression (passthrough audio + audio_codec set →
   no -c:<i> copy, codec rendered instead); no-sink graph unchanged args
   byte-for-byte vs today (regression pin); out_path override beats sink
   path; build_ffmpeg_args with neither → ValueError. CLI: run without -o
   but with... (cannot compile a COPY until plan 026 lands — use a
   monkeypatched compile_sql returning a sinked Graph for CLI tests; note
   this in the test).

## Verify
ruff, mypy --strict sqlmpeg/emit.py sqlmpeg/cli.py, `pytest tests/ -q` FULLY
green, `pytest -m exec -q` green. No git commands.
