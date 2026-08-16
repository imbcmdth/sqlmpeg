# 038 — ffmpeg.* namespace + video delay  (model: opus · main)

User direction (2026-08-15, amended same day): (a) prefer composable
primitives over bundled macros — the ad-insert use case is `overlay(...)`
composed with a video `delay(...)`, not an `overlay_at`; (b) fix the
Postgres-builtin name collisions once and for all via a NAMESPACE:
`ffmpeg.<filter>(...)` reaches every dynamic filter and can never collide
with SQL grammar (verified: all collision victims parse uniformly as
Dot(Identifier(ffmpeg), Anonymous(name, args)) with args and kwargs intact,
including ffmpeg.overlay(..., eof_action => 'pass') past the PLACING
grammar). The ff_ prefix originally specified below is SUPERSEDED by the
namespace — one spelling only; read the section that follows with
`ffmpeg.<name>` substituted for `ff_<name>`, plus: reserve `ffmpeg` as an
alias/CTE name in the parser.

## A. ff_ aliases (lower resolution)
- In tier-2 resolution: a name starting with `ff_` strips the prefix and
  resolves ONLY in the registry — never the stdlib, never a builtin.
  `ff_scale(...)` is raw ffmpeg scale (options named-only, like any tier-2
  call), regardless of the stdlib's `scale`. `ff_overlay(base, top,
  x => 20, y => 20, eof_action => 'pass')` reaches overlay's full option
  set that the OVERLAY..PLACING grammar hides.
- sqlglot guarantee to VERIFY empirically: no `ff_*` name parses as anything
  but exp.Anonymous under read="postgres" (spot-check ff_trim, ff_format,
  ff_overlay, ff_split, ff_left, ff_extract).
- Unknown `ff_x` → UNKNOWN_FUNCTION; did-you-mean spans registry names in
  BOTH spellings (suggest `ff_trim()` for `ff_trm()`); scope-fence names
  (ff_split, ff_concat, ff_testsrc) → the same UNSUPPORTED_SQL fence errors
  as the bare spelling.
- --portable / no-ffmpeg: same policy as bare tier-2 names (typed rejection
  with the availability hint).
- Enumerate the collision set once, empirically, in a test: for every
  registry name, parse f"{name}(a.frame)" under postgres; names that do NOT
  arrive as exp.Anonymous are the collision set. Assert (exec test) that
  every collided, in-fence name compiles via its ff_ alias. Record the
  discovered set in docs/dynamic-filters.md (replacing the per-name
  "Known limitation" prose with: the collision list + "every filter is also
  reachable as ff_<name>, which never collides").

## B. Video delay (stdlib)
- `delay` gains a video variant: `delay(f: video, seconds: num) -> video`,
  a MACRO expanding format(pix_fmts='yuva420p') -> tpad(start_duration=s,
  stop=-1, color='black@0'). Semantics doc: "a delayed video stream is
  transparent before its start time and after it ends, so it composes with
  overlay directly". Audio variant unchanged (adelay ms).
- Mechanism: FuncSpec currently has ONE expand for all variants and expand
  cannot see which variant matched. Choose the minimal extension —
  recommended: lower passes the matched variant index via a widened
  ExpandCtx or an optional FuncSpec field `expand_by_variant:
  tuple[Callable, ...] | None` (None for all 38 existing entries; when set,
  it replaces `expand` per matched index). Your judgment on the exact shape,
  but: no ripple through existing entries, mypy --strict, table stays data.
- named_target interplay: the video variant is a macro (no single filter) —
  named extras on a video-variant call must be rejected with the macro
  message; audio-variant calls keep targeting adelay. Make named_target
  variant-aware only as far as `delay` needs (document the rule).
- End-to-end exec test — the ad insert:
  overlay(f.frame, delay(scale(a.frame, 0.33), 1), 20, 20) + amix/delay
  audio against av2/av3, compile + RUN, ffprobe sanity (1 video + 1 audio
  out, duration ~= base). Plus IR-level tests for the expansion shape.

## C. Collateral
- docs/dynamic-filters.md: rewrite "Known limitation" per A. README: one
  sentence in "Any ffmpeg filter" introducing ff_ aliases; one sentence in
  Streams/Trims-adjacent prose is NOT needed. prompt.py: mention ff_
  aliases in the dynamic/named-arguments text (+_REPAIR hint tweak for
  UNKNOWN_FUNCTION if it references spellings); regen system-prompt.md;
  docs regen for the new stdlib variant (gen_docs).
- Version 0.6.0 (pyproject version line + __init__).
- Goldens: none needed (ff_ needs registry = exec territory; delay video
  variant is stdlib — add ONE symbolic golden 096-ad-insert using explicit
  subscripts and the video delay).

## Verify
ruff; mypy --strict on changed modules; pytest tests/ -q FULLY green;
pytest -m exec -q green; git diff on pre-existing goldens empty (096 new).
Baseline 949 + 66. Do not touch pyproject beyond the version line. No git
commands. Report: the collision set discovered, the expand-by-variant shape
shipped, exec results, anything left.
