# 104 — Absence, defaults, and what a package is called

Supersedes plan 103 and the manifest surface of plans 100–101. One
document for the whole set, because the pieces depend on each other and
the order they land in matters. Maintainer decisions, 2026-08-22.

The short version: **NULL is absence**, everywhere a value could be
omitted; an unset variable is NULL; a function parameter may declare a
DEFAULT; a package's name is the path a call writes; and the demos get
generalized against all of that before the registry is seeded.

---

## Part 1 — Absence

### 1.1 An unset variable is NULL

`:name`, `:'name'` and `:"name"` all substitute to the bare keyword
`NULL` when the variable was not set — never `''`, never the literal
text `:name`, never an error. That is a deviation from psql, which
leaves the text alone; psql's behaviour is useless here and we are a
dialect already.

Substitution records which NULLs came from which variable (the same
source-map idea the function expander uses for body lines), so an error
at the point of use can say `':source' was not set` rather than "NULL
is not a path." That map exists for messages only; nothing else needs
to know where a NULL came from.

### 1.2 NULL is absence at every option

A NULL in an option position means the option is not written, and the
thing being configured supplies its own default. One rule, applied at
each binding site:

| site | absent means |
| --- | --- |
| filter options, positional or named (`_bind_options`) | ffmpeg's default for that AVOption |
| source filter options (`ffmpeg.sine(...)`) | same |
| `input()` options | same |
| `COPY ... WITH (...)` options | the encoder's / muxer's default |

Positional binding already resolves index → option name before
validating (`lower.py:_bind_options`), so the drop is one step there:
after the name is known, before `_option_value` sees the value. A NULL
must never reach type validation. `scale(v, :w, :h)` with `:w` unset
binds index 1 to `height` and never writes `width`; nothing shifts. A
dropped position still counts as `occupied`, so `scale(v, :w, width =>
640)` stays the rejection it is today — the position was written.

This is the rule for a literal `NULL` too, not only for an unset
variable: `scale(v, NULL, 480)` omits `width`. That is what makes it one
rule with no knowledge of provenance.

Every numeric, boolean and enum option in the registry has a default
(2,676 of 2,860 in the reference snapshot), so dropping is always safe
there. The 184 without one are all strings, and "no default" means NULL
string, not required.

### 1.3 What is required

Required is derived from use, never declared:

- `input(NULL)` — a path is required.
- `TO NULL` — a destination is required.
- a NULL in a stream position of any call.
- a NULL for an option on the **curated required list** (below).

Each is a compile-time rejection naming the variable when the NULL came
from one. Everything else falls through to ffmpeg's own error at run
time, which `run` already surfaces — a worse message, but not silent.

**The curated list.** ffmpeg has no "required" metadata (`AVOption` has
no such flag; filters enforce it in `init()`), so this is hand-kept and
says so. Entries, each a filter people actually hit:

| filter | required |
| --- | --- |
| `subtitles` | `filename` |
| `lut3d` | `file` |
| `frei0r` | `filter_name` |
| `ladspa` | `file`, `plugin` |
| `movie`, `amovie` | `filename` |
| `drawtext` | `text` or `textfile` |
| `xfade` | `expr` when `transition = custom` |

The either/or and conditional rows are why this cannot be metadata.
Kept beside `MACROS` in `lower.py`, the other curated knowledge.

### 1.4 The `-v` check reverses

Today an undefined reference is the error. Now an unset reference is
NULL, so the check flips to the other side: **`-v name=value` for a
name the text never references is a usage error**, naming the names the
text does reference. Same for the MCP tools' `vars`. That catches
`-v dst=out.mp4` at least as well as the old rule did.

A typo *in the text* (`:crff`) is now silent — it becomes NULL and the
option drops. That is the price of the feature; for a packaged program
it is one line with a handful of names. If it ever needs catching, the
`-- variables:` header can become a declaration that text references
are checked against, as opt-in strictness. Not in scope.

### 1.5 Tags: the one place absence is not "leave alone"

Writing a tag column with NULL already means **clear the tag**. So
`:'title' AS title` with `:title` unset clears the title rather than
keeping it. That is the existing, correct semantics of NULL in a tag
write, and it is not special-cased: a program that means "keep unless
told otherwise" writes `COALESCE(:'title', f.title) AS title`, which is
ordinary SQL. `retitle` is the demo that needs it.

---

## Part 2 — Defaults in signatures

### 2.1 `DEFAULT` in `CREATE FUNCTION`

    CREATE FUNCTION resize(source text, width number DEFAULT 1280)
    RETURNS TABLE(video video_stream, audio audio_stream) AS $$ ... $$
    LANGUAGE sql;

An omitted trailing argument takes its default — Postgres semantics,
already valid Postgres syntax. The signature parser gains `DEFAULT
<literal>`; arity checks allow fewer arguments than parameters when the
missing ones all have defaults.

### 2.2 The one deviation, named

**A NULL argument to a parameter that declares a DEFAULT takes the
default.** In Postgres NULL is a value and only omission triggers a
default. Here, a wrapper that always passes `:crf` has no way to omit,
and the whole point of 1.1 is that unset means absent. So absence falls
through to the declared default, the same way it falls through to
ffmpeg's.

The cost: a caller cannot pass NULL explicitly to a defaulted
parameter. The escape is to not declare a default. Documented in
`docs/dialect.md` as a deviation, in those words. Without a DEFAULT,
NULL passes through as NULL — which then drops wherever the body uses
it as an option, by Part 1.

### 2.3 Why not COALESCE in the body

It works today and stays valid, but a default inside `COALESCE` is
invisible: `sqlmpeg list` cannot show it, the package page cannot show
it, a caller cannot know what omitting does without reading the body.
A default in the signature is the documentation. Both remain legal;
signatures are the recommended spelling and the one the tooling reads.

---

## Part 3 — The name is the path

### 3.1 `<namespace>/<package>`

A package is named `imbcmdth/clip`, and that IS what a call writes:
`imbcmdth.clip`. The manifest's separate `namespace` field goes, and
with it every rule that reconciled two names for one thing:

- `install --as` is deleted — it resolved namespace collisions, and
  `broadcast/tracks` cannot collide with itself.
- lockfile entries drop `namespace` and are keyed by name; "two
  packages claim one namespace" becomes "two entries name one package";
  `LOCK_FORMAT_VERSION` becomes 2.
- the entry-versus-package namespace agreement check goes; `name` and
  `version` agreement stays.

`sqlmpeg`, `ffmpeg` and `wasm` are refused as the first segment — in
`read_manifest`, and again in the registry's own validation so nothing
under them can be submitted.

### 3.2 The manifest

```json
{ "name": "imbcmdth/audio",
  "version": "1.0.0",
  "description": "Volume, loudness, ducking",

  "lib":  "src/audio.sql",
  "libs": { "quieter": "src/audio.sql",
            "louder":  "src/audio.sql",
            "duck":    "src/duck.sql" },

  "bin":  "queries/volume.sql",
  "bins": { "loudnorm": "queries/loudnorm-all.sql",
            "duck":     "queries/duck.sql" },

  "dependencies": { "tracks": "broadcast/tracks@^1.2.0" } }
```

Singular is the default, plural is the map, on both halves:

| key | reached as |
| --- | --- |
| `lib` | `imbcmdth.audio(...)` — the package path is a call; the export is named for the package |
| `libs` | `imbcmdth.audio.quieter(...)` |
| `bin` | `sqlmpeg run imbcmdth.audio` |
| `bins` | `sqlmpeg run imbcmdth.audio.duck` |

**`libs` is keyed by exported function name**, value the file defining
it. Several keys may name one file; a file may define more than it
exports, and the rest are private to the package (the existing
per-package scope is already exactly that rule). The manifest is the
complete index of what a package exports — Postgres has no export
statement, so the explicitness lives here — and the key-is-a-name shape
makes "one name, one definition" structural rather than checked.
Validation: the named function must exist in the named file.

All four keys optional. A manifest with none is the consumer project,
which holds `dependencies`. `exports` (plan 101 A) is gone; nothing
shipped with it.

### 3.3 Aliases: the layer between source text and registry identity

`"dependencies": { "tracks": "broadcast/tracks@^1.2.0" }` binds the
alias `tracks` in this project, Cargo's shape. `tracks.pick(...)` is
valid because the manifest bound it; `broadcast.tracks.pick(...)` is
always valid. The alias is what lets a published package be renamed,
forked or vendored without touching a query, and is the relief valve
for a long publisher name written five times in one SELECT.

Rules:

- an alias must be a plain identifier and may not equal an installed
  namespace or a reserved one — that disjointness is what makes a
  two-segment call `a.b(...)` decidable (alias.member, else
  namespace.package).
- the default `lib` is not reachable through an alias: `tracks(...)`
  would collide with filters and the script's own functions, and
  one-segment names stay the script's own. Reach a default by its full
  path.
- a project's own definitions stay **bare**. The path is for reaching
  into a package you installed.

The dependency value keeps the name and the version range together
because the alias is the key; the lockfile pins the exact version.

### 3.4 Three segments

`ffmpeg.<filter>` and `sqlmpeg.<macro>` stay two-part and reserved. A
package call is `<namespace>.<package>.<member>(...)`, or
`<namespace>.<package>(...)` for the default, or `<alias>.<member>(...)`.
The parse is decidable because a call has parentheses and a path read
(`t.tags.language`) does not; that distinction exists and carries one
more segment.

A package shipping both `lib` and `bin` makes `imbcmdth.clip` mean the
default function in SQL and the default program on the command line.
The two contexts never overlap, so it is accepted and stated once.

---

## Part 4 — The demos, generalized

Not functions. A six-line query that someone would copy rather than
depend on is a program, and making it a function plus a wrapper buys
nothing. Functions are for packages whose point is to be called from
other queries; none of the demos is that. (This also means the CTE
fan-out gap in `docs/known_gaps.md` is not on this path.)

The sweep: every program gains the optional variables Part 1 makes
free, and anything that is another program with different knobs
exposed is culled into it.

| program | change |
| --- | --- |
| `encode` ← `transcode`, `compress` | one program. All tracks by default (`f.video`, `f.audio`, subtitles carried). `vcodec`, `acodec`, `crf`, `video_bitrate`, `audio_bitrate`, `preset` all optional — unset drops the COPY option, ffmpeg decides. |
| `abr-ladder` | keep; a different shape (one decode, several outputs), not a knob set. Rung sizes optional. |
| `clip` | `start` and `end` each optional — open-ended trims already exist. |
| `resize` | `height` optional, defaults to `-2`. |
| `rotate` | `dir` optional. |
| `watermark` | `x`, `y`, `scale` optional. |
| `loudnorm-all` | `i`, `tp`, `lra` optional. |
| `volume` | `factor` required — there is nothing to do without it. |
| `fade` | `duration` optional. |
| `speed` | `factor` required. |
| `gif` | `fps`, `width` optional. |
| `retitle` | `title`, `artist` optional via `COALESCE(:'title', f.title)` — see 1.5. |
| `thumbnail`, `extract-frames` | stay separate: one image vs a sequence is a different query shape, not a default. |
| the rest | source and destination required, nothing else to generalize. |

One genuine merge falls out. The rest of the redundancy was in
hard-coded knobs, and absence fixes that without merging anything.

### 4.1 Grouping

After the sweep, seven packages under `imbcmdth`, default `bin` first:

| package | `bin` | `bins` |
| --- | --- | --- |
| `encode` | encode | abr-ladder |
| `edit` | clip | ad-insert, concat-fill, crossfade, fade, speed, split-chapters |
| `picture` | resize | crop, rotate, pip, side-by-side, watermark, blur-region |
| `audio` | volume | loudnorm-all, duck, replace-audio, strip-audio, split-channels |
| `tracks` | tracks-to-csv | extract-audio, extract-languages, remote-tracks, retitle |
| `subtitles` | burn-subtitles | mux-subtitles, extract-subtitles |
| `images` | thumbnail | extract-frames, gif |

Judgment calls: `retitle` is container-level and sits in `tracks` as
the loosest fit; `split-chapters` is in `edit` because it cuts on time.
Sign-off pending on this table.

### 4.2 What `list` and the site show

Required versus optional is derived, so `sqlmpeg list` derives it:
compile each program with nothing set and collect the required
rejections. Accurate by construction, no declaration. Defaults for
functions come from their signatures. The `-- variables:` header keeps
descriptions and the `-- example:` line, which is also what the
registry build compiles each program with.

---

## Part 5 — The registry

Plan 102 stands (main is source, `gh-pages` accumulates). Changes:

- the seed is the seven packages in 4.1, not `sqlmpeg/queries`;
- the reserved first segments are refused at submission;
- `owners.json` is keyed by namespace — `{"imbcmdth": ["imbcmdth"]}`;
- the build reads `libs` for the export list and still parses
  signatures for parameter types and defaults; package pages show each
  program's required and optional variables the way `list` derives
  them.

---

## Order

Each wave lands green, with its recipes in `docs/examples.md` first.

| wave | what | depends on |
| --- | --- | --- |
| A | 1.1–1.4: unset → NULL, the drop at every binding site, required errors naming the variable, the reversed `-v` check, the curated list | — |
| B | Part 2: `DEFAULT` in signatures, the NULL-takes-default deviation, `list` showing defaults | A |
| C | 3.1–3.2: the manifest, the lockfile v2, `--as` deleted, reserved first segment | — |
| D | 3.3–3.4: three-segment and alias resolution | C |
| E | Part 4: the sweep, the cull, the grouping; `list` deriving required-ness | A, B |
| F | Part 5: the registry seed and build | C, D, E |

A and C are independent and can run in parallel. Everything stays on
the `packages` branch; `main` remains at 0.26.0 until the whole set is
in.
