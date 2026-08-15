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
- Bare `a.video` / `a.audio` (whole array) is legal ONLY splatted directly in a
  SELECT list, meaning "all streams of that type, in order". Arrays never appear
  as function arguments (contains the type-system blast radius).
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

## Probing policy

- Explicit subscripts compile symbolically (no ffprobe, compile stays offline,
  file need not exist) — ffmpeg errors at runtime if absent. Matches v0's
  "trusts declared usage".
- `SELECT *` and bare-array splats need the real stream list → require probing:
  automatic in `run`, opt-in via `compile --probe` / `validate --probe`.
- New error codes: `PROBE_REQUIRED` (splat/* without probe), `STREAM_NOT_FOUND`
  (probed and missing). Probing also activates `CONCAT_MISMATCH` (fps/res/
  sample-rate checks) and enables warning on subscripts past the probed count.

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

- Column aliases (`AS eng_audio`) → `-metadata:s:<n> title=`? Nice, deferrable.
- CTEs with multi-column bodies: a CTE becomes a named record of streams;
  `pip.video[1]` vs single-column CTEs keeping bare `pip.frame`. Needs a rule
  for referencing CTE columns (probably: CTE column names come from the SELECT
  aliases, referenced as `cte.name`; subscripts only on inputs).
- Functions returning multiple streams (none in scope; UNION ALL covers concat;
  keep stdlib single-return).
- Subtitle/data streams: passthrough-only in v1 (`s.subtitle[1]` selectable,
  no filters).

## Staging

- **V1a (no probing):** typed columns + subscripts, multi-column SELECT,
  -map/-c copy emission, audio stdlib, typed WHERE-trim, UNION ALL v+a concat,
  asplit. `frame` sugar. All symbolic.
- **V1b (probing):** `SELECT *`, bare-array splats, PROBE_REQUIRED /
  STREAM_NOT_FOUND, real CONCAT_MISMATCH, `run` probes automatically.
- System prompt regenerates from the table as always; examples gain audio tasks.
