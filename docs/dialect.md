# The sqlmpeg dialect

Postgres syntax, compiled - never executed by a database. Guardrail:
every query must parse as valid Postgres; sqlmpeg then accepts the
subset below and rejects the rest with typed, line-anchored errors
([errors.md](errors.md)). This page is the language's formal surface:
what exists, and - just as binding - what does not.

## Statements

A query is ONE statement, or a script:

```
query   := select | copy
script  := (function ;)* (CREATE VIEW name AS select ;)* (copy ;)* copy?
function := CREATE FUNCTION name(param type, ...) RETURNS rtype
            AS $$ select $$ LANGUAGE sql
rtype   := text | number | boolean | <kind>_stream | chapter | cue
         | attachment | any of those with [] | TABLE(col type, ...)
select  := [WITH cte (, cte)*] SELECT columns FROM from [WHERE pred]
           [GROUP BY exprs] [ORDER BY exprs]
           (UNION ALL select)*
copy    := COPY ( select ) TO dest [WITH ( option value (, ...)* )]
cte     := name AS ( select )  |  name (col, ...) AS ( VALUES ... )
dest    := 'path' | STDOUT | ( value-expression )
```

- A bare `select` is a **table query**: the result prints (psql-style
  table, or CSV via `COPY ... TO STDOUT WITH (format 'csv')`), and
  ffmpeg never runs.
- A `copy` with a media destination compiles to the ffmpeg command(s).
- A script's views compile into ONE ffmpeg invocation, one output per
  COPY. A view nothing reads is rejected.
- A **function** is a reusable expression, expanded at compile time -
  it is the query you could have typed by hand. It must be defined
  before it is used and before the first `COPY`, every definition must
  be called, and a value-returning one is legal anywhere a value of its
  type is while a `TABLE`-returning one is a `FROM` row source only.
  Recipes [67-68](examples.md#67-write-a-function-and-reuse-it).
- Trailing `;` allowed; `--` and `/* */` comments allowed. Unquoted
  identifiers fold to lowercase. View, CTE, and alias names share one
  flat namespace across the whole script.

## Projects and packages

A directory holding a `sqlmpeg.json` is a project, and the project is a
package. A package is named `<namespace>/<package>`, and that name is
the path a call writes: `imbcmdth/audio` is called as `imbcmdth.audio`.

```json
{ "name": "imbcmdth/audio", "version": "1.0.0",
  "description": "Volume, loudness, ducking",
  "lib":  "src/audio.sql",
  "libs": { "quieter": "src/audio.sql", "duck": "src/duck.sql" },
  "bin":  "queries/volume.sql",
  "bins": { "loudnorm": "queries/loudnorm-all.sql" },
  "dependencies": { "tracks": "broadcast/tracks@^1.2.0" } }
```

`name` and `version` are required; the rest is optional. Each half of
the name is a lowercase plain identifier, and the first half - the
namespace - may not be `ffmpeg`, `sqlmpeg` or `wasm`. There is no
separate `namespace` key: the name carries it.

Singular is the default, plural is the map, on both halves.
`lib`/`libs` are the exports: each `libs` key is an exported function
name, its value the file defining it, and `lib`'s export is named for
the package segment (`audio` in `imbcmdth/audio`). Several keys may
name one file; a file may define more than the manifest exports, and
the rest are private to the package. Each exported name must be
defined in the file named for it. `bin`/`bins` are the programs:
`bins` maps a command word (`[a-z][a-z0-9_-]*`) to one query file, and
`bin` is the default program, named for the package segment. Every
file value is one path relative to the manifest, stays under it, is
not a pattern, and must exist. A manifest declaring none of the four
is a consumer project that holds dependencies.

The two halves are read by role. A lib file holds `CREATE FUNCTION`
definitions and nothing else, and a definition it exports but the query
never calls is fine - it is a library; an uncalled definition in the
query's own text is still rejected. A program's file is a whole query,
like any script, and the definition reader never opens it.

`dependencies` keys are aliases: each is a plain identifier this
project's queries may use for the package the value names. The value
keeps the installed package's name and the version range together,
`"<namespace>/<package>@<range>"`; the range is recorded and shown,
never solved. An alias may not be reserved, and may not equal the
namespace of any installed package.

A query calls into an installed package one of three ways:

- **Three segments**, `<namespace>.<package>.<member>(...)` -
  `imbcmdth.audio.quieter(f.audio[1], 0.5)` - always valid, reaching any
  export whatever a project's own `dependencies` say.
- **Two segments**, `<namespace>.<package>(...)` - `imbcmdth.audio(...)`
  - the package's DEFAULT export, the one `lib` names.
- **Two segments through an alias**, `<alias>.<member>(...)` -
  `tracks.pick(...)` when `dependencies` binds `tracks` to a package.
  The default export is not reachable through an alias - write the
  full three-segment path for that.

Aliases and namespaces are disjoint by construction (`install` refuses
an alias equal to an installed namespace), so a two-segment call is
decidable: sqlmpeg tries it as an alias first, then as a
namespace/package pair. `ffmpeg.<filter>` and `sqlmpeg.<macro>` stay
two-part and reserved, and take precedence over all of this - they are
never read as a package call.

Called as a value (`imbcmdth.audio.quieter(...)`) or as a row source
(`FROM imbcmdth.audio.pick('a.mka') t`), the call is expanded exactly
as a definition written into the query would be - same hygiene, same
arity and type checks, same command out. Nothing is prepended to the
script, and a package's names never enter the script's flat namespace.
A project's own definitions stay bare - `normalize(...)` inside the
project that defines it; unqualified names are never a package lookup.

`sqlmpeg list` prints what the project at the working directory and its
dependencies provide: the packages with their layer, the exports with
their signatures and files (the list is the manifest's; only parameter
types are read from the files), the programs with the variables each
declares (read from its `-- variables:` header), and the aliases.
`--json` for scripting.

The project is found by walking up from the query file's directory, or
from the working directory for a query typed on the command line; there
is no flag. Outside a project nothing changes: a namespaced call is
rejected as it always was. Library callers pass
`compile_sql(text, packages=sqlmpeg.discover(path))` rather than
relying on a working directory, and the MCP tools take the same path as
a `project` argument.

### Starting one

`sqlmpeg init` writes `sqlmpeg.json`, an empty `sqlmpeg.lock` and a
starter program into the working directory. The package segment is the
directory's name, folded to an identifier, unless `--name` says
otherwise; the namespace is `--namespace`'s, or derived from the git
remote `origin`'s owner, or required - `init` says which it used.
`--name <namespace>/<package>` gives the whole name at once. A name
nothing usable comes out of is rejected rather than guessed at. It
overwrites none of the three files, and what it writes reads back
through the same validation every other command applies.

The starter is a program, `queries/resize.sql` declared in `bins`, not
an export: a lib must name a file defining its export, so a fresh
directory has nothing to declare one with.

### Running a program

`sqlmpeg run split-chapters -v source=film.mkv -v dest=out.mkv` runs a
program a package ships. `compile`, `explain` and `validate` take a
program name in the same position, so a program can be inspected as well
as run.

Which the positional is, in order:

1. Text beginning with `SELECT`, `COPY`, `CREATE` or `WITH` (past
   leading whitespace and comments) is SQL, always.
2. Otherwise, a name a discovered package ships a program under is that
   program: its file's text is the query.
3. Otherwise it is SQL, and fails as any other query text would.

A manifest declaring a program named for one of those four words is
rejected where it is written: rule 1 would never let it be reached.
A program name may also be qualified: `<namespace>.<package>.<name>`
names one package's `bins` entry, `<namespace>.<package>` its default
`bin`. Either says which package when a bare name matches programs in
more than one; a bare name that does is rejected naming each
`<namespace>.<package>.<name>` it could mean. Variables still come
from `-v name=value`; an unset one substitutes to `NULL` (see
[Variables](#variables)), and a rejection at
its point of use names what the program's `-- variables:` header
declares.

### Installed packages

`sqlmpeg.lock`, beside the manifest, records what the project
installed. It is machine-owned: installing writes it, nothing else
should. Each entry is one of two kinds.

```json
{ "format_version": 2,
  "reproducible": false,
  "not_reproducible_because": "a package is linked to a working directory, so its files are not pinned here",
  "packages": [
    { "kind": "registry", "name": "broadcast/tracks", "version": "1.2.0",
      "sha256": "<64 hex>", "store": "v1/ab/<64 hex>" },
    { "kind": "link", "path": "../my-lib" }
  ] }
```

A **registry** entry is keyed by the package's name and pins a version
and the sha256 of the ARCHIVE the package travels as - one gzipped
tar, built so the same content always produces the same bytes.
`install` hashes the bytes it downloaded before opening them: a
download that does not match the pin is discarded unopened and nothing
is written. What matches is extracted into the store under
`~/.cache/sqlmpeg/packages/`, and the extractor takes regular files and
directories under the package root and nothing else - no absolute
paths, no `..`, no links, no devices, and a member count and
uncompressed size cap. Reading a stored package hashes nothing; content
that is missing from the store is a rejection naming the package, never
a fall back to what is there.

A **link** entry names a directory and nothing else - the package's
name comes from the manifest there, so renaming the package needs no
re-link. Its `sqlmpeg.json` is read like any other manifest, so an
edit lands in the next compile - which is the point, and which no
digest could survive. A lockfile holding a link is therefore not
reproducible, and says so in its own text. Two entries naming one
package are rejected. The lockfile carries no aliases - they are the
manifest's.

A package resolves through three layers, the first claim on its name
winning: the project's own manifest, then its lockfile, then the
machine-wide lockfile a global install writes. Two of those are worth
saying out loud, so a compile reports them without refusing: resolving
inside a project but landing on the machine-wide layer, and compiling
against a link. Each is reported once per package - as a `warning:`
line on stderr from the CLI, in the `warnings` array of an MCP tool
result, and through `compile_sql`'s optional `on_warning` callback for
a library caller.

### The registry

Packages come from a static site: `index.json` is the catalogue,
`p/<owner>/<name>.json` is one package's detail (every published
version, and per version the archive's sha256 and size), and
`archives/<sha256>` is the archive. There is no service and no API
beyond those files.

The base URL is one setting, `https://want.video`,
overridden by the `SQLMPEG_REGISTRY` environment variable. It may also
be a `file://` URL or a plain directory path holding those same files,
which is what a private registry behind any static host is.

`index.json` is cached under `~/.cache/sqlmpeg/`. The fetch is tried
every time - a stale catalogue would install an older version than the
one published - and the cache answers only when the registry cannot be
reached at all, saying so on stderr (`search`, `install`) or in the
result's `cached` and `unreachable` fields (the MCP tools). The cache is
only an optimization: one that cannot be read or written costs a fetch
and nothing else.

### Searching

`sqlmpeg search tracks` prints the catalogue, narrowed to what matches
the term: a case-insensitive substring of a package's name, its
description or the names of the functions it exports. No term lists
everything, a term matching nothing is an empty table and exit 0, and
`--json` emits the same results for scripting.

### Installing

`sqlmpeg install broadcast/tracks` resolves the package in the
catalogue, fetches the archive its detail file names, verifies the bytes
against the sha256 recorded there before opening them, writes the
content into the store, and then pins it - a registry entry in the
lockfile and a dependency in `sqlmpeg.json`. Nothing is recorded before
the content is in the store, so an install that fails leaves the project
pinning only what it had.

`install <pkg>@<version>` takes that version; without one, the highest
published version is taken and written exact. Exact pins only: a
manifest `dependencies` range is recorded and shown, never solved.

A version already in the store is not downloaded again - the same
content, installed into a second project or globally, is stored once.

The dependency is recorded in the manifest under the package segment
as its alias - `install broadcast/tracks` writes
`"tracks": "broadcast/tracks@1.2.0"` - and `--alias <name>` chooses
another. A default that collides with an existing alias or with an
installed package's namespace is a rejection naming `--alias`. A
global install (`-g`) has no manifest, so nothing is aliased.
Installing a package already pinned replaces its entry and says what
it replaced.

`-g` and the project rule are `link`'s, below.

### Linking

`sqlmpeg link ../my-lib` writes a link entry naming that directory; the
package's name comes from its manifest. `sqlmpeg unlink <name>` (the
package's name, or the directory for a link whose manifest is gone)
removes it and rewrites the file. Linking a package something else
already pins replaces that entry and says what it replaced.

`-g` writes the machine-wide lockfile instead of the project's. Without
it, no lockfile at or above the working directory is a usage error
(exit 2) naming both ways forward - `install -g` / `link -g`, or `init`
first. No command ever creates a lockfile as a side effect.

Every write to either file replaces it in one step and pins LF endings,
and the lockfile is written in insertion order with no timestamp, so
writing the same set of packages twice produces the same bytes.

### Publishing

`sqlmpeg publish` is not open yet: it exits nonzero saying so.
Submissions are a pull request to the registry repository.

## FROM items

Every FROM item is a compile-time table; the column model per shape is
[rows.md](rows.md), the type vocabulary [types.md](types.md).

| form | rows | notes |
| --- | --- | --- |
| `input('path', name => value, ...) alias` | 1 | alias mandatory; path is a literal, never computed; trailing named options are ffmpeg's per-input flags |
| `ffmpeg.<source>(name => value, ...) alias` | 1 | generated stream (testsrc2, sine, color, anullsrc, ...), no `-i`; options named-only |
| `unnest(alias.<array>) alias` | one per element | the four stream arrays, or `chapters` / `cues` / `attachments`, of an input declared earlier in the same FROM |
| `cte_or_view_name [alias]` | its body's rows | a multi-row body is a multi-row source; a `VALUES` list is one too |
| `function_name(args) alias` | its body's rows | a table-returning function, expanded at compile time |

Comma between items is a cross join with real multiplicity.
`JOIN ... ON` exists ONLY between two `unnest` tables (chapter rows included): `INNER`,
`LEFT [OUTER]`, `FULL [OUTER]`, each with its own `ON`. An outer
join's gap side has NULL streams; fill with `COALESCE` and a generated
source ([rows.md](rows.md#joins)).

## The SELECT list

Each column is one of:

- **A stream**: `f.video[1]`, a bare array splat (`f.audio` = every
  track), a bare track-row alias (`a`, the row IS the stream), `*`, or
  a filter call over any of these. In a media COPY, column order is
  `-map` order.
- **A filter call**: any filter of the installed ffmpeg, bare or
  `ffmpeg.<name>`, plus the `sqlmpeg.<name>` macros - streams first,
  then options positionally in the filter's own order, then
  `name => value` ([filters.md](filters.md)). Bare arrays broadcast;
  two arrays in one call zip elementwise.
- **A tag column**: an ALIASED non-stream expression. Over track rows
  it tags the row's streams; over input rows only, the container;
  `NULL` clears ([rows.md](rows.md#tags)). Aliased `disposition`, it
  sets the row's flags instead of a tag.
- **`array_agg(<per-row stream expression>)`**: gathers rows in row
  order; must be a whole column ([rows.md](rows.md#combining-rows)).
- **A metadata column** (table queries): any row column prints as
  data.
- **`*` / `<alias>.*`**: over an input, its array columns - the four
  stream arrays in `video`, `audio`, `subtitle`, `data` order in a
  media query, every array column including `chapters` in a table
  one. Over rows, the record's scalar fields (`tags` and `disposition`
  excluded, read them by name), which a table query prints and a media
  query rejects. Over a CTE, the columns its body named.

Subscripts are positive integer literals, 1-based.
`(f.audio[1]).codec`-style accessors reach row columns without
unnest; in WHERE they are assertions. A tag is read by path,
`f.tags.title` / `t.tags.language`, one key at a time, and so is a
disposition flag, `t.disposition.forced`, over a closed key set.

## Values and predicates

One compile-time value grammar serves predicates, tag columns, trim
bounds, computed filter arguments, and fan-out destinations:

```
value := literal | NULL | row-column | input-scalar
       | value || value            -- text only
       | value (+|-|*|/) value     -- Postgres typing; int/int truncates
       | value ::text | CAST(value AS text)
       | CASE WHEN pred THEN value [ELSE value] END
       | COALESCE(value, ...)      -- first argument a value, never a stream;
                                   -- arguments agree on one type

       | :'var' | :"var" | :var    -- CLI -v substitution, psql's forms
       | ARRAY[ROW(...)::chapter, ...]   -- record arrays: chapter,
       | ARRAY[ROW(...)::cue, ...]       -- cue, attachment
```

Predicates: `= != < <= > >= BETWEEN IS [NOT] NULL [NOT] IN (literals)`,
combined with `AND OR NOT`. A boolean value is a predicate on its own
(`WHERE t.disposition.default`). All decided at compile time against probed
metadata - never a runtime ffmpeg predicate. NULL follows SQL:
`=`/`!=` both fail against it.

`WHERE alias.t BETWEEN a AND b` (either bound alone also works) is the
trim window - it compiles to seeks, not filters
([trimming.md](trimming.md)). Bounds take the value grammar, including
`f.duration` and chapter columns.

## Grouping and combining

A single destination takes exactly ONE row; a multi-row relation
combines only when written (`array_agg` + `GROUP BY`) or fans out
(`TO (expression)`, one file per row/group). The four rules, the
resolved-row-count principle, and grouped fan-out are in
[rows.md](rows.md#combining-rows). GROUP BY and ORDER BY are legal
only over row-table queries; Postgres's grouping rule is enforced.

## Destinations and options

`TO 'path'` writes one file; `TO STDOUT WITH (format 'csv')` prints;
`TO (value-expression over row columns)` writes one file per row or
group. Sink options (`WITH (...)`) cover codecs, quality, bitrate
control, metadata copying, two-pass - the full table is
generated into the prompt (`sqlmpeg prompt`) and validated per option
with typed errors.

## Variables

`:'name'` (string literal), `:"name"` (identifier) and bare `:name`
(raw text) are psql's reference forms, filled by `-v name=value`. An
UNSET reference substitutes to the bare keyword `NULL` — never `''`,
never the literal text, never an error. That is a deviation from psql,
which leaves the text alone.

**NULL is absence.** A NULL — an unset variable's, or written literally
— in an option position means the option is not written and the thing
being configured supplies its own default. This holds at every binding
site: filter options (positional or named, `enable` included), source
filter options, `input()` options, and `COPY ... WITH (...)` options. A
dropped positional still occupies its slot: `scale(v, :w, :h)` with
`:w` unset writes only `height`, nothing shifts, and a named repeat of
the dropped option still collides.

What is required is derived from use, and a NULL there is a
compile-time rejection naming the variable when the NULL came from one
(`':source' was not set`):

- `input(NULL)` — a path is required.
- `COPY ... TO NULL`, and a `TO (expression)` that evaluates to NULL
  for a row — a destination is required.
- a NULL in a stream position of any call.
- a NULL for an option on the required list below — where omitting the
  option entirely is the same rejection, since ffmpeg's own init()
  would refuse it at run time.

The required list is hand-kept (ffmpeg has no required-option
metadata):

| filter | required |
| --- | --- |
| `subtitles` | `filename` |
| `lut3d` | `file` |
| `frei0r` | `filter_name` |
| `ladspa` | `file`, `plugin` |
| `movie`, `amovie` | `filename` |
| `drawtext` | `text` or `textfile` (either satisfies) |
| `xfade` | `expr`, only when `transition` is `custom` |

Everything else falls through to ffmpeg's own error at run time.

One place absence is not "leave alone": writing a tag column with NULL
clears the tag, so `:'title' AS title` unset clears the title. A
program that means "keep unless told otherwise" writes
`COALESCE(:'title', f.tags.title) AS title` — ordinary SQL.

The check on `-v` points the other way: since an unset reference is
legal, `-v name=value` for a name the text never references is the
usage error (exit 2), naming the names the text does reference. The
`-- variables:` header remains documentation, not a declaration.

## Not in the dialect

Every one of these is a typed rejection, never a silent reinterpretation:

- **Statements**: anything but SELECT / COPY / CREATE VIEW; more than
  one bare statement; INSERT/UPDATE/DELETE/DDL.
- **Subqueries** anywhere except CTE and view bodies - `IN (SELECT
  ...)`, `EXISTS`, derived tables in FROM.
- **Joins**: `RIGHT [OUTER] JOIN`, `CROSS JOIN` (spell it with a
  comma), `NATURAL JOIN`, `USING`, and any `JOIN ... ON` not between
  two unnest tables.
- **No streaming equivalent**: `HAVING`, `LIMIT`, `OFFSET`,
  `DISTINCT`, `UNION` without `ALL`, window functions, `QUALIFY`,
  aggregates other than `array_agg` (`count`, `sum`, ...), `ORDER BY`
  inside `array_agg`.
- **Aggregation context**: GROUP BY / array_agg inside a CTE body, a
  view body, or a UNION ALL branch; a per-stream tag column in a
  grouped query (tag inside a CTE, aggregate outside).
- **Values**: casts other than to text; computed input paths;
  computed subscripts; `0` or negative subscripts; `||` over numbers
  without `::text`; division by a known zero.
- **Multi-row into one path** (`ROW_COUNT_MISMATCH`): gather or fan
  out, explicitly.
- **Filters**: variable-pad (`split`, `concat` - both are what UNION
  ALL and the compiler's own split pass are for), multi-output
  (`scale2ref`, `feedback`), sinks, multi-output sources
  (`movie`, `avsynctest`); options typed `binary` or `dictionary`;
  runtime filter commands (`sendcmd`, `zmq`). The N-input escape:
  `amix`, `hstack`, `vstack`, `amerge`, `ffmpeg.join`, `interleave`,
  `ainterleave` take any stream count.
- **Functions**: `OR REPLACE`, `IF NOT EXISTS`, a schema-qualified
  name, any property but `RETURNS`/`LANGUAGE`, a language other than
  `sql`, parameter defaults or `OUT`/`VARIADIC`, overloading, recursion,
  a body with its own `WITH` or `GROUP BY`/`ORDER BY`/`LIMIT`, a body
  referencing anything but its parameters and its own `FROM` aliases, a
  definition in the query's own text that nothing calls, and a
  `TABLE`-returning call in the `SELECT` list.
- **Packages**: a namespace no package claims; a namespace with no
  package by that name; a call with no default export naming the
  package's `libs` instead; a member the package does not export; a
  manifest that is not one JSON object with `name` and `version`; a
  name that is not `<namespace>/<package>` in plain
  identifiers, or whose namespace is reserved; a `lib`, `libs`, `bin`
  or `bins` value naming a pattern, a missing file, or a path outside
  the project; an exported name not defined in the file named for it;
  a `libs` or `bins` key equal to the package segment; one name defined
  twice across a package's lib files; a lib file holding anything but
  `CREATE FUNCTION`; a program name that is not a command word or is
  declared twice; a dependency alias that is not a plain identifier, is
  reserved, or equals an installed package's namespace; a dependency
  value that is not `<namespace>/<package>@<range>`.
- **Lockfiles**: a lockfile that is not one JSON object with its three
  required keys, or is written in another format version; an entry of
  no known kind, missing a key, or holding an unknown one; two entries
  naming one package; a lockfile claiming to be reproducible while
  linking a directory; a linked directory with no manifest; stored
  content that is missing or was written by another store layout; a
  downloaded archive that does not hash to what the entry pins, or that
  holds a member outside the package root, a link, a device, or more
  members or bytes than the caps allow; an entry the package it points
  at disagrees with.
- **The registry**: a registry that cannot be read, with nothing cached
  to answer from; a catalogue or detail file that is not JSON, is not an
  object, is written in another format version, or holds a malformed
  field or a name that is not `<namespace>/<package>`; a package or a
  version the catalogue does not publish; an archive that is not the
  size or the digest its detail file records; an archive whose package
  says it is a different name or version than what was published;
  `--alias` naming something that is not an alias, one that is
  reserved, or one already taken.
- **Identifiers**: double-quoted identifiers (except tag-key aliases);
  the reserved names `ffmpeg` and `sqlmpeg` as aliases.
- **Written records**: a chapter whose span ends at or before it starts,
  or whose chapters overlap or run out of order (cues may overlap, but
  must still be ascending); an attachment with no `path`; reading
  `a.path` back.
- **Timeline**: `WHERE t` on generated sources (give the source its
  own `duration`); selecting chapter rows as streams; a bare
  `f.chapters` in a media query, or subscripting it (`unnest` it); a data/subtitle
  track through any filter (passthrough only).
- **Fields**: reading one off a filter output (`scale(v, 640, -2).width`,
  `volume(a, 0.2).tags.language`), since nothing probed it; setting a
  read-only one (`'h264' AS codec`, `3 AS index`, `12 AS duration`),
  since it is a probed fact and not an assertion; `SELECT *` over rows
  in a media query, since a star expands fields and a SELECT column is
  a stream.
- **Written chapters**: a chapter that ends at or before it starts, rows
  out of ascending order, or two chapters covering the same second.

What a specific rejection looks like, with captured JSON for every
error code: [errors.md](errors.md).
