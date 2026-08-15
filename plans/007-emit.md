# 007 — emit pass  (model: opus · wave 2)

Read `plans/000-interfaces.md`, `sqlmpeg/ir.py` (exists), the ref grammar documented
in `plans/006-split.md` (`"id"`, `"id:k"`, `"src:alias"`), and spec pass 4.

## Deliverables
`sqlmpeg/emit.py` per contract: `Emitted`, `emit(g)`, `build_ffmpeg_args(e, out_path)`.

### emit(g)
- Topo-sort nodes (Graph dict is already topo-ordered post-split; verify anyway,
  raise SqlmpegError(INTERNAL) on a cycle).
- Pad labels: `src:<alias>` → `[<idx>:v]`. Node outputs get `[nN]` labels; the final
  output gets `[out]`. Split node output pad k → label per pad.
- Chain merging: maximal linear runs (single input, single consumer, consumer
  directly follows) merge into comma-chains; semicolons between chains. Don't
  over-engineer: correctness first, merging is cosmetic but the README example must
  produce ≥1 comma-chain (crop,scale).
- Arg rendering: `filter=k1=v1:k2=v2`. Bare filters (hflip) render no `=`. Args whose
  key is `"expr"` render value-only (setpts=PTS-STARTPTS). `split` with n renders
  `split=2` (value-only) and needs N output labels on one node. concat renders
  `concat=n=2:v=1:a=0`.
- Escaping: single place. ffmpeg filtergraph escaping for values containing
  `: , ; ' [ ] \` — escape per ffmpeg filtergraph quoting rules (wrap in `'...'`
  with `\'` for quotes). drawtext text= goes through the same escaper. Write a
  `_escape_value(s: str) -> str` with its own unit tests covering `:`  `'`  `,`
  and a plain-safe string passing through unquoted.

### build_ffmpeg_args
`["ffmpeg", "-i", p0, ..., "-filter_complex", fc, "-map", "[out]", "-map", "0:a?",
"-c:a", "copy", out_path]` — audio-copy-from-first-input per spec (the `?` makes it
tolerate silent inputs).

## Tests
`tests/test_emit.py` — hand-built Graphs: single chain merges with commas; diamond
(post-split) produces correct labels and semicolons; split output pads consumed in
order; escaping unit tests; build_ffmpeg_args exact list for a small graph. ~12 tests.

## Verify
ruff, `mypy --strict sqlmpeg/emit.py`, pytest tests/test_emit.py — green. No git commit.
