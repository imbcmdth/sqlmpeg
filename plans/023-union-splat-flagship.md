# 023 — UNION ALL splat pairing as the flagship  (model: opus · main)

User direction (2026-08-15): the flagship example should highlight UNION ALL
column matching over ARRAY columns — concatenating two multi-language sources
where each branch splats `<alias>.audio`, so every language track pairs
automatically (ffmpeg 0:1↔1:1, 0:2↔1:2). "Shows beautifully how we
automatically spread operators."

Verified working today (against tests/fixtures/av2.mp4 twice):
`SELECT a.frame, a.audio ... UNION ALL SELECT b.frame, b.audio ...` →
`concat=n=2:v=1:a=2` with correct interleave, 3 outputs. Missing piece:
concat outputs carry NO metadata (provenance breaks at concat), so language
tags are lost.

## Deliverables
1. `sqlmpeg/lower.py` — concat provenance agreement: for each concat output
   pad, if EVERY zipped segment element resolves to a non-empty, identical
   provenance dict (post-"und"-filter), that metadata survives onto the
   output (thread the first element's `_Stream.source`); any disagreement or
   any empty → None as today. Applies to scalar and array-flattened columns
   alike. Keep the change small and local to `_concat`/provenance helpers.
2. `scripts/gen_fixtures.py` — `tests/fixtures/av3.mp4`: testsrc2 video + two
   sine tracks (550 Hz, 990 Hz) tagged language=eng / language=fra, ~2s,
   idempotent, same pattern as av2.
3. Tests (`tests/test_lower.py`, `tests/exec/test_exec.py`):
   - lower: union splat over av2+av3 (exec) → 3 outputs, audio outputs carry
     {"language": "eng"} / {"language": "fra"}; disagreement case (swap one
     branch's tracks via subscripts: amix? simpler — union of av2.audio[1]
     with av3.audio[2] scalar columns where languages differ → metadata {}).
   - exec end-to-end: compile+run union splat av2+av3, ffprobe the output:
     1 video + 2 audio streams, tags.language == ["eng", "fra"].
4. `README.md` — the union-splat example becomes the HEADLINE (paths like
   'episode1.mkv'/'episode2.mkv'; shown command = real compilation against
   av2/av3 with paths genericized — including the -metadata:s: flags once
   deliverable 1 lands). The PiP-audio example moves to second billing,
   unchanged. Keep prose tight; the point is "SQL's UNION ALL column-matching
   IS ffmpeg's concat segment contract, arrays included".
5. `tests/test_lower.py` README tests reworked: extract ALL ```sql fences;
   fences referencing episode*.mkv are path-rewritten to the fixtures and
   exec-marked (compile-level assertions: concat args n/v/a, outputs count,
   metadata when probed); symbolic fences (the PiP example) keep the exact
   to_dict assertion as today (update expected shape only if fence order
   changes which test reads it).
6. `sqlmpeg/prompt.py` — Concatenation section gains: array columns in UNION
   ALL branches pair elementwise (same length required); language/title tags
   survive when all segments agree. Add the union-splat worked example as a
   ```sql-probed fence. Regen docs/system-prompt.md (and stdlib.md if doc
   lines change — they should not).
7. Full gate: pytest, pytest -m exec, ruff, mypy sqlmpeg/ — all green.

## Do NOT
Touch emit/split/parser/ir/probe/stdlib. Goldens: only add if a symbolic one
is possible (it is not — splats need probing; skip goldens).
