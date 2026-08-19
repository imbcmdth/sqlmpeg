# RFC-015 — Compile-time arithmetic and casts

Status: accepted 2026-08-19 (execution order: this, then RFC-014, then
RFC-013).

## Operators
`+ - * /` over compile-time numbers: row/chapter columns, probed
scalars, numeric literals, bare `:name` variables. Postgres typing:
int op int -> int (`/` truncates), any float operand -> float. Float
results emit shortest-roundtrip repr. Precedence from the parser.

## Casts
`::text` and `CAST(x AS text)` on compile-time values (number -> text).
This is the honest bridge into `||` (which stays strictly text) and the
prerequisite RFC-014's path expressions need (`c.index::text`).
`::int`/`::float` on text: NOT in v1 (raw `:name` already parses as a
number in numeric position).

## Where values flow
Everywhere the compile-time grammar already goes (WHERE/ON comparisons,
BETWEEN bounds, CASE branches, tag values, later fan-out paths), plus
two new frontiers:
1. **Trim bounds as expressions**: `WHERE f.t BETWEEN <expr> AND
   <expr>` over compile-time numbers. Still lowers to the input seek.
2. **Computed filter arguments in row contexts**: a literal position of
   a call over a row-table stream may be an expression over THAT row's
   columns (`scale(t.track, t.width / 2, -2)`) - evaluated per row,
   feeding broadcasting as ordinary literals.

## `f.duration`
Input aliases grow a probed scalar pseudo-column `duration` (container
duration, seconds) usable in compile-time expressions - the general
form of what `seek_end` special-cased (`WHERE f.t <= f.duration - 60`).
Probed-only; referencing it on an unprobeable input is a typed
rejection. No other file-level scalars in v1.

## Fences
Division by a zero that is knowable at compile time: typed rejection.
Arithmetic on NULL follows SQL (NULL result; a NULL trim bound is a
rejection naming the unprobed field). No arithmetic on streams.

## Recipes (failing first, as always)
45: per-row computed scale (t.width / 2). 46: all-but-the-last-N
trim (f.duration - 0.5). RFC-014's padded chapter split lands with
RFC-014.
