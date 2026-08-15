# 003 — stdlib function table  (model: sonnet · wave 1)

Read `plans/000-interfaces.md` and the "Stdlib v0" table in `sqlmpeg-project.md`.

## Deliverables
`sqlmpeg/stdlib.py` implementing `Param`, `ParamKind`, `ExpandCtx` (Protocol),
`FuncSpec`, and `FUNCTIONS` per the contract, covering ALL 11 stdlib entries:

| SQL | expansion |
|---|---|
| `scale(f, factor)` | `scale` args `{"w": "iw*<factor>", "h": "-2"}` |
| `scale(f, w, h)` | `scale` args `{"w": w, "h": h}` |
| `crop(f, x, y, w, h)` | `crop` args `{"w": w, "h": h, "x": x, "y": y}` (order remap!) |
| `overlay(base, top, x, y)` | `overlay` args `{"x": x, "y": y}`, inputs `[base, top]` |
| `hflip(f)` / `vflip(f)` | bare filter, no args |
| `blur(f, sigma)` | `gblur` args `{"sigma": sigma}` |
| `blur_regions(f, x, y, w, h, sigma)` | MACRO: crop the region → gblur it → overlay back at (x, y). 3 nodes; `f` is consumed twice (split pass handles fan-out later — just reference it twice). |
| `draw_box(f, x, y, w, h, color)` | `drawbox` args `{"x","y","w","h","color"}`, plus `"t": "fill"`? NO — outline default; keep `{"x","y","w","h","color"}` only |
| `text(f, s, x, y, size)` | `drawtext` args `{"text": s, "x": x, "y": y, "fontsize": size}` — raw string; escaping is emit's job |
| `speed(f, factor)` | `setpts` args `{"expr": f"PTS/{factor}"}` |
| `fade_in(f, dur)` | `fade` args `{"type": "in", "st": 0, "d": dur}` |
| `fade_out(f, dur)` | `fade` args `{"type": "out", "d": dur}` (note in doc: v0 fades out at stream end requires known duration; emit uses `st` only if provided — keep args as given) |

- Every `FuncSpec.doc` is one crisp line (these become docs + the LLM prompt).
- A module-level `def signatures(name: str) -> str` returning a human-readable
  signature list for error messages, e.g. `"overlay(frame, frame, num, num)"`.
- `tests/test_stdlib.py`: a fake `ExpandCtx` recording nodes; assert node count,
  filter names, arg mapping for `crop` (order remap) and `blur_regions` (3 nodes,
  base referenced twice), and that all 11 names are present with correct arities.

## Verify
ruff, `mypy --strict sqlmpeg/stdlib.py`, pytest tests/test_stdlib.py — green.
Import only from `sqlmpeg.ir` (FrameRef). No git commit.
