# 100 — Namespaced package resolution (Phase 2)

The language half of the package system: how a query reaches a
definition that does not live in its own text. Plan 101 adds the client
that writes these files; this plan only READS and RESOLVES them.

## Why not text splicing

An unused definition is an error (`functions.py:890` "is never called",
`parser.py:2478` for a view). So prepending a library's SQL fails on
every script that uses fewer than all of it. Instead a namespaced call
is RESOLVED: the expander already inlines a function body per call site
and does not care where the definition came from. Nothing is spliced,
unused definitions never arise, and the flat script namespace is
untouched because package contents never enter it.

## The files (read side only here)

`sqlmpeg.json` - the project manifest, hand-edited:

    { "name": "my-edits", "version": "0.1.0", "namespace": "me",
      "description": "...", "sources": ["src/*.sql"],
      "dependencies": { "broadcast/tracks": "^1.2.0" } }

`sqlmpeg.lock` - machine-owned, added by 101; this plan reads it if
present: each entry pins name, version, namespace, sha256 and the store
path. JSON both (the 3.10 floor rules out `tomllib`).

## Resolution

Layered, first match wins:
1. **The local manifest's own sources** - the project IS a package. Its
   `namespace` resolves to its own `sources`, so a query can call a
   function from the package it lives in. This is how you develop one.
2. **The local lockfile** - what this project installed.
3. **The global lockfile** - what `install -g` put on this machine.

Both local files are found by walking up from the query file's
directory, or cwd when the query is a bare argument. `ffmpeg`,
`sqlmpeg` and `wasm` are reserved and may not be claimed.

**A package source is a LIBRARY, not a script**: an uncalled definition
in one is fine (the whole point), while an uncalled definition in the
user's own query stays an error. That asymmetry is deliberate and must
be explicit in the code.

**Shadowing warning**: resolving inside a project but landing on the
global layer is almost never intended - warn, naming the package and
suggesting a local install. A diagnostic, never a rejection, and
structured rather than printed for library/MCP callers.

## Waves

A. **Project discovery + the local layer.** Manifest parsing and
   validation, the upward walk, a `PackageSet` the compiler consults,
   `ns.fn(...)` resolution through the existing expander, the
   library-vs-script rule, typed rejections (unknown namespace, unknown
   member, reserved namespace, a source that fails to parse) with
   did-you-mean hints. No store, no network, no lockfile - and already
   useful on its own: multi-file projects, `src/*.sql` callable from
   `queries/*.sql`.

   The library API needs a way to say WHERE a query came from -
   `compile_sql(text)` has no idea today. Add an optional project/
   package-set parameter rather than reading cwd inside the compiler,
   so MCP and library callers stay explicit and tests stay hermetic.

B. **The lockfile layers.** Local then global, reading blobs from the
   content-addressed store, plus the shadowing warning. Store layout
   follows the registry disk cache exactly (`registry.py:414-643`):
   under `~/.cache/sqlmpeg/`, sha256-addressed, atomic replace, every
   OSError swallowed, format-versioned.

## Checks

A package function called from a query compiles to the SAME command as
the same body written inline - byte-identical argv, the test that
settled table functions. Plus: hermetic (no network, no home-dir
writes in the default tier), ruff, mypy, both suites.
