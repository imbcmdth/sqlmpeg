# 050 — Reference registry snapshot  (model: sonnet · main · RFC-007 wave 1)

Read plans/rfc-007-uniform-calls.md § "Offline compile". Registry-side only;
nothing else consumes the snapshot yet (051 wires resolution). Repo stays
fully green.

## Deliverables
1. `scripts/gen_snapshot.py`: introspects the LIVE ffmpeg via the existing
   registry machinery — force-load EVERY in-fence filter's options AND every
   source's, plus the fenced-options set for the array-returning trio and
   the census names (fenced_options path) — and writes
   `sqlmpeg/reference_registry.json`: the registry disk-cache format (same
   format_version) wrapped with {"snapshot_of": "<ffmpeg -version line 1>",
   "generated": "<passed via --stamp arg, not Date.now>"}. Deterministic
   output (sorted keys) so regeneration diffs cleanly.
2. `sqlmpeg/registry.py`: `load_reference() -> Registry` reading the
   vendored JSON (importlib.resources; the JSON ships in the wheel — add it
   to package data in pyproject [tool.setuptools] if needed: check how
   package data is configured; touch pyproject ONLY for that). A Registry
   built from the snapshot is fully populated (options preloaded — no lazy
   -help calls, no subprocess ever). `Registry.source` attribute or similar
   marking snapshot vs live (051 and `explain` annotations will want it).
3. Generate and COMMIT the snapshot from this machine's ffmpeg 7.1 (~464
   filters + ~40 sources with options; measure the JSON size — if it is
   embarrassingly large, options-for-all may need trimming to options-on-
   demand-at-generation... report the number; expect low single-digit MB at
   worst, fine for a wheel).
4. Tests: gen_snapshot determinism (two runs, identical bytes); round-trip
   (load_reference() serves the same answers the live registry gave for a
   spot-check set: gblur options, xfade constants, testsrc source, timeline
   flags, fenced trio); wheel-data presence (importlib.resources finds it
   from an installed -e package); snapshot loads with NO ffmpeg on PATH
   (monkeypatch which -> None, everything still answered, zero subprocess
   calls — count them).
5. Docs: none (053 owns prose). No CLI changes (051).

## Verify
ruff; mypy --strict changed modules; pytest tests/ -q FULLY green; -m exec
green; `pip install -e .` clean. Baseline 1388 + 106. No git commands; no
version bump. Report: snapshot size, format decisions, contract notes for
051 (how resolution should choose live-vs-snapshot, what marks the
difference).
