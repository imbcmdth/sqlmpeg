# 107 — Remove aliasing; every cross-package call is explicit

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

## The decision: remove aliasing

Maintainer, 2026-08-23. Not "scope aliases correctly" — **remove them**,
and require every call across packages to be written in full:

    imbcmdth.video.shot(...)

The reasoning is an asymmetry. Getting aliasing wrong is a breaking
change; not having it is not. An explicit path is valid under every
aliasing scheme we might later choose, so **reintroducing aliases is
purely additive** — no call site written today has to change. Shipping
the wrong scoping rule, by contrast, breaks queries the moment we fix
it, and we have already shipped one package that resolves by luck.

So the hard design question is deferred rather than answered, at the
cost of some verbosity, and nothing about that trade expires.

What goes with it:

- **`PackageSet.aliases` and `Package.aliases`** as a resolution input.
  A manifest still declares what it depends on; that is no longer a
  binding of a name.
- **`install --alias`**, and the alias-collision rejections around it.
- **The rule that an alias may not equal an installed namespace.** It
  existed only to keep a two-segment call decidable. With aliases gone,
  two segments are unambiguously `namespace.package` reaching a default
  `lib`, and one segment is the script's own definitions. There is
  nothing left to disambiguate.
- **The one-segment qualifier branch** in `_member` (`functions.py:1266`)
  — the site this plan opened with.

`dependencies` should then be keyed by package NAME rather than by an
alias, which is npm's shape and what a reader expects:

    "dependencies": { "imbcmdth/video": "^2.2.0" }

What we lose, and it is worth writing down: an alias was insulation
between a query's text and a package's registry identity, so an upstream
rename or a fork meant editing one manifest line rather than every call
site. That argument stands — it is simply not worth buying with a
scoping rule we would be guessing at. When aliases return they should
return deliberately, with the per-manifest scoping this plan describes
above, and explicit paths will still work.

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

## The registry follows

`build.py`'s `_resolvable` builds a flat `aliases` dict; it stops doing
that and simply makes each dependency's package resolvable by name. Its
check that a dependency names a package the registry publishes stays.

`imbcmdth/images` calls `video.shot(...)` and becomes
`imbcmdth.video.shot(...)`; its manifest's dependency is keyed by name.
That is a republish, and the version it replaces resolves by luck.

## Checks

- A cross-package call written in full, compiling and running.
- A one-segment qualifier that used to be an alias is now an unknown
  name, with a message saying calls across packages are written
  `<namespace>.<package>.<member>`.
- Two packages depending on one package, both compiling in one project,
  which is the case that could not work before.
- `install` of a package with a dependency bringing the dependency, and
  the program running with nothing installed by hand.
- A version conflict and a cycle, each rejected by name.

## Order

1. Remove aliasing: resolution, manifest key, `install --alias`, the
   disjointness rule, and the messages.
2. `install` walks dependencies.
3. `build.py` follows.
4. Republish `imbcmdth/images` against the explicit form.
