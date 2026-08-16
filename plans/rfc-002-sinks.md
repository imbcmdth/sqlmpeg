# RFC-002 — Sinks: output codecs and parameters in SQL (draft)

Status: draft for discussion · 2026-08-15

## Motivation

The query describes the whole edit except how the result is encoded: container,
codecs, codec parameters. Those currently fall back to CLI `-o` + ffmpeg
defaults. Encoding belongs in the query — it makes the SQL the complete,
validatable job description (and lets `validate --json` reject a bad codec
option at a line/col, which CLI flags cannot).

## Syntax: Postgres COPY, RisingWave semantics

```sql
COPY (
  SELECT ...                        -- any valid sqlmpeg query, CTEs included
) TO 'out.mkv' WITH (
  video_codec 'libx264', crf 20, preset 'slow',
  audio_codec 'aac', audio_bitrate '192k', faststart true
)
```

- `CREATE SINK` (RisingWave/Flink) has the right semantics but is not Postgres
  syntax — guardrail #2 rules it out. `COPY (query) TO 'file' WITH (options)`
  IS Postgres and means the same thing: one-shot sink with delivery options.
- VERIFIED: sqlglot 30.17 `read="postgres"` parses this to `exp.Copy` with
  `this=Subquery(query)`, `files=[Literal(path)]`, and structured
  `params=[CopyParameter(Var(name), value), ...]`. No dialect work needed.
- A bare `SELECT` (no COPY) stays legal: CLI `-o` supplies the path, ffmpeg
  defaults apply — today's behavior, unchanged.
- Precedence: the COPY's path is authoritative; CLI `-o` overrides ONLY the
  path (same encode, new destination). Options never come from the CLI.

## The option table is DATA (guardrail #4)

One `SINK_OPTIONS: dict[str, SinkOptionSpec]` drives lowering, validation,
docs, and the system prompt. Each spec: value type (str/int/bool/enum),
stream scope (video / audio / container), and ffmpeg rendering. v1 set:

| option | type | renders as |
|---|---|---|
| `video_codec` | str | `-c:<i>` per video output |
| `audio_codec` | str | `-c:<i>` per audio output |
| `crf` | int | `-crf:<i>` per video output |
| `preset` | str | `-preset:<i>` per video output |
| `pix_fmt` | str | `-pix_fmt:<i>` per video output |
| `video_bitrate` | str | `-b:<i>` per video output |
| `audio_bitrate` | str | `-b:<i>` per audio output |
| `sample_rate` | int | `-ar:<i>` per audio output |
| `format` | str | `-f` (container; else inferred from extension) |
| `faststart` | bool | `-movflags +faststart` |

- Unknown option → `UNKNOWN_SINK_OPTION` (line-anchored, did-you-mean from the
  table). Wrong value type → `SINK_OPTION_TYPE`. Both codes join docs/errors.md.
- NO `extra_args` escape hatch: arbitrary flag passthrough would break
  "reject, never approximate". The table grows instead.
- Scope rule (v1): `video_*` options apply to every video output stream,
  `audio_*` to every audio output. Per-stream overrides are v2 — the natural
  addressing is the SELECT column's `AS` name, which `Output.name` already
  carries.

## Passthrough interplay

An untouched stream keeps `-c:<i> copy` UNLESS a type-scoped codec option
covers its type — an explicit `audio_codec` means every audio output is
re-encoded, deterministically. (Rationale: the user asked for that codec;
silently keeping copy would make output depend on whether a filter happened
to touch the stream.)

## Plumbing

- parser: accept `exp.Copy` at top level; unwrap to the inner query for
  resolve; validate path is a single string literal; collect (name, value)
  option pairs with positions. Everything else about the query is unchanged.
- IR: `Graph.sink: Sink | None` where `Sink = (path: str, options: dict[str,
  object])` (insertion-ordered). Golden tests pin it. `to_dict` gains "sink".
- lower: validate options against SINK_OPTIONS (types, unknowns); store
  normalized. No graph-shape impact.
- emit: `build_ffmpeg_args(e, out_path=None)` — out_path argument becomes the
  override/default; renders option table entries per output index after the
  -map/-metadata block. `-c:<i> copy` suppressed where a codec option covers
  the stream's type.
- CLI: `compile`/`run` use the sink path when present; `-o` overrides path;
  `run` without a path from either → usage error (today's behavior for bare
  SELECT).
- prompt/docs: regenerate from the table as always; repair guidance for the
  two new codes.

## Future (not v1)

- Multiple sinks: several outputs from one graph (ffmpeg multi-output; tee
  muxer for same-encode fan-out). The COPY form generalizes (one statement
  per sink, shared CTEs) but multi-statement is currently rejected — design
  needed. `CREATE SINK`-style naming only becomes interesting here.
- Per-stream option overrides addressed by SELECT column `AS` name.
- Hardware encoder variants (h264_nvenc etc.) — just table entries when wanted.
