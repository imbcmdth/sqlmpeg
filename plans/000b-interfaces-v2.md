# 000b — Interface contracts v2 (stream-aware, RFC-001)

Authoritative for plans 013–022, branch `v2-streams`. Supersedes 000 where they
conflict; everything not mentioned here is unchanged. Read RFC-001
(`plans/rfc-001-stream-aware.md`) first — it is the design; this is the contract.

MIGRATION NOTE: the suite is allowed to be red mid-migration ON THIS BRANCH.
Each plan verifies its OWN tests plus any explicitly listed subsets. Plan 021
restores full green. Do not "fix" other plans' modules to make unrelated tests
pass.

## Stream types & refs (ir.py — plan 013)

```python
StreamType = Literal["video", "audio"]
FrameRef = str
```

Ref grammar v2 (extends 006's; emit/split consume this):
- `"src:<alias>:v:<k>"` / `"src:<alias>:a:<k>"` — typed source stream, k is the
  ffmpeg per-type index (0-BASED; the SQL surface is 1-based, lowering converts)
- `"<node-id>"` — node output pad 0 · `"<node-id>:<p>"` — output pad p
- v0's untyped `"src:<alias>"` is RETIRED. `is_src`/`src_alias` update; add
  `src_parts(ref) -> tuple[str, StreamType, int]`.

```python
@dataclass
class Node:
    id: str
    filter: str
    args: dict[str, object]
    inputs: list[FrameRef]
    outputs: list[StreamType]      # one entry per output pad; ["video"] typical.
                                   # split/asplit: N same-type entries.
                                   # concat v=1:a=1: ["video", "audio"].

@dataclass
class Output:                      # one top-level SELECT column
    ref: FrameRef
    type: StreamType
    name: str | None               # SELECT ... AS name, else None
    metadata: dict[str, str]       # provenance (e.g. {"language": "fra"}) → -metadata:s:

@dataclass
class Graph:
    input_paths: list[str]
    sources: dict[str, int]
    nodes: dict[str, Node]
    outputs: list[Output]          # REPLACES `output: FrameRef`; order = -map order
```

`to_dict()`/`from_dict()` extend accordingly (`"outputs"` is a list of output
dicts `{ref, type, name, metadata}`; node dicts gain `"outputs"`).

## New error codes (errors.py — plan 013)

`STREAM_NOT_FOUND` (probed; subscript out of range), `INPUT_NOT_FOUND`
(*/splat/broadcast needs a readable input and there is none),
`BROADCAST_MISMATCH` (zip length mismatch). Existing codes keep their meaning;
`SINGLE_OUTPUT_ONLY` is retired from new code paths (multi-column is legal now)
but stays in the enum for the docs' sake.

## probe.py (new — plan 014)

```python
@dataclass(frozen=True)
class StreamMeta:
    type: StreamType
    index: int                     # per-type, 0-based (0:a:<index>)
    metadata: dict[str, str]       # language/title tags when present
    width: int | None; height: int | None
    fps: str | None                # e.g. "30000/1001", verbatim from ffprobe
    sample_rate: int | None

@dataclass(frozen=True)
class ProbeResult:
    streams: list[StreamMeta]      # file order
    def by_type(self, t: StreamType) -> list[StreamMeta]: ...

def probe(path: str) -> ProbeResult | None
```

Returns None (NEVER raises) when: path looks like a URL (scheme://), file does
not exist, ffprobe not on PATH, ffprobe fails/times out (5s), or output is
unparseable. Results cached per (realpath, mtime_ns, size). `clear_cache()` for
tests.

## stdlib.py v2 (plan 015)

```python
ParamKind = Literal["video", "audio", "num", "str"]   # scalar kinds only —
# arrays/broadcasting are a lowering concept, invisible to the table.

@dataclass(frozen=True)
class FuncSpec:
    ...as before...
    returns: StreamType            # NEW

class ExpandCtx(Protocol):
    def node(self, filter: str, args: dict[str, object],
             inputs: list[FrameRef], outputs: list[StreamType]) -> FrameRef: ...
```

`frame` param kind is RENAMED `video` everywhere. `<alias>.frame` sugar for
`video[1]` lives in lower, not here. New audio entries (all `returns="audio"`):
`volume(a, factor)`, `amix(a, b)` → amix=inputs=2, `atempo(a, factor)`,
`afade_in(a, dur)` / `afade_out(a, dur[, at])` (mirror fade contract),
`reverb(a, decay)` → aecho=0.8:0.9:60:<decay> (document the mapping choice),
`atrim` is NOT public (WHERE owns trimming).

## parser.py v2 (plan 016)

`Resolved` unchanged in shape. Validation changes: multiple projections legal;
`a.video[1]` / `a.audio[2]` (exp.Bracket over Column — verify empirically) and
bare `a.video` / `a.audio` legal in projections; subscripts must be positive
integer literals (1-based) — 0 or negative → UNSUPPORTED_SQL with 1-based hint;
`#`-style refs impossible (won't parse — nothing to do). `a.frame` still legal.
Column names other than frame/video/audio/t → UNSUPPORTED_SQL. CTE-column
references (`cte.<as-name>`, incl. subscripted) are validated in lower, not
here — parser only checks the alias half is in scope.

## lower.py v2 (plans 019 core + 020 broadcasting)

```python
def lower(res: Resolved, probes: dict[str, ProbeResult | None]) -> Graph
# probes: keyed by ALIAS (not path). compiler.py builds it (probe once per path).
def compile_sql(text: str, *, probe: bool = True) -> Graph   # compiler.py
```

Core (019): typed env; subscript resolution (1-based SQL → 0-based ref);
`a.frame` ≡ `a.video[1]`; multi-column SELECT → Graph.outputs (AS names kept);
WHERE t BETWEEN instantiates trim+setpts on consumed video streams and
atrim+asetpts on consumed audio streams of that alias (lazily, per consumed
stream, shared per stream); UNION ALL → one concat node, inputs interleaved
per ffmpeg contract (seg1 v..., seg1 a..., seg2 v..., ...), outputs
["video"]*v + ["audio"]*a, branch column types/order must match →
CONCAT_MISMATCH; probed subscript bounds → STREAM_NOT_FOUND; unprobeable is
fine for explicit subscripts (symbolic).

Broadcasting (020): bare arrays in SELECT list (splat) and as stream-typed
args; elementwise expansion at lower time (N scalar subgraphs); zip for multi-
array calls → BROADCAST_MISMATCH on length mismatch; scalar+array broadcasts
scalar; CTE columns carry (type, is_array, length) — `cte.<name>` referencing,
subscriptable when array; requires probe for length → INPUT_NOT_FOUND
otherwise; provenance: each expanded element's Output.metadata copies the
source StreamMeta.metadata (language/title) when derived 1:1 from one source
stream (drop metadata on amix/multi-source results).

## split.py v2 (plan 017)

Same algorithm; filter name `split` or `asplit` chosen by the type of the
split ref (src ref → parse type; node ref → node.outputs[pad]). Split node
outputs = [type]*N.

## emit.py v2 (plan 018)

```python
@dataclass
class OutputMap:
    target: str                    # "[label]" (filtered) or "0:a:1" (passthrough)
    type: StreamType
    copy: bool                     # passthrough → True → -c:<i> copy
    metadata: dict[str, str]

@dataclass
class Emitted:
    inputs: list[str]
    filter_complex: str            # "" when ALL outputs are passthrough
    maps: list[OutputMap]

def emit(g: Graph) -> Emitted
def build_ffmpeg_args(e: Emitted, out_path: str) -> list[str]
```

Passthrough = an Output whose ref is a src ref with zero node consumers → bare
-map target + copy=True, NOT routed through the graph. Source refs render
`[<idx>:v:<k>]`. Output labels: `[out0]`, `[out1]`, ... (label = "out{i}"); a
single-output graph keeps plain `[out]`? NO — always `out0, out1...` for
uniformity (v0's `[out]` is gone; document). build_ffmpeg_args: -i list;
-filter_complex only if nonempty; per output i: `-map <target>`,
`-c:<i> copy` if copy, `-metadata:s:<i> k=v` per metadata item sorted by key.
The v0 implicit `-map 0:a? -c:a copy` is REMOVED.

## CLI / docs / version (plan 022)

`--no-probe` on compile/explain/validate; version → 0.2.0; README breaking-
change section; docs + system prompt regenerate; goldens regen (plan 021).
