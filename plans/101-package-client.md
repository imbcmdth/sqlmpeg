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
- `sqlmpeg publish` — a STUB: exits with a typed message that
  publishing is not open and submissions are a PR to the registry repo.
  It ships so the command surface is honest about the intended shape.

## Rules

- Every write is atomic (`tempfile.mkstemp` + `os.replace`), like the
  registry cache.
- The lockfile is byte-reproducible: no clock, insertion order, LF —
  the `scripts/gen_snapshot.py` discipline, already proven in CI.
- Network failures are typed errors naming the URL, never tracebacks.
  An install that cannot verify a hash leaves the store untouched.
- `init` and `install` never run ffmpeg and must work with none present.

## MCP

`search` becomes a tool (read-only, safe). `install`/`link` write to
disk and the lockfile — a THIRD trust posture beyond `--allow-run`, so
they need their own flag if exposed at all. Wave A already gives every
tool a `project` argument to work in.

## Checks

`init` then `install` then compile, hermetic against a fixture index
served from a tmp_path; the second install is offline; a tampered blob
fails the hash check and leaves nothing behind; the lockfile
regenerates byte-identically; `install` outside a project exits 2 with
both exits named.
