# 102 — The registry: a separate repo, a static site (Phase 4)

Where installable packages live. A second repository,
`sqlmpeg-registry`, whose CI validates submissions and publishes a
static site to GitHub Pages. Plan 101 writes the client that reads it.

The registry is not a service. Static JSON IS the API: the client
fetches files, the website fetches the same files, and there is nothing
running between them to break, rate-limit, or pay for. Homebrew's shape,
which is why the repo can be the repository for a long time.

## Submitting

A PR against `packages/<owner>/<name>/<version>/`, holding a
`sqlmpeg.json` and the files it names — the same manifest the compiler
already reads, so a package is a project someone pushed.

**One directory per version** (corrected 2026-08-22; an earlier draft of
this plan said otherwise and was wrong). A client pins an exact version
and refetches by digest, so every published version's files have to stay
here or its pins die the first time anyone reinstalls. Packages are
small text plus at most a binary, so the duplication is cheap, a version
is auditable as a PR diff, and `pack` being deterministic means any
version rebuilds to the digest it was published under. CI refuses a PR
that modifies or deletes an existing version directory: that is how a
published version stays the one people pinned.

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
- The directory agrees with the manifest: `packages/<owner>/<name>/<version>/`
  matches the manifest's `"name"` and `"version"`.
- Every export holds `CREATE FUNCTION` definitions and nothing else;
  every bin compiles — `sqlmpeg validate`, with the package's own
  namespace resolvable, so a bin may call its own exports.
- Every dependency resolves to a published version.
- The version is new and higher than the last published one, and no
  existing version directory was touched. A published version is never
  rewritten: content behind a pinned digest changing under people is
  the failure mode content addressing exists to prevent.

## What CI publishes

- `index.json` — the whole catalogue: name, latest version, namespace,
  description, the function names it exports and the programs it ships.
  Small enough that the site searches it client-side and the client
  caches it whole.
- `p/<owner>/<name>.json` — the detail: every published version, and per
  version the archive's `sha256` and size, the function signatures and the
  programs with their variables.
- `archives/<sha256>` — one gzipped tar per package version, addressed
  by the sha256 of the archive's own bytes.

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

The files are COPIED into the registry, not mirrored from the sqlmpeg
repo. A registry package is a directory in this repo — that is the whole
model, and a build that reaches into another repository to assemble one
is not it. A published version is frozen anyway, so a later divergence
is a new version, not drift.

## Not in v0

`sqlmpeg publish` pushing directly (submission stays a PR), download
counts, deprecation and yanking, and any server-side search.
