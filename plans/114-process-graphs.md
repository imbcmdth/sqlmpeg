# 114 — The command becomes a process graph

Maintainer, 2026-08-24. The wasm0r proof-of-concept settled that a
sidecar works and that a module can do more than push pixels — it can
behave like a real UDF, rows included. That makes the compiler's output
shape the next question: with ffmpeg plus pipes plus possibly several
UDFs in flight, one query may need several ffmpeg processes and several
sidecars, built, plumbed, and watched together. This plan is that
machinery. It is deliberately independent of building the sidecar
proper (wasm0r's repo) and of the `LANGUAGE wasm` surface (plan 106):
it is about giving the compiler the ability to produce DAGs that live
both inside ffmpeg (filtergraphs) and outside it (processes and pipes).

The project is cross-platform, and the platforms genuinely differ
here. Most use will be POSIX — Linux, macOS, and WSL on Windows —
with native Windows (PowerShell) the remaining column. Transport is
therefore a per-platform decision behind one interface, and every
finding below is tagged with the platform it was measured on.

## The design in one paragraph

Lowering does not change. It keeps producing one logical graph; a node
ffmpeg cannot host is simply marked external. A new pure pass — same
contract as `insert_splits`: plan in, plan out, no mutation — runs
after it and **partitions** the graph by contracting every maximal
ffmpeg-hostable subgraph into a single ffmpeg process. What falls out
is a process DAG: ffmpeg nodes, sidecar nodes, and typed edges between
them. Everything else in this plan is consequences of that pass.

## What the partition gives for free

- **Adjacent UDFs chain directly.** Two external nodes in series
  contract to sidecar → sidecar with no ffmpeg between them — both
  speak raw frames, so no decode step belongs there. Not a special
  case; the partition cannot produce anything else.
- **A filter between two UDFs gets its own ffmpeg.** UDF → scale → UDF
  partitions into sidecar → ffmpeg → sidecar. The middle process is an
  ordinary contracted subgraph that happens to be small.
- **A stream that never meets a UDF never leaves ffmpeg.** Audio
  passing through while video visits a sidecar: the final ffmpeg reads
  the original file for the audio and the pipe for the video. No edge
  is created that the query did not imply.

## Edges are typed, and the types are already known

Every edge carries a byte format the compiler can spell completely at
compile time, because probe already told it everything:

| edge | format | parameters |
| --- | --- | --- |
| video stream | NUT (`rawvideo` inside) | pix_fmt, size, timebase |
| audio stream | NUT (`pcm_f32le` inside) | rate, channels |
| rows | NDJSON, its own file | the UDF's declared row schema |
| file artifact | whatever the sink says | path |

The `-f nut -c:v rawvideo -pix_fmt X` incantations on BOTH ends of
every pipe are exactly the plumbing humans get wrong by hand. The
compiler emits them from facts it already holds; that is the whole
value proposition, extended one level up.

### Why NUT and not raw bytes

An earlier draft carried frames as bare `rawvideo`/`f32le`: fixed frame
size, byte offsets as frame boundaries, no framing at all. That is
enough for a module that pushes pixels one frame in, one frame out —
and not enough for anything interesting. A UDF that ramps time, doubles
a frame rate, drops frames or generates them consumes N frames and
emits M, each with a timestamp of its own, and raw carries no
timestamps and no frame identity.

NUT is ffmpeg's own low-overhead streaming container, and it earns the
edge on measurements rather than reputation (ffmpeg 9.0.1, this
machine, 30 frames of 320x240 yuv420p):

| property | measured |
| --- | --- |
| round trip through two pipes, ffmpeg as the middle process | 30 frames in, 30 out |
| timestamp manipulation survives (`setpts=2.0*PTS`) | 3.02s became 5.90s |
| frame-COUNT change survives (`fps=25`) | 30 frames became 75 |
| audio survives (`pcm_f32le`) | 44100/1 intact |
| overhead vs raw bytes | **42.5 B/frame, 0.037%** |

Timebase is a rational the muxer takes (`1/1000` by default,
`-video_track_timescale` moves it), so NTSC-style rates are
representable rather than rounded into millisecond drift.

Two consequences of choosing it. **Variable frame rate survives the
wire** — nothing is normalized at a process boundary, and an analysis
module's row timestamps refer to the source's real timeline. And
**there is no second, faster spelling**: 0.037% buys nothing worth a
second code path in the sidecar, and one way to say a thing is the
rule the dialect already runs on.

One trap, recorded so nobody rediscovers it: `-syncpoints none`, the
tempting "low overhead and unseekable" mode for exactly this case,
requires `-f_strict experimental` because NUT v4 is unfinalized. Not
something to build a protocol on. The default configuration's overhead
is already noise.

File edges are not new: the two-pass loudnorm sequence is already a
DAG whose edges are files with a happens-before ordering. One IR
carries both edge kinds — file edges order sequential stages, stream
edges wire concurrent groups — so today's command sequences become the
degenerate case of the new shape rather than a second mechanism.

## Deadlock is designed out, not handled

One producer feeding two consumers through pipes deadlocks the moment
one consumer stalls, and no amount of watching fixes that after the
fact. V1 rule: **an ffmpeg process may carry at most one raw-stream
OUTPUT edge.** A raw edge that would fan out instead duplicates its
producer — a second `-i` and a second decode, priced visibly at
compile time. Predictable cost over runtime buffering, the same trade
trims-are-seeks already makes. Fan-in at an ffmpeg node stays allowed:
ffmpeg schedules its own inputs.

The mirror rule was measured, not assumed (see the findings): **every
producer in a stage starts concurrently.** Feeding a fan-in
sequentially deadlocked exactly as pipe-buffer arithmetic predicts —
the consumer blocks opening its second input while the first producer
blocks on a full buffer the consumer is not draining.

## Execution: groups that live and die together

`execute.py` grows one concept: a **stage** is the set of processes
wired by stream edges, started together — all of them, concurrently,
per the rule above — and watched together. If any member exits
nonzero, the group is torn down and the failure names the member and
carries its stderr — diagnosability is the requirement, since "the
pipeline died" is useless across four processes. Stages connected by
file edges run in sequence, exactly as command lists do today.
Per-stage timeout, same knob as today's per-command one.

## What `compile` prints

A linear chain prints as a shell pipeline —

    ffmpeg ... -f nut pipe:1 | <sidecar> ... | ffmpeg -f nut -i pipe:0 ... out.mkv

— with the same POSIX-only caveat the printed loudnorm2 chain already
carries in `known_gaps.md`. A DAG that needs fan-in cannot be spelled
as a pipeline; `compile` prints the process list with its wiring
described, honestly marked as run-only, and `run` is the supported
path. This mirrors the existing posture: the printed command is a
courtesy, execution is the contract.

## Transport: findings first, decision after

### Findings, 2026-08-24, native Windows (ffmpeg 9.0.1, Python 3.14)

Measured with real ffmpeg on both ends; the middle process where used
was ffmpeg itself (`-f nut -i pipe:0 -vf negate -c:v rawvideo -f nut
pipe:1`), so nothing here depends on wasm existing. NUT keeps that
property: ffmpeg reads and writes it natively, so a stand-in middle
process is still one command.

| candidate | verdict |
| --- | --- |
| stdio anonymous pipes, linear chain | **works**, frame-exact, two- and three-stage |
| extra inherited fds (`pipe:3`) | **dead** — the fd never reaches the native CRT; Python's `pass_fds` is POSIX-only regardless |
| named pipes as input paths (`\\.\pipe\...`) | **works**, including two-way fan-in; created via ~40 lines of ctypes, no dependencies |
| TCP loopback (`tcp://127.0.0.1:PORT?listen`) | **works**, including two-way fan-in, sub-second end to end |

Three behaviors that shape the runner, whatever the transport:

- **ffmpeg opens its inputs sequentially.** The second `?listen` port
  is not listening until the first input has connected. A producer
  therefore cannot know when its consumer is ready; producers must
  either retry (TCP) or block politely on open (named pipes do this by
  nature). This is the strongest argument for pipes over TCP: the
  rendezvous problem disappears instead of being managed.
- **Sequential feeding deadlocks.** Confirmed live, not theorized.
- **Raw demuxers log a benign error at pipe-close EOF** ("Error during
  demuxing: Invalid argument") while exiting 0 with every frame
  delivered. The runner must judge members by exit code, never by
  stderr content.

### Pending: the POSIX column

Linux/macOS/WSL: `mkfifo` + ffmpeg reading the FIFO path is the
expected twin of the named-pipe result, and `pass_fds` reopens the
extra-fd option there. Verify before wave 2 fixes the edge
representation; WSL on the development machine is the nearest POSIX at
hand (its VM refused to boot during the first attempt — retry, else
CI's Linux runner does it).

### The decision this points at

stdio for linear chains everywhere. Named pipes for fan-in — `mkfifo`
on POSIX, `CreateNamedPipe` via ctypes on native Windows — one
interface, two creation calls, no ports, no retries, and ffmpeg sees
an ordinary path on every platform. TCP stays a proven fallback if a
platform's pipes hit a wall. Final confirmation waits on the POSIX
column.

## A frame UDF is a table function

The wire format decides the shape of the contract, and the contract
turns out to be one the language already has. A module receives
**rows** — metadata (timestamps included) plus a frame payload — and
returns rows, N in and M out. That is a table function whose row type
carries a frame, and rows carrying values beside their streams is
exactly what the language gained with value columns.

So `LANGUAGE wasm` is not a new kind of thing. It is a
`RETURNS TABLE(...)` function implemented somewhere else, and time
ramping, frame-rate doubling, frame generation and frame dropping are
all one idea: a function returning a different number of rows than it
consumed. SQL has always allowed that; only the transport was missing.

**Dependency this creates:** the sidecar must demux and mux NUT. Either
a minimal implementation (NUT was designed to be simple, and the
compiler controls both ends, so the subset is small — one stream, no
seeking, a known codec) or an existing crate. That is a scoping item
for the sidecar's own packaging work and for wasm0r's world, not a
language question, but nothing here runs until it exists.

**And it must be introspectable at compile time.** The compiler writes
the pixel format on the ffmpeg side of every edge, so it has to ask a
module what formats it accepts before emitting anything — the same way
the filter registry asks the local ffmpeg what it can do. The sidecar
therefore needs a describe mode, not only a run mode.

## Rows, and the line that keeps the language honest

Frames and rows never share a channel. Frames are the bulk and ride
the NUT edge; a row-returning UDF writes NDJSON to a file edge of its
own, which is where structured analysis output belongs — low volume,
read after the run, never parsed out of a frame stream. Two consumers exist
today conceptually: a table the user sees (`TO STDOUT` queries), and a
second-pass substitution (the loudnorm2 shape: run, parse, fold into
the next command). Both stay. What rows must NEVER do is drive the
SHAPE of the query they run in — shapes are known at compile time,
rows arrive at run time, and the day those mix, every guarantee about
countability dies. A UDF's rows are data out, or measurements folded
into a second pass; they are not a relation the same compile can scan.

## Testable with zero wasm

The chassis needs no sidecar to prove itself. ffmpeg plays the middle
process perfectly well (the findings above were produced exactly this
way), so the partition, the plumbing, the group supervision, and the
fail-together teardown all get exec-tier tests with ffmpeg-only nodes
— including the deliberate-failure case (a middle process that exits
nonzero mid-stream) proving the teardown and the named-member report.
wasm0r plugs into a proven socket later instead of debugging its
module model and this machinery at the same time.

## Waves

1. **Transport empirics.** Native Windows column: done, findings
   above. POSIX column: pending — `mkfifo` fan-in and `pass_fds` on
   WSL or CI Linux, then the transport decision is final.
2. **The IR and the partition pass.** Process-plan types beside
   `Graph`, the contraction pass, external nodes reachable through a
   test-only hook — no language surface. Unit-tested on shape alone.
3. **Execution.** Stages in `execute.py`: spawn concurrently, wire,
   watch, tear down, report by member. The ffmpeg-only exec tests
   above.
4. **Printing.** The pipeline form for chains, the honest run-only
   form for DAGs, `known_gaps.md` updated.

Plan 106 then builds `LANGUAGE wasm` ON this: parsing the two-part
`AS`, validating a module's declared schema against `describe()`, and
marking the call external — the partition and everything downstream of
it already existing.

## House rules

- **Do not commit.** Leave the work uncommitted for review.
- Never write the two banned words in code, comments, docs or reports.
- Never cite a plan or RFC number in code, comments, docstrings or
  error messages.
- Short factual WHAT comments only.
- Run the targeted tests, not the whole suite. Report concisely.
