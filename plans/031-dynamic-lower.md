# 031 — Dynamic filters + named args in lower  (model: opus · main ·
RFC-003 wave 2)

Read plans/rfc-003-dynamic-filters.md (INCLUDING the "Tier-1 named extras"
amendment), committed sqlmpeg/registry.py (module docstring documents the
introspection quirks) and sqlmpeg/stdlib.py (named_target on every FuncSpec),
current sqlmpeg/lower.py, parser.py, errors.py, prompt.py.

## Deliverables
1. `sqlmpeg/errors.py`: add `UNKNOWN_FILTER_OPTION`, `FILTER_OPTION_TYPE`.
2. `sqlmpeg/prompt.py`: _REPAIR entries for both (collection blocker
   otherwise) + a short "named arguments" paragraph in the Arguments section
   + a "beyond the stdlib" paragraph (any installed ffmpeg filter, named
   options, machine-dependent). Regen docs/system-prompt.md. Docs stubs in
   docs/errors.md + schema enum for the two codes (test_docs enforces;
   real captured JSON is plan 032's).
3. `sqlmpeg/parser.py`: allow exp.Kwarg args in calls structurally (currently
   they'd hit generic validation — check; Kwargs must be TRAILING: a
   positional after a named -> UNSUPPORTED_SQL; duplicate named ->
   UNSUPPORTED_SQL). Column whitelist untouched.
4. `sqlmpeg/lower.py` — the core:
   - `lower(res, probes, *, registry=None, portable=False)`;
     compiler: `compile_sql(text, *, probe=True, portable=False)` loads
     `registry.load()` unless portable (then None).
   - Call resolution order: FUNCTIONS first; else registry.get(name):
     - found -> tier-2 call: positional args must be EXACTLY the stream
       inputs (count+types from DynamicFilter.inputs; num/str positional ->
       UDF_ARG_TYPE telling the user options are named for dynamic filters);
       named args validated via registry.options(name): unknown ->
       UNKNOWN_FILTER_OPTION (did-you-mean over that filter's options);
       type/range/constants/unusable -> FILTER_OPTION_TYPE (message includes
       range or constants); node args = named options in written order;
       returns DynamicFilter.output type. Broadcasting/zip must work
       unchanged (type-driven — add tests).
     - registry None (no ffmpeg or portable): name known to NEITHER ->
       UNKNOWN_FUNCTION as today; if portable=False and ffmpeg absent,
       hint mentions dynamic filters need ffmpeg on PATH. If portable=True
       and the name IS a plausible filter, still UNKNOWN_FUNCTION with hint
       "dynamic filters are disabled by --portable".
   - Tier-1 named extras: trailing Kwargs on a stdlib call; spec.named_target
     None -> UDF_ARG_TYPE ("blur_regions is a macro; named options are not
     supported — use crop/gblur/overlay directly"); else validate each via
     registry.options(named_target) (same two codes); registry unavailable ->
     typed error ("named arguments are validated against your installed
     ffmpeg; ffmpeg was not found" — UDF_ARG_TYPE? no: use
     UNKNOWN_FILTER_OPTION? Neither is right; use UNSUPPORTED_SQL with that
     message and a hint naming --portable semantics. Your judgment, but typed
     and line-anchored, and portable=True gives the same rejection).
     Conflict with an option the positional mapping already set (compare
     against the arg KEYS the expand produced) -> UDF_ARG_TYPE ("'w' is
     already set by the positional signature"). Mechanism: run expand, then
     merge extras into the single produced node (named_target invariant:
     one node whose filter == named_target — assert, INTERNAL if violated);
     extras append after positional args in written order.
   - Provenance: tier-2 single-stream-input calls thread source; multi ->
     _agreed_source. (Reuse the existing positions machinery — it keys off
     the variant's stream positions; build the equivalent for tier-2 from
     DynamicFilter.inputs.)
5. Tests (test_lower.py + test_parser.py): tier-2 happy path (exec: gblur
   sigma named; unsharp two options; xfade as dynamic with transition enum
   validated — constants enforcement; range violation FILTER_OPTION_TYPE);
   tier-1 extras (blur planes; conflict rejection; macro rejection;
   crossfade transition via named on top of 4-positional); trailing-Kwarg
   ordering errors; portable/no-ffmpeg paths (monkeypatch registry.load ->
   None registry... design load() so tests can inject: lower takes registry
   param — pass a fake Registry built from the offline fixtures in
   test_registry.py patterns); broadcasting over a tier-2 audio filter
   (exec); UNKNOWN_FUNCTION did-you-mean now spans both tiers (exec:
   "gblu" suggests gblur).
6. Emit/split/ir: NO changes expected (tier-2 nodes are ordinary Nodes).
   If you find otherwise, report.

## Verify
ruff; mypy --strict sqlmpeg/lower.py sqlmpeg/parser.py sqlmpeg/compiler.py;
`pytest tests/ -q` FULLY green; `pytest -m exec -q` green. No git commands.
Report: resolution flow shipped, judgment calls, quirks for plan 032.
