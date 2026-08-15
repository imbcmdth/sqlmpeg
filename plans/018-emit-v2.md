# 018 — emit v2  (model: opus · wave B · branch v2-streams)

Read plans/000b-interfaces-v2.md (§ emit.py, § ir.py), RFC-001, new ir.py
(013), current emit.py.

## Deliverables
Update `sqlmpeg/emit.py` + `tests/test_emit.py` per the 000b contract:
- Src refs render `[<idx>:v:<k>]` / `[<idx>:a:<k>]`.
- Graph.outputs → Emitted.maps (OutputMap): passthrough detection (src ref
  with zero node consumers → bare "-map 0:a:1" target, copy=True, NOT routed
  through filter_complex — no null node); filtered outputs get labels
  out0, out1, ... in output order (v0's [out] and the null-passthrough hack
  are gone).
- filter_complex may be "" (all-passthrough graph) — build_ffmpeg_args omits
  -filter_complex entirely then.
- build_ffmpeg_args: -i list; -filter_complex if nonempty; then per output i:
  -map <target>; -c:<i> copy when copy; -metadata:s:<i> <k>=<v> per metadata
  entry (sorted by key, value escaped? — metadata values go through argv, no
  filtergraph escaping; pass raw). REMOVE the v0 -map 0:a?/-c:a copy tail.
- Consume-once check, topo verify, label collision handling, chain merging,
  _escape_value all carry over — update for multi-output and typed pads
  (out-pad count = len(node.outputs), replacing the split-args special case).
- Rewrite tests: multi-map graphs, pure-passthrough command shape, mixed
  filtered+passthrough, metadata rendering, split/asplit labels, concat
  (outputs ["video","audio"]) label order.

## Verify
ruff + mypy --strict sqlmpeg/emit.py; pytest tests/test_emit.py tests/test_ir.py
tests/test_split.py green. No git commands.
