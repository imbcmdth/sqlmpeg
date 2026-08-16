# RFC-005 — Everyday ffmpeg gaps: sources, enable, expressions, input options

Status: accepted 2026-08-16 (user picked items 1-4 of the gap analysis).
Target: v0.8.0. Waves: 040 registry ∥ 041 input options, then 042 sources,
then 043 enable+expr, then 044 polish.

## 1. Generated sources as table functions

```sql
SELECT f.video[1], s.audio[1]
FROM input('drone.mp4') f, ffmpeg.anullsrc(duration => 30) s
```

- FROM accepts `ffmpeg.<source>(named options) alias` — NAMESPACE-ONLY (one
  rule; `random` etc. collide bare; the namespace marks machine-dependence).
  Alias mandatory, options named-only, validated via the registry options
  path like any tier-2 call.
- Registry: sources are `|` entries with an output type (`|->V`, `|->A`);
  expose via `get_source(name)` -> output StreamType + doc. Sinks (`->|`)
  stay excluded. Multi-output sources stay excluded (fence message).
- Lower: a source alias binds one statically-known stream: `.video[1]` /
  `.frame` (V sources) or `.audio[1]` (A sources), star = that one column,
  out-of-range subscript = STREAM_NOT_FOUND (static, no probe). The source
  is a zero-input filter NODE (never passthrough; there is no -i). Works in
  CTEs and UNION ALL branches — silent-audio-for-concat is the headline.
- WHERE t on a source alias -> UNSUPPORTED_SQL, hint "use duration => ...".
- No ffmpeg/--portable -> same tier-2 unavailability errors. Goldens: not
  possible (registry-dependent); offline fake-registry tests + exec.

## 2. Timeline `enable`

- `enable => '<expr>'` accepted as a named argument (tier-2 calls AND
  tier-1 named extras) exactly when the target filter's -filters line
  carries the T flag. It is FRAMEWORK-level, never listed in -help output,
  so the option validator special-cases the name; value type str.
- Registry: DynamicFilter gains `timeline: bool` parsed from the flags
  column (capture it in 040).
- Non-T filter + enable -> UNKNOWN_FILTER_OPTION with a "no timeline
  support" flavored hint. Docs give the vocabulary (t, n, pos) and note
  expression content is validated by ffmpeg at run time, not at compile.

## 3. Expression strings in stdlib slots

- New ParamKind `"expr"`: accepts a num literal OR a str (passed through as
  an ffmpeg expression). Stdlib slots whose underlying option is
  string-typed switch num -> expr: overlay x/y; pad x/y/w/h; crop x/y/w/h;
  scale w/h (3-arg variant); text x/y (+fontsize IF the live registry says
  drawtext's fontsize is string-typed — follow the machine); draw_box
  x/y/w/h; rotate degrees stays num (we own the *PI/180 mapping; a raw
  expression belongs to ffmpeg.rotate).
- FAITHFULNESS TEST (exec): every expr-kind param maps via named_target to
  an option the live registry types as str. The stdlib can never claim
  expression support ffmpeg lacks.
- Centering, the motivating case: overlay(f.frame, p.frame, '(W-w)/2',
  '(H-h)/2'). Docs mention vocabularies are per-filter and runtime-checked.
- Broadcasting/zip: expr args are scalars (str literals), no change.

## 4. Input options

```sql
SELECT overlay(f.frame, logo.frame, 20, 20)
FROM input('film.mp4') f, input('logo.png', loop => true) logo
```

- `input('path', <named options>)` — table INPUT_OPTIONS as data (mirror of
  SINK_OPTIONS): loop (bool -> `-loop 1`), stream_loop (int ->
  `-stream_loop N`), framerate (num -> `-framerate`), itsoffset (num
  seconds -> `-itsoffset`), hwaccel (str -> `-hwaccel`). Curated, growable;
  no escape hatch (guardrail #3).
- Codes: UNKNOWN_INPUT_OPTION / INPUT_OPTION_TYPE (same shape as the sink
  pair; same coarse-anchor caveat).
- IR: `Graph.input_options: dict[alias, dict[str, object]]`, omit-when-empty
  in to_dict (goldens stable). Emit renders before the owning `-i`,
  alongside/before -ss/-to. Values normalized like sink options.
- Symbolic/portable-safe (pure table validation, no ffmpeg) -> goldens
  possible; loop's PNG-title-card and itsoffset get exec coverage.

## Non-goals (this RFC)

Multiple sinks; lossless concat demuxer; multi-output filters; compile-time
expression validation (one grammar, but per-filter variable vocabularies are
not introspectable — documented as ffmpeg-runtime territory).
