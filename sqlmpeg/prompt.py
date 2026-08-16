"""The portable LLM system prompt for sqlmpeg (plan 012).

``build_system_prompt()`` returns the text a user hands to whatever model they
like as a system prompt, so that "describe the edit in English, get a runnable
ffmpeg command" works without sqlmpeg ever calling an API itself.

Two properties this module must keep:

* **Deterministic and pure.** No I/O, no clock, no environment. The same string
  every call, on every machine -- ``scripts/gen_prompt.py`` commits it to
  ``docs/system-prompt.md`` and a test asserts the committed copy is fresh.
* **Generated from the real surface.** The function reference is rendered from
  :data:`sqlmpeg.stdlib.FUNCTIONS` (guardrail #4) and the repair guidance is
  keyed by :class:`sqlmpeg.errors.ErrorCode`, so a new function or code cannot
  silently go undocumented. Nothing about the stdlib is hardcoded here.

Marker convention (relied on by ``tests/test_prompt.py``): every fenced
```sql block in the prompt is one complete query that ``compile_sql`` accepts
standalone, with no input file required to exist. Rejected SQL is only ever
shown inline, in single backticks, so that the extract-and-compile test can
treat every fence as a promise. An example whose query needs a real, readable
file to compile (a bare-array broadcast, which needs the file's actual stream
count) is fenced ```sql-probed instead -- a distinct tag the extractor
deliberately does not match, so it is exempt from that promise.

The prompt is ASCII-only: it is printed by ``sqlmpeg prompt`` and piped around
on consoles whose encoding is not UTF-8.
"""

from __future__ import annotations

from sqlmpeg.errors import ErrorCode
from sqlmpeg.sink import SINK_OPTIONS
from sqlmpeg.stdlib import FUNCTIONS, Param

__all__ = ["build_system_prompt"]


# ---------------------------------------------------------------------------
# role
# ---------------------------------------------------------------------------

_ROLE = """\
# sqlmpeg SQL

You translate natural-language video-edit requests into sqlmpeg SQL. sqlmpeg
compiles that SQL into an ffmpeg `-filter_complex` command; a query is correct
only if it compiles, so stay strictly inside the dialect below.

Output only the query text. No prose, no explanation, no markdown fences,
unless you are explicitly asked for them. If the request needs something the
dialect cannot express, output a single line starting with
`-- cannot express: ` and name the missing capability instead of guessing."""


# ---------------------------------------------------------------------------
# dialect
# ---------------------------------------------------------------------------

_DIALECT = """\
## Dialect

Postgres syntax, one statement, read-only. A query is a single `SELECT`, or
several `SELECT`s joined by `UNION ALL`. A trailing `;` is allowed; a second
statement is not. `--` and `/* */` comments are allowed.

### Shape
- The SELECT list IS the output stream list: one expression is one output
  stream, and column order is `-map` order. Any number of columns is legal.
- Every `SELECT` needs a `FROM`.
- There is no implicit audio track. An input's audio is only in the output if
  you select it -- `<alias>.audio[k]` (or a call over it) as one of the
  columns.

### Sources
- `FROM input('path') alias` -- the alias is mandatory. The path is a
  single-quoted string literal: a file path or a URL, nothing computed.
- Combine sources with a comma cross-join: `FROM input('a.mp4') a,
  input('b.mp4') b`. `JOIN ... ON`, `USING`, and outer joins are rejected.
- Each `input(...) alias` is one ffmpeg input. The key is the ALIAS, not the
  path, so the same file under two aliases gives you two independent streams --
  that is how you composite a clip onto itself.
- Every alias and CTE name must be unique across the WHOLE query, including
  across `UNION ALL` branches.
- Unquoted identifiers fold to lowercase. Never double-quote an identifier.

### Columns
- `<alias>.video` and `<alias>.audio` are array-typed pseudo-columns, one
  entry per stream of that type in the file, in file order. `<alias>.video[k]`
  / `<alias>.audio[k]` picks the k-th stream, 1-based (`<alias>.video[1]` is
  the first video stream). `<alias>.frame` is sugar for `<alias>.video[1]`.
- Subscripts are positive integer literals only -- `0`, negative numbers, and
  computed subscripts are rejected.
- A bare `<alias>.video` / `<alias>.audio` (no subscript) is the WHOLE array.
  It is legal splatted directly into the SELECT list (one output stream per
  element, in order) and legal as a function argument, where it broadcasts
  (see Broadcasting below). Either use needs a readable input to know how many
  streams there are: `sqlmpeg compile` probes local files automatically, but a
  URL, a missing file, or `--no-probe` falls back to a fully symbolic compile,
  where a bare array cannot be sized and is rejected.
- `<alias>.t` is time in seconds. It is legal ONLY inside the `WHERE` form
  below; it is not a stream and cannot appear in the SELECT list.
- There are no other columns and no `*`.

### Broadcasting
- Passing a bare array where a function expects one stream applies the call
  once per element: `reverb(a.audio, 0.3)` over a 3-track file produces 3
  filtered streams, one per track. `num`/`str` arguments apply unchanged to
  every element. Nesting composes: `volume(reverb(a.audio, 0.3), 0.5)`.
- Passing more than one array to the same call zips them elementwise, in
  order -- no cross products. All arrays in one call must be the same length,
  or it is a typed `BROADCAST_MISMATCH`. A scalar argument (including a
  subscripted single stream) always broadcasts and never triggers that check.
- Every broadcast-expanded output stream automatically keeps its source
  stream's `language`/`title` tag in the compiled command.
- A call over two or more streams (`amix`, `overlay`) is a mix, not a
  passthrough: a zipped mix keeps a tag only when every zipped input agrees
  on it, e.g. `amix` over two English tracks keeps `language=eng`.
- `<cte>.<name>` for an array-valued CTE column behaves exactly like an
  input's `<alias>.video` / `<alias>.audio`: splat it, broadcast a function
  over it, or subscript one element with `<cte>.<name>[k]`.

### CTEs
- `WITH name AS (SELECT ...)`, multiple CTEs comma-separated. A CTE body is a
  `SELECT` or a `UNION ALL` of `SELECT`s; each column of its SELECT list
  becomes a named CTE column (name it with `AS`).
- Reference a CTE by its bare name in `FROM`: `FROM pip`. Never alias it
  (`FROM pip p` is rejected) and never wrap it in `input()`.
- A CTE sees only the CTEs defined BEFORE it. No forward references, no
  `RECURSIVE`, no `WITH` nested inside a CTE body, no CTE column lists.
- A name may appear at most once in a single `FROM` clause. To consume the
  same stream twice in one expression, just write `c.frame` (or
  `c.<name>`) twice -- the compiler inserts the `split`/`asplit` for you.
  Reuse is automatic; never duplicate the CTE.

### Time selection
- The ONLY predicate is `WHERE <alias>.t BETWEEN <start> AND <end>`, in
  seconds. Both bounds are plain numeric literals.
- One `BETWEEN` per alias per `SELECT`. Join different aliases with `AND`:
  `WHERE a.t BETWEEN 0 AND 5 AND b.t BETWEEN 2 AND 7`.
- No `OR`, no `NOT BETWEEN`, no `<`/`>`/`=`, no expressions as bounds, no
  second range for the same alias.
- The trim applies to that alias everywhere it appears in that `SELECT` --
  every video stream drawn from it and every audio stream drawn from it,
  kept in sync -- and rebases the clip to start at t=0. It works on CTE
  names too.

### Concatenation
- `UNION ALL` concatenates branches in order. Every branch must select the
  same number of columns, the same stream types, in the same order (a
  splatted array column counts one toward that signature per element, so
  branch arrays must also agree on length) -- otherwise `CONCAT_MISMATCH`.
  Branches must also already agree on resolution and frame rate for video
  columns -- `scale(...)` inside a branch if they do not.
- Columns pair up by position, arrays included: a splatted `<alias>.audio` in
  every branch concatenates element 1 with element 1, element 2 with element
  2, and so on, so two dual-language files joined by `UNION ALL` keep both
  language tracks. That is the way to concatenate multi-track sources -- do
  not subscript each track into its own column.
- A concatenated output stream keeps a `language`/`title` tag only when every
  branch's stream in that column carries the SAME one; where they disagree
  (or a branch's stream is untagged) the output carries no tag.
- Plain `UNION` is rejected: deduplication has no streaming meaning.
- Legal at the top level and as a CTE body.

### Arguments
- Calls nest. A `video` parameter takes `<alias>.video[k]`, `<alias>.frame`,
  or another call that returns `video`. An `audio` parameter takes
  `<alias>.audio[k]` or another call that returns `audio`. A bare array in
  either slot broadcasts (see Broadcasting).
- `num` is a bare numeric literal. `0.5`, `.5`, `-10`, `24` are fine.
  `1+1`, `2*w`, `'5'`, `a.t`, and casts are all rejected -- compute the value
  yourself and write the literal.
- `str` is a single-quoted literal; double a quote to escape it (`'it''s'`).
  In Postgres double quotes mean identifier, never string.
- Function names are case-insensitive."""


# ---------------------------------------------------------------------------
# output: COPY ... TO ... WITH (...) (option table rendered from SINK_OPTIONS)
# ---------------------------------------------------------------------------

_OUTPUT_HEADER = """\
## Output

By default a query has no destination: `sqlmpeg compile` writes to `-o` if
given, else a placeholder `out.mp4`; `sqlmpeg run` writes to `-o` if given,
else refuses with an error unless the query names its own destination. To
put the destination and the encoding in the query itself, wrap it in
`COPY (<query>) TO '<path>' WITH (<options>)`:

```sql
COPY (
  SELECT a.video[1], a.audio[1]
  FROM input('clip.mp4') a
) TO 'out.mkv' WITH (
  video_codec 'libx264', crf 20, audio_codec 'aac'
)
```

The query inside `COPY (...)` is a normal query -- everything above still
applies. `<path>` is a single-quoted string literal, on its own `TO` line.
`WITH (...)` is a comma-separated list of `name value` pairs, no `=`: a
single-quoted string for a `str` option (`video_codec 'libx264'`), a bare
integer literal for an `int` option (`crf 20`), or `true`/`false` for a
`bool` option (`faststart true`) -- a bare word with no quotes
(`preset slow`) or a computed value are both rejected. `-o` on the CLI
overrides only the path; it never supplies or overrides options.

### Options

An option applies to every output stream in its scope: a `video` option to
every video output stream, an `audio` option to every audio output stream, a
`container` option once for the whole file -- there is no per-stream
override. Setting a codec option (`video_codec`/`audio_codec`) re-encodes
every output stream of that type, even one that would otherwise be a plain
stream copy."""


def _output_section() -> str:
    lines = [_OUTPUT_HEADER, ""]
    for spec in SINK_OPTIONS.values():
        lines.append(f"- `{spec.name}` ({spec.type}, {spec.scope}) -- {spec.doc}")
    lines.append("")
    lines.append(
        "An option name outside this list is `UNKNOWN_SINK_OPTION`; a value "
        "whose shape does not match the option's type is `SINK_OPTION_TYPE` "
        "-- both are typed, anchored rejections with repair guidance below "
        "(see Repair loop)."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# rejections
# ---------------------------------------------------------------------------

_REJECTED = """\
## Rejected

These are typed errors, never a best-effort graph. Do not reach for them.

- No streaming equivalent: `GROUP BY`, `HAVING`, aggregates (`count`, `sum`,
  `max`, ...), `ORDER BY`, `LIMIT`, `OFFSET`, `DISTINCT`, `QUALIFY`, `WINDOW`,
  window functions (`OVER`), subquery predicates (`IN (SELECT ...)`, `EXISTS`),
  `UNION` without `ALL`.
- Outside the dialect: `SELECT *`, subqueries anywhere (use a CTE), explicit
  `JOIN ... ON` / `USING`, casts (`::` or `CAST`), arithmetic or any non-literal
  in an argument, unqualified columns, schema-qualified tables, aliasing a CTE,
  `WITH RECURSIVE`, nested `WITH`, CTE column lists, table functions other than
  `input()`, any statement that is not a `SELECT`, more than one statement, a
  zero/negative/computed array subscript.
- Any function not listed below, including transitions between concatenated
  branches, motion tracking, subtitle/data-stream filtering, and anything
  requiring more than one pass over the stream."""


# ---------------------------------------------------------------------------
# function reference (generated from stdlib.FUNCTIONS)
# ---------------------------------------------------------------------------

_FUNCTIONS_HEADER = """\
## Functions

The complete stdlib. A `video` parameter takes `<alias>.video[k]`,
`<alias>.frame`, a bare array to broadcast over, or a nested call that
returns `video`; an `audio` parameter takes `<alias>.audio[k]`, a bare array,
or a nested call that returns `audio`. `num` and `str` take literals only.
Overloads are separated by `|` -- match one exactly, on both arity and
argument kinds."""


def _render_signature(name: str, variant: tuple[Param, ...]) -> str:
    params = ", ".join(f"{param.name}: {param.kind}" for param in variant)
    return f"{name}({params})"


def _function_reference() -> str:
    lines = [_FUNCTIONS_HEADER, ""]
    for spec in FUNCTIONS.values():
        signatures = " | ".join(
            _render_signature(spec.name, variant) for variant in spec.variants
        )
        lines.append(f"- `{signatures}`")
        lines.append(f"  {spec.doc}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# worked examples
# ---------------------------------------------------------------------------

# (natural-language request, query). Every query here is compiled by
# tests/test_prompt.py -- an example that does not compile fails the build.
_EXAMPLES: tuple[tuple[str, str], ...] = (
    (
        "Make clip.mp4 half size.",
        "SELECT scale(a.frame, 0.5)\nFROM input('clip.mp4') a",
    ),
    (
        "Crop the 640x360 region at (100, 50) out of clip.mp4 and double it.",
        "SELECT scale(crop(a.frame, 100, 50, 640, 360), 2)\nFROM input('clip.mp4') a",
    ),
    (
        "Keep only the part of clip.mp4 from 5s to 12.5s.",
        "SELECT a.frame\nFROM input('clip.mp4') a\nWHERE a.t BETWEEN 5 AND 12.5",
    ),
    (
        "Put a half-size copy of the scoreboard (the 600x200 box at 1200,50) "
        "in the top-left corner of game.mp4, and mix its audio under the main "
        "feed's at 65/35.",
        "WITH pip AS (\n"
        "  SELECT scale(crop(b.frame, 1200, 50, 600, 200), 0.5) AS frame,\n"
        "         b.audio[1] AS sound\n"
        "  FROM input('game.mp4') b\n"
        ")\n"
        "SELECT overlay(a.frame, pip.frame, 20, 20),\n"
        "       amix(volume(a.audio[1], 0.65), volume(pip.sound, 0.35))\n"
        "FROM input('game.mp4') a, pip",
    ),
    (
        "Keep only the first video stream and the second audio stream of "
        "foo.mp4, untouched.",
        "SELECT a.video[1], a.audio[2]\nFROM input('foo.mp4') a",
    ),
    (
        "Halve the size of clip.mp4's video and keep its first audio track "
        "as-is.",
        "SELECT scale(a.video[1], 0.5), a.audio[1]\nFROM input('clip.mp4') a",
    ),
    (
        "Blur the 160x120 area at (220, 90) in clip.mp4.",
        "SELECT blur_regions(a.frame, 220, 90, 160, 120, 20)\nFROM input('clip.mp4') a",
    ),
    (
        "Outline a red 300x120 box at (40, 40) on clip.mp4 and label it "
        "TAKE 3 at (60, 70) in 36pt.",
        "SELECT text(draw_box(a.frame, 40, 40, 300, 120, 'red'), 'TAKE 3', 60, 70, 36)\n"
        "FROM input('clip.mp4') a",
    ),
    (
        "Play intro.mp4 and then main.mp4 as one video.",
        "SELECT a.frame FROM input('intro.mp4') a\n"
        "UNION ALL\n"
        "SELECT b.frame FROM input('main.mp4') b",
    ),
    (
        "Double-speed the 20-second clip.mp4 with a 1 second fade in and a "
        "1.5 second fade out at the end.",
        # 20s source at 2x -> 10s output; the fade-out window starts at 10 - 1.5.
        "SELECT fade_out(fade_in(speed(a.frame, 2), 1), 1.5, 8.5)\n"
        "FROM input('clip.mp4') a",
    ),
)

# Examples that broadcast a bare array need a real, readable file to know how
# many streams to expand over -- compile_sql cannot size an array symbolically
# (see "Broadcasting" above). tests/test_prompt.py extracts and compiles every
# ```sql fence as a promise that it succeeds standalone against whatever path
# it names, so these are fenced ```sql-probed instead: a distinct tag the
# extractor does not match, deliberately excluding them from that guarantee.
_PROBED_EXAMPLES: tuple[tuple[str, str], ...] = (
    (
        "Add reverb to every audio track in film.mkv, whatever language each "
        "one is in.",
        "SELECT v.video[1], reverb(v.audio, 0.3)\nFROM input('film.mkv') v",
    ),
    (
        "Play episode1.mkv then episode2.mkv as one file, keeping every "
        "language track of both.",
        "SELECT a.frame, a.audio FROM input('episode1.mkv') a\n"
        "UNION ALL\n"
        "SELECT b.frame, b.audio FROM input('episode2.mkv') b",
    ),
    (
        "Put a quarter-size picture-in-picture copy of commentary.mkv in the "
        "corner of film.mkv, and mix every language track of both under it, "
        "the film at 65% and the commentary at 35%.",
        "WITH pip AS (\n"
        "  SELECT scale(c.frame, 0.25) AS frame, c.audio AS sound\n"
        "  FROM input('commentary.mkv') c\n"
        ")\n"
        "SELECT overlay(f.frame, pip.frame, 20, 20),\n"
        "       amix(volume(f.audio, 0.65), volume(pip.sound, 0.35))\n"
        "FROM input('film.mkv') f, pip",
    ),
)

_EXAMPLES_HEADER = """\
## Examples"""

_PROBED_EXAMPLES_NOTE = (
    "The next examples use a bare array (`v.audio`, `a.audio`) -- broadcast "
    "over, or splatted into the SELECT list. Those only compile against a "
    "real, readable file, since sizing the array needs its actual stream "
    "count (see Broadcasting above)."
)


def _examples() -> str:
    lines = [_EXAMPLES_HEADER, ""]
    for task, query in _EXAMPLES:
        lines.append(task)
        lines.append("")
        lines.append("```sql")
        lines.append(query)
        lines.append("```")
        lines.append("")
    lines.append(_PROBED_EXAMPLES_NOTE)
    lines.append("")
    for task, query in _PROBED_EXAMPLES:
        lines.append(task)
        lines.append("")
        lines.append("```sql-probed")
        lines.append(query)
        lines.append("```")
        lines.append("")
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------------
# repair loop (keyed by ErrorCode -- every code must have guidance)
# ---------------------------------------------------------------------------

_REPAIR_HEADER = """\
## Repair loop

Validate every query you produce:

    sqlmpeg validate --json query.sql

Exit 0 with no output means it compiles. Exit 1 prints exactly one JSON object:

```json
{"line": 1, "col": 8, "code": "UDF_ARG_TYPE", "message": "scale() expects \
scale(video, num) | scale(video, num, num), got scale(video, video)", "hint": \
"arguments are stream expressions or literals, in the order shown"}
```

`line` and `col` are 1-based and point at the offending token. `hint` may be
null; when it is present it usually names the exact replacement. Change only
what the error names, then re-validate. Do not rewrite the whole query, and do
not repeat a fix that already failed -- if a construct is rejected twice, it is
not in the dialect."""

# code -> what to change in the SQL. Guidance, not a restatement of the error.
_REPAIR: dict[ErrorCode, str] = {
    ErrorCode.PARSE_ERROR: (
        "Not valid Postgres SQL. Check for unbalanced parentheses, a missing "
        "comma between FROM items, double quotes used for a string, or text "
        "after the statement. Re-emit one well-formed SELECT."
    ),
    ErrorCode.UNKNOWN_FUNCTION: (
        "That name is not in the stdlib. Take the did-you-mean from `hint` if "
        "there is one; otherwise rebuild the effect from the listed functions, "
        "or declare it inexpressible. Do not invent ffmpeg filter names."
    ),
    ErrorCode.UNKNOWN_ALIAS: (
        "The qualifier before the dot is not in this SELECT's FROM. Add "
        "`input('path') <alias>` or the CTE name to that FROM clause; `hint` "
        "lists what is in scope. Names do not cross UNION ALL branches, and a "
        "CTE is in scope only in the branches whose FROM names it."
    ),
    ErrorCode.UDF_ARG_TYPE: (
        "Wrong arity or wrong argument kind. Count the arguments against one "
        "signature in `message` and fix the mismatched slot: `video` needs "
        "`<alias>.video[k]`, `<alias>.frame`, or a nested video-returning call; "
        "`audio` needs `<alias>.audio[k]` or a nested audio-returning call; "
        "`num` needs a bare number; `str` needs a single-quoted literal. "
        "`video`/`audio` in the got-list where the other was expected usually "
        "means you mixed up video and audio; a stream kind where `num` was "
        "expected usually means you passed a stream or `<alias>.t` instead of "
        "a number."
    ),
    ErrorCode.SINGLE_OUTPUT_ONLY: (
        "Reserved; sqlmpeg never raises this code -- a multi-column SELECT is "
        "ordinary usage, one column per output stream. If you see it anyway, "
        "treat it as INTERNAL: re-emit the simplest form of the query."
    ),
    ErrorCode.NO_STREAMING_EQUIVALENT: (
        "Delete the clause named in `message`; there is no filtergraph form for "
        "it. If you used it to pick a time range, use "
        "`WHERE <alias>.t BETWEEN a AND b`. If you used it to pick one branch, "
        "just write that branch."
    ),
    ErrorCode.CONCAT_MISMATCH: (
        "UNION ALL branches disagree. `message` says how: if it names stream "
        "types, column counts, or order, make every branch select the same "
        "columns, the same types, in the same order (a splatted array counts "
        "one column per element, so array lengths must match too); if it is a "
        "resolution/frame-rate mismatch, wrap each branch's video column in "
        "`scale(<expr>, w, h)` with the same w and h everywhere."
    ),
    ErrorCode.STREAM_NOT_FOUND: (
        "The subscript is out of range for the actual stream count named in "
        "`message` (from the probed file, or a CTE array column's recorded "
        "length). Subscripts are 1-based -- lower the number into range, or "
        "select a different alias/column/element."
    ),
    ErrorCode.INPUT_NOT_FOUND: (
        "A bare array (`<alias>.video` / `<alias>.audio` with no subscript, "
        "splatted or handed to a function) needs to read the file to know how "
        "many streams it has, and this input cannot be read (missing path, a "
        "URL, or `--no-probe`). Subscript one specific stream instead -- "
        "`hint` names one -- or point at a path you know is readable."
    ),
    ErrorCode.BROADCAST_MISMATCH: (
        "Two array arguments to the same call have different lengths (both "
        "named in `message`) and cannot zip elementwise. Subscript one of "
        "them down to a single stream, e.g. `<alias>.audio[1]`, so it "
        "broadcasts as a scalar instead, or make both arrays the same length."
    ),
    ErrorCode.UNSUPPORTED_SQL: (
        "The construct is outside the dialect. Read `hint` -- it names the "
        "replacement: a CTE instead of a subquery, a comma instead of "
        "`JOIN ... ON`, a bare CTE name instead of an aliased one, one BETWEEN "
        "per alias, `<alias>.video`/`<alias>.audio` instead of `*` or an "
        "unqualified column, a positive integer literal subscript instead of "
        "zero, a negative number, or a computed index."
    ),
    ErrorCode.UNKNOWN_SINK_OPTION: (
        "The name inside `COPY ... WITH (...)` is not a known sink option. "
        "Take the did-you-mean from `hint` if there is one, otherwise pick "
        "from the exact set of option names sqlmpeg supports; do not invent "
        "an ffmpeg flag name or option that isn't in that set."
    ),
    ErrorCode.SINK_OPTION_TYPE: (
        "The value given for that `COPY ... WITH (...)` option does not "
        "match its expected type, named in `message`. A `str` option needs a "
        "single-quoted literal (e.g. `video_codec 'libx264'`), an `int` "
        "option needs a bare integer literal with no quotes and no decimal "
        "point (e.g. `crf 20`), and a `bool` option needs exactly `true` or "
        "`false` with no quotes."
    ),
    ErrorCode.INTERNAL: (
        "A compiler bug, not your SQL. Re-emit the simplest query that still "
        "expresses the request and report the original as a bug."
    ),
}


def _repair_section() -> str:
    lines = [_REPAIR_HEADER, ""]
    for code in ErrorCode:
        lines.append(f"- `{code.value}` -- {_REPAIR[code]}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------


def build_system_prompt() -> str:
    """The sqlmpeg system prompt: deterministic, pure, ASCII, no trailing newline."""
    sections = (
        _ROLE,
        _DIALECT,
        _output_section(),
        _REJECTED,
        _function_reference(),
        _examples(),
        _repair_section(),
    )
    return "\n\n".join(sections)
