# 036 — RFC-004 polish: docs, prompt, README, v0.5.0  (model: sonnet · main ·
final RFC-004 wave)

Read plans/rfc-004-star-subtitles.md END TO END — including the input-seek
amendment AND its caption correction (measured: ffmpeg does not retime
caption packets under input -ss; WHERE + selected captions is a typed
rejection). Docs must tell the measured story, not the original amendment's.

Accumulated staleness to fix (from plans 034/035/inline reviews):
1. prompt.py ~295 + docs/errors.md 205/224/231: "SELECT * is not supported"
   → star and alias.* are real now (probed; INPUT_NOT_FOUND when unreadable).
2. prompt.py Columns section: document subtitle/data pseudo-columns
   (passthrough-only: selectable, never filterable, never in UNION ALL),
   star semantics, and the caption-trim rejection + external-subtitle-join
   guidance. Time-selection section: WHERE on an input alias = input seek
   (-ss/-to; all stream types seeked; trimmed passthrough possible;
   copy-path cuts snap to keyframes), WHERE on a CTE = filter trim
   (video/audio only). Regen docs/system-prompt.md.
3. docs/dynamic-filters.md 149-163: "trim generated internally for WHERE" →
   CTE windows only.
4. NEW docs/trimming.md: the accuracy contract with the measured data —
   decoded = frame-accurate; copied = previous-keyframe snap (up to a GOP
   early); mkv stream-copy inherits source DURATION tags (format=duration
   lies about the trimmed length); caption packets are never retimed under
   -ss (the reason for the rejection), copy AND transcode paths measured
   identical. Link from README.
5. README: short Trims paragraph (input seek story + caption caveat +
   docs/trimming.md link); SELECT * one-liner in the streams section; a
   captions/webvtt-join subsection is ALREADY partially there from wave 2? —
   check; add the extraction one-liner (COPY subtitle[1] TO 'subs.srt') if
   absent. Keep all drift-pinned command blocks untouched unless you
   recompile and re-pin.
6. Version 0.5.0: pyproject version line ONLY (file has fresh user metadata —
   change nothing else in it) + sqlmpeg/__init__.py.
7. docs/errors.md: verify the UNSUPPORTED_SQL section's examples still match
   live output (the caption rejection is UNSUPPORTED_SQL — consider adding
   its real captured JSON as an example; validate --json with an inline
   string per the new CLI convention).
8. Full gate, paste: pytest tests/ -q; pytest -m exec -q; ruff check .;
   mypy sqlmpeg/. CI: verify no changes needed.

## Do NOT
Touch lower/parser/emit/split/ir/registry/sink/stdlib source, goldens, or
LICENSE. Baseline: 947 + 66 exec, all green — keep it that way.
