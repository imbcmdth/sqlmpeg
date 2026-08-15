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
invented. Errors that require reading the file (`STREAM_NOT_FOUND`,
`BROADCAST_MISMATCH`, some `INPUT_NOT_FOUND` cases) were captured against the
real fixtures in `tests/fixtures/` (`av.mp4`: one video stream, one audio
stream; `av2.mp4`: one video stream, two audio streams tagged
`language=eng`/`language=fra`).

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
SELECT a.video[1] FROM input('x.mp4' a
```

(note the missing closing paren after `'x.mp4'`)

**Error JSON:**

```json
{"line": 1, "col": 38, "code": "PARSE_ERROR", "message": "Expecting )", "hint": null}
```

## UNKNOWN_FUNCTION

**Meaning:** A call in the query names a function that is not in the
stdlib table (`sqlmpeg.stdlib.FUNCTIONS`, rendered in full in
[docs/stdlib.md](stdlib.md)). This is checked both for the outer call in a
projection/WHERE expression and for nested calls used as an argument to
another call.

**Fires when:** the query calls a name that isn't one of the stdlib
functions (video or audio).

**Example query:**

```sql
SELECT sharpen(a.video[1])
FROM input('x.mp4') a
```

**Error JSON:**

```json
{"line": 1, "col": 8, "code": "UNKNOWN_FUNCTION", "message": "unknown function sharpen()", "hint": "known functions: afade_in, afade_out, amix, atempo, blur, blur_regions, crop, draw_box, fade_in, fade_out, hflip, overlay, reverb, scale, speed, text, vflip, volume"}
```

The hint is a plain alphabetical listing of all known function names, not a
fuzzy "did you mean" match against the misspelled name.

## UNKNOWN_ALIAS

**Meaning:** A `<alias>.video`/`<alias>.audio`/`<alias>.frame` reference (or
a bare table reference in `FROM`) names an alias or CTE that was never
introduced by this query's `FROM`/CTE list.

**Fires when:** you reference `b.video[1]` but only declared `FROM
input('x.mp4') a` (no alias `b` in scope), or reference a CTE name that
hasn't been defined (or isn't visible yet — CTEs only see earlier CTEs in
the same `WITH`, no forward references).

**Example query:**

```sql
SELECT b.video[1] FROM input('x.mp4') a
```

**Error JSON:**

```json
{"line": 1, "col": 8, "code": "UNKNOWN_ALIAS", "message": "unknown alias 'b'", "hint": "known names: a"}
```

## UDF_ARG_TYPE

**Meaning:** A stdlib function call's argument count and/or kinds
(`video`/`audio`/`num`/`str`) don't match any of that function's declared
variants in the function table.

**Fires when:** you call a known function with the wrong arity, or pass a
stream where a number/string is expected (or vice versa) — e.g. calling
`blur(a.video[1])` when `blur` requires `(video, num)`.

**Example query:**

```sql
SELECT blur(a.video[1])
FROM input('x.mp4') a
```

**Error JSON:**

```json
{"line": 1, "col": 8, "code": "UDF_ARG_TYPE", "message": "blur() expects blur(video, num), got blur(video)", "hint": "arguments are stream expressions or literals, in the order shown"}
```

## SINGLE_OUTPUT_ONLY

**Reserved, not currently raised.** The SELECT list is the output stream
list — every column is a separate `-map`, in order — so a multi-column
SELECT is ordinary usage, not an error. This code is kept in the enum
(and in `docs/error-schema.json`'s `code` enum) purely for wire-format
stability; no code path in `sqlmpeg/*.py` raises it. No example JSON: it
cannot be triggered from SQL.

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
SELECT a.video[1]
FROM input('x.mp4') a
GROUP BY a.video[1]
```

**Error JSON:**

```json
{"line": 3, "col": 10, "code": "NO_STREAMING_EQUIVALENT", "message": "GROUP BY has no streaming equivalent", "hint": "remove the GROUP BY clause"}
```

## CONCAT_MISMATCH

**Meaning:** `UNION ALL` lowers to ffmpeg's `concat` filter, which requires
every branch to produce the identical column signature: same count, same
stream types, in the same order (array columns count each element, so
`audio[2]` and `audio[1]` also mismatch). When every input in the query is
probeable, this also catches real fps/resolution/sample-rate mismatches
between branches at the ffmpeg-filter level; the type/count/order check
above needs no probing at all and fires purely from the SQL shape.

**Fires when:** two `UNION ALL` branches select a different number of
columns, different stream types, columns in a different order, or
differently-sized array columns.

**Example query:**

```sql
SELECT a.video[1]
FROM input('x.mp4') a
UNION ALL
SELECT b.audio[1]
FROM input('y.mp4') b
```

**Error JSON:**

```json
{"line": 4, "col": 8, "code": "CONCAT_MISMATCH", "message": "UNION ALL branches must select the same stream types in the same order: branch 1 selects (video), branch 2 selects (audio)", "hint": "ffmpeg concat needs identical segments; reorder or add columns"}
```

## UNSUPPORTED_SQL

**Meaning:** Catch-all for syntactically valid SQL that falls outside the
dialect and isn't one of the more specific codes above (it has no
streaming-vs-batch distinction to make; it's simply not part of the
surface sqlmpeg accepts). This is by far the most common code in practice —
most of `sqlmpeg/parser.py`'s rejections use it, for things like: multiple
statements, unsupported clause keys, `SELECT *`, explicit `JOIN` syntax
(only comma cross-joins are supported), aliased/nested subqueries, `WITH
RECURSIVE`, malformed or duplicate CTE/alias names, an empty `WHERE`
clause, a non-positive or non-literal array subscript, or a top-level
statement that isn't a `SELECT`/`UNION ALL`.

**Fires when:** (one example among many) the query selects `*` instead of
explicit stream expressions.

**Example query:**

```sql
SELECT *
FROM input('x.mp4') a
```

**Error JSON:**

```json
{"line": 1, "col": 8, "code": "UNSUPPORTED_SQL", "message": "SELECT * is not supported", "hint": "select a single frame expression"}
```

## STREAM_NOT_FOUND

**Meaning:** A subscript (`<alias>.video[k]` / `<alias>.audio[k]`, or the
equivalent bound recorded for a CTE array column) is out of range for the
number of streams actually present. This is only reachable against a
probed input (a local, readable file, with `ffprobe` on `PATH`, and
`--no-probe` not passed) or against a CTE array column whose length was
already recorded when it was lowered — an explicit subscript against an
unprobed input compiles unchecked and lets ffmpeg fail at run time instead.

**Fires when:** the subscript is 1-based and positive but exceeds the
actual per-type stream count of the probed file, or (the CTE case) exceeds
the recorded length of an array-typed CTE column, or the whole array is
empty (e.g. splatting `.audio` on a video-only file).

**Example query** (`tests/fixtures/av.mp4` has exactly one video stream):

```sql
SELECT a.video[2]
FROM input('tests/fixtures/av.mp4') a
```

**Error JSON:**

```json
{"line": 1, "col": 8, "code": "STREAM_NOT_FOUND", "message": "'a.video[2]' does not exist: 'tests/fixtures/av.mp4' has 1 video stream", "hint": "stream subscripts are 1-based: a.video[1] is the first video stream"}
```

## INPUT_NOT_FOUND

**Meaning:** A bare array (`<alias>.video` / `<alias>.audio`, splatted in
the SELECT list or handed to a function to broadcast over) needs to know
how many streams the file has to expand, and the input could not be probed
— the file does not exist, is unreadable, is a URL, or `--no-probe` was
passed. "Cannot enumerate the streams of a file I cannot read" is reported
as its own natural error rather than reusing a generic probing-failure
code.

**Fires when:** a bare array appears (directly in the SELECT list or as a
function argument) over an input that has no probe result.

**Example query:**

```sql
SELECT a.audio
FROM input('nope.mp4') a
```

**Error JSON:**

```json
{"line": 1, "col": 8, "code": "INPUT_NOT_FOUND", "message": "cannot enumerate the streams of 'nope.mp4': file not found or unreadable", "hint": "'a.audio' is the whole stream array, and only a readable input can size it; subscript one stream, e.g. a.audio[1]"}
```

## BROADCAST_MISMATCH

**Meaning:** Broadcasting a function call over more than one array
argument zips them elementwise (no cross products); every array argument
to that call must be the same length. A scalar argument (including a
single-subscripted stream) always broadcasts and never triggers this.

**Fires when:** two or more array arguments to the same call have
different lengths.

**Example query** (`tests/fixtures/av2.mp4` has 2 audio streams,
`tests/fixtures/av.mp4` has 1):

```sql
SELECT amix(a.audio, b.audio)
FROM input('tests/fixtures/av2.mp4') a, input('tests/fixtures/av.mp4') b
```

**Error JSON:**

```json
{"line": 1, "col": 8, "code": "BROADCAST_MISMATCH", "message": "amix() cannot broadcast over arrays of different lengths: a.audio has 2 streams, b.audio has 1 stream", "hint": "broadcast arrays zip elementwise, one output per element; subscript one of them to pair a single stream with the other, e.g. a.audio[1]"}
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
here because there is no known SQL input that reaches this path
deliberately.
