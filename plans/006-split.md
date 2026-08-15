# 006 — split pass  (model: sonnet · wave 2)

Read `plans/000-interfaces.md` and `sqlmpeg/ir.py` (exists). Spec: pass 3 in
`sqlmpeg-project.md` — SQL is a DAG, ffmpeg pads are consume-once.

## Deliverables
`sqlmpeg/split.py` — `insert_splits(g: Graph) -> Graph` (returns a NEW Graph; do not
mutate the input):
- Count consumers of every FrameRef (node ids AND `src:` refs; `g.output` counts as
  one consumer).
- Any ref consumed N>1 times: insert a Node `filter="split"`, `args={"n": N}`,
  `inputs=[ref]`, id `<sanitized-ref>_split`. Rewire each consumer (in deterministic
  node-insertion order) to `"<split-id>:<k>"` for k = 0..N-1 — i.e. a FrameRef may be
  `"<node-id>:<outpad>"`. Plain `"<node-id>"` means output pad 0. Document this ref
  grammar in the module docstring (emit relies on it).
- `src:` refs with fan-out get split too (a source used twice must be split, same rule).
- New split nodes are inserted before their consumers in the node ordering so the
  dict stays topologically ordered.
- Idempotent: running twice changes nothing the second time.

## Tests
`tests/test_split.py` — no-fanout graph unchanged (deep-equal); node fan-out 2;
src fan-out 3; output-edge counted; idempotency. Build graphs by hand with ir.Node.

## Verify
ruff, `mypy --strict sqlmpeg/split.py`, pytest tests/test_split.py — green. No git commit.
