# 107 — An alias belongs to the manifest that declares it

Maintainer, 2026-08-23, after `imbcmdth/images` became the first package
to depend on another and the dependency resolved by luck.

## The bug, reproduced

`imbcmdth/images@2.3.0` declares `"dependencies": {"video":
"imbcmdth/video@^2.2.0"}` and its `motion-thumbnail` program calls
`video.shot(...)`. Install both into a project and it works — but only
because the consumer's own manifest happens to bind the same word:

    sqlmpeg install imbcmdth/video --alias vid
    sqlmpeg install imbcmdth/images
    sqlmpeg compile imbcmdth.images.motion-thumbnail ...
    error: unknown namespace 'video' (hint: did you mean 'demo'?)

Nothing about `images` changed. The consumer named its own dependency
differently, and a program inside `images` stopped resolving.

`functions.py:1266` is the whole of it: a one-segment qualifier is looked
up with `packages.aliased(qualifier)`, and `PackageSet.aliases` is ONE
flat table built from the project's manifest. Whose source the call site
sits in never enters into it.

## Why it is worse than it looks

- **A package cannot be depended on twice under different names.** A
  depends on B and C; both depend on D; B writes `codec.encode(...)` and
  C writes `d.encode(...)`. One flat table cannot hold both, so one of
  them breaks, and which one depends on install order.
- **A consumer can capture a package's calls.** If the project binds
  `video` to something else entirely, `images`' program resolves to the
  wrong package and compiles — silently, since both are valid packages.
- The published `imbcmdth/images@2.3.0` is affected today.

## The rule

**An alias is scoped to the manifest that declares it.** A call site
inside package P's source — a `lib` file or a `bin` — resolves a
one-segment qualifier through P's own `dependencies`. A call site in the
user's own query resolves through the project's manifest. There is no
global alias table.

That is Cargo's model and it exists for this reason: each crate's
manifest names its own dependencies, so two crates may call one
dependency different things and neither can be captured by a name a
consumer chose.

The full path is unaffected and stays the escape hatch:
`imbcmdth.video.shot(...)` means one thing everywhere.

## What changes

- **`PackageSet.aliases` stops being the answer.** It remains the
  PROJECT's bindings, used only for a call site in the user's own query.
  A package's bindings come from `Package.aliases`, which
  `read_manifest` already parses and nothing currently reads at
  resolution time.
- **The expander must know whose source it is expanding.**
  `_Expander._scoped` already tracks the owning package so a bare call
  in a library body finds that package's own definitions — the same
  hook answers "whose aliases", so this is a lookup change rather than
  new machinery.
- **A dependency's dependencies resolve the same way**, recursively: the
  scope follows the source, however deep.
- **Rejections name the manifest.** "package 'imbcmdth/images' declares
  no dependency 'video'" is a different sentence from "this project
  declares none", and the reader needs to know which manifest to edit.

## `install` must fetch dependencies

Separate defect, same discovery. `sqlmpeg install imbcmdth/images`
installs `images` and stops, so a package with a dependency is broken on
arrival — the consumer gets `unknown namespace` for a name they never
wrote.

Plan 101 deferred dependency SOLVING, and that stands: there is no
resolver and ranges are still recorded rather than solved. But not
solving a range is not the same as not fetching the package. `install`
should walk the fetched manifest's `dependencies`, install each at its
highest published version, and record each in the lockfile — no
backtracking, no unification, just not stopping at depth one.

Two rules keep that honest:

- A dependency already in the lockfile at a different version is a
  rejection naming both. Choosing between them is a solver's job, and
  there is no solver; saying so is better than picking.
- A cycle is a rejection naming the loop.

## The registry has the same flaw in miniature

`build.py`'s `_resolvable` builds one `PackageSet` with a flat `aliases`
dict, which works only because no package there has two dependencies
yet. It should hand each package its own bindings the same way, and its
check that a dependency resolves to a published package stays.

## Checks

- Two packages depending on one package under different aliases, both
  compiling, in one project.
- A consumer aliasing something else as `video`; `images`' program still
  reaches `imbcmdth/video`.
- A call site in the user's own query resolving through the project
  manifest, unchanged.
- `install` of a package with a dependency bringing the dependency, and
  the program running with nothing else installed by hand.
- The same version conflict and the same cycle, each rejected by name.

## Order

This is a one-way door: queries written against today's behaviour break
either way, and there are two published packages rather than twenty. It
goes before anything else in the package system.

1. Scope alias lookup to the declaring manifest, with the messages.
2. `install` walks dependencies.
3. `build.py` follows.
4. Republish `imbcmdth/images`, whose current version depends on the
   luck this removes.
