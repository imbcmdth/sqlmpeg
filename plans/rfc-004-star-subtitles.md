# RFC-004 — SELECT *, subtitle & timed-metadata streams, input-level seeking
(draft)

Status: draft · 2026-08-15 (input-seek amendment accepted same day) · Delivers
RFC-001's V1b plus two new stream types plus the WHERE-trim reimplementation.
Sequencing: lands AFTER the RFC-003 waves (plans 031-032) — both rewrite parts
of lower.py, so they serialize.

## Motivation

Remux workflows need the streams filtergraphs can't touch: captions and timed
metadata (timecode, GoPro gpmd). Users need to (a) keep them through an edit,
(b) extract them, (c) join external subtitle files as tracks (webvtt ->
mov_text in an mp4). And `SELECT *` (RFC-001 V1b) is the ergonomic entry:
"everything, keep it all".

## Stream types

`StreamType` widens: `video | audio | subtitle | data`.
- probe maps codec_type subtitle -> "subtitle", data -> "data" (attachments
  ignored). ffmpeg stream-spec letters: `s`, `d`. Src refs: "src:<alias>:s:0".
- New pseudo-columns `<alias>.subtitle` / `<alias>.data`, same array /
  subscript / splat semantics as video/audio.

## Passthrough-only (the load-bearing constraint)

ffmpeg filtergraphs carry only video/audio. Subtitle/data streams are only
ever `-map`'d. Therefore:
- Any function call with a subtitle/data-typed argument -> UDF_ARG_TYPE
  ("subtitle streams cannot be filtered; they can only be selected").
- ParamKind is UNCHANGED (no function takes them; nothing in stdlib/registry
  changes).
- Split pass: subtitle/data src refs are EXEMPT — never split (there is no
  subtitle split filter). Duplicate consumption is legal: two Outputs may
  reference the same subtitle src ref; emit renders two identical -map specs
  (legal ffmpeg), unlike filtergraph pads.
- Emit consume-once check: exempt subtitle/data src refs for the same reason.
  They never get labels or enter filter_complex.
- WHERE t BETWEEN on an INPUT alias trims subtitle/data streams coherently
  via input-level seeking (see below) — no rejection needed. The rejection
  survives only for WHERE on a CTE whose columns include subtitle/data refs
  (a CTE trim is a filtergraph trim, which cannot touch them).

## Input-level seeking is the primary trim (amendment, accepted 2026-08-15)

`WHERE <alias>.t BETWEEN a AND b` on an INPUT alias no longer splices
trim/setpts filter nodes; it lowers to input options on that alias's `-i`:

    ffmpeg -ss <a> -to <b> -i <path> ...

Why this is sound and better:
- Aliases are globally unique and each owns its own -i slot, so every alias
  has AT MOST ONE time window in the whole query — per-alias seeking maps
  1:1 onto per-input flags. (This was the objection that originally deferred
  the idea; it dissolves under our own input model.)
- Trims video and audio of that input coherently. CAPTION CORRECTION
  (measured 2026-08-15, post-implementation): ffmpeg does NOT retime
  subtitle/data packets under an input -ss — on both the copy and transcode
  paths, cue timestamps stay near-original while video rebases to zero
  (verified on muxed output: subtitle packets at 0.0/0.7/1.4 vs a 0.5s seek),
  so a seeked-and-SELECTED caption track plays out of sync by the seek
  amount, and pre-window cues are retained. Output-side -ss keeps mutual
  sync but rebases nothing and decodes everything. Therefore: WHERE + a
  SELECTED subtitle/data stream of that alias is a typed rejection
  (UNSUPPORTED_SQL, hint: join an external subtitle file timed for the cut);
  trimming an input whose captions are NOT selected remains fine (unmapped
  streams are seeked harmlessly).
- Faster (demuxer seek, no decode-and-discard) and smaller graphs (the
  trim+setpts / atrim+asetpts pairs disappear). Input -ss rebases timestamps
  to zero, which is exactly what setpts=PTS-STARTPTS did.
- NEW capability: trimmed passthrough. `SELECT a.video[1] ... WHERE a.t
  BETWEEN 5 AND 60` can now stream-copy (previously the trim filter forced a
  re-encode).

Accuracy contract (document loudly, exec-test both):
- Streams that are DECODED (anything filtered/re-encoded): frame-accurate —
  modern ffmpeg decodes from the prior keyframe and discards up to the seek
  point.
- Streams that are STREAM-COPIED: the cut snaps to the previous keyframe —
  may start up to a GOP early. This is inherent to copying; the docs say so
  and the fixture-based exec tests use tolerances accordingly. (A future
  `strict` knob could force re-encode for exactness; not in this RFC.)

Filter-level trim SURVIVES for one case: WHERE on a CTE name (a CTE output is
a filtergraph pad, not an -i) — video/audio only, captions rejected, exactly
the v0.3 behavior.

IR: `Graph.input_trims: dict[str, tuple[float, float]]` (alias -> (start,
end)); `to_dict` emits the key only when non-empty (golden-compat pattern,
same as sink). Emit renders `-ss <start> -to <end>` immediately before the
owning alias's `-i`. Golden 030-trim-where and friends regen to the new
shape (trim nodes gone, input_trims present).

## SELECT * (and alias.*)

- `SELECT *`: every stream of every FROM alias — alias order (FROM order,
  CTEs by position of reference? NO: same order as the FROM clause of that
  SELECT), file order within an alias, ALL types (v, a, s, d), each as a
  passthrough column. Requires probing (splat tier): unprobeable input ->
  INPUT_NOT_FOUND, same policy as bare arrays.
- `SELECT a.*, b.audio[1]` — qualified star mixes with other columns.
  sqlglot parses both forms; verify shapes empirically.
- Star over a CTE: expands to the CTE's columns (statically known, no probe
  needed; array columns splat).
- The parser's current `SELECT *` rejection is removed; goldens using it are
  repointed (the 900-series fixture audit is part of the plan work).
- Explicit SELECT lists remain authoritative — no implicit caption carrying
  outside `*` (consistent with the removed implicit audio copy; the SELECT
  list IS the -map list).

## Joining external subtitles / extraction

Falls out of streams-as-columns + sinks; NO new join semantics:
```sql
COPY (
  SELECT f.video[1], f.audio, s.subtitle[1]
  FROM input('film.mp4') f, input('subs.en.vtt') s
) TO 'out.mp4' WITH (subtitle_codec 'mov_text')

COPY (SELECT f.subtitle[1] FROM input('film.mkv') f) TO 'subs.en.srt'
```
- New sink option: `subtitle_codec` (scope subtitle, flag "-c", per_stream,
  e.g. 'mov_text', 'webvtt', 'srt'). Default remains `-c:<i> copy` for
  passthrough subtitle outputs (the existing rule).
- Subtitle provenance: language/title tags thread through the existing
  passthrough metadata machinery unchanged — multi-language caption sets
  keep their tags.

## Plumbing summary

ir (StreamType, ref markers s/d) -> probe (codec_type mapping) -> parser
(star acceptance, subtitle/data column names) -> lower (star expansion,
passthrough columns, trim rejection, function-arg rejection) -> split/emit
(exemptions, s/d specs) -> sink table (+subtitle_codec) -> docs/prompt/
goldens/exec tests (a real .vtt fixture + mov_text remux verified by
ffprobe; an extraction round-trip). Error surface: reuses existing codes
(INPUT_NOT_FOUND, UDF_ARG_TYPE, UNSUPPORTED_SQL, STREAM_NOT_FOUND).

## Non-goals

Subtitle filtering/styling (burn-in already exists as `subtitles()` — a VIDEO
filter); OCR; attachment streams; retiming/offsetting subtitle tracks;
implicit caption carrying in explicit SELECTs.
