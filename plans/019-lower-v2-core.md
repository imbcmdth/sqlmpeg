# 019 — lower v2 core  (model: opus · wave C · branch v2-streams)

Read plans/000b-interfaces-v2.md (§ lower.py core), RFC-001, and ALL committed
v2 code on this branch: ir.py, probe.py, stdlib.py, parser.py, split.py,
emit.py. Conform; modify only your files.

## Deliverables
Rewrite `sqlmpeg/lower.py` (+ `sqlmpeg/compiler.py`, `tests/test_lower.py`):
- `lower(res, probes)` per contract; compiler builds probes (one probe() per
  distinct path, keyed per alias), `compile_sql(text, *, probe=True)`.
- Typed env: alias → per-type stream access. Subscript 1-based → 0-based src
  ref. `a.frame` ≡ `a.video[1]`. Probed bounds → STREAM_NOT_FOUND (line-
  anchored); unprobed subscripts symbolic.
- Multi-column SELECT → Graph.outputs (Output.name from AS alias; single-
  source passthrough metadata: if the Output.ref is (or derives 1:1 from —
  core: only direct src refs count, broadcasting extends this in 020) a src
  ref of a probed input, copy language/title into Output.metadata).
- Non-stream projection (literal etc.) → UNSUPPORTED_SQL ("every SELECT column
  must be a stream expression").
- WHERE trim: per-alias time range; when a stream of that alias is consumed,
  splice trim+setpts (video) / atrim+asetpts (audio) — once per distinct
  stream, shared across consumers.
- UNION ALL → concat per 000b (interleaved inputs, typed outputs list, branch
  signature equality → CONCAT_MISMATCH with line anchor). Branch column count
  AND types AND order must match. n-branch flattening as in v0.
- CTE columns: record per-CTE list of (name, type, ref) from its SELECT (AS
  names; unnamed single column keeps v0 behavior: referencable as
  `<cte>.frame` when it is a single video column — preserves the README
  query). `cte.<name>` resolves; unknown → UNSUPPORTED_SQL listing known
  names. Array-typed CTE columns land in 020 — here every CTE column is
  scalar; a bare array splat inside a CTE body is UNSUPPORTED_SQL until 020
  (clear message: "coming from broadcasting", fine to reference plan).
- INTERNAL backstops as v0. UDF_ARG_TYPE messages now use video/audio kinds.
- Tests: README v0 query still compiles (frame sugar; note outputs list now
  len 1, no implicit audio); remap-only query; filtered+passthrough; typed
  WHERE trim on both stream kinds; concat v+a; STREAM_NOT_FOUND with a probed
  fixture (use tests/fixtures/av.mp4 via gen_fixtures, mark exec) and
  symbolic acceptance without probe.

## Verify
ruff + mypy --strict sqlmpeg/lower.py sqlmpeg/compiler.py; pytest
tests/test_lower.py tests/test_parser.py tests/test_split.py tests/test_emit.py
tests/test_ir.py tests/test_stdlib.py tests/test_probe.py green.
EXPECTED RED: golden, cli, prompt, fuzz (021/022). No git commands.
