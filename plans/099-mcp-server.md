# 099 — `sqlmpeg mcp`: the local MCP server

Runs after 098. Closes the AI loop: the dialect prompt and the typed
error contract were built for a repair loop, and MCP is how an editor
or agent reaches them without shelling out.

## Surface

`sqlmpeg mcp` starts a stdio server. Tools, all thin wrappers over the
library (never over the CLI):

- `compile(query, vars?)` -> the ffmpeg command(s)
- `validate(query, vars?)` -> `{}` or the typed error object; the
  repair loop `docs/error-schema.json` exists for
- `explain(query, vars?)` -> the IR graph as JSON
- `inspect(query, vars?)` -> a table query's rows (what tracks,
  chapters, cues, attachments a file has)
- `filters(pattern?)` -> what the LOCAL ffmpeg actually has, so a model
  grounds on the real binary rather than its training data
- `run(query, vars?)` -> OFF unless `--allow-run`: it writes files on
  model say-so, a different trust posture from returning text

Resources: the dialect system prompt (`build_system_prompt(load())`),
so a model gets the grammar without spending a tool call.

## Constraints

- stdio: NOTHING may print to stdout but the protocol. Every tool goes
  through the library; `execute.py` (098) is what makes `run` possible
  at all.
- `mcp` is an optional extra, not a hard dependency. `sqlmpeg mcp`
  without it exits with a typed message naming the install command.
- CI runs `mypy --strict` over `sqlmpeg/`; if the SDK lacks `py.typed`
  add an override stanza beside the existing two.
- Adding `mcp` to `_SUBCOMMANDS` is free (a subcommand name only
  shadows a query starting with that word, and a statement can only
  start with SELECT/COPY/CREATE/WITH).

## Checks

Unit-test every tool against the library with no subprocess and no
stdout writes; assert `validate` returns a schema-conformant object for
a bad query and empty for a good one. Then drive it from a real client
end to end.
