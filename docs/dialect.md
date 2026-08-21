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
script  := (CREATE VIEW name AS select ;)* (copy ;)* copy?
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
- Trailing `;` allowed; `--` and `/* */` comments allowed. Unquoted
  identifiers fold to lowercase. View, CTE, and alias names share one
  flat namespace across the whole script.

## FROM items

Every FROM item is a compile-time table; the column model per shape is
[rows.md](rows.md).

| form | rows | notes |
| --- | --- | --- |
| `input('path', name => value, ...) alias` | 1 | alias mandatory; path is a literal, never computed; trailing named options are ffmpeg's per-input flags |
| `ffmpeg.<source>(name => value, ...) alias` | 1 | generated stream (testsrc2, sine, color, anullsrc, ...), no `-i`; options named-only |
| `unnest(alias.<array>) alias` | one per element | the four stream arrays or `chapters`, of an input declared earlier in the same FROM |
| `cte_or_view_name [alias]` | its body's rows | a multi-row body is a multi-row source |

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
  `NULL` clears ([rows.md](rows.md#tags)).
- **`array_agg(<per-row stream expression>)`**: gathers rows in row
  order; must be a whole column ([rows.md](rows.md#combining-rows)).
- **A metadata column** (table queries): any row column prints as
  data.

Subscripts are positive integer literals, 1-based.
`(f.audio[1]).codec`-style accessors reach row columns without
unnest; in WHERE they are assertions. A tag is read by path,
`f.tags.title` / `t.tags.language`, one key at a time.

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
```

Predicates: `= != < <= > >= BETWEEN IS [NOT] NULL IN (literals)`,
combined with `AND OR NOT`. All decided at compile time against probed
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
control, metadata copying, chapters, two-pass - the full table is
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
- **Identifiers**: double-quoted identifiers (except tag-key aliases);
  the reserved names `ffmpeg` and `sqlmpeg` as aliases.
- **Timeline**: `WHERE t` on generated sources (give the source its
  own `duration`); selecting chapter rows as streams; a bare
  `f.chapters` in a media query, or subscripting it (`unnest` it); a data/subtitle
  track through any filter (passthrough only).

What a specific rejection looks like, with captured JSON for every
error code: [errors.md](errors.md).
