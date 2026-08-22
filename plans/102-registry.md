# 102 — The registry: main is source, gh-pages is the repository (Phase 4)

## Two things, both called the repository

Keep them apart, because almost every mistake in this plan so far came
from running them together.

**`main`** — the source, and only the source. `packages/<owner>/<name>/`
holds the CURRENT files of each package: one directory, no version in
the path, no built artifact anywhere. It stays a tree of text that reads
as a diff.

**`gh-pages`** — the package repository, and the only thing a client
ever talks to. Built from `main`, served by GitHub Pages, and it holds
the archives, the indexes and the HTML. No `packages/`, no raw `.sql`.

The branch ACCUMULATES: publishing adds this release's archive and
leaves every earlier one alone. That is what makes an old pin still
resolve, and it is why nothing in `main` has to remember what was
published — the published thing is the record. The branch's own git
history is the backup.

So publishing, on every push to `main`, is: check out `gh-pages`, pack
each package's current source, add the archive if its digest is not
there yet, regenerate the indexes and the HTML from the archives
present, commit.

It is not a service. Static files ARE the API: the client fetches them,
the website fetches the same ones, and there is nothing running between
to break, rate-limit or pay for.

## Submitting

A PR against `packages/<owner>/<name>/`, holding a `sqlmpeg.json` and
the files it names — the same manifest the compiler already reads, so a
package is a project someone pushed. A release is a version bump in that
manifest and nothing else; the archive is CI's to build, so a
contributor never runs a packaging step and there is no committed
artifact anyone has to trust.

One rule protects a pinned digest, and it is checkable rather than
procedural: **a version already published may not resolve to different
bytes.** `pack` is deterministic, so CI packs the source, looks up that
`name@version` in the published index, and refuses when the digest moved
— changed content needs a new version. An archive already on `gh-pages`
is never rewritten or deleted.

`owners.json` at the root maps `<owner>` to the GitHub accounts allowed
to touch it. First PR into an unclaimed owner directory claims it for
its author; a later PR touching someone else's is rejected by CI. That
is the whole trust story in v0, and it is enough while submission is a
review.

## What CI validates

Every check runs `sqlmpeg` itself, installed from PyPI. The registry
does not reimplement one rule of the dialect.

- The manifest parses and validates (`read_manifest`), and its
  `namespace` is not reserved.
- The directory agrees with the manifest: `packages/<owner>/<name>/`
  matches its `"name"`.
- Every export holds `CREATE FUNCTION` definitions and nothing else;
  every bin compiles — `sqlmpeg validate`, with the package's own
  namespace resolvable, so a bin may call its own exports.
- Every dependency resolves to a published version.
- Packing the source produces the digest the published index already
  records for that `name@version`, or that version is not published
  yet. Content that changed without a version bump is the rejection.

## What CI publishes

The indexes and the HTML are regenerated from the archives on the
branch, never from the source tree. What a version's page says about it
— its signatures, its programs, its description — comes from unpacking
that version's own archive, so an old version describes itself as it
was rather than as the current source is.


- `index.json` — the whole catalogue: name, latest version, namespace,
  description, the function names it exports and the programs it ships.
  Small enough that the site searches it client-side and the client
  caches it whole.
- `p/<owner>/<name>.json` — the detail: every published version, and per
  version the archive's `sha256` and size, the function signatures and the
  programs with their variables.
- `archives/<sha256>` — one gzipped tar per published version, under
  the sha256 of its own bytes. These are what accumulate; the indexes
  are what make them findable.

**The digest is over the archive.** A package is not only SQL: a wasm
filter ships a binary, so packages compress and the thing that travels
is one compressed file. Hashing what travels is what lets the client
throw a bad download away without opening it - bytes in, digest, compare
to the pin, discard. Nothing unverified ever reaches an unpacker, which
is the ordering that matters when the unpacker is the part with a
history of path traversal, symlinks and expansion bombs.

So `store.digest` over a directory is the wrong pinned value and goes:
the lockfile's `sha256` is the archive's, verified at install against
the bytes off the wire. Reads out of the store trust the store - it is
the user's own cache under their own home, and the pin already did its
work at the boundary where the content was untrusted. Re-hashing an
unpacked tree on every compile was affordable for a few KB of SQL and is
not for a wasm binary.

Verified is not the same as safe to extract: the digest proves the
archive is the one the registry published, not that its members are
well-behaved. The extractor takes regular files and directories under
the root and nothing else - no absolute paths, no `..`, no links, no
devices - with a member count and an uncompressed size cap. Python's
`tarfile` only grew a data filter in 3.12 and this project supports
3.10, so that check is ours to write.

**Deterministic archives.** Sorted member order, zeroed mtimes, uid/gid
0, normalized modes, gzip with no timestamp: CI rebuilding a version
produces the same bytes and so the same digest, and a published version
never changes underneath a pin.

**The client already pins the shape.** Both `index.json` and the detail
files must carry `"format_version": 1`; the client refuses another
rather than guessing what its keys mean. The detail file is what decides
which version is highest - the catalogue's `version` field is a search
convenience, so a stale cached catalogue can never pin an older release
than the one published. Package names are `<owner>/<name>`, lowercase,
each half starting with a letter or digit, checked before a name is ever
part of a URL or a path.

## The site

Built from the same JSON by the same CI run: a search page reading
`index.json` client-side, and a real HTML page per package — install
command, function signatures, programs and their variables, versions.
A registry people find through web search wants pages a crawler can
read, so the package pages are rendered, not fetched. The rule is that
they are rendered from the JSON the client reads, in the same run that
writes it; HTML maintained alongside that data is what drifts from it.

**No static site generator** (maintainer, 2026-08-22). The build already
has to be Python: it runs `sqlmpeg` from PyPI to validate manifests,
compile bins and pack archives, and it emits the JSON whether or not
there is a website. An SSG would be a second toolchain rendering two
templates from data the first one already produced, and every SSG wants
to own the content model — which here is
`packages/<owner>/<name>/sqlmpeg.json`, validated by sqlmpeg itself. So:
one `build.py`, Jinja2, one CI job.

## The client's base URL

One setting, defaulting to the published site, overridable by
environment variable. That is what makes plan 101's checks hermetic: a
fixture registry is a directory of these exact files served from
`tmp_path`, and a private registry is the same files behind any static
host.

## Versions

Exact pins only in v0. `install <pkg>` takes the highest published
version and writes it exact; `install <pkg>@<version>` takes that one.
A manifest's `dependencies` range is recorded and shown, not solved —
there is no resolver until there are transitive dependencies worth
solving.

## Namespaces collide, and that is the client's problem

Two packages may both claim `tracks`. The registry does not arbitrate:
a namespace is what the AUTHOR thinks the package should be called, and
the consumer's lockfile is where one name maps to one package. The
client already rejects two entries claiming one namespace; the escape
hatch is installing under a different one, which is a client feature
(plan 101) and not a registry rule.

## First package

`sqlmpeg`'s own `queries/` — 34 programs with `-- variables:` headers,
already exercised by that repo's CI. It makes the registry useful to the
first person who runs `sqlmpeg search`, rather than empty until authors
show up. Published as `sqlmpeg/queries`, namespace `queries`; programs
only, no exports.

The files are COPIED into the source repo, not mirrored from the
sqlmpeg repo. A package is a directory here — that is the whole model,
and a build that reaches into another repository to assemble one is not
it. A published version is frozen on `gh-pages` anyway, so a later
divergence is a new version, not drift.

## Not in v0

`sqlmpeg publish` pushing directly (submission stays a PR), download
counts, deprecation and yanking, and any server-side search.
