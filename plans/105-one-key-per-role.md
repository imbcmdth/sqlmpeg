# 105 — One key per role, string or map

Supersedes the manifest surface of plan 104 §3.2. Maintainer,
2026-08-22, after `imbcmdth.audio.volume` turned out not to resolve.

## The bug this fixes

`imbcmdth/audio` declares `bin: "queries/volume.sql"` and a `bins` map
beside it. The singular one is registered under the PACKAGE segment, so
a file called `volume.sql` is named `audio` — `imbcmdth.audio.volume`
finds nothing and the registry lists the program as "audio" next to its
three siblings under their real names.

That is not a rendering fault. The default has no name of its own in the
model, and there is no good one to give it: naming it for the package
lies about what it is, and naming it for its file makes
`imbcmdth.audio` and `imbcmdth.audio.volume` two spellings of one call.

## npm's shape

npm has ONE `bin` key that is either a string or a map. A string means
the command takes the package's own name (scope stripped: `@foo/bar`
installs `bar`); a map means the commands are its keys. Both at once is
unrepresentable, so npm never has to answer which is the default.

Take that shape.

## The manifest

```json
{ "name": "imbcmdth/audio",
  "version": "1.0.0",
  "description": "Volume, loudness and ducking.",
  "bin": { "volume": "queries/volume.sql",
           "loudnorm-all": "queries/loudnorm-all.sql" },
  "lib": "src/audio.sql",
  "dependencies": { "tracks": "broadcast/tracks@^1.2.0" } }
```

- **`bin`** — a string, or a map of program name to file.
- **`lib`** — a string, or a map of exported function name to file.
- `bins` and `libs` are gone. Their presence is an unknown-key
  rejection whose hint names the singular.

A **string** means one member, named for the package segment:

    "bin": "queries/clip.sql"   in imbcmdth/clip  ->  imbcmdth.clip
    "lib": "src/deband.sql"     in acme/deband    ->  acme.deband(v)

A **map** means named members, and there is no root-callable one:

    "bin": {"volume": ...}      ->  imbcmdth.audio.volume
    "lib": {"quieter": ...}     ->  imbcmdth.audio.quieter(...)

Several keys may name one file. A file may define more than it exports;
the rest are private to the package. Both keys stay optional, and a
manifest with neither is the consumer project.

Validation: a map's keys are program/function names as today; a
string's implied name is the package segment, and the named function
must exist in the named file exactly as a map entry's must.

## What this makes impossible

- A package cannot have both a root member and named ones, so nothing
  is reachable two ways and nothing needs a "(default)" marker in
  `sqlmpeg list` or on a package page.
- The name a member is registered under is always true: for a map it is
  the author's key, for a string it is the package's own name, which
  only reads as a lie if the package is misnamed.

## What it costs, stated plainly

**Growing out of the string form is a breaking rename.** A package that
ships `"bin": "queries/clip.sql"` and later adds a second program moves
to a map, and its command changes from `imbcmdth.clip` to
`imbcmdth.clip.clip`. npm does not have this problem — its bin names are
flat commands, so keying the map with the old name preserves it — but
ours are paths, and one segment versus two is structural. A major
version covers it, and a package ceasing to be one tool is a major
change, but it is a guaranteed break rather than an avoidable one.

**None of the eight seed packages uses the string form.** All have two
or more programs. So it ships exercised by tests and by future authors
only, the same gap `lib` already has.

**The two-segment ambiguity stays.** `imbcmdth.clip` (namespace.package)
and `tracks.pick` (alias.member) are both two segments, so the rule that
an alias may not equal an installed namespace remains load-bearing.

## Bare names already work

`sqlmpeg run volume` resolves a program name across installed packages
and rejects with the qualified candidates when two answer. That is
npx's ergonomics already, and it means the qualified path is the
DISAMBIGUATOR rather than the thing anyone types. The string form of
`bin` therefore buys little; the string form of `lib` buys the
difference between `acme.deband(v)` and `acme.deband.deband(v)`, which
is where the case for it actually lives.

## Work

1. **`project.py`** — `bin` and `lib` accept a string or a map; `bins`
   and `libs` become unknown keys with a hint. `Package.export`/
   `.program` keep their `None` case for the string form and reject a
   member lookup that the map does not hold.
2. **`functions.py`** — unchanged in shape: a two-segment
   `namespace.package(...)` still reaches a string `lib`, a
   three-segment call a map entry. The rejection for a two-segment call
   on a map-only package should say the package names its exports and
   list them.
3. **`cli.py`** — `list` shows the real name in every case and no
   marker; the program lookup follows `Package.program`.
4. **The eight manifests** — `bins` becomes `bin`, dropping the
   separate singular that named a program after its package.
5. **The registry** — `build.py` reads the new shape, and the package
   page shows each member's real name.
6. **Docs** — `docs/dialect.md`'s manifest section, and the prompt.

Then 0.28.0, and the registry's pin with it. Worth doing now: the
registry holds eight packages and has one consumer.
