# RFC-001 — Stream-aware sqlmpeg (v1 design sketch)

Status: draft for discussion · 2026-08-15
Supersedes: v0's single-implicit-video-stream model and the `-c:a copy` audio rule.

## Motivation

ffmpeg's `-map` syntax and multi-stream filtergraphs are the hardest part of its
CLI. v0 sidesteps them (one video stream, audio copied blindly). This RFC makes
streams first-class so SQL covers stream selection, audio filtering, and output
mapping — the full filtergraph interface.

## Core rule

**The top-level SELECT list is the output stream list.** One expression = one
output stream; column order = output stream order = `-map` order.
`SINGLE_OUTPUT_ONLY` is replaced by "every column must be stream-typed."

## Surface

- An input exposes two array-typed pseudo-columns: `<alias>.video` and
  `<alias>.audio` (later: `subtitle`). `a.video[1]` is the first video stream —
  1-based per Postgres array semantics; docs map to ffmpeg's 0-based `0:v:0`.
- `a.frame` remains as sugar for `a.video[1]` (v0 compat; every v0 query still
  compiles).
- Bare `a.video` / `a.audio` is the whole array. Legal splatted in a SELECT
  list ("all streams of that type, in order") AND as a function argument, where
  it broadcasts (see Broadcasting below).
- `SELECT *` = all streams of the input(s) in file order (requires probing, see
  below).
- Rejected: `#0_0`-style stream refs and `SELECT * EXCEPT (...)` — neither
  parses under `read="postgres"` (guardrail #2). Positive selection only, which
  also matches -map mental model.

## Types

`ParamKind` grows: `video | audio | num | str`. Existing stdlib re-types
`frame → video`. New audio stdlib (same FuncSpec table): `volume(a, factor)`,
`amix(a, b)`, `atempo(a, factor)`, `afade_in/afade_out`, `acopy`-free passthrough
by construction. `UDF_ARG_TYPE` messages already carry the shape
(`amix() expects (audio, audio), got (audio, video)`).

## Broadcasting (added 2026-08-15)

Passing an array where a function expects a scalar stream maps the call over
every element — a spread that happens AT LOWER TIME, so the IR, split pass, and
emit never see arrays; each element gets its own scalar subgraph. The feature
is entirely frontend: type checker + lowering walk.

- Elementwise: `reverb(a.audio, 0.3)` : `audio[]` — one aecho subgraph per
  source audio stream. `num`/`str` args apply unchanged to every element.
  Nesting composes: `volume(reverb(a.audio, 0.3), 0.5)` : `audio[]`.
- Multiple arrays zip elementwise; length mismatch is a typed, line-anchored
  `BROADCAST_MISMATCH` ("a.audio has 3 streams, b.audio has 2"). No cross
  products. Scalar + array mixes broadcast the scalar.
- Requires a probeable input (need N to expand): joins `SELECT *` in the
  needs-readable-input tier of the probing matrix; on the symbolic fallback
  path it fails with the same natural `INPUT_NOT_FOUND`.
- CTE columns carry types, arrays included: `WITH dubbed AS (SELECT
  reverb(a.audio, 0.3) AS aud ...)` gives `dubbed.aud : audio[]` — splat it,
  broadcast over it again, or subscript it (`dubbed.aud[2]`). Subscripting is
  one rule: positional selection on ANY array-typed column, physical or
  computed. (This resolves the multi-column-CTE open question: CTE columns are
  named by their AS aliases; subscripts work wherever the type is an array.)
- Provenance metadata: broadcast-expanded outputs know their source stream, so
  emit adds `-metadata:s:<out> language=...` (etc.) automatically from the
  probe — filtered streams keep their language tags, which raw ffmpeg drops.
  Headline use case: `reverb(a.audio, 0.3)` processes every language track and
  each output stays tagged.
- UNION ALL branches with array columns must agree on element count (probed);
  mismatch is `CONCAT_MISMATCH`.

## Semantics that fall out

1. **Passthrough = stream copy.** A SELECT column that is a bare subscript
   (never consumed by any function) does not enter the filtergraph: it emits
   `-map 0:a:1` with per-stream `-c:<n> copy`. Untouched streams are never
   re-encoded. IR: `Graph.outputs: list[Output]` where
   `Output = (name, ref, StreamType)`, and a ref that is `src-sub:<alias>:a:1`
   with zero node consumers renders as a plain -map.
2. **WHERE t BETWEEN = synchronized trim.** The per-alias trim applies to every
   stream drawn from that alias in that branch: `trim+setpts` on video pads,
   `atrim+asetpts` on audio pads. One predicate keeps A/V in sync.
3. **UNION ALL = concat n=N:v=V:a=A.** SQL already requires branches to agree on
   column count/order/types; that IS ffmpeg's concat segment contract. Branch
   columns interleave exactly as concat expects. CONCAT_MISMATCH (typed error)
   finally becomes reachable once probing lands.
4. **Split pass:** unchanged algorithm; picks `split`/`asplit` by pad type.
5. **Emit:** input refs become `[<idx>:v:<k>]` / `[<idx>:a:<k>]`; one `-map` per
   SELECT column ([label] for filtered, bare spec for passthrough). The v0
   implicit `-map 0:a? -c:a copy` is REMOVED — the SELECT list is authoritative.
   (Breaking vs v0 behavior; acceptable pre-release, loud README note. `frame`
   sugar does NOT re-add implicit audio.)

## Probing policy (revised 2026-08-15: probe by default, degrade gracefully)

Probing is not a mode and there is no PROBE_REQUIRED error. Every compile
probes opportunistically and falls back to symbolic lowering when it cannot:

- **Probe when possible:** local file exists AND ffprobe is on PATH. Results
  cached per (path, mtime, size). Probing only ADDS validation — subscript
  bounds (`STREAM_NOT_FOUND`, line-anchored), real `CONCAT_MISMATCH`
  (fps/resolution/sample-rate) — and RESOLVES `SELECT *` / bare-array splats.
  It never changes the lowering of explicit subscripts (`a.audio[2]` is
  `0:a:1` either way), so golden IR stays deterministic.
- **Fall back silently when not:** file missing, input is a URL (never fetch
  the network inside compile), or no ffprobe. Explicit-subscript queries
  compile to the same command; ffmpeg validates at runtime. This keeps
  compile-offline, the golden suite (nonexistent fixture paths), and the fuzz
  corpus (garbage paths, thousands of compiles) all working.
- `SELECT *` / splats on an unprobeable input fail with plain
  `INPUT_NOT_FOUND` — "cannot enumerate streams of a file I cannot read" is a
  natural error, not a policy error.
- `run` probes by construction (files must exist to execute), so the LLM
  validate-loop sees `STREAM_NOT_FOUND` at line/col instead of an ffmpeg
  runtime failure.
- Guardrail #1 update: compile never REQUIRES ffprobe, but uses it when
  available. `--no-probe` escape hatch for byte-reproducible offline compiles.

## Examples

```sql
-- Remap only: first video + second audio, nothing re-encoded
SELECT a.video[1], a.audio[2] FROM input('foo.mp4') a
-- ffmpeg -i foo.mp4 -map 0:v:0 -map 0:a:1 -c copy out.mp4

-- Filter video, pass audio through untouched (audio stays -c copy)
SELECT scale(a.video[1], 0.5), a.audio[1] FROM input('foo.mp4') a

-- Synchronized A/V trim
SELECT a.video[1], a.audio[1] FROM input('foo.mp4') a
WHERE a.t BETWEEN 5 AND 10

-- Concat with audio
SELECT a.video[1], a.audio[1] FROM input('intro.mp4') a
UNION ALL
SELECT b.video[1], b.audio[1] FROM input('main.mp4') b

-- Background music ducked under the talk track
SELECT v.video[1], amix(v.audio[1], volume(m.audio[1], 0.2))
FROM input('talk.mp4') v, input('music.mp3') m
```

## Open questions

- Column aliases (`AS eng_audio`) → `-metadata:s:<n> title=`? Nice, deferrable
  (broadcast provenance metadata covers the important case, language tags).
- Functions returning multiple streams (none in scope; UNION ALL covers concat;
  keep stdlib single-return).
- Subtitle/data streams: passthrough-only in v1 (`s.subtitle[1]` selectable,
  no filters).
- Array-length arithmetic beyond zip (e.g. filtering an array down by language
  predicate: `a.audio WHERE lang IN ('eng','fra')`)? v2 at the earliest.

(Resolved: multi-column CTE referencing — see Broadcasting; CTE columns are
AS-named and typed, subscripting works on any array-typed column.)

## Staging

- **V1a (no probing):** typed columns + subscripts, multi-column SELECT,
  -map/-c copy emission, audio stdlib, typed WHERE-trim, UNION ALL v+a concat,
  asplit. `frame` sugar. All symbolic.
- **V1b (probing):** `SELECT *`, bare-array splats, PROBE_REQUIRED /
  STREAM_NOT_FOUND, real CONCAT_MISMATCH, `run` probes automatically.
- System prompt regenerates from the table as always; examples gain audio tasks.
