# 103 — namespace/package, and what a call writes

**Superseded by plan 104**, which folds this in with the absence rule,
signature defaults, the Cargo-style alias, and the demo sweep. Kept for
the record; do not implement from here.

Supersedes the manifest surface of plans 100 and 101. Maintainer,
2026-08-22. The resolution machinery underneath is unchanged; what
changes is what a package is called and how a call reaches into one.

## The name is the path

A package is `<namespace>/<package>`, and that IS the qualifier:

    imbcmdth/clip            ->  imbcmdth.clip
    broadcast/tracks         ->  broadcast.tracks

So the manifest's `namespace` field goes away. It was a second, separate
claim that could disagree with the name, and every rule that existed to
reconcile the two goes with it:

- **`install --as` is deleted.** It existed because two packages could
  both claim `tracks`. They cannot both be `broadcast/tracks`, so the
  collision it answered no longer exists.
- **The lockfile entry drops `namespace`.** An entry is keyed by name,
  and the path a call writes is derived from it.
- **The entry-versus-package namespace agreement check goes**, along
  with the rule that a linked directory must keep claiming the name it
  was linked under. There is one name, in one place.

A link entry still names a directory; its package's name comes from the
manifest there, as it always did.

## The manifest

```json
{ "name": "imbcmdth/clip",
  "version": "1.0.0",
  "description": "Cut a clip out of a file",

  "lib":  "src/clip.sql",
  "libs": { "quieter": "src/quieter.sql" },

  "bin":  "queries/clip.sql",
  "bins": { "preview": "queries/preview.sql" },

  "dependencies": { "broadcast/tracks": "^1.2.0" } }
```

Singular is the DEFAULT, plural is the map, and the two halves are
symmetric:

| written | reached as |
| --- | --- |
| `lib` | `imbcmdth.clip(...)` — the package path is itself a call |
| `libs` | `imbcmdth.clip.quieter(...)` |
| `bin` | `sqlmpeg run imbcmdth.clip` |
| `bins` | `sqlmpeg run imbcmdth.clip.preview` |

`libs` is a map, the same shape as `bins`: **the key is the exported
function's name** and the value is the file defining it. The manifest is
therefore the complete index of what a package exports — no glob, no
surprise export, and `sqlmpeg list` answers without parsing a line of
SQL. `lib`'s export is named for the package.

A file may define more than the one function its key names; the rest are
private to the package, callable from its own bodies and from nowhere
else. The existing per-package scope is already exactly that rule.

All four keys are optional. A manifest with none is the consumer
project, which exists to hold `dependencies`.

`exports` (plan 101 wave A) becomes `libs`, and the `bin` map becomes
`bins`. Nothing has shipped with either.

## Three segments, and what still has two

`ffmpeg.<filter>` and `sqlmpeg.<macro>` stay two-part and stay reserved.
A package call is `<namespace>.<package>.<member>` or, for a default,
`<namespace>.<package>`.

The parse is decidable because a call has parentheses and a path read
does not: `t.tags.language` is a column reading a map, `imbcmdth.clip.x
(...)` is a call. That distinction already exists; it now has to carry
one more segment.

A project's own definitions stay **bare** (maintainer): `normalize(...)`
inside the project that defines it. The qualified path is for reaching
into a package you installed, and making a project spell its own name to
call its own function buys nothing.

## Reserved namespaces

`sqlmpeg`, `ffmpeg` and `wasm` are refused as the first segment of a
name — in `read_manifest`, and again in the registry's own validation so
nothing under them can be submitted. They belong to the dialect.

## The demo packages

One package per job, not one grab bag (maintainer): `imbcmdth/clip`,
`imbcmdth/transcode`, `imbcmdth/gif`, and so on, each shipping its query
as `bin` so it runs as `sqlmpeg run imbcmdth.clip`. A registry of 34
things people install one of beats one thing nobody wants all of, and it
is what the search page is for.

## Waves

**A — the manifest and the name.** `namespace` out, `lib`/`libs`/`bin`/
`bins` in, reserved first segment, lockfile entry keyed by name,
`--as` deleted. `sqlmpeg list` reads the manifest rather than parsing
exports.

**B — three-segment resolution.** `ns.pkg.fn(...)` and `ns.pkg(...)` in
the parser and the expander; `sqlmpeg run ns.pkg` and `ns.pkg.name` for
programs; every message that says "namespace" reworded.

**C — the registry.** The 34 demos split into packages under
`imbcmdth`, `owners.json` keyed by namespace, the build and the site
following the new manifest.
