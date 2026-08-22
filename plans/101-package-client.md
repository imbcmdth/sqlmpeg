# 101 — The package client: init, search, install, link (Phase 3)

The write side. Plan 100 reads manifests, lockfiles, links and the
store; this plan creates them. Registry access is HTTP against static
JSON — there is no API to build (plan 102 builds the site that serves
it).

## Commands

- `sqlmpeg init` — writes `sqlmpeg.json` (name from the directory,
  version 0.1.0, a namespace derived from the name), an empty
  `sqlmpeg.lock`, and a starter query. Refuses to overwrite an existing
  manifest.
- `sqlmpeg search <term>` — fetches `index.json` (cached in the store,
  so repeat searches and installs work offline), filters locally over
  name, namespace, description and provided functions. Prints through
  `render_table`; `--json` for scripting and the MCP tool.
- `sqlmpeg install <pkg>[@version]` — resolve against the index, fetch
  the content-addressed blob, VERIFY its sha256, write it to the store,
  add a registry entry to the lockfile and a dependency to the manifest.
  - **Refuses outside a project**: no lockfile walking up is a usage
    error (exit 2) naming both exits — `install -g`, or `init` first.
    Never creates a lockfile as a side effect.
  - `-g` writes the GLOBAL lockfile instead. Same store, same hashes:
    a package installed both ways is stored once.
- `sqlmpeg link <path>` — a link entry pointing at that directory, for
  developing a package against a consumer. Same project guard as
  `install`. `sqlmpeg unlink <namespace>` removes it. The path form is
  primary; npm's two-step register-then-link indirection is the thing
  people get lost in.
- **Running a package's program**: `sqlmpeg run <bin> -v name=value`.
  `main()` already decides whether `argv[0]` is a subcommand or query
  text, and a query can only start with `SELECT`/`COPY`/`CREATE`/
  `WITH` - so a positional that is not valid SQL and matches an
  installed bin resolves as one, unambiguously, by the same check one
  level down. `sqlmpeg list` shows what a project provides and what its
  dependencies provide: functions, programs, and their variables (read
  from the `-- variables:` header the cookbook harness already parses).
- `sqlmpeg publish` — a STUB: exits with a typed message that
  publishing is not open and submissions are a PR to the registry repo.
  It ships so the command surface is honest about the intended shape.

## Waves

Three, in order; each lands green on its own.

**A - the manifest grows a second half.** `sources` -> `exports`, plus
`bin`: a map of program name to query file. Validated by ROLE - an
export holds definitions and nothing else (wave 100A's rule, unchanged),
a bin file is a query and that rule must not reach it. `sqlmpeg list`
prints what a project and its dependencies provide: functions with
signatures, programs with their variables (from the `-- variables:`
header the cookbook harness parses). No new writes, no network.

**B - the local write commands.** `init`, `link`, `unlink`, the
`publish` stub, running a bin by name, and the `--allow-run` ->
`--allow-unsafe` rename. Every write atomic. No network.

**C - the registry client.** `search` and `install`: HTTP against static
JSON, the index cached in the store, blobs verified before they land.
The `search` MCP tool. `install` and `link` behind `--allow-unsafe`.

## Rules

- Every write is atomic (`tempfile.mkstemp` + `os.replace`), like the
  registry cache.
- The lockfile is byte-reproducible: no clock, insertion order, LF —
  the `scripts/gen_snapshot.py` discipline, already proven in CI.
- Network failures are typed errors naming the URL, never tracebacks.
  An install that cannot verify a hash leaves the store untouched.
- `init` and `install` never run ffmpeg and must work with none present.

## MCP

`search` becomes a tool: read-only, always available.

**One capability flag, not a matrix.** Rename `--allow-run` to
`--allow-unsafe` and put every side-effecting tool behind it - `run`,
`install`, `link`. Maintainer decision, 2026-08-22: a permissions
matrix for a local dev tool invites passing every flag, and the
fine-grained gating already lives in the MCP client, which prompts per
tool call. The flag is a coarse capability switch; the precision that
matters goes in each TOOL DESCRIPTION, since that is the text a client
shows when it asks.

Free to rename: `--allow-run` landed on main after the 0.26.0 tag and
has never been in a release. Deferred to this plan only to avoid
colliding with wave B, which is editing the same files.

Wave A already gives every tool a `project` argument to work in.

## Checks

`init` then `install` then compile, hermetic against a fixture index
served from a tmp_path; the second install is offline; a tampered blob
fails the hash check and leaves nothing behind; the lockfile
regenerates byte-identically; `install` outside a project exits 2 with
both exits named.
