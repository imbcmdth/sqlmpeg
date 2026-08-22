# 102 — The registry: a separate repo, a static site (Phase 4)

Where installable packages live. A second repository,
`sqlmpeg-registry`, whose CI validates submissions and publishes a
static site to GitHub Pages. Plan 101 writes the client that reads it.

The registry is not a service. Static JSON IS the API: the client
fetches files, the website fetches the same files, and there is nothing
running between them to break, rate-limit, or pay for. Homebrew's shape,
which is why the repo can be the repository for a long time.

## Submitting

A PR against `packages/<owner>/<name>/`, holding a `sqlmpeg.json` and
the files it names — the same manifest the compiler already reads, so a
package is a project someone pushed. One directory per package; one
directory per version is NOT the shape (see Versions).

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
  matches `"name": "<owner>/<name>"`.
- Every export holds `CREATE FUNCTION` definitions and nothing else;
  every bin compiles — `sqlmpeg validate`, with the package's own
  namespace resolvable, so a bin may call its own exports.
- Every dependency resolves to a published version.
- The version is new and higher than the last published one. A
  published version is never rewritten: content behind a pinned digest
  changing under people is the failure mode content addressing exists
  to prevent.

## What CI publishes

- `index.json` — the whole catalogue: name, latest version, namespace,
  description, the function names it exports and the programs it ships.
  Small enough that the site searches it client-side and the client
  caches it whole.
- `p/<owner>/<name>.json` — the detail: every published version, and per
  version the file list (`path`, `sha256`, `size`), the **tree digest**,
  the function signatures and the programs with their variables.
- `blobs/<sha256>` — one blob per FILE, addressed by the sha256 of its
  bytes.

**Per-file blobs, not archives.** The store's digest
(`store.digest`) is over an unpacked directory, so an archive's own hash
would not be the thing the lockfile pins — the client would have to
unpack before it could verify, which means trusting the archive first
(path traversal, symlinks, expansion bombs) and verifying second. With a
file list there is nothing to unpack: the client fetches each blob,
checks each against its own digest, writes the tree, and recomputes
`store.digest` over the result. It must equal the tree digest the detail
JSON records. Packages are a handful of small text files, so the request
count is not a cost worth an archive format. Files also dedupe across
versions for free.

CI computes the tree digest with `sqlmpeg`'s own `store.digest`. One
definition of what a package hashes to, used by both sides.

## The site

Built from the same JSON by the same CI run: a search page reading
`index.json`, and a page per package reading its detail file — install
command, function signatures, programs and their variables, versions.
No backend, no build-time templating of package content into HTML that
could drift from the JSON the client reads.

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
already exercised by this repo's CI. It makes the registry useful to the
first person who runs `sqlmpeg search`, rather than empty until authors
show up.

## Not in v0

`sqlmpeg publish` pushing directly (submission stays a PR), download
counts, deprecation and yanking, and any server-side search.
