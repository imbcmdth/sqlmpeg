# 032 — Dynamic filters polish: CLI, prompt --dynamic, docs, v0.4.0
(model: sonnet · main · RFC-003 final wave)

Read plans/rfc-003-dynamic-filters.md, the committed 030/031 code
(registry.py, lower.py/compiler.py signatures), cli.py, prompt.py,
docs/errors.md, README.md.

## Deliverables
1. cli.py: `--portable` on compile/explain/validate →
   compile_sql(portable=True). Help text: "reject filters and named options
   that depend on the installed ffmpeg". run does NOT get it (run needs
   ffmpeg anyway). Tests.
2. `sqlmpeg prompt --dynamic`: appends an "Installed filters" section from
   registry.load() — name, rendered pad signature (video, video) -> video,
   one-line doc; sorted; count in the header; a paragraph explaining these
   are machine-dependent and options are named (=>) and discoverable through
   validate --json repair. No per-filter option dumps (RFC). If the registry
   is unavailable: print the base prompt plus a single note line, exit 0.
   The BASE prompt (no flag) is byte-identical to today — docs/system-prompt.md
   freshness test must keep passing unchanged. Tests: --dynamic contains
   gblur and its signature (exec); base output unchanged (offline).
3. docs/dynamic-filters.md (new, hand-written): the two-tier model, named
   args, machine-dependence, --portable, the introspection cache location,
   the overlay/trim builtin-collision caveats (from lower.py's docstring).
   Linked from README.
4. docs/errors.md staleness pass: recapture UNKNOWN_FUNCTION (18-function
   hint + sharpen() example are stale — sharpen is real now) and any other
   example whose output drifted; verify every example against
   `.venv\Scripts\sqlmpeg.exe validate --json` (scratchpad files).
5. README: "Any ffmpeg filter" section after Encoding — the two-tier pitch,
   one tier-2 example with real compiled output (drift-pinned via the
   established content-keyed pattern; use a query against tests/fixtures so
   it compiles — e.g. unsharp with two named options), one tier-1 named-extra
   line. Mention --portable.
6. Version 0.4.0 (pyproject + __init__). CI: verify no changes needed.
7. Full gate: pytest, pytest -m exec, ruff, mypy sqlmpeg/ — paste all.

## Do NOT
Touch lower/parser/registry/stdlib/emit/split/ir source. The trim/split
builtin-collision wart has its own tracked task — do not fix it here, but
document it in docs/dynamic-filters.md.
