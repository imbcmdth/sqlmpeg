# 117 — The sidecar comes home: platforms and wheels

Maintainer, 2026-08-25. The wasm sidecar (today wasm0r's `pipe` host)
will likely move into this repo, and when it does it must build for
the big platforms and reach users as native Python packages. The
packaging shape is worth deciding BEFORE the code moves, because the
wrong first move — the binary inside the main wheel — is expensive to
unwind on PyPI.

## The decision: two packages, one repo

**`sqlmpeg` stays a pure-Python universal wheel. The sidecar ships as
a separate platform-wheel package, `sqlmpeg-sidecar`, that the core
discovers at run time.**

Why the split is right:

- The sidecar is a Rust binary embedding wasmtime — tens of MB per
  platform. Folding it in turns every `pip install sqlmpeg` into that
  download and every release into a platform-matrix build, for a
  feature most users have not turned on. Today's posture (wasm behind
  its own surface, `--allow-unsafe` precedent) says optional things
  install optionally.
- The core wheel staying `py3-none-any` keeps today's release
  procedure exactly as it is: `ci.yml` green, `release.yml` publishes
  via `uv publish`, no toolchain but Python.
- The pieces version independently. The sidecar changes when the wasm
  world (`wit/av.wit`) changes; the compiler changes weekly. Coupling
  their release cadence would make every compiler release rebuild six
  platform wheels for nothing.
- `pip install sqlmpeg[wasm]` is the user-facing spelling: an extra
  that depends on `sqlmpeg-sidecar` with a compatible-version range.

## Repo layout

    sqlmpeg/            the compiler, unchanged
    sidecar/            cargo workspace: the wasm host
      Cargo.toml
      src/
      pyproject.toml    maturin, package name sqlmpeg-sidecar
    pyproject.toml      the pure-Python package, unchanged

One repo so the compiler and the host evolve against one test suite
(plan 114's exec tests drive both), two publishable packages out of
it. wasm0r's repo keeps the module SDK and the frei0r host; what
moves here is the pipe host — the thing the compiler spawns.

## Build tool: maturin — challenged and kept

The maintainer's challenge (2026-08-25): this is not a Python
extension, just a native binary — is maturin necessary, and is a bare
binary on PyPI even allowed?

**Allowed, and mainstream.** `uv` — the tool this project runs every
command through — is a pure Rust binary shipped as PyPI wheels with no
Python code in the package. So is `ruff`. Both use maturin's
`bindings = "bin"` mode, which exists precisely for this shape: no
PyO3, no extension module, a compiled executable in the wheel's
scripts directory.

**Not necessary, but earning its keep.** The alternative is a
hatchling/setuptools custom build hook running cargo — and then this
repo owns the genuinely fiddly parts maturin does for free:

- honest platform tags, including the manylinux compliance audit
  (glibc symbol versions, linked libraries) — a hand-rolled hook can
  ship a wheel pip installs on systems where the binary then fails;
- scripts-directory placement, which is exactly where the
  `binaries.py` discovery chain looks;
- the CI matrix, via `maturin-action`'s manylinux containers and
  cross-compilation.

maturin is kept for the audit-and-tags work, not for any Python
machinery.

**`setuptools-rust`, also considered (maintainer, same day).** It has
the right mode — `RustBin` ships plain executables, beside the
`RustExtension` it is better known for — and the main package already
uses setuptools. Two things decide against it. The shared-backend
argument is smaller than it looks: the sidecar is its own package
with its own pyproject either way, so nothing is reused but
familiarity. And the lightweight comparison inverts over the whole
stack: setuptools-rust builds the wheel but leaves the platform work
to a `cibuildwheel` + `auditwheel`/`delocate` pairing — four tools
where maturin is one containing the audit, the tags, and the CI
action. The precedent splits the same way: Rust EXTENSIONS often use
setuptools-rust (cryptography), pure binaries landed on maturin (uv,
ruff). Kept as the named fallback if maturin ever grates; the
scaffolding wave should not need to revisit this.

Target matrix, in order of user population:

| target | notes |
| --- | --- |
| manylinux x86_64 | the CI baseline; also what WSL users get |
| macOS arm64 | |
| macOS x86_64 | or a universal2 wheel covering both |
| Windows x86_64 | |
| manylinux aarch64 | cross-compiled or QEMU in CI |
| musllinux x86_64 | cheap to add with maturin-action; decide by demand |

wasmtime supports all of these; nothing in the sidecar is
platform-conditional except the transport layer plan 114 already
scopes per-platform.

## Discovery: the binaries.py pattern, extended

`sqlmpeg/binaries.py` already answers "where is ffmpeg" with a
provider chain. The sidecar gets the same treatment, one function
beside `ffmpeg_path()`:

1. `SQLMPEG_SIDECAR` environment override — first, because every
   escape hatch here is env-shaped.
2. The installed `sqlmpeg-sidecar` package — resolved via
   `importlib.metadata` entry point, not PATH, so a venv's sidecar
   wins over a global one.
3. PATH fallback (`sqlmpeg-sidecar` executable) for people who built
   their own.

A version handshake at spawn: the sidecar prints its world version
(`wasm0r:av@x.y.z`) on request; the compiler refuses a world it does
not speak, with the install hint. Same posture as the ffmpeg version
handling: the tool in front of you is a fact to check, not an
assumption.

## CI

- `ci.yml` untouched. A new `sidecar.yml` runs on changes under
  `sidecar/` only: cargo test + clippy + fmt, then a maturin build of
  the current platform as a smoke check. The full matrix runs only on
  release tags — platform wheels are release artifacts, not
  per-commit ones.
- Release: a separate tag namespace (`sidecar-vX.Y.Z`) triggers the
  matrix build and `uv publish` of the wheel set. The main package's
  release procedure is untouched.
- The exec tier gains sidecar tests ONLY when plan 114's execution
  waves land; until then `sidecar/` carries its own cargo tests and
  nothing in the Python suite depends on it.

## What this plan is not

Not the sidecar's code move (that waits on wasm0r's plan 010 world
being settled), not the `LANGUAGE wasm` surface (plan 106), not the
process-graph machinery (plan 114). This is the packaging decision
and the scaffolding order, so that when the code moves, it lands in a
shape that already publishes.

## Order

1. Scaffold `sidecar/` with a hello-world host binary, maturin
   config, and `sidecar.yml` — prove the wheel builds and installs on
   the dev machine.
2. The discovery function in `binaries.py` + the version handshake
   stub, tested against the hello-world binary.
3. Release-tag workflow with the full matrix; publish an 0.0.x to
   PyPI to claim the name and prove the pipeline end to end.
4. The code move, when wasm0r's world settles — arriving into
   scaffolding that already ships.

## House rules

- **Do not commit.** Leave the work uncommitted for review.
- Never write the two banned words in code, comments, docs or reports.
- Never cite a plan or RFC number in code, comments, docstrings or
  error messages.
- Short factual WHAT comments only.
- Run the targeted tests, not the whole suite. Report concisely.
