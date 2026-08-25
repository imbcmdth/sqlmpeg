# 118 — ffrwd: the monorepo, and the whole name

Maintainer decisions, 2026-08-25. The project becomes **ffrwd**
(Welsh, "a stream", pronounced *frood*), in a monorepo at
`E:\projects\ffrwd` — off drive D, which is a drive pool that corrupts
small reads under load and breaks Rust compilation outright. Decided
in one sitting: both histories import, the rename goes all the way
down including the language, the pipe host moves in now, and the repo
stays local until the GitHub side is set up by hand.

## Layout

    docs/        the language docs, top level — they describe the system
    cli/         the compiler and CLI (was D:\projects\sqlmpeg)
    sidecar/     the wasm host (was wasm0r's ffwasm + host + wit)
    plans/       on dev-tools only, as before

## History: imported, not restarted

`git filter-repo` rewrites each source repo into its subdirectory
(`--to-subdirectory-filter cli` / `sidecar`), then both merge into one
root with `--allow-unrelated-histories`. Blame and bisect survive; the
rewritten commits lose their old signatures (rewrites re-hash), which
is the accepted price. Source repos are left untouched on their
drives as the signed historical record.

wasm0r's import is path-filtered: `ffwasm/`, `host/`, `wit/`, and the
workspace files come; `modules/` — the module SDK and examples — stays
behind, because wasm0r remains a real project: the module ecosystem's
home. The monorepo hosts modules; it does not own their SDK.

## The rename, all the way down

- **Repo, module, command, PyPI**: `sqlmpeg` → `ffrwd` everywhere —
  the Python package directory, every import, `pyproject.toml`'s name
  and console script, the MCP server name, CLAUDE.md, README (which
  gains the pronunciation), docs. The `sqlmpeg` PyPI name gets a final
  pointer release when ffrwd first publishes, not before.
- **The language**: the macro qualifier `sqlmpeg.` becomes `ffrwd.` —
  `ffrwd.speed`, `ffrwd.loudnorm2` — pairing with `ffmpeg.` as the
  raw/curated sibling namespaces. Breaking, pre-1.0, one release.
- **Project files**: `sqlmpeg.json` / `sqlmpeg.lock` become
  `ffrwd.json` / `ffrwd.lock`. The registry respell (plans 112/113 B)
  carries the manifest rename on its side of the break.
- **Environment**: `SQLMPEG_*` variables become `FFRWD_*`, no
  fallback — one spelling, per the house rule.
- **Recipes**: every `$ sqlmpeg compile ...` command line becomes
  `$ ffrwd ...`, and every pin regenerates by running the compiler —
  never retyped.
- **The sidecar**: the `ffwasm` package and binary become
  `ffrwd-wasm`. The **`wasm0r` lib crate and the `wasm0r:av` wit world
  keep their names**: that is the module ABI third-party modules
  target, an identity deliberately separate from the host tool — the
  same raw/curated split as `ffmpeg.` beside `ffrwd.`.

## What does NOT change

- The dialect's own name for itself in prose can say ffrwd, but the
  two-rules statement, the grammar, and every language feature are
  untouched — this is a rename, not a redesign.
- `want.video` / the registry repo: its own decision, rides 112/113 B.
- wasm0r's repo continues at its own address with `modules/`.

## Order

1. **Surgery** (orchestrator, by hand — history operations are not
   delegated): init `E:\projects\ffrwd` with the SSH signing config
   replicated; filter-import sqlmpeg (main + dev-tools) under `cli/`;
   filter-import wasm0r's host paths under `sidecar/`; merge; move
   `plans/` to the top level on dev-tools.
2. **Build proof**: `cargo build` in `sidecar/` on E: — the drive-pool
   escape is the point — and the full Python suite green from
   `cli/` before any rename.
3. **The rename wave** (agents, in the new repo): module and imports;
   pyproject and console script; the `ffrwd.` qualifier; env vars;
   project-file names; docs to top level with test paths following;
   every recipe pin regenerated; CI workflows ported with `cli/`
   paths. Full local checks green.
4. **Records**: top-level CLAUDE.md written for the monorepo; plan 117
   updated (names now decided, repo layout now real); memory updated.
5. GitHub, when the user creates the repo: remotes, branch
   protection, CI, and the first push — not before.

## House rules

Unchanged, and they travel: agents do not commit; the two banned words
appear nowhere; no plan numbers in code, comments, docstrings or
errors; recipes' pins are generated, never typed; never push red.
