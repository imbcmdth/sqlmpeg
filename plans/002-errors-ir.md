# 002 — errors.py + ir.py  (model: sonnet · wave 1)

Read `plans/000-interfaces.md` (authoritative signatures) and the "Error contract"
and IR sections of `sqlmpeg-project.md`.

## Deliverables
1. `sqlmpeg/errors.py` — `ErrorCode` enum and `SqlmpegError` exactly per contract.
   - `SqlmpegError.__init__(code, message, *, line=None, col=None, hint=None)`.
   - `to_dict()` returns `{"line", "col", "code", "message", "hint"}` (code as str value).
   - `__str__` renders `"line L:C: CODE: message (hint: ...)"` with graceful omission
     of missing parts.
2. `sqlmpeg/ir.py` — `FrameRef`, `Node`, `Graph` per contract.
   - `Graph.to_dict()`: `{"inputs": [...], "sources": {...}, "nodes": [node dicts in
     insertion order], "output": ...}`. Node dict: `{"id", "filter", "args", "inputs"}`.
   - `Graph.from_dict()` round-trips `to_dict()` exactly.
   - Helper `is_src(ref: FrameRef) -> bool` and `src_alias(ref) -> str` (strip `"src:"`).
3. `tests/test_ir.py` — round-trip test (build small graph → to_dict → from_dict →
   to_dict equal), plus `SqlmpegError.to_dict()` matches the spec's example JSON shape.

## Verify
ruff, `mypy --strict sqlmpeg/errors.py sqlmpeg/ir.py`, pytest tests/test_ir.py — all green.

## Do NOT
Touch `__init__.py`, stdlib, or any pass modules. No git commit.
