"""The portable LLM system prompt for sqlmpeg (plan 012; plan 032 for ``--dynamic``).

``build_system_prompt()`` returns the text a user hands to whatever model they
like as a system prompt, so that "describe the edit in English, get a runnable
ffmpeg command" works without sqlmpeg ever calling an API itself.

Two properties the no-arg path (``dynamic=None``) must keep:

* **Deterministic and pure.** No I/O, no clock, no environment. The same string
  every call, on every machine -- ``scripts/gen_prompt.py`` commits it to
  ``docs/system-prompt.md`` and a test asserts the committed copy is fresh.
* **Generated from the real surface.** The function reference is rendered from
  :data:`sqlmpeg.stdlib.FUNCTIONS` (guardrail #4) and the repair guidance is
  keyed by :class:`sqlmpeg.errors.ErrorCode`, so a new function or code cannot
  silently go undocumented. Nothing about the stdlib is hardcoded here.

``build_system_prompt(dynamic=registry.load())`` (``sqlmpeg prompt --dynamic``)
appends one more, deliberately impure section: what THIS machine's installed
ffmpeg additionally supports (RFC-003 tier 2), name + pad signature + doc, no
options (the repair loop's ``UNKNOWN_FILTER_OPTION``/``FILTER_OPTION_TYPE``
cover those). It is strictly additive -- every section above it is identical
to the base prompt, which is what keeps the freshness test passing unchanged.

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
from sqlmpeg.inputs import INPUT_OPTIONS
from sqlmpeg.registry import Registry
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

_DIALECT_HEAD = """\
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
- `FROM ffmpeg.<source>(<name> => <value>, ...) alias` GENERATES a stream with
  a zero-input ffmpeg filter -- no file, no `-i`. The `ffmpeg.` namespace is
  mandatory and the alias is too; options are named-only (a source has no
  stream inputs, so nothing is positional) and are validated against the
  installed ffmpeg exactly like a call's named arguments. Video sources:
  `testsrc`, `testsrc2`, `color`, `smptebars`, `nullsrc`. Audio sources:
  `anullsrc` (silence), `sine`, `aevalsrc`. Which ones exist is
  machine-dependent, like everything else under `ffmpeg.`.
- A source alias exposes exactly ONE stream, of a type fixed by the source:
  `t.frame` / `t.video[1]` for a video source, `s.audio[1]` for an audio one,
  the bare `t.video` / `s.audio` as a length-1 array, `t.*` as that one
  column. The other type, or any subscript but `[1]`, is `STREAM_NOT_FOUND`.
  `WHERE <alias>.t` is rejected -- there is nothing to seek; give the source a
  length with its own `duration => <seconds>` option instead.
- Sources are legal beside `input()` aliases, in CTE bodies and in `UNION ALL`
  branches. The last is what they are FOR: `concat` needs every branch to have
  the same stream shape, so a generated video segment joins a clip that has
  audio by pairing it with silence --
  `SELECT t.video[1], s.audio[1] FROM ffmpeg.testsrc2(duration => 1) t,
  ffmpeg.anullsrc(duration => 1) s` as the second branch."""

_INPUT_OPTIONS_HEADER = """\
- `input('path', <name> => <value>, ...)` also takes trailing named options,
  same `=>` syntax as a call's named arguments (RFC-003) -- CASE-SENSITIVE,
  unlike a sink option name. They set ffmpeg's own per-input flags, rendered
  immediately before that input's own `-i`:"""


def _input_options_section() -> str:
    lines = [_INPUT_OPTIONS_HEADER]
    for spec in INPUT_OPTIONS.values():
        lines.append(f"  - `{spec.name}` ({spec.type}) -- {spec.doc}")
    lines.append(
        "  An option name outside this list is `UNKNOWN_INPUT_OPTION`; a "
        "value whose shape does not match the option's type is "
        "`INPUT_OPTION_TYPE` -- both typed and anchored, same as a sink "
        "option's rejections (see Repair loop). Example: a still-image title "
        "card, `input('logo.png', loop => true, framerate => 15)`."
    )
    return "\n".join(lines)


_DIALECT_TAIL = """\
### Columns
- `<alias>.video`, `<alias>.audio`, `<alias>.subtitle`, and `<alias>.data` are
  array-typed pseudo-columns, one entry per stream of that type in the file,
  in file order. `<alias>.video[k]` / `<alias>.audio[k]` / `<alias>.subtitle[k]`
  / `<alias>.data[k]` picks the k-th stream, 1-based (`<alias>.video[1]` is
  the first video stream). `<alias>.frame` is sugar for `<alias>.video[1]`.
- Subscripts are positive integer literals only -- `0`, negative numbers, and
  computed subscripts are rejected.
- A bare `<alias>.video` / `<alias>.audio` / `<alias>.subtitle` / `<alias>.data`
  (no subscript) is the WHOLE array. It is legal splatted directly into the
  SELECT list (one output stream per element, in order) and legal as a
  function argument, where a video/audio array broadcasts (see Broadcasting
  below). Either use needs a readable input to know how many streams there
  are: `sqlmpeg compile` probes local files automatically, but a URL, a
  missing file, or `--no-probe` falls back to a fully symbolic compile, where
  a bare array cannot be sized and is rejected.
- `subtitle` and `data` streams are PASSTHROUGH-ONLY: select them (bare,
  subscripted, splatted, or carried through a CTE column), but never filter
  them. Passing one to any function -- stdlib or a filter beyond the stdlib --
  is `UDF_ARG_TYPE` ("cannot be filtered, only selected"); putting one in a
  `UNION ALL` branch is `UNSUPPORTED_SQL` (ffmpeg's `concat` has video/audio
  pads only). A caption or data track's `language`/`title` tag rides straight
  through to the output, exactly like an untouched audio track's.
- `SELECT *` selects every stream of every `FROM` alias, in `FROM` order and
  file order within each alias, all four types -- each one a plain
  passthrough column, the same as writing every subscript out by hand.
  `<alias>.*` does the same for one alias, and mixes freely with other
  columns: `SELECT a.*, b.audio[1]`. Star over an `input()` alias needs a
  readable file to size it (same policy as a bare array: `INPUT_NOT_FOUND` if
  it cannot be probed); star over a CTE name expands that CTE's recorded
  columns instead, with no probe needed.
- Joining an external subtitle file needs no special syntax: add it as
  another `input()` alias and select its `<alias>.subtitle[1]` alongside the
  rest of the columns. Set `subtitle_codec` (see Output options) to transcode
  it, e.g. `'mov_text'` to carry a `.vtt` track into an `.mp4` container.
- `<alias>.t` is time in seconds. It is legal ONLY inside the `WHERE` form
  below; it is not a stream and cannot appear in the SELECT list.
- There are no other columns.

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
- The supported predicates are `WHERE <alias>.t BETWEEN <start> AND <end>`,
  `<alias>.t >= <start>` (open-ended, no upper bound), and `<alias>.t <= <end>`
  (open-ended, no lower bound), all in seconds. Bounds are plain numeric
  literals. Either operand order works -- `<alias>.t >= 120` and
  `120 <= <alias>.t` are the exact same predicate, not an approximation.
- At most one lower bound and one upper bound per alias per `SELECT`, from any
  combination of the forms above -- `t >= 1 AND t <= 2` means exactly what
  `t BETWEEN 1 AND 2` means. Join different aliases with `AND`:
  `WHERE a.t BETWEEN 0 AND 5 AND b.t >= 2`. A second bound of the same kind
  for one alias (two lower bounds, a BETWEEN plus an overlapping `>=`, ...) is
  rejected, same as writing `BETWEEN` twice.
- No `OR`, no `NOT BETWEEN`, no strict `<`/`>` (seeks are time-based, and a
  strict bound has no frame-level meaning -- use `>=`/`<=`), no `=`, no
  expressions as bounds. A window with both bounds present where the start is
  not strictly before the end is a compile-time `UNSUPPORTED_SQL`, not an
  ffmpeg runtime error.
- On an `input()` alias, the window becomes an INPUT seek (`-ss <start>`
  and/or `-to <end>` immediately before that alias's own `-i`, whichever
  bounds are present), not a filter: it trims AND rebases to t=0 every stream
  of that input, selected or not -- video, audio, subtitle, data alike -- so
  a column nothing else filters can stay a plain stream copy instead of
  forcing a re-encode. A DECODED stream (anything filtered/re-encoded) trims
  frame-accurate; a STREAM-COPIED one snaps back to the preceding keyframe and
  may start up to one GOP early. An open-ended window with no `-to` reads to
  EOF, same as giving ffmpeg no end time at all.
- On a CTE name, the window is still a filtergraph trim (`trim`+`setpts` /
  `atrim`+`asetpts`, with only the present bound(s) as filter args), because a
  CTE's output is a filtergraph pad, not an input -- video/audio only, same as
  before RFC-004.
- Caption caveat: ffmpeg does not retime subtitle/data packets under an
  input seek, so a track that is both inside its own alias's WHERE window
  AND selected in the same query would play out of sync with the rebased
  video -- that combination is rejected (`UNSUPPORTED_SQL`). Trim the alias
  without selecting its subtitle/data columns, or select them from a query
  that puts no WHERE window on that alias; to caption an already-trimmed
  clip, join an external subtitle file whose cues are timed for the cut
  instead (see Columns above).

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
- `expr` is a number, OR a single-quoted ffmpeg expression that ffmpeg
  evaluates per frame: `overlay(f.frame, p.frame, '(W-w)/2', '(H-h)/2')`
  centers the overlay, `crop(f.frame, 0, 0, 'iw/2', 'ih')` keeps the left
  half. The variable vocabulary is per-filter (`iw`/`ih` input size, `W`/`H`
  and `w`/`h` in `overlay`, `t` for time, ...) and is checked by ffmpeg at
  RUN time, not at compile time -- a bad variable name compiles and then
  fails when the command runs, so only use variables you are sure of. Still
  a literal either way: `'(W-w)/2'` is quoted text, not SQL arithmetic.
- Function names are case-insensitive.

### Named arguments
- Any call may take trailing `<name> => <value>` arguments, which set options
  on the ffmpeg filter behind it: `blur(a.frame, 5, planes => 1)`,
  `scale(a.frame, 1280, 720, flags => 'lanczos')`. They come AFTER every
  positional argument, at most one per name.
- Option names are case-sensitive and are exactly ffmpeg's own (`sigmaV`,
  `luma_msize_x`). They are checked against the installed ffmpeg, so a wrong
  name is `UNKNOWN_FILTER_OPTION` with the real list in `hint`, and a wrong
  value is `FILTER_OPTION_TYPE` with the type, range or constants in
  `message`. Values are bare numbers, `true`/`false`, or single-quoted
  strings -- enum options take a quoted constant name, never its number.
- A named argument may not set something the call already sets: the positional
  form wins, and `crop(a.frame, 0, 0, 10, 10, w => 5)` is rejected rather than
  silently overridden. Use the overload that takes the value positionally.
- `enable => '<expression>'` is the one named argument that is not an option of
  the filter: it is ffmpeg's TIMELINE switch, and it turns the filter on and
  off as the stream plays. `blur(a.frame, 5, enable => 'between(t,0.5,1.5)')`
  blurs only that one second; outside it the frames pass through untouched.
  The expression is over `t` (seconds), `n` (frame number) or `pos`, and its
  content is checked by ffmpeg at run time. Only filters your ffmpeg flags
  with timeline support take it -- `gblur`, `drawbox`, `drawtext`, `overlay`,
  `eq` do; `scale`, `pad`, `fps`, `xfade` do not, and asking anyway is
  `UNKNOWN_FILTER_OPTION`, worded to say so. It is never valid on a generated
  source (there is no upstream frame to switch), and, like every named
  argument, it needs the installed ffmpeg.
- A call that expands to more than one ffmpeg filter takes no named arguments
  at all, because there is no single filter to set them on: `blur_regions`,
  and `delay` on a VIDEO stream. `delay` on an AUDIO stream is one filter and
  does take them.
- `overlay` is the one function that cannot take them either: Postgres has a
  builtin `OVERLAY(...)` and `=>` inside it is a `PARSE_ERROR`. Write
  `overlay(base, top, x, y)` positionally, or use the namespaced raw filter
  `ffmpeg.overlay(base, top, x => 20, y => 20, ...)` (see Beyond the stdlib),
  which has no such grammar problem and reaches every overlay option.

### Beyond the stdlib
- Any filter the installed ffmpeg reports can be called directly by its ffmpeg
  name, with its stream inputs positional and every option named:
  `unsharp(a.frame, luma_msize_x => 7, luma_amount => 1.5)`,
  `curves(a.frame, preset => 'lighter')`, `atadenoise(a.frame)`. The number
  and types of the positional arguments are that filter's input pads -- one
  for `V->V` filters, two for `VV->V` ones like `xfade` -- and nothing else is
  positional.
- EVERY filter is also callable as `ffmpeg.<name>(...)`, e.g.
  `ffmpeg.unsharp(a.frame, luma_amount => 1.5)`. That spelling resolves in the
  installed ffmpeg's filter set ONLY -- never the stdlib -- so
  `ffmpeg.scale(a.frame, w => 640, h => -2)` is ffmpeg's own `scale` filter
  rather than the stdlib function of the same name. Use it whenever you want
  the raw filter, and ALWAYS for a name Postgres parses specially: `trim`,
  `format`, `overlay`, `pad`, `normalize`, `reverse`, `median`, `random`,
  `corr`, `copy`, `null`. Bare `trim(a.frame)` is Postgres's string `TRIM` and
  loses the argument; `ffmpeg.trim(a.frame, start => 1)` is the filter.
  `ffmpeg` is a reserved name: never use it as an alias or a CTE name.
- Filters with a variable number of pads (`split`, `concat`) or more than one
  output are NOT callable under either spelling. Neither is a zero-input
  filter: `ffmpeg.testsrc(...)` is a generated SOURCE and belongs in `FROM`
  (see Dialect > Sources), never in the SELECT list.
- This is machine-dependent, and it is the only part of the dialect that is:
  the stdlib above compiles anywhere, while a query naming a filter (or a
  named option) only compiles where that ffmpeg has it. Prefer a stdlib
  function whenever one does the job, and reach for a raw filter name for
  effects the stdlib does not cover. A stdlib name always wins the BARE
  spelling: `scale` is the function above, not ffmpeg's `scale` filter --
  `ffmpeg.scale` is how you ask for the filter.
- If the filter set is unavailable (no ffmpeg, or `--portable`), every such
  call is `UNKNOWN_FUNCTION` and every named argument is `UNSUPPORTED_SQL`;
  the message says which."""


def _dialect_section() -> str:
    """## Dialect, with the input-options bullets (RFC-005 SS4, plan 041)
    rendered from INPUT_OPTIONS spliced in after the Sources bullets."""
    return "\n".join((_DIALECT_HEAD, _input_options_section(), _DIALECT_TAIL))


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
- Outside the dialect: subqueries anywhere (use a CTE), explicit `JOIN ...
  ON` / `USING`, casts (`::` or `CAST`), arithmetic or any non-literal in an
  argument, unqualified columns, schema-qualified tables, aliasing a CTE,
  `WITH RECURSIVE`, nested `WITH`, CTE column lists, table functions other than
  `input()`, any statement that is not a `SELECT`, more than one statement, a
  zero/negative/computed array subscript.
- Any name that is neither a function listed below nor a filter the installed
  ffmpeg provides (see "Beyond the stdlib"), including transitions between
  concatenated branches, motion tracking, subtitle/data-stream filtering, and
  anything requiring more than one pass over the stream.
- A `WHERE` window on an alias whose subtitle/data column is ALSO selected in
  the same query (ffmpeg cannot retime captions under an input seek -- see
  Time selection); a subtitle/data column inside a CTE that also carries a
  `WHERE` window (a CTE trim is a filtergraph trim, which cannot carry
  captions at all)."""


# ---------------------------------------------------------------------------
# function reference (generated from stdlib.FUNCTIONS)
# ---------------------------------------------------------------------------

_FUNCTIONS_HEADER = """\
## Functions

The complete stdlib. A `video` parameter takes `<alias>.video[k]`,
`<alias>.frame`, a bare array to broadcast over, or a nested call that
returns `video`; an `audio` parameter takes `<alias>.audio[k]`, a bare array,
or a nested call that returns `audio`. `num`, `str` and `expr` take literals
only -- `expr` accepting either a number or a quoted ffmpeg expression (see
Dialect > Arguments). Overloads are separated by `|` -- match one exactly, on
both arity and argument kinds."""


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
        "Drop everything before the 90 second mark in clip.mp4; keep the rest.",
        "SELECT a.frame\nFROM input('clip.mp4') a\nWHERE a.t >= 90",
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
        "Center watermark.png over film.mp4, whatever size either of them is.",
        # `expr` slots: ffmpeg evaluates (W-w)/2 against the real frame sizes,
        # so this needs no probe and works for any pair of inputs.
        "SELECT overlay(f.frame, logo.frame, '(W-w)/2', '(H-h)/2')\n"
        "FROM input('film.mp4') f, input('watermark.png') logo",
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
    (
        "Pull the first subtitle track out of film.mkv into its own file.",
        "COPY (SELECT a.subtitle[1] FROM input('film.mkv') a) TO 'subs.en.srt'",
    ),
    (
        "Join subs.en.vtt onto clip.mp4 as a proper subtitle track, mov_text "
        "coded for an mp4 output, video and audio untouched.",
        "COPY (\n"
        "  SELECT a.video[1], a.audio[1], s.subtitle[1]\n"
        "  FROM input('clip.mp4') a, input('subs.en.vtt') s\n"
        ") TO 'out.mp4' WITH (subtitle_codec 'mov_text')",
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
    (
        "Keep absolutely everything in film.mkv untouched -- every video, "
        "audio, subtitle and data stream it has.",
        "SELECT * FROM input('film.mkv') a",
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

    sqlmpeg validate --json "<your query>"

(or `sqlmpeg validate --json -f query.sql` if it is in a file). Exit 0 with
no output means it compiles. Exit 1 prints exactly one JSON object:

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
        "after the statement. One more cause: a `=>` argument inside a call "
        "whose name Postgres parses specially (`overlay`, `trim`, `format`, "
        "...) -- write that call as `ffmpeg.<name>(...)` instead. Re-emit one "
        "well-formed SELECT."
    ),
    ErrorCode.UNKNOWN_FUNCTION: (
        "That name is neither a stdlib function nor a filter the installed "
        "ffmpeg has. Take the did-you-mean from `hint` if there is one -- it "
        "comes back in the spelling that works, so `ffmpeg.<name>()` in the "
        "hint means write it with the namespace. Otherwise rebuild the effect "
        "from the listed functions, or declare it inexpressible. Do not invent "
        "ffmpeg filter names, and do not retry a bare name as "
        "`ffmpeg.<name>` unless the filter really exists: the namespace "
        "changes which tier resolves the name, not which filters exist."
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
        "a number. A got-list that is EMPTY (`trim(), got trim()`) for a call "
        "you did pass arguments to means Postgres claimed the name: rewrite it "
        "as `ffmpeg.<name>(...)`."
    ),
    ErrorCode.SINGLE_OUTPUT_ONLY: (
        "Reserved; sqlmpeg never raises this code -- a multi-column SELECT is "
        "ordinary usage, one column per output stream. If you see it anyway, "
        "treat it as INTERNAL: re-emit the simplest form of the query."
    ),
    ErrorCode.NO_STREAMING_EQUIVALENT: (
        "Delete the clause named in `message`; there is no filtergraph form for "
        "it. If you used it to pick a time range, use "
        "`WHERE <alias>.t BETWEEN a AND b`, `<alias>.t >= a`, or `<alias>.t <= "
        "b`. If you used it to pick one branch, just write that branch."
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
        "`JOIN ... ON`, a bare CTE name instead of an aliased one, one lower "
        "and one upper time bound per alias, `<alias>.video`/`<alias>.audio` "
        "instead of `*` or an unqualified column, a positive integer literal "
        "subscript instead of "
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
    ErrorCode.UNKNOWN_INPUT_OPTION: (
        "The name inside `input('path', ...)`'s trailing `name => value` "
        "arguments is not a known input option. Take the did-you-mean from "
        "`hint` if there is one, otherwise pick from the exact set of option "
        "names sqlmpeg supports (see Dialect > Sources); do not invent an "
        "ffmpeg flag name or option that isn't in that set. Unlike a sink "
        "option name, an input option name is CASE-SENSITIVE."
    ),
    ErrorCode.INPUT_OPTION_TYPE: (
        "The value given for that `input(...)` option does not match its "
        "expected type, named in `message`. A `str` option needs a "
        "single-quoted literal, an `int` option needs a bare integer literal "
        "with no quotes and no decimal point, a `num` option needs a bare "
        "numeric literal (a leading `-` is fine, e.g. `itsoffset => -1`), and "
        "a `bool` option needs exactly `true` or `false` with no quotes."
    ),
    ErrorCode.UNKNOWN_FILTER_OPTION: (
        "A `<name> => <value>` argument names an option the ffmpeg filter does "
        "not have. Take the did-you-mean from `hint` if there is one; otherwise "
        "pick from the option names `hint` lists -- they were read out of the "
        "installed ffmpeg, so they are the complete set for that filter. Never "
        "invent an option name, and never move the value to a positional "
        "argument: a dynamic filter takes only its stream inputs positionally."
    ),
    ErrorCode.FILTER_OPTION_TYPE: (
        "The value of a `<name> => <value>` argument does not match what the "
        "option accepts, and `message` says exactly what it does accept: a bare "
        "number (with the allowed range, if ffmpeg declares one), the bare word "
        "`true`/`false`, or one of a listed set of named constants -- those go "
        "in single quotes, e.g. `transition => 'wipeleft'`. Change the value "
        "only; the option name was accepted."
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
# dynamic filter list (plan 032, opt-in via `sqlmpeg prompt --dynamic`)
# ---------------------------------------------------------------------------
#
# NOT part of the base prompt: this section is appended only when a caller
# passes a Registry to build_system_prompt, so the no-arg path (what
# docs/system-prompt.md pins) is untouched. It lists every filter this
# machine's installed ffmpeg reports -- name, pad signature, one-line doc --
# with no per-filter option dump (the RFC's call: ~460 filters' worth of
# option tables would be enormous; validate --json's UNKNOWN_FILTER_OPTION /
# FILTER_OPTION_TYPE cover that long tail instead).

_DYNAMIC_UNAVAILABLE_NOTE = (
    "No installed ffmpeg was found on PATH (or its filter list could not be "
    "read), so there is no per-machine filter list to append here; \"Beyond "
    "the stdlib\" above still describes the mechanism, it simply has nothing "
    "to compile against on this machine."
)


def _dynamic_filter_line(name: str, registry: Registry) -> str:
    f = registry.get(name)
    assert f is not None  # name came from registry.names() itself
    signature = ", ".join(f.inputs)
    return f"- `{name}({signature}) -> {f.output}` -- {f.doc}"


def _dynamic_section(registry: Registry) -> str:
    if not registry.available():
        return _DYNAMIC_UNAVAILABLE_NOTE
    names = sorted(registry.names())
    lines = [
        f"## Installed filters ({len(names)})",
        "",
        "Every filter this machine's installed ffmpeg reports, beyond the "
        "curated stdlib above -- name, pad signature, one-line description, "
        "sorted alphabetically. This list is machine-dependent, exactly like "
        "the rest of \"Beyond the stdlib\": it is only what THIS ffmpeg "
        "reported when this prompt was generated. Options are never dumped "
        "here -- pass them by name (`<name> => <value>`) as usual and let "
        "`sqlmpeg validate --json` report the real option set on a mistake "
        "(`UNKNOWN_FILTER_OPTION` / `FILTER_OPTION_TYPE`); the repair loop "
        "above covers the rest.",
        "",
    ]
    lines.extend(_dynamic_filter_line(name, registry) for name in names)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------


def build_system_prompt(dynamic: Registry | None = None) -> str:
    """The sqlmpeg system prompt: deterministic, pure, ASCII, no trailing newline.

    With `dynamic=None` (the default) this is exactly the portable, tier-1
    prompt -- pure and machine-independent, byte-identical to
    `docs/system-prompt.md`. Passing a `Registry` (`sqlmpeg prompt --dynamic`)
    appends an "Installed filters" section listing what THIS machine's ffmpeg
    additionally supports; that section is the only impure, machine-dependent
    part of the output, and it never changes anything above it.
    """
    sections: tuple[str, ...] = (
        _ROLE,
        _dialect_section(),
        _output_section(),
        _REJECTED,
        _function_reference(),
        _examples(),
        _repair_section(),
    )
    if dynamic is not None:
        sections = sections + (_dynamic_section(dynamic),)
    return "\n\n".join(sections)
