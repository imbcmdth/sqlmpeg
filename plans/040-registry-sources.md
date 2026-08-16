# 040 — Registry: sources + timeline flag  (model: sonnet · main ·
RFC-005 wave 1, parallel with 041 — registry.py/tests/test_registry.py ONLY)

Read plans/rfc-005-everyday-gaps.md §1 §2 and sqlmpeg/registry.py (its module
docstring documents the -filters format quirks; extend, don't rewrite).

## Deliverables
1. Parse and RETAIN what the fence currently discards:
   - `DynamicFilter.timeline: bool` from the flags column (`T..`).
   - Sources: `|->V` / `|->A` lines become `SourceFilter(name, output:
     StreamType, doc)` exposed via `Registry.get_source(name)` and
     `source_names()`. Multi-output sources (`|->N`, `|->VV`?) and all
     sinks (`->|`) stay excluded — verify what shapes actually appear in
     the real -filters output and record counts.
   - `Registry.options(name)` must work for sources too (same lazy -help
     path; verify `-help filter=testsrc` / `anullsrc` / `sine` / `color`
     option output empirically — capture snippets as offline fixtures).
2. Column-function lookup (`get`) behavior UNCHANGED for sources (still
   None — a source is not callable as a column function; 042 wires FROM).
3. Disk-cache format: bump/extend compatibly (new fields; old cache files
   must be invalidated or tolerated — simplest: include a format version in
   the payload and rebuild on mismatch).
4. Tests: offline fixtures for T-flag parsing (gblur has T, hflip does
   not — verify), source parsing incl. output types, sinks-still-excluded,
   cache-format migration; exec tests: real counts (sources > 20?),
   testsrc/anullsrc/sine/color present with correct types, timeline flags
   sane (drawbox T, null not? — verify and pin a few).

## Verify
ruff; mypy --strict sqlmpeg/registry.py; pytest tests/test_registry.py -q +
-m exec; full pytest tests/ -q green (baseline 1025 + 76). No git commands.
Report format quirks for plan 042's author.
