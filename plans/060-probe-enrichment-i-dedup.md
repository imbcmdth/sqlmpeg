# 060 — Probe enrichment + -i dedup  (model: sonnet · branch v4-track-rows ·
RFC-009 wave 1; cookbook recipes 23-28 are the red TDD targets)

## Deliverables
1. probe.py: StreamMeta grows `codec` (codec_name), `channels`,
   `channel_layout`, `bitrate` (bit_rate as int), `duration` (float),
   `color_transfer` — each None when absent or wrong-typed (opportunistic,
   never raises; audio-only fields None on video rows and vice versa).
   ProbeResult grows container-level `duration` (needs `-show_format`
   added to the ffprobe invocation). Existing behavior byte-identical
   otherwise.
2. -i dedup: aliases with the SAME path, SAME input options and NO
   WHERE window share one `-i` (recipe 27's command pins this: two
   branches x two files = two -i's, not four). Distinct trim windows
   keep distinct -i's — the splice (golden 097/recipe 17 shape) must not
   change. Probe the pipeline to pick the seam (trims are only known
   post-lower, so emit-time or a post-lower normalization — your call,
   document it); regen any goldens whose commands change and EYEBALL.
3. Tests: test_probe unit (canned ffprobe JSON incl. the new fields) +
   exec (av-eng.mp4 language/duration, stereo.mp4 channel_layout=stereo,
   av2 bitrate/codec present); dedup unit tests (dedup fires; options
   mismatch blocks it; trim blocks it) + one exec run of a deduped
   compile. mypy --strict, ruff.

## Verify
Full default suite green; `pytest -m exec` red ONLY on cookbook recipes
23-28 (the TDD set — do not touch docs/examples.md). Report: seam chosen
for dedup, goldens regenerated, remaining red attribution. No git.
