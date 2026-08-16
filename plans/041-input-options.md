# 041 — Input options  (model: sonnet · main · RFC-005 wave 1, parallel
with 040 — do NOT touch registry.py/tests/test_registry.py)

Read plans/rfc-005-everyday-gaps.md §4. This is the sink-options pattern
(plans 025-028) replayed on the input side; mirror those shapes throughout.

## Deliverables
1. `sqlmpeg/inputs.py` (new): `INPUT_OPTIONS: dict[str, InputOptionSpec]`
   as pure data — loop (bool, renders `-loop 1` only when true),
   stream_loop (int, `-stream_loop N`), framerate (num, `-framerate`),
   itsoffset (num seconds, `-itsoffset`), hwaccel (str, `-hwaccel`);
   `validate_option(...)` mirroring sink.py's (typed errors, did-you-mean).
2. errors.py: UNKNOWN_INPUT_OPTION, INPUT_OPTION_TYPE (+ prompt _REPAIR
   entries, docs/errors.md sections with REAL captured JSON, schema enum —
   the full collateral checklist the sink codes needed; test_docs/
   test_prompt enforce).
3. parser.py: `input('path', <Kwargs>)` — path stays the single positional
   string literal; trailing named args collected into Resolved (extend the
   input-binding record with raw option pairs + positions; duplicate name
   -> UNSUPPORTED_SQL; positional after named -> UNSUPPORTED_SQL,
   consistent with call rules).
4. lower.py: validate via inputs.validate_option -> normalized values into
   `Graph.input_options: dict[str, dict[str, object]]` (alias-keyed).
5. ir.py: the field + omit-when-empty to_dict/from_dict (existing goldens
   byte-identical — verify with git diff).
6. split.py: carry input_options through the Graph reconstruction (the
   dropped-field bug pattern has bitten three times — there is a test shape
   for it in test_split.py to extend).
7. emit.py: Emitted carries per-input options resolved to positions (mirror
   input_trims); build_ffmpeg_args renders them before the owning -i,
   BEFORE any -ss/-to (order: options, then seek flags, then -i; verify
   ffmpeg accepts that ordering with a quick real run of -loop 1 + -ss).
8. Tests: table/validation units; parser shapes; lower normalization;
   emit argv (option order, bool-false omitted); golden 097-input-loop
   (symbolic: logo overlay with loop => true pinning input_options in IR);
   exec: PNG title-card (generate a tiny PNG fixture in gen_fixtures via
   ffmpeg testsrc frame, `input(png, loop => true, framerate => 15)` +
   WHERE t <= 2 + overlay onto testsrc — run, duration ≈ 2s); itsoffset
   compile-level argv test.
9. docs: trimming.md untouched; README one clause where input() is first
   shown? (check the Streams section — add one sentence + no fenced-block
   changes); prompt.py input-options paragraph + regen.

## Verify
ruff; mypy --strict changed modules; pytest tests/ -q FULLY green; -m exec
green; git diff pre-existing goldens empty. Baseline 1025 + 76. No git
commands; pyproject untouched (version bump is 044's).
