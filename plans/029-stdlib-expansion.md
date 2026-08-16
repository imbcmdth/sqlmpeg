# 029 — Stdlib expansion: promote common filters  (model: sonnet · main)

User direction: promote high-frequency filters into tier 1 with ergonomic
positional signatures; the niche long tail stays for the dynamic path
(RFC-003). Promotion criteria: common use case + awkward raw interface +
obvious <=5-arg positional signature.

## Deliverables
`sqlmpeg/stdlib.py` — 19 new FUNCTIONS entries (18 -> 37), same FuncSpec
pattern. EXACT signatures (variants; all returns as typed):

VIDEO:
- rotate(f: video, degrees: num) -> rotate, args {"a": f"{degrees}*PI/180"}
  (string expr; ffmpeg evaluates). Doc: degrees clockwise.
- pad(f, w, h) -> pad {"w","h","x":"(ow-iw)/2","y":"(oh-ih)/2"} (centered);
  pad(f, w, h, color: str) adds {"color"}; pad(f, w, h, x, y) explicit;
  pad(f, w, h, x, y, color: str). Four variants, kind-distinct.
- hstack(a: video, b: video) -> hstack {"inputs": 2}; vstack same.
- fps(f, rate: num) -> fps {"fps": rate}.
- sharpen(f, amount: num) -> unsharp {"luma_msize_x": 5, "luma_msize_y": 5,
  "luma_amount": amount}.
- deinterlace(f) -> yadif {} (defaults).
- denoise(f, strength: num) -> hqdn3d {"luma_spatial": strength} (other
  params derive from it per ffmpeg docs — say so in a comment).
- brightness(f, v: num) -> eq {"brightness": v} (doc: -1..1, 0 = unchanged).
- contrast(f, v: num) -> eq {"contrast": v} (doc: 0..2, 1 = unchanged).
- saturate(f, v: num) -> eq {"saturation": v} (doc: 0..3, 1 = unchanged).
- grayscale(f) -> hue {"s": 0}.
- crossfade(a: video, b: video, dur: num, offset: num) -> xfade
  {"transition": "fade", "duration": dur, "offset": offset};
  crossfade(..., transition: str) 5-arg variant overrides transition.
  Doc: offset = seconds into the FIRST input where the fade starts; inputs
  must share resolution/fps (validated by ffmpeg at run time in v1).
- subtitles(f, path: str) -> subtitles {"filename": path} (emit's escaper
  owns the quoting; doc: burned in at run time, file must exist then).
- reverse(f) -> reverse {} (doc: buffers the entire stream in memory).

AUDIO (returns "audio"):
- normalize(a) -> loudnorm {} (EBU R128 defaults);
  normalize(a, lufs: num) -> loudnorm {"I": lufs}.
- highpass(a, freq: num) -> highpass {"f": freq}; lowpass same.
- delay(a, seconds: num) -> adelay {"delays": <int ms = seconds*1000>,
  "all": 1}. Seconds (consistent with t/fades); convert in expand; document.
- acrossfade(a, b, dur: num) -> acrossfade {"d": dur}.
- areverse(a) -> areverse {} (same memory warning).

Rules: one-line docs (drive docs+prompt); table stays pure data; no name may
shadow an existing entry; expand fns follow the existing private-helper
pattern. NOTE hstack/vstack/crossfade/acrossfade are the first tier-1
multi-input additions since overlay/amix — nothing special needed, but their
provenance behaves like amix (agreement rule) automatically; do not touch
lower.py.

## Tests
tests/test_stdlib.py: extend EXPECTED_NAMES + arity table + per-function
expand tests (filter name, args mapping, outputs type) — the degrees/seconds
conversions and pad centering expressions each get an explicit test.

## Regen + collateral
scripts/gen_docs.py + scripts/gen_prompt.py rerun (freshness tests enforce);
docs/stdlib.md + docs/system-prompt.md updated. tests/test_prompt.py has
construct-coverage tests — check they still pass; do NOT add new prompt
worked examples (keep the prompt stable; the function reference grows
automatically). One exec test in tests/test_lower.py: compile+emit a query
using crossfade of two trimmed segments of av2/av3 against real ffmpeg
(compile-only assertion is fine; a full run is a bonus if cheap).

## Verify
ruff, mypy --strict sqlmpeg/stdlib.py, `pytest tests/ -q` FULLY green,
`pytest -m exec -q` green. No git commands. Files: sqlmpeg/stdlib.py,
tests/test_stdlib.py, tests/test_lower.py (one test), docs regen only.
