# 090 — Fan-out emits one ffmpeg invocation

Maintainer direction (2026-08-19): ffmpeg takes multiple output files
in one invocation, and the multi-COPY script machinery already emits
exactly the right shape — VERIFIED: a two-COPY script over one source
compiles to `ffmpeg -i src -map ... eng.m4a -map ... fra.m4a`, input
deduped, per-output indices restarting at 0. So the fan-out should
stop chaining N commands with `&&` and instead lower one Graph with
one SinkUnit per row/group — the ABR/view IR shape, repurposed.

## Semantics (unchanged relation, better emission)

- A fan-out (`TO (expression)`, grouped or not) over rows that share
  the same inputs compiles to ONE command with one output file per
  row/group: per-output maps, codecs, tags, container tags, path.
- Trim windows (WHERE f.t BETWEEN c.start_t AND c.end_t and friends):
  - Every mapped stream RE-ENCODES → the window rides as OUTPUT
    options per output file (`-ss X -to Y` before that output's
    maps). VERIFIED on ffmpeg 9: frame-accurate, timestamps rebased,
    the input decoded ONCE for all outputs.
  - Any mapped stream is a stream COPY → keep today's `&&` chain with
    input-side seeks. VERIFIED: output-side seeks with `-c copy`
    produce corrupt, unreadable files — this is ffmpeg, not us.
- `run` executes whatever the compile produced (one command, or the
  copy-trim chain) — no behavior change beyond fewer processes.
- two_pass and loudnorm2 fan-out combinations stay rejected as today.

## Mechanics

- The multi-sink path is the target: lower each fan-out row/group as
  its own SinkUnit in ONE Graph (shared node graph, split insertion
  handles shared streams), instead of one _Lowerer per row in
  lower_commands. The per-row pinning machinery is reused per sink
  rather than per command.
- SinkUnit grows an output-trim window (start/end, floats) emitted as
  `-ss`/`-to` in that output's option group, AHEAD of its maps.
  Emission order within an output group: seeks, maps with per-stream
  codecs/tags, container tags, sink options, path (matching the
  verified commands).
- The copy-trim decision is per fan-out: if any output's any stream
  is a copy AND that output has a window → the whole fan-out keeps
  the chain form (mixed forms would reorder work across outputs;
  keep it simple and predictable).
- Input dedup across sinks already exists (ir.dedup_inputs).

## TDD (recipes land red first)

- Recipe 48: repinned to the single command (bytes verified by hand
  against the multi-COPY equivalent).
- Recipe 55: repinned the same way, container titles per output.
- Recipe 47: TWO variants shown with the trade stated plainly —
  stream copy: `&&` chain, input seeks, fastest, cuts snap to
  keyframes; re-encode (WITH video_codec ...): one invocation, output
  seeks, frame-accurate, one decode of the source no matter how many
  chapters.

## Tests (wave)

- Fan-out over unnest rows / grouped / chapters: single command, one
  SinkUnit per row/group; per-output index restart; shared filtered
  streams split correctly (a fan-out whose SELECT filters f.frame
  must insert splits across sinks, the view machinery's job).
- Copy+trim fan-out keeps the chain (47's first form byte-identical).
- Re-encode+trim: output seeks per output, exec test probes the
  pieces' durations and start content.
- Duplicate-destination and ROW_COUNT_MISMATCH behavior unchanged.
- queries/ split-chapters.sql and extract-languages.sql still compile
  (their emitted shape changes; harness is compile-only).

## Waves

1. Orchestrator: plan + recipes 47/48/55 red.
2. Implementation (opus): lowering to multi-sink IR + output-trim
   emission + tests. Full suites green, recipes byte-for-byte.
3. Orchestrator: rows.md/examples prose touch-ups if needed, release
   (likely 0.23.0) per the release procedure.
