# sqlmpeg error contract

Every rejection sqlmpeg produces is a `SqlmpegError`: a typed `code`, a
human-readable `message`, an optional `line`/`col` anchor into the query
text, and an optional `hint`. See the "Error contract" section of
`sqlmpeg-project.md` for the design rationale (this is what makes the
generate -> `validate --json` -> repair loop converge for an LLM caller).

`code` is one of the `ErrorCode` enum values defined in `sqlmpeg/errors.py`.
The JSON shape is formalized in `docs/error-schema.json`.

Get the structured form from the CLI:

```
sqlmpeg validate query.sql --json
```

On success this prints nothing and exits 0. On rejection it prints one JSON
object to stdout and exits 1. Every example below is real output captured by
running that exact command against the example query — nothing here is
invented.

## PARSE_ERROR

**Meaning:** The query text is not valid SQL under sqlglot's `postgres`
dialect (guardrail #2: sqlmpeg always parses Postgres dialect). This fires
before any sqlmpeg-specific validation runs — it is whatever sqlglot itself
rejects, plus sqlmpeg's own "empty query" / "no statement found" checks for
degenerate input.

**Fires when:** the text fails to tokenize or parse — missing parens,
garbled keywords, truncated statements, or an empty/whitespace-only query.

**Example query:**

```sql
SELECT frame FROM input('x.mp4' a
```

(note the missing closing paren after `'x.mp4'`)

**Error JSON:**

```json
{"line": 1, "col": 33, "code": "PARSE_ERROR", "message": "Expecting )", "hint": null}
```

## UNKNOWN_FUNCTION

**Meaning:** A call in the query names a function that is not in the
stdlib table (`sqlmpeg.stdlib.FUNCTIONS`). This is checked both for the
outer call in a projection/WHERE expression and for nested calls used as a
frame argument to another call.

**Fires when:** the query calls something that isn't `scale`, `crop`,
`overlay`, `hflip`, `vflip`, `blur`, `blur_regions`, `draw_box`, `text`,
`speed`, `fade_in`, or `fade_out`.

**Example query:**

```sql
SELECT sharpen(a.frame)
FROM input('x.mp4') a
```

**Error JSON:**

```json
{"line": 1, "col": 8, "code": "UNKNOWN_FUNCTION", "message": "unknown function sharpen()", "hint": "known functions: blur, blur_regions, crop, draw_box, fade_in, fade_out, hflip, overlay, scale, speed, text, vflip"}
```

The hint is a plain alphabetical listing of all known function names, not a
fuzzy "did you mean" match against the misspelled name.

## UNKNOWN_ALIAS

**Meaning:** A `<alias>.frame` reference (or a bare table reference in
`FROM`) names an alias or CTE that was never introduced by this query's
`FROM`/CTE list.

**Fires when:** you reference `b.frame` but only declared `FROM
input('x.mp4') a` (no alias `b` in scope), or reference a CTE name that
hasn't been defined (or isn't visible yet — CTEs only see earlier CTEs in
the same `WITH`, no forward references).

**Example query:**

```sql
SELECT b.frame FROM input('x.mp4') a
```

**Error JSON:**

```json
{"line": 1, "col": 8, "code": "UNKNOWN_ALIAS", "message": "unknown alias 'b'", "hint": "known names: a"}
```

## UDF_ARG_TYPE

**Meaning:** A stdlib function call's argument count and/or kinds
(`frame`/`num`/`str`) don't match any of that function's declared variants
in the function table.

**Fires when:** you call a known function with the wrong arity, or pass a
frame where a number/string is expected (or vice versa) — e.g. calling
`blur(a.frame)` when `blur` requires `(frame, num)`.

**Example query:**

```sql
SELECT blur(a.frame)
FROM input('x.mp4') a
```

**Error JSON:**

```json
{"line": 1, "col": 8, "code": "UDF_ARG_TYPE", "message": "blur() expects blur(frame, num), got blur(frame)", "hint": "arguments are frame expressions or literals, in the order shown"}
```

## SINGLE_OUTPUT_ONLY

**Meaning:** The dialect requires exactly one output column of type
`frame` in each SELECT (top-level or CTE body).

**Fires when:** the projection list has more than one expression, or (a
narrower related case reported with the same code) fewer than the exactly-one
required.

**Example query:**

```sql
SELECT a.frame, a.frame
FROM input('x.mp4') a
```

**Error JSON:**

```json
{"line": 1, "col": 17, "code": "SINGLE_OUTPUT_ONLY", "message": "SELECT must produce exactly one frame column, got 2", "hint": "split the extra columns into separate queries"}
```

## NO_STREAMING_EQUIVALENT

**Meaning:** The construct is valid SQL with well-defined relational
semantics, but has no meaning in a single-pass streaming filtergraph
(ffmpeg processes frames as a stream; it cannot look ahead, sort, or
deduplicate across the whole input).

**Fires when:** the query uses `GROUP BY`, `HAVING`, `ORDER BY`, `SORT BY`,
`CLUSTER BY`, `DISTRIBUTE BY`, `LIMIT`, `OFFSET`, `DISTINCT`, `QUALIFY`,
`WINDOW`, `CONNECT BY`, an aggregate function, a window function, a
subquery predicate (`IN (SELECT ...)`, `EXISTS`, etc.), or `UNION` (as
opposed to `UNION ALL`, which requires deduplication).

**Example query:**

```sql
SELECT a.frame
FROM input('x.mp4') a
GROUP BY a.frame
```

**Error JSON:**

```json
{"line": 3, "col": 10, "code": "NO_STREAMING_EQUIVALENT", "message": "GROUP BY has no streaming equivalent", "hint": "remove the GROUP BY clause"}
```

## CONCAT_MISMATCH

**Reserved — not yet raised by v0.** Per the project spec, `UNION ALL`
lowers to ffmpeg's `concat` filter, which requires every branch to agree on
fps and resolution. v0 has no ffprobe-driven type inference (a stated
non-goal: "ffprobe-driven type inference ... probing inputs to validate
dimensions is a v1 idea"), so this mismatch cannot currently be detected at
compile time — nothing in `sqlmpeg/*.py` raises `ErrorCode.CONCAT_MISMATCH`
today. It is defined in the enum ahead of time so the wire format is stable
once v1 adds input probing. No example JSON: it cannot be triggered from
SQL in this version.

## UNSUPPORTED_SQL

**Meaning:** Catch-all for syntactically valid SQL that falls outside the
v0 dialect and isn't one of the more specific codes above (it has no
streaming-vs-batch distinction to make; it's simply not part of the
surface sqlmpeg accepts). This is by far the most common code in practice —
most of `sqlmpeg/parser.py`'s rejections use it, for things like: multiple
statements, unsupported clause keys, `SELECT *`, explicit `JOIN` syntax
(only comma cross-joins are supported), aliased/nested subqueries, `WITH
RECURSIVE`, malformed or duplicate CTE/alias names, an empty `WHERE`
clause, or a top-level statement that isn't a `SELECT`/`UNION ALL`.

**Fires when:** (one example among many) the query selects `*` instead of
an explicit frame expression.

**Example query:**

```sql
SELECT *
FROM input('x.mp4') a
```

**Error JSON:**

```json
{"line": 1, "col": 8, "code": "UNSUPPORTED_SQL", "message": "SELECT * is not supported", "hint": "select a single frame expression"}
```

## INTERNAL

**Bug backstop, not a user-input error.** Every compiler pass (`parse`,
`lower`, `insert_splits` via `compile_sql`) wraps its body in a catch-all
that converts any *unexpected* exception (a sqlglot internal bug, a
`RecursionError` on a pathologically nested query, or a bug in sqlmpeg
itself) into `ErrorCode.INTERNAL` instead of letting it propagate as a raw
traceback (guardrail #7: no panics on user input). The fuzz corpus
(`tests/test_fuzz.py`) asserts this code never fires across its mutated
query corpus — if you see `INTERNAL` in the wild, it is a bug in sqlmpeg;
please report the query that triggered it. No example JSON is included
here because there is no known SQL input in v0 that reaches this path
deliberately.
