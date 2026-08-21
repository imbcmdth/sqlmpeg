# Roadmap

## Where this is going

sqlmpeg is the query layer of something larger: a distributed,
persistent video pipeline engine, where live SQL DAGs run in the cloud
the way streaming-SQL databases run continuous queries today, and where
user-defined filters are ordinary functions in the query. Everything
below serves that trajectory: 1.0 is not a feature milestone but a
stability promise - the point where other things can be built on top of
this without the ground moving. The tag waits until the tool has been
used in anger for a while.

## Before 1.0

### Immediate fixes

- `sqlmpeg.__version__` is hardcoded and stale (`0.9.0` against a
  0.24.x package); read it from package metadata.
- One canonical limitations document: docs/filters.md's "Not
  supported" list and docs/known_gaps.md overlap without agreeing
  (runtime filter commands appear only in the former). Crown one.

### Contract stability - what 1.0 actually promises

- **A public Python API.** `sqlmpeg/__init__.py` exports nothing today,
  while the real, typed surface sits in `sqlmpeg.compiler`
  (`compile_sql`, `compile_commands`, `compile_table_sql`, `classify`)
  behind `py.typed`. The engine consumes the library, not the CLI -
  bless the names, document them, and hold them stable.
- **A frozen error contract.** The `ErrorCode` enum and
  `docs/error-schema.json` become append-only; tools built on
  `validate --json` (including LLM repair loops) can rely on them.
- A CHANGELOG.

### Robustness

- The fuzz property (tests/test_fuzz.py, hypothesis over mutated
  queries) currently allows `INTERNAL` as an outcome. Tighten it:
  arbitrary input produces a compile or a *typed, non-internal* error -
  guardrail #7 as an executable promise.
- A cross-version ffmpeg matrix in CI: the registry snapshot mechanism
  already isolates version differences; run the default tier against
  captured 6.x/7.x/9.x registries.
- A Windows `run` audit end to end - paths with spaces are now real
  (chapter-title filenames), quoting deserves its own pass.

### Expressiveness, by demand

In rough order of expected first-user pain:

- Protocol options (`headers`, `user_agent`, `rtsp_transport`,
  `timeout`) - the first authenticated URL needs them.
- Lossless concat (the concat *demuxer*): joining without re-encoding
  is the one common task with no spelling.
- `ladspa` audio plugins via the existing N-input mechanism (its
  dynamic pad count is why the registry excludes it today).
- Subtitle styling (`force_style`) on the burn-in path.
- Attachments and cover art.
- HLS/DASH packaging - as array columns on a single row, the shape the
  one-row rule already reserves for manifests.

### Documentation debt (the cheapest wins)

Plain in-scope features with no worked example: `drawtext` (the most
asked ffmpeg task on the internet), image-sequence input and output,
audio visualization (`showwaves`/`showspectrum`), a streaming *input*
example. And `frei0r` deserves a recipe of its own - see below.

## User-defined filters: the WASM ladder

The extension model for the engine is WASM filters, and the plan is a
ladder whose rungs share one stable artifact: the interface, not the
host. A WIT world (`sqlmpeg:av`) defines the frame and sample records,
`process-frame`, and a `describe()` export naming a module's options
and pad signature - so sqlmpeg introspects user modules exactly the way
it introspects ffmpeg, and a `wasm.<module>(...)` call is validated at
compile time like any other function. A module written for rung 1 runs
unchanged at rung 3.

1. **`wasm0r` - a frei0r host plugin.** ffmpeg's stock `frei0r` filter
   loads plugins at runtime, common builds ship it enabled, and
   `frei0r(f.frame, filter_name => '...')` already compiles through
   sqlmpeg unmodified. One native library (wasmtime behind the frei0r
   ABI) whose parameters name a `.wasm` module makes pluggable WASM
   video filters work on stock ffmpeg, no fork. The ABI's limits
   (video-only, packed RGBA, one input) are fine for the proof.
2. **An out-of-process host.** `ffmpeg | sqlmpeg-wasm-host | ffmpeg`:
   raw frames and PCM over pipes, compiled as a command sequence (the
   machinery two-pass encoding and loudnorm2 already use). Unlocks
   audio, high bit depths, and multi-stream filters - and a
   process-per-stage is exactly the unit a distributed engine
   schedules.
3. **A native `vf_wasm`/`af_wasm`.** wasmtime linked into libavfilter
   in the engine's own ffmpeg builds, frame planes mapped into WASM
   memory, full pixel-format and timeline support. Same WIT world; the
   modules come along unchanged.

## Not before 1.0

The engine itself - distribution, persistence, live session management.
Those are the next project's concerns; the compiler stays a compiler.
