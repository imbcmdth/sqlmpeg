# 074 — Cover the most common ffmpeg tasks  (model: sonnet · branch
common-tasks · cookbook recipe 34 is the failing test to make pass)

Gap analysis vs the common-task lists (convert/resize/trim/audio/speed/
watermark/compress/rotate/thumbnail/effects): our queries/ misses
resize, compress, mute, volume, rotate, crop, fade, subtitle burn-in,
subtitle extraction, and thumbnails. Thumbnails also need a new sink
option.

## Deliverables
1. sink.py: `frames` option - positive int, video scope, renders
   `-frames:<output-index> N` (measured working: `-frames:0 1`).
   Mirror `crf`'s spec shape and validation; unit tests in
   tests/test_sink.py + an emit test. Recipe 34 in docs/examples.md is
   the end-to-end check (make it pass; report true bytes if the pin
   differs on flag order only - do not edit the file otherwise).
2. New queries/ files, header convention as the existing twenty:
   - resize.sql (source, width, dest) - scale :width, height -2
   - compress.sql (source, crf, dest) - libx264/aac, bare :crf
   - strip-audio.sql (source, dest) - video only, stream copy
   - volume.sql (source, gain, dest) - volume :gain on all audio
   - rotate.sql (source, dir, dest) - transpose dir => :'dir'
   - crop.sql (source, w, h, x, y, dest)
   - fade.sql (source, duration, dest) - fade in at start AND out at
     end is impossible without knowing the file length; do fade-IN only
     and say so in the header
   - burn-subtitles.sql (source, subs, dest) - subtitles filter,
     filename => :'subs'
   - extract-subtitles.sql (source, dest) - subtitle[1] passthrough
   - thumbnail.sql (source, at, dest) - recipe 34's shape
3. queries/README.md: linked rows for each, alphabetical or grouped as
   the table already is.
4. tests/test_queries.py: dummy values for the new variables (numbers
   for width/crf/gain/w/h/x/y/at/duration, 'clock' for dir, path for
   subs); everything compiles hermetically.

## Explicitly skipped (report these so the decision is recorded)
- Text overlay (drawtext): font availability varies by platform/build;
  a program that fails on a stock Windows ffmpeg is worse than none.
- "Last N seconds": needs duration arithmetic at compile time, a queued
  feature; not expressible yet.
- Metadata set/strip: no dialect surface for writing tags.
- Frame-sequence export (frame-%04d.png patterns): verify it compiles
  as a plain sink path in one test; add extract-frames.sql (source,
  rate, dest) ONLY if it does, skip with a note if not.

## Verify
ruff + mypy --strict on sink.py/emit.py if touched; pytest
tests/test_sink.py, test_queries.py, tests/test_examples.py -q AND
-m exec -q (recipe 34 green); full default suite; full -m exec tail.
Report: file list, the frames option spec, skips confirmed, tails.
No git.
