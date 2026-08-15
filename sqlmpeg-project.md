# sqlmpeg — SQL frontend for FFmpeg filtergraphs

> SELECT * FROM video

A standalone CLI that compiles SQL into an ffmpeg `-filter_complex` invocation. Write a `SELECT` statement; get a runnable ffmpeg command. FFmpeg is the executor — this tool never touches pixels.

```sql
WITH pip AS (
  SELECT scale(crop(b.frame, 1200, 50, 600, 200), 0.5) AS frame
  FROM input('game.mp4') b
)
SELECT overlay(a.frame, pip.frame, 20, 20)
FROM input('game.mp4') a, pip
```

```
$ sqlmpeg run query.sql -o out.mp4
ffmpeg -i game.mp4 -i game.mp4 -filter_complex \
  "[1:v]crop=600:200:1200:50,scale=iw*0.5:-2[pip]; \
   [0:v][pip]overlay=20:20[out]" -map "[out]" out.mp4
```

## Why

- FFmpeg's filtergraph syntax is powerful and notoriously obtuse: positional args, pad-labeling bookkeeping, consume-once pad discipline, cryptic errors.
- SQL is ubiquitous, declarative, and — decisively, in 2026 — the language LLMs generate best. A SQL surface plus a strict validator makes "describe the edit in English, get a working ffmpeg command" reliable.
- Prior art gap: many programmatic builders exist (ffmpeg-python, typed-ffmpeg, ffcms's JSON, assorted gems/JS libs) but they are construction APIs — you assemble the graph node by node. Academic declarative video editors (DIVA's stream algebra; V2V/Vidformer at OSU, which use FFmpeg as an execution engine and SQL only for joined *data*) chose custom DSLs. Nobody ships literal SQL → filtergraph. That slot is open.

## Positioning

Standalone OSS project with its own repo, README, and release cadence. No dependency on any service. (Privately: it also de-risks the SQL dialect, stdlib vocabulary, and validation contract for a larger video-pipeline system, and its IR/collapse pass prototypes that system's sink materializer. Nothing in this repo references that system.)

## Language & dependencies

- **Python ≥3.10.** Single package, `pip install sqlmpeg`. Iterating on language design is the whole point; Python is fastest for that.
- **Parser: `sqlglot`** — pure Python, zero deps, transformation-oriented AST, token line/col preserved (feeds line-anchored errors). Parse with `read="postgres"`: every query this tool accepts must remain valid Postgres-dialect SQL (forward-compatibility constraint; do not let the dialect drift toward sqlglot's permissive default).
- **No other runtime deps.** ffmpeg/ffprobe on PATH required only for `run` (not for `compile`).

## SQL dialect (v0 surface)

Supported:
- `FROM input('path-or-url') alias` — each distinct `input()` becomes an ffmpeg `-i` in first-appearance order.
- Nested stdlib calls in SELECT (see stdlib table).
- `WHERE <alias>.t BETWEEN a AND b` → `trim=a:b,setpts=PTS-STARTPTS` (seconds; per-alias).
- `UNION ALL` → `concat` (fps/resolution must match; typed error otherwise).
- `WITH` CTEs → labeled pads; a CTE or alias referenced more than once triggers automatic `split` insertion.
- Exactly ONE output column of type frame in the top-level SELECT.

Rejected with typed errors (never half-supported — every accepted construct is a forever promise):
- `GROUP BY`, aggregates, `HAVING`, subquery predicates, `ORDER BY`, window functions → `NO_STREAMING_EQUIVALENT`
- Multiple output columns → `SINGLE_OUTPUT_ONLY`
- Unknown functions → `UNKNOWN_FUNCTION` (with did-you-mean)
- Arity/type mismatches → `UDF_ARG_TYPE`

Audio (v0): `-c:a copy` from the first input; SQL is video-only. Document loudly.

## Stdlib v0

The function table is DATA, not code — one dict drives lowering, `--help`, docs, and the LLM system prompt:

| SQL | ffmpeg | notes |
|---|---|---|
| `scale(f, factor)` / `scale(f, w, h)` | `scale` | `-2` for auto-even dimension |
| `crop(f, x, y, w, h)` | `crop=w:h:x:y` | arg-order mapping owned here |
| `overlay(base, top, x, y)` | `overlay=x:y` | multi-input |
| `hflip(f)` / `vflip(f)` | `hflip`/`vflip` | |
| `blur(f, sigma)` | `gblur` | |
| `blur_regions(f, x, y, w, h, sigma)` | crop+gblur+overlay expansion | macro: one SQL call → subgraph |
| `draw_box(f, x, y, w, h, color)` | `drawbox` | |
| `text(f, str, x, y, size)` | `drawtext` | escape hell lives here, once |
| `speed(f, factor)` | `setpts` | video-only in v0 |
| `fade_in(f, dur)` / `fade_out(f, dur)` | `fade` | |

`blur_regions` establishes the macro pattern: stdlib functions may expand to multi-node subgraphs, not just single filters. That's the abstraction raw filtergraph lacks.

## Architecture

```
SQL text ──sqlglot──▶ AST ──[4 passes]──▶ IR Graph ──emit──▶ filtergraph string
```

**IR (the load-bearing structure — golden tests assert HERE, not on strings):**
```python
@dataclass
class Node:
    id: str
    filter: str            # ffmpeg filter name, or macro-expanded already
    args: dict             # normalized; SQL arg order already mapped
    inputs: list[str]      # node ids or "src:<alias>"

@dataclass
class Graph:
    sources: dict[str, int]  # alias -> ffmpeg input index
    nodes: dict[str, Node]
    output: str
```

**Passes:**
1. **resolve** — build alias/CTE table; assign input indices; reject unknown references (`UNKNOWN_ALIAS`).
2. **lower** — walk SELECT expression bottom-up; each stdlib call → Node(s) via the function table; WHERE → trim nodes; UNION ALL → concat node; macros expand here.
3. **split** — count consumers per node; fan-out > 1 splices `split=N`. (SQL is a DAG; ffmpeg pads are consume-once. This pass is the headline UX win.)
4. **emit** — topo-sort; assign pad labels; merge single-consumer linear runs into comma-chains; semicolons between chains; render args with correct escaping.

## CLI

```
sqlmpeg compile query.sql            # print full ffmpeg command
sqlmpeg compile query.sql --graph-only
sqlmpeg run query.sql -o out.mp4     # compile + exec ffmpeg
sqlmpeg explain query.sql            # dump IR as JSON (debug/tests)
sqlmpeg validate query.sql           # errors only, exit code; JSON with --json
```

## Error contract (load-bearing; design for machines first)

Every rejection is line-anchored, coded, structured:
```json
{"line": 3, "col": 12, "code": "UDF_ARG_TYPE",
 "message": "overlay() expects (frame, frame, int, int), got (frame, varchar, int, int)",
 "hint": "did you mean to wrap the second argument in input()?"}
```
Codes are an enum, documented in `docs/errors.md`, schema in `docs/error-schema.json`. This is what makes the LLM loop converge: generate → `validate --json` → repair. Treat error quality as a feature with tests, not exhaust.

## Guardrails

1. **This tool never processes media.** It emits strings; ffmpeg executes. No pixel code, no decode paths. (`run` is a thin subprocess wrapper with a timeout.)
2. **Postgres dialect always.** CI includes a test that every fixture query parses under `read="postgres"`.
3. **Reject, never approximate.** Unsupported SQL gets a typed error, not a best-effort graph. Every accepted construct is a compatibility promise.
4. **The function table is the single source of truth** for names, arities, arg mapping, and docs. No lowering logic outside it.
5. **Golden tests on IR, execution tests on perceptual hashes.** Never golden-test emitted command strings byte-for-byte beyond a few smoke cases (formatting churn) and never encoded bytes (encoder churn).
6. **Subprocess hygiene in `run`:** args as a list (no shell), paths validated, timeout enforced, stderr surfaced on failure.
7. **No panics on user input:** any SQL text must produce either a compile result or a structured error — fuzz `compile` with random/mutated SQL in CI.
8. **Fixtures tiny + synthetic:** generate test media with ffmpeg `testsrc`/`smptebars` in a fixture script; nothing copyrighted, nothing large, in the repo.

## Testing

- `tests/golden/`: `NNN-name.sql` + expected `NNN-name.ir.json` (+ expected error JSON for rejection cases).
- `tests/exec/`: a handful of end-to-end runs on synthetic fixtures; assert output dimensions/duration via ffprobe and frame content via perceptual hash (`imagehash`) with threshold.
- Fuzz: `hypothesis` strategy mutating valid queries; property = never an unhandled exception.
- CI: lint (ruff), type-check (mypy, strict on the IR module), tests, fixture generation.

## Milestones

- **T1 — vertical slice:** `scale` + `crop` only, single input, passes 1/2/4 (no split), `compile` + `explain` + golden harness. A day of work; proves the shape.
- **T2 — full dialect:** WHERE/trim, UNION ALL/concat, CTEs, split pass, whole stdlib table, macro expansion (`blur_regions`).
- **T3 — polish for release:** `validate --json`, error docs, `run`, fuzzing, README with the pip/overlay example animated gif.
- **T4 — LLM demo:** `sqlmpeg ai "blur all faces and add a timestamp"` — ships the function table as system prompt, loops on `validate --json`. (Requires an API key env var; feature-flagged; the tool is fully useful without it.)

## Success criteria

- The README example compiles and runs, verbatim, on a fresh machine with only ffmpeg + pip.
- An LLM given `docs/stdlib.md` + 10 example queries one-shots ≥8/10 scripted natural-language tasks (or repairs within 2 validate-loop rounds).
- A filtergraph a competent engineer would take 30 minutes to hand-write (PiP + region blur + fade, multi-reference CTE) is expressible in ≤10 lines of SQL.
- Zero unhandled exceptions across the fuzz corpus.

## Non-goals (v0)

Audio filtergraphs · multiple outputs · GPU filter variants (`scale_npp` etc.) · streaming inputs (files/URLs only) · ffprobe-driven type inference (v0 trusts declared usage; probing inputs to validate dimensions is a v1 idea) · any notion of a server.
