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
sqlmpeg validate --json "<query>"
```

(or `sqlmpeg validate --json -f query.sql` if the query is in a file). On
success this prints nothing and exits 0. On rejection it prints one JSON
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

**Meaning:** A call in the query names a function that is neither in the
stdlib table (`sqlmpeg.stdlib.FUNCTIONS`, rendered in full in
[docs/stdlib.md](stdlib.md)) nor a filter the installed ffmpeg reports
(`sqlmpeg/registry.py`, RFC-003 -- see [docs/dynamic-filters.md](dynamic-filters.md)).
This is checked both for the outer call in a projection/WHERE expression and
for nested calls used as an argument to another call.

**Fires when:** the query calls a name that resolves in neither tier.

**Example query:**

```sql
SELECT gblu(a.video[1])
FROM input('x.mp4') a
```

**Error JSON** (against ffmpeg 7.1; the near match crosses BOTH tiers --
`gblu` is closer to the dynamic filter `gblur` than to any stdlib name):

```json
{"line": 1, "col": 8, "code": "UNKNOWN_FUNCTION", "message": "unknown function gblu()", "hint": "did you mean gblur()?"}
```

The hint is a "did you mean" match against the stdlib and, when available,
every name the installed ffmpeg reports. Without a near match it falls back
to a plain alphabetical listing of the stdlib alone (the dynamic list runs to
~460 entries -- too long to be a useful inline hint), suffixed with why the
short list might not be the whole story on another machine: `"(dynamic
ffmpeg filters need ffmpeg on PATH)"` when there is no ffmpeg to ask, or
`"(dynamic ffmpeg filters are disabled by --portable)"` when `--portable` was
passed.

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

Two RFC-003 rejections also land here: a named argument written out of place
(a positional argument after a named one, or the same name twice), and a
named argument when there is no ffmpeg to validate it against -- because it
was not found, or because `--portable` deliberately turned the introspection
off. The rule is one line: named arguments ARE your installed ffmpeg, so a
query that compiles portably compiles everywhere.

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

## UNKNOWN_SINK_OPTION

**Meaning:** A `COPY (query) TO 'path' WITH (...)` option name (RFC-002) is
not one of the entries in `sqlmpeg.sink.SINK_OPTIONS`.

**Fires when:** an option in the `WITH (...)` list is misspelled or simply
doesn't exist -- e.g. `video_codc 'libx264'` instead of `video_codec
'libx264'`, or an option outside the v1 table entirely.

**Example query:**

```sql
COPY (
  SELECT a.video[1]
  FROM input('x.mp4') a
) TO 'out.mp4' WITH (
  video_codc 'libx264'
)
```

**Error JSON:**

```json
{"line": 5, "col": 14, "code": "UNKNOWN_SINK_OPTION", "message": "unknown sink option 'video_codc'", "hint": "did you mean 'video_codec'?"}
```

The anchor lands on the option's VALUE, not its name: sqlglot records no
token position on a bare `WITH (...)` option name, only on a string/number
literal value (see "Anchoring is coarse" in the parser notes) -- `line`/`col`
here point at the `'libx264'` literal, one line below the misspelled name.

## SINK_OPTION_TYPE

**Meaning:** A `COPY (query) TO 'path' WITH (...)` option's value does not
match the type declared for it in `sqlmpeg.sink.SINK_OPTIONS` (`str` / `int`
/ `bool`).

**Fires when:** a `str`-typed option is given a number or bool, an
`int`-typed option is given a string or a float, or a `bool`-typed option is
given anything other than `true`/`false` -- e.g. `crf '20'` (a string where
`crf` wants an int) or `faststart 1` (an int where `faststart` wants a
bool).

**Example query:**

```sql
COPY (
  SELECT a.video[1]
  FROM input('x.mp4') a
) TO 'out.mp4' WITH (
  crf 'high'
)
```

**Error JSON:**

```json
{"line": 5, "col": 7, "code": "SINK_OPTION_TYPE", "message": "option 'crf' expects an int, got 'high'", "hint": "crf takes a bare integer literal, e.g. crf 20"}
```

## UNKNOWN_FILTER_OPTION

**Meaning:** A named argument (`<name> => <value>`, RFC-003) names an option
the ffmpeg filter it targets does not have. The option set is read out of the
installed ffmpeg (`ffmpeg -help filter=<name>`, see `sqlmpeg/registry.py`),
so it is exactly what that binary supports -- not a table in sqlmpeg.

**Fires when:** the option name is misspelled or belongs to a different
filter -- either on a dynamically-resolved call (`gblur(a.frame, sigmma =>
5)`) or on a stdlib call's trailing named extras, which are checked against
the filter that spec expands to (`blur` -> `gblur`).

**Example query:**

```sql
SELECT gblur(a.frame, sigmma => 5)
FROM input('x.mp4') a
```

**Error JSON** (against ffmpeg 7.1; the option list is that binary's):

```json
{"line": 1, "col": 33, "code": "UNKNOWN_FILTER_OPTION", "message": "filter 'gblur' has no option 'sigmma'", "hint": "did you mean sigma => ...?"}
```

The anchor lands on the option's VALUE: sqlglot records no token position on
the `exp.Var` holding a named argument's name (the same gap `COPY ... WITH`
option names have), so `line`/`col` point at the `5`.

Machine-dependence is the point of this code -- a query using it compiles
only where that ffmpeg does. See `UNSUPPORTED_SQL` for what happens when
there is no ffmpeg to check against at all.

## FILTER_OPTION_TYPE

**Meaning:** A named argument's value does not match the option's
introspected type, declared range, or set of named constants.

**Fires when:** a numeric option gets a string or a number outside
`(from A to B)`, a boolean option gets anything but `true`/`false`, an enum
option gets something that is not one of its constants (or gets a bare
number instead of a quoted constant name), or the option's ffmpeg type is
one sqlmpeg cannot set at all (`binary`, `dictionary`).

**Example query:**

```sql
SELECT gblur(a.frame, sigma => 5000)
FROM input('x.mp4') a
```

**Error JSON** (against ffmpeg 7.1; the range is that binary's):

```json
{"line": 1, "col": 32, "code": "FILTER_OPTION_TYPE", "message": "option 'sigma' of filter 'gblur' accepts a number from 0 to 1024, got 5000", "hint": "pick a value from 0 to 1024"}
```

Enum options quote their constant name (`transition => 'wipeleft'`), and the
message lists the constants (truncated with a count when there are many --
`xfade`'s `transition` has 59). Anchoring is the same as
`UNKNOWN_FILTER_OPTION`'s: the value, not the name.

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
