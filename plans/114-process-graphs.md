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
| video stream | `rawvideo` | width, height, rate, pix_fmt |
| audio stream | `f32le` | rate, channels |
| rows | NDJSON | the UDF's declared row schema |
| file artifact | whatever the sink says | path |

The `-f rawvideo -s WxH -r N -pix_fmt X` incantations on BOTH ends of
every pipe are exactly the plumbing humans get wrong by hand. The
compiler emits them from facts it already holds; that is the whole
value proposition, extended one level up.

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

## Execution: groups that live and die together

`execute.py` grows one concept: a **stage** is the set of processes
wired by stream edges, started together and watched together. If any
member exits nonzero, the group is torn down and the failure names the
member and carries its stderr — diagnosability is the requirement,
since "the pipeline died" is useless across four processes. Stages
connected by file edges run in sequence, exactly as command lists do
today. Per-stage timeout, same knob as today's per-command one.

## What `compile` prints

A linear chain prints as a shell pipeline —

    ffmpeg ... -f rawvideo pipe:1 | wasm0r-pipe ... | ffmpeg -f rawvideo ... out.mkv

— with the same POSIX-only caveat the printed loudnorm2 chain already
carries in `known_gaps.md`. A DAG that needs fan-in cannot be spelled
as a pipeline; `compile` prints the process list with its wiring
described, honestly marked as run-only, and `run` is the supported
path. This mirrors the existing posture: the printed command is a
courtesy, execution is the contract.

## The one open question: fan-in transport on Windows

stdin gives each process exactly one anonymous pipe. A mux ffmpeg
needing two piped inputs needs a second channel, and the options are
genuinely platform-ugly:

- extra-fd inheritance (`pipe:3`) — fragile on Windows through the C
  runtime; may be fine, may not.
- named pipes — ffmpeg has no native Windows named-pipe protocol
  handler.
- TCP loopback — `tcp://127.0.0.1:PORT?listen` is a real ffmpeg
  protocol on every platform, at the cost of a port rendezvous the
  runner must manage.

**Wave 1 is empirics, nothing else**: prove each candidate on this
machine with plain ffmpeg-to-ffmpeg pipes, measure the fuss, pick one
for fan-in and keep plain stdio for the linear case. No IR work starts
until the transport answer exists, because the edge representation
depends on whether an edge is a pipe or a rendezvous.

## Rows, and the line that keeps the language honest

A row-returning UDF writes NDJSON to a file edge. Two consumers exist
today conceptually: a table the user sees (`TO STDOUT` queries), and a
second-pass substitution (the loudnorm2 shape: run, parse, fold into
the next command). Both stay. What rows must NEVER do is drive the
SHAPE of the query they run in — shapes are known at compile time,
rows arrive at run time, and the day those mix, every guarantee about
countability dies. A UDF's rows are data out, or measurements folded
into a second pass; they are not a relation the same compile can scan.

## Testable with zero wasm

The chassis needs no sidecar to prove itself. ffmpeg plays the middle
process perfectly well:

    ffmpeg -f rawvideo -s ... -i pipe:0 -vf negate -f rawvideo pipe:1

so the partition, the plumbing, the group supervision, and the
fail-together teardown all get exec-tier tests with ffmpeg-only nodes
— including the deliberate-failure case (a middle process that exits
nonzero mid-stream) proving the teardown and the named-member report.
wasm0r plugs into a proven socket later instead of debugging its
module model and this machinery at the same time.

## Waves

1. **Transport empirics.** The three fan-in candidates, on Windows,
   with plain ffmpeg on both ends. A short findings note in this plan,
   then the decision.
2. **The IR and the partition pass.** Process-plan types beside
   `Graph`, the contraction pass, external nodes reachable through a
   test-only hook — no language surface. Unit-tested on shape alone.
3. **Execution.** Stages in `execute.py`: spawn, wire, watch, tear
   down, report. The ffmpeg-only exec tests above.
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
