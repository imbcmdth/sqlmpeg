# 015 — stdlib v2  (model: sonnet · wave A · branch v2-streams)

Read plans/000b-interfaces-v2.md (§ stdlib.py) and RFC-001. Depends on plan
013's ir.py (StreamType) — being written concurrently; retry imports if needed.

## Deliverables
1. `sqlmpeg/stdlib.py`: ParamKind video|audio|num|str (frame → video rename
   across all existing entries); FuncSpec.returns; ExpandCtx.node gains
   `outputs: list[StreamType]`; every existing expand passes
   outputs=["video"]. New audio entries per 000b: volume, amix, atempo,
   afade_in, afade_out (2- and 3-arg, mirroring fade_out), reverb → aecho
   (in_gain=0.8, out_gain=0.9, delays=60, decays=<decay>; document why in doc
   line comment). signatures() unchanged in shape.
2. Update `tests/test_stdlib.py`: FakeCtx records outputs; existing assertions
   updated; new tests for each audio function's filter/args mapping; arity
   table extended.

## Verify
ruff + mypy --strict sqlmpeg/stdlib.py; pytest tests/test_stdlib.py green.
EXPECTED RED elsewhere (do not fix). Only edit the two files above. No git
commands.
