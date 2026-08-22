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
- **Facts through filters.** A filter output carries no readable
  facts today (known_gaps.md), though ffmpeg derives many. The lever
  is the registry: an option typed `channel_layout` that names the
  OUTPUT layout (`surround`'s `chl_out`, `channelmap`/`join`'s
  `channel_layout`, `aformat`'s `channel_layouts`) propagates it when
  given as a literal; likewise `w`/`h` for `scale`/`crop`/`pad`,
  `fps` for `fps`, `pix_fmts` for `format`. Two audio exceptions need
  their own rule: `pan` (layout is the first token of `args`) and
  `channelsplit` (its option is the INPUT layout; each output pin is
  one channel, `FL`/`FR`). An expression argument (`iw/2`) stays
  unknown. Reads off a filter output then work where the fact is
  derivable and reject where it is not.

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
   `frei0r(f.video[1], filter_name => '...')` already compiles through
   sqlmpeg unmodified. One native library (wasmtime behind the frei0r
   ABI) whose parameters name a `.wasm` module makes pluggable WASM
   video filters work on stock ffmpeg, no fork. The ABI's limits
   (video-only, packed RGBA, one input) are fine for the proof.
2. **An out-of-process host** - and as of 2026-08-22 the maintainer
   expects this, not rung 1, to be the primary shape: a sidecar ffmpeg
   pipes into and back out of. The copy costs less than
   frame- or slice-level parallelism wins. `ffmpeg <filters> |
   sqlmpeg-wasm-host | ffmpeg <rest>`, with possibly several stages per
   query.

   What it asks of the compiler, worth knowing before starting:
   - **Partition the filter DAG at wasm nodes.** Each segment is one
     ffmpeg process. The objective is not fewest processes: the pipe
     carries uncompressed frames (~190 MB/s at 1080p60 yuv420p, ~500
     for RGBA), so cut where the stream is NARROWEST - after a
     scale-down, not before.
   - **The pipe needs a container**, not rawvideo, as soon as a cut
     carries more than one stream or any timestamps. `nut` is built for
     this: low overhead, raw-friendly, carries PTS. Losing timestamps
     across a cut is a silent desync.
   - **Linear chains print as a shell pipeline; a DAG of stages does
     not** (it needs fifos or process substitution). The precedent is
     loudnorm2: `run` orchestrates in process with no shell, `compile`
     prints a POSIX-only line. Same split - print a pipeline when the
     cut set is linear, something honest when it is not.
   - **The split pass grows a cut-set cost.** A stream consumed on both
     sides of a cut must either cross the pipe twice or have its
     upstream work duplicated; that is what makes cut placement a real
     optimization.
   - **`describe()` should declare the output format transform**, not
     just the pad signature, so facts survive a cut instead of going
     NULL at every wasm stage.

   A process per stage is also exactly the unit a distributed engine
   schedules.
3. **A native `vf_wasm`/`af_wasm`.** wasmtime linked into libavfilter
   in the engine's own ffmpeg builds, frame planes mapped into WASM
   memory, full pixel-format and timeline support. Same WIT world; the
   modules come along unchanged.

## Analysis functions: user-defined probes

A second kind of extension, orthogonal to the filter ladder above and
mostly buildable on machinery that already exists.

Scene detection into chapter breaks is not a filter - it produces DATA,
not pixels. But the row model's facts already come from a program:
ffprobe answers "what streams are in this file", and nothing says the
answer to "where are the scene cuts" must come from somewhere else. An
analysis function is a user-defined probe:

    COPY (
      SELECT f.video[1], f.audio[1],
             array_agg(ROW('Scene ' || s.index::text,
                           s.start_t, s.end_t)::chapter) AS chapters
      FROM input(:'src') f, analyze.scenes(f.video[1], threshold => 0.4) s
      GROUP BY f.video[1], f.audio[1]
    ) TO :'dest'

Every piece of that shape exists today: a table-returning function is a
row source (096), its rows become a chapter list (094), and running an
analysis pass before the real command is what `sqlmpeg.loudnorm2`
already does - `run` executes it in process, `compile` prints the shell
chain.

And the FIRST BATCH needs no plugin at all, because ffmpeg is already
the analysis engine. A detect filter attaches its results as frame
metadata and `metadata=mode=print` prints them (verified):

    ffmpeg -i film.mkv -vf 'scdet=threshold=5,metadata=mode=print:file=-' -f null -
    frame:1    pts:67      pts_time:0.067
    lavfi.scd.score=1.361

So `analyze.scenes` over `scdet`, `analyze.silence` over
`silencedetect`, ad boundaries over `blackdetect`, and crop arguments
over `cropdetect` are all buildable on the loudnorm2 machinery with no
new extension surface - near-term work, not a moonshot. ffprobe has no
plugin model and is the wrong place to look: it reports what the
demuxer already knows.

For USER-DEFINED analysis, frame metadata is the channel and it names
the plugin hook precisely: frei0r cannot reach it (pixels in, pixels
out), a native wasm filter can set it (rung 3), and an out-of-process
host can skip the ceremony and emit rows directly (rung 2). A second
WIT world beside the filter one - decoded frames and samples in, rows
out - is the surface. Speech to captions, face or shot tracks and
loudness all become the same shape.

Multi-modal TRANSFORMS - the moonshot, e.g. re-timing lips to a
different language track - are harder but not blocked by ffmpeg
itself: libavfilter allows a filter with both video and audio input
pads. They are blocked by the plugin ABIs (frei0r is video-only,
ladspa audio-only), which is why they live at rung 3, and by the
single-pass model, since that work wants lookahead or the whole file.
The escape is the same two-pass split: analyse first, transform with
the analysis in hand.

Both become ordinary once the engine owns the pipeline: a DAG node
that consumes video and audio and emits media plus metadata is just a
node with state.

## Not before 1.0

The engine itself - distribution, persistence, live session management.
Those are the next project's concerns; the compiler stays a compiler.
