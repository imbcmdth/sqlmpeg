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

**Install's only job is to put packages into the store by hash**
(maintainer, 2026-08-23). It walks the fetched manifest's
`dependencies`, installs each at the declared version, and recurses. A
package already there at that exact version is skipped — not refetched,
not walked again. A cycle is a rejection naming the loop.

**Two versions of one package are not a conflict.** They are different
content with different digests, they coexist in the store, and install
never arbitrates between them. It installs both and records both.

## Versions are per manifest, because calls are inlined

Install can stay that simple because the compiler INLINES every call at
its call site. There is no shared runtime for two versions to fight
over — a version exists only as a body pasted into a caller.

So if A depends on B and C, and B depends on D@1 while C depends on D@2:
expanding B's function resolves B's `imbcmdth.d.foo(...)` against
**D@1**, expanding C's resolves against **D@2**, and A ends up holding
both inlined bodies. Nothing has to agree, and no resolver is needed to
make them.

Which sharpens what removing aliases bought. It makes the NAME global
and unambiguous; it does not make the VERSION global.
`imbcmdth.video.shot` written inside package P means *the
`imbcmdth/video` that P declares*. Resolution still has to know whose
source it is expanding — `_Expander._scoped` already tracks the owning
package so a bare call finds that package's own definitions, and the
same hook answers which version to reach.

Consequences:

- The lockfile records every installed package as name, version and
  digest; several versions of one name are legal.
- `PackageSet` must answer "package `<name>` at the version P declares",
  not merely "package `<name>`".
- A call site in the user's own query resolves against the version the
  PROJECT manifest declares.

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
- Two packages depending on different versions of one package: both
  installed, both inlined, each caller reaching its own.
- A cycle rejected by name.

## Order

1. Remove aliasing: resolution, manifest key, `install --alias`, the
   disjointness rule, and the messages.
2. `install` walks dependencies.
3. `build.py` follows.
4. Republish `imbcmdth/images` against the explicit form.
