# 017 — split v2  (model: sonnet · wave B · branch v2-streams)

Read plans/000b-interfaces-v2.md (§ split.py, § ir.py) and the new ir.py (013,
committed on this branch).

## Deliverables
Update `sqlmpeg/split.py` + `tests/test_split.py`:
- Type of a ref: src ref → src_parts type; node ref → node.outputs[pad].
- Fan-out on video → `split`, on audio → `asplit`; split node
  outputs=[type]*N, args {"n": N} as before.
- Ref grammar v2 in docstring (src refs now contain ':' — sanitize id
  generation accordingly: "src:a:v:0" → "src_a_v_0_split").
- Graph.outputs (list) replaces output: every Output.ref counts as one
  consumer; rewiring order = nodes first (insertion order), then outputs in
  list order.
- Purity + idempotency tests updated; add an audio-fanout → asplit test and a
  mixed graph test.

## Verify
ruff + mypy --strict sqlmpeg/split.py; pytest tests/test_split.py
tests/test_ir.py green. No git commands.
