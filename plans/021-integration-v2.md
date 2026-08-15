# 021 — v2 integration: goldens, fuzz, exec, full green  (model: sonnet ·
wave D · branch v2-streams)

Read plans/000b-interfaces-v2.md, RFC-001, and the whole committed branch.
Goal: `pytest tests/ -q` fully green + `-m exec` green. Fix ONLY tests and
fixtures; report (don't fix) any compiler bug you find.

## Deliverables
1. Regen golden .ir.json for the v2 IR (regen_golden). Redesign
   910-two-columns: two stream columns now COMPILE — repoint the fixture to a
   still-invalid case (e.g. `SELECT a.frame, 42 FROM ...` → UNSUPPORTED_SQL)
   and rename it 910-nonstream-column. Add new goldens: 080-remap-only
   (video[1]+audio[2] passthrough), 081-filtered-plus-passthrough,
   082-union-av-concat, 083-cte-array-splat (probed? goldens must stay
   symbolic — use explicit subscripts; array splat goldens can't be
   deterministic without media, so cover splats in exec instead).
   EYEBALL every regenerated golden; call out anything semantically wrong.
2. tests/exec: add a multi-stream end-to-end: compile+run remap (2-audio
   fixture → keep audio[2] only; ffprobe asserts 1 audio stream out, correct
   language tag), and a reverb-all broadcast run (2 audio streams out, tags
   preserved). Keep v0 exec tests working (they now need explicit audio
   selection if they asserted audio — check).
3. Fuzz: corpus regenerated (golden .sql set changed); property unchanged;
   confirm 200 examples green with probing on (probe of garbage paths → None
   fast path — verify no big slowdown; if slow, monkeypatch probe in fuzz to
   None and note it).
4. `pytest tests/ -q` green, `pytest -m exec -q` green, ruff green,
   `mypy sqlmpeg/` green.

## Verify
Paste the four gate outputs. No git commands.
