# 098 — Lift execution out of the CLI; widen the library API

Prerequisites for the MCP server (099), and real gaps on their own.

## Why

`run`'s execution loop lives only in `sqlmpeg/cli.py` (~508-537): the
per-command timeout, the `-y`/`-n`/`-hide_banner` argv injection, and
the loudnorm2 capture -> `loudnorm.parse` -> `loudnorm.substitute`
handoff. Nothing outside the CLI can execute a compiled query. And
everything prints to stdout, which an MCP server cannot share with its
protocol stream.

The library exports nine names but not `registry.load`/`Registry` -
which `build_system_prompt(registry)` REQUIRES - nor `probe`, nor the
table renderers. A library consumer cannot render a table query or
build the prompt without reaching into private modules, as cli.py does.

## The work

1. `sqlmpeg/execute.py`: the run loop, returning a result object
   (per-command argv, exit code, captured stderr where captured) rather
   than printing. Keep the loudnorm2 in-process substitution exactly as
   it is - it is the reason `run` needs no shell. `cli.py` keeps its
   own printing (the `$ <cmd>` echo, the stderr surfacing) by consuming
   the result; ffmpeg's stderr must still reach the user's terminal
   unchanged when the CLI is the caller, and be capturable when it is
   not.
2. `sqlmpeg/__init__.py` grows the names a consumer needs:
   `registry.load`, `Registry`, `probe`, `render_table`, `render_csv`,
   `TableSink`, and the execution entry point. `tests/test_public_api.py`
   pins `__all__` deliberately - update it in the same commit.
3. `pyproject.toml`: `packages = ["sqlmpeg"]` is an explicit list, so a
   future `sqlmpeg/mcp/` subpackage would be silently missing from the
   wheel. Switch to `[tool.setuptools.packages.find]` (or list them)
   NOW, so 099 cannot ship broken.

## Checks

Pure refactor: no behavior change, no pinned command moves. The FULL
exec tier is the acceptance test - it is the only proof the loudnorm2
chain and the timeout survived the move. ruff, mypy --strict, both
suites.
