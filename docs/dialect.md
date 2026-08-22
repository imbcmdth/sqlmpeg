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
package: it claims a namespace, and the SQL files it lists export their
functions under it.

```json
{ "name": "my-edits", "version": "0.1.0", "namespace": "me",
  "description": "...", "sources": ["src/*.sql"] }
```

`name`, `version`, `namespace` and `sources` are required;
`description` is optional. `namespace` is a lowercase plain identifier
and may not be `ffmpeg`, `sqlmpeg` or `wasm`. Each `sources` pattern is
a glob relative to the manifest, stays under it, and must match at
least one file.

A query calls into the namespace: `me.quieter(f.audio[1], 0.5)` as a
value, `FROM me.pick('a.mka') t` as a row source. The call is expanded
exactly as a definition written into the query would be - same
hygiene, same arity and type checks, same command out. Nothing is
prepended to the script, and a package's names never enter the script's
flat namespace.

A package source holds `CREATE FUNCTION` definitions and nothing else,
and one it exports but the query never calls is fine - it is a library.
An uncalled definition in the query's own text is still rejected.

The project is found by walking up from the query file's directory, or
from the working directory for a query typed on the command line; there
is no flag. Outside a project nothing changes: a namespaced call is
rejected as it always was. Library callers pass
`compile_sql(text, packages=sqlmpeg.discover(path))` rather than
relying on a working directory, and the MCP tools take the same path as
a `project` argument.

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
- **Packages**: a namespace no manifest claims; a member the namespace
  does not define; a manifest that is not one JSON object with the four
  required keys; a namespace that is reserved or is not a plain
  identifier; a source pattern matching no file or leaving the project
  directory; one name defined twice across a package's sources; a
  package source holding anything but `CREATE FUNCTION`.
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
