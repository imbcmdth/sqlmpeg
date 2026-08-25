# The sqlmpeg dialect

SQL, compiled - never executed by a database. The dialect is one
grammar drawn from two sources, split by a rule rather than by taste:
**the statement and call surface is Postgres's** - `COPY ... TO`,
`$$`-quoted functions, `:'var'` substitution, `name => value` binding,
`VARIADIC`, `DEFAULT`, `::` casts - and **the value and row model is
BigQuery's** - `STRUCT` literals, arrays of structs, `* EXCEPT` /
`* REPLACE`, `ARRAY(select)` with `SELECT AS STRUCT` - because a media
file is an array of structured things and BigQuery is the SQL built
around that shape. Each borrowed spelling is noted where it appears.

The second rule is as binding as the first: **one way to say a thing**,
unless a second way carries a benefit of its own. When a borrowing
covered an older spelling, the older spelling was removed, and a
future feature lands under the same test.

sqlmpeg accepts the surface below and rejects the rest with typed,
line-anchored errors ([errors.md](errors.md)). This page is the
language's formal shape: what exists, and - just as binding - what
does not.

## Statements

A query is ONE statement, or a script:

```
query   := select | copy
script  := (function ;)* (CREATE VIEW name AS select ;)* (copy ;)* copy?
function := CREATE FUNCTION name(param type [DEFAULT literal], ...) RETURNS rtype
            AS $$ select $$ LANGUAGE sql
rtype   := text | number | boolean | <kind>_stream | chapter | cue
         | attachment | any of those with [] | TABLE(col type, ...)
select  := [WITH cte (, cte)*] SELECT columns FROM from [WHERE pred]
           [GROUP BY exprs] [ORDER BY exprs]
           (UNION ALL select)*
copy    := COPY ( select ) TO dest [WITH ( option value (, ...)* )]
cte     := name AS ( select )
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
  type is while a `TABLE`-returning one is a `FROM` row source only. A
  parameter may declare `DEFAULT literal`; calls are positional, so an
  omitted trailing argument takes it. Recipes
  [67-68](examples.md#67-write-a-function-and-reuse-it),
  [79](examples.md#79-give-a-parameter-a-default).
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
  "bin": { "volume": "queries/volume.sql", "loudnorm": "queries/loudnorm-all.sql" },
  "lib": "src/audio.sql",
  "dependencies": { "tracks": "broadcast/tracks@^1.2.0" } }
```

`name` and `version` are required; the rest is optional. Each half of
the name is a lowercase plain identifier, and the first half - the
namespace - may not be `ffmpeg`, `sqlmpeg` or `wasm`. There is no
separate `namespace` key: the name carries it.

`lib` and `bin` each take a string or a map, never both at once. A
string names one file, its member named for the package segment
(`audio` in `imbcmdth/audio`) - `imbcmdth/deband`'s `"lib": "src/deband.sql"`
calls as `imbcmdth.deband(v)`. A map names several members, one file
per key, and there is no root-callable one - `imbcmdth/audio`'s `bin`
above reaches `imbcmdth.audio.volume`, never `imbcmdth.audio(...)`.
`lib` is the exports: each map key an exported function name, its
value the file defining it. `bin` is the programs: a map key is a
command word (`[a-z][a-z0-9_-]*`), its value one query file. Several
keys may name one file; a file may define more than the manifest
exports, and the rest are private to the package. Each named member
must be defined in the file named for it. Every file value is one path
relative to the manifest, stays under it, is not a pattern, and must
exist. A manifest declaring neither `lib` nor `bin` is a consumer
project that holds dependencies. `bins` and `libs` are gone; a
manifest that still writes either is rejected, its hint naming the
singular that replaced it.

The two halves are read by role. A lib file holds `CREATE FUNCTION`
definitions and nothing else, and a definition it exports but the query
never calls is fine - it is a library; an uncalled definition in the
query's own text is still rejected. A program's file is a whole query,
like any script, and the definition reader never opens it.

`dependencies` keys are package names - `<namespace>/<package>` - and
the values are the version range for each, recorded and shown, never
solved.

A query calls into an installed package one of two ways, always
written in full:

- **Three segments**, `<namespace>.<package>.<member>(...)` -
  `imbcmdth.audio.quieter(f.audio[1], 0.5)` - reaching any export
  whatever a project's own `dependencies` say.
- **Two segments**, `<namespace>.<package>(...)` - `imbcmdth.deband(v)` -
  the export a string `lib` names. A package whose `lib` is a map has
  none: the call is rejected, naming the exports to call by their own
  three-segment path instead.

There is no alias, so a ONE-segment qualifier is never a package
lookup at all: it is either a call into the query's own definitions
(bare, no qualifier) or, qualified, unresolvable - `UNKNOWN_FUNCTION`,
its hint naming the three-segment form calls across packages are
written as. `ffmpeg.<filter>` and `sqlmpeg.<macro>` stay two-part and
reserved, and take precedence over all of this - they are never read
as a package call.

The VERSION a call reaches is whichever the CALLING package's own
manifest depends on, not a single project-wide binding: a call written
in the query itself resolves against the project's own `dependencies`,
a call written inside an installed package's lib file or program
resolves against THAT package's own. Two packages may each depend on a
different version of a third, and each keeps resolving against its
own - `install` walking `dependencies` records, on every package's own
lockfile entry, the exact version it resolved each of ITS dependencies
to (see [Installed packages](#installed-packages)).

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
declares (read from its `-- variables:` header), and the dependencies.
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

The starter is a program, `queries/resize.sql` declared as a map `bin`
entry, not an export: a lib must name a file defining its export, so a
fresh directory has nothing to declare one with.

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
names one entry of a map `bin`, `<namespace>.<package>` a string
`bin`'s program. Either says which package when a bare name matches
programs in more than one; a bare name that does is rejected naming each
`<namespace>.<package>.<name>` it could mean. Variables still come
from `-v name=value`; an unset one substitutes to `NULL` (see
[Variables](#variables)), and a rejection at
its point of use names what the program's `-- variables:` header
declares.

### Installed packages

`sqlmpeg.lock`, beside the manifest, records what the project
installed. It is machine-owned: installing writes it, nothing else
should. Each entry is one of two kinds, one per package VERSION - a
name may have more than one entry, since installing never removes one
version to make room for another.

```json
{ "format_version": 3,
  "reproducible": false,
  "not_reproducible_because": "a package is linked to a working directory, so its files are not pinned here",
  "dependencies": { "broadcast/tracks": "1.2.0" },
  "packages": [
    { "kind": "registry", "name": "broadcast/tracks", "version": "1.2.0",
      "sha256": "<64 hex>", "store": "v1/ab/<64 hex>",
      "dependencies": { "imbcmdth/audio": "2.1.0" } },
    { "kind": "link", "path": "../my-lib" }
  ] }
```

A **registry** entry is keyed by the package's name and version, and
pins the sha256 of the ARCHIVE the package travels as - one gzipped
tar, built so the same content always produces the same bytes.
`install` hashes the bytes it downloaded before opening them: a
download that does not match the pin is discarded unopened and nothing
is written. What matches is extracted into the store under
`~/.cache/sqlmpeg/packages/`, and the extractor takes regular files and
directories under the package root and nothing else - no absolute
paths, no `..`, no links, no devices, and a member count and
uncompressed size cap. Reading a stored package hashes nothing; content
that is missing from the store is a rejection naming the package, never
a fall back to what is there. Its own `dependencies` is what ITS
manifest's `dependencies` resolved to when this entry was installed -
package name to the exact version, never a range - which is what lets
a call written inside that package resolve at the right version even
when another installed package depends on a different one of the same
name.

A **link** entry names a directory and nothing else - the package's
name comes from the manifest there, so renaming the package needs no
re-link. Its `sqlmpeg.json` is read like any other manifest, so an
edit lands in the next compile - which is the point, and which no
digest could survive. A lockfile holding a link is therefore not
reproducible, and says so in its own text. Two entries pinning one
package at one version are rejected; two different versions are not -
that is the whole point of carrying several.

The lockfile's own top-level `dependencies` is the same shape, one
level up: what THIS project directly installed, package name to the
exact version - what a call written in the project's own query
resolves against. It is separate from the manifest's `dependencies`,
which records the range as written and is never solved.

A package resolves through three layers, the first claim on its name
winning: the project's own manifest, then its lockfile, then the
machine-wide lockfile a global install writes. Two of those are worth
saying out loud, so a compile reports them without refusing: resolving
inside a project but landing on the machine-wide layer, and compiling
against a link. Each is reported once per package - as a `warning:`
line on stderr from the CLI, in the `warnings` array of an MCP tool
result, and through `compile_sql`'s optional `on_warning` callback for
a library caller.

A third travels the same channel and is not about packages at all. A
stream array a file has no tracks for - `f.subtitle` where the file
carries none - is empty, contributes no streams, and warns
(`EMPTY_STREAM_ARRAY`). That is what `unnest` of it already does and
what `SELECT *` already does; naming the column is the third spelling
of the same thing. A sink left with NO streams at all is still a
rejection, since it would write a file with nothing in it.

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

It then walks the installed package's own manifest `dependencies` and
installs each of THOSE the same way, recursively - each at its highest
published version, exactly as a direct install resolves one. A
dependency already pinned in the lockfile at the exact version wanted
is left alone: not refetched, not walked again. A different version of
the same name already pinned is not a conflict - install never
arbitrates between versions of one package, so it simply pins the new
one beside the old, and each installed package keeps resolving its own
calls against the version IT depends on. A cycle in that walk is
rejected, naming the loop. Only the package named on the command line
is recorded in the project's manifest; what came along transitively is
the lockfile's business, reported as what was brought along.

A version already in the store is not downloaded again - the same
content, installed into a second project or globally, is stored once.

The dependency is recorded in the manifest keyed by the package's own
name - `install broadcast/tracks` writes
`"broadcast/tracks": "1.2.0"`. A global install (`-g`) has no
manifest, so nothing is recorded there, but its lockfile's own
top-level `dependencies` still records what was directly asked for.
Installing a package already directly pinned at another version
changes what the project points at; the old entry is not removed, in
case something else installed still depends on it.

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
| `unnest(ARRAY[STRUCT(v AS c, ...), ...]) alias` | one per array element | a written row table; columns are the STRUCT field names, every element declaring the same set |
| `generate_series(start, stop[, step]) alias` | `stop - start` over `step`, inclusive | alias mandatory, names both the row table and its one column (`i.i`); bounds and step are integer literals after substitution |
| `cte_or_view_name [alias]` | its body's rows | a multi-row body is a multi-row source |
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
  two arrays in one call zip elementwise. `VARIADIC <array>` is a third
  reading: a trailing, at-most-one argument that spreads the array as
  the pad list instead, for a filter whose pad count follows its
  argument count (the N-input set, `concat`) - `amix(VARIADIC f.audio)`
  or `concat(intro, VARIADIC array_agg(v))`. An explicit count option
  that disagrees, or an empty array, is a rejection; a fixed-arity
  filter does not take `VARIADIC` at all.
- **A `tags` column**: a column named `tags` holding a map. Over track
  rows its keys land on the row's streams; over input rows only, on the
  container; a `NULL` field clears its key ([rows.md](rows.md#tags)).
  It is the ONLY column that writes metadata.
- **A `disposition` column**: an aliased value that sets the row's
  flags rather than a tag.
- **A value column** (CTE bodies): any other aliased compile-time
  value becomes a column of the body's rows, readable downstream. At a
  media sink such a column is a rejection - a SELECT column there is an
  output stream.
- **`array_agg(<per-row stream expression>)`**: gathers rows in row
  order; must be a whole column, or the sole argument of `VARIADIC`
  ([rows.md](rows.md#combining-rows)).
- **`ARRAY(<select>)`**: gathers a single-column, countable subquery's
  rows into an array, in expression position - the converse of
  `unnest`, and everywhere an `array_agg` result already stands
  (a whole column, or `VARIADIC`'s argument). `SELECT AS STRUCT
  <cols>` gathers a multi-column subquery into an array of structs
  instead, feeding a `chapters` / `attachments` column or a cue array
  the way `array_agg(STRUCT(...)::<record>)` does by hand. The
  subquery is self-contained (its own FROM, no reference to a row
  source outside it) and this branch may have no row source of its
  own already.
- **A metadata column** (table queries): any row column prints as
  data.
- **`*` / `<alias>.*`**: over an input, its array columns - the four
  stream arrays in `video`, `audio`, `subtitle`, `data` order in a
  media query, every array column including `chapters` in a table
  one. Over rows, the record's scalar fields (`tags` and `disposition`
  excluded, read them by name), which a table query prints and a media
  query rejects. Over a CTE, the stream columns its body named.

  `* EXCEPT(name, ...)` drops the named columns from the expansion;
  `* REPLACE(expr AS name, ...)` keeps the expansion's order but
  produces `name`'s slot from `expr` instead - a media query only. A
  name is a kind (`video`/`audio`/`subtitle`/`data`) over an input or a
  generated source, or the column name a CTE gave it. A name may
  appear in EXCEPT or REPLACE at most once, and a name absent from
  this file (a kind with no streams) is a no-op, exactly like a bare
  `*` skipping it. `REPLACE`'s `expr` does not have to keep the slot's
  original kind - the same freedom an ordinary aliased SELECT column
  already has.

Subscripts are positive integer literals, 1-based.
`(f.audio[1]).codec`-style accessors reach row columns without
unnest; in WHERE they are assertions. A tag is read by path,
`f.tags.title` / `t.tags.language`, one key at a time, and so is a
disposition flag, `t.disposition.forced`, over a closed key set. A bare
`f.tags` is the whole map: no value on its own, but an operand of `||`
in a `tags` column.

## Values and predicates

The borrowings below are instances of the split named at the top of
this page: value-model spellings come from BigQuery, and each is
recorded here at its point of use.

`STRUCT(value AS name, ...)` is a **deviation**: Postgres has no such
literal, and the spelling is borrowed (BigQuery's). It is the dialect's
one way to write a map or a record by field name — the `tags` column
takes a map, and a `::chapter` / `::cue` / `::attachment` cast turns one
into that record.

`* EXCEPT(...)` / `* REPLACE(...)` are borrowed the same way (BigQuery's
splat modifiers); `EXCEPT` is otherwise a Postgres set operator, but the
parenthesized form only ever appears after a bare `*`, where set
subtraction has no meaning.

`SELECT AS STRUCT <cols>` inside `ARRAY(...)` is borrowed too (BigQuery's
struct-valued SELECT); it names the array-of-structs form of a gathered
subquery, since a plain multi-column SELECT there is a typed rejection.
`ARRAY(<select>)` itself is not a borrowing - Postgres has it natively,
and this dialect's `unnest(ARRAY[STRUCT(...), ...])` row table is its own
addition, not a borrowed spelling.


One compile-time value grammar serves predicates, `tags` fields, value
columns, trim bounds, computed filter arguments, and fan-out
destinations:

```
value := literal | NULL | row-column | input-scalar
       | value || value            -- text only
       | value (+|-|*|/) value     -- Postgres typing; int/int truncates
       | value ::text
       | CASE WHEN pred THEN value [ELSE value] END
       | COALESCE(value, ...)      -- first argument a value, never a stream;
                                   -- arguments agree on one type

       | :'var' | :"var" | :var    -- CLI -v substitution, psql's forms
       | STRUCT(value AS name, ...)          -- a map, or a record with a cast
       | map || map                          -- merge, right side wins
       | ARRAY[STRUCT(...)::chapter, ...]    -- record arrays: chapter,
       | ARRAY[STRUCT(...)::cue, ...]        -- cue, attachment
```

`::text` is the spelling; `CAST(value AS text)` compiles too, but only
because sqlglot 30.17 parses it to the identical node with no marker
telling the two apart, so it is an undocumented synonym rather than a
second supported spelling.

Predicates: `= != < <= > >= BETWEEN IS [NOT] NULL [NOT] IN (literals)`,
combined with `AND OR NOT`. A boolean value is a predicate on its own
(`WHERE t.disposition.default`). All decided at compile time against probed
metadata - never a runtime ffmpeg predicate. NULL follows SQL:
`=`/`!=` both fail against it.

`WHERE alias.t BETWEEN a AND b` (either bound alone also works) is the
trim window - it compiles to seeks, not filters
([trimming.md](trimming.md)). Bounds take the value grammar, including
`f.duration` and any row column. A bound reading a row column is one
window per row, and the query says where those rows go: a fan-out
`TO (expression)` gives each a file, an aggregate gathers them into one.

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

**A function parameter's `DEFAULT` reads NULL as absence too — a
deviation from Postgres.** In Postgres, NULL is a value and only an
omitted argument triggers a `DEFAULT`; here, an explicit NULL argument to
a defaulted parameter takes the `DEFAULT` the same way omitting it does,
because NULL is absence everywhere else in the dialect and calls are
otherwise positional, so a caller who always writes the argument (an
unset variable, say) has no other way to omit it. A parameter with no
`DEFAULT` still takes a written NULL unchanged — that stays the way to
mean NULL itself.

One place absence is not "leave alone": a NULL field of a `tags` column
clears that key, so `STRUCT(:'title' AS title) AS tags` unset clears the
title. A program that means "keep unless told otherwise" writes
`STRUCT(COALESCE(:'title', f.tags.title) AS title) AS tags` — ordinary
SQL.

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
  view body, or a UNION ALL branch; a per-stream `tags` column in a
  grouped query (tag inside a CTE, aggregate outside).
- **Values**: casts other than to text; computed input paths;
  computed subscripts; `0` or negative subscripts; `||` over numbers
  without `::text`; division by a known zero.
- **`generate_series`**: a bound or step that is not an integer literal
  after substitution (a column reference included); a `0` step; a
  descending or empty range; an unaliased call.
- **Multi-row into one path** (`ROW_COUNT_MISMATCH`): gather or fan
  out, explicitly.
- **Filters**: variable-OUTPUT-pad (`split` - what the compiler's own
  split pass is for; UNION ALL is `concat` without ever naming it),
  multi-output (`scale2ref`, `feedback`), sinks, multi-output sources
  (`movie`, `avsynctest`); options typed `binary` or `dictionary`;
  runtime filter commands (`sendcmd`, `zmq`). A variable-INPUT-pad
  filter (`amix`, `hstack`, `xstack`, and every other filter your
  ffmpeg reports that way) is an ordinary callable filter, taking any
  stream count positionally or under `VARIADIC`. `ffmpeg.concat`/
  `concat` takes any stream count too, but ONLY under `VARIADIC` -
  called without it, `concat` is still `UNKNOWN_FUNCTION` (its own pad
  count is variable on the OUTPUT side too).
- **Functions**: `OR REPLACE`, `IF NOT EXISTS`, a schema-qualified
  name, any property but `RETURNS`/`LANGUAGE`, a language other than
  `sql`, `OUT`/`INOUT`/`VARIADIC`/`COLLATE` on a parameter, `DEFAULT
  NULL` (an unwritten argument already means NULL), a parameter without
  a `DEFAULT` written after one that has one, overloading, recursion,
  a body with its own `WITH` or `GROUP BY`/`ORDER BY`/`LIMIT`, a body
  referencing anything but its parameters and its own `FROM` aliases, a
  definition in the query's own text that nothing calls, and a
  `TABLE`-returning call in the `SELECT` list.
- **Packages**: a namespace no package claims; a namespace with no
  package by that name; a two-segment call on a package whose `lib` is
  a map, naming its exports instead; a member the package does not
  export; a manifest that is not one JSON object with `name` and
  `version`; a name that is not `<namespace>/<package>` in plain
  identifiers, or whose namespace is reserved; a `bins` or `libs` key
  (`lib`/`bin` replaced them, its hint naming the singular); a `lib` or
  `bin` value naming a pattern, a missing file, or a path outside the
  project; an exported name not defined in the file named for it; one name defined twice
  across a package's lib files; a lib file holding anything but
  `CREATE FUNCTION`; a program name that is not a command word or is
  declared twice; a dependency key that is not a package name or whose
  namespace is reserved; a dependency value that is not a non-empty
  string; a dependency cycle, naming the loop.
- **Lockfiles**: a lockfile that is not one JSON object with its three
  required keys, or is written in another format version; an entry of
  no known kind, missing a key, or holding an unknown one; two entries
  pinning one package at one version; a lockfile claiming to be
  reproducible while linking a directory; a linked directory with no
  manifest; stored content that is missing or was written by another
  store layout; a downloaded archive that does not hash to what the
  entry pins, or that holds a member outside the package root, a link,
  a device, or more members or bytes than the caps allow; an entry the
  package it points at disagrees with; a `dependencies` map -
  the lockfile's own, or one carried on a registry entry - that is not
  an object of name to version.
- **The registry**: a registry that cannot be read, with nothing cached
  to answer from; a catalogue or detail file that is not JSON, is not an
  object, is written in another format version, or holds a malformed
  field or a name that is not `<namespace>/<package>`; a package or a
  version the catalogue does not publish; an archive that is not the
  size or the digest its detail file records; an archive whose package
  says it is a different name or version than what was published.
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
